import logging.config
import asyncio
import pathlib
import os
from datetime import datetime, timedelta
import re
from concurrent.futures import ThreadPoolExecutor
import json
import time
import shutil
import signal

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile, BotCommand, BotCommandScopeDefault, ReplyKeyboardRemove
from aiogram.exceptions import TelegramBadRequest
from openai import OpenAI
from sqlalchemy.orm import Session
from sqlalchemy import select
from contextlib import contextmanager
import aiohttp

from create_bot import db, env_config, superusers
from models import UserMessageCount
from audio_utils import transcribe_with_whisper, convert_audio_format, list_downloaded_models, should_use_smaller_model, predict_processing_time

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

# Получаем URL локального Bot API сервера из .env файла
LOCAL_BOT_API = env_config.get('LOCAL_BOT_API', None)
# Путь к директории с файлами Local Bot API на локальной файловой системе
LOCAL_BOT_API_FILES_PATH = env_config.get('LOCAL_BOT_API_FILES_PATH', 'telegram_bot_api_data')

# Инициализация бота и диспетчера
if LOCAL_BOT_API:
    bot = Bot(token=env_config.get('TELEGRAM_TOKEN'), base_url=LOCAL_BOT_API)
    logger.info(f'Используется локальный Telegram Bot API сервер: {LOCAL_BOT_API}')
    if os.path.exists(LOCAL_BOT_API_FILES_PATH):
        logger.info(f'Директория с файлами Local Bot API доступна: {LOCAL_BOT_API_FILES_PATH}')
    else:
        logger.warning(f'Директория с файлами Local Bot API недоступна: {LOCAL_BOT_API_FILES_PATH}')
else:
    bot = Bot(token=env_config.get('TELEGRAM_TOKEN'))
dp = Dispatcher()

# Настройки для Whisper
WHISPER_MODEL = env_config.get('WHISPER_MODEL', 'base')
USE_LOCAL_WHISPER = env_config.get('USE_LOCAL_WHISPER', 'True').lower() in ('true', '1', 'yes')
WHISPER_MODELS_DIR = env_config.get('WHISPER_MODELS_DIR', 'whisper_models')

# Директории для файлов
TEMP_AUDIO_DIR = "temp_audio"
TRANSCRIPTION_DIR = "transcriptions"

# Ограничения для Telegram
MAX_MESSAGE_LENGTH = 4096  # максимальная длина сообщения в Telegram
MAX_CAPTION_LENGTH = 1024  # максимальная длина подписи к файлу

# Устанавливаем лимиты в зависимости от использования локального Bot API
STANDARD_API_LIMIT = 20 * 1024 * 1024  # 20 МБ для обычного Bot API
MAX_FILE_SIZE = STANDARD_API_LIMIT

if LOCAL_BOT_API:
    MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2000 МБ для локального Bot API
    logger.info(f'Используется увеличенный лимит файлов: {MAX_FILE_SIZE/1024/1024:.1f} МБ')
else:
    logger.info(f'Используется стандартный лимит файлов: {MAX_FILE_SIZE/1024/1024:.1f} МБ')

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
    BotCommand(command="cancel", description="Отменить текущую обработку аудио"),
    BotCommand(command="queue", description="Показать очередь задач"),
]

# Создаем очередь задач для обработки аудио в фоновом режиме
audio_task_queue = asyncio.Queue()
# Пул потоков для CPU-интенсивных операций
thread_executor = ThreadPoolExecutor(max_workers=3)

# Флаг для отслеживания статуса обработчика очереди
background_worker_running = False

# Словарь для отслеживания активных задач транскрибации по пользователям
# Ключ - user_id, значение - (future, message_id, file_path)
active_transcriptions = {}

# Путь для сохранения очереди
QUEUE_SAVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_queue.json")

async def set_commands():
    """Установка команд бота в меню"""
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())


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

def save_transcription_to_file(text, user_id, original_file_name=None, username=None, first_name=None, last_name=None):
    """Сохраняет транскрибированный текст в файл
    
    Args:
        text: Текст транскрибации или словарь с результатами
        user_id: ID пользователя
        original_file_name: Оригинальное имя файла (если доступно)
        username: Ник пользователя в Telegram
        first_name: Имя пользователя
        last_name: Фамилия пользователя
        
    Returns:
        Путь к сохраненному файлу
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{TRANSCRIPTION_DIR}/transcription_{user_id}_{timestamp}.txt"
    
    # Обрабатываем разные форматы результатов транскрибации
    if isinstance(text, dict):
        # Если результат в формате словаря Whisper, извлекаем текст и дополнительные данные
        transcription_text = text.get('text', '')
        language = text.get('language', 'Не определен')
        segments = text.get('segments', [])
        
        with open(filename, "w", encoding="utf-8") as file:
            file.write(f"Транскрибация аудио\n")
            file.write(f"Дата и время: {timestamp}\n")
            file.write(f"Язык: {language}\n")
            file.write(f"ID пользователя: {user_id}\n")
            # Добавляем информацию о пользователе
            if username:
                file.write(f"Username: @{username}\n")
            if first_name or last_name:
                user_fullname = f"{first_name or ''} {last_name or ''}".strip()
                file.write(f"Имя: {user_fullname}\n")
            if original_file_name and original_file_name != "Голосовое сообщение":
                file.write(f"Файл: {original_file_name}\n")
            file.write("\n=== ПОЛНЫЙ ТЕКСТ ===\n\n")
            
            # Разделяем текст на абзацы
            paragraphs = transcription_text.replace('. ', '.\n').replace('! ', '!\n').replace('? ', '?\n')
            file.write(paragraphs)
            
            # Если есть сегменты, добавляем детальную информацию с таймкодами
            if segments:
                file.write("\n\n=== ДЕТАЛЬНАЯ ТРАНСКРИБАЦИЯ С ТАЙМКОДАМИ ===\n\n")
                for i, segment in enumerate(segments):
                    start = segment.get('start', 0)
                    end = segment.get('end', 0)
                    segment_text = segment.get('text', '')
                    file.write(f"[{format_timestamp(start)} --> {format_timestamp(end)}] {segment_text}\n")
                
                # Создаем SRT-файл для субтитров, если есть сегменты
                srt_filename = f"{TRANSCRIPTION_DIR}/transcription_{user_id}_{timestamp}.srt"
                save_srt_file(segments, srt_filename)
                logger.info(f"Создан SRT-файл субтитров: {srt_filename}")
    else:
        # Просто сохраняем текст, если это строка или другой формат, также разделяя на абзацы
        text_str = str(text)
        paragraphs = text_str.replace('. ', '.\n').replace('! ', '!\n').replace('? ', '?\n')
        
        with open(filename, "w", encoding="utf-8") as file:
            file.write(f"Транскрибация аудио\n")
            file.write(f"Дата и время: {timestamp}\n")
            file.write(f"ID пользователя: {user_id}\n")
            # Добавляем информацию о пользователе
            if username:
                file.write(f"Username: @{username}\n")
            if first_name or last_name:
                user_fullname = f"{first_name or ''} {last_name or ''}".strip()
                file.write(f"Имя: {user_fullname}\n")
            if original_file_name and original_file_name != "Голосовое сообщение":
                file.write(f"Файл: {original_file_name}\n")
            file.write("\n=== ПОЛНЫЙ ТЕКСТ ===\n\n")
            file.write(paragraphs)
    
    return filename

def save_srt_file(segments, filename):
    """Сохраняет сегменты транскрибации в формате SRT (SubRip Subtitle)
    
    Args:
        segments: Список сегментов из транскрибации Whisper
        filename: Путь для сохранения SRT-файла
        
    Returns:
        Путь к сохраненному файлу
    """
    try:
        with open(filename, "w", encoding="utf-8") as file:
            for i, segment in enumerate(segments, 1):
                start = segment.get('start', 0)
                end = segment.get('end', 0)
                segment_text = segment.get('text', '').strip()
                
                # Формат SRT требует:
                # 1. Порядковый номер
                # 2. Временной интервал в формате ЧЧ:ММ:СС,ммм --> ЧЧ:ММ:СС,ммм
                # 3. Текст субтитров
                # 4. Пустая строка для разделения записей
                
                # Записываем в файл в формате SRT
                file.write(f"{i}\n")
                file.write(f"{format_timestamp(start)} --> {format_timestamp(end)}\n")
                file.write(f"{segment_text}\n\n")
        
        return filename
    except Exception as e:
        logger.exception(f"Ошибка при создании SRT-файла: {e}")
        return None

def format_timestamp(seconds):
    """Форматирует время в секундах в формат часы:минуты:секунды,миллисекунды"""
    milliseconds = int((seconds % 1) * 1000)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

def split_text_into_chunks(text, max_length=MAX_MESSAGE_LENGTH):
    """Разделяет длинный текст на части с учетом границ предложений
    
    Args:
        text: Исходный текст
        max_length: Максимальная длина каждой части
        
    Returns:
        Список частей текста
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    
    # Регулярное выражение для поиска конца предложения
    sentence_end = re.compile(r'[.!?]\s+')
    
    # Сначала разбиваем по предложениям
    sentences = sentence_end.split(text)
    
    # Если есть очень длинные предложения, разбиваем их дополнительно
    for i, sentence in enumerate(sentences):
        if len(sentence) > max_length:
            # Если предложение слишком длинное, разбиваем его по словам
            words = sentence.split()
            current_chunk = ""
            
            for word in words:
                if len(current_chunk) + len(word) + 1 <= max_length:
                    if current_chunk:
                        current_chunk += " "
                    current_chunk += word
                else:
                    chunks.append(current_chunk)
                    current_chunk = word
            
            if current_chunk:
                chunks.append(current_chunk)
        else:
            # Добавляем точку обратно, если это не последнее предложение
            end_mark = ". " if i < len(sentences) - 1 else ""
            
            # Проверяем, можно ли добавить это предложение к последнему чанку
            if chunks and len(chunks[-1]) + len(sentence) + len(end_mark) <= max_length:
                chunks[-1] += sentence + end_mark
            else:
                chunks.append(sentence + end_mark)
    
    return chunks

async def send_file_safely(message, file_path, caption=None):
    """Безопасно отправляет файл с обработкой ошибок и разделением больших файлов
    
    Args:
        message: Исходное сообщение для ответа
        file_path: Путь к файлу
        caption: Подпись к файлу
        
    Returns:
        Успешность отправки
    """
    try:
        file_size = os.path.getsize(file_path)
        
        if file_size > MAX_FILE_SIZE:
            # Файл слишком большой, разделяем его на части
            await message.answer("Файл слишком большой для отправки, разделяю на части...")
            
            # Читаем содержимое файла
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Разделяем содержимое на части
            chunks = split_text_into_chunks(content, MAX_MESSAGE_LENGTH - 100)  # Оставляем запас
            
            # Создаем отдельные файлы для каждой части
            for i, chunk in enumerate(chunks):
                part_filename = f"{os.path.splitext(file_path)[0]}_part{i+1}{os.path.splitext(file_path)[1]}"
                
                with open(part_filename, 'w', encoding='utf-8') as f:
                    f.write(chunk)
                
                # Формируем подпись для каждой части
                part_caption = f"Часть {i+1}/{len(chunks)}"
                if i == 0 and caption:
                    part_caption = f"{caption}\n\n{part_caption}"
                
                # Отправляем файл
                await message.answer_document(
                    FSInputFile(part_filename),
                    caption=part_caption[:MAX_CAPTION_LENGTH]
                )
            
            return True
        else:
            # Обычная отправка файла
            if caption and len(caption) > MAX_CAPTION_LENGTH:
                caption = caption[:MAX_CAPTION_LENGTH-3] + "..."
                
            await message.answer_document(
                FSInputFile(file_path),
                caption=caption
            )
            return True
            
    except TelegramBadRequest as e:
        if "file is too big" in str(e).lower():
            # Если все равно получаем ошибку о большом размере файла
            logger.error(f"Файл {file_path} слишком большой для отправки через Telegram API: {e}")
            await message.answer(
                "Файл слишком большой для отправки через Telegram. "
                "Попробуйте транскрибировать аудио меньшей длительности."
            )
        else:
            logger.exception(f"Ошибка Telegram при отправке файла: {e}")
            await message.answer(f"Ошибка при отправке файла: {str(e)}")
        return False
    except Exception as e:
        logger.exception(f"Ошибка при отправке файла: {e}")
        await message.answer(f"Произошла ошибка при отправке файла: {str(e)}")
        return False

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот, который может общаться с ChatGPT и транскрибировать аудио.\n\n"
        "Отправь мне сообщение или аудиофайл, и я обработаю его. "
        "Используй меню для доступа к основным функциям."
    , reply_markup=ReplyKeyboardRemove())

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
        
        # Группируем модели по имени для более наглядного отображения
        grouped_models = {}
        for model in models:
            name = model['name']
            if name not in grouped_models:
                grouped_models[name] = []
            grouped_models[name].append(model)
        
        # Формируем текст списка моделей
        for name, model_variants in grouped_models.items():
            # Выбираем самую большую/последнюю версию модели для отображения размера
            latest_model = max(model_variants, key=lambda m: m.get('file_size_mb', m.get('size_mb', 0)))
            size_mb = latest_model.get('size_mb', 0)
            
            if len(model_variants) > 1:
                # Если есть несколько вариантов одной модели
                variant_info = ", ".join([os.path.basename(m['path']) for m in model_variants])
                models_text += f"• {name} ({size_mb} MB) - {variant_info}\n"
            else:
                # Если только один вариант
                file_path = os.path.basename(latest_model['path'])
                models_text += f"• {name} ({size_mb} MB) - {file_path}\n"
        
        models_text += f"\nТекущая модель: {WHISPER_MODEL}"
        models_text += f"\nДиректория моделей: {WHISPER_MODELS_DIR}"
        
        await message.answer(models_text)
    except Exception as e:
        logger.exception(f"Ошибка при получении списка моделей: {e}")
        await message.answer(f"Произошла ошибка при получении списка моделей: {str(e)}")

@dp.message(Command("queue"))
async def cmd_queue(message: types.Message):
    """Отображает текущую очередь задач на обработку аудио"""
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    #if user_id not in superusers:
    #    await message.answer("⚠️ У вас нет прав для просмотра очереди задач.")
    #queue    return

    # Получаем текущую очередь задач
    queue_size = audio_task_queue.qsize()

    if queue_size == 0 and not active_transcriptions:
        await message.answer("🟢 Очередь пуста. Нет активных задач на обработке.")
        return

    # Формируем информацию о текущей очереди
    queue_info = f"📋 <b>Статус очереди обработки аудио:</b>\n\n"

    # Информация об активных задачах транскрибации
    if active_transcriptions:
        queue_info += f"🔄 <b>Активные задачи ({len(active_transcriptions)}):</b>\n"
        for user_id, (future, message_id, file_path) in active_transcriptions.items():
            if future == "cancelled":
                status = "⏹ Отменяется"
            else:
                status = "▶️ Обрабатывается"

            file_name = os.path.basename(file_path)
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024) if os.path.exists(file_path) else 0

            queue_info += f"- Пользователь: <code>{user_id}</code>, {status}\n"
            queue_info += f"  Файл: {file_name} ({file_size_mb:.2f} МБ)\n"

        queue_info += "\n"

    # Получаем задачи из очереди (не удаляя их)
    if queue_size > 0:
        queue_info += f"⏳ <b>В очереди ожидания ({queue_size}):</b>\n"

        # Нельзя напрямую перебрать асинхронную очередь, создадим временный список для отображения
        queue_list = []
        unfinished = audio_task_queue._unfinished_tasks

        # Если есть задачи, указываем их количество
        if unfinished > 0:
            queue_info += f"- Количество задач в очереди: {unfinished}\n"
        else:
            queue_info += "- Очередь пуста или невозможно получить детальную информацию\n"

    # Добавляем информацию о состоянии фоновых процессов
    queue_info += f"\n🖥 <b>Системная информация:</b>\n"
    queue_info += f"- Фоновый обработчик: {'Работает' if background_worker_running else 'Остановлен'}\n"
    queue_info += f"- Рабочих потоков: {thread_executor._max_workers}\n"

    # Отправляем информацию
    await message.answer(queue_info, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = """
🤖 <b>Возможности бота:</b>

• Отправьте текстовое сообщение для получения ответа от ChatGPT
• Отправьте аудиофайл (голосовое сообщение или аудио) для его транскрибации
• При транскрибации аудио вы получите текст и файл с транскрибацией
• Бот создает файл субтитров (SRT) с таймкодами для использования в видеоредакторах
• Лимит: 50 сообщений в сутки

<b>Команды бота:</b>
/start - Начать общение с ботом
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
• Создание файлов субтитров (SRT) по таймкодам
• Кеширование моделей между запусками
• Ограничение в 50 сообщений в сутки
"""
    await message.answer(about_text, parse_mode="HTML")

@dp.message(lambda message: message.text == "🔍 Очередь")
async def button_queue(message: types.Message):
    """Обработчик кнопки 'Очередь'"""
    # Используем тот же обработчик, что и для команды /queue
    await cmd_queue(message)

async def download_voice(file, destination):
    """Скачивание голосового сообщения"""
    try:
        # Проверяем, существует ли директория
        directory = os.path.dirname(destination)
        if not os.path.exists(directory):
            try:
                os.makedirs(directory, exist_ok=True)
                logger.info(f"Создана директория: {directory}")
            except PermissionError:
                logger.error(f"Нет прав для создания директории: {directory}")
                return False
        
        # Проверяем права на запись в директорию
        if not os.access(directory, os.W_OK):
            logger.error(f"Нет прав на запись в директорию: {directory}")
            return False
            
        # Скачиваем файл
        await bot.download(file, destination=destination)
        
        # Проверяем, скачался ли файл
        if os.path.exists(destination):
            logger.info(f"Файл успешно скачан: {destination}")
            return True
        else:
            logger.error(f"Файл не был скачан: {destination}")
            return False
            
    except PermissionError as e:
        logger.error(f"Ошибка доступа при скачивании файла: {e}")
        # Отладочная информация о правах
        try:
            directory = os.path.dirname(destination)
            logger.error(f"Права доступа к директории {directory}: {oct(os.stat(directory).st_mode)[-3:]}")
            logger.error(f"Владелец директории: {os.stat(directory).st_uid}:{os.stat(directory).st_gid}")
            current_user = os.getuid()
            current_group = os.getgid()
            logger.error(f"Текущий пользователь: {current_user}:{current_group}")
        except Exception as debug_e:
            logger.error(f"Ошибка при получении отладочной информации: {debug_e}")
        return False
    except Exception as e:
        logger.exception(f"Ошибка при скачивании файла: {e}")
        return False

async def get_file_path_direct(file_id, bot_token, return_full_info=False):
    """
    Получает прямой путь к файлу на сервере Telegram.
    
    Args:
        file_id: ID файла в Telegram
        bot_token: Токен бота для авторизации
        return_full_info: Возвращает полную информацию о файле
        
    Returns:
        str: Путь к файлу на сервере Telegram или None в случае ошибки
        dict: Полная информация о файле, если return_full_info=True
    """
    logger.info(f"Получаем информацию о файле с ID {file_id}")
    
    # URL для получения информации о файле
    url = f"{LOCAL_BOT_API}/bot{bot_token}/getFile"
    
    try:
        async with aiohttp.ClientSession() as session:
            # Используем POST-запрос с JSON данными
            logger.info(f"Отправляем запрос к Local Bot API: {url}")
            async with session.post(url, json={'file_id': file_id}) as response:
                if response.status != 200:
                    response_text = await response.text()
                    logger.error(f"Ошибка при получении информации о файле. Статус: {response.status}. "
                                f"Ответ: {response_text}")
                    return None
                
                json_response = await response.json()
                logger.debug(f"Получен ответ от API: {json_response}")
                
                if not json_response.get('ok'):
                    logger.error(f"API вернул ошибку: {json_response}")
                    return None
                
                file_info = json_response.get('result', {})
                file_path = file_info.get('file_path')
                
                if not file_path:
                    logger.error(f"Не удалось получить путь к файлу: {json_response}")
                    return None
                
                # Пути могут приходить в разных форматах от API
                logger.info(f"Получен путь к файлу: {file_path}")
                
                # Для Local Bot API может приходить полный путь к файлу
                # Мы возвращаем его как есть, а обработка происходит в download_large_file_direct
                if return_full_info:
                    return file_info
                else:
                    return file_path
                
    except Exception as e:
        logger.exception(f"Ошибка при получении информации о файле: {e}")
        return None

async def download_large_file_direct(file_id, destination, bot_token):
    """
    Загружает файл напрямую с сервера Local Bot API, обходя ограничения 
    стандартного API Telegram. Поддерживает файлы до 100МБ.
    
    Args:
        file_id: ID файла в Telegram
        destination: Путь, куда сохранить файл
        bot_token: Токен бота для авторизации
        
    Returns:
        bool: True если загрузка прошла успешно, False в противном случае
    """
    # Получаем информацию о пути к файлу
    file_path = await get_file_path_direct(file_id, bot_token)
    if not file_path:
        logger.error(f"Не удалось получить путь к файлу {file_id}")
        return False
    
    # Проверяем доступность файла через API
    file_info = await get_file_path_direct(file_id, bot_token, return_full_info=True)
    if file_info and 'file_size' in file_info:
        file_size = file_info['file_size']
        logger.info(f"Размер загружаемого файла (из API): {file_size/1024/1024:.2f} МБ")
        
        # Проверяем размер файла
        if file_size > MAX_FILE_SIZE:
            logger.error(f"Файл слишком большой для загрузки: {file_size/1024/1024:.2f} МБ (максимум {MAX_FILE_SIZE/1024/1024:.1f} МБ)")
            return False
    else:
        logger.warning("Не удалось получить размер файла из API, продолжаем без проверки размера")
    
    # Пробуем сначала прямой доступ к файлу, если это возможно
    if os.path.isfile(file_path) and os.access(file_path, os.R_OK):
        try:
            logger.info(f"Файл доступен локально, копируем напрямую: {file_path} -> {destination}")
            
            # Создаем директорию назначения, если она не существует
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            
            # Копируем файл
            import shutil
            shutil.copy2(file_path, destination)
            
            file_size = os.path.getsize(destination)
            logger.info(f"Файл успешно скопирован локально, размер: {file_size/1024/1024:.2f} МБ")
            return True
        except (IOError, OSError) as e:
            logger.error(f"Ошибка при локальном копировании файла: {e}")
            logger.info("Продолжаем с методом загрузки через HTTP")
    elif os.path.isfile(file_path) and not os.access(file_path, os.R_OK):
        # Файл существует, но нет прав доступа - попробуем через sudo
        try:
            logger.info(f"Файл существует, но требуются права root для копирования: {file_path}")
            
            # Создаем директорию назначения, если она не существует
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            
            # Пробуем скопировать файл используя sudo (если разрешено)
            import subprocess
            
            # Проверяем, настроен ли sudo без пароля для данного пользователя и этого файла
            logger.info("Пробуем копировать через sudo")
            
            # Формируем команду для копирования
            cmd = f"sudo cp '{file_path}' '{destination}'"
            
            # Выполняем команду
            process = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            
            if process.returncode == 0:
                # Проверяем, что файл скопирован и имеет правильный размер
                if os.path.exists(destination) and os.path.getsize(destination) > 0:
                    # Меняем права доступа для скопированного файла, чтобы бот мог его читать
                    os.chmod(destination, 0o644)
                    file_size = os.path.getsize(destination)
                    logger.info(f"Файл успешно скопирован через sudo, размер: {file_size/1024/1024:.2f} МБ")
                    return True
                else:
                    logger.error("Файл скопирован через sudo, но он пустой или не существует")
            else:
                logger.error(f"Ошибка при копировании через sudo: {process.stderr}")
                logger.info("Возможно, требуется настроить sudo без пароля для данной команды")
        except Exception as e:
            logger.exception(f"Ошибка при попытке копирования через sudo: {e}")
    elif file_path.startswith('/var/lib/telegram-bot-api'):
        # Пытаемся использовать настраиваемый путь к файлам Local Bot API
        bot_files_path = str(pathlib.Path(__file__).resolve().parent / LOCAL_BOT_API_FILES_PATH)
        bot_specific_path = file_path.replace('/var/lib/telegram-bot-api', bot_files_path)
        
        logger.info(f"Пробуем найти файл по настраиваемому пути: {bot_specific_path}")
        
        if os.path.isfile(bot_specific_path) and os.access(bot_specific_path, os.R_OK):
            try:
                logger.info(f"Файл найден по настраиваемому пути, копируем: {bot_specific_path} -> {destination}")
                
                # Создаем директорию назначения, если она не существует
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                
                # Копируем файл
                import shutil
                shutil.copy2(bot_specific_path, destination)
                
                file_size = os.path.getsize(destination)
                logger.info(f"Файл успешно скопирован локально, размер: {file_size/1024/1024:.2f} МБ")
                return True
            except (IOError, OSError) as e:
                logger.error(f"Ошибка при локальном копировании файла через настраиваемый путь: {e}")
                logger.info("Продолжаем с проверкой других путей")
        elif os.path.isfile(bot_specific_path) and not os.access(bot_specific_path, os.R_OK):
            # Файл существует по альтернативному пути, но нет прав доступа
            try:
                logger.info(f"Файл существует по настраиваемому пути, но требуются права root для копирования: {bot_specific_path}")
                
                # Создаем директорию назначения, если она не существует
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                
                # Пробуем скопировать файл используя sudo
                import subprocess
                
                # Формируем команду для копирования
                cmd = f"sudo cp '{bot_specific_path}' '{destination}'"
                
                # Выполняем команду
                process = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                
                if process.returncode == 0:
                    # Проверяем, что файл скопирован и имеет правильный размер
                    if os.path.exists(destination) and os.path.getsize(destination) > 0:
                        # Меняем права доступа для скопированного файла, чтобы бот мог его читать
                        os.chmod(destination, 0o644)
                        file_size = os.path.getsize(destination)
                        logger.info(f"Файл успешно скопирован через sudo из настраиваемого пути, размер: {file_size/1024/1024:.2f} МБ")
                        return True
                    else:
                        logger.error("Файл скопирован через sudo, но он пустой или не существует")
                else:
                    logger.error(f"Ошибка при копировании через sudo: {process.stderr}")
            except Exception as e:
                logger.exception(f"Ошибка при попытке копирования через sudo: {e}")
        
        # Проверяем еще несколько альтернативных вариантов пути
        alt_paths = [
            # Попробуем несколько вариантов монтирования Docker-томов
            file_path.replace('/var/lib/telegram-bot-api', '/data/telegram-bot-api'),
            # Добавьте другие возможные пути здесь
        ]
        
        # Проверяем каждый альтернативный путь
        for alt_path in alt_paths:
            if os.path.isfile(alt_path):
                try:
                    logger.info(f"Файл найден по альтернативному пути, копируем: {alt_path} -> {destination}")
                    
                    # Создаем директорию назначения, если она не существует
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    
                    # Копируем файл
                    import shutil
                    shutil.copy2(alt_path, destination)
                    
                    file_size = os.path.getsize(destination)
                    logger.info(f"Файл успешно скопирован локально, размер: {file_size/1024/1024:.2f} МБ")
                    return True
                except (IOError, OSError) as e:
                    logger.error(f"Ошибка при локальном копировании файла через альтернативный путь: {e}")
                    logger.info("Продолжаем с методом загрузки через HTTP")
                    break  # Если файл найден, но копирование не удалось, прекращаем попытки с альт. путями
    
    # Если локальное копирование не удалось или файл недоступен, продолжаем через HTTP
    logger.info(f"Локальное копирование невозможно, загружаем файл через HTTP")
    
    # Обрабатываем путь к файлу (убираем абсолютный путь если он есть)
    # В Local Bot API путь может быть абсолютным, но в URL нужен относительный
    if file_path.startswith('/'):
        # Проверяем, содержит ли путь специфичную директорию Local Bot API
        bot_api_dir = f"/var/lib/telegram-bot-api/{bot_token}/"
        if bot_api_dir in file_path:
            # Извлекаем только часть пути после токена бота
            file_path = file_path.split(bot_api_dir)[1]
        else:
            # Просто убираем начальный слеш для формирования корректного URL
            file_path = file_path.lstrip('/')
    
    # Формируем URL для загрузки файла напрямую
    url = f"{LOCAL_BOT_API}/file/bot{bot_token}/{file_path}"
    
    logger.info(f"Начинаем загрузку файла напрямую через HTTP: {url}")
    local_max_file_size = 100 * 1024 * 1024  # 100 МБ максимум для загрузки через HTTP
    
    try:
        # Получаем размер файла и верхнее ограничение из API getFile
        file_info = await get_file_path_direct(file_id, bot_token, return_full_info=True)
        if file_info and 'file_size' in file_info:
            file_size = file_info['file_size']
            logger.info(f"Размер загружаемого файла (из API): {file_size/1024/1024:.2f} МБ")
            
            # Проверяем размер файла
            if file_size > local_max_file_size:
                logger.error(f"Файл слишком большой для загрузки через HTTP: {file_size/1024/1024:.2f} МБ (максимум {local_max_file_size/1024/1024} МБ)")
                return False
        else:
            logger.warning("Не удалось получить размер файла из API, продолжаем без проверки размера")
            
        async with aiohttp.ClientSession() as session:
            # Загружаем файл блоками с таймаутом
            # Не используем HEAD-запросы, так как Local Bot API может их не поддерживать (ошибка 501)
            async with session.get(url, timeout=300) as response:
                if response.status != 200:
                    logger.error(f"Ошибка при загрузке файла. Статус: {response.status}. "
                                f"Ответ: {await response.text()}")
                    return False
                
                # Получаем размер файла из заголовка ответа, если он есть
                if 'Content-Length' in response.headers:
                    content_length = int(response.headers.get('Content-Length', 0))
                    logger.info(f"Размер загружаемого файла (из заголовка Content-Length): {content_length/1024/1024:.2f} МБ")
                
                # Убедимся, что директория существует
                os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
                
                # Загружаем и записываем файл блоками
                downloaded_size = 0
                chunk_size = 1024 * 1024  # 1 МБ
                
                logger.info(f"Начинаем сохранение файла в {destination}")
                with open(destination, 'wb') as fd:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        fd.write(chunk)
                        downloaded_size += len(chunk)
                        if downloaded_size % (5 * chunk_size) == 0:  # Каждые 5 МБ
                            logger.info(f"Загружено {downloaded_size/1024/1024:.2f} МБ")
                
                # Проверяем, что файл не пустой
                if os.path.getsize(destination) == 0:
                    logger.error("Загруженный файл пуст")
                    os.remove(destination)
                    return False
                
                # Проверяем, что размер файла совпадает с ожидаемым, если известен размер из API
                if file_info and 'file_size' in file_info:
                    expected_size = file_info['file_size']
                    actual_size = os.path.getsize(destination)
                    if expected_size != actual_size:
                        logger.error(f"Размер загруженного файла ({actual_size}) не соответствует ожидаемому из API ({expected_size})")
                        os.remove(destination)
                        return False
                
                logger.info(f"Файл успешно загружен в {destination}, размер: {os.path.getsize(destination)/1024/1024:.2f} МБ")
                return True
                
    except asyncio.TimeoutError:
        logger.error(f"Тайм-аут при загрузке файла")
        if os.path.exists(destination):
            os.remove(destination)
        return False
    except Exception as e:
        logger.exception(f"Ошибка при загрузке файла: {str(e)}")
        if os.path.exists(destination):
            os.remove(destination)
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

# Функция для очистки временных файлов
def cleanup_temp_files(file_path=None, older_than_hours=24):
    """
    Удаляет временные файлы после обработки аудио
    
    Args:
        file_path: Конкретный файл для удаления (если указан)
        older_than_hours: Удалить все файлы старше указанного количества часов
    """
    try:
        # Если указан конкретный файл, удаляем его
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"Удален временный файл: {file_path}")
            return
            
        # Если файл не указан, очищаем старые файлы
        if not os.path.exists(TEMP_AUDIO_DIR):
            return
            
        current_time = datetime.now()
        count_removed = 0
        
        for filename in os.listdir(TEMP_AUDIO_DIR):
            file_path = os.path.join(TEMP_AUDIO_DIR, filename)
            
            # Проверяем, что это файл, а не директория
            if os.path.isfile(file_path):
                # Получаем время последнего изменения файла
                file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                # Вычисляем, сколько часов прошло
                age_hours = (current_time - file_mod_time).total_seconds() / 3600
                
                # Если файл старше указанного времени, удаляем его
                if age_hours > older_than_hours:
                    os.remove(file_path)
                    count_removed += 1
                    
        if count_removed > 0:
            logger.info(f"Очищено {count_removed} временных файлов старше {older_than_hours} часов")
    except Exception as e:
        logger.exception(f"Ошибка при очистке временных файлов: {e}")

async def background_audio_processor():
    """Фоновый обработчик очереди аудиофайлов"""
    global background_worker_running
    background_worker_running = True
    logger.info("Запущен фоновый обработчик аудиофайлов")
    
    # Счетчик для периодической очистки файлов
    cleanup_counter = 0
    
    try:
        while True:
            try:
                # Инкрементируем счетчик очистки
                cleanup_counter += 1
                
                # Каждые 10 циклов выполняем очистку старых файлов
                if cleanup_counter >= 10:
                    cleanup_counter = 0
                    # logger.debug("Запуск периодической очистки временных файлов")
                    cleanup_temp_files(older_than_hours=24)
                
                # Получаем задачу из очереди (с таймаутом, чтобы можно было корректно завершить поток)
                task = await asyncio.wait_for(audio_task_queue.get(), timeout=1.0)
                
                try:
                    # Распаковываем данные задачи
                    message, file_path, processing_msg, user_id, file_name = task
                    
                    # Получаем данные пользователя для транскрибации
                    username = message.from_user.username
                    first_name = message.from_user.first_name
                    last_name = message.from_user.last_name
                    
                    # Проверяем, не отменена ли задача
                    if user_id in active_transcriptions and active_transcriptions[user_id][0] == "cancelled":
                        logger.info(f"Задача для пользователя {user_id} была отменена. Пропускаем обработку.")
                        
                        # Удаляем временные файлы
                        try:
                            cleanup_temp_files(file_path)
                        except Exception as e:
                            logger.exception(f"Ошибка при удалении временных файлов после отмены: {e}")
                        
                        # Сообщаем пользователю об отмене
                        await processing_msg.edit_text("❌ Обработка аудио была отменена.")
                        
                        # Удаляем задачу из активных
                        del active_transcriptions[user_id]
                        
                        # Отмечаем задачу как выполненную
                        audio_task_queue.task_done()
                        continue
                    
                    # Сообщаем о начале транскрибации
                    await processing_msg.edit_text(
                        f"Транскрибирую аудио {'с помощью локального Whisper' if USE_LOCAL_WHISPER else 'через OpenAI API'}...\n\n"
                        f"Это может занять некоторое время в зависимости от длины аудио. Вы можете продолжать использовать бота.\n\n"
                        f"Чтобы отменить обработку, используйте команду /cancel"
                    )
                    
                    # Проверяем размер файла для предупреждения о возможном переключении модели
                    try:
                        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                        should_switch, smaller_model = should_use_smaller_model(file_size_mb, WHISPER_MODEL)
                        
                        if should_switch:
                            await processing_msg.edit_text(
                                f"Транскрибирую аудио...\n\n"
                                f"⚠️ Обратите внимание: Файл имеет большой размер ({file_size_mb:.1f} МБ), "
                                f"поэтому вместо модели {WHISPER_MODEL} будет использована модель {smaller_model} для оптимизации памяти.\n\n"
                                f"Это может повлиять на качество транскрибации, но позволит обработать большой файл без ошибок."
                            )
                    except Exception as e:
                        logger.exception(f"Ошибка при проверке размера файла: {e}")
                    
                    # Запускаем транскрибацию в отдельном потоке, чтобы не блокировать event loop
                    loop = asyncio.get_event_loop()
                    try:
                        # Создаем объект будущего результата
                        future = loop.run_in_executor(
                            thread_executor,
                            # Оборачиваем асинхронную функцию в синхронную
                            lambda fp=file_path: asyncio.run(transcribe_audio(fp))
                        )
                        
                        # Сохраняем информацию о текущей задаче в словаре активных задач
                        active_transcriptions[user_id] = (future, processing_msg.message_id, file_path)
                        
                        # Ожидаем результат с периодическим обновлением статуса
                        start_time = datetime.now()
                        while not future.done():
                            # Проверяем, не отменена ли задача
                            if user_id in active_transcriptions and active_transcriptions[user_id][0] == "cancelled":
                                # Проверяем, что отмена относится к текущей задаче
                                current_msg_id = active_transcriptions[user_id][1]
                                if current_msg_id == processing_msg.message_id:
                                    # Отменяем future (если возможно)
                                    future.cancel()
                                    logger.info(f"Транскрибация для пользователя {user_id} была отменена во время обработки.")
                                    
                                    # Удаляем временные файлы
                                    try:
                                        cleanup_temp_files(file_path)
                                    except Exception as e:
                                        logger.exception(f"Ошибка при удалении временных файлов после отмены: {e}")
                                    
                                    # Сообщаем пользователю об отмене
                                    await processing_msg.edit_text("❌ Обработка аудио была отменена.")
                                    
                                    # Удаляем задачу из активных
                                    del active_transcriptions[user_id]
                                    
                                    # Отмечаем задачу как выполненную
                                    audio_task_queue.task_done()
                                    break
                            
                            # Обновляем сообщение о статусе каждые 30 секунд
                            elapsed = (datetime.now() - start_time).total_seconds()
                            if elapsed > 0 and elapsed % 30 < 1:  # примерно каждые 30 секунд
                                time_str = str(timedelta(seconds=int(elapsed)))
                                
                                # Определяем, какая модель используется
                                current_model = WHISPER_MODEL
                                file_size_mb = os.path.getsize(file_path) / (1024 * 1024) if os.path.exists(file_path) else 0
                                should_switch, smaller_model = should_use_smaller_model(file_size_mb, WHISPER_MODEL)
                                
                                if should_switch:
                                    current_model = smaller_model
                                
                                # Получаем предполагаемое оставшееся время
                                estimated_total = predict_processing_time(file_path, current_model)
                                elapsed_td = timedelta(seconds=int(elapsed))
                                remaining = estimated_total - elapsed_td if estimated_total > elapsed_td else timedelta(seconds=10)
                                
                                # Расчет примерного процента завершения
                                if estimated_total.total_seconds() > 0:
                                    percent_complete = min(95, int((elapsed / estimated_total.total_seconds()) * 100))
                                    progress_bar = "█" * (percent_complete // 5) + "░" * ((100 - percent_complete) // 5)
                                else:
                                    percent_complete = 0
                                    progress_bar = "░" * 20
                                
                                status_message = (
                                    f"Транскрибирую аудио {'с помощью локального Whisper' if USE_LOCAL_WHISPER else 'через OpenAI API'}...\n\n"
                                    f"⏱ Прошло времени: {time_str}\n"
                                    f"⌛ Осталось примерно: {str(remaining)}\n"
                                    f"📊 Прогресс: {progress_bar} {percent_complete}%\n"
                                    f"📁 Файл: {file_name}\n"
                                    f"🎯 Модель: {current_model}\n\n"
                                    f"Вы можете продолжать использовать бота для других задач.\n\n"
                                    f"Для отмены обработки используйте команду /cancel"
                                )
                                
                                await processing_msg.edit_text(status_message)
                            
                            # Небольшая пауза, чтобы не нагружать процессор
                            await asyncio.sleep(1)
                        
                        # После завершения удаляем задачу из словаря активных задач
                        if user_id in active_transcriptions and active_transcriptions[user_id][0] == future:
                            del active_transcriptions[user_id]
                        
                        # Получаем результат (если задача не была отменена)
                        if not future.cancelled():
                            transcription = await future
                        else:
                            # Если задача была отменена, пропускаем дальнейшую обработку
                            continue
                        
                    except Exception as e:
                        logger.exception(f"Ошибка при асинхронной транскрибации: {e}")
                        raise
                    
                    # Проверяем, получили ли мы результат
                    if transcription is None:
                        # Если транскрибация не удалась, сообщаем об ошибке
                        await processing_msg.edit_text(
                            f"❌ Ошибка при транскрибации аудио: {file_name}\n\n"
                            f"Не удалось обработать аудиофайл. Возможные причины:\n"
                            f"• Файл повреждён или имеет неподдерживаемый формат\n"
                            f"• Аудио не содержит речи или имеет слишком низкое качество\n"
                            f"• Ошибка при обработке модели Whisper\n\n"
                            f"Пожалуйста, попробуйте отправить другой аудиофайл или обратитесь к администратору."
                        )
                        
                        # Удаляем временные файлы
                        try:
                            cleanup_temp_files(file_path)
                        except Exception as e:
                            logger.exception(f"Ошибка при удалении временных файлов: {e}")
                        
                        # Отмечаем задачу как выполненную
                        audio_task_queue.task_done()
                        continue
                    
                    # Сохраняем транскрибацию в файл
                    transcript_file_path = save_transcription_to_file(
                        transcription, 
                        user_id, 
                        file_name, 
                        username, 
                        first_name, 
                        last_name
                    )
                    
                    # Формируем текстовое сообщение
                    message_text = f"🎤 Транскрибация аудио: {file_name}\n\n"
                    
                    # Определяем, какая модель использовалась
                    used_model = WHISPER_MODEL
                    
                    # Пытаемся получить информацию о фактически использованной модели из результата
                    if isinstance(transcription, dict) and "whisper_model" in transcription:
                        used_model = transcription.get("whisper_model")
                        
                        # Если использованная модель отличается от заданной, добавляем информацию
                        if used_model != WHISPER_MODEL:
                            processing_time = transcription.get("processing_time", 0)
                            processing_time_str = f" (время обработки: {processing_time:.1f} сек)" if processing_time > 0 else ""
                            message_text += f"ℹ️ Использована модель {used_model} вместо {WHISPER_MODEL} для оптимизации памяти{processing_time_str}.\n\n"
                    else:
                        # Если информации нет в результате, используем приблизительную проверку по размеру файла
                        file_size_mb = os.path.getsize(file_path) / (1024 * 1024) if os.path.exists(file_path) else 0
                        should_switch, smaller_model = should_use_smaller_model(file_size_mb, WHISPER_MODEL)
                        
                        if should_switch:
                            used_model = smaller_model
                            message_text += f"ℹ️ Для обработки использована модель {smaller_model} вместо {WHISPER_MODEL} из-за большого размера файла.\n\n"
                    
                    # Получаем текст транскрибации
                    transcription_text = transcription
                    # Если результат в формате словаря, извлекаем текст
                    if isinstance(transcription, dict):
                        transcription_text = transcription.get('text', '')
                    
                    # Проверяем, не пустой ли текст транскрибации
                    if not transcription_text:
                        await processing_msg.edit_text(
                            f"⚠️ Предупреждение: Транскрибация аудио не содержит текста.\n\n"
                            f"Возможно, аудио не содержит распознаваемой речи или имеет слишком низкое качество."
                        )
                        
                        # Удаляем временные файлы
                        try:
                            cleanup_temp_files(file_path)
                        except Exception as e:
                            logger.exception(f"Ошибка при удалении временных файлов: {e}")
                        
                        # Отмечаем задачу как выполненную
                        audio_task_queue.task_done()
                        continue
                    
                    # Если текст слишком длинный, разбиваем на части
                    if len(transcription_text) > MAX_MESSAGE_LENGTH - len(message_text):
                        # Отправляем превью транскрибации
                        preview_length = MAX_MESSAGE_LENGTH - len(message_text) - 50  # Оставляем запас
                        preview_text = transcription_text[:preview_length] + "...\n\n(полный текст в файле)"
                        await processing_msg.edit_text(message_text + preview_text)
                        
                        # Отправляем файл с полной транскрибацией безопасным способом
                        await send_file_safely(
                            message,
                            transcript_file_path,
                            caption="Полная транскрибация аудио"
                        )
                        
                        # Проверяем наличие SRT-файла и отправляем его
                        srt_file_path = transcript_file_path.replace('.txt', '.srt')
                        if os.path.exists(srt_file_path):
                            await send_file_safely(
                                message,
                                srt_file_path,
                                caption="Файл субтитров (SRT) для видеоредакторов"
                            )
                    else:
                        # Для коротких транскрибаций просто отправляем весь текст
                        await processing_msg.edit_text(message_text + transcription_text)
                        
                        # Отправляем файл для удобства
                        await send_file_safely(
                            message,
                            transcript_file_path,
                            caption="Транскрибация аудио в виде файла"
                        )
                        
                        # Проверяем наличие SRT-файла и отправляем его
                        srt_file_path = transcript_file_path.replace('.txt', '.srt')
                        if os.path.exists(srt_file_path):
                            await send_file_safely(
                                message,
                                srt_file_path,
                                caption="Файл субтитров (SRT) для видеоредакторов"
                            )
                    
                    # Удаляем временные файлы
                    try:
                        cleanup_temp_files(file_path)
                    except Exception as e:
                        logger.exception(f"Ошибка при удалении временных файлов: {e}")
                    
                    # Отмечаем задачу как выполненную
                    audio_task_queue.task_done()
                    
                except TelegramBadRequest as e:
                    if "file is too big" in str(e).lower():
                        await processing_msg.edit_text(
                            "⚠️ Ошибка: Файл слишком большой для обработки в Telegram. "
                            "Пожалуйста, отправьте аудиофайл меньшего размера (до 20 МБ)."
                        )
                    else:
                        await processing_msg.edit_text(f"Произошла ошибка при обработке аудио: {str(e)}")
                    logger.exception(f"Ошибка Telegram при обработке аудио: {e}")
                    audio_task_queue.task_done()
                    
                except Exception as e:
                    await processing_msg.edit_text(f"Произошла ошибка при обработке аудио: {str(e)}")
                    logger.exception(f"Ошибка при обработке аудио: {e}")
                    audio_task_queue.task_done()
                    
            except asyncio.TimeoutError:
                # Проверка пустой очереди - нормальная ситуация
                continue
            except asyncio.CancelledError:
                # Обработчик был остановлен
                logger.info("Фоновый обработчик аудиофайлов остановлен")
                break
            except Exception as e:
                logger.exception(f"Неожиданная ошибка в обработчике очереди: {e}")
                # Продолжаем работу, несмотря на ошибку
                await asyncio.sleep(1)
    finally:
        background_worker_running = False
        logger.info("Фоновый обработчик аудиофайлов завершен")

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    """Отменяет текущую обработку аудио для пользователя"""
    user_id = message.from_user.id
    
    if user_id not in active_transcriptions:
        await message.answer("У вас нет активных задач обработки аудио.")
        return
    
    future, message_id, file_path = active_transcriptions[user_id]
    
    # Если задача еще не начала выполняться (future - реальный объект Future)
    if future != "cancelled" and not isinstance(future, str):
        try:
            # Помечаем задачу как отмененную
            active_transcriptions[user_id] = ("cancelled", message_id, file_path)
            
            # Пытаемся отправить уведомление об отмене
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=message_id,
                    text="⏱ Отмена обработки аудио...\n\nПожалуйста, подождите."
                )
            except Exception as e:
                logger.exception(f"Ошибка при обновлении сообщения об отмене: {e}")
            
            # Удаляем временный файл
            if file_path and os.path.exists(file_path):
                try:
                    cleanup_temp_files(file_path)
                    logger.info(f"Временный файл {file_path} был удален при отмене задачи пользователем {user_id}")
                except Exception as e:
                    logger.exception(f"Ошибка при удалении временного файла при отмене: {e}")
            
            await message.answer("✅ Задача обработки аудио отменена.")
            logger.info(f"Пользователь {user_id} отменил обработку аудио")
        except Exception as e:
            await message.answer(f"Произошла ошибка при попытке отменить задачу: {str(e)}")
            logger.exception(f"Ошибка при отмене задачи: {e}")
    else:
        # Если задача уже отменена
        await message.answer("Задача уже отменена или находится в процессе отмены.")

@dp.message(lambda message: message.voice or message.audio)
async def handle_audio(message: types.Message):
    user_id = message.from_user.id
    
    if not USE_LOCAL_WHISPER and not check_message_limit(user_id):
        await message.answer("Вы достигли дневного лимита в 50 сообщений. Попробуйте завтра!")
        return
    
    # Проверка, нет ли уже активной задачи с отметкой "отменено"
    if user_id in active_transcriptions and active_transcriptions[user_id][0] == "cancelled":
        # Если найдена отмененная задача, удаляем её из словаря, так как она больше не актуальна
        del active_transcriptions[user_id]
        logger.info(f"Удалена устаревшая отмененная задача для пользователя {user_id}")
    
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
        
        # Получаем информацию о файле и скачиваем его
        is_large_file = False
        file_size = 0
        
        try:
            # Сначала пробуем получить информацию о файле
            await processing_msg.edit_text("Получаю информацию о файле...")
            
            try:
                file = await bot.get_file(file_id)
                file_size = file.file_size
                
                logger.info(f"Информация о файле получена: file_id={file_id}, size={file_size/1024/1024:.2f} МБ")
                
                # Проверяем размер файла
                if file_size > MAX_FILE_SIZE:
                    await processing_msg.edit_text(
                        f"⚠️ Файл слишком большой для обработки. Максимальный размер: {MAX_FILE_SIZE/1024/1024:.1f} МБ.\n\n"
                        f"Размер вашего файла: {file_size/1024/1024:.1f} МБ.\n\n"
                        f"Рекомендации:\n"
                        f"• Сократите длительность аудио\n"
                        f"• Разделите длинное аудио на несколько частей\n"
                        f"• Используйте формат с большим сжатием (MP3 с низким битрейтом)"
                    )
                    return
                
                # Проверяем, необходимо ли использовать прямую загрузку
                if file_size <= STANDARD_API_LIMIT:
                    await processing_msg.edit_text("Скачиваю аудиофайл стандартным методом...")
                    download_success = await download_voice(file, file_path)
                    
                    if not download_success:
                        await processing_msg.edit_text(
                            "⚠️ Не удалось скачать аудиофайл стандартным методом. "
                            "Попробуйте еще раз или отправьте файл меньшего размера."
                        )
                        return
                else:
                    is_large_file = True
            except TelegramBadRequest as e:
                if "file is too big" in str(e).lower():
                    # Файл слишком большой для стандартного API, пробуем через Local Bot API напрямую
                    is_large_file = True
                else:
                    raise
            
            # Если файл большой и есть Local Bot API, используем прямую загрузку
            if is_large_file:
                if not LOCAL_BOT_API:
                    await processing_msg.edit_text(
                        f"⚠️ Файл слишком большой для стандартного Telegram Bot API (> 20 МБ).\n\n"
                        f"Для обработки файлов такого размера необходимо настроить Local Bot API Server. "
                        f"Обратитесь к администратору бота или следуйте инструкциям в документации."
                    )
                    return
                
                await processing_msg.edit_text("Файл слишком большой для стандартного API. Использую прямую загрузку через Local Bot API...")
                
                # Получаем токен бота
                bot_token = env_config.get('TELEGRAM_TOKEN')
                
                # Получаем путь к файлу через прямой запрос
                await processing_msg.edit_text("Получаю информацию о большом файле через Local Bot API...")
                file_path_on_server = await get_file_path_direct(file_id, bot_token)
                
                if not file_path_on_server:
                    await processing_msg.edit_text(
                        "⚠️ Не удалось получить информацию о файле через Local Bot API. "
                        "Возможно, файл всё ещё слишком большой или возникла другая ошибка."
                    )
                    return
                
                # Загружаем файл напрямую через Local Bot API
                await processing_msg.edit_text(f"Загружаю большой файл напрямую через Local Bot API...\nЭтот процесс может занять некоторое время для файлов большого размера.")
                
                if not await download_large_file_direct(file_id, file_path, bot_token):
                    await processing_msg.edit_text(
                        "⚠️ Не удалось загрузить файл через Local Bot API. "
                        "Возможно, файл слишком большой или возникла ошибка сервера."
                    )
                    return
                
                # Получаем размер скачанного файла
                file_size = os.path.getsize(file_path)
        except TelegramBadRequest as e:
            if "file is too big" in str(e).lower():
                await processing_msg.edit_text(
                    f"⚠️ Ошибка при загрузке: файл слишком большой для API Telegram.\n\n"
                    f"Даже при использовании Local Bot API существуют ограничения. "
                    f"Максимальный поддерживаемый размер файла: 2000 МБ.\n\n"
                    f"Рекомендации:\n"
                    f"• Используйте файл меньшего размера\n"
                    f"• Сократите длительность аудио\n"
                    f"• Разделите длинное аудио на несколько частей\n"
                    f"• Используйте формат с большим сжатием (MP3 с низким битрейтом)"
                )
                return
            else:
                await processing_msg.edit_text(f"Ошибка при загрузке файла: {str(e)}")
                logger.exception(f"Ошибка Telegram при загрузке: {e}")
                return
        except Exception as e:
            await processing_msg.edit_text(f"Произошла ошибка при загрузке файла: {str(e)}")
            logger.exception(f"Ошибка при загрузке файла: {e}")
            return
        
        # Проверяем, что файл успешно скачан
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            await processing_msg.edit_text("Ошибка: не удалось скачать аудиофайл или файл пустой.")
            return
        
        # Предсказываем время обработки
        estimated_time = predict_processing_time(file_path, WHISPER_MODEL)
        estimated_time_str = str(estimated_time)
        
        # Уведомляем пользователя о постановке в очередь
        file_size_mb = file_size / (1024 * 1024)
        
        # Проверяем, нужно ли использовать модель меньшего размера
        should_switch, smaller_model = should_use_smaller_model(file_size_mb, WHISPER_MODEL)
        model_info = f"Модель: {WHISPER_MODEL}"
        if should_switch:
            model_info = f"Модель: {smaller_model} (автоматически выбрана для большого файла вместо {WHISPER_MODEL})"
            # Обновляем время с учетом фактически используемой модели
            estimated_time = predict_processing_time(file_path, smaller_model)
            estimated_time_str = str(estimated_time)
        
        # Запускаем фоновый обработчик очереди, если он еще не запущен
        global background_worker_running
        if not background_worker_running:
            # Создаем и запускаем задачу, не ожидая ее завершения
            background_task = asyncio.create_task(background_audio_processor())
            # Мы не используем await, так как не хотим блокировать выполнение текущего кода
        
        # Получаем текущий размер очереди для определения позиции файла
        queue_size = audio_task_queue.qsize()
        queue_position = queue_size + 1  # Позиция нового файла будет на 1 больше текущего размера
        
        # Обновляем сообщение с учетом позиции в очереди
        position_text = ""
        if queue_position == 1:
            position_text = "🔥 Ваш файл первый в очереди и будет обработан немедленно!"
        else:
            # Склонение слова "файл" в зависимости от позиции
            files_before = queue_position - 1
            files_word = "файл"
            if files_before == 1:
                files_word = "файл"
            elif 2 <= files_before <= 4:
                files_word = "файла"
            else:
                files_word = "файлов"
            
            position_text = f"🕒 Номер вашего файла в очереди: {queue_position}\nПеред вами {files_before} {files_word} ожидают обработки."
        
        await processing_msg.edit_text(
            f"Аудиофайл успешно загружен и поставлен в очередь на обработку.\n"
            f"Размер файла: {file_size_mb:.2f} МБ\n"
            f"{model_info}\n"
            f"Метод загрузки: {'Прямая загрузка через Local Bot API' if is_large_file else 'Стандартный API'}\n\n"
            f"{position_text}\n\n"
            f"⏱ Примерное время обработки: {estimated_time_str}\n\n"
            f"Обработка начнется автоматически. Вы получите уведомление, когда транскрибация будет готова.\n\n"
            f"Для отмены обработки используйте команду /cancel"
        )
        
        # Добавляем задачу в очередь
        await audio_task_queue.put((message, file_path, processing_msg, user_id, file_name))
        logger.info(f"Аудиофайл от пользователя {user_id} добавлен в очередь на обработку. Текущий размер очереди: {audio_task_queue.qsize()}")
        
    except TelegramBadRequest as e:
        if "file is too big" in str(e).lower():
            await processing_msg.edit_text(
                f"⚠️ Ошибка: Файл слишком большой для обработки в Telegram.\n\n"
                f"Текущее ограничение: 20 МБ (даже при использовании Local Bot API)\n\n"
                f"Рекомендации:\n"
                f"• Используйте файл меньшего размера (до 20 МБ)\n"
                f"• Сократите длительность аудио\n"
                f"• Разделите длинное аудио на несколько частей\n"
                f"• Конвертируйте файл в формат с бóльшим сжатием (например, MP3 96 kbps)"
            )
            logger.error(f"Ошибка 'file is too big' при обработке аудио: {e}")
        else:
            await processing_msg.edit_text(f"Произошла ошибка при подготовке аудио к обработке: {str(e)}")
            logger.exception(f"Ошибка Telegram при обработке аудио: {e}")
    except Exception as e:
        await processing_msg.edit_text(f"Произошла ошибка при подготовке аудио к обработке: {str(e)}")
        logger.exception(f"Ошибка при обработке аудио: {e}")

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
        
        # Получаем текст ответа
        response_text = response.choices[0].message.content
        
        # Если ответ слишком длинный, разбиваем на части
        if len(response_text) > MAX_MESSAGE_LENGTH:
            chunks = split_text_into_chunks(response_text)
            
            # Обновляем первое сообщение
            await processing_msg.edit_text(chunks[0])
            
            # Отправляем остальные части
            for chunk in chunks[1:]:
                await message.answer(chunk)
        else:
            # Отправляем ответ пользователю
            await processing_msg.edit_text(response_text)
        
    except Exception as e:
        logger.exception(f"Произошла ошибка при обработке сообщения: {e}")
        await processing_msg.edit_text(f"Произошла ошибка при обработке сообщения: {str(e)}")

# Функция для сохранения очереди в файл
async def save_queue_to_file():
    """
    Сохраняет текущую очередь задач в JSON файл
    """
    try:
        # Получаем все элементы из очереди
        queue_items = []
        temp_queue = asyncio.Queue()
        
        # Сохраняем размер очереди
        queue_size = audio_task_queue.qsize()
        if queue_size == 0:
            logger.info("Очередь пуста, нечего сохранять")
            
            # Если файл существует - удаляем его
            if os.path.exists(QUEUE_SAVE_PATH):
                os.remove(QUEUE_SAVE_PATH)
                logger.info(f"Удален файл с сохраненной очередью: {QUEUE_SAVE_PATH}")
            return
            
        logger.info(f"Сохранение очереди заданий, количество элементов: {queue_size}")
        
        # Извлекаем все элементы из очереди во временный список
        for _ in range(queue_size):
            try:
                item = audio_task_queue.get_nowait()
                
                # Разбираем кортеж
                message, file_path, processing_msg, user_id, file_name = item
                
                # Сохраняем только необходимые данные, которые можно сериализовать
                if os.path.exists(file_path):
                    serializable_item = {
                        "user_id": user_id,
                        "file_path": file_path,
                        "file_name": file_name,
                        "message_id": processing_msg.message_id,
                        "chat_id": processing_msg.chat.id,
                        "timestamp": time.time()
                    }
                    queue_items.append(serializable_item)
                    
                # Возвращаем элемент обратно в очередь
                temp_queue.put_nowait(item)
            except asyncio.QueueEmpty:
                break
        
        # Восстанавливаем основную очередь
        while not temp_queue.empty():
            audio_task_queue.put_nowait(temp_queue.get_nowait())
        
        # Сохраняем данные в файл
        if queue_items:
            with open(QUEUE_SAVE_PATH, 'w', encoding='utf-8') as f:
                json.dump(queue_items, f, ensure_ascii=False, indent=2)
            logger.info(f"Очередь заданий сохранена в файл: {QUEUE_SAVE_PATH}, элементов: {len(queue_items)}")
        else:
            logger.info("Нет заданий для сохранения")
            
            # Если файл существует - удаляем его
            if os.path.exists(QUEUE_SAVE_PATH):
                os.remove(QUEUE_SAVE_PATH)
                
    except Exception as e:
        logger.exception(f"Ошибка при сохранении очереди в файл: {e}")

# Функция для загрузки очереди из файла
async def load_queue_from_file():
    """
    Загружает сохраненную очередь задач из JSON файла
    """
    try:
        if not os.path.exists(QUEUE_SAVE_PATH):
            logger.info(f"Файл с сохраненной очередью не найден: {QUEUE_SAVE_PATH}")
            return 0
            
        with open(QUEUE_SAVE_PATH, 'r', encoding='utf-8') as f:
            saved_items = json.load(f)
            
        if not saved_items:
            logger.info("Сохраненная очередь пуста")
            return 0
            
        logger.info(f"Загружаем сохраненную очередь заданий, элементов: {len(saved_items)}")
        
        # Сортируем задания по timestamp (сначала старые)
        saved_items.sort(key=lambda x: x.get("timestamp", 0))
        
        # Проверяем каждый файл и добавляем его в очередь
        restored_count = 0
        for item in saved_items:
            user_id = item.get("user_id")
            file_path = item.get("file_path")
            file_name = item.get("file_name")
            message_id = item.get("message_id")
            chat_id = item.get("chat_id")
            
            # Проверяем существование файла
            if file_path and os.path.exists(file_path):
                try:
                    # Создаем объекты, необходимые для очереди
                    # Нам нужно загрузить сообщение из Telegram для ответа
                    try:
                        # Пробуем получить сообщение из Telegram
                        chat = types.Chat(id=chat_id, type="private")
                        message = types.Message(message_id=message_id, chat=chat, date=int(time.time()))
                        processing_msg = await bot.edit_message_text(
                            "🔄 Задание восстановлено после перезапуска и добавлено в очередь...",
                            chat_id=chat_id,
                            message_id=message_id
                        )
                        
                        # Добавляем задание в очередь
                        await audio_task_queue.put((message, file_path, processing_msg, user_id, file_name))
                        logger.info(f"Восстановлено задание: user_id={user_id}, file={file_name}")
                        restored_count += 1
                    except Exception as msg_error:
                        logger.warning(f"Не удалось восстановить сообщение для задания: {msg_error}")
                        continue
                        
                except Exception as e:
                    logger.warning(f"Ошибка при восстановлении задания {file_name}: {e}")
            else:
                logger.warning(f"Файл не найден при восстановлении очереди: {file_path}")
        
        # Удаляем файл сохранения, чтобы не восстанавливать дважды
        if os.path.exists(QUEUE_SAVE_PATH):
            os.remove(QUEUE_SAVE_PATH)
            
        logger.info(f"Восстановление очереди завершено. Восстановлено заданий: {restored_count} из {len(saved_items)}")
        return restored_count
        
    except Exception as e:
        logger.exception(f"Ошибка при загрузке очереди из файла: {e}")
        return 0

# Периодическое сохранение очереди
async def periodic_queue_save():
    """
    Периодически сохраняет очередь задач в файл
    """
    while True:
        try:
            await save_queue_to_file()
        except Exception as e:
            logger.error(f"Ошибка при периодическом сохранении очереди: {e}")
            
        # Сохраняем каждые 60 секунд
        await asyncio.sleep(60)

# Функция для обработки сигналов завершения
async def shutdown(signal, loop):
    """
    Корректное завершение при получении сигнала
    """
    logger.info(f"Получен сигнал {signal.name}, выполняем корректное завершение...")
    
    # Сохраняем очередь перед выключением
    await save_queue_to_file()
    
    # Останавливаем задачи
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    
    for task in tasks:
        task.cancel()
    
    logger.info(f"Отменено {len(tasks)} задач")
    await asyncio.gather(*tasks, return_exceptions=True)
    
    loop.stop()
    logger.info("Завершение работы бота")
    
async def register_shutdown_handler():
    """
    Регистрирует обработчик сигналов завершения
    """
    try:
        loop = asyncio.get_running_loop()
        
        # Регистрируем обработчики сигналов для SIGINT и SIGTERM
        for sig in [signal.SIGINT, signal.SIGTERM]:
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(shutdown(s, loop))
            )
        logger.info("Зарегистрированы обработчики сигналов завершения")
    except NotImplementedError:
        # Windows не поддерживает add_signal_handler
        logger.info("Регистрация обработчиков сигналов не поддерживается на этой платформе")
    except Exception as e:
        logger.error(f"Ошибка при регистрации обработчиков сигналов: {e}")

async def main():
    logger.info('Бот запущен.')
    try:
        logger.info(f'Используемая модель Whisper: {WHISPER_MODEL}')
        logger.info(f'Директория для моделей Whisper: {WHISPER_MODELS_DIR}')
        
        # Очищаем старые временные файлы при запуске
        cleanup_temp_files(older_than_hours=24)
        logger.info('Выполнена очистка старых временных файлов')
        
        # Проверяем и настраиваем доступ к файлам Telegram Bot API
        #await check_bot_api_files_access()
        #logger.info('Проверен доступ к файлам Telegram Bot API')
        
        # Восстанавливаем сохраненную очередь
        restored_count = await load_queue_from_file()
        if restored_count > 0:
            logger.info(f'Восстановлено {restored_count} заданий из сохраненной очереди')
        
        # Запускаем фоновый обработчик очереди
        background_task = asyncio.create_task(background_audio_processor())
        logger.info('Запущен фоновый обработчик очереди аудиофайлов')
        
        # Запускаем периодическое сохранение очереди
        save_task = asyncio.create_task(periodic_queue_save())
        logger.info('Запущено периодическое сохранение очереди')
        
        # Регистрируем обработчики сигналов завершения
        await register_shutdown_handler()
        
        # Устанавливаем команды в меню бота
        await set_commands()
        logger.info('Команды бота установлены')

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        logger.info('Остановка фонового обработчика очереди...')
        # Даем время очереди завершить текущие задачи
        await asyncio.sleep(1)
    finally:
        await bot.session.close()
        logger.info('Бот остановлен.')

# Функция для проверки доступа к файлам Bot API
async def check_bot_api_files_access():
    """
    Проверяет и настраивает доступ к файлам Local Bot API
    для решения проблем с правами доступа в Docker томах
    """
    if not LOCAL_BOT_API:
        return
        
    try:
        # Проверяем, существует ли папка с файлами Telegram Bot API
        bot_api_dir = LOCAL_BOT_API_FILES_PATH
        if not os.path.exists(bot_api_dir):
            logger.warning(f"Директория с файлами Telegram Bot API не найдена: {bot_api_dir}")
            return
            
        if not os.path.isdir(bot_api_dir):
            logger.warning(f"Путь не является директорией: {bot_api_dir}")
            return
            
        logger.info(f"Проверка файлов в директории {bot_api_dir}")
        
        # Ищем поддиректории с токеном бота
        token_dirs = []
        for item in os.listdir(bot_api_dir):
            item_path = os.path.join(bot_api_dir, item)
            if os.path.isdir(item_path) and ':' in item:  # Предположительно директория с токеном бота
                token_dirs.append(item_path)
                
        if not token_dirs:
            logger.info(f"Не найдены директории с токенами ботов в {bot_api_dir}")
            return
            
        for token_dir in token_dirs:
            logger.info(f"Проверка директории с токеном: {token_dir}")
            
            # Проверяем наличие поддиректории music
            music_dir = os.path.join(token_dir, 'music')
            if os.path.exists(music_dir) and os.path.isdir(music_dir):
                logger.info(f"Проверка прав доступа в директории музыки: {music_dir}")
                
                # Получаем список всех файлов
                try:
                    files = os.listdir(music_dir)
                    logger.info(f"Найдено {len(files)} файлов в директории {music_dir}")
                    
                    # Проверяем каждый файл и исправляем права доступа при необходимости
                    for file_name in files:
                        file_path = os.path.join(music_dir, file_name)
                        if not os.path.isfile(file_path):
                            continue
                            
                        # Проверяем текущие права доступа
                        if not os.access(file_path, os.R_OK):
                            logger.warning(f"Не хватает прав доступа к файлу: {file_path}")
                            
                            try:
                                import stat
                                # Получаем текущие права доступа
                                current_perms = os.stat(file_path).st_mode
                                logger.info(f"Текущие права: {oct(current_perms)}")
                                
                                # Пробуем изменить права доступа для чтения
                                try:
                                    # Устанавливаем права на чтение для всех
                                    new_perms = current_perms | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
                                    os.chmod(file_path, new_perms)
                                    logger.info(f"Права доступа изменены на: {oct(os.stat(file_path).st_mode)}")
                                    
                                    # Проверяем, помогло ли изменение прав
                                    if os.access(file_path, os.R_OK):
                                        logger.info(f"Файл теперь доступен для чтения: {file_path}")
                                    else:
                                        logger.warning(f"Файл все еще недоступен после изменения прав: {file_path}")
                                except (PermissionError, OSError) as chmod_error:
                                    logger.warning(f"Не удалось изменить права доступа: {chmod_error}")
                                    
                                    # Пробуем через sudo
                                    try:
                                        import subprocess
                                        cmd = f"sudo chmod a+r '{file_path}'"
                                        process = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                                        if process.returncode == 0:
                                            logger.info(f"Права доступа успешно изменены через sudo для файла: {file_path}")
                                        else:
                                            logger.warning(f"Не удалось изменить права через sudo: {process.stderr}")
                                    except Exception as sudo_error:
                                        logger.warning(f"Ошибка при использовании sudo: {sudo_error}")
                            except Exception as e:
                                logger.warning(f"Ошибка при проверке/изменении прав доступа: {e}")
                except (PermissionError, OSError) as e:
                    logger.error(f"Ошибка при чтении файлов из директории {music_dir}: {e}")
            else:
                logger.info(f"Директория music не найдена в {token_dir}")
                
    except Exception as e:
        logger.error(f"Ошибка при проверке доступа к файлам Telegram Bot API: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Клавиатурное прерывание')
    except asyncio.CancelledError:
        logger.info('Прерывание')
    except Exception:
        logger.exception('Неизвестная ошибка')
