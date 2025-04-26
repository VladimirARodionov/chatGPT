import logging.config
import asyncio
import pathlib
import os
from datetime import datetime

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile, BotCommand, BotCommandScopeDefault
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from openai import OpenAI
from sqlalchemy.orm import Session
from sqlalchemy import select
from contextlib import contextmanager

from create_bot import db, env_config
from models import UserMessageCount
from audio_utils import transcribe_with_whisper, convert_audio_format, list_downloaded_models

# Загрузка переменных окружения
load_dotenv()

# Инициализация Alembic для миграций базы данных
alembic_cfg = Config("alembic.ini")
alembic_cfg.attributes['configure_logger'] = False
command.upgrade(alembic_cfg, "head")

# Настройка логирования
logging.config.fileConfig(fname=pathlib.Path(__file__).resolve().parent / 'logging.ini',
                          disable_existing_loggers=False)
logging.getLogger('aiogram.dispatcher').propagate = False
logging.getLogger('aiogram.event').propagate = False

logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=env_config.get('TELEGRAM_TOKEN'))
dp = Dispatcher()

# Настройки для Whisper
WHISPER_MODEL = env_config.get('WHISPER_MODEL', 'base')
USE_LOCAL_WHISPER = env_config.get('USE_LOCAL_WHISPER', 'True').lower() in ('true', '1', 'yes')
WHISPER_MODELS_DIR = env_config.get('WHISPER_MODELS_DIR', 'whisper_models')

# Директории для файлов
TEMP_AUDIO_DIR = "temp_audio"
TRANSCRIPTION_DIR = "transcriptions"

# Создаем директории, если они не существуют
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
os.makedirs(TRANSCRIPTION_DIR, exist_ok=True)
os.makedirs(WHISPER_MODELS_DIR, exist_ok=True)

# Список команд бота для меню
BOT_COMMANDS = [
    BotCommand(command="start", description="Начать общение с ботом"),
    BotCommand(command="help", description="Показать справку"),
    BotCommand(command="status", description="Проверить лимит сообщений"),
    BotCommand(command="models", description="Показать список моделей Whisper"),
    BotCommand(command="menu", description="Показать главное меню")
]

async def set_commands():
    """Установка команд бота в меню"""
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())

def get_main_keyboard():
    """Создает клавиатуру главного меню"""
    builder = ReplyKeyboardBuilder()
    builder.row(
        types.KeyboardButton(text="💬 Помощь"),
        types.KeyboardButton(text="📊 Статус")
    )
    builder.row(
        types.KeyboardButton(text="🎤 Модели Whisper"),
        types.KeyboardButton(text="ℹ️ О боте")
    )
    return builder.as_markup(resize_keyboard=True)

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

def save_transcription_to_file(text, user_id):
    """Сохраняет транскрибированный текст в файл
    
    Args:
        text: Текст транскрибации
        user_id: ID пользователя
        
    Returns:
        Путь к сохраненному файлу
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{TRANSCRIPTION_DIR}/transcription_{user_id}_{timestamp}.txt"
    
    with open(filename, "w", encoding="utf-8") as file:
        file.write(text)
    
    return filename

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот, который может общаться с ChatGPT и транскрибировать аудио.\n\n"
        "Отправь мне сообщение или аудиофайл, и я обработаю его. "
        "Используй меню для доступа к основным функциям."
    )

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

@dp.message(Command("models"))
async def cmd_models(message: types.Message):
    """Отображает список скачанных моделей Whisper"""
    try:
        models = list_downloaded_models()
        
        if not models:
            await message.answer("Нет скачанных моделей Whisper. Модели будут скачаны автоматически при первом использовании.")
            return
        
        # Формируем сообщение
        models_text = "📚 Скачанные модели Whisper:\n\n"
        for model in models:
            models_text += f"• {model['name']} ({model['size_mb']} MB)\n"
        
        models_text += f"\nТекущая модель: {WHISPER_MODEL}"
        models_text += f"\nДиректория моделей: {WHISPER_MODELS_DIR}"
        
        await message.answer(models_text)
    except Exception as e:
        logger.exception(f"Ошибка при получении списка моделей: {e}")
        await message.answer(f"Произошла ошибка при получении списка моделей: {str(e)}")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = """
🤖 <b>Возможности бота:</b>

• Отправьте текстовое сообщение для получения ответа от ChatGPT
• Отправьте аудиофайл (голосовое сообщение или аудио) для его транскрибации
• При транскрибации аудио вы получите текст и файл с транскрибацией
• Лимит: 50 сообщений в сутки

<b>Команды бота:</b>
/start - Начать общение с ботом
/menu - Показать главное меню
/status - Проверить лимит сообщений
/models - Показать список моделей Whisper
/help - Показать эту справку

<b>Текущие настройки:</b>
• Используемая модель для транскрибации: <code>%s</code>
• Режим транскрибации: <code>%s</code>
"""
    transcribe_mode = "Локальная модель Whisper" if USE_LOCAL_WHISPER else "OpenAI API"
    await message.answer(help_text % (WHISPER_MODEL, transcribe_mode), parse_mode="HTML")

@dp.message(lambda message: message.text == "💬 Помощь")
async def button_help(message: types.Message):
    await cmd_help(message)

@dp.message(lambda message: message.text == "📊 Статус")
async def button_status(message: types.Message):
    await cmd_status(message)

@dp.message(lambda message: message.text == "🎤 Модели Whisper")
async def button_models(message: types.Message):
    await cmd_models(message)

@dp.message(lambda message: message.text == "ℹ️ О боте")
async def button_about(message: types.Message):
    about_text = """
<b>📱 ChatGPT Бот с транскрибацией</b>

Этот бот позволяет общаться с ChatGPT и транскрибировать аудиосообщения с помощью технологии Whisper.

<b>Технологии:</b>
• OpenAI ChatGPT для обработки текстовых запросов
• Whisper для транскрибации аудио

<b>Особенности:</b>
• Поддержка транскрибации голосовых сообщений
• Сохранение транскрибаций в текстовые файлы
• Кеширование моделей между запусками
• Ограничение в 50 сообщений в сутки
"""
    await message.answer(about_text, parse_mode="HTML")

async def download_voice(file, destination):
    """Скачивание голосового сообщения"""
    try:
        await bot.download(file, destination=destination)
        return True
    except Exception as e:
        logger.exception(f"Ошибка при скачивании файла: {e}")
        return False

async def transcribe_audio(file_path, use_local_whisper=USE_LOCAL_WHISPER):
    """Транскрибация аудио с использованием OpenAI API или локальной модели Whisper"""
    try:
        if use_local_whisper:
            # Конвертируем в нужный формат для Whisper если нужно
            converted_file = await convert_audio_format(file_path)
            
            # Используем локальную модель Whisper
            transcription = await transcribe_with_whisper(
                converted_file, 
                model_name=WHISPER_MODEL
            )
            
            # Удаляем конвертированный файл если он отличается от оригинала
            if converted_file != file_path:
                try:
                    os.remove(converted_file)
                except Exception as e:
                    logger.error(f"Ошибка при удалении временного файла: {e}")
                
            return transcription
        else:
            # Используем OpenAI API
            client = OpenAI(api_key=env_config.get('OPEN_AI_TOKEN'),
                        max_retries=3,
                        timeout=30)
            
            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model="whisper-1", 
                    file=audio_file
                )
            return transcription.text
    except Exception as e:
        logger.exception(f"Ошибка при транскрибации: {e}")
        raise

@dp.message(lambda message: message.voice or message.audio)
async def handle_audio(message: types.Message):
    user_id = message.from_user.id

    if not USE_LOCAL_WHISPER and not check_message_limit(user_id):
        await message.answer("Вы достигли дневного лимита в 50 сообщений. Попробуйте завтра!")
        return

    # Отправляем сообщение о начале обработки
    processing_msg = await message.answer("Загружаю и обрабатываю аудио...")
    
    try:
        # Определяем, что за файл пришел
        file_id = message.voice.file_id if message.voice else message.audio.file_id
        
        # Имя исходного файла
        file_name = "Голосовое сообщение"
        if message.audio and message.audio.file_name:
            file_name = message.audio.file_name
        
        # Путь для сохранения аудио
        file_path = f"{TEMP_AUDIO_DIR}/audio_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.ogg"
        file = await bot.get_file(file_id)
        
        # Скачиваем файл
        await processing_msg.edit_text("Скачиваю аудиофайл...")
        if not await download_voice(file, file_path):
            await processing_msg.edit_text("Ошибка при скачивании аудиофайла.")
            return
        
        # Транскрибируем аудио
        await processing_msg.edit_text(f"Транскрибирую аудио {'с помощью локального Whisper' if USE_LOCAL_WHISPER else 'через OpenAI API'}...")
        transcription = await transcribe_audio(file_path)
        
        # Сохраняем транскрибацию в файл
        transcript_file_path = save_transcription_to_file(transcription, user_id)
        
        # Формируем текстовое сообщение
        message_text = f"🎤 Транскрибация аудио: {file_name}\n\n"
        # Если текст слишком длинный, обрезаем его для предпросмотра
        preview_text = transcription[:1000] + "..." if len(transcription) > 1000 else transcription
        message_text += preview_text
        
        # Отправляем текстовое сообщение
        await processing_msg.edit_text(message_text)
        
        # Отправляем файл с полной транскрибацией
        await message.answer_document(
            FSInputFile(transcript_file_path),
            caption="Полная транскрибация аудио"
        )
        
        # Удаляем временные файлы
        try:
            os.remove(file_path)
            # Оставляем файл с транскрибацией, можно настроить его автоматическое удаление позже
            # os.remove(transcript_file_path)
        except Exception as e:
            logger.exception(f"Ошибка при удалении временных файлов: {e}")
        
    except Exception as e:
        await processing_msg.edit_text(f"Произошла ошибка при обработке аудио: {str(e)}")

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
        logger.exception(f"Произошла ошибка при обработке сообщения: {e}")
        await processing_msg.edit_text(f"Произошла ошибка при обработке сообщения: {str(e)}")

async def main():
    logger.info('Бот запущен.')
    try:
        logger.info(f'Используемая модель Whisper: {WHISPER_MODEL}')
        logger.info(f'Директория для моделей Whisper: {WHISPER_MODELS_DIR}')
        
        # Устанавливаем команды в меню бота
        await set_commands()
        logger.info('Команды бота установлены')
        
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
