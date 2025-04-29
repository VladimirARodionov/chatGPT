import logging
from contextlib import contextmanager
from datetime import datetime

from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.orm import Session

from create_bot import db
from models import UserMessageCount, TranscribeQueue

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


def get_queue(user_id: int):
    with get_db_session() as session:
        result = session.query(TranscribeQueue).where(TranscribeQueue.user_id == user_id,
                                              TranscribeQueue.finished == False,
                                              TranscribeQueue.cancelled == False).order_by(TranscribeQueue.id.asc()).all()
        return result

def add_to_queue(user_id: int, file_path: str, file_name: str, file_size_mb:float, message_id: int, chat_id: int):
    with get_db_session() as session:
        item = TranscribeQueue(user_id=user_id,
                               file_path=file_path,
                               file_name=file_name,
                               file_size_mb=file_size_mb,
                               message_id=message_id,
                               chat_id=chat_id,
                               is_active=False,
                               finished=False,
                               cancelled=False)
        session.add(item)
        session.commit()

def set_active_queue(id: int):
    with get_db_session() as session:
        item = session.query(TranscribeQueue).where(TranscribeQueue.id == id).first()
        if item:
            item.is_active = True
            session.add(item)
            session.commit()
            return True
        else:
            return False


def set_finished_queue(id: int):
    with get_db_session() as session:
        item = session.query(TranscribeQueue).where(TranscribeQueue.id == id).first()
        if item:
            item.is_active = False
            item.finished = True
            session.add(item)
            session.commit()
            return True
        else:
            return False


def set_cancelled_queue(id: int):
    with get_db_session() as session:
        item = session.query(TranscribeQueue).where(TranscribeQueue.id == id).first()
        if item:
            item.is_active = False
            item.cancelled = True
            session.add(item)
            session.commit()
            return True
        else:
            return False

def get_first_from_queue():
    with get_db_session() as session:
        first_item = session.query(TranscribeQueue).filter(
            TranscribeQueue.finished == False,
            TranscribeQueue.cancelled == False,
            TranscribeQueue.is_active == False
        ).order_by(TranscribeQueue.id.asc()).first()
        return first_item

def get_all_from_queue():
    with get_db_session() as session:
        all_from_queue = session.query(TranscribeQueue).filter(
            TranscribeQueue.finished == False,
            TranscribeQueue.cancelled == False
        ).order_by(TranscribeQueue.id.asc()).all()
        return all_from_queue

def reset_active_tasks():
    """
    Сбрасывает флаг is_active у всех активных задач.
    Используется при перезапуске приложения, чтобы вернуть активные задачи в очередь.
    """
    with get_db_session() as session:
        # Находим все активные задачи, которые не помечены как завершенные или отмененные
        active_tasks = session.query(TranscribeQueue).filter(
            TranscribeQueue.is_active == True,
            TranscribeQueue.finished == False, 
            TranscribeQueue.cancelled == False
        ).all()
        
        # Сбрасываем флаг активности
        for task in active_tasks:
            task.is_active = False
            session.add(task)
        
        # Сохраняем изменения
        session.commit()
        
        # Возвращаем количество сброшенных задач
        return len(active_tasks)

def get_active_tasks():
    """
    Возвращает все активные задачи, которые не помечены как завершенные или отмененные.
    """
    with get_db_session() as session:
        active_tasks = session.query(TranscribeQueue).filter(
            TranscribeQueue.is_active == True,
            TranscribeQueue.finished == False,
            TranscribeQueue.cancelled == False
        ).order_by(TranscribeQueue.id.asc()).all()
        return active_tasks