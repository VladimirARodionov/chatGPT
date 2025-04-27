import logging
from contextlib import contextmanager
from datetime import datetime

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.orm import Session

from create_bot import db
from models import UserMessageCount

logger = logging.getLogger(__name__)

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

async def get_cmd_status(message: Message):
    user_id = message.from_user.id

    with get_db_session() as session:
        query = select(UserMessageCount).where(UserMessageCount.user_id == user_id).order_by(UserMessageCount.date.desc())
        result = session.execute(query).scalars().first()

        if result is None or result.date != datetime.now().date():
            await message.answer("Сегодня вы еще не отправляли сообщений.")
        else:
            remaining = max(0, 50 - result.count)
            await message.answer(f"Сегодня вы отправили {result.count} сообщений. Осталось сообщений: {remaining}.")
