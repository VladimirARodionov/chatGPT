#!/bin/bash

# Проверка наличия API_ID и API_HASH
if [ -z "$API_ID" ] || [ -z "$API_HASH" ]; then
    echo "Ошибка: Необходимо указать API_ID и API_HASH в переменных окружения"
    echo "Пример запуска:"
    echo "docker run -d --name telegram-bot-api -p 8082:8082 -e API_ID=your_api_id -e API_HASH=your_api_hash telegram-bot-api"
    exit 1
fi

# Настройка порта (по умолчанию 8082)
PORT=${API_PORT:-8082}

# Создаем необходимые директории, если их нет
mkdir -p /var/lib/telegram-bot-api /tmp/telegram-bot-api /var/log/telegram-bot-api
chmod -R 777 /var/lib/telegram-bot-api /tmp/telegram-bot-api /var/log/telegram-bot-api

echo "Запуск Telegram Bot API Server..."
echo "API_ID: $API_ID"
echo "API_HASH: $API_HASH (скрыт для безопасности)"
echo "Порт: $PORT"

# Запуск Telegram Bot API Server с перенаправлением логов
/usr/local/bin/telegram-bot-api \
    --api-id=$API_ID \
    --api-hash=$API_HASH \
    --local \
    --http-port=$PORT \
    --dir=/var/lib/telegram-bot-api \
    --temp-dir=/tmp/telegram-bot-api \
    --log=/var/log/telegram-bot-api/bot-api.log \
    --verbosity=1 > /var/log/telegram-bot-api/stdout.log 2>&1 &

# Сохраняем PID процесса
BOT_API_PID=$!
echo "Telegram Bot API Server запущен с PID: $BOT_API_PID"

# Небольшая задержка для инициализации сервера
sleep 5

# Проверяем, запустился ли сервер
if ps -p $BOT_API_PID > /dev/null; then
    echo "Telegram Bot API Server успешно запущен, ожидание HTTP-запросов на порту $PORT"
    
    # Проверяем, отвечает ли сервер на HTTP-запросы
    for i in {1..12}; do
        if curl -s http://localhost:$PORT/ > /dev/null; then
            echo "Telegram Bot API Server готов к работе!"
            break
        else
            echo "Ожидание готовности HTTP-сервера... ($i/12)"
            sleep 5
        fi
        
        # Если это последняя попытка, показываем журнал для диагностики
        if [ $i -eq 12 ]; then
            echo "Сервер запущен, но не отвечает на HTTP-запросы. Проверьте лог для диагностики:"
            tail -n 20 /var/log/telegram-bot-api/bot-api.log
        fi
    done
else
    echo "Ошибка: Telegram Bot API Server не запустился"
    cat /var/log/telegram-bot-api/stdout.log
    exit 1
fi

# Запуск отфильтрованного вывода логов - исключаем строки с "CPU usage"
tail -f /var/log/telegram-bot-api/bot-api.log | grep -v "CPU usage"