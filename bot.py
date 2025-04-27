import logging.config
import asyncio
import pathlib
import os
import json
import time
import signal

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from aiogram import types, Dispatcher
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeDefault, ReplyKeyboardRemove
from openai import OpenAI

import audio_service
from create_bot import env_config, bot, WHISPER_MODEL, WHISPER_MODELS_DIR, MAX_MESSAGE_LENGTH, \
    USE_LOCAL_WHISPER
from db_service import get_cmd_status, check_message_limit
from files_service import cleanup_temp_files
from audio_utils import list_downloaded_models

dp = Dispatcher()
# Загрузка переменных окружения
load_dotenv()

# Инициализация Alembic для миграций базы данных
alembic_cfg = Config("alembic.ini")
alembic_cfg.attributes['configure_logger'] = False
command.upgrade(alembic_cfg, "head")

# Настройка логирования
logging.config.fileConfig(fname=pathlib.Path(__file__).resolve().parent / 'logging.ini',
                          disable_existing_loggers=False)
logging.getLogger('aiogram.dispatcher').propagate = False
logging.getLogger('aiogram.event').propagate = False

logger = logging.getLogger(__name__)


# Список команд бота для меню
BOT_COMMANDS = [
    BotCommand(command="start", description="Начать общение с ботом"),
    BotCommand(command="help", description="Показать справку"),
    BotCommand(command="status", description="Проверить лимит сообщений"),
    BotCommand(command="models", description="Показать список моделей Whisper"),
    BotCommand(command="cancel", description="Отменить текущую обработку аудио"),
    BotCommand(command="queue", description="Показать очередь задач"),
]


# Путь для сохранения очереди
QUEUE_DIR = "queue"
QUEUE_SAVE_PATH = os.path.join(QUEUE_DIR, "saved_queue.json")

async def set_commands():
    """Установка команд бота в меню"""
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот, который может общаться с ChatGPT и транскрибировать аудио.\n\n"
        "Отправь мне сообщение или аудиофайл, и я обработаю его. "
        "Используй меню для доступа к основным функциям."
    , reply_markup=ReplyKeyboardRemove())

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    await get_cmd_status(message)

@dp.message(Command("models"))
async def cmd_models(message: types.Message):
    """Отображает список скачанных моделей Whisper"""
    try:
        models = list_downloaded_models()
        
        if not models:
            await message.answer("Нет скачанных моделей Whisper. Модели будут скачаны автоматически при первом использовании.")
            return
        
        # Формируем сообщение
        models_text = "📚 Скачанные модели Whisper:\n\n"
        
        # Группируем модели по имени для более наглядного отображения
        grouped_models = {}
        for model in models:
            name = model['name']
            if name not in grouped_models:
                grouped_models[name] = []
            grouped_models[name].append(model)
        
        # Формируем текст списка моделей
        for name, model_variants in grouped_models.items():
            # Выбираем самую большую/последнюю версию модели для отображения размера
            latest_model = max(model_variants, key=lambda m: m.get('file_size_mb', m.get('size_mb', 0)))
            size_mb = latest_model.get('size_mb', 0)
            
            if len(model_variants) > 1:
                # Если есть несколько вариантов одной модели
                variant_info = ", ".join([os.path.basename(m['path']) for m in model_variants])
                models_text += f"• {name} ({size_mb} MB) - {variant_info}\n"
            else:
                # Если только один вариант
                file_path = os.path.basename(latest_model['path'])
                models_text += f"• {name} ({size_mb} MB) - {file_path}\n"
        
        models_text += f"\nТекущая модель: {WHISPER_MODEL}"
        models_text += f"\nДиректория моделей: {WHISPER_MODELS_DIR}"
        
        await message.answer(models_text)
    except Exception as e:
        logger.exception(f"Ошибка при получении списка моделей: {e}")
        await message.answer(f"Произошла ошибка при получении списка моделей: {str(e)}")

@dp.message(Command("queue"))
async def cmd_queue(message: types.Message):
    """Отображает текущую очередь задач на обработку аудио"""
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    #if user_id not in superusers:
    #    await message.answer("⚠️ У вас нет прав для просмотра очереди задач.")
    #queue    return

    # Получаем текущую очередь задач
    queue_size = audio_service.audio_task_queue.qsize()

    if queue_size == 0 and not audio_service.active_transcriptions:
        await message.answer("🟢 Очередь пуста. Нет активных задач на обработке.")
        return

    # Формируем информацию о текущей очереди
    queue_info = f"📋 <b>Статус очереди обработки аудио:</b>\n\n"

    # Информация об активных задачах транскрибации
    if audio_service.active_transcriptions:
        queue_info += f"🔄 <b>Активные задачи ({len(audio_service.active_transcriptions)}):</b>\n"
        for user_id, (future, message_id, file_path) in audio_service.active_transcriptions.items():
            if future == "cancelled":
                status = "⏹ Отменяется"
            else:
                status = "▶️ Обрабатывается"

            file_name = os.path.basename(file_path)
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024) if os.path.exists(file_path) else 0

            queue_info += f"- Пользователь: <code>{user_id}</code>, {status}\n"
            queue_info += f"  Файл: {file_name} ({file_size_mb:.2f} МБ)\n"

        queue_info += "\n"

    # Получаем задачи из очереди (не удаляя их)
    if queue_size > 0:
        queue_info += f"⏳ <b>В очереди ожидания ({queue_size}):</b>\n"

        # Нельзя напрямую перебрать асинхронную очередь, создадим временный список для отображения
        queue_list = []
        unfinished = audio_service.audio_task_queue._unfinished_tasks

        # Если есть задачи, указываем их количество
        if unfinished > 0:
            queue_info += f"- Количество задач в очереди: {unfinished}\n"
        else:
            queue_info += "- Очередь пуста или невозможно получить детальную информацию\n"

    # Добавляем информацию о состоянии фоновых процессов
    queue_info += f"\n🖥 <b>Системная информация:</b>\n"
    queue_info += f"- Фоновый обработчик: {'Работает' if audio_service.background_worker_running else 'Остановлен'}\n"
    queue_info += f"- Рабочих потоков: {audio_service.thread_executor._max_workers}\n"

    # Отправляем информацию
    await message.answer(queue_info, parse_mode="HTML")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = """
🤖 <b>Возможности бота:</b>

• Отправьте текстовое сообщение для получения ответа от ChatGPT
• Отправьте аудиофайл (голосовое сообщение или аудио) для его транскрибации
• При транскрибации аудио вы получите текст и файл с транскрибацией
• Бот создает файл субтитров (SRT) с таймкодами для использования в видеоредакторах
• Лимит: 50 сообщений в сутки

<b>Команды бота:</b>
/start - Начать общение с ботом
/status - Проверить лимит сообщений
/models - Показать список моделей Whisper
/help - Показать эту справку
/cancel - Отменить текущую обработку аудио
/queue - Показать очередь задач

<b>Текущие настройки:</b>
• Используемая модель для транскрибации: <code>%s</code>
• Режим транскрибации: <code>%s</code>
"""
    transcribe_mode = "Локальная модель Whisper" if USE_LOCAL_WHISPER else "OpenAI API"
    await message.answer(help_text % (WHISPER_MODEL, transcribe_mode), parse_mode="HTML")

@dp.message(lambda message: message.text == "💬 Помощь")
async def button_help(message: types.Message):
    await cmd_help(message)

@dp.message(lambda message: message.text == "📊 Статус")
async def button_status(message: types.Message):
    await cmd_status(message)

@dp.message(lambda message: message.text == "🎤 Модели Whisper")
async def button_models(message: types.Message):
    await cmd_models(message)

@dp.message(lambda message: message.text == "ℹ️ О боте")
async def button_about(message: types.Message):
    about_text = """
<b>📱 ChatGPT Бот с транскрибацией</b>

Этот бот позволяет общаться с ChatGPT и транскрибировать аудиосообщения с помощью технологии Whisper.

<b>Технологии:</b>
• OpenAI ChatGPT для обработки текстовых запросов
• Whisper для транскрибации аудио

<b>Особенности:</b>
• Поддержка транскрибации голосовых сообщений
• Сохранение транскрибаций в текстовые файлы
• Создание файлов субтитров (SRT) по таймкодам
• Кеширование моделей между запусками
• Ограничение в 50 сообщений в сутки
"""
    await message.answer(about_text, parse_mode="HTML")

@dp.message(lambda message: message.text == "🔍 Очередь")
async def button_queue(message: types.Message):
    """Обработчик кнопки 'Очередь'"""
    # Используем тот же обработчик, что и для команды /queue
    await cmd_queue(message)


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    """Отменяет текущую обработку аудио для пользователя"""
    user_id = message.from_user.id
    
    if user_id not in audio_service.active_transcriptions:
        await message.answer("У вас нет активных задач обработки аудио.")
        return
    
    future, message_id, file_path = audio_service.active_transcriptions[user_id]
    
    # Если задача еще не начала выполняться (future - реальный объект Future)
    if future != "cancelled" and not isinstance(future, str):
        try:
            # Помечаем задачу как отмененную
            audio_service.active_transcriptions[user_id] = ("cancelled", message_id, file_path)
            
            # Пытаемся отправить уведомление об отмене
            try:
                await bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=message_id,
                    text="⏱ Отмена обработки аудио...\n\nПожалуйста, подождите."
                )
            except Exception as e:
                logger.exception(f"Ошибка при обновлении сообщения об отмене: {e}")
            
            # Удаляем временный файл
            if file_path and os.path.exists(file_path):
                try:
                    cleanup_temp_files(file_path)
                    logger.info(f"Временный файл {file_path} был удален при отмене задачи пользователем {user_id}")
                except Exception as e:
                    logger.exception(f"Ошибка при удалении временного файла при отмене: {e}")
            
            await message.answer("✅ Задача обработки аудио отменена.")
            logger.info(f"Пользователь {user_id} отменил обработку аудио")
        except Exception as e:
            await message.answer(f"Произошла ошибка при попытке отменить задачу: {str(e)}")
            logger.exception(f"Ошибка при отмене задачи: {e}")
    else:
        # Если задача уже отменена
        await message.answer("Задача уже отменена или находится в процессе отмены.")

@dp.message(lambda message: message.voice or message.audio)
async def handle_audio(message: types.Message):
    await audio_service.handle_audio(message)


@dp.message()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    
    if not check_message_limit(user_id):
        await message.answer("Вы достигли дневного лимита в 50 сообщений. Попробуйте завтра!")
        return
    
    # Отправляем сообщение о начале обработки
    processing_msg = await message.answer("Обрабатываю ваше сообщение...")
    
    try:
        # Инициализация клиента OpenAI
        client = OpenAI(api_key=env_config.get('OPEN_AI_TOKEN'),
                        max_retries=3,
                        timeout=30
                        )
        # Получаем ответ от ChatGPT
        response = client.chat.completions.create(
            model=env_config.get('MODEL'),
            messages=[
                {"role": "user", "content": message.text}
            ]
        )
        
        # Получаем текст ответа
        response_text = response.choices[0].message.content
        
        # Если ответ слишком длинный, разбиваем на части
        if len(response_text) > MAX_MESSAGE_LENGTH:
            chunks = audio_service.split_text_into_chunks(response_text)
            
            # Обновляем первое сообщение
            await processing_msg.edit_text(chunks[0])
            
            # Отправляем остальные части
            for chunk in chunks[1:]:
                await message.answer(chunk)
        else:
            # Отправляем ответ пользователю
            await processing_msg.edit_text(response_text)
        
    except Exception as e:
        logger.exception(f"Произошла ошибка при обработке сообщения: {e}")
        await processing_msg.edit_text(f"Произошла ошибка при обработке сообщения: {str(e)}")

# Функция для сохранения очереди в файл
async def save_queue_to_file():
    """
    Сохраняет текущую очередь задач и активные транскрибации в JSON файл
    """
    try:
        # Проверяем наличие директории queue, создаем если нет
        if not os.path.exists(QUEUE_DIR):
            os.makedirs(QUEUE_DIR, exist_ok=True)
            logger.info(f"Создана директория для сохранения очереди: {QUEUE_DIR}")
            
        # Получаем все элементы из очереди
        queue_items = []
        temp_queue = asyncio.Queue()
        
        # Сохраняем активные транскрибации
        active_items = []
        for user_id, (future, message_id, file_path) in audio_service.active_transcriptions.items():
            # Пропускаем отмененные задачи
            if future == "cancelled":
                continue
                
            try:
                # Поскольку future не может быть сериализован напрямую, 
                # сохраняем только метаданные задачи
                if os.path.exists(file_path):
                    serializable_active_item = {
                        "user_id": user_id,
                        "file_path": file_path,
                        "file_name": os.path.basename(file_path),
                        "message_id": message_id,
                        "chat_id": await get_chat_id_by_message_id(message_id, user_id),
                        "timestamp": time.time(),
                        "is_active": True  # Пометка, что это активная задача
                    }
                    active_items.append(serializable_active_item)
                    logger.info(f"Сохранена активная задача транскрибации: user_id={user_id}, file={os.path.basename(file_path)}")
            except Exception as e:
                logger.warning(f"Ошибка при сохранении активной задачи для пользователя {user_id}: {e}")
        
        # Сохраняем размер очереди
        queue_size = audio_service.audio_task_queue.qsize()
        if queue_size == 0 and not active_items:
            #logger.debug("Очередь пуста и нет активных задач, нечего сохранять")
            
            # Если файл существует - удаляем его
            if os.path.exists(QUEUE_SAVE_PATH):
                os.remove(QUEUE_SAVE_PATH)
                logger.info(f"Удален файл с сохраненной очередью: {QUEUE_SAVE_PATH}")
            return
            
        logger.info(f"Сохранение очереди заданий: {queue_size} в очереди, {len(active_items)} активных")
        
        # Извлекаем все элементы из очереди во временный список
        for _ in range(queue_size):
            try:
                item = audio_service.audio_task_queue.get_nowait()
                
                # Разбираем кортеж
                message, file_path, processing_msg, user_id, file_name = item
                
                # Сохраняем только необходимые данные, которые можно сериализовать
                if os.path.exists(file_path):
                    serializable_item = {
                        "user_id": user_id,
                        "file_path": file_path,
                        "file_name": file_name,
                        "message_id": processing_msg.message_id,
                        "chat_id": processing_msg.chat.id,
                        "timestamp": time.time(),
                        "is_active": False  # Пометка, что это задача в очереди
                    }
                    queue_items.append(serializable_item)
                    
                # Возвращаем элемент обратно в очередь
                temp_queue.put_nowait(item)
            except asyncio.QueueEmpty:
                break
        
        # Восстанавливаем основную очередь
        while not temp_queue.empty():
            audio_service.audio_task_queue.put_nowait(temp_queue.get_nowait())
        
        # Объединяем активные задачи и задачи в очереди
        all_items = active_items + queue_items
        
        # Сохраняем данные в файл
        if all_items:
            with open(QUEUE_SAVE_PATH, 'w', encoding='utf-8') as f:
                json.dump(all_items, f, ensure_ascii=False, indent=2)
            logger.info(f"Очередь заданий сохранена в файл: {QUEUE_SAVE_PATH}, "
                       f"элементов: {len(all_items)} (активных: {len(active_items)}, в очереди: {len(queue_items)})")
        else:
            logger.info("Нет заданий для сохранения")
            
            # Если файл существует - удаляем его
            if os.path.exists(QUEUE_SAVE_PATH):
                os.remove(QUEUE_SAVE_PATH)
                
    except Exception as e:
        logger.exception(f"Ошибка при сохранении очереди в файл: {e}")

# Вспомогательная функция для получения chat_id по message_id
async def get_chat_id_by_message_id(message_id, user_id):
    """
    Пытается получить chat_id по message_id для сохранения активной задачи
    """
    try:
        # В большинстве случаев chat_id совпадает с user_id для личных сообщений
        return user_id
    except Exception:
        # Если не удалось получить, используем user_id как fallback
        return user_id

# Функция для загрузки очереди из файла
async def load_queue_from_file():
    """
    Загружает сохраненную очередь задач из JSON файла
    """
    try:
        if not os.path.exists(QUEUE_SAVE_PATH):
            logger.info(f"Файл с сохраненной очередью не найден: {QUEUE_SAVE_PATH}")
            return 0
            
        with open(QUEUE_SAVE_PATH, 'r', encoding='utf-8') as f:
            saved_items = json.load(f)
            
        if not saved_items:
            logger.info("Сохраненная очередь пуста")
            return 0
            
        logger.info(f"Загружаем сохраненную очередь заданий, элементов: {len(saved_items)}")
        
        # Сортируем задания по timestamp (сначала старые)
        saved_items.sort(key=lambda x: x.get("timestamp", 0))
        
        # Проверяем каждый файл и добавляем его в очередь
        restored_count = 0
        for item in saved_items:
            user_id = item.get("user_id")
            file_path = item.get("file_path")
            file_name = item.get("file_name")
            message_id = item.get("message_id")
            chat_id = item.get("chat_id")
            is_active = item.get("is_active", False)
            
            # Проверяем существование файла
            if file_path and os.path.exists(file_path):
                try:
                    # Создаем объекты, необходимые для очереди
                    # Нам нужно загрузить сообщение из Telegram для ответа
                    try:
                        # Пробуем получить сообщение из Telegram
                        chat = types.Chat(id=chat_id, type="private")
                        
                        # Создаем фиктивный объект пользователя для восстановленных сообщений
                        from_user = types.User(
                            id=user_id,
                            is_bot=False,
                            first_name="Восстановлено",
                            last_name="",
                            username="restored_user",
                            language_code="ru"
                        )
                        
                        # Создаем объект сообщения с from_user
                        message = types.Message(
                            message_id=message_id, 
                            chat=chat, 
                            date=int(time.time()),
                            from_user=from_user
                        )
                        
                        # Разные сообщения для активных задач и задач в очереди
                        if is_active:
                            status_text = "⚠️ Активная задача транскрибации восстановлена после перезапуска и будет запущена заново..."
                        else:
                            status_text = "🔄 Задача восстановлена после перезапуска и добавлена в очередь..."
                            
                        try:
                            # Пробуем редактировать старое сообщение
                            processing_msg = await bot.edit_message_text(
                                status_text,
                                chat_id=chat_id,
                                message_id=message_id
                            )
                        except Exception as edit_error:
                            logger.warning(f"Не удалось отредактировать сообщение {message_id}: {edit_error}")
                            # Отправляем новое сообщение, если не получилось отредактировать старое
                            processing_msg = await bot.send_message(
                                chat_id=chat_id,
                                text=f"{status_text}\n(Оригинальное сообщение не найдено)"
                            )
                        
                        # Добавляем задание в очередь
                        await audio_service.audio_task_queue.put((message, file_path, processing_msg, user_id, file_name))
                        logger.info(f"Восстановлено задание: user_id={user_id}, file={file_name}, активное={is_active}")
                        restored_count += 1
                    except Exception as msg_error:
                        logger.warning(f"Не удалось восстановить сообщение для задания: {msg_error}")
                        continue
                        
                except Exception as e:
                    logger.warning(f"Ошибка при восстановлении задания {file_name}: {e}")
            else:
                logger.warning(f"Файл не найден при восстановлении очереди: {file_path}")
        
        # Удаляем файл сохранения, чтобы не восстанавливать дважды
        if os.path.exists(QUEUE_SAVE_PATH):
            os.remove(QUEUE_SAVE_PATH)
            
        logger.info(f"Восстановление очереди завершено. Восстановлено заданий: {restored_count} из {len(saved_items)}")
        return restored_count
        
    except Exception as e:
        logger.exception(f"Ошибка при загрузке очереди из файла: {e}")
        return 0

# Периодическое сохранение очереди
async def periodic_queue_save():
    """
    Периодически сохраняет очередь задач в файл
    """
    while True:
        try:
            await save_queue_to_file()
        except Exception as e:
            logger.error(f"Ошибка при периодическом сохранении очереди: {e}")
            
        # Сохраняем каждые 60 секунд
        await asyncio.sleep(60)

# Функция для обработки сигналов завершения
async def shutdown(signal, loop):
    """
    Корректное завершение при получении сигнала
    """
    logger.info(f"Получен сигнал {signal.name}, выполняем корректное завершение...")
    
    # Сохраняем очередь перед выключением
    await save_queue_to_file()
    
    # Останавливаем задачи
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    
    for task in tasks:
        task.cancel()
    
    logger.info(f"Отменено {len(tasks)} задач")
    await asyncio.gather(*tasks, return_exceptions=True)
    
    loop.stop()
    logger.info("Завершение работы бота")
    
async def register_shutdown_handler():
    """
    Регистрирует обработчик сигналов завершения
    """
    try:
        loop = asyncio.get_running_loop()
        
        # Регистрируем обработчики сигналов для SIGINT и SIGTERM
        for sig in [signal.SIGINT, signal.SIGTERM]:
            loop.add_signal_handler(
                sig, lambda s=sig: asyncio.create_task(shutdown(s, loop))
            )
        logger.info("Зарегистрированы обработчики сигналов завершения")
    except NotImplementedError:
        # Windows не поддерживает add_signal_handler
        logger.info("Регистрация обработчиков сигналов не поддерживается на этой платформе")
    except Exception as e:
        logger.error(f"Ошибка при регистрации обработчиков сигналов: {e}")

async def main():
    logger.info('Бот запущен.')
    try:
        logger.info(f'Используемая модель Whisper: {WHISPER_MODEL}')
        logger.info(f'Директория для моделей Whisper: {WHISPER_MODELS_DIR}')
        
        # Очищаем старые временные файлы при запуске
        cleanup_temp_files(older_than_hours=24)
        logger.info('Выполнена очистка старых временных файлов')
        
        # Проверяем и настраиваем доступ к файлам Telegram Bot API
        #await check_bot_api_files_access()
        #logger.info('Проверен доступ к файлам Telegram Bot API')
        
        # Восстанавливаем сохраненную очередь
        restored_count = await load_queue_from_file()
        if restored_count > 0:
            logger.info(f'Восстановлено {restored_count} заданий из сохраненной очереди')
        
        # Запускаем фоновый обработчик очереди
        background_task = asyncio.create_task(audio_service.background_audio_processor())
        logger.info('Запущен фоновый обработчик очереди аудиофайлов')
        
        # Запускаем периодическое сохранение очереди
        save_task = asyncio.create_task(periodic_queue_save())
        logger.info('Запущено периодическое сохранение очереди')
        
        # Регистрируем обработчики сигналов завершения
        await register_shutdown_handler()
        
        # Устанавливаем команды в меню бота
        await set_commands()
        logger.info('Команды бота установлены')

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        logger.info('Остановка фонового обработчика очереди...')
        # Даем время очереди завершить текущие задачи
        await asyncio.sleep(1)
    finally:
        await bot.session.close()
        logger.info('Бот остановлен.')

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Клавиатурное прерывание')
    except asyncio.CancelledError:
        logger.info('Прерывание')
    except Exception:
        logger.exception('Неизвестная ошибка')
