import os
import json
import threading
import time
import telebot
from telebot.types import WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request, jsonify
from flask_cors import CORS

# Берем секретные ключи из вкладки Variables на Railway
TOKEN = os.environ.get('BOT_TOKEN')
PAYMENT_TOKEN = os.environ.get('PAYMENT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))

bot = telebot.TeleBot(TOKEN)
PENDING_ORDERS = {}

# Запускаем мини-сервер для приема заказов с синих кнопок Open / ОТКРЫТЬ
app = Flask(__name__)
CORS(app)

def build_order_data(items, payment, user_dict):
    drinks_receipt = ""
    foods_receipt = ""
    total_sum = 0
    prices = []
    
    for item_id, item_data in items.items():
        title = item_data['title']
        count = int(item_data['count'])
        price = int(item_data['price'])
        sum_item = count * price
        total_sum += sum_item
        
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
        
    structured_receipt += f"💰 <b>Итого к оплате: {total_sum} ₽</b>\n"
    structured_receipt += f"💳 Способ оплаты: <b>{payment}</b>"
    
    username = f"@{user_dict.get('username')}" if user_dict.get('username') else "без юзернейма"
    first_name = user_dict.get('first_name', 'Гость')
    user_info = f"👤 <b>Клиент:</b> {first_name} ({username})\n\n"
    
    return structured_receipt, user_info, prices

@app.route('/create_order', methods=['POST'])
def create_order():
    try:
        data = request.json
        user = data.get('user', {})
        chat_id = user.get('id')
        items = data.get('items', {})
        payment = data.get('payment', 'Картой онлайн')
        
        if not chat_id:
            return jsonify({'ok': False, 'error': 'Не удалось определить ID пользователя в Telegram'}), 400
            
        structured_receipt, user_info, prices = build_order_data(items, payment, user)
        
        order_key = f"order_{chat_id}_{int(time.time())}"
        PENDING_ORDERS[order_key] = user_info + structured_receipt
        
        # Отправляем красивый состав заказа в чат гостю
        bot.send_message(chat_id, "🧾 <b>Состав твоего заказа:</b>\n\n" + structured_receipt, parse_mode='HTML')
        
        # Генерируем ссылку на оплату для открытия прямо поверх мини-аппа
        invoice_link = bot.create_invoice_link(
            title="Заказ в Calido Coffee",
            description="Самовывоз: Саратов, ул. Рабочая, 49",
            payload=order_key,
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=prices
        )
        
        return jsonify({'ok': True, 'invoice_link': invoice_link})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@bot.message_handler(commands=['myid'])
def get_id(message):
    bot.send_message(message.chat.id, f"ID этого чата: `{message.chat.id}`", parse_mode='Markdown')

@bot.message_handler(commands=['start'])
def start(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    web_app = WebAppInfo(url="https://calidocoffee.github.io/calido-menu/?v=16") 
    markup.add(KeyboardButton(text="☕️ Меню Calido coffee", web_app=web_app))
    
    welcome_text = "Привет! Это Calido coffee🤎 Твой вкусный кофе и завтраки уже ждут в меню!"
    bot.send_message(message.chat.id, welcome_text, reply_markup=markup, parse_mode='HTML')

# Оставляем поддержку и старой нижней кнопки на всякий случай
@bot.message_handler(content_types=['web_app_data'])
def web_app_data(message):
    try:
        data = json.loads(message.web_app_data.data)
        items = data.get('items', {})
        payment = data.get('payment', 'Картой онлайн')
        
        user_dict = {
            'first_name': message.from_user.first_name,
            'username': message.from_user.username
        }
        structured_receipt, user_info, prices = build_order_data(items, payment, user_dict)
        
        bot.send_message(message.chat.id, "🧾 <b>Состав твоего заказа:</b>\n\n" + structured_receipt, parse_mode='HTML')
        order_key = f"order_{message.chat.id}_{message.message_id}"
        PENDING_ORDERS[order_key] = user_info + structured_receipt
        
        bot.send_invoice(
            chat_id=message.chat.id,
            title="Заказ в Calido Coffee",
            description="Самовывоз: Саратов, ул. Рабочая, 49",
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

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    suma = message.successful_payment.total_amount // 100
    order_key = message.successful_payment.invoice_payload
    full_order_text = PENDING_ORDERS.get(order_key, "Состав заказа не найден в памяти бота.")
    
    bot.send_message(
        message.chat.id, 
        f"🔥 <b>Оплата {suma} ₽ прошла успешно!</b>\nБариста уже начал готовить твой заказ. Ждем на Рабочей, 49!", 
        parse_mode='HTML'
    )
    
    if ADMIN_ID != 0:
        admin_markup = InlineKeyboardMarkup()
        admin_markup.add(InlineKeyboardButton(text="✅ Готово (позвать гостя)", callback_data=f"ready_{message.chat.id}"))
        
        admin_text = f"💸 <b>ЗАКАЗ ОПЛАЧЕН ОНЛАЙН ({suma} ₽)!</b>\n\n" + full_order_text
        bot.send_message(ADMIN_ID, admin_text, parse_mode='HTML', reply_markup=admin_markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('ready_'))
def order_ready_callback(call):
    client_chat_id = int(call.data.split('_')[1])
    
    try:
        bot.send_message(client_chat_id, "Мы уже все приготовили! Ждем Вас🫶🏻", parse_mode='HTML')
        bot.answer_callback_query(call.id, text="Гостю отправлено уведомление!")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        bot.edit_message_text(call.message.text + "\n\n✅ <i>Статус: Заказ готов</i>", call.message.chat.id, call.message.message_id, parse_mode='HTML')
    except Exception as e:
        bot.answer_callback_query(call.id, text="Ошибка: не удалось отправить сообщение гостю.")

def run_bot():
    bot.infinity_polling()

if __name__ == '__main__':
    # Запускаем бота в фоновом потоке, а веб-сервер — на основном порту Railway
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
