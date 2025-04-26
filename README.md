# Бот ChatGPT с транскрибацией аудио

Telegram-бот, который позволяет общаться с ChatGPT и транскрибировать аудиосообщения с помощью OpenAI Whisper.

## Функциональность

- Обмен текстовыми сообщениями с ChatGPT
- Транскрибация голосовых сообщений и аудиофайлов с помощью:
  - Локальной модели Whisper (https://github.com/openai/whisper)
  - API OpenAI (whisper-1)
- Сохранение транскрибаций в текстовые файлы с возможностью скачивания
- Кеширование моделей Whisper для использования между перезапусками
- Удобное главное меню с доступом ко всем командам
- Ограничение в 50 сообщений на пользователя в сутки
- Сохранение статистики использования в базе данных PostgreSQL

## Установка

1. Клонировать репозиторий:
```
git clone <repository_url>
cd <repository_folder>
```

2. Установить зависимости:
```
pip install -r requirements.txt
```

3. Установить FFmpeg (необходим для обработки аудио):
   - **Windows**: Скачать с [ffmpeg.org](https://ffmpeg.org/download.html) и добавить в PATH
   - **Linux**: `sudo apt-get install ffmpeg`
   - **MacOS**: `brew install ffmpeg`

4. Создать файл .env со следующими параметрами:
```
TELEGRAM_TOKEN=<your_telegram_bot_token>
OPEN_AI_TOKEN=<your_openai_api_key>
MODEL=gpt-3.5-turbo
POSTGRES_USER=<database_user>
POSTGRES_PASSWORD=<database_password>
POSTGRES_DB=<database_name>
POSTGRES_PORT=5432
USE_LOCAL_WHISPER=True  # True - использовать локальную модель, False - API OpenAI
WHISPER_MODEL=base  # Модель Whisper: tiny, base, small, medium, large
WHISPER_MODELS_DIR=whisper_models  # Директория для хранения моделей
```

5. Настроить базу данных PostgreSQL и запустить миграции:
```
alembic upgrade head
```

## Запуск

```
python bot.py
```

## Доступные модели Whisper

| Размер  | Параметры | Только английский | Мультиязычная модель | Требуемая VRAM | Относит. скорость |
|---------|-----------|-------------------|----------------------|----------------|-------------------|
| tiny    | 39M       | tiny.en           | tiny                 | ~1 GB          | ~10x              |
| base    | 74M       | base.en           | base                 | ~1 GB          | ~7x               |
| small   | 244M      | small.en          | small                | ~2 GB          | ~4x               |
| medium  | 769M      | medium.en         | medium               | ~5 GB          | ~2x               |
| large   | 1550M     | N/A               | large                | ~10 GB         | 1x                |
| turbo   | 809M      | N/A               | turbo                | ~6 GB          | ~8x               |

Для русского языка рекомендуется использовать мультиязычные модели.

## Команды бота

- `/start` - Начать общение с ботом
- `/menu` - Показать главное меню
- `/status` - Проверить лимит сообщений на сегодня
- `/models` - Показать список скачанных моделей Whisper
- `/help` - Показать справку

## Использование

1. Отправьте текстовое сообщение боту для получения ответа от ChatGPT
2. Отправьте голосовое сообщение или аудиофайл для транскрибации:
   - Бот вернет текст транскрибации в сообщении
   - Также бот отправит файл с полной транскрибацией (удобно для длинных аудио)
   - Файлы транскрибаций сохраняются в директории `transcriptions/`

## Структура директорий

- `temp_audio/` - временная директория для сохранения аудиофайлов
- `transcriptions/` - директория для сохранения файлов с транскрибацией
- `whisper_models/` - директория для хранения скачанных моделей Whisper
- `alembic_migrations/` - миграции базы данных Alembic