#!/bin/bash

# Проверка наличия API_ID и API_HASH
if [ -z "$API_ID" ] || [ -z "$API_HASH" ]; then
    echo "Ошибка: Необходимо указать API_ID и API_HASH в переменных окружения"
    echo "Пример запуска:"
    echo "docker run -d --name telegram-bot-api -p 8081:8081 -e API_ID=your_api_id -e API_HASH=your_api_hash telegram-bot-api"
    exit 1
fi

# Настройка порта (по умолчанию 8081)
PORT=${API_PORT:-8081}

echo "Запуск Telegram Bot API Server..."
echo "API_ID: $API_ID"
echo "API_HASH: $API_HASH (скрыт для безопасности)"
echo "Порт: $PORT"

# Запуск Telegram Bot API Server
/usr/local/bin/telegram-bot-api \
    --api-id=$API_ID \
    --api-hash=$API_HASH \
    --local \
    --http-port=$PORT \
    --dir=/var/lib/telegram-bot-api \
    --temp-dir=/tmp/telegram-bot-api \
    --log=/var/log/telegram-bot-api/bot-api.log

# Проверка статуса запуска
if [ $? -ne 0 ]; then
    echo "Ошибка при запуске Telegram Bot API Server"
    exit 1
fi

# Запуск сервера в фоновом режиме и вывод логов
tail -f /var/log/telegram-bot-api/bot-api.log