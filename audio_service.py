import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor

from audio_utils import predict_processing_time, should_use_smaller_model, convert_audio_format, \
    transcribe_with_whisper, should_condition_on_previous_text, extract_audio_from_video
from create_bot import MAX_FILE_SIZE, bot, MAX_MESSAGE_LENGTH, USE_LOCAL_WHISPER, TEMP_AUDIO_DIR, \
    LOCAL_BOT_API, env_config, WHISPER_MODEL, STANDARD_API_LIMIT
from db_service import check_message_limit, get_queue, add_to_queue, set_active_queue, set_finished_queue, \
    set_cancelled_queue, get_db_session, get_first_from_queue, get_active_tasks, reset_active_tasks
from files_service import cleanup_temp_files, save_transcription_to_file, download_voice, \
    get_file_path_direct, download_large_file_direct, send_file_safely
from models import TranscribeQueue

logger = logging.getLogger(__name__)

# Пул потоков для CPU-интенсивных операций
thread_executor = ThreadPoolExecutor(max_workers=3)

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


async def handle_audio_service(message: Message):
    user_id = message.from_user.id

    if not USE_LOCAL_WHISPER and not check_message_limit(user_id):
        await message.answer("Вы достигли дневного лимита в 50 сообщений. Попробуйте завтра!")
        return

    # Определяем тип файла
    is_video = message.video is not None or message.video_note is not None
    is_audio = message.voice is not None or message.audio is not None
    
    # Отправляем сообщение о начале обработки
    file_type_text = "видео" if is_video else "аудио"
    processing_msg = await message.answer(f"Загружаю и обрабатываю {file_type_text}...")

    try:
        # Определяем, что за файл пришел
        if is_video:
            file_id = message.video.file_id if message.video else message.video_note.file_id
        else:
            file_id = message.voice.file_id if message.voice else message.audio.file_id

        # Имя исходного файла
        file_name = "Голосовое сообщение"
        if message.audio and message.audio.file_name:
            file_name = message.audio.file_name
        elif message.video and message.video.file_name:
            file_name = message.video.file_name
        elif message.video_note:
            file_name = "Видеосообщение"

        # Определяем расширение файла для сохранения
        if is_video:
            # Для видео сохраняем в исходном формате, затем извлечем аудио
            file_ext = "mp4"  # По умолчанию для видео
            if message.video and message.video.file_name:
                file_ext = os.path.splitext(message.video.file_name)[1][1:] or "mp4"
            file_path = f"{TEMP_AUDIO_DIR}/video_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{file_ext}"
        else:
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

            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
            return transcription.text
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
                    cleanup_temp_files(older_than_hours=24)

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
                
                # Проверяем, существует ли файл
                if not os.path.exists(file_path):
                    logger.error(f"Файл {file_path} не существует для задачи {active_task.id}")
                    set_finished_queue(active_task.id)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Ошибка: Файл для транскрибации не найден. Возможно, он был удален."
                    )
                    continue
                
                # Пытаемся получить объект message для ответа
                try:
                    # Сначала попробуем получить сообщение по ID
                    processing_msg = await bot.get_message(chat_id=chat_id, message_id=message_id)
                except Exception as e:
                    logger.warning(f"Не удалось получить сообщение: {e}")
                    # Если не удалось получить сообщение, создадим новое
                    processing_msg = await bot.send_message(
                        chat_id=chat_id,
                        text="Обработка аудиофайла началась..."
                    )

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
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"Транскрибирую аудио...\n\n"
                                f"⚠️ Обратите внимание: Файл имеет большой размер ({file_size_mb:.1f} МБ), "
                                f"поэтому вместо модели {WHISPER_MODEL} будет использована модель {smaller_model} для оптимизации памяти.\n\n"
                                f"Это может повлиять на качество транскрибации, но позволит обработать большой файл без ошибок."
                        )
                except Exception as e:
                    logger.exception(f"Ошибка при проверке размера файла: {e}")

                # Запускаем транскрибацию в отдельном потоке, чтобы не блокировать event loop
                loop = asyncio.get_event_loop()
                try:
                    # Перед созданием future, убедимся, что файл существует
                    if not os.path.exists(file_path):
                        logger.error(f"Файл не существует перед запуском транскрибации: {file_path}")
                        await processing_msg.edit_text(f"❌ Ошибка: Файл для транскрибации не найден.")
                        set_finished_queue(active_task.id)
                        continue
                        
                    # Создаем объект будущего результата
                    future = loop.run_in_executor(
                        thread_executor,
                        # Оборачиваем асинхронную функцию в синхронную
                        lambda fp=file_path: asyncio.run(transcribe_audio(fp, should_condition_on_previous_text(file_size_mb)))
                    )

                    # Ожидаем результат с периодическим обновлением статуса
                    start_time = datetime.now()
                    cancelled = False
                    
                    while not future.done():
                        # Проверяем, не отменена ли задача
                        with get_db_session() as session:
                            task_status = session.query(TranscribeQueue).filter(TranscribeQueue.id == active_task.id).first()
                            if task_status and task_status.cancelled:
                                cancelled = True
                                # Отменяем future (если возможно)
                                future.cancel()
                                logger.info(f"Транскрибация для пользователя {user_id} была отменена во время обработки.")

                                # Удаляем временные файлы
                                try:
                                    cleanup_temp_files(file_path)
                                except Exception as e:
                                    logger.exception(f"Ошибка при удалении временных файлов после отмены: {e}")

                                # Сообщаем пользователю об отмене
                                await processing_msg.edit_text("❌ Обработка была отменена.")
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

                            # Определяем тип файла
                            is_video_file = file_name and any(ext in file_name.lower() for ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv'])
                            file_type_label = "видео" if is_video_file or "Видеосообщение" in file_name else "аудио"
                            
                            status_message = (
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

                        # Небольшая пауза, чтобы не нагружать процессор
                        await asyncio.sleep(1)

                    # Если задача была отменена, пропускаем дальнейшую обработку
                    if cancelled:
                        continue

                    # Получаем результат
                    try:
                        transcription = await future
                    except asyncio.CancelledError:
                        logger.info(f"Транскрибация для пользователя {user_id} отменена")
                        await processing_msg.edit_text("❌ Обработка была отменена.")
                        set_cancelled_queue(active_task.id)
                        continue
                    except Exception as transcribe_error:
                        logger.exception(f"Ошибка при получении результата транскрибации: {transcribe_error}")
                        await processing_msg.edit_text(f"❌ Произошла ошибка при транскрибации: {str(transcribe_error)}")
                        set_finished_queue(active_task.id)
                        continue

                except Exception as e:
                    logger.exception(f"Ошибка при асинхронной транскрибации: {e}")
                    await processing_msg.edit_text(f"❌ Произошла ошибка при транскрибации: {str(e)}")
                    set_finished_queue(active_task.id)
                    continue

                # Определяем тип файла для сообщений об ошибках
                is_video_file = file_name and any(ext in file_name.lower() for ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv'])
                file_type_label = "видео" if is_video_file or "Видеосообщение" in file_name else "аудио"
                
                # Проверяем, получили ли мы результат
                if transcription is None:
                    # Если транскрибация не удалась, сообщаем об ошибке
                    await processing_msg.edit_text(
                        f"❌ Ошибка при транскрибации {file_type_label}: {file_name}\n\n"
                        f"Не удалось обработать {file_type_label}файл. Возможные причины:\n"
                        f"• Файл повреждён или имеет неподдерживаемый формат\n"
                        f"• {file_type_label.capitalize()} не содержит речи или имеет слишком низкое качество\n"
                        f"• Ошибка при обработке модели Whisper\n\n"
                        f"Пожалуйста, попробуйте отправить другой {file_type_label}файл или обратитесь к администратору."
                    )

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
                username = "unknown"
                first_name = "Unknown"
                last_name = ""
                
                # Пытаемся получить данные пользователя из БД или другим способом
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
                        f"⚠️ Предупреждение: Транскрибация {file_type_label} не содержит текста.\n\n"
                        f"Возможно, {file_type_label} не содержит распознаваемой речи или имеет слишком низкое качество."
                    )

                    # Удаляем временные файлы
                    try:
                        cleanup_temp_files(file_path)
                    except Exception as e:
                        logger.exception(f"Ошибка при удалении временных файлов: {e}")

                    # Отмечаем задачу как выполненную
                    set_finished_queue(active_task.id)
                    continue

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

def cancel_audio_processing(user_id: int) -> tuple[bool, str]:
    """Отмена обработки аудио для пользователя
    
    Args:
        user_id: ID пользователя
        
    Returns:
        Кортеж (успех операции, сообщение для пользователя)
    """
    logger.info(f"Попытка отмены обработки аудио для пользователя {user_id}")
    
    # Получаем все активные задачи пользователя
    user_queue = get_queue(user_id)
    
    if not user_queue:
        logger.info(f"Для пользователя {user_id} не найдено активных задач")
        return False, "У вас нет активных задач на транскрибацию."
    
    cancelled_count = 0
    
    # Отменяем все активные задачи пользователя
    for task in user_queue:
        if set_cancelled_queue(task.id):
            cancelled_count += 1
            logger.info(f"Задача {task.id} для пользователя {user_id} успешно отменена")
        else:
            logger.warning(f"Не удалось отменить задачу {task.id} для пользователя {user_id}")
    
    if cancelled_count > 0:
        # Формируем текст в зависимости от количества отмененных задач
        task_text = "задача" if cancelled_count == 1 else "задачи"
        if cancelled_count >= 5:
            task_text = "задач"
        
        return True, f"✅ {cancelled_count} {task_text} на транскрибацию отменено."
    else:
        return False, "Не удалось отменить задачи на транскрибацию."

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
