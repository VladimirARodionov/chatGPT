import logging.config
import os
import pathlib

import sqlalchemy
import decouple
from aiogram import Bot

ENVIRONMENT = os.getenv("ENVIRONMENT", default="DEVELOPMENT")

def get_env_config() -> decouple.Config:
    """
    Creates and returns a Config object based on the environment setting.
    It uses .env.dev for development and .env for production.
    """
    env_files = {
        "DEVELOPMENT": ".env.dev",
        "PRODUCTION": ".env",
    }

    app_dir_path = pathlib.Path(__file__).resolve().parent
    env_file_name = env_files.get(ENVIRONMENT, ".env.dev")
    file_path = app_dir_path / env_file_name

    if not file_path.is_file():
        raise FileNotFoundError(f"Environment file not found: {file_path}")

    return decouple.Config(decouple.RepositoryEnv(file_path))

env_config = get_env_config()

if ENVIRONMENT == 'PRODUCTION':
    db_name = env_config.get('POSTGRES_DB')
    db_user = env_config.get('POSTGRES_USER')
    db_pass = env_config.get('POSTGRES_PASSWORD')
    db_host = 'postgres'
    db_port = env_config.get('POSTGRES_PORT') or '5432'
    db_string = 'postgresql://{}:{}@{}:{}/{}'.format(db_user, db_pass, db_host, db_port, db_name)
else:
    db_string = 'sqlite:///local.db'
db = sqlalchemy.create_engine(
    db_string,
    **(
        dict(pool_recycle=900, pool_size=100, max_overflow=3)
    )
)

logging.config.fileConfig(fname=pathlib.Path(__file__).resolve().parent / 'logging.ini',
                          disable_existing_loggers=False)
logging.getLogger('aiogram.dispatcher').propagate = False
logging.getLogger('aiogram.event').propagate = False

superusers = [int(superuser_id) for superuser_id in env_config.get('SUPERUSERS').split(',')]


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

# Настройки для Whisper
WHISPER_MODEL = env_config.get('WHISPER_MODEL', 'base')
USE_LOCAL_WHISPER = env_config.get('USE_LOCAL_WHISPER', 'True').lower() in ('true', '1', 'yes')
WHISPER_MODELS_DIR = env_config.get('WHISPER_MODELS_DIR', 'whisper_models')
# Порог размера файла (в МБ) для переключения на модель small
SMALL_MODEL_THRESHOLD_MB = int(env_config.get('SMALL_MODEL_THRESHOLD_MB', '20'))

# Директории для файлов
TEMP_AUDIO_DIR = "temp_audio"
DOWNLOADS_DIR = "downloads"
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
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
os.makedirs(TRANSCRIPTION_DIR, exist_ok=True)
os.makedirs(WHISPER_MODELS_DIR, exist_ok=True)
