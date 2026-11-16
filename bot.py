import telebot
from telebot.types import WebAppInfo, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton
import json

TOKEN = '9134707044:AAE5WyeyFaSaRwGVgFCje60vonaLxitgP5Y'
PAYMENT_TOKEN = '381764678:TEST:196498'
ADMIN_ID = 165418736  # Твой цифровой ID без кавычек

bot = telebot.TeleBot(TOKEN)
PENDING_ORDERS = {}
# Словарь для связи кнопки админа с чатом клиента: {message_id: client_chat_id}
ORDER_CLIENT_MAP = {}

DRINK_CATEGORIES = ['seasonal', 'milk', 'black', 'not-coffee']

@bot.message_handler(commands=['myid'])
def get_id(message):
    bot.send_message(message.chat.id, f"ID этого чата: `{message.chat.id}`", parse_mode='Markdown')

@bot.message_handler(commands=['start'])
def start(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    web_app = WebAppInfo(url="https://calidocoffee.github.io/calido-menu/?v=10") 
    markup.add(KeyboardButton(text="☕️ Меню Calido coffee", web_app=web_app))
    
    welcome_text = "Привет! Это Calido coffee🤎 Твой вкусный кофе и завтраки уже ждут в меню!"
    bot.send_message(message.chat.id, welcome_text, reply_markup=markup, parse_mode='HTML')

@bot.message_handler(content_types=['web_app_data'])
def web_app_data(message):
    try:
        data = json.loads(message.web_app_data.data)
        items = data.get('items', {})
        payment = data.get('payment', 'Не указан')
        
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
            
            # Определяем, напиток это или еда по ID или названию
            # Или передаем категорию прямо из вебаппа. Сделаем универсально по ключевым словам или спискам:
            is_drink = any(d in item_id for d in ['s1', 's2', 's3', 's4', 's5', 'm1', 'm2', 'm3', 'm4', 'm5', 'm6', 'm7', 'b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'nc1', 'nc2', 'nc3', 'nc4', 'nc5', 'nc6', 'nc7', 'nc8', 'nc9', 'nc10', 'nc11'])
            
            item_line = f"▫️ <b>{title}</b>\n   Количество: <b>{count} шт.</b> | Сумма: <b>{sum_item} ₽</b>\n\n"
            
            if is_drink:
                drinks_receipt += item_line
            else:
                foods_receipt += item_line
                
            prices.append(LabeledPrice(label=f"{title} x{count}"[:32], amount=sum_item * 100))
            
        # Собираем красивый структурированный чек с отступами
        structured_receipt = ""
        if drinks_receipt:
            structured_receipt += "☕️ <b>НАПИТКИ:</b>\n\n" + drinks_receipt
        if foods_receipt:
            structured_receipt += "🍽 <b>ЕДА И ЗАВТРАКИ:</b>\n\n" + foods_receipt
            
        structured_receipt += f"💰 <b>Итого к оплате: {total_sum} ₽</b>\n"
        structured_receipt += f"💳 Способ оплаты: <b>{payment}</b>"
        
        username = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
        user_info = f"👤 <b>Клиент:</b> {message.from_user.first_name} ({username})\n\n"
        
        if "онлайн" in payment.lower():
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
        else:
            bot.send_message(message.chat.id, "✅ <b>Заказ принят! Ждем тебя на Рабочей, 49</b>\n\n" + structured_receipt, parse_mode='HTML')
            
            if int(ADMIN_ID) != 0:
                # Создаем инлайн-кнопку "Готово" для бариста
                admin_markup = InlineKeyboardMarkup()
                admin_markup.add(InlineKeyboardButton(text="✅ Готово (позвать гостя)", callback_data=f"ready_{message.chat.id}"))
                
                admin_msg = bot.send_message(
                    int(ADMIN_ID), 
                    "🔔 <b>НОВЫЙ ЗАКАЗ (Оплата при получении)!</b>\n\n" + user_info + structured_receipt, 
                    parse_mode='HTML', 
                    reply_markup=admin_markup
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
    
    if int(ADMIN_ID) != 0:
        admin_markup = InlineKeyboardMarkup()
        admin_markup.add(InlineKeyboardButton(text="✅ Готово (позвать гостя)", callback_data=f"ready_{message.chat.id}"))
        
        admin_text = f"💸 <b>ЗАКАЗ ОПЛАЧЕН ОНЛАЙН ({suma} ₽)!</b>\n\n" + full_order_text
        bot.send_message(int(ADMIN_ID), admin_text, parse_mode='HTML', reply_markup=admin_markup)

# Обработка нажатия кнопки "Готово" бариста
@bot.callback_query_handler(func=lambda call: call.data.startswith('ready_'))
def order_ready_callback(call):
    client_chat_id = int(call.data.split('_')[1])
    
    try:
        # Отправляем сообщение клиенту
        bot.send_message(client_chat_id, "Мы уже все приготовили! Ждем Вас🫶🏻", parse_mode='HTML')
        
        # Уведомляем бариста в чате, что статус обновлен
        bot.answer_callback_query(call.id, text="Гостю отправлено уведомление!")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None) # убираем кнопку
        bot.edit_message_text(call.message.text + "\n\n✅ <i>Статус: Заказ готов</i>", call.message.chat.id, call.message.message_id, parse_mode='HTML')
    except Exception as e:
        bot.answer_callback_query(call.id, text=f"Ошибка: не удалось отправить сообщение гостю.")

bot.infinity_polling()
