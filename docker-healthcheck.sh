#!/bin/bash

# Проверка доступности Local Bot API если переменная установлена
if [ ! -z "$LOCAL_BOT_API" ]; then
  # Извлекаем хост и порт из переменной LOCAL_BOT_API
  HOST=$(echo $LOCAL_BOT_API | sed -E 's|^https?://||g' | cut -d ':' -f 1)
  PORT=$(echo $LOCAL_BOT_API | sed -E 's|^https?://||g' | grep -o ':[0-9]*' | cut -d ':' -f 2)
  
  # Если порт не указан, используем 8081 по умолчанию
  if [ -z "$PORT" ]; then
    PORT=8082
  fi
  
  echo "Проверка соединения с Local Bot API Server: $HOST:$PORT"
  
  # Пробуем подключиться к Local Bot API Server
  if curl -s "$HOST:$PORT" > /dev/null; then
    echo "Local Bot API Server доступен"
  else
    echo "Ошибка: Local Bot API Server недоступен"
    # Не завершаем процесс с ошибкой, чтобы контейнер мог продолжить работу без Local API
    # exit 1
  fi
fi

# Проверка установки и работы ffmpeg
if command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg установлен"
else
  echo "Ошибка: FFmpeg не установлен"
  exit 1
fi

# Проверка наличия директорий для данных
for dir in logs temp_audio transcriptions whisper_models; do
  if [ -d "/app/$dir" ]; then
    echo "Директория /app/$dir существует"
  else
    echo "Ошибка: директория /app/$dir не существует"
    exit 1
  fi
done

# Проверка доступности Python и виртуальной среды
if python -c "import sys; print(sys.version)" > /dev/null 2>&1; then
  echo "Python доступен"
else
  echo "Ошибка: Python недоступен"
  exit 1
fi

echo "Проверка работоспособности завершена успешно"
exit 0 