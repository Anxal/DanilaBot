import telebot
from telebot import types
import logging
import signal
import sys
from datetime import datetime, timedelta
import re
from typing import Dict, List, Tuple, Optional
import io
import sqlite3

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = "7653090953:AAE24Fq4Ftupk6JkZ_Tje5RQ0vpSANkyX1E"  # Замените на токен от @BotFather
ADMIN_IDS = [1150119683]  # Список ID администраторов, замените на свои
DATE_FORMAT = "%d.%m.%Y %H:%M"
WORKING_HOURS = {'start': 9, 'end': 18, 'interval': 1}
MESSAGES = {
    'welcome': "Привет, {name}! Я бот для записи на прием.",
    'enter_fullname': "Введите ваше ФИО:",
    'enter_vehicle': "Введите марку, модель и гос. номер автомобиля:",
    'enter_service': "Опишите требуемые услуги:",
    'enter_phone': "Введите номер телефона в формате +7XXXXXXXXXX\n(или /skip, если хотите использовать сохраненный номер):",
    'invalid_phone': "Неверный формат. Введите +7XXXXXXXXXX\n(или /skip для сохраненного номера):",
    'enter_datetime': "Выберите дату записи:",
    'select_time': "Выберите время записи:",
    'appointment_pending': "Ваша запись отправлена на рассмотрение администратору. Ожидайте ответа.",
    'appointment_approved': "Ваша запись на {} одобрена!",
    'appointment_rejected': "Ваша запись на {} отклонена администратором.",
    'no_appointments': "У вас нет записей.",
    'system_error': "Произошла ошибка. Попробуйте позже.",
    'blocked_slots_updated': "Часы записи обновлены.",
    'admin_added': "Администратор с ID {} добавлен.",
    'date_added': "Дата {} добавлена для записи.",
    'date_removed': "Дата {} удалена из записи.",
    'appointment_deleted': "Запись на {} для {} успешно удалена",
    'appointment_deleted_user': "Ваша запись на {} была удалена администратором."
}

# Класс для управления пользовательскими данными
class UserData:
    def __init__(self):
        self.data: Dict[int, dict] = {}
        self.last_access: Dict[int, datetime] = {}

    def set(self, user_id: int, key: str, value: any) -> None:
        self.data.setdefault(user_id, {})[key] = value
        self.last_access[user_id] = datetime.now()

    def get(self, user_id: int) -> dict:
        if user_id in self.data:
            self.last_access[user_id] = datetime.now()
            return self.data[user_id]
        return {}

    def cleanup_old_data(self, timeout_minutes: int = 30) -> None:
        now = datetime.now()
        expired = [uid for uid, last in self.last_access.items() if (now - last).total_seconds() > timeout_minutes * 60]
        for uid in expired:
            self.data.pop(uid, None)
            self.last_access.pop(uid, None)

    def delete(self, user_id: int) -> None:
        self.data.pop(user_id, None)
        self.last_access.pop(user_id, None)

# Класс базы данных
class Database:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()

    def is_connected(self) -> bool:
        try:
            self.cursor.execute("SELECT 1")
            return True
        except:
            return False

    def create_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                phone_number TEXT,
                vehicle_info TEXT
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                vehicle_info TEXT,
                service_type TEXT,
                phone_number TEXT,
                appointment_time TEXT,
                status TEXT DEFAULT 'pending_approval'
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                vehicle_info TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS blocked_slots (
                slot_time TEXT PRIMARY KEY
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS available_dates (
                date TEXT PRIMARY KEY
            )
        ''')
        self.conn.commit()

    def get_user_data(self, user_id: int) -> Optional[Dict]:
        self.cursor.execute("SELECT full_name, phone_number, vehicle_info FROM users WHERE user_id = ?", (user_id,))
        result = self.cursor.fetchone()
        return {'full_name': result[0], 'phone_number': result[1], 'vehicle_info': result[2]} if result else None

    def get_user_vehicles(self, user_id: int) -> List[Tuple[int, str, int]]:
        self.cursor.execute("SELECT id, vehicle_info, is_active FROM vehicles WHERE user_id = ?", (user_id,))
        return self.cursor.fetchall()

    def check_slot_available(self, datetime_str: str) -> bool:
        self.cursor.execute(
            "SELECT COUNT(*) FROM appointments WHERE appointment_time = ? AND status IN ('pending', 'approved')",
            (datetime_str,))
        booked = self.cursor.fetchone()[0] > 0
        self.cursor.execute("SELECT COUNT(*) FROM blocked_slots WHERE slot_time = ?", (datetime_str,))
        blocked = self.cursor.fetchone()[0] > 0
        return not (booked or blocked)

    def block_slot(self, slot_time: str) -> bool:
        try:
            self.cursor.execute("INSERT OR IGNORE INTO blocked_slots (slot_time) VALUES (?)", (slot_time,))
            self.conn.commit()
            return True
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error blocking slot: {e}")
            return False

    def unblock_slot(self, slot_time: str) -> bool:
        try:
            self.cursor.execute("DELETE FROM blocked_slots WHERE slot_time = ?", (slot_time,))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error unblocking slot: {e}")
            return False

    def get_blocked_slots(self) -> List[str]:
        self.cursor.execute("SELECT slot_time FROM blocked_slots")
        return [row[0] for row in self.cursor.fetchall()]

    def add_available_date(self, date: str) -> bool:
        try:
            self.cursor.execute("INSERT OR IGNORE INTO available_dates (date) VALUES (?)", (date,))
            self.conn.commit()
            return True
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error adding available date: {e}")
            return False

    def remove_available_date(self, date: str) -> bool:
        try:
            self.cursor.execute("DELETE FROM available_dates WHERE date = ?", (date,))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error removing available date: {e}")
            return False

    def get_available_dates(self) -> List[str]:
        self.cursor.execute("SELECT date FROM available_dates")
        return [row[0] for row in self.cursor.fetchall()]

    def update_user_data(self, user_id: int, full_name: str, phone_number: str, vehicle_info: str) -> bool:
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO users (user_id, full_name, phone_number, vehicle_info)
                VALUES (?, ?, ?, ?)
            """, (user_id, full_name, phone_number, vehicle_info))
            self.conn.commit()
            return True
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error updating user data: {e}")
            return False

    def add_pending_appointment(self, user_id: int, username: str, client_data: Dict, datetime_str: str) -> int:
        try:
            self.cursor.execute("""
                INSERT INTO appointments (user_id, username, full_name, vehicle_info, service_type, phone_number, appointment_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_approval')
            """, (user_id, username, client_data['full_name'], client_data['vehicle_info'],
                  client_data['service_type'], client_data['phone_number'], datetime_str))
            appointment_id = self.cursor.lastrowid
            self.update_user_data(user_id, client_data['full_name'], client_data['phone_number'],
                                  client_data['vehicle_info'])
            self.cursor.execute("INSERT INTO vehicles (user_id, vehicle_info) VALUES (?, ?)",
                                (user_id, client_data['vehicle_info']))
            self.conn.commit()
            return appointment_id
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error adding pending appointment: {e}")
            return -1

    def approve_appointment(self, appointment_id: int) -> bool:
        try:
            self.cursor.execute("UPDATE appointments SET status = 'approved' WHERE id = ?", (appointment_id,))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error approving appointment: {e}")
            return False

    def reject_appointment(self, appointment_id: int) -> bool:
        try:
            self.cursor.execute("UPDATE appointments SET status = 'rejected' WHERE id = ?", (appointment_id,))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error rejecting appointment: {e}")
            return False

    def get_user_appointments(self, user_id: int) -> List[Tuple]:
        self.cursor.execute(
            "SELECT id, appointment_time, full_name, vehicle_info, service_type, status FROM appointments WHERE user_id = ?",
            (user_id,))
        return self.cursor.fetchall()

    def get_all_appointments(self) -> List[Tuple]:
        self.cursor.execute("SELECT * FROM appointments")
        return self.cursor.fetchall()

    def get_appointment_by_id(self, appointment_id: int) -> Tuple:
        self.cursor.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
        return self.cursor.fetchone()

    def cancel_user_appointment(self, appointment_id: int, user_id: int) -> bool:
        try:
            self.cursor.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ? AND user_id = ?",
                                (appointment_id, user_id))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except:
            self.conn.rollback()
            return False

    def delete_appointment(self, appointment_id: int) -> bool:
        try:
            self.cursor.execute("DELETE FROM appointments WHERE id = ?", (appointment_id,))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Error deleting appointment: {e}")
            return False

    def commit_and_close(self):
        self.conn.commit()
        self.conn.close()

# Глобальные переменные
user_data = UserData()
bot = None
db = None

# Обработчик сигналов
def signal_handler(signum, frame):
    logger.info("Received shutdown signal, cleaning up...")
    try:
        if db and db.is_connected():
            db.commit_and_close()
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")
    logger.info("Cleanup completed, shutting down")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Инициализация бота и БД
try:
    logger.info("Initializing bot and database...")
    bot = telebot.TeleBot(BOT_TOKEN)
    db = Database('appointments.db')
    if not db.is_connected():
        raise Exception("Database connection failed")
    logger.info("Bot and database initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize bot or database: {e}")
    raise

# Создание клавиатуры главного меню
def create_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("📅 Записать на прием"))
    markup.row(types.KeyboardButton("📋 Мои записи"), types.KeyboardButton("❌ Отменить запись"))
    markup.row(types.KeyboardButton("👤 Мой профиль"))
    return markup

# Обработка команды /start
@bot.message_handler(commands=['start'])
def send_welcome(message):
    try:
        user_id = message.from_user.id
        logger.info(f"Processing /start command for user {user_id}")
        markup = create_markup()
        name = message.from_user.first_name or "гость"
        welcome_message = MESSAGES['welcome'].format(name=name)
        bot.send_message(message.chat.id, welcome_message, reply_markup=markup)
        logger.info(f"Welcome message sent to user {user_id}")
    except Exception as e:
        logger.error(f"Error in send_welcome: {e}")
        bot.send_message(message.chat.id, "Произошла ошибка. Попробуйте позже.", reply_markup=create_markup())

# Генерация клавиатуры с датами
def generate_dates_keyboard():
    markup = types.InlineKeyboardMarkup()
    available_dates = db.get_available_dates()
    if not available_dates:
        markup.add(types.InlineKeyboardButton("Нет доступных дат", callback_data="no_dates"))
    else:
        weekday_names = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
        for date_str in available_dates:
            date = datetime.strptime(date_str, "%d.%m.%Y")
            weekday = weekday_names[date.weekday()]
            button_text = f"{date_str} ({weekday})"
            markup.add(types.InlineKeyboardButton(text=button_text, callback_data=f"date_{date_str}"))
    return markup

    def cleanup_old_appointments(self, days_threshold: int = 1) -> int:
        """Удаляет записи старше указанного количества дней"""
        try:
            # Получаем текущую дату
            current_date = datetime.now()
            # Вычисляем дату, старше которой записи нужно удалить
            threshold_date = (current_date - timedelta(days=days_threshold)).strftime("%d.%m.%Y")
            
            # Запрос на удаление записей
            self.cursor.execute("""
                DELETE FROM appointments 
                WHERE strftime('%d.%m.%Y', substr(appointment_time, 1, 10)) < ?
            """, (threshold_date,))
            
            # Сохраняем количество удаленных записей
            deleted_count = self.cursor.rowcount
            self.conn.commit()
            
            logger.info(f"Удалено {deleted_count} устаревших записей (старше {days_threshold} дней)")
            return deleted_count
        except Exception as e:
            self.conn.rollback()
            logger.error(f"Ошибка при удалении старых записей: {e}")
            return 0


# Генерация клавиатуры со временем
def generate_time_keyboard(selected_date: str):
    markup = types.InlineKeyboardMarkup(row_width=3)
    start_hour = WORKING_HOURS['start']
    end_hour = WORKING_HOURS['end']
    interval = WORKING_HOURS['interval']
    time_buttons = []
    for hour in range(start_hour, end_hour, interval):
        time_str = f"{hour:02d}:00"
        full_datetime = f"{selected_date} {time_str}"
        if db.check_slot_available(full_datetime):
            time_buttons.append(types.InlineKeyboardButton(text=time_str, callback_data=f"time_{full_datetime}"))
    for i in range(0, len(time_buttons), 3):
        markup.add(*time_buttons[i:i + 3])
    markup.add(types.InlineKeyboardButton(text="← Назад", callback_data="back_to_dates"))
    return markup

# Обработка кнопки "Записать на прием"
@bot.message_handler(func=lambda message: message.text == "📅 Записать на прием")
def start_appointment(message):
    logger.info(f"User {message.from_user.id} starting appointment process")
    user_id = message.from_user.id
    user_data.cleanup_old_data()
    user_saved_data = db.get_user_data(user_id)

    if user_saved_data:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Использовать прошлые данные", callback_data="use_saved"))
        markup.add(types.InlineKeyboardButton("Обновить данные", callback_data="update_data"))
        markup.add(types.InlineKeyboardButton("Ввести новые данные", callback_data="enter_new"))
        response = (
            f"Мы помним вас, ваш Telegram ID: {user_id}\n"
            f"Ваши прошлые данные:\n"
            f"👤 ФИО: {user_saved_data['full_name']}\n"
            f"📱 Телефон: {user_saved_data['phone_number']}\n"
        )
        if user_saved_data['vehicle_info']:
            response += f"🚗 Автомобиль: {user_saved_data['vehicle_info']}\n"
        response += "\nЧто хотите сделать?"
        bot.send_message(message.chat.id, response, reply_markup=markup)
    else:
        user_data.set(user_id, 'step', 'fullname')
        bot.send_message(message.chat.id, f"Ваш Telegram ID: {user_id}\n" + MESSAGES['enter_fullname'])
        bot.register_next_step_handler(message, process_fullname)

# Обработка выбора действия
@bot.callback_query_handler(func=lambda call: call.data in ["use_saved", "update_data", "enter_new"])
def handle_data_choice(call):
    user_id = call.from_user.id
    user_saved_data = db.get_user_data(user_id)

    if call.data == "use_saved":
        if not user_saved_data:
            bot.answer_callback_query(call.id, "Ошибка получения данных")
            return
        user_data.set(user_id, 'full_name', user_saved_data['full_name'])
        user_data.set(user_id, 'phone_number', user_saved_data['phone_number'])
        user_data.set(user_id, 'vehicle_info', user_saved_data['vehicle_info'])
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("Продолжить с этими данными", callback_data="proceed"))
        markup.add(types.InlineKeyboardButton("Изменить автомобиль", callback_data="car_new"))
        bot.edit_message_text("Продолжить с текущими данными или изменить автомобиль?", call.message.chat.id,
                              call.message.message_id, reply_markup=markup)

    elif call.data == "update_data":
        user_data.set(user_id, 'step', 'fullname')
        user_data.set(user_id, 'old_data', user_saved_data)
        bot.edit_message_text(f"Обновите ФИО (было: {user_saved_data['full_name']}):", call.message.chat.id,
                              call.message.message_id)
        bot.register_next_step_handler(call.message, process_fullname_update)

    elif call.data == "enter_new":
        user_data.set(user_id, 'step', 'fullname')
        bot.edit_message_text(MESSAGES['enter_fullname'], call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(call.message, process_fullname)

# Обработка продолжения с текущими данными
@bot.callback_query_handler(func=lambda call: call.data == "proceed")
def proceed_with_data(call):
    user_id = call.from_user.id
    bot.edit_message_text(MESSAGES['enter_service'], call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(call.message, process_service)

# Обработка выбора автомобиля
@bot.callback_query_handler(func=lambda call: call.data.startswith('car_'))
def handle_car_choice(call):
    user_id = call.from_user.id
    choice = call.data.split('_')[1]
    user_saved_data = db.get_user_data(user_id)

    if not user_saved_data:
        bot.answer_callback_query(call.id, "Ошибка получения данных")
        return

    user_data.set(user_id, 'full_name', user_saved_data['full_name'])
    user_data.set(user_id, 'phone_number', user_saved_data['phone_number'])

    if choice == 'new':
        bot.edit_message_text(MESSAGES['enter_vehicle'], call.message.chat.id, call.message.message_id)
        bot.register_next_step_handler(call.message, process_vehicle)
        return

    user_data.set(user_id, 'vehicle_info', user_saved_data['vehicle_info'])
    bot.edit_message_text(MESSAGES['enter_service'], call.message.chat.id, call.message.message_id)
    bot.register_next_step_handler(call.message, process_service)

# Обработка ввода ФИО
def process_fullname(message):
    if message.text.lower() == "отмена":
        user_data.delete(message.from_user.id)
        bot.send_message(message.chat.id, "Ввод отменен", reply_markup=create_markup())
        return
    text = sanitize_input(message.text)
    if not re.match(r'^[А-Яа-яЁё\s-]{5,100}$', text) or len(text.split()) < 2:
        bot.send_message(message.chat.id, "Введите корректное ФИО (только русские буквы, минимум 2 слова)")
        bot.register_next_step_handler(message, process_fullname)
        return
    user_data.set(message.from_user.id, 'full_name', text)
    bot.send_message(message.chat.id, MESSAGES['enter_vehicle'])
    bot.register_next_step_handler(message, process_vehicle)

# Обновление ФИО
def process_fullname_update(message):
    if message.text.lower() == "отмена":
        user_data.delete(message.from_user.id)
        bot.send_message(message.chat.id, "Ввод отменен", reply_markup=create_markup())
        return
    text = sanitize_input(message.text)
    if not re.match(r'^[А-Яа-яЁё\s-]{5,100}$', text) or len(text.split()) < 2:
        bot.send_message(message.chat.id, "Введите корректное ФИО (только русские буквы, минимум 2 слова)")
        bot.register_next_step_handler(message, process_fullname_update)
        return
    user_id = message.from_user.id
    user_data.set(user_id, 'full_name', text)
    old_data = user_data.get(user_id).get('old_data', {})
    bot.send_message(message.chat.id, f"Обновите телефон (было: {old_data.get('phone_number', 'не указан')}):")
    bot.register_next_step_handler(message, process_phone_update)

# Обработка ввода автомобиля
def process_vehicle(message):
    if message.text.lower() == "отмена":
        user_data.delete(message.from_user.id)
        bot.send_message(message.chat.id, "Ввод отменен", reply_markup=create_markup())
        return
    text = sanitize_input(message.text)
    if len(text.split()) < 2 or len(text) < 5:
        bot.send_message(message.chat.id, "Введите марку, модель и гос. номер автомобиля")
        bot.register_next_step_handler(message, process_vehicle)
        return
    user_data.set(message.from_user.id, 'vehicle_info', text)
    bot.send_message(message.chat.id, MESSAGES['enter_service'])
    bot.register_next_step_handler(message, process_service)

# Обновление автомобиля
def process_vehicle_update(message):
    if message.text.lower() == "отмена":
        user_data.delete(message.from_user.id)
        bot.send_message(message.chat.id, "Ввод отменен", reply_markup=create_markup())
        return
    text = sanitize_input(message.text)
    if len(text.split()) < 2 or len(text) < 5:
        bot.send_message(message.chat.id, "Введите марку, модель и гос. номер автомобиля")
        bot.register_next_step_handler(message, process_vehicle_update)
        return
    user_id = message.from_user.id
    user_data.set(user_id, 'vehicle_info', text)
    bot.send_message(message.chat.id, MESSAGES['enter_service'])
    bot.register_next_step_handler(message, process_service)

# Обработка ввода услуг
def process_service(message):
    if message.text.lower() == "отмена":
        user_data.delete(message.from_user.id)
        bot.send_message(message.chat.id, "Ввод отменен", reply_markup=create_markup())
        return
    if len(message.text.strip()) < 3:
        bot.send_message(message.chat.id, "Опишите требуемые услуги подробнее")
        bot.register_next_step_handler(message, process_service)
        return
    user_id = message.from_user.id
    user_data.set(user_id, 'service_type', message.text)

    saved_data = db.get_user_data(user_id)
    if saved_data and saved_data['phone_number']:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(types.KeyboardButton(saved_data['phone_number']))
        markup.add(types.KeyboardButton("Изменить номер"))
        bot.send_message(message.chat.id, f"Ваш сохраненный номер: {saved_data['phone_number']}. Использовать его?",
                         reply_markup=markup)
        bot.register_next_step_handler(message, process_phone_choice)
    else:
        bot.send_message(message.chat.id, MESSAGES['enter_phone'], reply_markup=None)
        bot.register_next_step_handler(message, process_phone)

# Выбор использования сохраненного номера
def process_phone_choice(message):
    user_id = message.from_user.id
    saved_data = db.get_user_data(user_id)

    if message.text.lower() == "отмена":
        user_data.delete(user_id)
        bot.send_message(message.chat.id, "Ввод отменен", reply_markup=create_markup())
        return
    elif message.text.lower() in ["изменить номер", "изменить"]:

# Запускаем автоматическую чистку старых записей при старте бота
def setup_automatic_cleanup():
    try:
        deleted_count = db.cleanup_old_appointments(1)  # Удаляем записи старше 1 дня
        logger.info(f"Автоматическая чистка завершена: удалено {deleted_count} устаревших записей")
    except Exception as e:
        logger.error(f"Ошибка при автоматической чистке: {e}")

# Запуск бота
if __name__ == "__main__":
    logger.info("Bot starting...")
    try:
        # Запускаем автоматическую чистку при старте
        setup_automatic_cleanup()
        
        bot.remove_webhook()
        updates = bot.get_updates(offset=-1, timeout=1)
        if updates:
            bot.get_updates(offset=updates[-1].update_id + 1)
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        if db and db.is_connected():
            db.commit_and_close()
        raise
    finally:
        if db and db.is_connected():
            db.commit_and_close()

        bot.send_message(message.chat.id, MESSAGES['enter_phone'], reply_markup=None)
        bot.register_next_step_handler(message, process_phone)
    elif is_valid_phone(message.text) or message.text == saved_data['phone_number']:
        user_data.set(user_id, 'phone_number',
                      message.text if is_valid_phone(message.text) else saved_data['phone_number'])
        bot.send_message(message.chat.id, MESSAGES['enter_datetime'], reply_markup=generate_dates_keyboard())
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add(types.KeyboardButton(saved_data['phone_number']))
        markup.add(types.KeyboardButton("Изменить номер"))
        bot.send_message(message.chat.id, "Пожалуйста, выберите один из вариантов или введите корректный номер:",
                         reply_markup=markup)
        bot.register_next_step_handler(message, process_phone_choice)

# Валидация телефона
def is_valid_phone(phone: str) -> bool:
    return bool(re.match(r'^\+7\d{10}$', phone.strip()))

# Обработка ввода телефона
def process_phone(message):
    user_id = message.from_user.id
    if message.text.lower() == "отмена":
        user_data.delete(user_id)
        bot.send_message(message.chat.id, "Ввод отменен", reply_markup=create_markup())
        return
    if message.text.lower() == "/skip":
        saved_data = db.get_user_data(user_id)
        if saved_data and saved_data['phone_number']:
            user_data.set(user_id, 'phone_number', saved_data['phone_number'])
            bot.send_message(message.chat.id, MESSAGES['enter_datetime'], reply_markup=generate_dates_keyboard())
            return
        else:
            bot.send_message(message.chat.id, "У вас нет сохраненного номера. Введите его:")
            bot.register_next_step_handler(message, process_phone)
            return
    if not is_valid_phone(message.text):
        bot.send_message(message.chat.id, MESSAGES['invalid_phone'])
        bot.register_next_step_handler(message, process_phone)
        return
    user_data.set(user_id, 'phone_number', message.text)
    bot.send_message(message.chat.id, MESSAGES['enter_datetime'], reply_markup=generate_dates_keyboard())

# Обновление телефона
def process_phone_update(message):
    if message.text.lower() == "отмена":
        user_data.delete(message.from_user.id)
        bot.send_message(message.chat.id, "Ввод отменен", reply_markup=create_markup())
        return
    if not is_valid_phone(message.text):
        bot.send_message(message.chat.id, MESSAGES['invalid_phone'])
        bot.register_next_step_handler(message, process_phone_update)
        return
    user_id = message.from_user.id
    user_data.set(user_id, 'phone_number', message.text)
    old_data = user_data.get(user_id).get('old_data', {})
    bot.send_message(message.chat.id, f"Обновите автомобиль (было: {old_data.get('vehicle_info', 'не указан')}):")
    bot.register_next_step_handler(message, process_vehicle_update)

# Обработка выбора даты
@bot.callback_query_handler(func=lambda call: call.data.startswith('date_'))
def process_date_selection(call):
    try:
        selected_date = call.data.replace('date_', '')
        logger.info(f"User {call.from_user.id} selected date: {selected_date}")
        markup = generate_time_keyboard(selected_date)
        bot.edit_message_text(MESSAGES['select_time'], call.message.chat.id, call.message.message_id,
                              reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Error processing date selection: {e}")
        bot.edit_message_text(f"Произошла ошибка: {str(e)}", call.message.chat.id, call.message.message_id)

# Обработка выбора времени
@bot.callback_query_handler(func=lambda call: call.data.startswith('time_'))
def process_time_selection(call):
    try:
        full_datetime = call.data.replace('time_', '')
        user_id = call.from_user.id
        client_data = user_data.get(user_id)

        if not client_data:
            bot.edit_message_text("Сессия истекла. Начните заново.", call.message.chat.id, call.message.message_id)
            return

        if not db.check_slot_available(full_datetime):
            markup = generate_time_keyboard(full_datetime.split()[0])
            bot.edit_message_text("Это время занято или заблокировано. Выберите другое:", call.message.chat.id,
                                  call.message.message_id, reply_markup=markup)
            return

        appointment_id = db.add_pending_appointment(user_id, call.from_user.username, client_data, full_datetime)
        if appointment_id == -1:
            bot.answer_callback_query(call.id, "Ошибка при создании записи")
            return

        bot.edit_message_text(MESSAGES['appointment_pending'], call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Заявка отправлена")
        user_data.delete(user_id)

        admin_message = (
            f"Новая запись от пользователя @{call.from_user.username} (ID: {user_id}):\n"
            f"📅 Дата: {full_datetime}\n"
            f"👤 ФИО: {client_data['full_name']}\n"
            f"🚗 Автомобиль: {client_data['vehicle_info']}\n"
            f"🔧 Услуги: {client_data['service_type']}\n"
            f"📱 Телефон: {client_data['phone_number']}"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("Одобрить", callback_data=f"approve_{appointment_id}"),
            types.InlineKeyboardButton("Отклонить", callback_data=f"reject_{appointment_id}")
        )
        for admin_id in ADMIN_IDS:
            bot.send_message(admin_id, admin_message, reply_markup=markup)

    except Exception as e:
        logger.error(f"Error in time selection: {e}")
        bot.edit_message_text(f"Произошла ошибка: {str(e)}", call.message.chat.id, call.message.message_id)

# Обработка решения администратора
@bot.callback_query_handler(func=lambda call: call.data.startswith('approve_') or call.data.startswith('reject_'))
def process_admin_decision(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "У вас нет прав доступа.")
        return
    try:
        action, appointment_id = call.data.split('_')
        appointment_id = int(appointment_id)
        appointment = db.get_appointment_by_id(appointment_id)

        if not appointment:
            bot.answer_callback_query(call.id, "Запись не найдена")
            return

        user_id = appointment[1]
        appointment_time = appointment[7]

        if action == "approve":
            if db.approve_appointment(appointment_id):
                bot.edit_message_text(f"Запись на {appointment_time} одобрена", call.message.chat.id,
                                      call.message.message_id)
                bot.send_message(user_id, MESSAGES['appointment_approved'].format(appointment_time))
                bot.answer_callback_query(call.id, "Запись одобрена")
            else:
                bot.answer_callback_query(call.id, "Ошибка при одобрении")
        elif action == "reject":
            if db.reject_appointment(appointment_id):
                bot.edit_message_text(f"Запись на {appointment_time} отклонена", call.message.chat.id,
                                      call.message.message_id)
                bot.send_message(user_id, MESSAGES['appointment_rejected'].format(appointment_time))
                bot.answer_callback_query(call.id, "Запись отклонена")
            else:
                bot.answer_callback_query(call.id, "Ошибка при отклонении")
    except Exception as e:
        logger.error(f"Error processing admin decision: {e}")
        bot.answer_callback_query(call.id, "Произошла ошибка")

# Обработка возврата к выбору даты
@bot.callback_query_handler(func=lambda call: call.data == 'back_to_dates')
def back_to_dates(call):
    try:
        markup = generate_dates_keyboard()
        bot.edit_message_text(MESSAGES['enter_datetime'], call.message.chat.id, call.message.message_id,
                              reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Error returning to dates: {e}")
        bot.edit_message_text(f"Произошла ошибка: {str(e)}", call.message.chat.id, call.message.message_id)

# Обработка отмены записи
@bot.message_handler(func=lambda message: message.text == "❌ Отменить запись")
def cancel_user_appointment_start(message):
    try:
        appointments = db.get_user_appointments(message.from_user.id)
        if not appointments:
            bot.reply_to(message, MESSAGES['no_appointments'])
            return
        markup = types.InlineKeyboardMarkup()
        for appointment_id, appointment_time, status in appointments:
            if status in ['pending_approval', 'approved']:
                markup.add(types.InlineKeyboardButton(
                    text=f"Отменить запись на {appointment_time}",
                    callback_data=f"cancel_{appointment_id}"
                ))
        bot.reply_to(message, "Выберите запись для отмены:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Error in cancel appointment: {e}")
        bot.reply_to(message, MESSAGES['system_error'])

# Подтверждение отмены
@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_'))
def process_cancellation(call):
    try:
        appointment_id = int(call.data.split('_')[1])
        if db.cancel_user_appointment(appointment_id, call.from_user.id):
            bot.edit_message_text("Запись успешно отменена", call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, "Запись отменена")
        else:
            bot.answer_callback_query(call.id, "Не удалось отменить запись")
    except Exception as e:
        logger.error(f"Error processing cancellation: {e}")
        bot.edit_message_text(f"Произошла ошибка: {str(e)}", call.message.chat.id, call.message.message_id)

# Показать мои записи
@bot.message_handler(func=lambda message: message.text == "📋 Мои записи")
def show_my_appointments(message):
    try:
        appointments = db.get_user_appointments(message.from_user.id)
        if not appointments:
            bot.reply_to(message, MESSAGES['no_appointments'])
            return
        response = "Ваши записи (включая историю):\n"
        status_translation = {
            'pending_approval': 'Ожидает одобрения',
            'approved': 'Одобрена',
            'rejected': 'Отклонена',
            'cancelled': 'Отменена',
            'completed': 'Выполнена'
        }
        for appointment in appointments:
            response += f"\n📅 Дата: {appointment[1]}\n👤 ФИО: {appointment[2]}\n🚗 Авто: {appointment[3]}\n🔧 Услуги: {appointment[4]}\n📊 Статус: {status_translation.get(appointment[5], appointment[5])}\n-------------------------\n"
        if len(response) > 4096:
            parts = [response[i:i + 4000] for i in range(0, len(response), 4000)]
            for part in parts:
                bot.send_message(message.chat.id, part)
        else:
            bot.send_message(message.chat.id, response)
    except Exception as e:
        logger.error(f"Error showing appointments: {e}")
        bot.reply_to(message, MESSAGES['system_error'])

# Показать профиль
@bot.message_handler(func=lambda message: message.text == "👤 Мой профиль")
def show_profile(message):
    user_id = message.from_user.id
    user_data_saved = db.get_user_data(user_id)
    if user_data_saved:
        response = (
            f"Ваш профиль (Telegram ID: {user_id}):\n"
            f"👤 ФИО: {user_data_saved['full_name']}\n"
            f"📱 Телефон: {user_data_saved['phone_number']}\n"
            f"🚗 Автомобиль: {user_data_saved['vehicle_info']}"
        )
        bot.send_message(message.chat.id, response, reply_markup=create_markup())
    else:
        bot.send_message(message.chat.id, "Ваш профиль пуст. Сделайте первую запись!", reply_markup=create_markup())

# Добавление нового администратора
@bot.message_handler(commands=['add_admin'])
def add_admin(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "У вас нет прав для добавления администратора.")
        return
    try:
        new_admin_id = int(message.text.split()[1])
        if new_admin_id in ADMIN_IDS:
            bot.reply_to(message, "Этот пользователь уже администратор.")
            return
        ADMIN_IDS.append(new_admin_id)
        bot.reply_to(message, MESSAGES['admin_added'].format(new_admin_id))
    except (IndexError, ValueError):
        bot.reply_to(message, "Используйте: /add_admin <Telegram ID> (например, /add_admin 987654321)")

# Админ-панель
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id not in ADMIN_IDS:
        logger.warning(f"Unauthorized admin access attempt from user {message.from_user.id}")
        bot.reply_to(message, "У вас нет прав доступа.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("Показать все записи"))
    markup.row(types.KeyboardButton("Экспорт записей"))
    markup.row(types.KeyboardButton("Удалить запись"))
    markup.row(types.KeyboardButton("Управление часами записи"))
    markup.row(types.KeyboardButton("Управление датами записи"))
    markup.row(types.KeyboardButton("Главное меню"))
    bot.reply_to(message, "Панель администратора:", reply_markup=markup)

# Показать все записи (админ)
@bot.message_handler(func=lambda message: message.text == "Показать все записи")
def show_all_appointments(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    appointments = db.get_all_appointments()
    if not appointments:
        bot.reply_to(message, "Записей пока нет.")
        return
    status_translation = {
        'pending_approval': 'Ожидает одобрения',
        'approved': 'Одобрена',
        'rejected': 'Отклонена',
        'cancelled': 'Отменена',
        'completed': 'Выполнена'
    }
    response = "Все записи:\n\n"
    for app in appointments:
        response += f"📅 Дата: {app[7]}\n👤 Клиент: {app[3]}\n🚗 Авто: {app[4]}\n🔧 Услуги: {app[5]}\n📱 Телефон: {app[6]}\n📊 Статус: {status_translation.get(app[8], app[8])}\n-------------------------\n"
    if len(response) > 4096:
        parts = [response[i:i + 4000] for i in range(0, len(response), 4000)]
        for part in parts:
            bot.send_message(message.chat.id, part)
    else:
        bot.send_message(message.chat.id, response)

# Экспорт записей (админ)
@bot.message_handler(func=lambda message: message.text == "Экспорт записей")
def export_appointments(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "У вас нет прав доступа.")
        return
    try:
        # Очистка старых записей перед экспортом
        cleaned_count = db.cleanup_old_appointments(1)  # Удаляем записи старше 1 дня
        if cleaned_count > 0:
            logger.info(f"Удалено {cleaned_count} старых записей перед экспортом")
        
        appointments = db.get_all_appointments()
        logger.info(f"Exporting {len(appointments)} appointments")
        if not appointments:
            bot.reply_to(message, "Нет данных для экспорта.")
            return

        status_translation = {
            'pending_approval': 'Ожидает одобрения',
            'approved': 'Одобрена',
            'rejected': 'Отклонена',
            'cancelled': 'Отменена',
            'completed': 'Выполнена'
        }
        
        # Создаем буфер для CSV данных
        csv_buffer = io.StringIO()
        csv_buffer.write("Дата,ФИО,Автомобиль,Услуги,Телефон,Статус\n")
        
        for app in appointments:
            status = status_translation.get(app[8], app[8])
            # Обрабатываем специальные символы в полях
            date = app[7].replace('"', '""')
            name = app[3].replace('"', '""')
            vehicle = app[4].replace('"', '""')
            service = app[5].replace('"', '""')
            phone = app[6].replace('"', '""')
            
            # Заключаем каждое поле в кавычки
            csv_buffer.write(f'"{date}","{name}","{vehicle}","{service}","{phone}","{status}"\n')

        # Конвертируем в байты с правильной кодировкой
        csv_bytes = io.BytesIO(csv_buffer.getvalue().encode('utf-8-sig'))
        csv_bytes.seek(0)
        
        logger.info(f"CSV data size: {len(csv_buffer.getvalue())} characters")
        
        bot.send_document(
            message.chat.id,
            document=types.InputFile(csv_bytes, filename="appointments.csv"),
            caption="Экспорт записей на прием"
        )
        logger.info("Export successful")
    except Exception as e:
        logger.error(f"Export failed: {str(e)}")
        bot.reply_to(message, f"Ошибка при экспорте: {str(e)}")

# Удаление записи (админ)
@bot.message_handler(func=lambda message: message.text == "Удалить запись")
def delete_appointment_start(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "У вас нет прав доступа.")
        return
    try:
        appointments = db.get_all_appointments()
        if not appointments:
            bot.reply_to(message, "Нет записей для удаления.")
            return
        markup = types.InlineKeyboardMarkup()
        status_translation = {
            'pending_approval': 'Ожидает',
            'approved': 'Одобрена',
            'rejected': 'Отклонена',
            'cancelled': 'Отменена',
            'completed': 'Выполнена'
        }
        for app in appointments:
            status = status_translation.get(app[8], app[8])
            button_text = f"{app[7]} - {app[3]} ({status})"
            markup.add(types.InlineKeyboardButton(
                text=button_text,
                callback_data=f"delete_app_{app[0]}"
            ))
        markup.add(types.InlineKeyboardButton("Назад", callback_data="back_to_admin"))
        bot.reply_to(message, "Выберите запись для удаления:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Error in delete_appointment_start: {e}")
        bot.reply_to(message, MESSAGES['system_error'])

# Обработка выбора записи для удаления
@bot.callback_query_handler(func=lambda call: call.data.startswith('delete_app_'))
def process_delete_appointment(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "У вас нет прав доступа.")
        return
    try:
        appointment_id = int(call.data.replace('delete_app_', ''))
        appointment = db.get_appointment_by_id(appointment_id)

        if not appointment:
            bot.answer_callback_query(call.id, "Запись не найдена")
            return

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("Да, удалить", callback_data=f"confirm_delete_{appointment_id}"),
            types.InlineKeyboardButton("Нет, отменить", callback_data="cancel_delete")
        )
        confirmation_text = (
            f"Вы уверены, что хотите удалить запись?\n"
            f"📅 Дата: {appointment[7]}\n"
            f"👤 Клиент: {appointment[3]}\n"
            f"🚗 Авто: {appointment[4]}\n"
            f"🔧 Услуги: {appointment[5]}"
        )
        bot.edit_message_text(confirmation_text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"Error in process_delete_appointment: {e}")
        bot.answer_callback_query(call.id, "Произошла ошибка")

# Подтверждение удаления
@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_delete_') or call.data == "cancel_delete")
def confirm_delete_appointment(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "У вас нет прав доступа.")
        return
    try:
        if call.data == "cancel_delete":
            bot.edit_message_text("Удаление отменено", call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        appointment_id = int(call.data.replace('confirm_delete_', ''))
        appointment = db.get_appointment_by_id(appointment_id)

        if not appointment:
            bot.edit_message_text("Запись не найдена", call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id)
            return

        if db.delete_appointment(appointment_id):
            bot.edit_message_text(
                f"Запись на {appointment[7]} для {appointment[3]} успешно удалена",
                call.message.chat.id,
                call.message.message_id
            )
            if appointment[8] in ['approved', 'pending_approval']:
                bot.send_message(
                    appointment[1],
                    f"Ваша запись на {appointment[7]} была удалена администратором."
                )
            bot.answer_callback_query(call.id, "Запись удалена")
        else:
            bot.edit_message_text("Ошибка при удалении записи", call.message.chat.id, call.message.message_id)
            bot.answer_callback_query(call.id, "Ошибка")
    except Exception as e:
        logger.error(f"Error in confirm_delete_appointment: {e}")
        bot.edit_message_text(f"Произошла ошибка: {str(e)}", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Произошла ошибка")

# Управление часами записи (админ)
@bot.message_handler(func=lambda message: message.text == "Управление часами записи")
def manage_slots(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "У вас нет прав доступа.")
        return
    markup = types.InlineKeyboardMarkup()
    available_dates = db.get_available_dates()
    if not available_dates:
        bot.reply_to(message, "Сначала добавьте доступные даты в 'Управление датами записи'.")
        return
    for date_str in available_dates:
        markup.add(types.InlineKeyboardButton(f"Управление {date_str}", callback_data=f"manage_{date_str}"))
    bot.send_message(message.chat.id, "Выберите день для управления часами:", reply_markup=markup)

# Управление датами записи (админ)
@bot.message_handler(func=lambda message: message.text == "Управление датами записи")
def manage_dates(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "У вас нет прав доступа.")
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Добавить дату", callback_data="add_date"))
    markup.add(types.InlineKeyboardButton("Удалить дату", callback_data="remove_date"))
    markup.add(types.InlineKeyboardButton("Вернуться в админ-панель", callback_data="back_to_admin"))
    bot.send_message(message.chat.id, "Управление датами записи:", reply_markup=markup)

# Обработка управления датами
@bot.callback_query_handler(func=lambda call: call.data in ["add_date", "remove_date"])
def process_date_management(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "У вас нет прав доступа.")
        return
    if call.data == "add_date":
        markup = types.InlineKeyboardMarkup(row_width=3)
        today = datetime.now()
        for i in range(7):
            date = today + timedelta(days=i)
            date_str = date.strftime("%d.%m.%Y")
            markup.add(types.InlineKeyboardButton(date_str, callback_data=f"add_date_{date_str}"))
        markup.add(types.InlineKeyboardButton("Назад", callback_data="back_to_manage_dates"))
        bot.edit_message_text("Выберите дату для добавления:", call.message.chat.id, call.message.message_id,
                              reply_markup=markup)
    elif call.data == "remove_date":
        available_dates = db.get_available_dates()
        if not available_dates:
            bot.edit_message_text("Нет доступных дат для удаления.", call.message.chat.id, call.message.message_id)
            return
        markup = types.InlineKeyboardMarkup()
        for date in available_dates:
            markup.add(types.InlineKeyboardButton(date, callback_data=f"remove_date_{date}"))
        markup.add(types.InlineKeyboardButton("Назад", callback_data="back_to_manage_dates"))
        bot.edit_message_text("Выберите дату для удаления:", call.message.chat.id, call.message.message_id,
                              reply_markup=markup)

# Возврат к управлению датами
@bot.callback_query_handler(func=lambda call: call.data == "back_to_manage_dates")
def back_to_manage_dates(call):
    if call.from_user.id not in ADMIN_IDS:
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Добавить дату", callback_data="add_date"))
    markup.add(types.InlineKeyboardButton("Удалить дату", callback_data="remove_date"))
    markup.add(types.InlineKeyboardButton("Вернуться в админ-панель", callback_data="back_to_admin"))
    bot.edit_message_text("Управление датами записи:", call.message.chat.id, call.message.message_id,
                          reply_markup=markup)

# Добавление даты
@bot.callback_query_handler(func=lambda call: call.data.startswith('add_date_'))
def add_date(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "У вас нет прав доступа.")
        return
    date_str = call.data.replace('add_date_', '')
    if db.add_available_date(date_str):
        bot.edit_message_text(MESSAGES['date_added'].format(date_str), call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Дата добавлена")
    else:
        bot.answer_callback_query(call.id, "Ошибка при добавлении даты")

# Удаление даты
@bot.callback_query_handler(func=lambda call: call.data.startswith('remove_date_'))
def remove_date(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "У вас нет прав доступа.")
        return
    date_str = call.data.replace('remove_date_', '')
    if db.remove_available_date(date_str):
        bot.edit_message_text(MESSAGES['date_removed'].format(date_str), call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id, "Дата удалена")
    else:
        bot.answer_callback_query(call.id, "Ошибка при удалении даты")

# Выбор часов для блокировки/разблокировки
@bot.callback_query_handler(func=lambda call: call.data.startswith('manage_'))
def manage_slots_day(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "У вас нет прав доступа.")
        return
    selected_date = call.data.replace('manage_', '')
    markup = types.InlineKeyboardMarkup(row_width=3)
    start_hour = WORKING_HOURS['start']
    end_hour = WORKING_HOURS['end']
    interval = WORKING_HOURS['interval']

    blocked_slots = db.get_blocked_slots()
    for hour in range(start_hour, end_hour, interval):
        time_str = f"{hour:02d}:00"
        full_datetime = f"{selected_date} {time_str}"
        if full_datetime in blocked_slots:
            markup.add(
                types.InlineKeyboardButton(f"{time_str} (заблокировано)", callback_data=f"unblock_{full_datetime}"))
        else:
            markup.add(types.InlineKeyboardButton(time_str, callback_data=f"block_{full_datetime}"))
    markup.add(types.InlineKeyboardButton("Назад", callback_data="back_to_admin"))
    bot.edit_message_text(f"Управление часами на {selected_date}:", call.message.chat.id, call.message.message_id,
                          reply_markup=markup)

# Блокировка/разблокировка слота
@bot.callback_query_handler(func=lambda call: call.data.startswith('block_') or call.data.startswith('unblock_'))
def process_slot_action(call):
    if call.from_user.id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "У вас нет прав доступа.")
        return
    action, slot_time = call.data.split('_', 1)
    if action == "block":
        if db.block_slot(slot_time):
            bot.answer_callback_query(call.id, f"Слот {slot_time} заблокирован")
        else:
            bot.answer_callback_query(call.id, "Ошибка при блокировке")
    elif action == "unblock":
        if db.unblock_slot(slot_time):
            bot.answer_callback_query(call.id, f"Слот {slot_time} разблокирован")
        else:
            bot.answer_callback_query(call.id, "Ошибка при разблокировке")

    selected_date = slot_time.split()[0]
    markup = types.InlineKeyboardMarkup(row_width=3)
    start_hour = WORKING_HOURS['start']
    end_hour = WORKING_HOURS['end']
    interval = WORKING_HOURS['interval']
    blocked_slots = db.get_blocked_slots()
    for hour in range(start_hour, end_hour, interval):
        time_str = f"{hour:02d}:00"
        full_datetime = f"{selected_date} {time_str}"
        if full_datetime in blocked_slots:
            markup.add(
                types.InlineKeyboardButton(f"{time_str} (заблокировано)", callback_data=f"unblock_{full_datetime}"))
        else:
            markup.add(types.InlineKeyboardButton(time_str, callback_data=f"block_{full_datetime}"))
    markup.add(types.InlineKeyboardButton("Назад", callback_data="back_to_admin"))
    bot.edit_message_text(f"Управление часами на {selected_date}:", call.message.chat.id, call.message.message_id,
                          reply_markup=markup)

# Возврат к админ-панели
@bot.callback_query_handler(func=lambda call: call.data == "back_to_admin")
def back_to_admin(call):
    if call.from_user.id not in ADMIN_IDS:
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(types.KeyboardButton("Показать все записи"))
    markup.row(types.KeyboardButton("Экспорт записей"))
    markup.row(types.KeyboardButton("Удалить запись"))
    markup.row(types.KeyboardButton("Управление часами записи"))
    markup.row(types.KeyboardButton("Управление датами записи"))
    markup.row(types.KeyboardButton("Главное меню"))
    bot.edit_message_text("Панель администратора закрыта", call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id, "Панель администратора:", reply_markup=markup)

# Возврат в главное меню
@bot.message_handler(func=lambda message: message.text == "Главное меню")
def return_to_main_menu(message):
    bot.send_message(message.chat.id, "Главное меню:", reply_markup=create_markup())

# Функция очистки ввода
def sanitize_input(text: str) -> str:
    return re.sub(r'[<>;]', '', text.strip())

# Запуск бота
if __name__ == "__main__":
    logger.info("Bot starting...")
    try:
        bot.remove_webhook()
        updates = bot.get_updates(offset=-1, timeout=1)
        if updates:
            bot.get_updates(offset=updates[-1].update_id + 1)
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        if db and db.is_connected():
            db.commit_and_close()
        raise
    finally:
        if db and db.is_connected():
            db.commit_and_close()