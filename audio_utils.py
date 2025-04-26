import os
import logging
import whisper
from datetime import datetime
from pathlib import Path

from create_bot import env_config

logger = logging.getLogger(__name__)

# Директория для хранения моделей Whisper
MODELS_DIR = env_config.get('WHISPER_MODELS_DIR', 'whisper_models')
os.makedirs(MODELS_DIR, exist_ok=True)

# Устанавливаем переменную окружения для кеширования моделей
os.environ['XDG_CACHE_HOME'] = str(Path(MODELS_DIR).absolute())

# Глобальная переменная для хранения модели
_whisper_model = None

def get_whisper_model(model_name="base"):
    """
    Загрузка модели Whisper (с кешированием)
    
    Args:
        model_name: Название модели Whisper 
                    (tiny, base, small, medium, large или их варианты с .en)
    
    Returns:
        Загруженная модель Whisper
    """
    global _whisper_model
    
    if _whisper_model is None or _whisper_model.name != model_name:
        logger.info(f"Загрузка модели Whisper: {model_name}")
        try:
            # Сообщаем о директории, где будет храниться модель
            model_dir = Path(MODELS_DIR) / model_name
            logger.info(f"Директория для моделей Whisper: {os.environ['XDG_CACHE_HOME']}")
            
            # Загружаем модель (скачивается автоматически, если её нет)
            _whisper_model = whisper.load_model(model_name)
            logger.info(f"Модель Whisper {model_name} успешно загружена")
        except Exception as e:
            logger.error(f"Ошибка при загрузке модели Whisper: {e}")
            raise
    
    return _whisper_model

def get_model_size(model_name):
    """
    Возвращает примерный размер модели Whisper в мегабайтах
    """
    model_sizes = {
        "tiny": 39,
        "tiny.en": 39,
        "base": 74,
        "base.en": 74,
        "small": 244,
        "small.en": 244,
        "medium": 769,
        "medium.en": 769,
        "large": 1550,
        "large-v2": 1550,
        "large-v3": 1550,
        "turbo": 809
    }
    return model_sizes.get(model_name, 0)

def list_downloaded_models():
    """
    Проверяет, какие модели уже скачаны
    
    Returns:
        Список скачанных моделей
    """
    try:
        models_path = Path(MODELS_DIR)
        if not models_path.exists():
            return []
            
        available_models = []
        for item in models_path.iterdir():
            if item.is_dir() and (item / "model.bin").exists():
                model_name = item.name
                size_mb = get_model_size(model_name)
                available_models.append({
                    "name": model_name,
                    "size_mb": size_mb,
                    "path": str(item)
                })
        
        return available_models
    except Exception as e:
        logger.error(f"Ошибка при проверке доступных моделей: {e}")
        return []

async def transcribe_with_whisper(file_path, language=None, model_name="base"):
    """
    Транскрибация аудиофайла с использованием локальной модели Whisper
    
    Args:
        file_path: Путь к аудиофайлу
        language: Язык аудио (если известен)
        model_name: Название модели Whisper
        
    Returns:
        Текст транскрибации
    """
    try:
        # Загрузка модели (или использование уже загруженной)
        model = get_whisper_model(model_name)
        
        # Параметры транскрибации
        options = {}
        if language:
            options["language"] = language
        
        # Транскрибация
        logger.info(f"Начало транскрибации файла: {file_path}")
        result = model.transcribe(file_path, **options)
        logger.info(f"Транскрибация завершена")
        
        return result["text"]
    
    except Exception as e:
        logger.exception(f"Ошибка при транскрибации с Whisper: {e}")
        raise

async def convert_audio_format(input_file, output_format="wav"):
    """
    Конвертирует аудиофайл в нужный формат для обработки Whisper
    
    Args:
        input_file: Путь к исходному аудиофайлу
        output_format: Целевой формат (по умолчанию wav)
        
    Returns:
        Путь к преобразованному файлу
    """
    try:
        import ffmpeg
        
        # Создаем временный файл
        temp_dir = "temp_audio"
        os.makedirs(temp_dir, exist_ok=True)
        output_file = f"{temp_dir}/converted_{datetime.now().strftime('%Y%m%d%H%M%S')}.{output_format}"
        
        # Конвертируем файл
        (
            ffmpeg
            .input(input_file)
            .output(output_file)
            .run(quiet=True, overwrite_output=True)
        )
        
        return output_file
        
    except Exception as e:
        logger.exception(f"Ошибка при конвертации аудио: {e}")
        raise 