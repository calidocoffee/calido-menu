import os
import json
import hmac
import html
import uuid
import hashlib
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl
from datetime import datetime, timedelta, timezone
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import telebot
from telebot.types import (
    WebAppInfo, LabeledPrice, InlineKeyboardMarkup,
    InlineKeyboardButton, ReplyKeyboardRemove
)
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from waitress import serve

TOKEN = os.environ.get('BOT_TOKEN')
PAYMENT_TOKEN = os.environ.get('PAYMENT_TOKEN')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))
BARISTA_CHAT_ID = int(os.environ.get('BARISTA_CHAT_ID', 0))
DATABASE_URL = os.environ.get('DATABASE_URL')
# Код ставки НДС для ЮKassa: 1 = Без НДС (УСН/Патент)
YOOKASSA_VAT_CODE = int(os.environ.get('YOOKASSA_VAT_CODE', 1))

bot = telebot.TeleBot(TOKEN)

# Часовой пояс Саратова (UTC+4)
SARATOV_TZ = timezone(timedelta(hours=4))

# === ПУЛ СОЕДИНЕНИЙ POSTGRESQL С ЗАЩИТОЙ ОТ РАЗРЫВА ===
db_pool = None

def init_pool():
    global db_pool
    if not DATABASE_URL:
        raise RuntimeError("Не задана переменная окружения DATABASE_URL")
    db_pool = pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=20,
        dsn=DATABASE_URL,
        cursor_factory=RealDictCursor
    )

def get_fresh_conn():
    if db_pool is None:
        init_pool()
    conn = db_pool.getconn()
    try:
        if conn.closed:
            db_pool.putconn(conn, close=True)
            conn = db_pool.getconn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except Exception:
        try:
            db_pool.putconn(conn, close=True)
        except Exception:
            pass
        return db_pool.getconn()

@contextmanager
def get_db_conn():
    conn = get_fresh_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        db_pool.putconn(conn, close=bool(conn.closed))

def init_db():
    init_pool()
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_key TEXT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    text TEXT NOT NULL,
                    structured_receipt TEXT,
                    ready_label TEXT,
                    target_hhmm TEXT,
                    prep_minutes INTEGER NOT NULL DEFAULT 10,
                    is_delayed BOOLEAN NOT NULL DEFAULT FALSE,
                    has_food BOOLEAN NOT NULL DEFAULT FALSE,
                    total_sum INTEGER NOT NULL,
                    short_items TEXT,
                    prod_ids TEXT,
                    payment_charge_id TEXT,
                    paid_time TEXT,
                    paid_at DOUBLE PRECISION,
                    ready_at DOUBLE PRECISION,
                    remind_at DOUBLE PRECISION,
                    created_at DOUBLE PRECISION NOT NULL
                );
            """)
            cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS structured_receipt TEXT;")
            cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS prod_ids TEXT;")
            cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_charge_id TEXT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_chat_status ON orders (chat_id, status);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_remind ON orders (status, remind_at);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_pending_cleanup ON orders (status, created_at);")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS stop_list (
                    prod_id TEXT PRIMARY KEY
                );
            """)

# === КЭШ СТОП-ЛИСТА (10 СЕК) ===
_stoplist_lock = threading.Lock()
_stoplist_cache = set()
_stoplist_cache_ts = 0.0
STOPLIST_CACHE_TTL = 10.0

def invalidate_stoplist_cache():
    global _stoplist_cache_ts
    with _stoplist_lock:
        _stoplist_cache_ts = 0.0

def get_stoplist_set(force_refresh=False):
    global _stoplist_cache, _stoplist_cache_ts
    now_ts = time.time()
    with _stoplist_lock:
        if not force_refresh and (now_ts - _stoplist_cache_ts) < STOPLIST_CACHE_TTL:
            return set(_stoplist_cache)
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT prod_id FROM stop_list")
                fresh = {row['prod_id'] for row in cur.fetchall()}
        with _stoplist_lock:
            _stoplist_cache = fresh
            _stoplist_cache_ts = time.time()
            return set(_stoplist_cache)
    except Exception as e:
        print(f"Ошибка чтения stop_list: {e}")
        with _stoplist_lock:
            return set(_stoplist_cache)

# === СЕРВЕРНЫЙ ПРАЙС-ЛИСТ (ЕДИНСТВЕННЫЙ ИСТОЧНИК ПРАВДЫ ДЛЯ ЦЕН) ===
SYRUP_PRICE = 30
PORRIDGE_ALT_MILK_PRICE = 100

HAS_ALT_MILK = {'m1', 'm2', 'm7', 'nc1', 'nc4'}
HAS_SYRUP_LIST = {'m1', 'm2', 'm7', 'b1', 'b7', 'nc1', 'nc4', 'nc5'}
HAS_BUMBLE_MODS = {'b2', 'b3', 'nc2', 'nc3'}
HAS_CINNAMON = {'m1', 'm2', 'm7', 'b1', 'nc1', 'm4'}
DRINK_CATEGORIES = {'seasonal', 'milk', 'black', 'not-coffee'}
FAST_DRINK_IDS = {'s2', 'b1', 'b5', 'b6'}

HAS_EGG_CHOICE = {'hb1', 'hb3'}
EGG_CHOICES = {'Глазунья', 'Омлет'}
PORRIDGE_IDS = {'sb1', 'sb5', 'sb8'}

HEARTY_ADDONS = {
    'Лосось 40 г': 150,
    'Охотничья колбаска 60 г': 120,
    'Креветки 50 г': 150,
    'Бриошь 50 г': 30,
    'Бекон 30 г': 50,
}

SWEET_ADDONS = {
    'Сметана 50 г': 50,
    'Ежевичный соус 50 г': 50,
    'Манговое конфи 50 г': 50,
    'Молоко сгущенное 50 г': 50,
}

CATEGORY_LABELS = {
    'seasonal': '⚡️ Сезонное меню',
    'milk': '🥛 С молоком',
    'black': '☕️ Черный',
    'not-coffee': '🍵 Не кофе',
    'hearty-bf': '🍳 Сытные завтраки',
    'sweet-bf': '🥞 Сладкие завтраки',
    'pasta': '🍝 Паста',
    'salads': '🥗 Салаты',
}

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
    'hb9': {'title': 'Драники с лососем', 'category': 'hearty-bf', 'variants': [('', 630)]},
    'hb10': {'title': 'Драники с курицей в сливочном соусе', 'category': 'hearty-bf', 'variants': [('', 490)]},
    'hb11': {'title': 'Круассан с курицей и скремблом', 'category': 'hearty-bf', 'variants': [('', 530)]},
    'hb12': {'title': 'Круассан с лососем и творожным сыром', 'category': 'hearty-bf', 'variants': [('', 650)]},
    'hb13': {'title': 'Оладьи из цукини с запеченной курицей', 'category': 'hearty-bf', 'variants': [('', 510)]},
    'hb14': {'title': 'Оладьи из цукини с пряной сметаной', 'category': 'hearty-bf', 'variants': [('', 460)]},
    'hb15': {'title': 'Скрембл с запеченной курицей в сливочном соусе', 'category': 'hearty-bf', 'variants': [('', 610)]},
    'hb16': {'title': 'Омлет с хрустящей курочкой', 'category': 'hearty-bf', 'variants': [('', 510)]},

    # СЛАДКИЕ ЗАВТРАКИ
    'hb8': {'title': 'Сырник классический, 1 шт', 'category': 'sweet-bf', 'variants': [('', 100)]},
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
app.config['MAX_CONTENT_LENGTH'] = 65536
CORS(
    app,
    resources={r"/*": {"origins": ["https://calidocoffee.github.io"]}},
    allow_headers=["Content-Type", "X-Telegram-Init-Data"]
)

@app.errorhandler(413)
def handle_too_large(e):
    return jsonify({'ok': False, 'error': 'Слишком большой запрос.'}), 413

def to_int(val, default=None):
    if isinstance(val, bool):
        return default
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        return default

@app.route('/', methods=['GET'])
def index():
    return "Calido Server is running!", 200

@app.route('/stoplist', methods=['GET'])
def get_stoplist_api():
    return jsonify({'ok': True, 'stoplist': list(get_stoplist_set())})

def verify_telegram_init_data(init_data_str):
    if not init_data_str or not TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data_str, keep_blank_values=True))
        received_hash = parsed.pop('hash', None)
        if not received_hash:
            return None

        auth_date = to_int(parsed.get('auth_date'), 0)
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

def format_min_to_hhmm(total_min):
    return f"{total_min // 60:02d}:{total_min % 60:02d}"

def get_schedule_rules(now_dt):
    is_weekend = now_dt.weekday() in (5, 6)
    now_min = now_dt.hour * 60 + now_dt.minute
    return {
        'now_min': now_min,
        'drinks_open': 8 * 60 if is_weekend else 7 * 60 + 30,
        'drinks_close': 21 * 60,
        'kitchen_open': 9 * 60 if is_weekend else 7 * 60 + 30,
        'kitchen_close': 15 * 60 if is_weekend else 14 * 60 + 45,
    }

def validate_order_schedule(target_hhmm, prep_minutes, has_food, now_dt):
    rules = get_schedule_rules(now_dt)
    now_min = rules['now_min']

    if now_min >= rules['drinks_close']:
        raise ValueError("Кофейня уже закрыта (работаем до 21:00).")
    if has_food and now_min >= rules['kitchen_close']:
        raise ValueError(f"Кухня работает до {format_min_to_hhmm(rules['kitchen_close'])}. Удалите блюда кухни из корзины.")

    base_open = max(rules['drinks_open'], rules['kitchen_open']) if has_food else rules['drinks_open']
    limit_close = rules['kitchen_close'] if has_food else rules['drinks_close']

    effective_start = max(now_min, base_open)
    earliest_ready = effective_start + prep_minutes

    if earliest_ready > limit_close:
        raise ValueError(f"До закрытия ({format_min_to_hhmm(limit_close)}) не успеем приготовить этот заказ.")

    if isinstance(target_hhmm, str) and ':' in target_hhmm:
        try:
            parts = target_hhmm.split(':')
            if len(parts) != 2:
                raise ValueError()
            th, tm = int(parts[0]), int(parts[1])
            if not (0 <= th <= 23 and 0 <= tm <= 59):
                raise ValueError()
            target_min = th * 60 + tm
        except ValueError:
            raise ValueError("Некорректный формат времени готовности.")

        if target_min < earliest_ready - 2:
            raise ValueError("Выбранное время уже недоступно. Обновите экран оформления.")
        if target_min > limit_close:
            raise ValueError(f"Выбранное время выходит за часы работы (до {format_min_to_hhmm(limit_close)}).")

        normalized_hhmm = format_min_to_hhmm(target_min)
        is_delayed = (target_min - now_min) > 25 or (now_min < base_open)

        if now_min < base_open and target_min <= earliest_ready + 2:
            server_ready_label = f"К открытию ({normalized_hhmm})"
        elif target_min <= earliest_ready + 2:
            server_ready_label = f"Как можно скорее (к {normalized_hhmm})"
        else:
            server_ready_label = f"К {normalized_hhmm}"

        return normalized_hhmm, is_delayed, server_ready_label

    normalized_hhmm = format_min_to_hhmm(earliest_ready)
    is_delayed = now_min < base_open
    server_ready_label = f"К открытию ({normalized_hhmm})" if is_delayed else f"Как можно скорее (к {normalized_hhmm})"
    return normalized_hhmm, is_delayed, server_ready_label

def get_alt_milk_extra(size_str):
    if '200' in size_str:
        return 80
    if '300' in size_str:
        return 100
    if '400' in size_str:
        return 120
    return 0

def calculate_server_item(item_data):
    if not isinstance(item_data, dict):
        raise ValueError("Некорректный формат позиции.")

    cfg = item_data.get('config')
    if not isinstance(cfg, dict):
        cfg = {}

    prod_id = cfg.get('prodId') or item_data.get('prodId')
    if not isinstance(prod_id, str) or prod_id not in MENU_CATALOG:
        raise ValueError("Неизвестный товар в корзине.")

    product = MENU_CATALOG[prod_id]
    variants = product['variants']
    var_idx = to_int(cfg.get('varIdx'), 0)
    if var_idx is None or var_idx < 0 or var_idx >= len(variants):
        var_idx = 0

    size_str, base_price = variants[var_idx]
    category = product['category']
    is_drink = category in DRINK_CATEGORIES
    price = base_price
    mods = []

    if size_str:
        mods.append(size_str)

    if prod_id in HAS_ALT_MILK:
        milk = cfg.get('milk', 'Обычное')
        if not isinstance(milk, str) or milk not in MILK_LIST:
            milk = 'Обычное'
        if milk != 'Обычное':
            price += get_alt_milk_extra(size_str)
            mods.append(f"молоко: {milk.lower()}")

    if prod_id in PORRIDGE_IDS:
        milk = cfg.get('milk', 'Обычное')
        if not isinstance(milk, str) or milk not in MILK_LIST:
            milk = 'Обычное'
        if milk != 'Обычное':
            price += PORRIDGE_ALT_MILK_PRICE
            mods.append(f"молоко: {milk.lower()}")

    if prod_id in HAS_SYRUP_LIST:
        syrup = cfg.get('syrup', 'Без сиропа')
        if not isinstance(syrup, str) or syrup not in SYRUPS_LIST:
            syrup = 'Без сиропа'
        if syrup != 'Без сиропа':
            price += SYRUP_PRICE
            mods.append(f"сироп: {syrup.lower()}")

    if prod_id in HAS_BUMBLE_MODS:
        b_syrup = cfg.get('bumbleSyrup', 'С сиропом')
        b_temp = cfg.get('temp', 'Со льдом')
        if isinstance(b_syrup, str) and b_syrup in BUMBLE_SYRUPS:
            mods.append(b_syrup.lower())
        if isinstance(b_temp, str) and b_temp in BUMBLE_TEMPS:
            mods.append(b_temp.lower())

    if prod_id in HAS_CINNAMON and cfg.get('cinnamon') is True:
        mods.append('+корица')

    if is_drink:
        sugar = to_int(cfg.get('sugar'), 0)
        if sugar is not None and 1 <= sugar <= 3:
            mods.append(f"сахар: {sugar}")

    if prod_id in HAS_EGG_CHOICE:
        egg_type = cfg.get('eggType', 'Глазунья')
        if not isinstance(egg_type, str) or egg_type not in EGG_CHOICES:
            egg_type = 'Глазунья'
        mods.append(egg_type.lower())

    raw_addons = cfg.get('addons')
    if isinstance(raw_addons, list) and raw_addons:
        allowed_addons = None
        if category == 'hearty-bf':
            allowed_addons = HEARTY_ADDONS
        elif category == 'sweet-bf':
            allowed_addons = SWEET_ADDONS

        if allowed_addons:
            requested_set = {a for a in raw_addons if isinstance(a, str)}
            for addon_name, addon_price in allowed_addons.items():
                if addon_name in requested_set:
                    price += addon_price
                    mods.append(f"+{addon_name.lower()}")

    title = f"{product['title']} ({', '.join(mods)})" if mods else product['title']
    return prod_id, title, price, is_drink

def calculate_prep_minutes(fast_count, reg_count, food_count):
    fast_min = 0
    if 1 <= fast_count <= 2:
        fast_min = 5
    elif 3 <= fast_count <= 4:
        fast_min = 7
    elif fast_count >= 5:
        fast_min = 10

    reg_min = 0
    if reg_count == 1:
        reg_min = 7
    elif 2 <= reg_count <= 3:
        reg_min = 12
    elif reg_count >= 4:
        reg_min = 20

    food_min = 0
    if food_count == 1:
        food_min = 12
    elif food_count == 2:
        food_min = 17
    elif food_count >= 3:
        food_min = 20

    return max(fast_min, reg_min, food_min, 5)

def build_order_data(items, user_dict, raw_target_hhmm):
    if not items or not isinstance(items, dict):
        raise ValueError("Корзина пуста")
    if len(items) > 30:
        raise ValueError("В одном заказе может быть не более 30 разных позиций.")

    stoplist = get_stoplist_set()
    drinks_receipt = ""
    foods_receipt = ""
    total_sum = 0
    prices = []
    receipt_items = []
    short_items_list = []
    prod_ids = []

    fast_count = 0
    reg_count = 0
    food_count = 0

    for item_id, item_data in items.items():
        if not isinstance(item_data, dict):
            raise ValueError("Некорректный формат позиции в корзине.")
        count = to_int(item_data.get('count'), 0)
        if count is None or count <= 0 or count > 50:
            raise ValueError("Некорректное количество товара.")

        prod_id, title, price, is_drink = calculate_server_item(item_data)
        if prod_id in stoplist:
            base_title = MENU_CATALOG[prod_id]['title']
            raise ValueError(f"Позиция «{base_title}» сейчас отсутствует. Удалите её из корзины.")

        prod_ids.append(prod_id)
        sum_item = count * price
        total_sum += sum_item

        if is_drink:
            if prod_id in FAST_DRINK_IDS:
                fast_count += count
            else:
                reg_count += count
        else:
            food_count += count

        short_items_list.append(f"{title} × {count}")
        safe_title = html.escape(title)
        item_line = f"▫️ <b>{safe_title}</b>\n   Количество: <b>{count} шт.</b> | Сумма: <b>{sum_item} ₽</b>\n\n"

        if is_drink:
            drinks_receipt += item_line
        else:
            foods_receipt += item_line

        prices.append(LabeledPrice(label=f"{title} x{count}"[:32], amount=sum_item * 100))

        receipt_items.append({
            "description": title[:128],
            "quantity": f"{count}.00",
            "amount": {
                "value": f"{price}.00",
                "currency": "RUB"
            },
            "vat_code": YOOKASSA_VAT_CODE,
            "payment_mode": "full_payment",
            "payment_subject": "commodity"
        })

    prep_minutes = calculate_prep_minutes(fast_count, reg_count, food_count)
    has_food = food_count > 0
    target_hhmm, is_delayed, ready_label = validate_order_schedule(
        raw_target_hhmm, prep_minutes, has_food, get_saratov_now()
    )

    payment_label = "Картой онлайн"
    safe_ready_label = html.escape(ready_label)

    structured_receipt = ""
    if drinks_receipt:
        structured_receipt += "☕️ <b>НАПИТКИ:</b>\n\n" + drinks_receipt
    if foods_receipt:
        structured_receipt += "🍽 <b>ЕДА И ЗАВТРАКИ:</b>\n\n" + foods_receipt

    structured_receipt += f"⏰ <b>Время готовности: {safe_ready_label}</b>\n"
    structured_receipt += f"💰 <b>Итого: {total_sum} ₽</b>\n"
    structured_receipt += f"💳 Способ оплаты: <b>{payment_label}</b>"

    raw_username = user_dict.get('username')
    safe_username = f"@{html.escape(str(raw_username))}" if raw_username else "без юзернейма"
    safe_first_name = html.escape(str(user_dict.get('first_name') or 'Гость'))
    user_info = f"👤 <b>Клиент:</b> {safe_first_name} ({safe_username})\n\n"

    return (
        structured_receipt, user_info, prices, receipt_items, total_sum,
        short_items_list, prep_minutes, has_food, prod_ids,
        target_hhmm, is_delayed, ready_label
    )

def save_pending_order(order_key, chat_id, full_text, structured_receipt, ready_label, target_hhmm, prep_minutes, is_delayed, has_food, total_sum, short_items, prod_ids):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = 'expired' WHERE chat_id = %s AND status = 'pending'", (chat_id,))
            cur.execute("""
                INSERT INTO orders (
                    order_key, chat_id, status, text, structured_receipt, ready_label, target_hhmm,
                    prep_minutes, is_delayed, has_food, total_sum, short_items, prod_ids, created_at
                ) VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                order_key, chat_id, full_text, structured_receipt, ready_label, target_hhmm,
                prep_minutes, is_delayed, has_food, total_sum, short_items, json.dumps(prod_ids), time.time()
            ))

@app.route('/create_order', methods=['POST'])
def create_order():
    try:
        init_data = request.headers.get('X-Telegram-Init-Data', '')
        user = verify_telegram_init_data(init_data)
        if not user or not user.get('id'):
            return jsonify({'ok': False, 'error': 'Ошибка авторизации Telegram. Переоткройте меню через кнопку Open.'}), 401

        chat_id = user['id']
        data = request.get_json(force=True, silent=True)
        if not isinstance(data, dict):
            return jsonify({'ok': False, 'error': 'Некорректный формат запроса.'}), 400

        items = data.get('items', {})
        raw_target_hhmm = data.get('target_hhmm', '')

        (
            structured_receipt, user_info, prices, receipt_items, total_sum,
            short_items_list, prep_minutes, has_food, prod_ids,
            target_hhmm, is_delayed, ready_label
        ) = build_order_data(items, user, raw_target_hhmm)

        order_key = f"order_{chat_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        save_pending_order(
            order_key, chat_id, user_info + structured_receipt, structured_receipt, ready_label,
            target_hhmm, prep_minutes, is_delayed, has_food, total_sum, ", ".join(short_items_list), prod_ids
        )

        provider_data = json.dumps({
            "receipt": {
                "items": receipt_items
            }
        }, ensure_ascii=False)

        invoice_link = bot.create_invoice_link(
            title="Заказ в Calido Coffee",
            description=f"Самовывоз (Рабочая, 49) • {ready_label}",
            payload=order_key,
            provider_token=PAYMENT_TOKEN,
            currency="RUB",
            prices=prices,
            provider_data=provider_data,
            need_email=True,
            send_email_to_provider=True
        )

        return jsonify({
            'ok': True,
            'invoice_link': invoice_link,
            'order_key': order_key,
            'total_sum': total_sum
        })
    except HTTPException:
        raise
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

    chat_id = user['id']
    now_ts = time.time()
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT order_key, status, ready_label, target_hhmm, total_sum,
                           paid_time, paid_at, ready_at, short_items
                    FROM orders
                    WHERE chat_id = %s AND status IN ('scheduled', 'cooking', 'ready')
                    ORDER BY paid_at DESC
                    LIMIT 1
                """, (chat_id,))
                order = cur.fetchone()
    except Exception as e:
        print(f"Ошибка чтения /order_status: {e}")
        return jsonify({'ok': True, 'active_order': None})

    if not order:
        return jsonify({'ok': True, 'active_order': None})

    if order['status'] == 'ready' and order['ready_at'] and (now_ts - order['ready_at']) > 2700:
        return jsonify({'ok': True, 'active_order': None})
    if order['paid_at'] and (now_ts - order['paid_at']) > 43200:
        return jsonify({'ok': True, 'active_order': None})

    return jsonify({'ok': True, 'active_order': dict(order)})

# === ПРАВА ДОСТУПА И ЧАТЫ ===
def is_admin_chat(chat_id, user_id):
    return ADMIN_ID != 0 and (chat_id == ADMIN_ID or user_id == ADMIN_ID)

def is_barista_or_admin(chat_id, user_id):
    if is_admin_chat(chat_id, user_id):
        return True
    return BARISTA_CHAT_ID != 0 and chat_id == BARISTA_CHAT_ID

def get_orders_chat_id():
    return BARISTA_CHAT_ID if BARISTA_CHAT_ID != 0 else ADMIN_ID

# === УПРАВЛЕНИЕ СТОП-ЛИСТОМ (ТОЛЬКО ДЛЯ ADMIN_ID) ===
def build_stoplist_main_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton(text=label, callback_data=f"sl_cat_{cat_id}")
        for cat_id, label in CATEGORY_LABELS.items()
    ]
    markup.add(*buttons)
    markup.add(InlineKeyboardButton(text="📋 Что сейчас в стопе", callback_data="sl_show"))
    markup.add(InlineKeyboardButton(text="🗑 Очистить весь стоп-лист", callback_data="sl_clear"))
    return markup

def build_stoplist_cat_markup(cat_id):
    stoplist = get_stoplist_set(force_refresh=True)
    markup = InlineKeyboardMarkup(row_width=1)
    for prod_id, p in MENU_CATALOG.items():
        if p['category'] == cat_id:
            icon = "🔴" if prod_id in stoplist else "🟢"
            markup.add(InlineKeyboardButton(
                text=f"{icon} {p['title']}",
                callback_data=f"sl_tog_{cat_id}_{prod_id}"
            ))
    markup.add(InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="sl_main"))
    return markup

@bot.message_handler(commands=['stoplist'])
def stoplist_command(message):
    if not is_admin_chat(message.chat.id, message.from_user.id):
        return
    count = len(get_stoplist_set(force_refresh=True))
    text = f"🛑 <b>Управление стоп-листом</b>\nСейчас в стопе позиций: <b>{count}</b>\n\nВыберите категорию:"
    bot.send_message(message.chat.id, text, parse_mode='HTML', reply_markup=build_stoplist_main_markup())

@bot.callback_query_handler(func=lambda call: call.data.startswith('sl_'))
def stoplist_callback(call):
    if not is_admin_chat(call.message.chat.id, call.from_user.id):
        bot.answer_callback_query(call.id, text="Нет доступа.")
        return

    data = call.data
    if data == 'sl_main':
        count = len(get_stoplist_set(force_refresh=True))
        text = f"🛑 <b>Управление стоп-листом</b>\nСейчас в стопе позиций: <b>{count}</b>\n\nВыберите категорию:"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=build_stoplist_main_markup())
        bot.answer_callback_query(call.id)

    elif data.startswith('sl_cat_'):
        cat_id = data[len('sl_cat_'):]
        cat_label = CATEGORY_LABELS.get(cat_id, cat_id)
        text = f"<b>{cat_label}</b>\nНажмите на позицию, чтобы убрать в стоп (🔴) или вернуть в меню (🟢):"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=build_stoplist_cat_markup(cat_id))
        bot.answer_callback_query(call.id)

    elif data.startswith('sl_tog_'):
        parts = data.split('_')
        cat_id = parts[2]
        prod_id = parts[3]
        if prod_id not in MENU_CATALOG:
            bot.answer_callback_query(call.id, text="Товар не найден.")
            return

        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT prod_id FROM stop_list WHERE prod_id = %s", (prod_id,))
                exists = cur.fetchone()
                if exists:
                    cur.execute("DELETE FROM stop_list WHERE prod_id = %s", (prod_id,))
                    status_msg = f"🟢 {MENU_CATALOG[prod_id]['title']} — снова в меню"
                else:
                    cur.execute("INSERT INTO stop_list (prod_id) VALUES (%s) ON CONFLICT DO NOTHING", (prod_id,))
                    status_msg = f"🔴 {MENU_CATALOG[prod_id]['title']} — убрано в стоп"

        invalidate_stoplist_cache()
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_stoplist_cat_markup(cat_id))
        bot.answer_callback_query(call.id, text=status_msg)

    elif data == 'sl_show':
        stoplist = get_stoplist_set(force_refresh=True)
        if not stoplist:
            text = "✅ <b>Стоп-лист пуст</b> — все позиции доступны в меню."
        else:
            lines = [f"🔴 {MENU_CATALOG[pid]['title']}" for pid in stoplist if pid in MENU_CATALOG]
            text = "🛑 <b>Сейчас в стоп-листе:</b>\n\n" + "\n".join(lines)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="sl_main"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        bot.answer_callback_query(call.id)

    elif data == 'sl_clear':
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM stop_list")
        invalidate_stoplist_cache()
        text = "🛑 <b>Управление стоп-листом</b>\nСейчас в стопе позиций: <b>0</b>\n\nВыберите категорию:"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=build_stoplist_main_markup())
        bot.answer_callback_query(call.id, text="Стоп-лист полностью очищен!")

@bot.message_handler(commands=['myid'])
def get_id(message):
    bot.send_message(message.chat.id, f"ID этого чата: `{message.chat.id}`", parse_mode='Markdown')

@bot.message_handler(commands=['start'])
def start(message):
    try:
        tmp = bot.send_message(message.chat.id, "⏳", reply_markup=ReplyKeyboardRemove())
        bot.delete_message(message.chat.id, tmp.message_id)
    except Exception:
        pass

    markup = InlineKeyboardMarkup()
    web_app = WebAppInfo(url="https://calidocoffee.github.io/calido-menu/?v=32")
    markup.add(InlineKeyboardButton(text="☕️ Открыть меню Calido coffee", web_app=web_app))

    welcome_text = "Привет! Это Calido coffee🤎 Твой вкусный кофе и завтраки уже ждут в меню!"
    bot.send_message(message.chat.id, welcome_text, reply_markup=markup, parse_mode='HTML')

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    try:
        order_key = pre_checkout_query.invoice_payload
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM orders WHERE order_key = %s", (order_key,))
                order_info = cur.fetchone()

        if not order_info or order_info['status'] != 'pending':
            bot.answer_pre_checkout_query(
                pre_checkout_query.id,
                ok=False,
                error_message="Заказ устарел или уже оплачен. Пожалуйста, оформите его заново."
            )
            return

        created_at = order_info.get('created_at')
        if not created_at or (time.time() - float(created_at)) > 900:
            bot.answer_pre_checkout_query(
                pre_checkout_query.id,
                ok=False,
                error_message="Время на оплату заказа истекло (15 мин). Пожалуйста, оформите заказ заново."
            )
            return

        if pre_checkout_query.from_user.id != order_info['chat_id']:
            bot.answer_pre_checkout_query(
                pre_checkout_query.id,
                ok=False,
                error_message="Этот счёт предназначен для другого пользователя."
            )
            return

        if pre_checkout_query.currency != 'RUB':
            bot.answer_pre_checkout_query(
                pre_checkout_query.id,
                ok=False,
                error_message="Неверная валюта платежа."
            )
            return

        if pre_checkout_query.total_amount != order_info['total_sum'] * 100:
            bot.answer_pre_checkout_query(
                pre_checkout_query.id,
                ok=False,
                error_message="Сумма заказа изменилась. Пожалуйста, оформите заказ заново."
            )
            return

        if order_info.get('prod_ids'):
            try:
                order_prod_ids = json.loads(order_info['prod_ids'])
                stoplist = get_stoplist_set(force_refresh=True)
                for pid in order_prod_ids:
                    if pid in stoplist:
                        title = MENU_CATALOG.get(pid, {}).get('title', 'позиция')
                        bot.answer_pre_checkout_query(
                            pre_checkout_query.id,
                            ok=False,
                            error_message=f"К сожалению, «{title}» сейчас отсутствует."
                        )
                        return
            except Exception:
                pass

        rules = get_schedule_rules(get_saratov_now())
        if rules['now_min'] >= rules['drinks_close']:
            bot.answer_pre_checkout_query(
                pre_checkout_query.id,
                ok=False,
                error_message="К сожалению, кофейня уже закрылась на сегодня."
            )
            return

        if order_info['has_food'] and rules['now_min'] >= rules['kitchen_close']:
            bot.answer_pre_checkout_query(
                pre_checkout_query.id,
                ok=False,
                error_message="К сожалению, кухня уже закончила работу на сегодня."
            )
            return

        bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    except Exception as e:
        print(f"Неожиданная ошибка в pre_checkout: {e}")
        try:
            bot.answer_pre_checkout_query(
                pre_checkout_query.id,
                ok=False,
                error_message="Ошибка сервера. Попробуйте ещё раз."
            )
        except Exception:
            pass

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    suma = message.successful_payment.total_amount // 100
    order_key = message.successful_payment.invoice_payload
    charge_id = message.successful_payment.telegram_payment_charge_id
    now_saratov = get_saratov_now()
    paid_time_str = now_saratov.strftime('%H:%M')
    now_ts = time.time()

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_key = %s FOR UPDATE", (order_key,))
            order_info = cur.fetchone()

            if order_info and order_info['status'] != 'pending':
                if charge_id and order_info.get('payment_charge_id') == charge_id:
                    return
                status = order_info['status']
                print(f"ВНИМАНИЕ: Оплата заказа {order_key} со статусом {status}, сумма {suma} ₽, charge_id={charge_id}")
                if ADMIN_ID != 0:
                    try:
                        user_name = html.escape(str(message.from_user.first_name or 'Гость'))
                        user_tag = f"@{html.escape(str(message.from_user.username))}" if message.from_user.username else f"id:{message.from_user.id}"
                        head = (
                            "ОПЛАЧЕН ПРОСРОЧЕННЫЙ СЧЁТ (заказ бариста НЕ передан)"
                            if status == 'expired'
                            else "ВОЗМОЖНАЯ ДВОЙНАЯ ОПЛАТА"
                        )
                        alert_text = (
                            f"⚠️ <b>ВНИМАНИЕ: {head}!</b>\n"
                            f"Заказ <code>{html.escape(str(order_key))}</code> уже имел статус <b>{html.escape(str(status))}</b>.\n"
                            f"Клиент: {user_name} ({user_tag})\n"
                            f"Сумма платежа: <b>{suma} ₽</b>\n"
                            f"charge_id: <code>{html.escape(str(charge_id))}</code>\n"
                            f"Проверьте платежи в ЛК ЮKassa и при необходимости сделайте возврат."
                        )
                        bot.send_message(ADMIN_ID, alert_text, parse_mode='HTML')
                    except Exception as alert_err:
                        print(f"Ошибка отправки алерта о платеже: {alert_err}")
                return

            if order_info:
                full_order_text = order_info['text']
                structured_receipt = order_info.get('structured_receipt') or ''
                ready_label = order_info['ready_label'] or 'Как можно скорее'
                target_hhmm = order_info['target_hhmm'] or ''
                prep_minutes = int(order_info['prep_minutes'] or 10)
                is_delayed = bool(order_info['is_delayed'])
            else:
                full_order_text = "Состав заказа не найден в базе."
                structured_receipt = ""
                ready_label = "Как можно скорее"
                target_hhmm = ""
                prep_minutes = 10
                is_delayed = False

            remind_ts = None
            remind_time_str = ""
            if is_delayed and target_hhmm and ':' in target_hhmm:
                try:
                    th, tm = map(int, target_hhmm.split(':'))
                    target_dt = now_saratov.replace(hour=th, minute=tm, second=0, microsecond=0)
                    remind_dt = target_dt - timedelta(minutes=prep_minutes)
                    delay_seconds = (remind_dt - now_saratov).total_seconds()
                    if delay_seconds > 300:
                        remind_ts = remind_dt.timestamp()
                        remind_time_str = remind_dt.strftime('%H:%M')
                except Exception as e:
                    print(f"Ошибка расчета таймера предзаказа: {e}")

            new_status = 'scheduled' if remind_ts else 'cooking'

            if order_info:
                cur.execute("""
                    UPDATE orders
                    SET status = %s, paid_time = %s, paid_at = %s, remind_at = %s, payment_charge_id = %s
                    WHERE order_key = %s
                """, (new_status, paid_time_str, now_ts, remind_ts, charge_id, order_key))

    orders_chat = get_orders_chat_id()
    if orders_chat != 0:
        try:
            if remind_ts:
                pre_text = (
                    f"📅 <b>ОФОРМЛЕН ПРЕДЗАКАЗ НА {html.escape(str(target_hhmm))}!</b>\n"
                    f"⚠️ <i>Готовить сейчас НЕ нужно! Бот пришлет уведомление с кнопкой «Готово» в <b>{remind_time_str}</b> (за {prep_minutes} мин до прихода гостя).</i>\n"
                    f"🕒 Оплачено в: {paid_time_str} ({suma} ₽)\n\n"
                    f"{full_order_text}"
                )
                bot.send_message(orders_chat, pre_text, parse_mode='HTML')
            else:
                admin_markup = InlineKeyboardMarkup()
                admin_markup.add(InlineKeyboardButton(text="✅ Готово (позвать гостя)", callback_data=f"ready_{order_key}"))

                admin_text = (
                    f"💸 <b>ЗАКАЗ ОПЛАЧЕН ОНЛАЙН ({suma} ₽)!</b>\n"
                    f"🕒 Оплачено в: {paid_time_str}\n\n"
                    f"{full_order_text}"
                )
                bot.send_message(orders_chat, admin_text, parse_mode='HTML', reply_markup=admin_markup)
        except Exception as bar_err:
            print(f"Критическая ошибка отправки заказа бариста ({order_key}): {bar_err}")

    try:
        client_msg = (
            f"🔥 <b>Оплата {suma} ₽ прошла успешно!</b>\n"
            f"Ждем тебя на Рабочей, 49!\n\n"
        )
        if structured_receipt:
            client_msg += f"🧾 <b>Твой заказ:</b>\n\n{structured_receipt}"
        else:
            client_msg += f"⏰ Время готовности: <b>{html.escape(str(ready_label))}</b>"

        bot.send_message(message.chat.id, client_msg, parse_mode='HTML')
    except Exception as cli_err:
        print(f"Ошибка отправки чека клиенту ({order_key}): {cli_err}")

def scheduler_loop():
    while True:
        try:
            now_ts = time.time()
            due_orders = []
            with get_db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE orders
                        SET status = 'cooking', remind_at = NULL
                        WHERE status = 'scheduled' AND remind_at IS NOT NULL AND remind_at <= %s
                        RETURNING order_key, chat_id, target_hhmm, total_sum, text
                    """, (now_ts,))
                    due_orders = cur.fetchall()

                    cur.execute("""
                        DELETE FROM orders
                        WHERE status IN ('pending', 'expired') AND created_at < %s
                    """, (now_ts - 3600,))

            orders_chat = get_orders_chat_id()
            for o in due_orders:
                if orders_chat != 0:
                    try:
                        admin_markup = InlineKeyboardMarkup()
                        admin_markup.add(InlineKeyboardButton(
                            text="✅ Готово (позвать гостя)",
                            callback_data=f"ready_{o['order_key']}"
                        ))
                        reminder_text = (
                            f"⏰🔥 <b>ПОРА ГОТОВИТЬ ПРЕДЗАКАЗ К {html.escape(str(o['target_hhmm']))}!</b>\n"
                            f"<i>(Оплачено онлайн: {o['total_sum']} ₽)</i>\n\n"
                            f"{o['text']}"
                        )
                        bot.send_message(orders_chat, reminder_text, parse_mode='HTML', reply_markup=admin_markup)
                    except Exception as send_err:
                        print(f"Ошибка отправки напоминания по {o['order_key']}, повтор через 60с: {send_err}")
                        try:
                            with get_db_conn() as conn:
                                with conn.cursor() as cur:
                                    cur.execute("""
                                        UPDATE orders
                                        SET status = 'scheduled', remind_at = %s
                                        WHERE order_key = %s AND status = 'cooking'
                                    """, (time.time() + 60, o['order_key']))
                        except Exception as rb_err:
                            print(f"Ошибка отката статуса предзаказа {o['order_key']}: {rb_err}")
        except Exception as e:
            print(f"Ошибка в scheduler_loop: {e}")

        time.sleep(30)

@bot.callback_query_handler(func=lambda call: call.data.startswith('ready_'))
def order_ready_callback(call):
    if not is_barista_or_admin(call.message.chat.id, call.from_user.id):
        bot.answer_callback_query(call.id, text="Нет доступа.")
        return

    order_key = call.data[len('ready_'):]
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE orders
                    SET status = 'ready', ready_at = %s
                    WHERE order_key = %s AND status IN ('scheduled', 'cooking')
                    RETURNING chat_id
                """, (time.time(), order_key))
                row = cur.fetchone()
    except Exception as e:
        print(f"Ошибка БД в order_ready_callback: {e}")
        bot.answer_callback_query(call.id, text="Ошибка базы данных.")
        return

    if not row:
        bot.answer_callback_query(call.id, text="Заказ уже отмечен готовым или не найден.")
        return

    client_chat_id = row['chat_id']
    try:
        bot.send_message(client_chat_id, "Мы уже все приготовили! Ждем Вас🫶🏻", parse_mode='HTML')
    except Exception:
        bot.answer_callback_query(call.id, text="Статус обновлен, но гостю не удалось написать (заблокировал бота).")
        return

    try:
        bot.answer_callback_query(call.id, text="Гостю отправлено уведомление!")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        safe_old_text = html.escape(call.message.text or "")
        bot.edit_message_text(
            safe_old_text + "\n\n✅ <i>Статус: Заказ готов</i>",
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML'
        )
    except Exception:
        pass

def run_bot():
    bot.infinity_polling()

if __name__ == '__main__':
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    serve(app, host='0.0.0.0', port=port, threads=8, max_request_body_size=131072)
