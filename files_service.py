import logging
import os
import re
from datetime import datetime

from create_bot import TEMP_AUDIO_DIR, TRANSCRIPTION_DIR, MAX_MESSAGE_LENGTH

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

