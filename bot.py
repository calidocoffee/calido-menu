import os
import json
import hmac
import hashlib
import threading
import time
from urllib.parse import parse_qsl
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
ACTIVE_USER_ORDERS = {}

# Часовой пояс Саратова (UTC+4)
SARATOV_TZ = timezone(timedelta(hours=4))

# === СЕРВЕРНЫЙ ПРАЙС-ЛИСТ (ЕДИНСТВЕННЫЙ ИСТОЧНИК ПРАВДЫ ДЛЯ ЦЕН) ===
SYRUP_PRICE = 30
HAS_ALT_MILK = {'m1', 'm2', 'm7', 'nc1', 'nc4'}
HAS_SYRUP_LIST = {'m1', 'm2', 'm7', 'b1', 'b7', 'nc1', 'nc4', 'nc5'}
HAS_BUMBLE_MODS = {'b2', 'b3', 'nc2', 'nc3'}
HAS_CINNAMON = {'m1', 'm2', 'm7', 'b1', 'nc1', 'm4'}
DRINK_CATEGORIES = {'seasonal', 'milk', 'black', 'not-coffee'}

MILK_LIST = {'Обычное', 'Кокосовое', 'Банановое', 'Миндальное', 'Безлактозное'}
SYRUPS_LIST = {
    'Без сиропа', 'Корица', 'Малина', 'Миндаль', 'Имбирный пряник',
    'Обжаренный лесной орех', 'Фисташка', 'Вишня', 'Ваниль',
    'Соленая карамель', 'Кокос', 'Лесной орех', 'Карамель',
    'Лаванда', 'Груша', 'Клубника', 'Черника', 'Зеленое яблоко',
    'Грецкий орех', 'Темный шоколад', 'Персик', 'Белый шоколад',
    'Апельсин', 'Слива', 'Каштан', 'Тыква'
}
BUMBLE_SYRUPS = {'С сиропом', 'Без сиропа'}
BUMBLE_TEMPS = {'Горячий', 'Со льдом', 'Без льда'}

MENU_CATALOG = {
    # СЕЗОННОЕ МЕНЮ
    's1': {'title': 'Латте пряная тыква', 'category': 'seasonal', 'variants': [('300 мл', 280), ('400 мл', 330)]},
    's2': {'title': 'Фильтр слива', 'category': 'seasonal', 'variants': [('200 мл', 240), ('300 мл', 280)]},
    's3': {'title': 'Какао апельсин-перец', 'category': 'seasonal', 'variants': [('200 мл', 340), ('300 мл', 410)]},
    's4': {'title': 'Раф каштан-мед', 'category': 'seasonal', 'variants': [('300 мл', 360), ('400 мл', 420)]},
    's5': {'title': 'Капучино груша-кардамон', 'category': 'seasonal', 'variants': [('300 мл', 290), ('400 мл', 390)]},

    # С МОЛОКОМ
    'm1': {'title': 'Капучино', 'category': 'milk', 'variants': [('200 мл', 240), ('300 мл', 280), ('400 мл', 350)]},
    'm2': {'title': 'Латте', 'category': 'milk', 'variants': [('300 мл', 260), ('400 мл', 290)]},
    'm3': {'title': 'Раф арахис', 'category': 'milk', 'variants': [('200 мл', 350), ('300 мл', 410)]},
    'm4': {'title': 'Раф классика', 'category': 'milk', 'variants': [('300 мл', 310), ('400 мл', 340)]},
    'm5': {'title': 'Раф марципан', 'category': 'milk', 'variants': [('200 мл', 370), ('300 мл', 410)]},
    'm6': {'title': 'Раф фисташка', 'category': 'milk', 'variants': [('200 мл', 390), ('300 мл', 430)]},
    'm7': {'title': 'Флэт уайт', 'category': 'milk', 'variants': [('200 мл', 260)]},

    # ЧЕРНЫЙ
    'b1': {'title': 'Американо', 'category': 'black', 'variants': [('200 мл', 230), ('300 мл', 260), ('400 мл', 290)]},
    'b2': {'title': 'Бамбл апельсин на соке', 'category': 'black', 'variants': [('300 мл', 350), ('400 мл', 390)]},
    'b3': {'title': 'Бамбл апельсин на фреше', 'category': 'black', 'variants': [('300 мл', 430), ('400 мл', 490)]},
    'b4': {'title': 'Бамбл гранатовый', 'category': 'black', 'variants': [('300 мл', 390)]},
    'b5': {'title': 'Фильтр', 'category': 'black', 'variants': [('200 мл', 220), ('300 мл', 260)]},
    'b6': {'title': 'Эспрессо', 'category': 'black', 'variants': [('36 мл', 190)]},
    'b7': {'title': 'Эспрессо-тоник', 'category': 'black', 'variants': [('300 мл', 340), ('400 мл', 390)]},

    # НЕ КОФЕ
    'nc1': {'title': 'Какао', 'category': 'not-coffee', 'variants': [('200 мл', 310), ('300 мл', 360)]},
    'nc2': {'title': 'Матча-апельсин', 'category': 'not-coffee', 'variants': [('300 мл', 370)]},
    'nc3': {'title': 'Матча-апельсин фреш', 'category': 'not-coffee', 'variants': [('300 мл', 470)]},
    'nc4': {'title': 'Матча-латте', 'category': 'not-coffee', 'variants': [('200 мл', 270), ('300 мл', 310)]},
    'nc5': {'title': 'Матча-тоник', 'category': 'not-coffee', 'variants': [('300 мл', 310), ('400 мл', 350)]},
    'nc6': {'title': 'Молочный коктейль ваниль', 'category': 'not-coffee', 'variants': [('300 мл', 380), ('400 мл', 430)]},
    'nc7': {'title': 'Молочный коктейль шоколад', 'category': 'not-coffee', 'variants': [('300 мл', 380), ('400 мл', 430)]},
    'nc8': {'title': 'Молочный коктейль ягоды', 'category': 'not-coffee', 'variants': [('300 мл', 390), ('400 мл', 440)]},
    'nc9': {'title': 'Смузи зелёный', 'category': 'not-coffee', 'variants': [('300 мл', 420), ('400 мл', 460)]},
    'nc10': {'title': 'Смузи клубника-банан', 'category': 'not-coffee', 'variants': [('300 мл', 420), ('400 мл', 460)]},
    'nc11': {'title': 'Смузи ягодный', 'category': 'not-coffee', 'variants': [('300 мл', 420), ('400 мл', 460)]},

    # СЫТНЫЕ ЗАВТРАКИ
    'hb1': {'title': 'Английский завтрак', 'category': 'hearty-bf', 'variants': [('', 610)]},
    'hb2': {'title': 'Белковый омлет с креветками', 'category': 'hearty-bf', 'variants': [('', 650)]},
    'hb3': {'title': 'Дальневосточный завтрак', 'category': 'hearty-bf', 'variants': [('', 810)]},
    'hb4': {'title': 'Зеленый омлет с сыром фета', 'category': 'hearty-bf', 'variants': [('', 390)]},
    'hb5': {'title': 'Блинчики с курицей', 'category': 'hearty-bf', 'variants': [('', 450)]},
    'hb6': {'title': 'Блинчики с лососем', 'category': 'hearty-bf', 'variants': [('', 580)]},
    'hb7': {'title': 'Скрембл с креветками и беконом', 'category': 'hearty-bf', 'variants': [('', 790)]},
    'hb8': {'title': 'Сырник классический, 1 шт', 'category': 'hearty-bf', 'variants': [('', 100)]},
    'hb9': {'title': 'Драники с лососем', 'category': 'hearty-bf', 'variants': [('', 630)]},
    'hb10': {'title': 'Драники с курицей в сливочном соусе', 'category': 'hearty-bf', 'variants': [('', 490)]},
    'hb11': {'title': 'Круассан с курицей и скремблом', 'category': 'hearty-bf', 'variants': [('', 530)]},
    'hb12': {'title': 'Круассан с лососем и творожным сыром', 'category': 'hearty-bf', 'variants': [('', 650)]},
    'hb13': {'title': 'Оладьи из цукини с запеченной курицей', 'category': 'hearty-bf', 'variants': [('', 510)]},
    'hb14': {'title': 'Оладьи из цукини с пряной сметаной', 'category': 'hearty-bf', 'variants': [('', 460)]},
    'hb15': {'title': 'Скрембл с запеченной курицей в сливочном соусе', 'category': 'hearty-bf', 'variants': [('', 610)]},

    # СЛАДКИЕ ЗАВТРАКИ
    'sb1': {'title': 'Бельгийская рисовая каша', 'category': 'sweet-bf', 'variants': [('', 350)]},
    'sb2': {'title': 'Блинчики креп-сюзетт', 'category': 'sweet-bf', 'variants': [('', 400)]},
    'sb3': {'title': 'Блинчики с крем-чизом и ежевичным соусом', 'category': 'sweet-bf', 'variants': [('', 410)]},
    'sb4': {'title': 'Креп-паста с пломбиром', 'category': 'sweet-bf', 'variants': [('', 530)]},
    'sb5': {'title': 'Пшенная каша на топленом молоке с ягодами', 'category': 'sweet-bf', 'variants': [('', 390)]},
    'sb6': {'title': 'Сырники с крем-брюле', 'category': 'sweet-bf', 'variants': [('', 390)]},
    'sb7': {'title': 'Сырники с крем-чизом и манговым конфи', 'category': 'sweet-bf', 'variants': [('', 410)]},
    'sb8': {'title': 'Шоколадная овсянка с бананом', 'category': 'sweet-bf', 'variants': [('', 350)]},

    # ПАСТА
    'p1': {'title': 'Паста с креветками и томатами', 'category': 'pasta', 'variants': [('', 790)]},
    'p2': {'title': 'Паста с курицей и грибами', 'category': 'pasta', 'variants': [('', 650)]},

    # САЛАТЫ
    'sa1': {'title': 'Зеленый салат со страчателлой', 'category': 'salads', 'variants': [('', 450)]},
    'sa2': {'title': 'Салат креветки с апельсинами', 'category': 'salads', 'variants': [('', 750)]},
    'sa3': {'title': 'Цезарь с креветками', 'category': 'salads', 'variants': [('', 650)]},
    'sa4': {'title': 'Цезарь с курицей', 'category': 'salads', 'variants': [('', 570)]},
}

app = Flask(__name__)
# Разрешаем запросы только с нашего GitHub Pages
CORS(
    app,
    resources={r"/*": {"origins": ["https://calidocoffee.github.io"]}},
    allow_headers=["Content-Type", "X-Telegram-Init-Data"]
)

@app.route('/', methods=['GET'])
def index():
    return "Calido Server is running!", 200

def verify_telegram_init_data(init_data_str):
    """Проверяет криптографическую подпись Telegram WebApp и возвращает словарь user."""
    if not init_data_str or not TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data_str, keep_blank_values=True))
        received_hash = parsed.pop('hash', None)
        if not received_hash:
            return None

        # Проверяем, что данные не старше 24 часов
        auth_date = int(parsed.get('auth_date', 0))
        if abs(time.time() - auth_date) > 86400:
            return None

        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode('utf-8'), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        user_json = parsed.get('user')
        if not user_json:
            return None
        return json.loads(user_json)
    except Exception as e:
        print(f"Ошибка валидации initData: {e}")
        return None

def get_saratov_now():
    return datetime.now(SARATOV_TZ)

def get_alt_milk_extra(size_str):
    if '200' in size_str:
        return 80
    if '300' in size_str:
        return 100
    if '400' in size_str:
        return 120
    return 0

def calculate_server_item(item_data):
    """Пересчитывает название, тип и цену позиции строго по серверному прайсу."""
    cfg = item_data.get('config') or {}
    prod_id = cfg.get('prodId') or item_data.get('prodId')
    if prod_id not in MENU_CATALOG:
        raise ValueError(f"Неизвестный товар: {prod_id}")

    product = MENU_CATALOG[prod_id]
    variants = product['variants']
    var_idx = int(cfg.get('varIdx', 0))
    if var_idx < 0 or var_idx >= len(variants):
        var_idx = 0

    size_str, base_price = variants[var_idx]
    is_drink = product['category'] in DRINK_CATEGORIES
    price = base_price
    mods = []

    if size_str:
        mods.append(size_str)

    if prod_id in HAS_ALT_MILK:
        milk = cfg.get('milk', 'Обычное')
        if milk not in MILK_LIST:
            milk = 'Обычное'
        if milk != 'Обычное':
            price += get_alt_milk_extra(size_str)
            mods.append(f"молоко: {milk.lower()}")

    if prod_id in HAS_SYRUP_LIST:
        syrup = cfg.get('syrup', 'Без сиропа')
        if syrup not in SYRUPS_LIST:
            syrup = 'Без сиропа'
        if syrup != 'Без сиропа':
            price += SYRUP_PRICE
            mods.append(f"сироп: {syrup.lower()}")

    if prod_id in HAS_BUMBLE_MODS:
        b_syrup = cfg.get('bumbleSyrup', 'С сиропом')
        b_temp = cfg.get('temp', 'Со льдом')
        if b_syrup in BUMBLE_SYRUPS:
            mods.append(b_syrup.lower())
        if b_temp in BUMBLE_TEMPS:
            mods.append(b_temp.lower())

    if prod_id in HAS_CINNAMON and cfg.get('cinnamon'):
        mods.append('+корица')

    if is_drink:
        sugar = int(cfg.get('sugar', 0))
        if 1 <= sugar <= 3:
            mods.append(f"сахар: {sugar}")

    title = f"{product['title']} ({', '.join(mods)})" if mods else product['title']
    return title, price, is_drink

def build_order_data(items, payment, user_dict, ready_label):
    if not items or not isinstance(items, dict):
        raise ValueError("Корзина пуста")

    drinks_receipt = ""
    foods_receipt = ""
    total_sum = 0
    prices = []
    short_items_list = []
    
    for item_id, item_data in items.items():
        count = int(item_data.get('count', 0))
        if count <= 0 or count > 50:
            raise ValueError("Некорректное количество товара")

        title, price, is_drink = calculate_server_item(item_data)
        sum_item = count * price
        total_sum += sum_item
        
        short_items_list.append(f"{title} × {count}")
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
        # Проверяем личность гостя по подписи Telegram
        init_data = request.headers.get('X-Telegram-Init-Data', '')
        user = verify_telegram_init_data(init_data)
        if not user or not user.get('id'):
            return jsonify({'ok': False, 'error': 'Ошибка авторизации Telegram. Переоткройте меню через кнопку Open.'}), 401

        chat_id = user['id']
        data = request.get_json(force=True)
        items = data.get('items', {})
        payment = data.get('payment', 'Картой онлайн')
        
        ready_label = data.get('ready_label', 'Как можно скорее')
        target_hhmm = data.get('target_hhmm', '')
        prep_minutes = int(data.get('prep_minutes', 10))
        is_delayed = bool(data.get('is_delayed', False))
        
        structured_receipt, user_info, prices, total_sum, short_items_list = build_order_data(items, payment, user, ready_label)
        
        order_key = f"order_{chat_id}_{int(time.time())}"
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
    except ValueError as ve:
        return jsonify({'ok': False, 'error': str(ve)}), 400
    except Exception as e:
        print(f"Ошибка в /create_order: {e}")
        return jsonify({'ok': False, 'error': 'Внутренняя ошибка сервера'}), 500

@app.route('/order_status', methods=['GET'])
def get_order_status():
    init_data = request.headers.get('X-Telegram-Init-Data', '')
    user = verify_telegram_init_data(init_data)
    if not user or not user.get('id'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    user_id = str(user['id'])
    order = ACTIVE_USER_ORDERS.get(user_id)
    if not order:
        return jsonify({'ok': True, 'active_order': None})
    
    now_ts = time.time()
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
    web_app = WebAppInfo(url="https://calidocoffee.github.io/calido-menu/?v=22") 
    markup.add(KeyboardButton(text="☕️ Меню Calido coffee", web_app=web_app))
    
    welcome_text = "Привет! Это Calido coffee🤎 Твой вкусный кофе и завтраки уже ждут в меню!"
    photo_url = "https://images.unsplash.com/photo-1501339847302-ac426a4a7cbb?w=800&q=80"
    
    try:
        bot.send_photo(
            chat_id=message.chat.id,
            photo=photo_url,
            caption=welcome_text,
            reply_markup=markup,
            parse_mode='HTML'
        )
    except Exception:
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
        print(f"Ошибка в web_app_data: {e}")
        bot.send_message(message.chat.id, "⚠️ Не удалось обработать заказ. Попробуйте ещё раз.")

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

def send_delayed_cooking_reminder(admin_id, client_chat_id, target_hhmm, suma, full_order_text):
    try:
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
                f"🕒 Оплачено в: {paid_time_str} ({suma} ₽)\n\n"
                f"{full_order_text}"
            )
            bot.send_message(ADMIN_ID, pre_text, parse_mode='HTML')
            
            timer = threading.Timer(
                delay_seconds, 
                send_delayed_cooking_reminder, 
                args=(
