import logging.config
import asyncio
import pathlib
import os
from datetime import datetime
import re

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile, BotCommand, BotCommandScopeDefault
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
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

# Получаем URL локального Bot API сервера из .env файла
LOCAL_BOT_API = env_config.get('LOCAL_BOT_API', None)

# Инициализация бота и диспетчера
if LOCAL_BOT_API:
    bot = Bot(token=env_config.get('TELEGRAM_TOKEN'), base_url=LOCAL_BOT_API)
    logger.info(f'Используется локальный Telegram Bot API сервер: {LOCAL_BOT_API}')
else:
    bot = Bot(token=env_config.get('TELEGRAM_TOKEN'))
dp = Dispatcher()

# Настройки для Whisper
WHISPER_MODEL = env_config.get('WHISPER_MODEL', 'base')
USE_LOCAL_WHISPER = env_config.get('USE_LOCAL_WHISPER', 'True').lower() in ('true', '1', 'yes')
WHISPER_MODELS_DIR = env_config.get('WHISPER_MODELS_DIR', 'whisper_models')

# Ограничения для Telegram
MAX_MESSAGE_LENGTH = 4096  # максимальная длина сообщения в Telegram
MAX_CAPTION_LENGTH = 1024  # максимальная длина подписи к файлу

# Устанавливаем лимиты в зависимости от использования локального Bot API
if LOCAL_BOT_API:
    MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2000 МБ для локального Bot API
    logger.info(f'Используется увеличенный лимит файлов: {MAX_FILE_SIZE/1024/1024:.1f} МБ')
else:
    MAX_FILE_SIZE = 20 * 1024 * 1024    # 20 МБ для обычного Bot API

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
    )

@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer(
        "Главное меню бота:",
        reply_markup=get_main_keyboard()
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
        
        # Получаем информацию о файле
        file = await bot.get_file(file_id)
        file_size = file.file_size
        
        # Проверяем размер файла
        if file_size > MAX_FILE_SIZE:
            await processing_msg.edit_text(
                f"⚠️ Файл слишком большой ({file_size/1024/1024:.1f} МБ). "
                f"Максимальный размер файла для обработки: {MAX_FILE_SIZE/1024/1024:.1f} МБ.\n\n"
                f"Пожалуйста, отправьте аудиофайл меньшего размера или разделите его на части."
            )
            return
        
        # Путь для сохранения аудио
        file_path = f"{TEMP_AUDIO_DIR}/audio_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.ogg"
        
        # Скачиваем файл
        await processing_msg.edit_text("Скачиваю аудиофайл...")
        try:
            await bot.download(file, destination=file_path)
        except TelegramBadRequest as e:
            if "file is too big" in str(e).lower():
                await processing_msg.edit_text(
                    f"⚠️ Ошибка при скачивании: файл слишком большой для API Telegram (> 20 МБ).\n\n"
                    f"Размер файла: {file_size/1024/1024:.1f} МБ\n"
                    f"Ограничение Telegram: {MAX_FILE_SIZE/1024/1024:.1f} МБ\n\n"
                    f"Пожалуйста, используйте аудиофайл меньшего размера."
                )
                logger.error(f"Ошибка при скачивании файла - слишком большой размер: {e}")
            else:
                await processing_msg.edit_text(f"Ошибка при скачивании аудиофайла: {str(e)}")
                logger.exception(f"Ошибка Telegram при скачивании: {e}")
            return
        except Exception as e:
            await processing_msg.edit_text(f"Ошибка при скачивании аудиофайла: {str(e)}")
            logger.exception(f"Ошибка при скачивании файла: {e}")
            return
        
        # Проверяем, что файл успешно скачан
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            await processing_msg.edit_text("Ошибка: не удалось скачать аудиофайл или файл пустой.")
            return
            
        # Транскрибируем аудио
        await processing_msg.edit_text(f"Транскрибирую аудио {'с помощью локального Whisper' if USE_LOCAL_WHISPER else 'через OpenAI API'}...")
        transcription = await transcribe_audio(file_path)
        
        # Сохраняем транскрибацию в файл
        transcript_file_path = save_transcription_to_file(transcription, user_id)
        
        # Формируем текстовое сообщение
        message_text = f"🎤 Транскрибация аудио: {file_name}\n\n"
        
        # Если текст слишком длинный, разбиваем на части
        if len(transcription) > MAX_MESSAGE_LENGTH - len(message_text):
            # Отправляем превью транскрибации
            preview_length = MAX_MESSAGE_LENGTH - len(message_text) - 50  # Оставляем запас
            preview_text = transcription[:preview_length] + "...\n\n(полный текст в файле)"
            await processing_msg.edit_text(message_text + preview_text)
            
            # Отправляем файл с полной транскрибацией безопасным способом
            await send_file_safely(
                message,
                transcript_file_path,
                caption="Полная транскрибация аудио"
            )
        else:
            # Для коротких транскрибаций просто отправляем весь текст
            await processing_msg.edit_text(message_text + transcription)
            
            # Отправляем файл для удобства
            await send_file_safely(
                message,
                transcript_file_path,
                caption="Транскрибация аудио в виде файла"
            )
        
        # Удаляем временные файлы
        try:
            os.remove(file_path)
            # Оставляем файл с транскрибацией, можно настроить его автоматическое удаление позже
            # os.remove(transcript_file_path)
        except Exception as e:
            logger.exception(f"Ошибка при удалении временных файлов: {e}")
        
    except TelegramBadRequest as e:
        if "file is too big" in str(e).lower():
            await processing_msg.edit_text(
                "⚠️ Ошибка: Файл слишком большой для обработки в Telegram. "
                "Пожалуйста, отправьте аудиофайл меньшего размера (до 20 МБ)."
            )
        else:
            await processing_msg.edit_text(f"Произошла ошибка при обработке аудио: {str(e)}")
        logger.exception(f"Ошибка Telegram при обработке аудио: {e}")
    except Exception as e:
        await processing_msg.edit_text(f"Произошла ошибка при обработке аудио: {str(e)}")
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
