# Руководство по настройке Local Bot API Server

Это руководство поможет вам настроить локальный сервер Telegram Bot API для обработки файлов размером до 2000 МБ (2 ГБ).

## Преимущества Local Bot API Server

- **Увеличенный лимит размера файлов**: до 2000 МБ вместо стандартных 20 МБ
- **Уменьшенные задержки**: сервер работает локально, что снижает задержки при обработке запросов
- **Дополнительный контроль**: вы можете настроить сервер под свои нужды

## Требования

- Linux-сервер (рекомендуется Ubuntu 20.04 или новее)
- Минимум 2 ГБ оперативной памяти
- Не менее 10 ГБ свободного дискового пространства
- Доступ по SSH с правами администратора
- Открытый порт (по умолчанию 8081)

## Шаги по установке

### 1. Установка зависимостей

```bash
sudo apt-get update
sudo apt-get install -y build-essential libssl-dev zlib1g-dev cmake git
```

### 2. Клонирование репозитория Telegram Bot API

```bash
git clone --recursive https://github.com/tdlib/telegram-bot-api.git
cd telegram-bot-api
```

### 3. Сборка проекта

```bash
mkdir build
cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build .
```

Сборка может занять некоторое время (10-30 минут в зависимости от мощности сервера).

### 4. Получение API ID и API Hash

1. Перейдите на сайт [my.telegram.org](https://my.telegram.org/) и войдите в систему.
2. Выберите "API development tools".
3. Создайте новое приложение, заполнив форму:
   - App title: ваше название (например, "My Local Bot API")
   - Short name: краткое название (например, "mylocalbotapi")
   - Platform: Other
   - Description: краткое описание вашего приложения
4. Нажмите "Create application".
5. Запишите значения **api_id** и **api_hash** - они понадобятся для запуска сервера.

### 5. Запуск Local Bot API Server

```bash
./telegram-bot-api --api-id=YOUR_API_ID --api-hash=YOUR_API_HASH --local
```

Замените YOUR_API_ID и YOUR_API_HASH на значения, полученные на предыдущем шаге.

Для запуска в фоновом режиме:

```bash
nohup ./telegram-bot-api --api-id=YOUR_API_ID --api-hash=YOUR_API_HASH --local > bot_api.log 2>&1 &
```

### 6. Настройка Local Bot API Server как службы systemd

Создайте файл службы:

```bash
sudo nano /etc/systemd/system/telegram-bot-api.service
```

Добавьте следующее содержимое:

```
[Unit]
Description=Telegram Bot API Server
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/path/to/telegram-bot-api/build
ExecStart=/path/to/telegram-bot-api/build/telegram-bot-api --api-id=YOUR_API_ID --api-hash=YOUR_API_HASH --local
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Замените YOUR_USERNAME на имя вашего пользователя, а также замените пути и API-параметры.

Активируйте и запустите службу:

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot-api
sudo systemctl start telegram-bot-api
```

Проверьте статус службы:

```bash
sudo systemctl status telegram-bot-api
```

## Настройка бота для использования Local Bot API Server

### 1. Изменение файла .env

Добавьте в ваш файл .env следующую строку:

```
LOCAL_BOT_API=http://localhost:8081
```

### 2. Обновление кода бота

Откройте файл bot.py и добавьте поддержку Local Bot API Server. 

Для этого измените инициализацию бота следующим образом:

```python
# Получаем URL локального Bot API сервера из .env файла
LOCAL_BOT_API = env_config.get('LOCAL_BOT_API', None)

# Инициализация бота с учетом локального сервера, если он настроен
if LOCAL_BOT_API:
    bot = Bot(token=env_config.get('TELEGRAM_TOKEN'), base_url=LOCAL_BOT_API)
    logger.info(f'Используется локальный Telegram Bot API сервер: {LOCAL_BOT_API}')
else:
    bot = Bot(token=env_config.get('TELEGRAM_TOKEN'))
```

### 3. Обновление ограничений размера файла

Если настроен локальный Bot API сервер, обновите ограничение размера файла:

```python
# Ограничения для Telegram
MAX_MESSAGE_LENGTH = 4096  # максимальная длина сообщения в Telegram
MAX_CAPTION_LENGTH = 1024  # максимальная длина подписи к файлу

# Устанавливаем лимиты в зависимости от использования локального Bot API
if LOCAL_BOT_API:
    MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2000 МБ для локального Bot API
else:
    MAX_FILE_SIZE = 20 * 1024 * 1024    # 20 МБ для обычного Bot API
```

## Проверка работоспособности

Для проверки, что Local Bot API Server работает корректно:

1. Перезапустите вашего бота
2. Попробуйте отправить большой аудиофайл (до 2 ГБ)
3. Бот должен успешно его обработать и выполнить транскрибацию

## Возможные проблемы и их решение

### Ошибка соединения с сервером

Если бот не может подключиться к локальному серверу, убедитесь, что:
- Сервер запущен и работает (проверьте `systemctl status telegram-bot-api`)
- Порт открыт и доступен (проверьте `netstat -tulpn | grep 8081`)
- URL в .env файле указан правильно, включая http:// или https:// префикс

### Ошибка авторизации

Если при запуске локального сервера возникают ошибки авторизации:
- Проверьте правильность введенных api_id и api_hash
- Убедитесь, что у вас есть доступ к API Telegram (не забанен аккаунт)

### Сервер запускается, но бот все равно не может загружать большие файлы

- Проверьте логи сервера и бота для выявления ошибок
- Убедитесь, что в коде бота правильно настроены ограничения размера файла
- Проверьте, что бот фактически использует локальный сервер (должна быть запись в логах)

## Дополнительные ресурсы

- [Официальная документация Telegram Bot API](https://core.telegram.org/bots/api)
- [GitHub репозиторий telegram-bot-api](https://github.com/tdlib/telegram-bot-api)
- [Информация о Local Bot API Server](https://core.telegram.org/bots/api#using-a-local-bot-api-server)

## Заключение

Настройка Local Bot API Server требует дополнительных технических знаний и доступа к серверу, но она значительно расширяет возможности бота по работе с большими файлами. После настройки ваш бот сможет обрабатывать аудиофайлы размером до 2 ГБ, что позволит транскрибировать даже очень длинные записи. 