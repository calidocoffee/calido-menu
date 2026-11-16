import telebot
from telebot import apihelper
from telebot.types import WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice
import json

apihelper.proxy = {'https': 'http://proxy.server:3128'}

TOKEN = '9134707044:AAE5WyeyFaSaRwGVgFCje60vonaLxitgP5Y'
PAYMENT_TOKEN = '381764678:TEST:196498'
ADMIN_ID = 165418736  # Твой цифровой ID без кавычек

bot = telebot.TeleBot(TOKEN)

# Временное хранилище заказов до подтверждения оплаты
PENDING_ORDERS = {}

@bot.message_handler(commands=['myid'])
def get_id(message):
    bot.send_message(message.chat.id, f"ID этого чата: `{message.chat.id}`", parse_mode='Markdown')

@bot.message_handler(commands=['start'])
def start(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    web_app = WebAppInfo(url="https://calidocoffee.github.io/calido-menu/?v=4") 
    markup.add(KeyboardButton(text="☕️ Меню Calido coffee", web_app=web_app))
    
    bot.send_message(message.chat.id, "Привет! Жми кнопку внизу, чтобы собрать заказ.", reply_markup=markup)

@bot.message_handler(content_types=['web_app_data'])
def web_app_data(message):
    try:
        data = json.loads(message.web_app_data.data)
        items = data.get('items', {})
        payment = data.get('payment', 'Не указан')
        
        receipt = ""
        total_sum = 0
        prices = []
        
        for item_id, item_data in items.items():
            title = item_data['title']
            count = int(item_data['count'])
            price = int(item_data['price'])
            sum_item = count * price
            total_sum += sum_item
            
            receipt += f"▫️ {title} x{count} = {sum_item} ₽\n"
            prices.append(LabeledPrice(label=f"{title} x{count}"[:32], amount=sum_item * 100))
            
        receipt += f"\n💰 <b>Итого: {total_sum} ₽</b>\n"
        receipt += f"💳 Способ оплаты: {payment}"
        
        username = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
        user_info = f"👤 <b>Клиент:</b> {message.from_user.first_name} ({username})\n\n"
        
        if "онлайн" in payment.lower():
            bot.send_message(message.chat.id, "🧾 <b>Состав заказа:</b>\n\n" + receipt, parse_mode='HTML')
            
            # Генерируем уникальный ключ заказа и сохраняем полный текст для админа
            order_key = f"order_{message.chat.id}_{message.message_id}"
            PENDING_ORDERS[order_key] = user_info + receipt
            
            bot.send_invoice(
                chat_id=message.chat.id,
                title="Заказ в Calido Coffee",
                description="Самовывоз: Саратов, ул. Рабочая, 49",
                invoice_payload=order_key,  # Передаем короткий ключ без обрезания текста!
                provider_token=PAYMENT_TOKEN,
                currency="RUB",
                prices=prices
            )
        else:
            bot.send_message(message.chat.id, "✅ <b>Заказ принят!</b>\n\n" + receipt, parse_mode='HTML')
            if int(ADMIN_ID) != 0:
                bot.send_message(int(ADMIN_ID), "🔔 <b>НОВЫЙ ЗАКАЗ (Оплата при получении)!</b>\n\n" + user_info + receipt, parse_mode='HTML')
            
    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ Ошибка при обработке заказа: {e}")
        print("ERROR:", e)

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    suma = message.successful_payment.total_amount // 100
    order_key = message.successful_payment.invoice_payload
    
    # Достаем полный текст заказа из нашей памяти
    full_order_text = PENDING_ORDERS.get(order_key, "Состав заказа не найден в памяти бота.")
    
    bot.send_message(
        message.chat.id, 
        f"🔥 <b>Оплата {suma} ₽ прошла успешно!</b>\nБариста уже начал готовить твой заказ. Ждем на Рабочей, 49!", 
        parse_mode='HTML'
    )
    
    if int(ADMIN_ID) != 0:
        admin_text = f"💸 <b>ЗАКАЗ ОПЛАЧЕН ОНЛАЙН ({suma} ₽)!</b>\n\n" + full_order_text
        bot.send_message(int(ADMIN_ID), admin_text, parse_mode='HTML')

bot.polling(non_stop=True)
