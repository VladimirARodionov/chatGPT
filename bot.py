import logging.config
import asyncio
import pathlib
import os

from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from aiogram import types, Dispatcher
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeDefault, ReplyKeyboardRemove
from openai import OpenAI

from audio_service import background_worker_running, thread_executor, \
    handle_audio_service, \
    background_audio_processor, init_monitoring, cancel_audio_processing
from create_bot import env_config, bot, WHISPER_MODEL, WHISPER_MODELS_DIR, MAX_MESSAGE_LENGTH, \
    USE_LOCAL_WHISPER
from db_service import get_cmd_status, check_message_limit, get_all_from_queue, reset_active_tasks
from files_service import cleanup_temp_files, split_text_into_chunks
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
    BotCommand(command="cancel", description="Отменить все свои задачи на обработку аудио"),
    BotCommand(command="queue", description="Показать очередь задач"),
]


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

    # Получаем все задачи из очереди
    all_tasks = get_all_from_queue()

    if not all_tasks:
        await message.answer("🟢 Очередь пуста. Нет активных задач на обработке.")
        return

    # Формируем информацию о текущей очереди
    queue_info = f"📋 <b>Статус очереди обработки аудио:</b>\n\n"

    # Группируем задачи по активности
    active_tasks = [task for task in all_tasks if task.is_active]
    waiting_tasks = [task for task in all_tasks if not task.is_active]

    # Информация об активных задачах транскрибации
    if active_tasks:
        queue_info += f"🔄 <b>Активные задачи ({len(active_tasks)}):</b>\n"
        for task in active_tasks:
            file_name = task.file_name or os.path.basename(task.file_path)

            queue_info += f"- Пользователь: <code>{task.user_id}</code>{" (Я)" if user_id == task.user_id else ""}, ▶️ Обрабатывается\n"
            queue_info += f"  Файл: {file_name} ({task.file_size_mb:.2f} МБ)\n"

        queue_info += "\n"

    # Получаем задачи из очереди ожидания
    if waiting_tasks:
        queue_info += f"⏳ <b>В очереди ожидания ({len(waiting_tasks)}):</b>\n"
        
        for i, task in enumerate(waiting_tasks, 1):
            file_name = task.file_name or os.path.basename(task.file_path)

            queue_info += f"{i}. Пользователь: <code>{task.user_id}</code>{" (Я)" if user_id == task.user_id else ""}, Файл: {file_name} ({task.file_size_mb:.2f} МБ)\n"

    # Добавляем информацию о состоянии фоновых процессов
    queue_info += f"\n🖥 <b>Системная информация:</b>\n"
    queue_info += f"- Фоновый обработчик: {'Работает' if background_worker_running else 'Остановлен'}\n"
    queue_info += f"- Рабочих потоков: {thread_executor._max_workers}\n"

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
/cancel - Отменить все свои задачи на обработку аудио
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
    result, msg = cancel_audio_processing(user_id)
    await message.answer(msg)

@dp.message(lambda message: message.voice or message.audio)
async def handle_audio(message: types.Message):
    await handle_audio_service(message)


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
            chunks = split_text_into_chunks(response_text)
            
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

async def main():
    logger.info('Бот запущен.')
    try:
        logger.info(f'Используемая модель Whisper: {WHISPER_MODEL}')
        logger.info(f'Директория для моделей Whisper: {WHISPER_MODELS_DIR}')
        
        # Сбрасываем активные задачи из предыдущего запуска
        reset_count = reset_active_tasks()
        if reset_count > 0:
            logger.info(f'Сброшено {reset_count} активных задач из предыдущего запуска')
        
        # Очищаем старые временные файлы при запуске
        cleanup_temp_files(older_than_hours=24)
        logger.info('Выполнена очистка старых временных файлов')
        
        # Запускаем фоновый обработчик очереди
        background_task = asyncio.create_task(background_audio_processor())
        # Ждем 2 секунды, чтобы обработчик успел инициализироваться
        await asyncio.sleep(2)
        logger.info('Запущен фоновый обработчик очереди аудиофайлов')
        
        # Инициализируем мониторинг фонового обработчика
        init_monitoring()
        logger.info('Инициализирован мониторинг фонового обработчика')
        
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
