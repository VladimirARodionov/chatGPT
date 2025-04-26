import os
import logging
import whisper
from datetime import datetime
from pathlib import Path
import time

from create_bot import env_config

logger = logging.getLogger(__name__)

# Директория для хранения моделей Whisper
MODELS_DIR = env_config.get('WHISPER_MODELS_DIR', 'whisper_models')
os.makedirs(MODELS_DIR, exist_ok=True)

# Устанавливаем переменную окружения для кеширования моделей
os.environ['XDG_CACHE_HOME'] = str(Path(MODELS_DIR).absolute())

# Глобальная переменная для хранения модели
_whisper_model = None
_current_model_name = None

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
    global _current_model_name
    
    if _whisper_model is None or _current_model_name != model_name:
        logger.info(f"Загрузка модели Whisper: {model_name}")
        try:
            # Сообщаем о директории, где будет храниться модель
            model_dir = Path(MODELS_DIR) / model_name
            logger.info(f"Директория для моделей Whisper: {os.environ['XDG_CACHE_HOME']}")
            
            # Загружаем модель (скачивается автоматически, если её нет)
            _whisper_model = whisper.load_model(model_name)
            _current_model_name = model_name
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

async def transcribe_with_whisper(file_path, language=None, model_name="small"):
    """
    Транскрибирует аудио файл с помощью модели Whisper.
    
    Args:
        file_path: Путь к аудио файлу
        language: Код языка (если None, Whisper попытается определить язык)
        model_name: Имя модели Whisper для использования
        
    Returns:
        Словарь с результатами транскрипции или None в случае ошибки
    """
    try:
        start_time = time.time()
        logger.info(f"Начата транскрипция файла: {file_path}")
        
        # Проверяем существование файла
        if not os.path.exists(file_path):
            logger.error(f"Файл не найден: {file_path}")
            return None
            
        # Проверяем размер файла
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        logger.info(f"Размер файла: {file_size_mb:.2f} МБ")
        
        # Дополнительная проверка файла
        try:
            import wave
            if file_path.lower().endswith('.wav'):
                try:
                    with wave.open(file_path, 'rb') as wave_file:
                        frames = wave_file.getnframes()
                        rate = wave_file.getframerate()
                        duration = frames / float(rate)
                        logger.info(f"WAV файл: {frames} фреймов, {rate} Гц, длительность: {duration:.2f} сек")
                except Exception as wave_error:
                    logger.warning(f"Не удалось прочитать WAV файл: {wave_error}")
        except ImportError:
            logger.warning("Модуль wave не установлен, пропускаем дополнительную проверку WAV")
            
        # Загружаем модель
        try:
            model = get_whisper_model(model_name)
            if model is None:
                logger.error("Не удалось загрузить модель Whisper")
                return None
        except Exception as model_error:
            logger.exception(f"Ошибка при загрузке модели Whisper: {model_error}")
            return None
            
        # Проверяем свободную память перед началом транскрипции
        # Если файл большой, используем более экономичный подход
        is_large_file = file_size_mb > 20  # Файлы больше 20 МБ считаем большими
        
        if is_large_file:
            logger.info(f"Обрабатываем большой аудио файл ({file_size_mb:.2f} МБ), применяем оптимизации для памяти")
            
        # Выполняем транскрипцию
        transcribe_options = {
            "language": language,
            "verbose": None,
            "task": "transcribe",
        }
        
        # Для больших файлов добавляем дополнительные опции оптимизации
        if is_large_file:
            # Используем fp16 для экономии памяти
            transcribe_options["fp16"] = True
            
            # Настройки для больших аудиофайлов
            transcribe_options["beam_size"] = 2  # Уменьшаем beam_size для экономии памяти
            transcribe_options["best_of"] = 1    # Ограничиваем количество кандидатов
            
            # Если файл очень большой, уменьшаем еще больше
            if file_size_mb > 100:
                # При высоких температурах результат менее точный, но требует меньше памяти
                transcribe_options["temperature"] = 0.2
                
            logger.info(f"Применяем оптимизации для большого файла: {transcribe_options}")
            
            # Можно также переключиться на более легкую модель, если текущая слишком тяжелая
            if model_name in ["medium", "large", "large-v2", "large-v3", "turbo"]:
                logger.info(f"Для большого файла временно переключаемся с модели {model_name} на small для экономии памяти")
                try:
                    model = get_whisper_model("small")
                    if model is None:
                        logger.error("Не удалось загрузить облегченную модель для большого файла")
                        return None
                except Exception as model_error:
                    logger.warning(f"Ошибка при переключении на облегченную модель: {model_error}, продолжаем с исходной")
                
        # Выполняем транскрипцию
        try:
            # Доступные параметры для DecodingOptions в whisper:
            # language, task, temperature, sample_len, best_of, beam_size, patience,
            # length_penalty, prompt, prefix, suffix, suppress_tokens, without_timestamps,
            # max_initial_timestamp, fp16, suppress_blank
            logger.info(f"Запускаем транскрибацию с опциями: {transcribe_options}")
            
            # Если размер файла очень большой, добавляем дополнительное логирование и обработку участками
            if file_size_mb > 200:
                logger.info("Очень большой файл (>200МБ), возможны проблемы с памятью")
                
                # Если есть библиотека torch, проверяем доступную память GPU
                try:
                    import torch
                    if torch.cuda.is_available():
                        free_memory = torch.cuda.get_device_properties(0).total_memory - torch.cuda.memory_allocated(0)
                        free_memory_mb = free_memory / (1024 * 1024)
                        logger.info(f"Доступная память GPU: {free_memory_mb:.2f} МБ")
                        
                        # Если свободной памяти мало, выдаем предупреждение
                        if free_memory_mb < file_size_mb * 2:  # Примерное правило: нужно в 2 раза больше памяти, чем размер файла
                            logger.warning(f"Недостаточно памяти GPU для обработки файла. Может произойти ошибка Out of Memory.")
                except ImportError:
                    logger.warning("Не удалось проверить память GPU, так как torch недоступен")
                except Exception as e:
                    logger.warning(f"Ошибка при проверке памяти GPU: {e}")
                    
            # Выполняем транскрибацию
            result = model.transcribe(file_path, **transcribe_options)
            
            if not result:
                logger.error("Транскрибация вернула пустой результат")
                return None
                
            if 'text' not in result:
                logger.error(f"Транскрибация вернула неожиданный формат результата: {result.keys() if hasattr(result, 'keys') else type(result)}")
                return None
                
            # Проверяем, не пустой ли текст
            if not result['text'].strip():
                logger.warning("Транскрибация вернула пустой текст")
                
            # Засекаем время выполнения
            elapsed_time = time.time() - start_time
            audio_duration = result.get("duration", 0)
            ratio = audio_duration / elapsed_time if elapsed_time > 0 else 0
            
            logger.info(f"Транскрипция завершена за {elapsed_time:.2f} сек. " 
                        f"Длительность аудио: {audio_duration:.2f} сек. "
                        f"Соотношение: {ratio:.2f}x")
                        
            return result
        except Exception as transcribe_error:
            logger.exception(f"Ошибка при выполнении транскрибации: {transcribe_error}")
            return None
            
    except Exception as e:
        logger.exception(f"Ошибка при транскрипции файла {file_path}: {e}")
        return None

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