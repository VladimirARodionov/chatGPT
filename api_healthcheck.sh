#!/bin/bash

# Скрипт для проверки работоспособности Telegram Bot API Server

# Получаем порт API сервера (по умолчанию 8082)
PORT=${API_PORT:-8082}

# Проверяем доступность API сервера
if curl -s http://localhost:$PORT/ > /dev/null; then
    echo "Telegram Bot API Server работает на порту $PORT"
    exit 0
else
    echo "Ошибка: Telegram Bot API Server не отвечает на порту $PORT"
    
    # Проверяем, запущен ли процесс
    if pgrep -f "telegram-bot-api" > /dev/null; then
        echo "Процесс telegram-bot-api запущен, но не отвечает на HTTP-запросы"
        # Дополнительная диагностика
        ps aux | grep telegram-bot-api
        # Проверяем лог файл
        if [ -f /var/log/telegram-bot-api/bot-api.log ]; then
            echo "Последние 10 строк лога:"
            tail -n 10 /var/log/telegram-bot-api/bot-api.log
        else
            echo "Лог-файл не найден"
        fi
    else
        echo "Процесс telegram-bot-api не запущен"
    fi
    
    exit 1
fi 