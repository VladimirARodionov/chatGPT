import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta
import multiprocessing

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from openai import OpenAI
from concurrent.futures import ProcessPoolExecutor, Future

from audio_utils import predict_processing_time, should_use_smaller_model, convert_audio_format, \
    transcribe_with_whisper, should_condition_on_previous_text, extract_audio_from_video
from create_bot import MAX_FILE_SIZE, bot, MAX_MESSAGE_LENGTH, USE_LOCAL_WHISPER, TEMP_AUDIO_DIR, DOWNLOADS_DIR, \
    LOCAL_BOT_API, env_config, WHISPER_MODEL, STANDARD_API_LIMIT, superusers
from db_service import check_message_limit, get_queue, add_to_queue, set_active_queue, set_finished_queue, \
    set_cancelled_queue, get_db_session, get_first_from_queue, get_active_tasks, reset_active_tasks, is_task_cancelled
from files_service import cleanup_temp_files, save_transcription_to_file, download_voice, \
    get_file_path_direct, download_large_file_direct, send_file_safely
from models import TranscribeQueue

logger = logging.getLogger(__name__)

# Пул процессов для CPU-интенсивных операций (можно убить процесс при отмене)
process_executor = ProcessPoolExecutor(max_workers=3)

# Словарь для отслеживания активных процессов транскрибации по task_id
# Формат: {task_id: {'process': Process, 'future': Future, 'pid': int}}
active_transcription_processes = {}

# Блокировка для безопасного доступа к словарю процессов
processes_lock = asyncio.Lock()

# Хранение ссылки на задачу фонового обработчика
background_worker_task = None
# Флаг для автоматического перезапуска обработчика
AUTO_RESTART_PROCESSOR = True
# Максимальное количество последовательных перезапусков
MAX_AUTO_RESTARTS = 5
# Счетчик перезапусков
auto_restart_counter = 0
# Время последнего перезапуска
last_restart_time = None
# Блокировка для предотвращения одновременного запуска нескольких экземпляров обработчика
processor_lock = asyncio.Lock()


def format_processing_time(time_value):
    """Форматирует время обработки в читаемый формат: часы:минуты:секунды или минуты:секунды или секунды
    
    Args:
        time_value: Время в секундах (может быть float/int) или timedelta объект
    
    Returns:
        Строка с отформатированным временем
    """
    # Если это timedelta, преобразуем в секунды
    if isinstance(time_value, timedelta):
        total_seconds = int(time_value.total_seconds())
    else:
        total_seconds = int(time_value)
    
    if total_seconds < 60:
        return f"{total_seconds} сек"
    
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    
    if hours > 0:
        return f"{hours} ч {minutes} мин {secs} сек"
    elif minutes > 0:
        return f"{minutes} мин {secs} сек"
    else:
        return f"{secs} сек"


async def handle_audio_service(message: Message):
    user_id = message.from_user.id

    if not USE_LOCAL_WHISPER and not check_message_limit(user_id):
        await message.answer("Вы достигли дневного лимита в 50 сообщений. Попробуйте завтра!")
        return

    # Определяем тип файла
    is_video = message.video is not None or message.video_note is not None
    is_audio = message.voice is not None or message.audio is not None
    is_document = message.document is not None
    
    # Если это документ, проверяем его тип по MIME-типу или расширению
    if is_document and not (is_video or is_audio):
        mime_type = message.document.mime_type or ""
        file_name = message.document.file_name or ""
        
        # Видео форматы
        video_mime_types = ["video/", "application/vnd.apple.mpegurl"]
        video_extensions = [".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".wmv", ".m4v", ".3gp", ".ogv"]
        
        # Аудио форматы
        audio_mime_types = ["audio/"]
        audio_extensions = [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".wma", ".opus", ".amr"]
        
        file_name_lower = file_name.lower()
        
        # Проверяем, является ли документ видео
        if any(mime_type.startswith(vt) for vt in video_mime_types) or \
           any(file_name_lower.endswith(ext) for ext in video_extensions):
            is_video = True
        # Проверяем, является ли документ аудио
        elif any(mime_type.startswith(at) for at in audio_mime_types) or \
             any(file_name_lower.endswith(ext) for ext in audio_extensions):
            is_audio = True
    
    # Отправляем сообщение о начале обработки
    file_type_text = "видео" if is_video else "аудио"
    processing_msg = await message.answer(f"Загружаю и обрабатываю {file_type_text}...")

    try:
        # Определяем, что за файл пришел
        if is_video:
            if message.video:
                file_id = message.video.file_id
            elif message.video_note:
                file_id = message.video_note.file_id
            elif message.document:
                file_id = message.document.file_id
            else:
                await processing_msg.edit_text("Ошибка: не удалось определить файл видео")
                return
        elif is_audio:
            if message.voice:
                file_id = message.voice.file_id
            elif message.audio:
                file_id = message.audio.file_id
            elif message.document:
                file_id = message.document.file_id
            else:
                await processing_msg.edit_text("Ошибка: не удалось определить файл аудио")
                return
        else:
            await processing_msg.edit_text("Ошибка: неподдерживаемый тип файла")
            return

        # Имя исходного файла
        file_name = "Голосовое сообщение"
        if message.audio and message.audio.file_name:
            file_name = message.audio.file_name
        elif message.video and message.video.file_name:
            file_name = message.video.file_name
        elif message.document and message.document.file_name:
            file_name = message.document.file_name
        elif message.video_note:
            file_name = "Видеосообщение"

        # Определяем расширение файла для сохранения
        # Сохраняем файлы в папку downloads для загрузки и транскрибации
        if is_video:
            # Для видео сохраняем в исходном формате, затем извлечем аудио
            file_ext = "mp4"  # По умолчанию для видео
            if message.video and message.video.file_name:
                file_ext = os.path.splitext(message.video.file_name)[1][1:] or "mp4"
            elif message.document and message.document.file_name:
                file_ext = os.path.splitext(message.document.file_name)[1][1:] or "mp4"
            file_path = f"{DOWNLOADS_DIR}/video_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{file_ext}"
        else:
            # Путь для сохранения аудио
            if message.document and message.document.file_name:
                # Сохраняем с оригинальным расширением для документов
                file_ext = os.path.splitext(message.document.file_name)[1][1:] or "ogg"
                file_path = f"{DOWNLOADS_DIR}/audio_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{file_ext}"
            else:
                file_path = f"{DOWNLOADS_DIR}/audio_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.ogg"

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
                        f"• Сократите длительность {'видео' if is_video else 'аудио'}\n"
                        f"• Разделите длинное {'видео' if is_video else 'аудио'} на несколько частей\n"
                        f"• Используйте формат с большим сжатием"
                    )
                    return

                # Проверяем, необходимо ли использовать прямую загрузку
                if file_size <= STANDARD_API_LIMIT:
                    download_text = f"Скачиваю {file_type_text}файл стандартным методом..."
                    await processing_msg.edit_text(download_text)
                    download_success = await download_voice(file, file_path)

                    if not download_success:
                        await processing_msg.edit_text(
                            f"⚠️ Не удалось скачать {file_type_text}файл стандартным методом. "
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
                    f"• Сократите длительность {'видео' if is_video else 'аудио'}\n"
                    f"• Разделите длинное {'видео' if is_video else 'аудио'} на несколько частей\n"
                    f"• Используйте формат с большим сжатием"
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
            await processing_msg.edit_text(f"Ошибка: не удалось скачать {file_type_text}файл или файл пустой.")
            return

        # Если это видео, извлекаем аудио из него
        original_file_path = file_path
        if is_video:
            try:
                await processing_msg.edit_text("Извлекаю аудиодорожку из видео...")
                file_path = await extract_audio_from_video(file_path)
                logger.info(f"Аудио успешно извлечено из видео: {file_path}")
                
                # Удаляем оригинальное видео после извлечения аудио (опционально, для экономии места)
                # Можно оставить, если нужно сохранить видео
                # try:
                #     os.remove(original_file_path)
                # except Exception as e:
                #     logger.warning(f"Не удалось удалить оригинальное видео: {e}")
            except Exception as e:
                await processing_msg.edit_text(f"Ошибка при извлечении аудио из видео: {str(e)}")
                logger.exception(f"Ошибка при извлечении аудио из видео: {e}")
                return

        # Предсказываем время обработки
        # Передаем информацию о типе файла (видео/аудио) для правильного определения
        estimated_time = predict_processing_time(file_path, WHISPER_MODEL, is_video=is_video)
        estimated_time_str = format_processing_time(estimated_time)

        # Уведомляем пользователя о постановке в очередь
        file_size_mb = file_size / (1024 * 1024)

        # Проверяем, нужно ли использовать модель меньшего размера
        should_switch, smaller_model = should_use_smaller_model(file_size_mb, WHISPER_MODEL)
        model_info = f"Модель: {WHISPER_MODEL}"
        if should_switch:
            model_info = f"Модель: {smaller_model} (автоматически выбрана для большого файла вместо {WHISPER_MODEL})"
            # Обновляем время с учетом фактически используемой модели
            estimated_time = predict_processing_time(file_path, smaller_model, is_video=is_video)
            estimated_time_str = format_processing_time(estimated_time)

        # Запускаем фоновый обработчик очереди, если он еще не запущен
        await ensure_background_processor_running()

        # Добавляем задачу в базу данных
        add_to_queue(user_id, file_path, file_name, file_size_mb, processing_msg.message_id, message.chat.id)

        # Получаем информацию о позиции в очереди
        user_queue = get_queue(user_id)
        position = len(user_queue)
        
        # Получаем общий размер очереди (число незавершенных и не отмененных задач)
        position_text = ""
        if position == 1:
            position_text = "🔥 Ваш файл первый в очереди."
        else:
            # Склонение слова "файл" в зависимости от позиции
            files_before = position - 1
            files_word = "файл"
            if files_before == 1:
                files_word = "файл"
            elif 2 <= files_before <= 4:
                files_word = "файла"
            else:
                files_word = "файлов"

            position_text = f"🕒 Номер вашего файла в очереди: {position}\nПеред вами {files_before} {files_word} ожидают обработки."

        file_type_label = "Видеофайл" if is_video else "Аудиофайл"
        await processing_msg.edit_text(
            f"{file_type_label} успешно загружен и поставлен в очередь на обработку.\n"
            f"Размер файла: {file_size_mb:.2f} МБ\n"
            f"{model_info}\n"
            f"Метод загрузки: {'Прямая загрузка через Local Bot API' if is_large_file else 'Стандартный API'}\n\n"
            f"{position_text}\n\n"
            f"⏱ Примерное время обработки: {estimated_time_str}\n\n"
            f"Обработка начнется автоматически. Вы получите уведомление, когда транскрибация будет готова.\n\n"
            f"Для отмены обработки используйте команду /cancel"
        )
        
        logger.info(f"{file_type_label} от пользователя {user_id} добавлен в очередь на обработку.")

    except TelegramBadRequest as e:
        if "file is too big" in str(e).lower():
            await processing_msg.edit_text(
                f"⚠️ Ошибка: Файл слишком большой для обработки в Telegram.\n\n"
                f"Текущее ограничение: 20 МБ (даже при использовании Local Bot API)\n\n"
                f"Рекомендации:\n"
                f"• Используйте файл меньшего размера (до 20 МБ)\n"
                f"• Сократите длительность {'видео' if is_video else 'аудио'}\n"
                f"• Разделите длинное {'видео' if is_video else 'аудио'} на несколько частей\n"
                f"• Конвертируйте файл в формат с бóльшим сжатием"
            )
            logger.error(f"Ошибка 'file is too big' при обработке аудио: {e}")
        else:
            await processing_msg.edit_text(f"Произошла ошибка при подготовке аудио к обработке: {str(e)}")
            logger.exception(f"Ошибка Telegram при обработке аудио: {e}")
    except Exception as e:
        await processing_msg.edit_text(f"Произошла ошибка при подготовке аудио к обработке: {str(e)}")
        logger.exception(f"Ошибка при обработке аудио: {e}")


def _run_transcribe_in_process(file_path, condition_on_previous_text, task_id, result_queue, error_queue):
    """
    Функция, которая выполняется в отдельном процессе для транскрибации аудио.
    Результат помещается в result_queue, ошибки - в error_queue.
    """
    try:
        result = _transcribe_audio_sync(file_path, condition_on_previous_text, USE_LOCAL_WHISPER, task_id)
        result_queue.put(result)
    except Exception as e:
        error_queue.put(e)
        import traceback
        logger.exception(f"Ошибка в процессе транскрибации для задачи {task_id}: {e}")
        logger.error(traceback.format_exc())


def _transcribe_audio_sync(file_path, condition_on_previous_text=False, use_local_whisper=USE_LOCAL_WHISPER, task_id=None):
    """
    Синхронная обертка для транскрибации аудио, которая может быть выполнена в отдельном процессе.
    Периодически проверяет, не отменена ли задача, и прерывает транскрибацию при отмене.
    """
    try:
        if use_local_whisper:
            # Проверяем отмену перед началом транскрибации
            if task_id is not None and is_task_cancelled(task_id):
                logger.info(f"Задача {task_id} была отменена перед транскрибацией")
                return None
            
            # Конвертируем в нужный формат для Whisper если нужно
            try:
                # Для синхронной версии нужно использовать синхронную конвертацию
                # Пока используем оригинальный файл, конвертация будет выполнена внутри transcribe_with_whisper
                converted_file = file_path
            except Exception as conv_error:
                logger.error(f"Ошибка при конвертации аудиофайла: {conv_error}")
                converted_file = file_path

            # Проверяем, существует ли файл и не пустой ли он
            if not os.path.exists(converted_file) or os.path.getsize(converted_file) == 0:
                logger.error(f"Файл не существует или пуст: {converted_file}")
                return None

            # Проверяем отмену перед запуском транскрибации
            if task_id is not None and is_task_cancelled(task_id):
                logger.info(f"Задача {task_id} была отменена перед запуском транскрибации")
                return None

            # Используем локальную модель Whisper (синхронно через asyncio.run)
            # В процессе можно использовать asyncio.run, так как у процесса свой event loop
            transcription = asyncio.run(transcribe_with_whisper(
                converted_file,
                model_name=WHISPER_MODEL,
                condition_on_previous_text=condition_on_previous_text
            ))

            # Проверяем отмену после транскрибации
            if task_id is not None and is_task_cancelled(task_id):
                logger.info(f"Задача {task_id} была отменена после транскрибации, игнорируем результат")
                return None

            return transcription
        else:
            # Используем OpenAI API
            client = OpenAI(api_key=env_config.get('OPEN_AI_TOKEN'),
                            max_retries=3,
                            timeout=30)

            # Проверяем, что файл существует и не пустой
            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                logger.error(f"Файл не существует или пуст: {file_path}")
                return None

            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
            
            # Проверяем результат транскрибации
            if transcription is None:
                logger.error("OpenAI API вернул None при транскрибации")
                return None
            
            # Проверяем наличие текста в результате
            if not hasattr(transcription, 'text') or transcription.text is None:
                logger.error("OpenAI API вернул транскрибацию без текста")
                return None
            
            text = transcription.text.strip()
            if not text:
                logger.warning("Транскрибация вернула пустую строку")
                return ""
            
            return text
    except Exception as e:
        logger.exception(f"Ошибка при транскрибации: {e}")
        raise


async def transcribe_audio(file_path, condition_on_previous_text = False, use_local_whisper=USE_LOCAL_WHISPER):
    """Транскрибация аудио с использованием OpenAI API или локальной модели Whisper"""
    try:
        if use_local_whisper:
            # Конвертируем в нужный формат для Whisper если нужно
            try:
                converted_file = await convert_audio_format(file_path)
            except Exception as conv_error:
                logger.error(f"Ошибка при конвертации аудиофайла: {conv_error}")
                # Пробуем использовать оригинальный файл если конвертация не удалась
                converted_file = file_path

            # Проверяем, существует ли файл и не пустой ли он
            if not os.path.exists(converted_file) or os.path.getsize(converted_file) == 0:
                logger.error(f"Файл не существует или пуст после конвертации: {converted_file}")
                raise FileNotFoundError(f"Файл не существует или пуст: {converted_file}")

            # Используем локальную модель Whisper
            transcription = await transcribe_with_whisper(
                converted_file,
                model_name=WHISPER_MODEL,
                condition_on_previous_text=condition_on_previous_text
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

            # Проверяем, что файл существует и не пустой
            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                logger.error(f"Файл не существует или пуст перед транскрибацией через OpenAI API: {file_path}")
                raise FileNotFoundError(f"Файл не существует или пуст: {file_path}")

            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
            
            # Проверяем результат транскрибации
            if transcription is None:
                logger.error("OpenAI API вернул None при транскрибации")
                raise ValueError("Транскрибация вернула пустой результат")
            
            # Проверяем наличие текста в результате
            if not hasattr(transcription, 'text') or transcription.text is None:
                logger.error("OpenAI API вернул транскрибацию без текста")
                raise ValueError("Транскрибация не содержит текста")
            
            text = transcription.text.strip()
            if not text:
                logger.warning("Транскрибация вернула пустую строку")
                return ""
            
            return text
    except Exception as e:
        logger.exception(f"Ошибка при транскрибации: {e}")
        raise


async def background_processor():
    """Фоновый обработчик очереди аудиофайлов из базы данных"""
    global background_worker_task
    
    # Используем блокировку для защиты от одновременного запуска нескольких обработчиков
    async with processor_lock:
        # Защита от параллельного запуска нескольких обработчиков
        if background_worker_task:
            logger.warning("Попытка запустить фоновый обработчик, когда он уже запущен")
            return
            
        # Важно: сначала сохраняем ссылку на текущую задачу, затем устанавливаем флаг
        background_worker_task = asyncio.current_task()
    
    logger.info("Запущен фоновый обработчик аудиофайлов")

    # Счетчик для периодической очистки файлов
    cleanup_counter = 0
    # Счетчик для отслеживания последовательных ошибок
    error_counter = 0
    # Максимальное количество последовательных ошибок перед небольшим ожиданием
    MAX_CONSECUTIVE_ERRORS = 5

    # Первым делом проверяем, есть ли активные задачи, которые были при перезапуске
    # Это нужно для того, чтобы возобновить обработку задач после перезагрузки сервера
    active_tasks = get_active_tasks()
    if active_tasks:
        logger.info(f"Обнаружено {len(active_tasks)} активных задач после перезапуска. Продолжаем их обработку.")
        
        # Сбрасываем флаг активности у всех активных задач, чтобы они были обработаны в правильном порядке
        reset_active_tasks()

    try:
        while True:
            try:
                # Инкрементируем счетчик очистки
                cleanup_counter += 1

                # Каждые 10 циклов выполняем очистку старых файлов
                if cleanup_counter >= 10:
                    cleanup_counter = 0
                    # Передаем список файлов, которые еще загружаются, чтобы не удалять их
                    exclude_files = list(files_being_uploaded.keys()) if files_being_uploaded else None
                    cleanup_temp_files(older_than_hours=24, exclude_files=exclude_files)

                # Найдем первую задачу в очереди, которая не активна, не завершена и не отменена
                # Для этого получим все очереди пользователей и найдем первую неактивную задачу
                active_task = None
                
                # Получим первую задачу из базы
                try:
                    active_task = get_first_from_queue()
                    # Если задача успешно получена, сбрасываем счетчик ошибок
                    error_counter = 0
                except Exception as db_error:
                    logger.error(f"Ошибка при получении задачи из базы данных: {db_error}")
                    error_counter += 1
                    
                    # Если слишком много последовательных ошибок, делаем небольшую паузу
                    if error_counter >= MAX_CONSECUTIVE_ERRORS:
                        logger.warning(f"Обнаружено {error_counter} последовательных ошибок. Делаем паузу перед следующей попыткой.")
                        await asyncio.sleep(10)  # Пауза на 10 секунд
                        error_counter = 0  # Сбрасываем счетчик после паузы
                    
                    await asyncio.sleep(1)
                    continue
                
                # Если нет задач, ждем 1 секунду и проверяем снова
                if not active_task:
                    await asyncio.sleep(1)
                    continue
                
                # Отмечаем задачу как активную
                set_active_queue(active_task.id)
                    
                # Получаем информацию о задаче
                user_id = active_task.user_id
                file_path = active_task.file_path
                file_name = active_task.file_name
                chat_id = active_task.chat_id
                message_id = active_task.message_id
                
                # Проверяем, является ли это файлом из папки downloads
                is_downloads_file = (user_id == DOWNLOADS_USER_ID and chat_id == 0 and message_id == 0)
                
                # Проверяем, существует ли файл
                if not os.path.exists(file_path):
                    logger.error(f"Файл {file_path} не существует для задачи {active_task.id}")
                    set_finished_queue(active_task.id)
                    if not is_downloads_file:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"❌ Ошибка: Файл для транскрибации не найден. Возможно, он был удален."
                        )
                    continue
                
                # Создаем объект-заглушку для сообщения, которое будем редактировать
                # В aiogram нет метода get_message, поэтому создаем заглушку с методом edit_text
                class MessageStub:
                    def __init__(self, bot, chat_id, message_id, is_downloads_file=False):
                        self.bot = bot
                        self.chat_id = chat_id
                        self.message_id = message_id
                        self.chat = type('obj', (object,), {'id': chat_id})()
                        self.is_downloads_file = is_downloads_file
                        # Для файлов из downloads храним словарь message_id для каждого superuser
                        self.superuser_messages = {} if is_downloads_file else None
                    
                    async def edit_text(self, text, **kwargs):
                        """Редактирует существующее сообщение, при неудаче создает новое"""
                        if self.is_downloads_file:
                            # Для файлов из downloads отправляем сообщения всем superusers
                            logger.info(f"[Downloads] {text}")
                            for superuser_id in superusers:
                                try:
                                    if superuser_id in self.superuser_messages:
                                        # Пытаемся отредактировать существующее сообщение
                                        try:
                                            await self.bot.edit_message_text(
                                                chat_id=superuser_id,
                                                message_id=self.superuser_messages[superuser_id],
                                                text=text,
                                                **kwargs
                                            )
                                        except Exception as e:
                                            logger.warning(f"Не удалось отредактировать сообщение {self.superuser_messages[superuser_id]} для superuser {superuser_id}: {e}")
                                            # Если редактирование не удалось, отправляем новое сообщение
                                            new_msg = await self.bot.send_message(
                                                chat_id=superuser_id,
                                                text=text,
                                                **kwargs
                                            )
                                            self.superuser_messages[superuser_id] = new_msg.message_id
                                    else:
                                        # Отправляем новое сообщение
                                        new_msg = await self.bot.send_message(
                                            chat_id=superuser_id,
                                            text=text,
                                            **kwargs
                                        )
                                        self.superuser_messages[superuser_id] = new_msg.message_id
                                except Exception as e:
                                    logger.error(f"Ошибка при отправке сообщения superuser {superuser_id}: {e}")
                            return
                        try:
                            await self.bot.edit_message_text(
                                chat_id=self.chat_id,
                                message_id=self.message_id,
                                text=text,
                                **kwargs
                            )
                        except Exception as e:
                            logger.warning(f"Не удалось отредактировать сообщение {self.message_id}: {e}")
                            # Если редактирование не удалось, отправляем новое сообщение
                            new_msg = await self.bot.send_message(
                                chat_id=self.chat_id,
                                text=text
                            )
                            # Обновляем message_id для последующих вызовов
                            self.message_id = new_msg.message_id

                # Создаем заглушку для сохраненного сообщения
                # При первом вызове edit_text она попытается отредактировать сообщение,
                # а если не получится - создаст новое
                processing_msg = MessageStub(bot, chat_id, message_id, is_downloads_file=is_downloads_file)

                # Сообщаем о начале транскрибации
                start_message = (
                    f"📥 Начинаю транскрибацию файла из папки downloads:\n"
                    f"📁 Файл: {file_name}\n\n"
                    f"Транскрибирую {'с помощью локального Whisper' if USE_LOCAL_WHISPER else 'через OpenAI API'}...\n\n"
                    f"Это может занять некоторое время в зависимости от длины аудио."
                )
                await processing_msg.edit_text(
                    start_message if is_downloads_file else
                    f"Транскрибирую аудио {'с помощью локального Whisper' if USE_LOCAL_WHISPER else 'через OpenAI API'}...\n\n"
                    f"Это может занять некоторое время в зависимости от длины аудио. Вы можете продолжать использовать бота.\n\n"
                    f"Чтобы отменить обработку, используйте команду /cancel"
                )

                # Проверяем размер файла для предупреждения о возможном переключении модели
                try:
                    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                    should_switch, smaller_model = should_use_smaller_model(file_size_mb, WHISPER_MODEL)

                    if should_switch:
                        switch_message = (
                            f"Транскрибирую аудио...\n\n"
                            f"⚠️ Обратите внимание: Файл имеет большой размер ({file_size_mb:.1f} МБ), "
                            f"поэтому вместо модели {WHISPER_MODEL} будет использована модель {smaller_model} для оптимизации памяти.\n\n"
                            f"Это может повлиять на качество транскрибации, но позволит обработать большой файл без ошибок."
                        )
                        if is_downloads_file:
                            logger.info(f"[Downloads] Файл имеет большой размер ({file_size_mb:.1f} МБ), будет использована модель {smaller_model}")
                            # Отправляем сообщение всем superusers
                            for superuser_id in superusers:
                                try:
                                    await bot.send_message(chat_id=superuser_id, text=switch_message)
                                except Exception as e:
                                    logger.error(f"Ошибка при отправке сообщения superuser {superuser_id}: {e}")
                        else:
                            await bot.send_message(chat_id=chat_id, text=switch_message)
                except Exception as e:
                    logger.exception(f"Ошибка при проверке размера файла: {e}")

                # Запускаем транскрибацию в отдельном потоке, чтобы не блокировать event loop
                loop = asyncio.get_event_loop()
                try:
                    # Проверяем отмену ПЕРЕД запуском транскрибации
                    with get_db_session() as session:
                        task_status = session.query(TranscribeQueue).filter(TranscribeQueue.id == active_task.id).first()
                        if task_status and task_status.cancelled:
                            logger.info(f"Задача {active_task.id} была отменена до запуска транскрибации")
                            cancel_message = f"❌ Обработка файла {file_name} была отменена." if is_downloads_file else "❌ Обработка была отменена."
                            await processing_msg.edit_text(cancel_message)
                            if is_downloads_file:
                                logger.info(f"[Downloads] Обработка файла {file_name} была отменена до запуска транскрибации")
                            # Удаляем временные файлы
                            try:
                                cleanup_temp_files(file_path)
                            except Exception as e:
                                logger.exception(f"Ошибка при удалении временных файлов после отмены: {e}")
                            continue
                    
                    # Перед созданием future, убедимся, что файл существует
                    if not os.path.exists(file_path):
                        logger.error(f"Файл не существует перед запуском транскрибации: {file_path}")
                        error_msg = (
                            f"❌ Ошибка: Файл для транскрибации не найден.\n"
                            f"📁 Файл: {file_name}"
                        ) if is_downloads_file else f"❌ Ошибка: Файл для транскрибации не найден."
                        await processing_msg.edit_text(error_msg)
                        set_finished_queue(active_task.id)
                        continue
                        
                    # Создаем процесс для транскрибации, чтобы можно было убить его при отмене
                    result_queue = multiprocessing.Queue()
                    error_queue = multiprocessing.Queue()
                    
                    # Создаем процесс для транскрибации
                    transcribe_process = multiprocessing.Process(
                        target=_run_transcribe_in_process,
                        args=(file_path, should_condition_on_previous_text(file_size_mb), active_task.id, result_queue, error_queue),
                        daemon=True
                    )
                    transcribe_process.start()
                    
                    # Сохраняем ссылку на процесс для возможности убить его при отмене
                    async with processes_lock:
                        active_transcription_processes[active_task.id] = {
                            'process': transcribe_process,
                            'pid': transcribe_process.pid,
                            'result_queue': result_queue,
                            'error_queue': error_queue
                        }
                    
                    logger.info(f"Запущен процесс транскрибации для задачи {active_task.id}, PID: {transcribe_process.pid}")
                    
                    # Создаем future-подобный объект для совместимости с существующим кодом
                    class ProcessFuture:
                        def __init__(self, process, result_queue, error_queue, task_id):
                            self.process = process
                            self.result_queue = result_queue
                            self.error_queue = error_queue
                            self.task_id = task_id
                            self._result = None
                            self._done = False
                            self._exception = None
                        
                        def done(self):
                            return self._done or not self.process.is_alive()
                        
                        def cancel(self):
                            """Пытается убить процесс (синхронный метод)"""
                            if self.process.is_alive():
                                logger.info(f"Попытка убить процесс {self.process.pid} для задачи {self.task_id}")
                                try:
                                    self.process.terminate()
                                    self.process.join(timeout=5)
                                    if self.process.is_alive():
                                        logger.warning(f"Процесс {self.process.pid} не завершился после terminate, убиваем принудительно")
                                        self.process.kill()
                                        self.process.join(timeout=2)
                                    logger.info(f"Процесс {self.process.pid} для задачи {self.task_id} успешно убит")
                                except Exception as e:
                                    logger.exception(f"Ошибка при попытке убить процесс {self.process.pid}: {e}")
                            
                            self._done = True
                            # Удаляем процесс из словаря активных процессов (синхронный доступ безопасен)
                            try:
                                active_transcription_processes.pop(self.task_id, None)
                            except Exception as e:
                                logger.warning(f"Ошибка при удалении процесса из словаря: {e}")
                            return True
                        
                        async def get_result(self):
                            """Получает результат из очереди (асинхронный метод)"""
                            try:
                                while self.process.is_alive():
                                    try:
                                        # Проверяем очередь результатов
                                        if not self.result_queue.empty():
                                            result = self.result_queue.get_nowait()
                                            self._result = result
                                            self._done = True
                                            return result
                                        
                                        # Проверяем очередь ошибок
                                        if not self.error_queue.empty():
                                            error = self.error_queue.get_nowait()
                                            self._exception = error
                                            self._done = True
                                            raise error
                                        
                                        # Небольшая пауза перед следующей проверкой
                                        await asyncio.sleep(0.5)
                                    except (EOFError, OSError):
                                        # Очередь закрыта или недоступна, это нормально
                                        break
                                
                                # Процесс завершился, проверяем очереди еще раз
                                try:
                                    if not self.result_queue.empty():
                                        result = self.result_queue.get_nowait()
                                        self._result = result
                                        self._done = True
                                        return result
                                    
                                    if not self.error_queue.empty():
                                        error = self.error_queue.get_nowait()
                                        self._exception = error
                                        self._done = True
                                        raise error
                                except (EOFError, OSError):
                                    # Очередь закрыта или недоступна, это нормально после завершения процесса
                                    pass
                                
                                # Процесс завершился, но результат не получен
                                self._done = True
                                if self.process.exitcode != 0 and self.process.exitcode is not None:
                                    raise RuntimeError(f"Процесс транскрибации завершился с кодом {self.process.exitcode}")
                                return None
                            finally:
                                # Удаляем процесс из словаря активных процессов после получения результата или ошибки
                                try:
                                    active_transcription_processes.pop(self.task_id, None)
                                except Exception as e:
                                    logger.warning(f"Ошибка при удалении процесса из словаря: {e}")
                    
                    future = ProcessFuture(transcribe_process, result_queue, error_queue, active_task.id)

                    # Ожидаем результат с периодическим обновлением статуса
                    start_time = datetime.now()
                    cancelled = False
                    
                    while not future.done():
                        # Проверяем, не отменена ли задача
                        with get_db_session() as session:
                            task_status = session.query(TranscribeQueue).filter(TranscribeQueue.id == active_task.id).first()
                            if task_status and task_status.cancelled:
                                cancelled = True
                                # Убиваем процесс транскрибации
                                future.cancel()
                                logger.info(f"Транскрибация для пользователя {user_id} была отменена во время обработки, процесс убит")

                                # Удаляем временные файлы
                                try:
                                    cleanup_temp_files(file_path)
                                except Exception as e:
                                    logger.exception(f"Ошибка при удалении временных файлов после отмены: {e}")

                                # Сообщаем пользователю об отмене
                                cancel_message = f"❌ Обработка файла {file_name} была отменена." if is_downloads_file else "❌ Обработка была отменена."
                                await processing_msg.edit_text(cancel_message)
                                if is_downloads_file:
                                    logger.info(f"[Downloads] Обработка файла {file_name} была отменена, процесс убит")
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

                            # Определяем тип файла для передачи в predict_processing_time
                            # Используем оригинальное имя файла из базы данных, чтобы правильно определить тип
                            # даже если файл был извлечен из видео (имеет расширение .wav)
                            is_video_file = False
                            if file_name:
                                file_name_lower = file_name.lower()
                                # Проверяем расширения видео
                                video_extensions = ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.3gp', '.ogv']
                                is_video_file = any(file_name_lower.endswith(ext) for ext in video_extensions)
                                # Проверяем специальные названия
                                is_video_file = is_video_file or "Видеосообщение" in file_name or "видео" in file_name_lower
                            
                            # Получаем предполагаемое оставшееся время
                            estimated_total = predict_processing_time(file_path, current_model, is_video=is_video_file)
                            elapsed_td = timedelta(seconds=int(elapsed))
                            remaining = estimated_total - elapsed_td if estimated_total > elapsed_td else timedelta(seconds=10)

                            # Расчет примерного процента завершения
                            if estimated_total.total_seconds() > 0:
                                percent_complete = min(95, int((elapsed / estimated_total.total_seconds()) * 100))
                                progress_bar = "█" * (percent_complete // 5) + "░" * ((100 - percent_complete) // 5)
                            else:
                                percent_complete = 0
                                progress_bar = "░" * 20

                            # Определяем тип файла для отображения (используем уже определенную переменную is_video_file)
                            file_type_label = "видео" if is_video_file else "аудио"
                            
                            status_message = (
                                f"📥 Транскрибирую {file_type_label} из downloads:\n"
                                f"📁 Файл: {file_name}\n\n"
                                f"{'С помощью локального Whisper' if USE_LOCAL_WHISPER else 'Через OpenAI API'}...\n\n"
                                f"⏱ Прошло времени: {time_str}\n"
                                f"⌛ Осталось примерно: {str(remaining)}\n"
                                f"📊 Прогресс: {progress_bar} {percent_complete}%\n"
                                f"🎯 Модель: {current_model}"
                                f"Вы можете продолжать использовать бота для других задач.\n\n"
                                f"Для отмены обработки используйте команду /cancel"
                            ) if is_downloads_file else (
                                f"Транскрибирую {file_type_label} {'с помощью локального Whisper' if USE_LOCAL_WHISPER else 'через OpenAI API'}...\n\n"
                                f"⏱ Прошло времени: {time_str}\n"
                                f"⌛ Осталось примерно: {str(remaining)}\n"
                                f"📊 Прогресс: {progress_bar} {percent_complete}%\n"
                                f"📁 Файл: {file_name}\n"
                                f"🎯 Модель: {current_model}\n\n"
                                f"Вы можете продолжать использовать бота для других задач.\n\n"
                                f"Для отмены обработки используйте команду /cancel"
                            )
                            await processing_msg.edit_text(status_message)
                            if is_downloads_file:
                                logger.info(f"[Downloads] Транскрибация {file_name}: {percent_complete}% ({time_str} прошло, {str(remaining)} осталось)")

                        # Небольшая пауза, чтобы не нагружать процессор
                        await asyncio.sleep(1)

                    # Если задача была отменена, пропускаем дальнейшую обработку
                    if cancelled:
                        # Процесс уже убит в цикле выше
                        # Помечаем задачу как отмененную в базе данных
                        set_cancelled_queue(active_task.id)
                        # Очищаем ссылку на процесс
                        async with processes_lock:
                            active_transcription_processes.pop(active_task.id, None)
                        continue

                    # Дополнительная проверка отмены перед ожиданием результата
                    with get_db_session() as session:
                        task_status = session.query(TranscribeQueue).filter(TranscribeQueue.id == active_task.id).first()
                        if task_status and task_status.cancelled:
                            logger.info(f"Задача {active_task.id} была отменена перед получением результата")
                            if not future.done():
                                future.cancel()
                            cancel_message = f"❌ Обработка файла {file_name} была отменена." if is_downloads_file else "❌ Обработка была отменена."
                            await processing_msg.edit_text(cancel_message)
                            if is_downloads_file:
                                logger.info(f"[Downloads] Обработка файла {file_name} была отменена перед получением результата")
                            set_cancelled_queue(active_task.id)
                            continue

                    # Получаем результат только если задача не была отменена
                    transcription = None
                    try:
                        # Проверяем отмену перед ожиданием результата
                        with get_db_session() as session:
                            task_status = session.query(TranscribeQueue).filter(TranscribeQueue.id == active_task.id).first()
                            if task_status and task_status.cancelled:
                                logger.info(f"Задача {active_task.id} была отменена перед получением результата, убиваем процесс")
                                future.cancel()
                                cancel_message = f"❌ Обработка файла {file_name} была отменена." if is_downloads_file else "❌ Обработка была отменена."
                                await processing_msg.edit_text(cancel_message)
                                if is_downloads_file:
                                    logger.info(f"[Downloads] Обработка файла {file_name} была отменена, процесс убит")
                                set_cancelled_queue(active_task.id)
                                continue
                        
                        # Если задача не отменена, ждем результат
                        transcription = await future.get_result()
                    except asyncio.CancelledError:
                        logger.info(f"Транскрибация для пользователя {user_id} отменена")
                        cancel_message = f"❌ Обработка файла {file_name} была отменена." if is_downloads_file else "❌ Обработка была отменена."
                        await processing_msg.edit_text(cancel_message)
                        if is_downloads_file:
                            logger.info(f"[Downloads] Обработка файла {file_name} была отменена")
                        set_cancelled_queue(active_task.id)
                        continue
                    except Exception as transcribe_error:
                        logger.exception(f"Ошибка при получении результата транскрибации: {transcribe_error}")
                        error_message = (
                            f"❌ Произошла ошибка при транскрибации файла {file_name}:\n{str(transcribe_error)}"
                            if is_downloads_file else
                            f"❌ Произошла ошибка при транскрибации: {str(transcribe_error)}"
                        )
                        await processing_msg.edit_text(error_message)
                        if is_downloads_file:
                            logger.error(f"[Downloads] Ошибка при транскрибации файла {file_name}: {transcribe_error}")
                        set_finished_queue(active_task.id)
                        continue

                except Exception as e:
                    logger.exception(f"Ошибка при асинхронной транскрибации: {e}")
                    error_message = (
                        f"❌ Произошла ошибка при транскрибации файла {file_name}:\n{str(e)}"
                        if is_downloads_file else
                        f"❌ Произошла ошибка при транскрибации: {str(e)}"
                    )
                    await processing_msg.edit_text(error_message)
                    if is_downloads_file:
                        logger.error(f"[Downloads] Ошибка при транскрибации файла {file_name}: {e}")
                    set_finished_queue(active_task.id)
                    continue

                # Определяем тип файла для сообщений об ошибках
                is_video_file = file_name and any(ext in file_name.lower() for ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv'])
                file_type_label = "видео" if is_video_file or "Видеосообщение" in file_name else "аудио"
                
                # Проверяем, получили ли мы результат
                if transcription is None:
                    # Если транскрибация не удалась, сообщаем об ошибке
                    error_msg = (
                        f"❌ Ошибка при транскрибации {file_type_label} из downloads:\n"
                        f"📁 Файл: {file_name}\n\n"
                        f"Не удалось обработать {file_type_label}файл. Возможные причины:\n"
                        f"• Файл повреждён или имеет неподдерживаемый формат\n"
                        f"• {file_type_label.capitalize()} не содержит речи или имеет слишком низкое качество\n"
                        f"• Ошибка при обработке модели Whisper"
                    ) if is_downloads_file else (
                        f"❌ Ошибка при транскрибации {file_type_label}: {file_name}\n\n"
                        f"Не удалось обработать {file_type_label}файл. Возможные причины:\n"
                        f"• Файл повреждён или имеет неподдерживаемый формат\n"
                        f"• {file_type_label.capitalize()} не содержит речи или имеет слишком низкое качество\n"
                        f"• Ошибка при обработке модели Whisper\n\n"
                        f"Пожалуйста, попробуйте отправить другой {file_type_label}файл или обратитесь к администратору."
                    )
                    await processing_msg.edit_text(error_msg)
                    if is_downloads_file:
                        logger.error(f"[Downloads] {error_msg}")

                    # Удаляем временные файлы
                    try:
                        cleanup_temp_files(file_path)
                    except Exception as e:
                        logger.exception(f"Ошибка при удалении временных файлов: {e}")

                    # Отмечаем задачу как выполненную
                    set_finished_queue(active_task.id)
                    continue

                # Сохраняем транскрибацию в файл
                # Получаем данные пользователя для транскрибации
                username = "downloads" if is_downloads_file else "unknown"
                first_name = "Downloads" if is_downloads_file else "Unknown"
                last_name = ""
                
                # Пытаемся получить данные пользователя из БД или другим способом (только для файлов не из downloads)
                if not is_downloads_file:
                    try:
                        user = await bot.get_chat_member(chat_id, user_id)
                        if user and user.user:
                            username = user.user.username or "unknown"
                            first_name = user.user.first_name or "Unknown"
                            last_name = user.user.last_name or ""
                    except Exception as e:
                        logger.warning(f"Не удалось получить данные пользователя: {e}")

                transcript_file_path = save_transcription_to_file(
                    transcription,
                    user_id,
                    file_name,
                    username,
                    first_name,
                    last_name
                )

                # Определяем тип файла
                is_video_file = file_name and any(ext in file_name.lower() for ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv'])
                file_type_label = "видео" if is_video_file or "Видеосообщение" in file_name else "аудио"
                emoji = "🎥" if file_type_label == "видео" else "🎤"
                
                # Формируем текстовое сообщение
                message_text = f"{emoji} Транскрибация {file_type_label}: {file_name}\n\n"

                # Определяем, какая модель использовалась
                used_model = WHISPER_MODEL

                # Пытаемся получить информацию о фактически использованной модели из результата
                if isinstance(transcription, dict) and "whisper_model" in transcription:
                    used_model = transcription.get("whisper_model", WHISPER_MODEL)

                    # Если использованная модель отличается от заданной, добавляем информацию
                    if used_model != WHISPER_MODEL:
                        processing_time = transcription.get("processing_time", 0)
                        processing_time_str = f" (время обработки: {format_processing_time(processing_time)})" if processing_time > 0 else ""
                        message_text += f"ℹ️ Использована модель {used_model} вместо {WHISPER_MODEL} для оптимизации памяти{processing_time_str}.\n\n"

                # Получаем текст транскрибации
                transcription_text = ""
                # Если результат в формате словаря, извлекаем текст
                if isinstance(transcription, dict):
                    transcription_text = transcription.get('text', '') or ''
                elif isinstance(transcription, str):
                    transcription_text = transcription or ''
                else:
                    # Если это объект с атрибутом text
                    transcription_text = getattr(transcription, 'text', '') if transcription else ''
                
                # Убеждаемся, что transcription_text - это строка и не None
                if transcription_text is None:
                    transcription_text = ''
                else:
                    transcription_text = str(transcription_text).strip()

                # Проверяем, не пустой ли текст транскрибации
                if not transcription_text:
                    warning_msg = (
                        f"⚠️ Предупреждение: Транскрибация {file_type_label} из downloads не содержит текста.\n"
                        f"📁 Файл: {file_name}\n\n"
                        f"Возможно, {file_type_label} не содержит распознаваемой речи или имеет слишком низкое качество."
                    ) if is_downloads_file else (
                        f"⚠️ Предупреждение: Транскрибация {file_type_label} не содержит текста.\n\n"
                        f"Возможно, {file_type_label} не содержит распознаваемой речи или имеет слишком низкое качество."
                    )
                    await processing_msg.edit_text(warning_msg)
                    if is_downloads_file:
                        logger.warning(f"[Downloads] {warning_msg}")

                    # Удаляем временные файлы
                    try:
                        cleanup_temp_files(file_path)
                    except Exception as e:
                        logger.exception(f"Ошибка при удалении временных файлов: {e}")

                    # Отмечаем задачу как выполненную
                    set_finished_queue(active_task.id)
                    continue

                # Отправляем результаты транскрибации
                if is_downloads_file:
                    # Для файлов из downloads отправляем результаты всем superusers
                    logger.info(f"[Downloads] Транскрибация файла {file_name} завершена успешно")
                    logger.info(f"[Downloads] Транскрибация сохранена в: {transcript_file_path}")
                    
                    # Обновляем финальное сообщение о завершении
                    final_message = (
                        f"✅ Транскрибация завершена!\n\n"
                        f"📥 Файл из downloads:\n"
                        f"📁 {file_name}\n\n"
                        f"{message_text}"
                    )
                    await processing_msg.edit_text(final_message)
                    
                    # Отправляем результаты всем superusers
                    for superuser_id in superusers:
                        try:
                            # Создаем объект сообщения для отправки файлов
                            class SuperuserMessageStub:
                                def __init__(self, chat_id):
                                    self.chat = type('obj', (object,), {'id': chat_id})
                                    
                                async def answer(self, text):
                                    return await bot.send_message(chat_id=self.chat.id, text=text)
                                    
                                async def answer_document(self, document, caption=None):
                                    return await bot.send_document(chat_id=self.chat.id, document=document, caption=caption)

                            message_stub = SuperuserMessageStub(superuser_id)
                            
                            # Если текст слишком длинный, разбиваем на части
                            if len(transcription_text) > MAX_MESSAGE_LENGTH - len(message_text):
                                # Отправляем превью транскрибации
                                preview_length = MAX_MESSAGE_LENGTH - len(message_text) - 50  # Оставляем запас
                                preview_text = transcription_text[:preview_length] + "...\n\n(полный текст в файле)"
                                await bot.send_message(chat_id=superuser_id, text=message_text + preview_text)

                                # Отправляем файл с полной транскрибацией безопасным способом
                                caption_text = f"Полная транскрибация {file_type_label} из downloads"
                                await send_file_safely(
                                    message_stub,
                                    transcript_file_path,
                                    caption=caption_text
                                )

                                # Проверяем наличие SRT-файла и отправляем его
                                srt_file_path = transcript_file_path.replace('.txt', '.srt')
                                if os.path.exists(srt_file_path):
                                    await send_file_safely(
                                        message_stub,
                                        srt_file_path,
                                        caption="Файл субтитров (SRT) для видеоредакторов"
                                    )
                            else:
                                # Для коротких транскрибаций просто отправляем весь текст
                                await bot.send_message(chat_id=superuser_id, text=message_text + transcription_text)

                                # Отправляем файл для удобства
                                await send_file_safely(
                                    message_stub,
                                    transcript_file_path,
                                    caption="Транскрибация аудио в виде файла"
                                )

                                # Проверяем наличие SRT-файла и отправляем его
                                srt_file_path = transcript_file_path.replace('.txt', '.srt')
                                if os.path.exists(srt_file_path):
                                    await send_file_safely(
                                        message_stub,
                                        srt_file_path,
                                        caption="Файл субтитров (SRT) для видеоредакторов"
                                    )
                        except Exception as e:
                            logger.error(f"Ошибка при отправке результатов superuser {superuser_id}: {e}")
                    
                    srt_file_path = transcript_file_path.replace('.txt', '.srt')
                    if os.path.exists(srt_file_path):
                        logger.info(f"[Downloads] Файл субтитров сохранен в: {srt_file_path}")
                else:
                    # Создаем объект сообщения для отправки файлов
                    class MessageStub:
                        def __init__(self, chat_id):
                            self.chat = type('obj', (object,), {'id': chat_id})
                            
                        async def answer(self, text):
                            return await bot.send_message(chat_id=self.chat.id, text=text)
                            
                        async def answer_document(self, document, caption=None):
                            return await bot.send_document(chat_id=self.chat.id, document=document, caption=caption)

                    message_stub = MessageStub(chat_id)

                    # Если текст слишком длинный, разбиваем на части
                    if len(transcription_text) > MAX_MESSAGE_LENGTH - len(message_text):
                        # Отправляем превью транскрибации
                        preview_length = MAX_MESSAGE_LENGTH - len(message_text) - 50  # Оставляем запас
                        preview_text = transcription_text[:preview_length] + "...\n\n(полный текст в файле)"
                        await processing_msg.edit_text(message_text + preview_text)

                        # Отправляем файл с полной транскрибацией безопасным способом
                        caption_text = f"Полная транскрибация {file_type_label}"
                        await send_file_safely(
                            message_stub,
                            transcript_file_path,
                            caption=caption_text
                        )

                        # Проверяем наличие SRT-файла и отправляем его
                        srt_file_path = transcript_file_path.replace('.txt', '.srt')
                        if os.path.exists(srt_file_path):
                            await send_file_safely(
                                message_stub,
                                srt_file_path,
                                caption="Файл субтитров (SRT) для видеоредакторов"
                            )
                    else:
                        # Для коротких транскрибаций просто отправляем весь текст
                        await processing_msg.edit_text(message_text + transcription_text)

                        # Отправляем файл для удобства
                        await send_file_safely(
                            message_stub,
                            transcript_file_path,
                            caption="Транскрибация аудио в виде файла"
                        )

                        # Проверяем наличие SRT-файла и отправляем его
                        srt_file_path = transcript_file_path.replace('.txt', '.srt')
                        if os.path.exists(srt_file_path):
                            await send_file_safely(
                                message_stub,
                                srt_file_path,
                                caption="Файл субтитров (SRT) для видеоредакторов"
                            )

                # Удаляем временные файлы
                try:
                    cleanup_temp_files(file_path)
                except Exception as e:
                    logger.exception(f"Ошибка при удалении временных файлов: {e}")

                # Отмечаем задачу как выполненную
                set_finished_queue(active_task.id)
                
                # Удаляем процесс из словаря активных процессов после завершения транскрибации
                async with processes_lock:
                    active_transcription_processes.pop(active_task.id, None)

            except asyncio.TimeoutError:
                # Проверка пустой очереди - нормальная ситуация
                continue
            except asyncio.CancelledError:
                # Обработчик был остановлен
                logger.info("Фоновый обработчик аудиофайлов остановлен по запросу отмены")
                break
            except Exception as e:
                logger.exception(f"Неожиданная ошибка в обработчике очереди: {e}")
                # Добавляем дополнительный лог для мониторинга более серьезных проблем
                logger.error(f"Обработчик продолжит работу несмотря на ошибку: {str(e)}")
                # Увеличиваем счетчик ошибок
                error_counter += 1
                
                # Если много последовательных ошибок, делаем более длинную паузу
                if error_counter >= MAX_CONSECUTIVE_ERRORS:
                    logger.warning(f"Слишком много ошибок подряд ({error_counter}). Делаем паузу для стабилизации.")
                    await asyncio.sleep(30)  # Пауза на 30 секунд после серии ошибок
                    error_counter = 0
                else:
                    # Небольшая пауза после ошибки
                    await asyncio.sleep(1)
            
            # Периодически логируем состояние обработчика для мониторинга
            if cleanup_counter % 50 == 0:
                logger.info(f"Фоновый обработчик продолжает работать. Счетчик очистки: {cleanup_counter}")
                
    except Exception as e:
        # Логируем любые непредвиденные ошибки вне внутреннего try-except блока
        logger.exception(f"Критическая ошибка в фоновом обработчике: {e}")
        raise  # Пробрасываем ошибку, чтобы она была видна в .done() проверке
    finally:
        async with processor_lock:
            logger.info("Фоновый обработчик аудиофайлов завершен")

def _kill_transcription_process(task_id: int):
    """Убивает процесс транскрибации для задачи с указанным ID (синхронная функция)"""
    try:
        # Получаем информацию о процессе из словаря
        # Словарь Python потокобезопасен для чтения, но не для записи
        # В нашем случае мы только читаем и удаляем элементы, что должно быть безопасно
        process_info = active_transcription_processes.get(task_id)
        if process_info:
            process = process_info.get('process')
            pid = process_info.get('pid')
            if process and process.is_alive():
                logger.info(f"Убиваем процесс {pid} для задачи {task_id}")
                try:
                    process.terminate()
                    process.join(timeout=5)
                    if process.is_alive():
                        logger.warning(f"Процесс {pid} не завершился после terminate, убиваем принудительно")
                        process.kill()
                        process.join(timeout=2)
                    logger.info(f"Процесс {pid} для задачи {task_id} успешно убит")
                except Exception as e:
                    logger.exception(f"Ошибка при попытке убить процесс {pid} для задачи {task_id}: {e}")
                finally:
                    # Удаляем процесс из словаря (это может быть небезопасно, но в нашем случае это редко происходит)
                    try:
                        active_transcription_processes.pop(task_id, None)
                    except Exception as e:
                        logger.warning(f"Ошибка при удалении процесса из словаря: {e}")
        else:
            logger.debug(f"Процесс для задачи {task_id} не найден в словаре активных процессов")
    except Exception as e:
        logger.exception(f"Ошибка при попытке убить процесс для задачи {task_id}: {e}")


async def cancel_audio_processing(user_id: int) -> tuple[bool, str]:
    """Отмена обработки аудио для пользователя
    
    Args:
        user_id: ID пользователя
        
    Returns:
        Кортеж (успех операции, сообщение для пользователя)
    """
    logger.info(f"Попытка отмены обработки аудио для пользователя {user_id}")
    
    # Получаем все активные задачи пользователя
    user_queue = get_queue(user_id)
    
    cancelled_count = 0
    
    # Отменяем все активные задачи пользователя
    if user_queue:
        for task in user_queue:
            if set_cancelled_queue(task.id):
                cancelled_count += 1
                logger.info(f"Задача {task.id} для пользователя {user_id} успешно отменена")
                # Убиваем процесс транскрибации для этой задачи
                _kill_transcription_process(task.id)
            else:
                logger.warning(f"Не удалось отменить задачу {task.id} для пользователя {user_id}")
    
    # Если пользователь является superuser, также отменяем задачи из downloads
    if user_id in superusers:
        downloads_queue = get_queue(DOWNLOADS_USER_ID)
        if downloads_queue:
            downloads_cancelled = 0
            for task in downloads_queue:
                if set_cancelled_queue(task.id):
                    downloads_cancelled += 1
                    cancelled_count += 1
                    logger.info(f"Задача {task.id} из downloads для superuser {user_id} успешно отменена")
                    # Убиваем процесс транскрибации для этой задачи
                    _kill_transcription_process(task.id)
                else:
                    logger.warning(f"Не удалось отменить задачу {task.id} из downloads для superuser {user_id}")
            
            if downloads_cancelled > 0:
                logger.info(f"Отменено {downloads_cancelled} задач из downloads для superuser {user_id}")
    
    if cancelled_count > 0:
        # Формируем текст в зависимости от количества отмененных задач
        task_text = "задача" if cancelled_count == 1 else "задачи"
        if cancelled_count >= 5:
            task_text = "задач"
        
        return True, f"✅ {cancelled_count} {task_text} на транскрибацию отменено."
    else:
        if user_id in superusers:
            return False, "Не найдено активных задач для отмены (ни ваших, ни из downloads)."
        else:
            return False, "У вас нет активных задач на транскрибацию."

async def ensure_background_processor_running():
    """Гарантирует, что фоновый процессор аудио запущен и работает корректно.
    Проверяет текущее состояние и при необходимости перезапускает процессор."""
    global background_worker_task
    
    #logger.debug(f"Проверка фонового процессора: task={background_worker_task}")
    
    # Флаг, указывающий на необходимость перезапуска
    need_restart = False
    
    # Проверяем состояние задачи, если она существует
    if background_worker_task:
        if background_worker_task.done():
            try:
                if not background_worker_task.cancelled():
                    background_worker_task.result()  # Проверяем на исключения
                    logger.info("Фоновый процессор завершился без ошибок, требуется перезапуск")
                else:
                    logger.info("Фоновый процессор был отменен, требуется перезапуск")
                need_restart = True
            except Exception as e:
                logger.error(f"Фоновый процессор завершился с ошибкой: {str(e)}, требуется перезапуск")
                need_restart = True
        elif background_worker_task.cancelled():
            logger.info("Фоновый процессор отменен, требуется перезапуск")
            need_restart = True
    else:
        # Если задачи нет, необходим запуск
        logger.info("Фоновый процессор не запущен, требуется запуск")
        need_restart = True
    
    # Если требуется перезапуск, сначала отменяем текущую задачу
    if need_restart:
        logger.info("Перезапуск фонового процессора аудио...")
        
        # Отменяем текущую задачу, если она существует и еще не завершена
        if background_worker_task and not background_worker_task.done():
            try:
                background_worker_task.cancel()
                try:
                    # Даем время на корректное завершение
                    await asyncio.wait_for(asyncio.shield(background_worker_task), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    logger.debug("Задача успешно отменена или тайм-аут ожидания")
            except Exception as e:
                logger.error(f"Ошибка при отмене предыдущей задачи: {str(e)}")
        
        # Сбрасываем состояние
        background_worker_task = None
        
        # Запускаем новую фоновую задачу
        background_worker_task = asyncio.create_task(background_processor())
        logger.info("Новая задача фонового процессора успешно запущена")
    else:
        #logger.debug("Фоновый процессор работает корректно, перезапуск не требуется")
        pass
    
    return background_worker_task

# Периодическая проверка состояния обработчика
async def monitor_background_processor():
    """
    Периодически проверяет состояние фонового обработчика и перезапускает его при необходимости
    """
    while True:
        try:
            # Проверяем и перезапускаем обработчик, если необходимо
            await ensure_background_processor_running()
        except Exception as e:
            logger.exception(f"Ошибка в мониторинге фонового обработчика: {e}")
        
        # Проверяем каждые 5 минут
        await asyncio.sleep(300)

# Функция фактического запуска мониторинга, которая должна вызываться
# после создания и запуска цикла событий asyncio
def init_monitoring():
    """
    Инициализирует мониторинг фонового обработчика. 
    Должна вызываться после запуска event loop.
    """
    # Добавляем задержку перед запуском мониторинга, чтобы дать время фоновому обработчику запуститься
    async def delayed_start():
        await asyncio.sleep(10)  # Задержка 10 секунд
        await asyncio.create_task(monitor_background_processor())
        logger.info("Запущен мониторинг фонового обработчика")
        
    asyncio.create_task(delayed_start())
    logger.info("Мониторинг фонового обработчика будет запущен через 10 секунд")

# Специальный user_id для файлов из папки downloads
DOWNLOADS_USER_ID = 0

# Множество для отслеживания уже обработанных файлов из downloads
processed_downloads_files = set()

# Словарь для отслеживания файлов, которые еще загружаются (путь -> размер)
files_being_uploaded = {}

async def is_file_fully_uploaded(file_path: str, check_interval: float = 2.0, stability_checks: int = 3) -> bool:
    """
    Проверяет, что файл полностью загружен, проверяя стабильность его размера
    
    Args:
        file_path: Путь к файлу
        check_interval: Интервал между проверками размера (в секундах)
        stability_checks: Количество проверок, при которых размер должен оставаться неизменным
    
    Returns:
        True если файл полностью загружен, False если еще загружается
    """
    try:
        if not os.path.exists(file_path):
            return False
        
        # Получаем начальный размер
        initial_size = os.path.getsize(file_path)
        
        # Если файл пустой, считаем что он еще не начал загружаться
        if initial_size == 0:
            return False
        
        # Проверяем, что файл доступен для чтения (не заблокирован)
        try:
            with open(file_path, 'rb') as f:
                f.read(1)  # Пытаемся прочитать хотя бы один байт
        except (IOError, OSError, PermissionError) as e:
            logger.debug(f"Файл {file_path} заблокирован для чтения: {e}")
            return False
        
        # Проверяем стабильность размера несколько раз
        for i in range(stability_checks):
            await asyncio.sleep(check_interval)
            
            if not os.path.exists(file_path):
                return False
            
            current_size = os.path.getsize(file_path)
            
            # Если размер изменился, файл еще загружается
            if current_size != initial_size:
                logger.debug(f"Файл {os.path.basename(file_path)} еще загружается: размер изменился с {initial_size} на {current_size} байт")
                return False
        
        # Если размер оставался неизменным во всех проверках, файл загружен
        logger.debug(f"Файл {os.path.basename(file_path)} полностью загружен, размер: {initial_size} байт")
        return True
        
    except Exception as e:
        logger.exception(f"Ошибка при проверке загрузки файла {file_path}: {e}")
        return False

async def monitor_downloads_folder():
    """
    Мониторит папку downloads и автоматически добавляет новые файлы в очередь транскрибации
    """
    logger.info(f"Запущен мониторинг папки downloads: {DOWNLOADS_DIR}")
    
    while True:
        try:
            # Проверяем папку downloads на наличие новых файлов
            if not os.path.exists(DOWNLOADS_DIR):
                os.makedirs(DOWNLOADS_DIR, exist_ok=True)
                await asyncio.sleep(30)  # Проверяем каждые 30 секунд
                continue
            
            # Получаем список файлов в папке downloads
            files = [f for f in os.listdir(DOWNLOADS_DIR) if os.path.isfile(os.path.join(DOWNLOADS_DIR, f))]
            
            # Расширения для видео
            video_extensions = ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v', '.3gp', '.ogv']
            # Расширения для аудио
            audio_extensions = ['.mp3', '.wav', '.ogg', '.m4a', '.flac', '.aac', '.wma', '.opus', '.amr', '.amr']
            
            for filename in files:
                file_path = os.path.join(DOWNLOADS_DIR, filename)
                
                # Пропускаем уже обработанные файлы
                if file_path in processed_downloads_files:
                    continue
                
                # Определяем тип файла по расширению
                file_ext = os.path.splitext(filename)[1].lower()
                is_video = file_ext in video_extensions
                is_audio = file_ext in audio_extensions
                
                # Пропускаем файлы, которые не являются аудио или видео
                if not (is_video or is_audio):
                    continue
                
                # Проверяем, загружен ли файл полностью
                # Если файл уже отслеживается как загружающийся, проверяем его снова
                if file_path in files_being_uploaded:
                    # Проверяем, завершилась ли загрузка
                    if await is_file_fully_uploaded(file_path):
                        # Файл загружен, удаляем из списка загружающихся
                        del files_being_uploaded[file_path]
                        # Продолжаем обработку ниже
                    else:
                        # Файл еще загружается, пропускаем на этот раз
                        logger.debug(f"Файл {filename} еще загружается, пропускаем на этот раз")
                        continue
                else:
                    # Новый файл, проверяем загружен ли он
                    if not await is_file_fully_uploaded(file_path):
                        # Файл еще загружается, добавляем в список отслеживания
                        files_being_uploaded[file_path] = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                        logger.debug(f"Файл {filename} обнаружен, но еще загружается. Добавлен в список отслеживания.")
                        continue
                
                # Проверяем размер файла
                try:
                    file_size = os.path.getsize(file_path)
                    file_size_mb = file_size / (1024 * 1024)
                    
                    if file_size == 0:
                        logger.warning(f"Пропускаем пустой файл: {filename}")
                        processed_downloads_files.add(file_path)  # Помечаем как обработанный
                        continue
                    
                    if file_size > MAX_FILE_SIZE:
                        logger.warning(f"Файл слишком большой для обработки: {filename} ({file_size_mb:.2f} МБ)")
                        processed_downloads_files.add(file_path)  # Помечаем как обработанный, чтобы не проверять снова
                        continue
                    
                    # Определяем тип файла для сообщения
                    file_type = "видео" if is_video else "аудио"
                    logger.info(f"Обнаружен новый {file_type} файл в downloads (полностью загружен): {filename} ({file_size_mb:.2f} МБ)")
                    
                    # Предсказываем время обработки
                    # Не передаем is_video явно, чтобы predict_processing_time могла точно определить тип файла через ffprobe
                    # Это обеспечит одинаковую логику расчета времени для файлов из downloads и из Telegram
                    estimated_time = predict_processing_time(file_path, WHISPER_MODEL, is_video=None)
                    estimated_time_str = format_processing_time(estimated_time)
                    
                    # Проверяем, нужно ли использовать модель меньшего размера
                    should_switch, smaller_model = should_use_smaller_model(file_size_mb, WHISPER_MODEL)
                    if should_switch:
                        estimated_time = predict_processing_time(file_path, smaller_model, is_video=None)
                        estimated_time_str = format_processing_time(estimated_time)
                    
                    # Запускаем фоновый обработчик очереди, если он еще не запущен
                    await ensure_background_processor_running()
                    
                    # Добавляем задачу в базу данных
                    # Используем специальный user_id для файлов из downloads и фиктивные message_id и chat_id
                    add_to_queue(DOWNLOADS_USER_ID, file_path, filename, file_size_mb, 0, 0)
                    
                    # Помечаем файл как обработанный
                    processed_downloads_files.add(file_path)
                    
                    # Удаляем из списка загружающихся, если был там
                    files_being_uploaded.pop(file_path, None)
                    
                    logger.info(f"Файл {filename} добавлен в очередь транскрибации из папки downloads")
                    
                except Exception as e:
                    logger.exception(f"Ошибка при обработке файла {filename} из downloads: {e}")
                    # Удаляем из списка загружающихся при ошибке
                    files_being_uploaded.pop(file_path, None)
            
            # Очищаем устаревшие записи о загружающихся файлах (файлы, которых больше нет)
            files_to_remove = []
            for tracked_path in list(files_being_uploaded.keys()):
                if not os.path.exists(tracked_path):
                    files_to_remove.append(tracked_path)
                    logger.debug(f"Удаляем из отслеживания несуществующий файл: {os.path.basename(tracked_path)}")
            
            for path in files_to_remove:
                files_being_uploaded.pop(path, None)
            
            # Проверяем каждые 30 секунд
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.exception(f"Ошибка в мониторинге папки downloads: {e}")
            await asyncio.sleep(60)  # При ошибке ждем дольше

def init_downloads_monitoring():
    """
    Инициализирует мониторинг папки downloads для автоматической обработки файлов
    """
    async def delayed_start():
        await asyncio.sleep(15)  # Задержка 15 секунд после запуска бота
        await asyncio.create_task(monitor_downloads_folder())
        logger.info("Запущен мониторинг папки downloads")
        
    asyncio.create_task(delayed_start())
    logger.info("Мониторинг папки downloads будет запущен через 15 секунд")
