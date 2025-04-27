#!/bin/bash

# Скрипт для диагностики и исправления проблем с Telegram Bot API Server
set -e

echo "Диагностика и исправление Telegram Bot API Server..."

# Получаем имя контейнера API сервера
API_CONTAINER=$(docker ps -a | grep telegram-bot-api | awk '{print $1}')

if [ -z "$API_CONTAINER" ]; then
    echo "Ошибка: контейнер Telegram Bot API не найден."
    echo "Проверьте, запущен ли Docker и запускали ли вы docker-compose."
    exit 1
fi

echo "Найден контейнер Telegram Bot API: $API_CONTAINER"

# Проверяем статус контейнера
CONTAINER_STATUS=$(docker inspect --format='{{.State.Status}}' $API_CONTAINER)
echo "Статус контейнера: $CONTAINER_STATUS"

if [ "$CONTAINER_STATUS" = "running" ]; then
    # Проверяем логи для диагностики
    echo "Проверка логов контейнера для диагностики:"
    docker logs --tail 50 $API_CONTAINER

    # Проверяем наличие необходимых директорий
    echo "Проверка директорий внутри контейнера:"
    docker exec $API_CONTAINER ls -la /var/lib/telegram-bot-api /tmp/telegram-bot-api /var/log/telegram-bot-api

    # Проверяем, запущен ли процесс Telegram Bot API
    echo "Проверка процессов внутри контейнера:"
    docker exec $API_CONTAINER ps aux | grep telegram-bot-api

    # Проверяем, доступен ли порт API сервера
    PORT=$(docker exec $API_CONTAINER bash -c 'echo $API_PORT')
    PORT=${PORT:-8082}
    echo "Проверка доступности порта $PORT внутри контейнера:"
    docker exec $API_CONTAINER bash -c "netstat -tulpn | grep $PORT"

    echo "Проверка доступности HTTP-сервера внутри контейнера:"
    docker exec $API_CONTAINER curl -v http://localhost:$PORT/
else
    echo "Контейнер не запущен. Пробуем запустить..."
    docker start $API_CONTAINER
    
    # Проверяем состояние после запуска
    sleep 5
    NEW_STATUS=$(docker inspect --format='{{.State.Status}}' $API_CONTAINER)
    echo "Новый статус контейнера: $NEW_STATUS"
    
    if [ "$NEW_STATUS" = "running" ]; then
        echo "Контейнер успешно запущен, проверяем логи:"
        docker logs --tail 20 $API_CONTAINER
    else
        echo "Не удалось запустить контейнер. Проверьте логи для выяснения причины:"
        docker logs $API_CONTAINER
    fi
fi

echo "Рекомендации по исправлению:"
echo "1. Убедитесь, что в .env файле корректно указаны API_ID и API_HASH от Telegram"
echo "2. Проверьте, что порт API_PORT не занят другим приложением"
echo "3. Попробуйте пересоздать контейнеры: docker-compose down && docker-compose up -d --build"

echo "Для полного пересоздания всех контейнеров, очистки томов и перестройки образов выполните:"
echo "docker-compose down -v"
echo "docker-compose build --no-cache"
echo "docker-compose up -d" 