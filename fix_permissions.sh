#!/bin/bash

# Этот скрипт исправляет права доступа для текущих контейнеров Docker
# Запустите его, если у вас возникли проблемы с правами доступа к файлам

set -e

echo "Исправление прав доступа для контейнеров Docker..."

# Получаем имя контейнера приложения
APP_CONTAINER=$(docker ps | grep chatgptapp | awk '{print $1}')

if [ -z "$APP_CONTAINER" ]; then
    echo "Ошибка: контейнер приложения не найден. Убедитесь, что Docker работает и контейнеры запущены."
    exit 1
fi

echo "Найден контейнер приложения: $APP_CONTAINER"

# Создаем и исправляем права доступа для директорий
echo "Исправление прав доступа для директорий в контейнере..."

docker exec -it $APP_CONTAINER bash -c "
    mkdir -p /app/logs /app/temp_audio /app/transcriptions /app/whisper_models &&
    chown -R app:app /app/logs /app/temp_audio /app/transcriptions /app/whisper_models &&
    chmod -R 755 /app/logs /app/temp_audio /app/transcriptions /app/whisper_models
"

echo "Права доступа исправлены."

# Перезапуск контейнера для применения изменений
echo "Перезапуск контейнера приложения..."
docker restart $APP_CONTAINER

echo "Готово! Проблема с правами доступа должна быть исправлена."
echo "Если проблема не решена, попробуйте пересобрать и перезапустить контейнеры с помощью:"
echo "docker-compose down && docker-compose up -d --build" 