import os
import json
import threading
import time
from datetime import datetime, timedelta, timezone
import telebot
from telebot.types import WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, jsonify
from flask_cors import CORS

TOKEN = os.environ.get('BOT_TOKEN')
PAYMENT_TOKEN = os.environ.get('PAYMENT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))

bot = telebot.TeleBot(TOKEN)
PENDING_ORDERS = {}
ACTIVE_USER_ORDERS = {}  # Хранит живой статус текущего заказа для каждого гостя (по chat_id)

# Часовой пояс Саратова (UTC+4)
SARATOV_TZ = timezone(timedelta(hours=4))

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

@app.route('/', methods=['GET'])
def index():
    return "Calido Server is running!", 200

def get_saratov_now():
    return datetime.now(SARATOV_TZ)

def build_order_data(items, payment, user_dict, ready_label):
    drinks_receipt = ""
    foods_receipt = ""
    total_sum = 0
    prices = []
    short_items_list = []
    
    for item_id, item_data in items.items():
        title = item_data['title']
        count = int(item_data['count'])
        price = int(item_data['price'])
        sum_item = count * price
        total_sum += sum_item
        
        short_items_list.append(f"{title} × {count}")
        
        is_drink = item_data.get('isDrink', False)
        item_line = f"▫️ <b>{title}</b>\n   Количество: <b>{count} шт.</b> | Сумма: <b>{sum_item} ₽</b>\n\n"
        
        if is_drink:
            drinks_receipt += item_line
        else:
            foods_receipt += item_line
            
        prices.append(LabeledPrice(label=f"{title} x{count}"[:32], amount=sum_item * 100))
        
    structured_receipt = ""
    if drinks_receipt:
        structured_receipt += "☕️ <b>НАПИТКИ:</b>\n\n" + drinks_receipt
    if foods_receipt:
        structured_receipt += "🍽 <b>ЕДА И ЗАВТРАКИ:</b>\n\n" + foods_receipt
        
    structured_receipt += f"⏰ <b>Время готовности: {ready_label}</b>\n"
    structured_receipt += f"💰 <b>Итого к оплате: {total_sum} ₽</b>\n"
    structured_receipt += f"💳 Способ оплаты: <b>{payment}</b>"
    
    username = f"@{user_dict.get('username')}" if user_dict.get('username') else "без юзернейма"
    first_name = user_dict.get('first_name', 'Гость')
    user_info = f"👤 <b>Клиент:</b> {first_name} ({username})\n\n"
    
    return structured_receipt, user_info, prices, total_sum, short_items_list

@app.route('/create_order', methods=['POST'])
def create_order():
    try:
        data = request.get_json(force=True)
        user = data.get('user') or {}
        chat_id = user.get('id')
        items = data.get('items', {})
        payment = data.get('payment', 'Картой онлайн')
        
        ready_label = data.get('ready_label', 'Как можно скорее')
        target_hhmm = data.get('target_hhmm', '')
        prep_minutes = int(data.get('prep_minutes', 10))
        is_delayed = bool(data.get('is_delayed', False))
        
        structured_receipt, user_info, prices, total_sum, short_items_list = build_order_data(items, payment, user, ready_label)
        
        order_key = f"order_{chat_id or 'guest'}_{int(time.time())}"
        PENDING_ORDERS[order_key] = {
            'chat_id': chat_id,
            'text': user_info + structured_receipt,
            'structured_receipt': structured_receipt,
            'ready_label': ready_label,
            'target_hhmm': target_hhmm,
            'prep_minutes': prep_minutes,
            'is_delayed': is_delayed,
            'total_sum': total_sum,
            'short_items': ", ".join(short_items_list)
        }
        
        if chat_id:
            try:
                bot.send_message(chat_id, "🧾 <b>Состав твоего заказа:</b>\n\n" + structured_receipt, parse_mode='HTML')
            except Exception as msg_err:
                print(f"Не удалось отправить пречек в чат: {msg_err}")
        
        invoice_link = bot.create_invoice_link(
            title="Заказ в Calido Coffee",
            description=f"Самовывоз (Рабочая, 49) • {ready_label}",
            payload=order_key,
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=prices
        )
        
        return jsonify({'ok': True, 'invoice_link': invoice_link, 'order_key': order_key})
    except Exception as e:
        print(f"Ошибка в /create_order: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/order_status', methods=['GET'])
def get_order_status():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({'ok': True, 'active_order': None})
    
    order = ACTIVE_USER_ORDERS.get(str(user_id))
    if not order:
        return jsonify({'ok': True, 'active_order': None})
    
    now_ts = time.time()
    # Если заказ готов, показываем зеленую плашку еще 45 минут; если готовится — храним до 12 часов
    if order.get('status') == 'ready' and (now_ts - order.get('ready_at', now_ts)) > 2700:
        return jsonify({'ok': True, 'active_order': None})
    if (now_ts - order.get('paid_at', now_ts)) > 43200:
        return jsonify({'ok': True, 'active_order': None})
        
    return jsonify({'ok': True, 'active_order': order})

@bot.message_handler(commands=['myid'])
def get_id(message):
    bot.send_message(message.chat.id, f"ID этого чата: `{message.chat.id}`", parse_mode='Markdown')

@bot.message_handler(commands=['start'])
def start(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    web_app = WebAppInfo(url="https://calidocoffee.github.io/calido-menu/?v=20") 
    markup.add(KeyboardButton(text="☕️ Меню Calido coffee", web_app=web_app))
    
    welcome_text = "Привет! Это Calido coffee🤎 Твой вкусный кофе и завтраки уже ждут в меню!"
    bot.send_message(message.chat.id, welcome_text, reply_markup=markup, parse_mode='HTML')

@bot.message_handler(content_types=['web_app_data'])
def web_app_data(message):
    try:
        data = json.loads(message.web_app_data.data)
        items = data.get('items', {})
        payment = data.get('payment', 'Картой онлайн')
        ready_label = data.get('ready_label', 'Как можно скорее')
        target_hhmm = data.get('target_hhmm', '')
        prep_minutes = int(data.get('prep_minutes', 10))
        is_delayed = bool(data.get('is_delayed', False))
        
        user_dict = {
            'first_name': message.from_user.first_name,
            'username': message.from_user.username
        }
        structured_receipt, user_info, prices, total_sum, short_items_list = build_order_data(items, payment, user_dict, ready_label)
        
        bot.send_message(message.chat.id, "🧾 <b>Состав твоего заказа:</b>\n\n" + structured_receipt, parse_mode='HTML')
        order_key = f"order_{message.chat.id}_{message.message_id}"
        PENDING_ORDERS[order_key] = {
            'chat_id': message.chat.id,
            'text': user_info + structured_receipt,
            'structured_receipt': structured_receipt,
            'ready_label': ready_label,
            'target_hhmm': target_hhmm,
            'prep_minutes': prep_minutes,
            'is_delayed': is_delayed,
            'total_sum': total_sum,
            'short_items': ", ".join(short_items_list)
        }
        
        bot.send_invoice(
            chat_id=message.chat.id,
            title="Заказ в Calido Coffee",
            description=f"Самовывоз (Рабочая, 49) • {ready_label}",
            invoice_payload=order_key,
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=prices
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ Ошибка при обработке заказа: {e}")

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

def send_delayed_cooking_reminder(admin_id, client_chat_id, target_hhmm, suma, full_order_text):
    try:
        # Меняем статус в личном кабинете гостя с "Запланирован" на "Готовится"
        uid_str = str(client_chat_id)
        if uid_str in ACTIVE_USER_ORDERS and ACTIVE_USER_ORDERS[uid_str].get('status') == 'scheduled':
            ACTIVE_USER_ORDERS[uid_str]['status'] = 'cooking'

        admin_markup = InlineKeyboardMarkup()
        admin_markup.add(InlineKeyboardButton(text="✅ Готово (позвать гостя)", callback_data=f"ready_{client_chat_id}"))
        
        reminder_text = (
            f"⏰🔥 <b>ПОРА ГОТОВИТЬ ПРЕДЗАКАЗ К {target_hhmm}!</b>\n"
            f"<i>(Оплачено онлайн: {suma} ₽)</i>\n\n"
            f"{full_order_text}"
        )
        bot.send_message(admin_id, reminder_text, parse_mode='HTML', reply_markup=admin_markup)
    except Exception as e:
        print(f"Ошибка при отправке напоминания бариста: {e}")

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    suma = message.successful_payment.total_amount // 100
    order_key = message.successful_payment.invoice_payload
    order_info = PENDING_ORDERS.get(order_key)
    
    now_saratov = get_saratov_now()
    paid_time_str = now_saratov.strftime('%H:%M')

    if isinstance(order_info, dict):
        full_order_text = order_info.get('text', '')
        ready_label = order_info.get('ready_label', 'Как можно скорее')
        target_hhmm = order_info.get('target_hhmm', '')
        prep_minutes = int(order_info.get('prep_minutes', 10))
        is_delayed = order_info.get('is_delayed', False)
        short_items = order_info.get('short_items', 'Кофе и завтраки')
    else:
        full_order_text = str(order_info or "Состав заказа не найден в памяти бота.")
        ready_label = "Как можно скорее"
        target_hhmm = ""
        prep_minutes = 10
        is_delayed = False
        short_items = "Заказ в Calido Coffee"

    bot.send_message(
        message.chat.id, 
        f"🔥 <b>Оплата {suma} ₽ прошла успешно!</b>\n"
        f"⏰ Время готовности: <b>{ready_label}</b>\n"
        f"Ждем тебя на Рабочей, 49!", 
        parse_mode='HTML'
    )
    
    delay_seconds = 0
    remind_time_str = ""
    if is_delayed and target_hhmm and ':' in target_hhmm:
        try:
            th, tm = map(int, target_hhmm.split(':'))
            target_dt = now_saratov.replace(hour=th, minute=tm, second=0, microsecond=0)
            remind_dt = target_dt - timedelta(minutes=prep_minutes)
            delay_seconds = (remind_dt - now_saratov).total_seconds()
            remind_time_str = remind_dt.strftime('%H:%M')
        except Exception as e:
            print(f"Ошибка расчета таймера предзаказа: {e}")
            delay_seconds = 0

    # Сохраняем активный заказ для отображения живого статуса в Личном кабинете гостя
    ACTIVE_USER_ORDERS[str(message.chat.id)] = {
        'order_key': order_key,
        'status': 'scheduled' if delay_seconds > 300 else 'cooking',
        'ready_label': ready_label,
        'target_hhmm': target_hhmm,
        'total_sum': suma,
        'paid_time': paid_time_str,
        'paid_at': time.time(),
        'short_items': short_items
    }

    if ADMIN_ID != 0:
        if delay_seconds > 300:
            pre_text = (
                f"📅 <b>ОФОРМЛЕН ПРЕДЗАКАЗ НА {target_hhmm}!</b>\n"
                f"⚠️ <i>Готовить сейчас НЕ нужно! Бот пришлет уведомление с кнопкой «Готово» в <b>{remind_time_str}</b> (за {prep_minutes} мин до прихода гостя).</i>\n"
                f"🕒 Оплачено в: {paid
