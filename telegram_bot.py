import openai
from dotenv import load_dotenv
import os
from datetime import datetime
import pytz
import uuid
import json
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Загрузка переменных из .env файла
load_dotenv()

# Устанавливаем API-ключ OpenAI
openai.api_key = os.getenv('OPENAI_API_KEY')

# Определение состояний для ConversationHandler
ASK_CITY = 1
POST_TIMEZONE_SET = 2
CHANGE_TIMEZONE = 3

# Хранилище задач: {user_id: {task_id: {'description': str, 'time': datetime, 'job': Job}}}
user_tasks = {}

# Функция для общения с GPT через новый API для получения часового пояса
def get_timezone_via_gpt(city, current_time):
    try:
        system_message = {
            "role": "system",
            "content": (
                "Ты помощник, который может определить часовой пояс по названию города. "
                "Название города может содержать опечатки или быть написано с ошибками. "
                f"Текущее время: {current_time.strftime('%Y-%m-%d %H:%M:%S')} (формат: YYYY-MM-DD HH:MM:SS). "
                f"Пользователь ввёл название города: {city}. "
                "Определи наиболее вероятный часовой пояс этого города и верни его в формате строки, например 'Europe/Moscow'. "
                "Если город не найден или не существует, верни 'Unknown'."
            )
        }
        user_message = {
            "role": "user",
            "content": city
        }
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[system_message, user_message],
            max_tokens=50,
            temperature=0.0
        )
        content = response['choices'][0]['message']['content'].strip()
        logger.info(f"GPT ответ на запрос часового пояса для города '{city}': '{content}'")

        # Убираем возможные кавычки и лишние символы
        timezone = content.strip('"').strip("'").strip()

        # Проверяем, что часовой пояс валиден
        if timezone in pytz.all_timezones:
            return timezone
        else:
            logger.warning(f"Получен неизвестный часовой пояс: '{timezone}'")
            return None
    except Exception as e:
        logger.error(f"Ошибка при получении часового пояса через GPT: {e}")
        return None

# Функция для общения с GPT через новый API для извлечения задачи и времени
def extract_task_and_time(prompt, current_time):
    try:
        system_message = {
            "role": "system",
            "content": (
                "Ты Telegram-бот, созданный Радомиром Брызгаловым. Твоя задача — извлечь из пользовательского сообщения описание задачи и время напоминания. "
                f"Текущее время пользователя: {current_time.strftime('%Y-%m-%d %H:%M:%S')} (формат: YYYY-MM-DD HH:MM:SS). "
                "Если время указано относительно текущего времени (например, 'через 5 мин'), рассчитай абсолютное время. "
                "Верни результат в формате JSON: {\"task\": \"описание задачи\", \"time\": \"время в формате YYYY-MM-DD HH:MM:SS\"}."
            )
        }
        user_message = {
            "role": "user",
            "content": prompt
        }
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[system_message, user_message],
            max_tokens=150,
            temperature=0.3
        )
        content = response['choices'][0]['message']['content'].strip()
        logger.info(f"GPT ответ на запрос задачи и времени: '{content}'")
        # Попытка парсинга JSON из ответа
        result = json.loads(content)
        return result.get('task'), result.get('time')
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка при парсинге JSON: {e}")
        logger.error(f"Не удалось распарсить содержимое: '{content}'")
        return None, None
    except Exception as e:
        logger.error(f"Ошибка при извлечении задачи и времени: {e}")
        return None, None

# Функция для отправки инструкций
async def send_instructions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    instructions = (
        "📋 <b>Инструкции по использованию бота:</b>\n\n"
        "1. <b>Добавление задачи:</b> Напишите любую задачу вместе со временем, когда нужно напомнить. Пример: 'Встреча с командой завтра в 15:00' или 'Пробежка через час'.\n"
        "2. <b>Просмотр задач:</b> Нажмите кнопку '📋 Просмотреть задачи', чтобы увидеть все свои запланированные задачи.\n"
        "3. <b>Удаление задачи:</b> Нажмите кнопку '🗑 Удалить задачу' и выберите задачу, которую хотите удалить.\n"
        "4. <b>Дополнительные настройки:</b> В разделе '➕ Ещё' вы можете изменить свой часовой пояс или снова просмотреть инструкции.\n\n"
    )
    await query.message.reply_text(
        instructions,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )

# Стартовая команда для бота
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = context.user_data

    # Проверяем, установлен ли уже часовой пояс
    if 'timezone' in user_data:
        welcome_message = (
            "Привет! Я Telegram-бот, созданный Радомиром Брызгаловым. "
            "Вы уже настроили свой часовой пояс. Выберите действие ниже."
        )
        await update.message.reply_text(welcome_message, reply_markup=main_menu())
        return ConversationHandler.END

    # Если часовой пояс не установлен, начинаем процесс установки
    welcome_message = (
        "Привет! Я Telegram-бот, созданный Радомиром Брызгаловым. "
        "Для корректного планирования задач, пожалуйста, укажите ваш город."
    )
    await update.message.reply_text(welcome_message)
    await update.message.reply_text("🌍 В каком городе вы находитесь?")
    return ASK_CITY

# Меню после установки часового пояса
def post_timezone_menu():
    keyboard = [
        [InlineKeyboardButton("📄 Инструкции", callback_data='instructions')],
        [InlineKeyboardButton("🚀 Начать сразу", callback_data='start_now')]
    ]
    return InlineKeyboardMarkup(keyboard)

# Меню основного интерфейса
def main_menu():
    keyboard = [
        [InlineKeyboardButton("📋 Просмотреть задачи", callback_data='view_tasks')],
        [InlineKeyboardButton("🗑 Удалить задачу", callback_data='delete_task')],
        [InlineKeyboardButton("➕ Ещё", callback_data='more')],
    ]
    return InlineKeyboardMarkup(keyboard)

# Меню "ещё"
def more_menu():
    keyboard = [
        [InlineKeyboardButton("📄 Инструкции", callback_data='instructions')],
        [InlineKeyboardButton("🔄 Изменить часовой пояс", callback_data='change_timezone')],
    ]
    return InlineKeyboardMarkup(keyboard)

# Обработка ответа пользователя на вопрос о городе
async def receive_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    city = update.message.text.strip()
    logger.info(f"Пользователь {user_id} указал город: {city}")

    # Текущее время в UTC для GPT
    now = datetime.now(pytz.utc)
    # Получаем часовой пояс через GPT
    timezone_str = get_timezone_via_gpt(city, now)

    if not timezone_str:
        await update.message.reply_text(
            "❌ Не удалось определить часовой пояс для указанного города. Пожалуйста, попробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data='retry_city')]
            ])
        )
        return ASK_CITY

    # Сохраняем часовой пояс в user_data
    previous_timezone = context.user_data.get('timezone')
    context.user_data['timezone'] = timezone_str
    logger.info(f"Пользователь {user_id} установлен в часовом поясе: {timezone_str}")

    # Если часовой пояс изменился, обновляем время задач
    if previous_timezone and previous_timezone != timezone_str:
        new_timezone = pytz.timezone(timezone_str)
        old_timezone = pytz.timezone(previous_timezone)
        tasks = user_tasks.get(user_id, {})
        for task in tasks.values():
            # Конвертируем время задачи из старого часового пояса в новый
            task_time_utc = task['time'].astimezone(pytz.utc)
            task['time'] = task_time_utc.astimezone(new_timezone)
            # Пересоздаём напоминание с новым временем
            current_jobs = context.job_queue.get_jobs_by_name(task['id'])
            for job in current_jobs:
                job.schedule_removal()
            context.job_queue.run_once(
                send_reminder,
                when=(task['time'] - datetime.now(new_timezone)).total_seconds(),
                data={'user_id': user_id, 'task_id': task['id']},
                name=task['id']
            )
        logger.info(f"Время задач пользователя {user_id} обновлено согласно новому часовому поясу.")

    # Подтверждение пользователю и выбор инструкций или начать сразу
    confirmation_message = (
        f"✅ Часовой пояс успешно установлен: {timezone_str}.\n"
        "Что вы хотите сделать дальше?"
    )
    await update.message.reply_text(
        confirmation_message,
        reply_markup=post_timezone_menu()
    )

    return POST_TIMEZONE_SET

# Обработка нажатия кнопки "Попробовать снова"
async def retry_city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🌍 Пожалуйста, введите название вашего города ещё раз.")
    return ASK_CITY

# Обработка нажатия кнопки "Начать сразу"
async def start_now_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "Вы готовы использовать бота! Добавляйте задачи, и я буду напоминать вам о них.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )
    return ConversationHandler.END

# Обработка нажатий на кнопки меню
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == 'view_tasks':
        tasks = user_tasks.get(user_id, {})
        if not tasks:
            await query.message.reply_text("📭 У вас нет запланированных задач.", reply_markup=main_menu())
            return

        # Сортировка задач по времени (от ближайших к самым дальним)
        sorted_tasks = sorted(tasks.values(), key=lambda x: x['time'])

        message = "📝 <b>Ваши запланированные задачи:</b>\n\n"
        for idx, task in enumerate(sorted_tasks, start=1):
            message += (
                f"{idx}. <b>{task['description']}</b> - <i>{task['time'].strftime('%Y-%m-%d %H:%M:%S')}</i>\n"
            )
        await query.message.reply_text(message, parse_mode=ParseMode.HTML, reply_markup=main_menu())

    elif query.data == 'delete_task':
        tasks = user_tasks.get(user_id, {})
        if not tasks:
            await query.message.reply_text("📭 У вас нет запланированных задач для удаления.", reply_markup=main_menu())
            return

        keyboard = []
        for task in tasks.values():
            button_text = f"🗑 {task['description']}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f'delete_{task["id"]}')])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Выберите задачу для удаления:", reply_markup=reply_markup)

    elif query.data == 'more':
        await query.message.reply_text("⚙️ <b>Дополнительные опции:</b>", parse_mode=ParseMode.HTML, reply_markup=more_menu())

    elif query.data == 'instructions':
        instructions = (
            "📋 <b>Инструкции по использованию бота:</b>\n\n"
            "1. <b>Добавление задачи:</b> Напишите любую задачу вместе со временем, когда нужно напомнить. Пример: 'Встреча с командой завтра в 15:00' или 'Пробежка через час'.\n"
            "2. <b>Просмотр задач:</b> Нажмите кнопку '📋 Просмотреть задачи', чтобы увидеть все свои запланированные задачи.\n"
            "3. <b>Удаление задачи:</b> Нажмите кнопку '🗑 Удалить задачу' и выберите задачу, которую хотите удалить.\n"
            "4. <b>Дополнительные настройки:</b> В разделе '➕ Ещё' вы можете изменить свой часовой пояс или снова просмотреть инструкции.\n\n"
        )
        await query.message.reply_text(
            instructions,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu()
        )

    elif query.data == 'start_now':
        await query.message.reply_text(
            "Вы готовы использовать бота! Добавляйте задачи, и я буду напоминать вам о них.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu()
        )

    elif query.data == 'change_timezone':
        await query.message.reply_text("🌍 В каком городе вы находитесь?")
        return CHANGE_TIMEZONE

    elif query.data.startswith('delete_'):
        task_id = query.data.split('_')[1]
        tasks = user_tasks.get(user_id, {})
        task = tasks.get(task_id)

        if task:
            # Запоминаем ID задачи для подтверждения
            context.user_data['delete_task_id'] = task_id

            confirmation_keyboard = [
                [InlineKeyboardButton("✅ Удалить", callback_data='confirm_delete')],
                [InlineKeyboardButton("❌ Отмена", callback_data='cancel_delete')]
            ]
            reply_markup = InlineKeyboardMarkup(confirmation_keyboard)

            await query.message.reply_text(
                f"Вы уверены, что хотите удалить задачу '{task['description']}'?",
                reply_markup=reply_markup
            )

            return  # Оставляем ConversationHandler активным
        else:
            await query.message.reply_text(
                "⚠ Задача не найдена или уже была удалена.",
                reply_markup=main_menu()
            )

    elif query.data == 'confirm_delete':
        task_id = context.user_data.get('delete_task_id')
        if not task_id:
            await query.message.reply_text(
                "⚠ Произошла ошибка. Пожалуйста, попробуйте снова.",
                reply_markup=main_menu()
            )
            return

        tasks = user_tasks.get(user_id, {})
        task = tasks.get(task_id)

        if task:
            # Удаление задачи из хранилища
            del tasks[task_id]

            # Удаление задачи из очереди
            current_jobs = context.job_queue.get_jobs_by_name(task_id)
            for job in current_jobs:
                job.schedule_removal()

            await query.message.reply_text(
                f"✅ Задача '{task['description']}' удалена.",
                reply_markup=main_menu()
            )
            logger.info(f"Задача {task_id} пользователя {user_id} удалена.")
        else:
            await query.message.reply_text(
                "⚠ Задача не найдена или уже была удалена.",
                reply_markup=main_menu()
            )

        # Очистка сохранённого ID задачи
        context.user_data.pop('delete_task_id', None)

    elif query.data == 'cancel_delete':
        await query.message.reply_text(
            "❌ Удаление задачи отменено.",
            reply_markup=main_menu()
        )
        # Очистка сохранённого ID задачи
        context.user_data.pop('delete_task_id', None)

    return ConversationHandler.END

# Обработка сообщений от пользователя (задачи)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message.text

    logger.info(f"Получено сообщение от пользователя {user_id}: {message}")

    # Проверяем, установлен ли часовой пояс
    user_data = context.user_data
    if 'timezone' not in user_data:
        await update.message.reply_text(
            "❌ Часовой пояс не установлен. Пожалуйста, начните сначала с команды /start.",
            reply_markup=main_menu()
        )
        return

    timezone_str = user_data['timezone']
    user_timezone = pytz.timezone(timezone_str)

    # Текущее время пользователя
    now = datetime.now(user_timezone)
    logger.info(f"Текущее время пользователя {user_id}: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    # Извлечение задачи и времени с помощью GPT
    task_description, task_time_str = extract_task_and_time(message, now)

    if not task_description or not task_time_str:
        await update.message.reply_text(
            "❌ Не удалось распознать задачу или время. Пожалуйста, попробуйте еще раз.",
            reply_markup=main_menu()
        )
        return

    # Парсинг времени
    try:
        task_time = datetime.strptime(task_time_str, '%Y-%m-%d %H:%M:%S')
        task_time = user_timezone.localize(task_time)
        logger.info(f"Распознанное время задачи: {task_time.strftime('%Y-%m-%d %H:%M:%S')}")
    except ValueError:
        await update.message.reply_text(
            "❌ Не удалось распознать формат времени. Пожалуйста, используйте формат YYYY-MM-DD HH:MM:SS.",
            reply_markup=main_menu()
        )
        return

    # Проверка, что время в будущем
    if task_time <= now:
        await update.message.reply_text(
            "⚠ Время должно быть в будущем. Пожалуйста, укажите корректное время.",
            reply_markup=main_menu()
        )
        return

    # Создание уникального ID задачи
    task_id = str(uuid.uuid4())[:8]

    # Создание задачи
    task = {
        'id': task_id,
        'description': task_description,
        'time': task_time
    }

    # Добавление задачи в хранилище
    if user_id not in user_tasks:
        user_tasks[user_id] = {}
    user_tasks[user_id][task_id] = task

    # Планирование напоминания
    context.job_queue.run_once(
        send_reminder,
        when=(task_time - now).total_seconds(),
        data={'user_id': user_id, 'task_id': task_id},
        name=task_id
    )

    # Подтверждение пользователю
    confirmation_message = (
        f"✅ <b>Задача добавлена!</b>\n\n"
        f"📝 <b>Задача:</b> {task_description}\n"
        f"🕒 <b>Время:</b> {task_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await update.message.reply_text(
        confirmation_message,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )

# Функция для отправки напоминания
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.data['user_id']
    task_id = job.data['task_id']

    task = user_tasks.get(user_id, {}).get(task_id)

    if task:
        reminder_message = (
            f"⏰ <b>Напоминание:</b>\n\n"
            f"📝 <b>Задача:</b> {task['description']}\n"
            f"🕒 <b>Время:</b> {task['time'].strftime('%Y-%m-%d %H:%M:%S')}"
        )
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=reminder_message,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Напоминание отправлено пользователю {user_id} для задачи {task_id}")
        except Exception as e:
            logger.error(f"Ошибка при отправке напоминания: {e}")

        # Удаление выполненной задачи
        del user_tasks[user_id][task_id]

# Функция для отмены текущей операции
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "❌ Операция отменена.",
            reply_markup=main_menu()
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            "❌ Операция отменена.",
            reply_markup=main_menu()
        )
    return ConversationHandler.END

# Функция для начала изменения часового пояса
async def start_change_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🌍 В каком городе вы находитесь?")
    return CHANGE_TIMEZONE

# Основной код для создания и запуска Telegram-бота
if __name__ == '__main__':
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TOKEN:
        logger.error("❌ Ошибка: TELEGRAM_TOKEN не установлен в .env файле.")
        exit(1)

    application = ApplicationBuilder().token(TOKEN).build()

    # Определение ConversationHandler для установки часового пояса при запуске
    conv_handler_setup_timezone = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            ASK_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_city),
                CallbackQueryHandler(retry_city_handler, pattern='^retry_city$')
            ],
            POST_TIMEZONE_SET: [
                CallbackQueryHandler(send_instructions, pattern='^instructions$'),
                CallbackQueryHandler(start_now_handler, pattern='^start_now$')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Определение ConversationHandler для изменения часового пояса через настройки
    conv_handler_change_timezone = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_change_timezone, pattern='^change_timezone$')],
        states={
            CHANGE_TIMEZONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_city),
                CallbackQueryHandler(retry_city_handler, pattern='^retry_city$')
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    # Добавление обработчиков
    application.add_handler(conv_handler_setup_timezone)
    application.add_handler(conv_handler_change_timezone)
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CommandHandler('cancel', cancel))  # Обработчик команды /cancel

    # Запуск бота
    logger.info("🚀 Бот запущен...")
    application.run_polling()
