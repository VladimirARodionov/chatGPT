import logging.config
import asyncio
import pathlib
from datetime import datetime

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from openai import OpenAI
from sqlalchemy.orm import Session
from sqlalchemy import select
from contextlib import contextmanager

from create_bot import db, env_config
from models import UserMessageCount

# Загрузка переменных окружения
load_dotenv()

alembic_cfg = Config("alembic.ini")
alembic_cfg.attributes['configure_logger'] = False
command.upgrade(alembic_cfg, "head")

logging.config.fileConfig(fname=pathlib.Path(__file__).resolve().parent / 'logging.ini',
                          disable_existing_loggers=False)
logging.getLogger('aiogram.dispatcher').propagate = False
logging.getLogger('aiogram.event').propagate = False


logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=env_config.get('TELEGRAM_TOKEN'))
dp = Dispatcher()


@contextmanager
def get_db_session():
    """контекстный менеджер для сессии БД"""
    session = Session(db)
    try:
        yield session
    except Exception as e:
        logger.exception(str(e))
        session.rollback()
    finally:
        session.close()

def check_message_limit(user_id: int) -> bool:
    """Проверка лимита сообщений для пользователя с использованием БД"""
    current_date = datetime.now().date()
    
    with get_db_session() as session:
        # Поиск записи пользователя
        query = select(UserMessageCount).where(UserMessageCount.user_id == user_id).order_by(UserMessageCount.date.desc())
        result = session.execute(query).scalars().first()
        
        if result is None:
            # Создаем новую запись если пользователь не найден
            user_count = UserMessageCount(user_id=user_id, count=1, date=current_date)
            session.add(user_count)
            session.commit()
            return True
        
        if result.date != current_date:
            # Сбрасываем счетчик если это новый день
            result.count = 1
            result.date = current_date
            session.commit()
            return True
        
        if result.count >= 50:
            # Лимит превышен
            return False
        
        # Увеличиваем счетчик
        result.count += 1
        session.commit()
        return True

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я бот, который может общаться с ChatGPT. Отправь мне сообщение, и я перешлю его в ChatGPT.")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id

    with get_db_session() as session:
        query = select(UserMessageCount).where(UserMessageCount.user_id == user_id).order_by(UserMessageCount.date.desc())
        result = session.execute(query).scalars().first()
        
        if result is None or result.date != datetime.now().date():
            await message.answer("Сегодня вы еще не отправляли сообщений.")
        else:
            remaining = max(0, 50 - result.count)
            await message.answer(f"Сегодня вы отправили {result.count} сообщений. Осталось сообщений: {remaining}.")

@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    
    if not check_message_limit(user_id):
        await message.answer("Вы достигли дневного лимита в 50 сообщений. Попробуйте завтра!")
        return
    
    # Отправляем сообщение о начале обработки
    processing_msg = await message.answer("Обрабатываю ваше сообщение...")
    
    try:
        # Инициализация клиента OpenAI
        client = OpenAI(api_key=env_config.get('OPEN_AI_TOKEN'),
                        max_retries=3,
                        timeout=30
                        )
        # Получаем ответ от ChatGPT
        response = client.chat.completions.create(
            model=env_config.get('MODEL'),
            messages=[
                {"role": "user", "content": message.text}
            ]
        )
        
        # Отправляем ответ пользователю
        await processing_msg.edit_text(response.choices[0].message.content)
        
    except Exception as e:
        await processing_msg.edit_text(f"Произошла ошибка при обработке сообщения: {str(e)}")

async def main():
    logger.info('Бот запущен.')
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        pass
    finally:
        await bot.session.close()
        logger.info('Бот остановлен.')

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Клавиатурное прерывание')
    except asyncio.CancelledError:
        logger.info('Прерывание')
    except Exception:
        logger.exception('Неизвестная ошибка')
