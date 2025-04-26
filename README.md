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
- Поддержка Local Bot API Server для работы с файлами размером до 2 ГБ

## Ограничения

- **Максимальный размер аудиофайла**: 
  - 20 МБ (стандартный Telegram Bot API)
  - 2000 МБ / 2 ГБ (с использованием Local Bot API Server)
- Максимальная длина сообщения: 4096 символов (ограничение Telegram)
- Максимальная длина подписи к файлу: 1024 символа

## Установка и запуск

### Запуск с использованием Docker (рекомендуется)

1. Клонировать репозиторий:
```bash
git clone <repository_url>
cd <repository_folder>
```

2. Создать файл `.env` на основе `.env.example`:
```bash
cp .env.example .env
```

3. Заполнить необходимые переменные в файле `.env`:
   - `TELEGRAM_TOKEN` - токен бота Telegram (получить у @BotFather)
   - `OPEN_AI_TOKEN` - API ключ OpenAI
   - `API_ID` и `API_HASH` - данные для Telegram API (получить на my.telegram.org)

4. Запустить контейнеры с помощью Docker Compose:
```bash
docker-compose up -d
```

5. Проверить логи:
```bash
docker-compose logs -f
```

### Установка без Docker

1. Клонировать репозиторий:
```bash
git clone <repository_url>
cd <repository_folder>
```

2. Установить зависимости:
```bash
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
USE_LOCAL_WHISPER=True
WHISPER_MODEL=base
WHISPER_MODELS_DIR=whisper_models
```

5. Настроить базу данных PostgreSQL и запустить миграции:
```bash
alembic upgrade head
```

6. Запустить бота:
```bash
python bot.py
```

## Настройка Local Bot API Server для больших файлов

Для работы с большими аудиофайлами (до 2 ГБ) бот поддерживает [Local Bot API Server](https://core.telegram.org/bots/api#using-a-local-bot-api-server).

### Запуск с Docker Compose (автоматическая настройка)

При запуске через `docker-compose up -d`, сервер Local Bot API будет настроен автоматически. 
Необходимо указать в файле `.env` следующие параметры:

```
API_ID=your_api_id_here         # Получить на my.telegram.org
API_HASH=your_api_hash_here     # Получить на my.telegram.org
LOCAL_BOT_API=http://telegram-bot-api:8081
```

### Ручная настройка без Docker

Для ручной настройки Local Bot API Server следуйте инструкциям в файле [local_bot_api_guide.md](local_bot_api_guide.md).

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
   - **Важно**: При использовании стандартного Telegram Bot API, аудио должно быть не более 20 МБ
   - **Важно**: При использовании Local Bot API Server, аудио может быть до 2 ГБ

## Структура директорий

- `temp_audio/` - временная директория для сохранения аудиофайлов
- `transcriptions/` - директория для сохранения файлов с транскрибацией
- `whisper_models/` - директория для хранения скачанных моделей Whisper
- `alembic_migrations/` - миграции базы данных Alembic
- `logs/` - директория для логов приложения и Bot API Server

## Устранение проблем

### Ошибка "file is too big"

Если вы получаете ошибку "Telegram server says - Bad Request: file is too big", это означает, что размер файла превышает ограничение Telegram Bot API в 20 МБ. Решения:

1. Используйте аудиофайл меньшего размера
2. Сократите длительность аудио
3. Разделите длинное аудио на несколько частей
4. Настройте Local Bot API Server как описано выше для обработки файлов до 2 ГБ

### Проблемы с правами доступа в Docker

Если вы получаете ошибку "Permission denied" при работе с файлами в Docker, это связано с неправильными правами доступа к директориям. 

#### Быстрое решение (без перезапуска)

1. Выполните скрипт для исправления прав доступа:
```bash
chmod +x fix_permissions.sh
./fix_permissions.sh
```

2. Скрипт найдет контейнер приложения, исправит права доступа внутри него и перезапустит контейнер.

#### Полное решение (с перезапуском)

1. Остановите и удалите текущие контейнеры:
```bash
docker-compose down
```

2. Пересоберите образы и запустите контейнеры заново:
```bash
docker-compose up -d --build
```

3. Проверьте логи, чтобы убедиться, что проблема устранена:
```bash
docker-compose logs -f chatgptapp
```

#### Проверка прав доступа вручную

Если проблемы с правами доступа сохраняются, вы можете проверить права доступа внутри контейнера:

```bash
# Получите ID контейнера
docker ps

# Запустите оболочку внутри контейнера
docker exec -it <container_id> bash

# Проверьте права доступа
ls -la /app/temp_audio
ls -la /app/transcriptions
```

Все директории должны иметь права на чтение и запись для пользователя `app` (755 или 777).
