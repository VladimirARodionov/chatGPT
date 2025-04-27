import asyncio
import logging
import os
import pathlib
from datetime import datetime, timedelta

import aiohttp
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, FSInputFile, User
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor

from audio_utils import predict_processing_time, should_use_smaller_model, convert_audio_format, \
    transcribe_with_whisper, should_condition_on_previous_text
from create_bot import MAX_FILE_SIZE, bot, MAX_MESSAGE_LENGTH, MAX_CAPTION_LENGTH, USE_LOCAL_WHISPER, TEMP_AUDIO_DIR, \
    LOCAL_BOT_API, env_config, WHISPER_MODEL, STANDARD_API_LIMIT, LOCAL_BOT_API_FILES_PATH
from db_service import check_message_limit
from files_service import cleanup_temp_files, save_transcription_to_file, split_text_into_chunks

logger = logging.getLogger(__name__)


# Создаем очередь задач для обработки аудио в фоновом режиме
audio_task_queue = asyncio.Queue()
# Пул потоков для CPU-интенсивных операций
thread_executor = ThreadPoolExecutor(max_workers=3)

# Флаг для отслеживания статуса обработчика очереди
background_worker_running = False

# Словарь для отслеживания активных задач транскрибации по пользователям
# Ключ - user_id, значение - (future, message_id, file_path)
active_transcriptions = {}


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
            # Проверяем, привязан ли message к боту
            if hasattr(message, "bot") and message.bot is not None:
                await message.answer("Файл слишком большой для отправки, разделяю на части...")
            else:
                await bot.send_message(chat_id=message.chat.id, text="Файл слишком большой для отправки, разделяю на части...")

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
                try:
                    if hasattr(message, "bot") and message.bot is not None:
                        await message.answer_document(
                            FSInputFile(part_filename),
                            caption=part_caption[:MAX_CAPTION_LENGTH]
                        )
                    else:
                        await bot.send_document(
                            chat_id=message.chat.id,
                            document=FSInputFile(part_filename),
                            caption=part_caption[:MAX_CAPTION_LENGTH]
                        )
                except Exception as e:
                    logger.error(f"Ошибка при отправке части файла {i+1}: {e}")
                    # Пробуем через основной метод если частичный не сработал
                    if hasattr(message, "bot") and message.bot is not None:
                        await message.answer(f"Ошибка при отправке части {i+1}: {str(e)}")
                    else:
                        await bot.send_message(chat_id=message.chat.id, text=f"Ошибка при отправке части {i+1}: {str(e)}")

            return True
        else:
            # Обычная отправка файла
            if caption and len(caption) > MAX_CAPTION_LENGTH:
                caption = caption[:MAX_CAPTION_LENGTH-3] + "..."

            try:
                if hasattr(message, "bot") and message.bot is not None:
                    await message.answer_document(
                        FSInputFile(file_path),
                        caption=caption
                    )
                else:
                    await bot.send_document(
                        chat_id=message.chat.id,
                        document=FSInputFile(file_path),
                        caption=caption
                    )
                return True
            except Exception as e:
                logger.error(f"Ошибка при отправке файла: {e}")
                # Пробуем сообщить об ошибке
                if hasattr(message, "bot") and message.bot is not None:
                    await message.answer(f"Произошла ошибка при отправке файла: {str(e)}")
                else:
                    await bot.send_message(chat_id=message.chat.id, text=f"Произошла ошибка при отправке файла: {str(e)}")
                return False

    except TelegramBadRequest as e:
        if "file is too big" in str(e).lower():
            # Если все равно получаем ошибку о большом размере файла
            logger.error(f"Файл {file_path} слишком большой для отправки через Telegram API: {e}")
            if hasattr(message, "bot") and message.bot is not None:
                await message.answer(
                    "Файл слишком большой для отправки через Telegram. "
                    "Попробуйте транскрибировать аудио меньшей длительности."
                )
            else:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text="Файл слишком большой для отправки через Telegram. "
                         "Попробуйте транскрибировать аудио меньшей длительности."
                )
        else:
            logger.exception(f"Ошибка Telegram при отправке файла: {e}")
            if hasattr(message, "bot") and message.bot is not None:
                await message.answer(f"Ошибка при отправке файла: {str(e)}")
            else:
                await bot.send_message(chat_id=message.chat.id, text=f"Ошибка при отправке файла: {str(e)}")
        return False
    except Exception as e:
        logger.exception(f"Ошибка при отправке файла: {e}")
        try:
            if hasattr(message, "bot") and message.bot is not None:
                await message.answer(f"Произошла ошибка при отправке файла: {str(e)}")
            else:
                await bot.send_message(chat_id=message.chat.id, text=f"Произошла ошибка при отправке файла: {str(e)}")
        except Exception as msg_error:
            logger.exception(f"Не удалось отправить сообщение об ошибке: {msg_error}")
        return False


async def handle_audio(message: Message):
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
            position_text = "🔥 Ваш файл первый в очереди."
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

async def transcribe_audio(file_path, condition_on_previous_text = False, use_local_whisper=USE_LOCAL_WHISPER):
    """Транскрибация аудио с использованием OpenAI API или локальной модели Whisper"""
    try:
        if use_local_whisper:
            # Конвертируем в нужный формат для Whisper если нужно
            converted_file = await convert_audio_format(file_path)

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

                    # Проверка и исправление from_user (если нужно)
                    if not hasattr(message, 'from_user') or message.from_user is None:
                        # Если from_user отсутствует, создаем его
                        from_user = User(
                            id=user_id,
                            is_bot=False,
                            first_name="Восстановлено",
                            last_name="",
                            username="restored_user",
                            language_code="ru"
                        )
                        message.from_user = from_user
                        logger.info(f"Добавлен атрибут from_user для восстановленного сообщения (user_id={user_id})")

                    # Получаем данные пользователя для транскрибации
                    try:
                        username = message.from_user.username
                        if username is None:
                            message.from_user.username = "restored_user"
                            username = "restored_user"
                            logger.info(f"Пользователь не имеет username, установлен restored_user (user_id={user_id})")

                        first_name = message.from_user.first_name
                        if first_name is None:
                            message.from_user.first_name = "Восстановлено"
                            first_name = "Восстановлено"

                        last_name = message.from_user.last_name
                        if last_name is None:
                            message.from_user.last_name = ""
                            last_name = ""
                    except AttributeError as e:
                        logger.warning(f"Ошибка при получении данных пользователя, пробуем исправить: {e}")
                        # Если у from_user нет нужных атрибутов, добавляем их
                        try:
                            if not hasattr(message.from_user, 'username'):
                                message.from_user.username = "restored_user"
                            if not hasattr(message.from_user, 'first_name'):
                                message.from_user.first_name = "Восстановлено"
                            if not hasattr(message.from_user, 'last_name'):
                                message.from_user.last_name = ""

                            username = message.from_user.username
                            first_name = message.from_user.first_name
                            last_name = message.from_user.last_name
                            logger.info(f"Атрибуты пользователя добавлены (user_id={user_id})")
                        except Exception as fix_err:
                            # Если не удалось добавить атрибуты, создаем новый from_user
                            logger.error(f"Не удалось добавить атрибуты, создаем новый from_user: {fix_err}")
                            from_user = User(
                                id=user_id,
                                is_bot=False,
                                first_name="Восстановлено",
                                last_name="",
                                username="restored_user",
                                language_code="ru"
                            )
                            message.from_user = from_user
                            username = "restored_user"
                            first_name = "Восстановлено"
                            last_name = ""

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
                            await bot.send_message(processing_msg.chat.id,
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
                            lambda fp=file_path: asyncio.run(transcribe_audio(fp, should_condition_on_previous_text(file_size_mb)))
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
