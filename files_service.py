import asyncio
import logging
import os
import pathlib
import re
import aiohttp
from datetime import datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

from create_bot import TEMP_AUDIO_DIR, TRANSCRIPTION_DIR, MAX_MESSAGE_LENGTH, LOCAL_BOT_API, MAX_CAPTION_LENGTH, \
    MAX_FILE_SIZE, bot, LOCAL_BOT_API_FILES_PATH

logger = logging.getLogger(__name__)

def format_timestamp(seconds):
    """Форматирует время в секундах в формат часы:минуты:секунды,миллисекунды"""
    milliseconds = int((seconds % 1) * 1000)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = int(seconds % 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


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

