import os
import logging
import whisper
from datetime import datetime, timedelta
from pathlib import Path
import time
import subprocess
import json

from create_bot import env_config, SMALL_MODEL_THRESHOLD_MB

logger = logging.getLogger(__name__)

# Директория для хранения моделей Whisper
MODELS_DIR = env_config.get('WHISPER_MODELS_DIR', 'whisper_models')
os.makedirs(MODELS_DIR, exist_ok=True)

# Устанавливаем переменную окружения для кеширования моделей
os.environ['XDG_CACHE_HOME'] = str(Path(MODELS_DIR).parent.absolute())

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
            # Используем единую директорию для моделей (без дублирования)
            logger.info(f"Директория для моделей Whisper: {MODELS_DIR}")
            
            # Проверяем наличие моделей
            model_files = []
            for filename in os.listdir(MODELS_DIR):
                filepath = os.path.join(MODELS_DIR, filename)
                if os.path.isfile(filepath) and filename.endswith('.pt'):
                    model_files.append(filepath)
            
            if model_files:
                logger.info(f"Найдены модели в директории: {model_files}")
            
            # Загружаем модель
            _whisper_model = whisper.load_model(model_name, download_root=MODELS_DIR)
            _current_model_name = model_name
            logger.info(f"Модель Whisper {model_name} успешно загружена")
            
            # Проверяем, не осталось ли дубликатов в подпапке whisper
            whisper_subdir = os.path.join(MODELS_DIR, "whisper")
            if os.path.exists(whisper_subdir) and os.path.isdir(whisper_subdir):
                # Проверяем, есть ли в подпапке модели
                has_models = False
                for filename in os.listdir(whisper_subdir):
                    if filename.endswith('.pt'):
                        has_models = True
                        break
                
                if has_models:
                    logger.warning(f"Обнаружены дублирующиеся модели в подпапке {whisper_subdir}. "
                                  f"Рекомендуется переместить их в {MODELS_DIR} и удалить подпапку для экономии места.")
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
        "large-v1": 1550,
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
        
        # Ищем все .pt файлы в основной директории
        for item in models_path.glob('*.pt'):
            if item.is_file():
                # Определяем имя модели из имени файла
                model_name = get_model_name_from_file(item.name)
                size_mb = get_model_size(model_name) or round(item.stat().st_size / (1024 * 1024))
                available_models.append({
                    "name": model_name,
                    "size_mb": size_mb,
                    "path": str(item),
                    "location": "основная директория"
                })
        
        # Также проверяем подпапку whisper, где могут быть .pt файлы (для обратной совместимости)
        whisper_dir = models_path / "whisper"
        if whisper_dir.exists() and whisper_dir.is_dir():
            for item in whisper_dir.glob('*.pt'):
                if item.is_file():
                    # Определяем имя модели из имени файла
                    model_name = get_model_name_from_file(item.name)
                    size_mb = get_model_size(model_name) or round(item.stat().st_size / (1024 * 1024))
                    available_models.append({
                        "name": model_name,
                        "size_mb": size_mb,
                        "path": str(item),
                        "location": "подпапка whisper (дубликат)"
                    })
        
        return available_models
    except Exception as e:
        logger.error(f"Ошибка при проверке доступных моделей: {e}")
        return []

def get_model_name_from_file(filename):
    """
    Определяет название модели из имени файла
    
    Args:
        filename: Имя файла модели (например, large-v3.pt)
        
    Returns:
        Название модели (например, large)
    """
    # Убираем расширение .pt
    base_name = filename.replace('.pt', '')
    
    # Обработка специальных случаев
    if base_name.endswith('-turbo') or 'turbo' in base_name:
        return "turbo"
        
    # Удаляем версии (v1, v2, v3)
    for version in ['-v1', '-v2', '-v3']:
        if version in base_name:
            base_name = base_name.split(version)[0]
            
    return base_name

async def transcribe_with_whisper(file_path, language=None, model_name="small", condition_on_previous_text=True):
    """
    Транскрибирует аудиофайл с помощью модели Whisper.
    
    Args:
        file_path: Путь к аудиофайлу
        language: Код языка (опционально)
        model_name: Название модели Whisper
        condition_on_previous_text: Если False, отключает авторегрессию и предотвращает зацикливание текста
        
    Returns:
        Результат транскрибации (словарь с текстом и метаданными) или None в случае ошибки
    """
    try:
        start_time = time.time()
        logger.info(f"Начинаем транскрибацию файла {file_path} с использованием модели {model_name}")
        logger.info(f"Параметр condition_on_previous_text: {condition_on_previous_text}")
        
        # Проверяем существование файла
        if not os.path.isfile(file_path):
            logger.error(f"Файл не найден: {file_path}")
            return None
            
        # Проверяем размер файла
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        if file_size == 0:
            logger.error(f"Файл пуст: {file_path}")
            return None
            
        logger.info(f"Размер файла: {file_size_mb:.2f} МБ")
        
        # Проверка валидности аудиофайла и получение его длительности
        audio_duration = 0
        try:
            # Используем ffprobe для проверки файла и получения длительности
            ffprobe_result = subprocess.run(
                [
                    "ffprobe", 
                    "-v", "error", 
                    "-show_entries", "format=duration", 
                    "-of", "json", 
                    file_path
                ],
                capture_output=True,
                text=True,
                check=False  # Не выбрасываем исключение при ненулевом коде возврата
            )
            
            # Проверяем, успешно ли выполнилась команда
            if ffprobe_result.returncode == 0:
                try:
                    output = json.loads(ffprobe_result.stdout)
                    if "format" in output and "duration" in output["format"]:
                        audio_duration = float(output["format"]["duration"])
                        logger.info(f"Определена длительность аудио: {audio_duration:.2f} сек")
                        
                        # Проверяем очень короткие файлы
                        if audio_duration < 0.5:
                            logger.warning(f"Очень короткий аудиофайл ({audio_duration:.2f} сек), возможны проблемы с транскрибацией")
                    else:
                        logger.warning("ffprobe не вернул информацию о длительности")
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Ошибка при парсинге вывода ffprobe: {e}")
            else:
                logger.warning(f"ffprobe вернул ошибку: {ffprobe_result.stderr}")
                logger.warning("Файл может не быть валидным аудиофайлом или иметь неподдерживаемый формат")
        except Exception as e:
            logger.warning(f"Ошибка при проверке аудиофайла: {e}")
        
        # Дополнительная проверка файла
        try:
            import wave
            if file_path.lower().endswith('.wav'):
                try:
                    with wave.open(file_path, 'rb') as wave_file:
                        frames = wave_file.getnframes()
                        rate = wave_file.getframerate()
                        duration = frames / float(rate)
                        channels = wave_file.getnchannels()
                        sample_width = wave_file.getsampwidth()
                        
                        logger.info(f"WAV файл: {frames} фреймов, {rate} Гц, {channels} каналов, {sample_width} байт/сэмпл, длительность: {duration:.2f} сек")
                        
                        # Если ffprobe не определил длительность, используем информацию из wave
                        if audio_duration == 0:
                            audio_duration = duration
                            
                        # Проверяем, что файл имеет фреймы и не пустой
                        if frames == 0:
                            logger.error("WAV файл не содержит аудио данных (0 фреймов)")
                            return None
                except Exception as wave_error:
                    logger.warning(f"Не удалось прочитать WAV файл: {wave_error}")
        except ImportError:
            logger.warning("Модуль wave не установлен, пропускаем дополнительную проверку WAV")
        
        # Если длительность не удалось определить, оцениваем по размеру файла
        if audio_duration == 0:
            estimated_duration = file_size_mb * 60  # Приблизительно 1MB ~ 1 минута для аудио с битрейтом 128 kbps
            logger.info(f"Используем оценочную длительность на основе размера файла: {estimated_duration:.2f} сек")
            audio_duration = estimated_duration
            
        # Дополнительная проверка файла с помощью ffmpeg
        try:
            # Получаем информацию об аудио-каналах и убеждаемся, что аудио содержит данные
            ffmpeg_result = subprocess.run(
                [
                    "ffmpeg", 
                    "-v", "error", 
                    "-i", file_path, 
                    "-f", "null", 
                    "-"
                ],
                capture_output=True,
                text=True
            )
            
            # Если ffmpeg выдал ошибку, это может указывать на проблемы с файлом
            if ffmpeg_result.returncode != 0:
                logger.error(f"Ошибка при проверке файла с помощью ffmpeg: {ffmpeg_result.stderr}")
                
                # Если ошибка связана с данными аудио, можно попробовать исправить
                if "Invalid data found" in ffmpeg_result.stderr:
                    logger.warning("Обнаружены некорректные данные в аудиофайле, пробуем исправить")
                    
                    # Создаем новый файл с исправленными данными
                    fixed_file_path = f"{file_path}.fixed.wav"
                    fix_result = subprocess.run(
                        [
                            "ffmpeg", 
                            "-v", "warning", 
                            "-i", file_path, 
                            "-ar", "16000",  # Устанавливаем частоту дискретизации 16kHz
                            "-ac", "1",      # Преобразуем в моно
                            "-c:a", "pcm_s16le",  # Используем стандартный формат PCM
                            fixed_file_path
                        ],
                        capture_output=True,
                        text=True
                    )
                    
                    if fix_result.returncode == 0 and os.path.exists(fixed_file_path) and os.path.getsize(fixed_file_path) > 0:
                        logger.info(f"Аудиофайл исправлен и сохранен в {fixed_file_path}")
                        file_path = fixed_file_path  # Используем исправленный файл для транскрибации
                    else:
                        logger.error(f"Не удалось исправить аудиофайл: {fix_result.stderr}")
                        return None
        except Exception as e:
            logger.warning(f"Ошибка при выполнении проверки через ffmpeg: {e}")
            
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
        
        # Проверяем, нужно ли использовать модель меньшего размера
        should_switch, smaller_model = should_use_smaller_model(file_size_mb, model_name)
        
        if is_large_file:
            logger.info(f"Обрабатываем большой аудио файл ({file_size_mb:.2f} МБ), применяем оптимизации для памяти")
            
        # Выполняем транскрипцию
        transcribe_options = {
            "language": language,
            "verbose": None,
            "task": "transcribe",
            "condition_on_previous_text": condition_on_previous_text,
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
            if should_switch:
                logger.info(f"Для большого файла временно переключаемся с модели {model_name} на {smaller_model} для экономии памяти")
                try:
                    model = get_whisper_model(smaller_model)
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
            
            # Безопасно загружаем аудиофайл перед транскрибацией
            try:
                import torch
                import numpy as np
                from whisper.audio import SAMPLE_RATE, N_FRAMES, log_mel_spectrogram, pad_or_trim
                
                # Для безопасной загрузки аудио используем функцию из whisper
                logger.info("Загружаем аудиофайл перед транскрибацией")
                
                # Проверяем, что файл существует и не равен 0
                if not os.path.isfile(file_path) or os.path.getsize(file_path) == 0:
                    logger.error(f"Файл не найден или пуст: {file_path}")
                    return None
                
                # Пробуем загрузить аудио напрямую через ffmpeg
                try:
                    cmd = ["ffmpeg", "-i", file_path, "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    output, stderr = process.communicate()
                    
                    if process.returncode != 0:
                        logger.error(f"Ошибка при загрузке аудио через ffmpeg: {stderr.decode()}")
                        return None
                        
                    if len(output) == 0:
                        logger.error("Загруженное аудио не содержит данных")
                        return None
                        
                    # Преобразуем байты в numpy array
                    audio = np.frombuffer(output, np.int16).astype(np.float32) / 32768.0
                    
                    # Проверяем, что audio не пустой и содержит данные
                    if len(audio) == 0:
                        logger.error("Аудио не содержит данных после загрузки")
                        return None
                    
                    logger.info(f"Успешно загружено аудио длиной {len(audio) / SAMPLE_RATE:.2f} сек")
                    
                    # Вычисляем мел-спектрограмму
                    mel = log_mel_spectrogram(audio)
                    
                    # Проверяем, что mel не пустой
                    if mel.shape[0] == 0 or mel.shape[1] == 0:
                        logger.error(f"Мел-спектрограмма имеет неверный размер: {mel.shape}")
                        return None
                    
                    # Проверяем, что тензор не содержит NaN
                    if torch.isnan(mel).any():
                        logger.error("Мел-спектрограмма содержит NaN значения")
                        return None
                    
                except Exception as e:
                    logger.error(f"Ошибка при предварительной обработке аудио: {e}")
                    # Продолжаем с обычной загрузкой через whisper
            except ImportError:
                logger.warning("Не удалось выполнить предварительную проверку аудио, продолжаем с обычной загрузкой")
            except Exception as e:
                logger.warning(f"Непредвиденная ошибка при проверке аудио: {e}")
                
            # Выполняем транскрибацию с обработкой потенциальных ошибок тензора
            try:
                result = model.transcribe(file_path, **transcribe_options)
            except RuntimeError as e:
                # Обрабатываем ошибку reshape тензора
                if "cannot reshape tensor of 0 elements" in str(e):
                    logger.error(f"Ошибка тензора нулевого размера при транскрибации: {e}")
                    logger.info("Пробуем конвертировать файл в стандартный формат и повторить попытку")
                    
                    # Создаем новый файл с исправленными данными
                    fixed_file_path = f"{file_path}.fixed.wav"
                    try:
                        convert_result = subprocess.run(
                            [
                                "ffmpeg", 
                                "-y",  # Перезаписать существующий файл
                                "-v", "warning", 
                                "-i", file_path, 
                                "-ar", "16000",  # Устанавливаем частоту дискретизации 16kHz (как в примерах Whisper)
                                "-ac", "1",      # Преобразуем в моно
                                "-c:a", "pcm_s16le",  # Используем стандартный формат PCM
                                fixed_file_path
                            ],
                            capture_output=True,
                            text=True
                        )
                        
                        if convert_result.returncode == 0 and os.path.exists(fixed_file_path) and os.path.getsize(fixed_file_path) > 0:
                            logger.info(f"Аудиофайл конвертирован и сохранен в {fixed_file_path}, пробуем транскрибировать заново")
                            
                            # Пробуем транскрибировать исправленный файл
                            try:
                                result = model.transcribe(fixed_file_path, **transcribe_options)
                            except Exception as retry_error:
                                logger.error(f"Не удалось транскрибировать даже после исправления файла: {retry_error}")
                                return None
                        else:
                            logger.error(f"Не удалось конвертировать файл: {convert_result.stderr}")
                            return None
                    except Exception as convert_error:
                        logger.error(f"Ошибка при конвертации файла: {convert_error}")
                        return None
                else:
                    # Другие ошибки RuntimeError
                    logger.error(f"Ошибка RuntimeError при транскрибации: {e}")
                    return None
            except Exception as transcribe_error:
                logger.exception(f"Ошибка при выполнении транскрибации: {transcribe_error}")
                return None
            
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
            
            # Проверяем, если длительность аудио равна 0, попробуем получить её через ffprobe
            if audio_duration == 0:
                logger.warning("Whisper вернул нулевую длительность аудио, пробуем получить длительность через ffprobe")
                try:
                    # Выполняем команду ffprobe для получения информации о длительности
                    ffprobe_result = subprocess.run(
                        [
                            "ffprobe", 
                            "-v", "error", 
                            "-show_entries", "format=duration", 
                            "-of", "json", 
                            file_path
                        ],
                        capture_output=True,
                        text=True,
                        check=True
                    )
                    
                    # Парсим результат
                    if ffprobe_result.returncode == 0:
                        output = json.loads(ffprobe_result.stdout)
                        audio_duration = float(output["format"]["duration"])
                        logger.info(f"Получена длительность через ffprobe: {audio_duration:.2f} сек")
                        
                        # Обновляем результат с правильной длительностью
                        result["duration"] = audio_duration
                    else:
                        logger.warning(f"Не удалось получить длительность через ffprobe. Код возврата: {ffprobe_result.returncode}")
                except (subprocess.SubprocessError, ValueError, KeyError, json.JSONDecodeError, FileNotFoundError) as e:
                    logger.warning(f"Ошибка при получении длительности через ffprobe: {e}")
                except Exception as e:
                    logger.warning(f"Непредвиденная ошибка при получении длительности: {e}")
                
                # Если все методы определения длительности не сработали, оцениваем по размеру файла
                if audio_duration == 0:
                    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    # Приблизительно 1MB ~ 1 минута для аудио с битрейтом 128 kbps
                    estimated_duration = file_size_mb * 60
                    logger.info(f"Используем оценочную длительность на основе размера файла: {estimated_duration:.2f} сек")
                    audio_duration = estimated_duration
                    result["duration"] = audio_duration
            
            ratio = audio_duration / elapsed_time if elapsed_time > 0 else 0
            
            # Добавляем информацию о том, что модель была переключена
            actual_model_used = model_name
            if is_large_file:
                # Используем функцию для определения модели
                was_switched, smaller_model_name = should_use_smaller_model(file_size_mb, model_name)
                if was_switched:
                    actual_model_used = smaller_model_name
                
            # Добавляем метаданные о транскрибации
            result["whisper_model"] = actual_model_used
            result["processing_time"] = elapsed_time
            result["file_size_mb"] = file_size_mb
            result["processing_ratio"] = ratio
            
            logger.info(f"Транскрипция завершена за {elapsed_time:.2f} сек. " 
                        f"Длительность аудио: {audio_duration:.2f} сек. "
                        f"Соотношение: {ratio:.2f}x")
                        
            # Удаляем временные исправленные файлы
            try:
                if 'fixed_file_path' in locals() and os.path.exists(fixed_file_path):
                    os.remove(fixed_file_path)
                    logger.info(f"Удален временный исправленный файл: {fixed_file_path}")
            except Exception as cleanup_error:
                logger.warning(f"Ошибка при удалении временного файла: {cleanup_error}")
                
            return result
        except Exception as transcribe_error:
            logger.exception(f"Ошибка при выполнении транскрибации: {transcribe_error}")
            return None
            
    except Exception as e:
        logger.exception(f"Ошибка при транскрипции файла {file_path}: {e}")
        return None

async def extract_audio_from_video(video_file, output_format="wav"):
    """
    Извлекает аудиодорожку из видеофайла для обработки Whisper
    
    Args:
        video_file: Путь к исходному видеофайлу
        output_format: Целевой формат аудио (по умолчанию wav)
        
    Returns:
        Путь к извлеченному аудиофайлу
        
    Raises:
        Exception: Если видео не содержит аудиодорожки или произошла ошибка при извлечении
    """
    try:
        import ffmpeg
        
        # Проверяем, существует ли файл
        if not os.path.exists(video_file):
            raise FileNotFoundError(f"Видеофайл не найден: {video_file}")
        
        # Создаем временный файл
        temp_dir = "temp_audio"
        os.makedirs(temp_dir, exist_ok=True)
        output_file = f"{temp_dir}/extracted_{datetime.now().strftime('%Y%m%d%H%M%S')}.{output_format}"
        
        # Извлекаем аудио из видео
        # Используем параметры для оптимальной обработки Whisper:
        # - acodec='pcm_s16le': 16-bit PCM (поддерживается Whisper)
        # - ac=1: моно канал (уменьшает размер файла)
        # - ar='16000': частота дискретизации 16kHz (стандарт для Whisper)
        try:
            (
                ffmpeg
                .input(video_file)
                .output(output_file, acodec='pcm_s16le', ac=1, ar='16000')
                .run(quiet=True, overwrite_output=True, capture_stderr=True)
            )
        except ffmpeg.Error as e:
            error_message = e.stderr.decode() if e.stderr else str(e)
            # Проверяем, есть ли аудиодорожка в видео
            if "Stream map" in error_message or "does not contain any stream" in error_message:
                raise ValueError(f"Видеофайл не содержит аудиодорожки: {video_file}")
            raise Exception(f"Ошибка FFmpeg при извлечении аудио: {error_message}")
        
        # Проверяем, что файл был создан и не пустой
        if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
            raise Exception(f"Не удалось извлечь аудио из видео. Результирующий файл пуст или не создан.")
        
        logger.info(f"Аудио успешно извлечено из видео: {video_file} -> {output_file}")
        return output_file
        
    except Exception as e:
        logger.exception(f"Ошибка при извлечении аудио из видео: {e}")
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

def should_use_smaller_model(file_size_mb, model_name):
    """
    Определяет, требуется ли переключение на модель меньшего размера
    для данного размера файла и модели.
    
    Args:
        file_size_mb: Размер файла в МБ
        model_name: Название текущей модели
    
    Returns:
        (bool, str): Кортеж (нужно ли менять модель, название новой модели)
    """
    # Модели, требующие много памяти
    heavy_models = ["medium", "large", "large-v2", "large-v3", "turbo"]
    
    # Пороги размера файла для переключения на модель меньшего размера
    # Значение берется из конфигурации .env файла (SMALL_MODEL_THRESHOLD_MB)
    file_size_threshold = SMALL_MODEL_THRESHOLD_MB  # МБ
    
    # Проверяем, нужно ли переключаться на меньшую модель
    if file_size_mb > file_size_threshold and model_name in heavy_models:
        # По умолчанию используем small для больших файлов
        return True, "small"
    
    # Модель менять не нужно
    return False, model_name

def should_condition_on_previous_text(file_size_mb):
    """
    Определяет, требуется ли переключение на condition_on_previous_text
    для данного размера файла.

    Args:
        file_size_mb: Размер файла в МБ

    Returns:
        bool: нужно ли менять condition_on_previous_text
    """
    # Пороги размера файла для переключения
    file_size_threshold = 2  # МБ

    # Проверяем, нужно ли переключаться на меньшую модель
    if file_size_mb > file_size_threshold:
        return False
    return True

def predict_processing_time(file_path, model_name):
    """
    Предсказывает примерное время обработки аудиофайла с использованием Whisper.
    
    Аргументы:
        file_path (str): Путь к аудиофайлу
        model_name (str): Название модели Whisper (tiny, base, small, medium, large-v1, large-v2, large-v3)
        
    Возвращает:
        datetime.timedelta: Предполагаемое время обработки
    """
    # Получаем размер файла в МБ
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    
    # Определяем тип файла (видео или аудио) по расширению
    file_ext = os.path.splitext(file_path)[1].lower()
    video_extensions = ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.3gp', '.ogv']
    is_video_file = file_ext in video_extensions
    
    # Проверяем наличие ffprobe для получения длительности
    audio_duration_seconds = 0
    try:
        # Выполняем команду ffprobe для получения информации о длительности
        result = subprocess.run(
            [
                "ffprobe", 
                "-v", "error", 
                "-show_entries", "format=duration", 
                "-of", "json", 
                file_path
            ],
            capture_output=True,
            text=True
        )
        
        # Парсим результат
        if result.returncode == 0:
            output = json.loads(result.stdout)
            audio_duration_seconds = float(output["format"]["duration"])
        else:
            # Если ffprobe не удалось получить длительность, оцениваем по размеру файла
            if is_video_file:
                # Для видео: размер файла намного больше из-за видеодорожки
                # Приблизительно 1 МБ видео ~ 0.45 минуты аудио (зависит от качества видео)
                # Это учитывает, что большая часть размера видео - это видеодорожка
                audio_duration_seconds = file_size_mb * 20  # 20 секунд на МБ для видео
            else:
                # Для аудио: приблизительно 1MB ~ 1 минута для аудио с битрейтом 128 kbps
                audio_duration_seconds = file_size_mb * 60
    except (subprocess.SubprocessError, ValueError, KeyError, json.JSONDecodeError, FileNotFoundError):
        # Если произошла ошибка, оцениваем по размеру файла
        if is_video_file:
            # Для видео: используем меньший коэффициент (учитывая, что большая часть размера - видеодорожка)
            audio_duration_seconds = file_size_mb * 27  # 27 секунд на МБ для видео
        else:
            # Для аудио: стандартная оценка
            audio_duration_seconds = file_size_mb * 60
    
    # Коэффициенты скорости обработки для разных моделей (относительно реального времени)
    # Это приблизительные значения, которые могут отличаться в зависимости от оборудования
    # Увеличены в ~2 раза, так как фактическая обработка происходит быстрее
    speed_factors = {
        "tiny": 10.0,    # примерно в 10 раз быстрее реального времени
        "base": 6.0,     # примерно в 6 раз быстрее реального времени
        "small": 3.0,    # примерно в 3 раза быстрее реального времени
        "medium": 2.0,   # примерно в 2.0 раза быстрее реального времени
        "large": 1.0,    # примерно реального времени
        "large-v1": 1.0, # аналогично large
        "large-v2": 0.8, # немного медленнее чем large-v1
        "large-v3": 0.7, # самая медленная
        "turbo": 1.7    # примерно в 1.7 раза быстрее реального времени
    }
    
    # Получаем коэффициент для выбранной модели, или используем значение по умолчанию
    speed_factor = speed_factors.get(model_name.lower(), 2.0)  # Увеличено значение по умолчанию
    
    # Фиксированное время на инициализацию модели (в секундах)
    init_time = {
        "tiny": 1,       # Уменьшено время инициализации
        "base": 2,
        "small": 3,
        "medium": 5,
        "large": 8,
        "large-v1": 8,
        "large-v2": 10,
        "large-v3": 12,
        "turbo": 10      # Аналогично large-v2
    }.get(model_name.lower(), 5)  # Уменьшено время по умолчанию
    
    # Фактор для файлов большого размера - обработка замедляется
    size_penalty = 1.0
    if file_size_mb > 15:
        # Для файлов > 15 МБ добавляем штраф времени
        size_penalty = 1.0 + (file_size_mb - 15) * 0.015  # Уменьшено влияние размера файла
    
    # Рассчитываем время обработки
    processing_time_seconds = (audio_duration_seconds / speed_factor) * size_penalty + init_time
    
    # Добавляем 5% запаса для учета других факторов (уменьшено с 10%)
    processing_time_seconds *= 1.05
    
    return timedelta(seconds=int(processing_time_seconds)) 