import asyncio
import logging
import os
import random
import re
import secrets
import time
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.exceptions import TelegramBadRequest

# ----------------------------------------------------
# 1. КОНФИГУРАЦИЯ И ВЛАДЕЛЕЦ
# ----------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOK", "ТВОЙ_ТОКЕН_ПО_УМОЛЧАНИЮ_ЕСЛИ_НЕТ_ENV")
OWNER_ID = 5480751648  # ID владельца с бесконечным балансом
ADMIN_IDS = [5480751648]  # Список Telegram ID админов, имеющих доступ к /allb и /annb
DB_NAME = "bot.db"

# Время перезарядки бонуса: 1.5 часа = 5400 секунд
BONUS_COOLDOWN = 5400
BONUS_AMOUNT = 4000.0  # Размер бонуса в GHRAM

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальное соединение с базой данных (для максимальной скорости и надежности)
bot_db: aiosqlite.Connection = None

# Активные и прошлые ставки в рулетке
active_roulette_bets = {}
last_roulette_bets = {}

# Активные игры в МИНЫ и ДЖОКЕР
active_mines_games = {}
active_joker_games = {}

# Активные игры в КРАШ
active_crash_games = {}

# Список пользователей с включенным X-Ray режимом
xray_users = set()

# Активные и ожидающие дуэли
pending_duels = {}
active_duels = {}

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}

# ----------------------------------------------------
# 1.1 НОВЫЕ КОНСТАНТЫ: HOUSE EDGE, ЭКОНОМИКА, КЛАНЫ
# ----------------------------------------------------
# --- HOUSE EDGE (перевес казино) ---
EVEN_PAYOUT = 1.95           # Выплата 1.95x вместо 2.0x для шансов ~50/50 (кости, монетка, рулетка)
BJ_WIN_PAYOUT = 1.95         # Обычная победа в блэкджеке
BJ_BLACKJACK_PAYOUT = 2.45   # Натуральный блэкджек (21 с двух карт)
PVP_RAKE = 0.025             # Комиссия бота 2.5% с банка PvP-игр
SLOTS_RTP_NOTE = "91%"       # Средний возврат слот-машины игроку

DICE_FACES = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}

# --- P2P НАЛОГ НА ПЕРЕВОДЫ (прогрессивный) ---
def calc_transfer_fee(amount: float) -> float:
    """До 100k — 5% | 100k–500k — 10% | от 500k — 15%."""
    if amount >= 500_000:
        rate = 0.15
    elif amount >= 100_000:
        rate = 0.10
    else:
        rate = 0.05
    return round(amount * rate, 2)

# --- ОБСЛУЖИВАНИЕ И ИЗНОС МАЙНИНГ-ФЕРМ ---
FARM_UPKEEP_RATE = 0.07      # 7% на электричество/аренду при каждом сборе
FARM_WEAR_MIN = 4            # Мин. износ прочности за сбор (%)
FARM_WEAR_MAX = 8            # Макс. износ прочности за сбор (%)
FARM_REPAIR_RATE = 0.25      # Ремонт = 25% от полной стоимости фермы

# --- КЛАНЫ ---
CLAN_CREATE_COST = 1_000_000.0
CLAN_NAME_MIN = 3
CLAN_NAME_MAX = 24
CLAN_MAX_LEVEL = 10
CLAN_INVITE_TTL = 86400      # Приглашение живёт 24 часа
CLAN_ROLES = {"owner": "👑 Главарь", "elder": "⭐ Заместитель", "member": "👤 Боец"}

# Состояния новых игр
active_dice_games = {}       # Кости PvE
pending_dice_pvp = {}        # Кости PvP (ожидание)
active_dice_pvp = {}         # Кости PvP (идёт игра)
active_coin_games = {}       # Монетка
active_blackjack_games = {}  # Блэкджек
pending_ttt = {}             # Крестики-нолики (ожидание)
active_ttt_games = {}        # Крестики-нолики (идёт игра)
active_slots_spins = set()   # Защита от параллельных вращений слотов

# ----------------------------------------------------
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ----------------------------------------------------
def get_balance_str(tg_id: int, balance: float) -> str:
    if tg_id == OWNER_ID:
        return "∞"
    return f"{balance:,.2f}"

def check_balance(tg_id: int, current_balance: float, required_amount: float) -> bool:
    if tg_id == OWNER_ID:
        return True
    return current_balance >= required_amount

def format_time_remaining(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}:{minutes:02d}"
    else:
        secs = seconds % 60
        return f"{minutes}:{secs:02d}"

def parse_amount(text: str, current_balance: float = 0.0) -> float | None:
    if not text:
        return None
    cleaned = text.strip().lower().replace(" ", "").replace("_", "").replace(",", ".")
    if cleaned in ["все", "всё", "all", "макс", "max"]:
        return current_balance
    multiplier = 1.0
    if cleaned.endswith(("kkk", "ккк", "b", "б")):
        multiplier = 1_000_000_000.0
        cleaned = re.sub(r"[kkkкккbб]$", "", cleaned)
    elif cleaned.endswith(("kk", "кк", "m", "м")):
        multiplier = 1_000_000.0
        cleaned = re.sub(r"[kkккmм]$", "", cleaned)
    elif cleaned.endswith(("k", "к")):
        multiplier = 1_000.0
        cleaned = re.sub(r"[kк]$", "", cleaned)
    try:
        val = float(cleaned)
        if val <= 0:
            return None
        return val * multiplier
    except ValueError:
        return None

async def get_user_lang(chat_type: str, tg_id: int) -> str:
    if chat_type in ["group", "supergroup"]:
        return "ru"
    async with bot_db.execute("SELECT language FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]
    return "ru"

def display_name_of(username: str | None) -> str:
    return f"@{username}" if username and username != "Неизвестно" else "Игрок"

# ----------------------------------------------------
# 3. РАБОТА С БАЗОЙ ДАННЫХ (ИДЕАЛЬНАЯ АРХИТЕКТУРА)
# ----------------------------------------------------
async def init_db():
    global bot_db
    bot_db = await aiosqlite.connect(DB_NAME)
    bot_db.row_factory = aiosqlite.Row

    # --- НАСТРОЙКИ SQLITE ДЛЯ МАКСИМАЛЬНОЙ НАДЕЖНОСТИ И СКОРОСТИ ---
    await bot_db.execute("PRAGMA journal_mode=WAL")       # Защита от поломки при перезагрузке
    await bot_db.execute("PRAGMA synchronous=NORMAL")     # Баланс скорости и безопасности
    await bot_db.execute("PRAGMA busy_timeout=5000")      # Ожидание при одновременных запросах
    await bot_db.execute("PRAGMA foreign_keys=ON")        # Контроль целостности
    await bot_db.execute("PRAGMA cache_size=-10000")      # Увеличенный кэш в оперативной памяти

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            balance REAL DEFAULT 1000.0,
            bank REAL DEFAULT 0.0,
            hourly_income REAL DEFAULT 150.0,
            last_claim INTEGER DEFAULT 0,
            last_bonus INTEGER DEFAULT 0,
            language TEXT DEFAULT 'ru'
        )
    """)

    # Безопасная миграция
    user_columns = [
        ("bank", "REAL DEFAULT 0.0"),
        ("hourly_income", "REAL DEFAULT 150.0"),
        ("last_claim", "INTEGER DEFAULT 0"),
        ("last_bonus", "INTEGER DEFAULT 0"),
        ("language", "TEXT DEFAULT 'ru'")
    ]
    for col_name, col_type in user_columns:
        try:
            await bot_db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
        except Exception:
            pass

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            amount REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS loans (
            user_id INTEGER PRIMARY KEY,
            amount REAL DEFAULT 0.0,
            repayment_amount REAL DEFAULT 0.0,
            created_at INTEGER DEFAULT 0
        )
    """)

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS mining_farms (
            user_id INTEGER PRIMARY KEY,
            level INTEGER DEFAULT 1,
            gpu_count INTEGER DEFAULT 1,
            last_collect INTEGER DEFAULT 0
        )
    """)

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS active_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_type TEXT NOT NULL,
            game_key TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            bet REAL NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            UNIQUE(game_type, game_key, user_id)
        )
    """)

    # --- ТАБЛИЦЫ КЛАНОВ ---
    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS clans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            balance REAL DEFAULT 0.0,
            total_donated REAL DEFAULT 0.0,
            level INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT 0
        )
    """)

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS clan_members (
            user_id INTEGER PRIMARY KEY,
            clan_id INTEGER NOT NULL,
            role TEXT DEFAULT 'member',
            joined_at INTEGER DEFAULT 0
        )
    """)

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS clan_invites (
            invite_id TEXT PRIMARY KEY,
            clan_id INTEGER NOT NULL,
            inviter_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            created_at INTEGER DEFAULT 0
        )
    """)

    # Миграция ферм: прочность оборудования и статус поломки
    farm_columns = [
        ("durability", "REAL DEFAULT 100.0"),
        ("broken", "INTEGER DEFAULT 0")
    ]
    for col_name, col_type in farm_columns:
        try:
            await bot_db.execute(f"ALTER TABLE mining_farms ADD COLUMN {col_name} {col_type}")
        except Exception:
            pass

    # --- ИНДЕКСЫ ДЛЯ МГНОВЕННОЙ РАБОТЫ ---
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, id DESC)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_loans_user ON loans(user_id)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_farms_user ON mining_farms(user_id)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_active_games_type_key ON active_games(game_type, game_key)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_active_games_user ON active_games(user_id)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_active_games_created ON active_games(created_at)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_clans_balance ON clans(balance DESC)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_clan_members_clan ON clan_members(clan_id)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_clan_invites_target ON clan_invites(target_id)")

    await bot_db.commit()

async def get_or_create_user(tg_id: int, username: str | None = None):
    now = int(time.time())
    initial_balance = 1018 if tg_id == OWNER_ID else 1000.0
    # INSERT OR IGNORE защищает от гонки состояний при одновременной регистрации
    await bot_db.execute(
        "INSERT OR IGNORE INTO users (tg_id, username, balance, last_claim, last_bonus, language) VALUES (?, ?, ?, ?, 0, 'ru')",
        (tg_id, username or "Неизвестно", initial_balance, now)
    )
    if tg_id == OWNER_ID:
        await bot_db.execute("UPDATE users SET balance = ? WHERE tg_id = ? AND balance < ?", (1018, tg_id, 10**17))
    if username:
        await bot_db.execute("UPDATE users SET username = ? WHERE tg_id = ? AND username != ?", (username, tg_id, username))
    await bot_db.commit()
    async with bot_db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
        return await cursor.fetchone()

async def get_user_by_identifier(identifier: str):
    identifier = identifier.strip()
    if identifier.startswith("@"):
        username = identifier[1:]
        async with bot_db.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)) as cursor:
            return await cursor.fetchone()
    elif identifier.isdigit():
        tg_id = int(identifier)
        async with bot_db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
            return await cursor.fetchone()
    else:
        async with bot_db.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (identifier,)) as cursor:
            return await cursor.fetchone()

async def update_balance(tg_id: int, amount: float):
    if tg_id == OWNER_ID and amount < 0:
        return
    await bot_db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, tg_id))
    await bot_db.commit()

async def add_history(user_id: int, action: str, amount: float):
    await bot_db.execute(
        "INSERT INTO history (user_id, action, amount) VALUES (?, ?, ?)",
        (user_id, action, amount)
    )
    await bot_db.commit()

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С АКТИВНЫМИ ИГРАМИ В БД ---
async def _save_game(game_type: str, game_key: str, chat_id: int, user_id: int, bet: float):
    try:
        now = int(time.time())
        await bot_db.execute(
            """INSERT INTO active_games (game_type, game_key, chat_id, user_id, bet, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_type, game_key, user_id) DO UPDATE SET bet = ?""",
            (game_type, game_key, chat_id, user_id, bet, now, bet)
        )
        await bot_db.commit()
    except Exception as e:
        logging.error(f"Failed to save game to DB: {e}")

async def _remove_game(game_type: str, game_key: str):
    try:
        await bot_db.execute(
            "DELETE FROM active_games WHERE game_type = ? AND game_key = ?",
            (game_type, game_key)
        )
        await bot_db.commit()
    except Exception as e:
        logging.error(f"Failed to remove game from DB: {e}")

async def cleanup_all_active_games():
    refunded_count = 0
    total_refunded = 0.0
    try:
        async with bot_db.execute("SELECT * FROM active_games") as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            await update_balance(row['user_id'], row['bet'])
            refunded_count += 1
            total_refunded += row['bet']
        if rows:
            await bot_db.execute("DELETE FROM active_games")
            await bot_db.commit()
    except Exception as e:
        logging.error(f"Error during startup cleanup: {e}")
    if refunded_count > 0:
        logging.info(f"Startup cleanup: refunded {refunded_count} games, total {total_refunded:,.2f} GHRAM")

async def cleanup_stale_games():
    stale_time = int(time.time()) - 1800
    cleaned = 0
    try:
        async with bot_db.execute("SELECT * FROM active_games WHERE created_at < ?", (stale_time,)) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            await update_balance(row['user_id'], row['bet'])
            cleaned += 1
            game_type = row['game_type']
            chat_id = row['chat_id']
            user_id = row['user_id']
            game_key_str = row['game_key']
            if game_type == "mines":
                active_mines_games.pop((chat_id, user_id), None)
            elif game_type == "joker":
                active_joker_games.pop((chat_id, user_id), None)
            elif game_type == "crash":
                game = active_crash_games.pop((chat_id, user_id), None)
                if game and game.get('status') == 'flying':
                    game['status'] = 'cancelled'
            elif game_type == "roulette":
                key = (chat_id, user_id)
                if key in active_roulette_bets:
                    active_roulette_bets[key] = []
            elif game_type == "duel_pending":
                d = pending_duels.pop(game_key_str, None)
                if d:
                    d['timer_task'].cancel()
            elif game_type == "duel_active":
                d = active_duels.pop(game_key_str, None)
                if d:
                    d['timer_task'].cancel()
            elif game_type == "dice":
                active_dice_games.pop((chat_id, user_id), None)
            elif game_type == "coin":
                active_coin_games.pop((chat_id, user_id), None)
            elif game_type == "blackjack":
                active_blackjack_games.pop((chat_id, user_id), None)
            elif game_type == "dicepvp_pending":
                d = pending_dice_pvp.pop(game_key_str, None)
                if d:
                    d['timer_task'].cancel()
            elif game_type == "dicepvp_active":
                d = active_dice_pvp.pop(game_key_str, None)
                if d:
                    d['timer_task'].cancel()
            elif game_type == "ttt_pending":
                t = pending_ttt.pop(game_key_str, None)
                if t:
                    t['timer_task'].cancel()
            elif game_type == "ttt_active":
                active_ttt_games.pop(game_key_str, None)
        if rows:
            await bot_db.execute("DELETE FROM active_games WHERE created_at < ?", (stale_time,))
            await bot_db.commit()
    except Exception as e:
        logging.error(f"Error during stale games cleanup: {e}")
    if cleaned > 0:
        logging.info(f"Periodic cleanup: cleaned {cleaned} stale games")

async def periodic_cleanup_task():
    while True:
        await asyncio.sleep(300)
        try:
            await cleanup_stale_games()
        except Exception as e:
            logging.error(f"Error in periodic cleanup: {e}")

# ----------------------------------------------------
# 4. КЛАВИАТУРЫ
# ----------------------------------------------------
def add_to_chat_keyboard():
    url = (
        "https://t.me/ghramtg_bot?startgroup&admin="
        "change_info+post_messages+edit_messages+delete_messages+"
        "restrict_members+invite_users+pin_messages+promote_members+"
        "manage_video_chats+manage_topics+manage_chat"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить бота в чат", url=url)]
    ])

def main_reply_keyboard(lang: str = "ru"):
    if lang == "en":
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="👤 Profile"), KeyboardButton(text="😶‍🌫 Change Language")],
                [KeyboardButton(text="🎁 Bonus"), KeyboardButton(text="🗣 About Us")]
            ],
            resize_keyboard=True
        )
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="😶‍🌫 Изменить язык")],
            [KeyboardButton(text="🎁 Бонус"), KeyboardButton(text="🗣 О нас")]
        ],
        resize_keyboard=True
    )

def language_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_set:ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_set:en")
        ]
    ])

def balance_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Бонус", callback_data="claim_bonus")]
    ])

def build_mines_keyboard(user_id: int, opened: set, mines: set, game_over: bool = False, is_win: bool = False):
    keyboard = []
    for row in range(5):
        line = []
        for col in range(5):
            idx = row * 5 + col
            if idx in opened:
                text = "💣" if idx in mines else "💎"
            else:
                text = "💣" if (game_over and idx in mines) else "❓"
            cb_data = f"mine_dis:{user_id}" if game_over else f"mine_c:{idx}:{user_id}"
            line.append(InlineKeyboardButton(text=text, callback_data=cb_data))
        keyboard.append(line)
    bottom_symbol = ("✅" if is_win else "❌") if game_over else ("✅" if len(opened) > 0 else "❌")
    cb_data = f"mine_dis:{user_id}" if game_over else f"mine_t:{user_id}"
    keyboard.append([InlineKeyboardButton(text=bottom_symbol, callback_data=cb_data)])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def build_joker_keyboard(user_id: int, cards: list | None = None, game_over: bool = False, is_win: bool = False):
    line = []
    if not game_over:
        for idx in range(3):
            line.append(InlineKeyboardButton(text="🎴", callback_data=f"joker_c:{idx}:{user_id}"))
        keyboard = [line, [InlineKeyboardButton(text="❌", callback_data=f"joker_can:{user_id}")]]
    else:
        for card in cards:
            line.append(InlineKeyboardButton(text=card, callback_data=f"joker_dis:{user_id}"))
        bottom_symbol = "✅" if is_win else "❌"
        keyboard = [line, [InlineKeyboardButton(text=bottom_symbol, callback_data=f"joker_dis:{user_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def build_mining_keyboard(user_id: int, gpu_cost: float, lvl_cost: float, broken: bool = False, repair_cost: float = 0.0):
    if broken:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🔧 ПОЧИНИТЬ ФЕРМУ ({repair_cost:,.0f})", callback_data=f"farm_repair:{user_id}")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"farm_refresh:{user_id}")]
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Собрать прибыль", callback_data=f"farm_claim:{user_id}")],
        [
            InlineKeyboardButton(text=f"🛒 +1 GPU ({gpu_cost:,.0f})", callback_data=f"farm_buy_gpu:{user_id}"),
            InlineKeyboardButton(text=f"⬆️ Уровень ({lvl_cost:,.0f})", callback_data=f"farm_upgrade_lvl:{user_id}")
        ],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"farm_refresh:{user_id}")]
    ])

def build_loan_keyboard(user_id: int, has_loan: bool):
    if has_loan:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Погасить весь кредит", callback_data=f"loan_pay_all:{user_id}")],
            [InlineKeyboardButton(text="🔄 Обновить статус", callback_data=f"loan_refresh:{user_id}")]
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Взять быстрый займ (50,000)", callback_data=f"loan_take_fast:{user_id}")]
    ])

# ----------------------------------------------------
# 5. АДМИН-КОМАНДЫ
# ----------------------------------------------------
@dp.message(Command("allb"))
async def cmd_allb(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❌ Использование: `/allb [сумма]`", parse_mode="Markdown")
        return
    parsed = parse_amount(parts[1])
    if parsed is None or parsed <= 0:
        await message.reply("❌ Укажите корректную сумму!")
        return
    await bot_db.execute("UPDATE users SET balance = balance + ? WHERE tg_id != ?", (parsed, OWNER_ID))
    await bot_db.commit()
    await message.reply(f"✅ Всем пользователям начислено по `{parsed:,.2f}` монет!", parse_mode="Markdown")

@dp.message(Command("annb"))
async def cmd_annb(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) > 1:
        target_str = parts[1]
        target_user = await get_user_by_identifier(target_str)
        if not target_user:
            await message.reply("❌ Пользователь не найден!")
            return
        await bot_db.execute("UPDATE users SET balance = 0 WHERE tg_id = ?", (target_user['tg_id'],))
        await bot_db.commit()
        await message.reply(f"🔥 Баланс пользователя @{target_user['username']} (ID: {target_user['tg_id']}) аннулирован!")
    else:
        await bot_db.execute("UPDATE users SET balance = 0 WHERE tg_id != ?", (OWNER_ID,))
        await bot_db.commit()
        await message.reply("🔥 Баланс *всех игроков* был успешно аннулирован!", parse_mode="Markdown")

@dp.message(Command("annf"))
async def cmd_annf(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) > 1:
        target_user = await get_user_by_identifier(parts[1])
        if not target_user:
            await message.reply("❌ Пользователь не найден!")
            return
        await bot_db.execute("DELETE FROM mining_farms WHERE user_id = ?", (target_user['tg_id'],))
        await bot_db.commit()
        await message.reply(f"🔥 Майнинг-ферма пользователя @{target_user['username']} (ID: {target_user['tg_id']}) полностью аннулирована!")
    else:
        await bot_db.execute("DELETE FROM mining_farms")
        await bot_db.commit()
        await message.reply("🔥 Майнинг-фермы *всех игроков* были успешно аннулированы!", parse_mode="Markdown")

@dp.message(Command("secm"))
async def cmd_secm(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❌ Использование: `/secm [ID / @username]`", parse_mode="Markdown")
        return
    target_user = await get_user_by_identifier(parts[1])
    if not target_user:
        await message.reply("❌ Пользователь не найден!")
        return
    xray_users.add(target_user['tg_id'])
    await message.reply(f"👁 X-Ray режим активирован для @{target_user['username']} (ID: {target_user['tg_id']})!")

@dp.message(Command("ansec"))
async def cmd_ansec(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❌ Использование: `/ansec [ID / @username]`", parse_mode="Markdown")
        return
    target_user = await get_user_by_identifier(parts[1])
    if not target_user:
        await message.reply("❌ Пользователь не найден!")
        return
    xray_users.discard(target_user['tg_id'])
    await message.reply(f"👁 X-Ray режим отключен для @{target_user['username']} (ID: {target_user['tg_id']})!")

@dp.message(Command("gamb"))
async def cmd_gamb(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id != OWNER_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("❌ Использование: `/gamb [ID / @username]`", parse_mode="Markdown")
        return
    target_user = await get_user_by_identifier(parts[1])
    if not target_user:
        await message.reply("❌ Пользователь не найден!")
        return
    target_id = target_user['tg_id']
    refund_amount = 0.0
    cancelled_games = 0

    for key in list(active_mines_games.keys()):
        if key[1] == target_id:
            game = active_mines_games.pop(key)
            refund_amount += game['bet']
            cancelled_games += 1
            await _remove_game("mines", f"{key[0]}:{key[1]}")

    for key in list(active_joker_games.keys()):
        if key[1] == target_id:
            game = active_joker_games.pop(key)
            refund_amount += game['bet']
            cancelled_games += 1
            await _remove_game("joker", f"{key[0]}:{key[1]}")

    for key in list(active_crash_games.keys()):
        if key[1] == target_id:
            game = active_crash_games.pop(key)
            game['status'] = 'cancelled'
            refund_amount += game['bet']
            cancelled_games += 1
            await _remove_game("crash", f"{key[0]}:{key[1]}")

    for key in list(active_roulette_bets.keys()):
        if key[1] == target_id:
            bets = active_roulette_bets.pop(key)
            refund_amount += sum(b['bet'] for b in bets)
            cancelled_games += 1
            await _remove_game("roulette", f"{key[0]}:{key[1]}")

    # --- НОВЫЕ ИГРЫ ---
    for key in list(active_dice_games.keys()):
        if key[1] == target_id:
            game = active_dice_games.pop(key)
            refund_amount += game['bet']
            cancelled_games += 1
            await _remove_game("dice", f"{key[0]}:{key[1]}")

    for key in list(active_coin_games.keys()):
        if key[1] == target_id:
            game = active_coin_games.pop(key)
            refund_amount += game['bet']
            cancelled_games += 1
            await _remove_game("coin", f"{key[0]}:{key[1]}")

    for key in list(active_blackjack_games.keys()):
        if key[1] == target_id:
            game = active_blackjack_games.pop(key)
            refund_amount += game['bet']
            cancelled_games += 1
            await _remove_game("blackjack", f"{key[0]}:{key[1]}")

    if refund_amount > 0:
        await update_balance(target_id, refund_amount)

    for d_id in list(pending_duels.keys()):
        d = pending_duels.get(d_id)
        if d and (d['challenger_id'] == target_id or d['target_id'] == target_id):
            d = pending_duels.pop(d_id)
            d['timer_task'].cancel()
            await update_balance(d['challenger_id'], d['bet'])
            cancelled_games += 1
            await _remove_game("duel_pending", d_id)
            try:
                await d['msg'].edit_text("🚫 Дуэль отменена администратором.")
            except Exception:
                pass

    for d_id in list(active_duels.keys()):
        d = active_duels.get(d_id)
        if d and (d['p1_id'] == target_id or d['p2_id'] == target_id):
            d = active_duels.pop(d_id)
            d['timer_task'].cancel()
            await update_balance(d['p1_id'], d['bet'])
            await update_balance(d['p2_id'], d['bet'])
            cancelled_games += 1
            await _remove_game("duel_active", d_id)
            try:
                await d['msg'].edit_text("🚫 Дуэль принудительно остановлена администратором. Ставки возвращены.")
            except Exception:
                pass

    # --- НОВЫЕ PVP ИГРЫ ---
    for d_id in list(pending_dice_pvp.keys()):
        d = pending_dice_pvp.get(d_id)
        if d and (d['challenger_id'] == target_id or d['target_id'] == target_id):
            d = pending_dice_pvp.pop(d_id)
            d['timer_task'].cancel()
            await update_balance(d['challenger_id'], d['bet'])
            cancelled_games += 1
            await _remove_game("dicepvp_pending", d_id)
            try:
                await d['msg'].edit_text("🚫 Игра в кости отменена администратором.")
            except Exception:
                pass

    for d_id in list(active_dice_pvp.keys()):
        d = active_dice_pvp.get(d_id)
        if d and (d['p1_id'] == target_id or d['p2_id'] == target_id):
            d = active_dice_pvp.pop(d_id)
            d['timer_task'].cancel()
            await update_balance(d['p1_id'], d['bet'])
            await update_balance(d['p2_id'], d['bet'])
            cancelled_games += 1
            await _remove_game("dicepvp_active", d_id)
            try:
                await d['msg'].edit_text("🚫 Игра в кости остановлена администратором. Ставки возвращены.")
            except Exception:
                pass

    for t_id in list(pending_ttt.keys()):
        t = pending_ttt.get(t_id)
        if t and (t['challenger_id'] == target_id or t['target_id'] == target_id):
            t = pending_ttt.pop(t_id)
            t['timer_task'].cancel()
            await update_balance(t['challenger_id'], t['bet'])
            cancelled_games += 1
            await _remove_game("ttt_pending", t_id)
            try:
                await t['msg'].edit_text("🚫 Игра в крестики-нолики отменена администратором.")
            except Exception:
                pass

    for t_id in list(active_ttt_games.keys()):
        t = active_ttt_games.get(t_id)
        if t and (t['x_id'] == target_id or t['o_id'] == target_id):
            active_ttt_games.pop(t_id)
            await update_balance(t['x_id'], t['bet'])
            if not t['vs_bot']:
                await update_balance(t['o_id'], t['bet'])
            cancelled_games += 1
            await _remove_game("ttt_active", t_id)
            try:
                await t['msg'].edit_text("🚫 Игра остановлена администратором. Ставки возвращены.")
            except Exception:
                pass

    if cancelled_games == 0:
        await message.reply(f"ℹ️ У пользователя @{target_user['username']} нет активных игр.")
    else:
        await message.reply(
            f"🛑 Все активные игры для @{target_user['username']} (ID: {target_id}) отменены.\n"
            f"💰 Средства успешно возвращены игроку на баланс!",
            parse_mode="Markdown"
        )

# ----------------------------------------------------
# 6. ОСНОВНЫЕ КОМАНДЫ
# ----------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type in ["group", "supergroup"]:
        return
    await get_or_create_user(message.from_user.id, message.from_user.username)
    lang = await get_user_lang(message.chat.type, message.from_user.id)
    if lang == "en":
        welcome_text = (
            "👋 Welcome!\n"
            "GHRAM is an entertainment bot for your chat:\n"
            "• ⚔️ Create your own clan\n"
            "• 🏆 Participate in tournaments\n"
            "• 🎮 Mini-games\n"
            "• 🤺 Duels\n"
            "By starting the bot, you automatically agree to the terms of use."
        )
        menu_text = "Select the section you need in the menu below 👇"
    else:
        welcome_text = (
            "👋 Добро пожаловать!\n"
            "GHRAM — это развлекательный бот для вашего чата:\n"
            "• ⚔️ Создание собственного клана\n"
            "• 🏆 Участие в турнирах\n"
            "• 🎮 Мини-игры\n"
            "• 🤺 Дуэли\n"
            "Запуская бота, вы автоматически соглашаетесь с условиями использования."
        )
        menu_text = "Выберите нужный раздел в меню ниже 👇"
    await message.answer(welcome_text, reply_markup=add_to_chat_keyboard())
    if message.chat.type == "private":
        await message.answer(menu_text, reply_markup=main_reply_keyboard(lang))

@dp.message(F.text.in_(["🗣 О нас", "о нас", "О нас", "about us", "About Us", "/about", "🗣 About Us"]))
async def menu_about(message: types.Message):
    about_text = (
        "Создатель проекта — @cdsai\n"
        "По всем проблемам писать в тех поддержку — @AnoCdsai_bot"
    )
    await message.reply(about_text)

@dp.message(F.text.in_(["😶‍🌫 Изменить язык", "изменить язык", "язык", "Language", "Change Language", "😶‍🌫 Change Language", "/language", "/lang"]))
async def menu_change_language(message: types.Message):
    lang = await get_user_lang(message.chat.type, message.from_user.id)
    if message.chat.type in ["group", "supergroup"]:
        await message.reply("🌐 В группах всегда используется русский язык.")
        return
    text = (
        "🌐 *Выберите язык интерфейса:*\n"
        "(Настройка языка применяется только в личных сообщениях с ботом. В групповых чатах всегда используется русский язык)"
    ) if lang == "ru" else (
        "🌐 *Select interface language:*\n"
        "(Language setting applies only to direct messages with the bot. In group chats, the language is always Russian)"
    )
    await message.reply(text, parse_mode="Markdown", reply_markup=language_inline_keyboard())

@dp.callback_query(F.data.startswith("lang_set:"))
async def callback_set_language(callback: types.CallbackQuery):
    if callback.message.chat.type in ["group", "supergroup"]:
        await callback.answer("В группах всегда используется русский язык!", show_alert=True)
        return
    new_lang = callback.data.split(":")[1]
    await bot_db.execute("UPDATE users SET language = ? WHERE tg_id = ?", (new_lang, callback.from_user.id))
    await bot_db.commit()
    if new_lang == "en":
        alert_msg = "✅ Language changed to English!"
        text_msg = "✅ *Language changed to English!*\nNow bot will answer in English in DMs."
        kb = main_reply_keyboard("en")
    else:
        alert_msg = "✅ Язык успешно изменен на Русский!"
        text_msg = "✅ *Язык успешно изменен на Русский!*"
        kb = main_reply_keyboard("ru")
    await callback.answer(alert_msg, show_alert=True)
    await callback.message.edit_text(text_msg, parse_mode="Markdown")
    if callback.message.chat.type == "private":
        await callback.message.answer(
            "Menu updated 👇" if new_lang == "en" else "Меню обновлено 👇",
            reply_markup=kb
        )

# --- БОНУС ---
async def process_bonus_claim(user_id: int, username: str | None) -> str:
    user = await get_or_create_user(user_id, username)
    now = int(time.time())
    last_bonus = user['last_bonus'] or 0
    time_passed = now - last_bonus
    if time_passed >= BONUS_COOLDOWN:
        await update_balance(user['tg_id'], BONUS_AMOUNT)
        await bot_db.execute("UPDATE users SET last_bonus = ? WHERE tg_id = ?", (now, user['tg_id']))
        await bot_db.commit()
        await add_history(user['tg_id'], "Получение бонуса", BONUS_AMOUNT)
        next_time_str = format_time_remaining(BONUS_COOLDOWN)
        return f"Вам начислено: {int(BONUS_AMOUNT)} GHRAM\nСледующий бонус будет доступен через {next_time_str}"
    else:
        remaining = BONUS_COOLDOWN - time_passed
        time_str = format_time_remaining(remaining)
        return f"❌ Вы уже забирали бонус!\nСледующий бонус будет доступен через {time_str}"

@dp.message(F.text.in_(["🎁 Бонус", "бонус", "/bonus", "Бонус", "🎁 Bonus", "Bonus"]))
async def menu_bonus(message: types.Message):
    response_text = await process_bonus_claim(message.from_user.id, message.from_user.username)
    await message.reply(response_text)

@dp.callback_query(F.data == "claim_bonus")
async def callback_bonus(callback: types.CallbackQuery):
    response_text = await process_bonus_claim(callback.from_user.id, callback.from_user.username)
    await callback.answer(response_text, show_alert=True)

# --- БАЛАНС ---
@dp.message(F.text.lower().in_(["б", "баланс", "/balance", "/баланс", "balance"]))
async def show_balance(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    bal_str = get_balance_str(user['tg_id'], user['balance'])
    await message.reply(
        f"💰 Ваш баланс: *{bal_str}* монет",
        parse_mode="Markdown",
        reply_markup=balance_inline_keyboard()
    )

# --- ПРОФИЛЬ ---
@dp.message(F.text.lower().in_(["/профиль", "профиль", "👤 профиль", "profile", "👤 profile"]))
async def show_profile(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    lang = await get_user_lang(message.chat.type, message.from_user.id)
    bal_str = get_balance_str(user['tg_id'], user['balance'])
    bank_str = get_balance_str(user['tg_id'], user['bank'])
    if lang == "en":
        text = (
            f"👤 *User Profile:* @{user['username']}\n"
            f"🆔 ID: `{user['tg_id']}`\n"
            f"💰 Balance: `{bal_str}` coins\n"
            f"🏦 In Bank: `{bank_str}` coins\n"
            f"📈 Passive Income: `{user['hourly_income']:,.2f}`/hour"
        )
    else:
        text = (
            f"👤 *Профиль пользователя:* @{user['username']}\n"
            f"🆔 ID: `{user['tg_id']}`\n"
            f"💰 Баланс: `{bal_str}` монет\n"
            f"🏦 В банке: `{bank_str}` монет\n"
            f"📈 Пассивный доход: `{user['hourly_income']:,.2f}`/час"
        )
    await message.reply(text, parse_mode="Markdown")

# --- ИСТОРИЯ ---
@dp.message(F.text.lower().in_(["/история", "история", "history"]))
async def show_history(message: types.Message):
    async with bot_db.execute(
        "SELECT action, amount, created_at FROM history WHERE user_id = ? ORDER BY id DESC LIMIT 5",
        (message.from_user.id,)
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        await message.reply("📜 Ваша история операций пока пуста.")
        return
    text = "📜 *Последние операции:*\n"
    for r in rows:
        sign = "+" if r['amount'] > 0 else ""
        text += f"• {r['action']}: `{sign}{r['amount']:,.2f}` монет ({r['created_at']})\n"
    await message.reply(text, parse_mode="Markdown")

# --- ЕДИНЫЙ ТОП ---
@dp.message(F.text.lower().startswith(("/top", "топ", "/топ")))
async def show_top(message: types.Message):
    parts = message.text.split()
    limit = 10
    if len(parts) > 1 and parts[1].isdigit():
        limit = min(int(parts[1]), 50)
    async with bot_db.execute(
        "SELECT tg_id, username, balance FROM users ORDER BY balance DESC LIMIT ?", (limit,)
    ) as cursor:
        users = await cursor.fetchall()
    text = f"🏆 *Глобальный топ-{len(users)} игроков:*\n"
    medals = ["🥇", "🥈", "🥉"]
    for idx, u in enumerate(users, start=1):
        icon = medals[idx-1] if idx <= 3 else f"{idx}."
        bal_str = get_balance_str(u['tg_id'], u['balance'])
        text += f"{icon} @{u['username']} — `{bal_str}` монет\n"
    await message.reply(text, parse_mode="Markdown")

# --- ПЕРЕВОД (С P2P НАЛОГОМ) ---
@dp.message(F.text.lower().startswith("п "))
async def transfer_money(message: types.Message):
    sender = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    target_user = None
    amount = 0.0

    if message.reply_to_message:
        if len(parts) < 2:
            await message.reply("❌ Неверный формат! Используйте: `п [сумма]` в ответ на сообщение.")
            return
        parsed = parse_amount(parts[1], sender['balance'])
        if parsed is None:
            await message.reply("❌ Неверно указана сумма перевода!")
            return
        amount = parsed
        target_id = message.reply_to_message.from_user.id
        target_user = await get_or_create_user(target_id, message.reply_to_message.from_user.username)
    elif len(parts) >= 3:
        target_identifier = parts[1]
        parsed = parse_amount(parts[2], sender['balance'])
        if parsed is None:
            await message.reply("❌ Неверно указана сумма перевода!")
            return
        amount = parsed
        target_user = await get_user_by_identifier(target_identifier)
        if not target_user:
            await message.reply("❌ Пользователь не найден в базе данных бота!")
            return
    else:
        await message.reply("❌ Используйте: `п [сумма]` в ответ на сообщение или `п [ID/@username] [сумма]`.")
        return

    if amount <= 0:
        await message.reply("❌ Сумма перевода должна быть больше 0.")
        return
    if not check_balance(sender['tg_id'], sender['balance'], amount):
        await message.reply("❌ У вас недостаточно монет!")
        return
    if target_user['tg_id'] == sender['tg_id']:
        await message.reply("❌ Нельзя переводить монеты самому себе!")
        return

    # --- P2P TAX: прогрессивная комиссия 5% / 10% / 15% ---
    if sender['tg_id'] == OWNER_ID or target_user['tg_id'] == OWNER_ID:
        fee = 0.0
    else:
        fee = calc_transfer_fee(amount)
    received = round(amount - fee, 2)

    await update_balance(sender['tg_id'], -amount)
    await update_balance(target_user['tg_id'], received)

    target_name = f"@{target_user['username']}" if target_user['username'] != "Неизвестно" else f"ID {target_user['tg_id']}"
    await add_history(sender['tg_id'], f"Перевод для {target_name} (комиссия {fee:,.2f})", -amount)
    await add_history(target_user['tg_id'], f"Перевод от @{sender['username']}", received)

    if fee > 0:
        fee_rate = round(fee / amount * 100)
        await message.reply(
            f"✅ Перевод выполнен!\n"
            f"💸 Сумма: `{amount:,.2f}` монет\n"
            f"🏷 Налог на перевод (P2P Tax, {fee_rate}%): `−{fee:,.2f}` монет\n"
            f"💰 Пользователю *{target_name}* зачислено: `{received:,.2f}` монет",
            parse_mode="Markdown"
        )
    else:
        await message.reply(
            f"✅ Вы успешно перевели `{amount:,.2f}` монет пользователю *{target_name}*!",
            parse_mode="Markdown"
        )

# --- КОМАНДА /стоп ДЛЯ ОТМЕНЫ ВСЕХ ИГР ---
@dp.message(F.text.lower().in_(["/stop", "/стоп", "стоп", "stop", "прекратить", "/cancel", "/отмена"]))
async def cmd_stop_all_games(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    key = (chat_id, user_id)
    refunds = []

    if key in active_mines_games:
        game = active_mines_games.pop(key)
        refunds.append((user_id, game['bet']))
        await _remove_game("mines", f"{chat_id}:{user_id}")

    if key in active_joker_games:
        game = active_joker_games.pop(key)
        refunds.append((user_id, game['bet']))
        await _remove_game("joker", f"{chat_id}:{user_id}")

    if key in active_crash_games:
        game = active_crash_games.pop(key)
        if game.get('status') == 'flying':
            game['status'] = 'cancelled'
        refunds.append((user_id, game['bet']))
        await _remove_game("crash", f"{chat_id}:{user_id}")

    if key in active_roulette_bets and active_roulette_bets[key]:
        bets = active_roulette_bets[key]
        total = sum(b['bet'] for b in bets)
        refunds.append((user_id, total))
        active_roulette_bets[key] = []
        await _remove_game("roulette", f"{chat_id}:{user_id}")

    # --- НОВЫЕ ИГРЫ ---
    if key in active_dice_games:
        game = active_dice_games.pop(key)
        refunds.append((user_id, game['bet']))
        await _remove_game("dice", f"{chat_id}:{user_id}")

    if key in active_coin_games:
        game = active_coin_games.pop(key)
        refunds.append((user_id, game['bet']))
        await _remove_game("coin", f"{chat_id}:{user_id}")

    if key in active_blackjack_games:
        game = active_blackjack_games.pop(key)
        refunds.append((user_id, game['bet']))
        await _remove_game("blackjack", f"{chat_id}:{user_id}")

    for d_id in list(pending_duels.keys()):
        d = pending_duels[d_id]
        if d['challenger_id'] == user_id and d['chat_id'] == chat_id:
            d['timer_task'].cancel()
            pending_duels.pop(d_id)
            refunds.append((user_id, d['bet']))
            await _remove_game("duel_pending", d_id)
            try:
                await d['msg'].edit_text("🚫 Дуэль отменена. Ставка возвращена.")
            except Exception:
                pass
            break

    for d_id in list(active_duels.keys()):
        d = active_duels[d_id]
        if user_id in (d['p1_id'], d['p2_id']) and d['chat_id'] == chat_id:
            d['timer_task'].cancel()
            active_duels.pop(d_id)
            refunds.append((d['p1_id'], d['bet']))
            refunds.append((d['p2_id'], d['bet']))
            await _remove_game("duel_active", d_id)
            try:
                await d['msg'].edit_text("🚫 Дуэль отменена. Ставки возвращены обоим игрокам.")
            except Exception:
                pass
            break

    for d_id in list(pending_dice_pvp.keys()):
        d = pending_dice_pvp[d_id]
        if d['challenger_id'] == user_id and d['chat_id'] == chat_id:
            d['timer_task'].cancel()
            pending_dice_pvp.pop(d_id)
            refunds.append((user_id, d['bet']))
            await _remove_game("dicepvp_pending", d_id)
            try:
                await d['msg'].edit_text("🚫 Игра в кости отменена. Ставка возвращена.")
            except Exception:
                pass
            break

    for d_id in list(active_dice_pvp.keys()):
        d = active_dice_pvp[d_id]
        if user_id in (d['p1_id'], d['p2_id']) and d['chat_id'] == chat_id:
            d['timer_task'].cancel()
            active_dice_pvp.pop(d_id)
            refunds.append((d['p1_id'], d['bet']))
            refunds.append((d['p2_id'], d['bet']))
            await _remove_game("dicepvp_active", d_id)
            try:
                await d['msg'].edit_text("🚫 Игра в кости отменена. Ставки возвращены обоим игрокам.")
            except Exception:
                pass
            break

    for t_id in list(pending_ttt.keys()):
        t = pending_ttt[t_id]
        if t['challenger_id'] == user_id and t['chat_id'] == chat_id:
            t['timer_task'].cancel()
            pending_ttt.pop(t_id)
            refunds.append((user_id, t['bet']))
            await _remove_game("ttt_pending", t_id)
            try:
                await t['msg'].edit_text("🚫 Игра в крестики-нолики отменена. Ставка возвращена.")
            except Exception:
                pass
            break

    for t_id in list(active_ttt_games.keys()):
        t = active_ttt_games[t_id]
        if user_id in (t['x_id'], t['o_id']) and t['chat_id'] == chat_id:
            active_ttt_games.pop(t_id)
            refunds.append((t['x_id'], t['bet']))
            if not t['vs_bot']:
                refunds.append((t['o_id'], t['bet']))
            await _remove_game("ttt_active", t_id)
            try:
                await t['msg'].edit_text("🚫 Игра отменена. Ставки возвращены.")
            except Exception:
                pass
            break

    if refunds:
        for uid, amt in refunds:
            await update_balance(uid, amt)
        own_refund = sum(amt for uid, amt in refunds if uid == user_id)
        if own_refund > 0:
            await message.reply(
                f"✅ Все ваши активные игры отменены.\n💰 Возвращено: `{own_refund:,.2f}` GHRAM.",
                parse_mode="Markdown"
            )
        else:
            await message.reply("✅ Активная игра отменена. Ставки возвращены обоим игрокам.")
    else:
        await message.reply("ℹ️ У вас нет активных игр для отмены.")

# --- ИНТЕРАКТИВНАЯ ДУЭЛЬ ---
@dp.message(F.text.lower().startswith(("/дуэль", "дуэль")))
async def make_duel(message: types.Message):
    sender = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    target_user = None
    bet = 100.0

    if message.reply_to_message:
        if len(parts) >= 2:
            parsed = parse_amount(parts[1], sender['balance'])
            if parsed:
                bet = parsed
        target_id = message.reply_to_message.from_user.id
        target_user = await get_or_create_user(target_id, message.reply_to_message.from_user.username)
    elif len(parts) >= 3:
        target_identifier = parts[1]
        parsed = parse_amount(parts[2], sender['balance'])
        if parsed:
            bet = parsed
        target_user = await get_user_by_identifier(target_identifier)
        if not target_user:
            await message.reply("❌ Пользователь не найден в базе данных бота!")
            return
    elif len(parts) == 2:
        target_user = await get_user_by_identifier(parts[1])
        if not target_user:
            await message.reply("❌ Пользователь не найден в базе данных бота!")
            return
    else:
        await message.reply("⚔️ Чтобы вызвать на дуэль, напишите `дуэль [@username/ID] [сумма]` или ответьте на сообщение!")
        return

    if bet <= 0:
        await message.reply("❌ Ставка должна быть больше 0.")
        return
    if target_user['tg_id'] == sender['tg_id']:
        await message.reply("❌ Нельзя вызывать на дуэль самого себя!")
        return
    if not check_balance(sender['tg_id'], sender['balance'], bet):
        await message.reply(f"❌ У вас недостаточно монет для дуэли (нужно {bet:,.2f}).")
        return
    if not check_balance(target_user['tg_id'], target_user['balance'], bet):
        await message.reply(f"❌ У соперника недостаточно монет (нужно {bet:,.2f}).")
        return

    for d in list(pending_duels.values()) + list(active_duels.values()):
        if d['chat_id'] == message.chat.id and (sender['tg_id'] in (d.get('p1_id'), d.get('p2_id'), d.get('challenger_id'), d.get('target_id')) or target_user['tg_id'] in (d.get('p1_id'), d.get('p2_id'), d.get('challenger_id'), d.get('target_id'))):
            await message.reply("❌ Один из участников уже находится в активной дуэли!")
            return

    await update_balance(sender['tg_id'], -bet)
    duel_id = secrets.token_hex(4)

    target_mention = f"@{target_user['username']}" if target_user['username'] and target_user['username'] != "Неизвестно" else f"ID {target_user['tg_id']}"
    sender_mention = f"@{sender['username']}" if sender['username'] and sender['username'] != "Неизвестно" else f"ID {sender['tg_id']}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Согласиться ✅", callback_data=f"duel_acc:{duel_id}"),
        InlineKeyboardButton(text="Отказаться ⛔", callback_data=f"duel_dec:{duel_id}")
    ]])

    sent_msg = await message.answer(
        f"{target_mention}, вас вызвали на дуэль 🥳\n"
        f"👤 Инициатор: {sender_mention}\n"
        f"💰 Ставка: *{bet:,.2f}* монет",
        reply_markup=kb
    )
    await _save_game("duel_pending", duel_id, message.chat.id, sender['tg_id'], bet)

    async def invite_timeout():
        await asyncio.sleep(180)
        if duel_id in pending_duels:
            d = pending_duels.pop(duel_id, None)
            if d:
                await update_balance(d['challenger_id'], d['bet'])
                await _remove_game("duel_pending", duel_id)
                try:
                    await sent_msg.edit_text("⏳ Время ожидания ответа на дуэль истекло (3 мин). Дуэль отменена, ставка возвращена.")
                except Exception:
                    pass

    timer_task = asyncio.create_task(invite_timeout())
    pending_duels[duel_id] = {
        "chat_id": message.chat.id,
        "challenger_id": sender['tg_id'],
        "challenger_name": sender_mention,
        "target_id": target_user['tg_id'],
        "target_name": target_mention,
        "bet": bet,
        "msg": sent_msg,
        "timer_task": timer_task
    }

@dp.callback_query(F.data.startswith("duel_acc:"))
async def callback_duel_accept(callback: types.CallbackQuery):
    duel_id = callback.data.split(":")[1]
    duel = pending_duels.get(duel_id)
    if not duel:
        await callback.answer("Эта дуэль больше неактивна!", show_alert=True)
        return
    if callback.from_user.id != duel["target_id"]:
        await callback.answer("❌ Принять дуэль может только вызванный игрок!", show_alert=True)
        return
    target_user = await get_or_create_user(duel["target_id"], callback.from_user.username)
    if not check_balance(target_user['tg_id'], target_user['balance'], duel['bet']):
        await callback.answer("❌ У вас недостаточно монет для принятия дуэли!", show_alert=True)
        return

    duel["timer_task"].cancel()
    del pending_duels[duel_id]
    await _remove_game("duel_pending", duel_id)
    await update_balance(target_user['tg_id'], -duel['bet'])

    p1 = (duel["challenger_id"], duel["challenger_name"])
    p2 = (duel["target_id"], duel["target_name"])
    first, second = secrets.choice([(p1, p2), (p2, p1)])
    first_id, first_name = first
    second_id, second_name = second

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔫 Выстрел", callback_data=f"duel_shot:{duel_id}")
    ]])

    msg_text = (
        f"⚔️ *Дуэль началась!*\n"
        f"💰 Ставка: *{duel['bet']:,.2f}* монет (банк {duel['bet'] * 2:,.2f})\n"
        f"{first_name}, вам дано право на выстрел."
    )
    await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer("Дуэль принята!")

    await _save_game("duel_active", duel_id, callback.message.chat.id, first_id, duel['bet'])
    await _save_game("duel_active", duel_id, callback.message.chat.id, second_id, duel['bet'])

    async def shot_timeout(current_duel_id):
        await asyncio.sleep(180)
        if current_duel_id in active_duels:
            ad = active_duels.pop(current_duel_id, None)
            if ad:
                await update_balance(ad["p1_id"], ad["bet"])
                await update_balance(ad["p2_id"], ad["bet"])
                await _remove_game("duel_active", current_duel_id)
                try:
                    await ad["msg"].edit_text("⏳ Время ожидания выстрела истекло (3 мин). Дуэль отменена, деньги возвращены.")
                except Exception:
                    pass

    timer_task = asyncio.create_task(shot_timeout(duel_id))
    active_duels[duel_id] = {
        "chat_id": callback.message.chat.id,
        "p1_id": first_id,
        "p1_name": first_name,
        "p2_id": second_id,
        "p2_name": second_name,
        "current_turn_id": first_id,
        "bet": duel["bet"],
        "msg": callback.message,
        "timer_task": timer_task
    }

@dp.callback_query(F.data.startswith("duel_dec:"))
async def callback_duel_decline(callback: types.CallbackQuery):
    duel_id = callback.data.split(":")[1]
    duel = pending_duels.get(duel_id)
    if not duel:
        await callback.answer("Эта дуэль больше неактивна!", show_alert=True)
        return
    if callback.from_user.id != duel["target_id"]:
        await callback.answer("❌ Отклонить дуэль может только вызванный игрок!", show_alert=True)
        return
    duel["timer_task"].cancel()
    del pending_duels[duel_id]
    await _remove_game("duel_pending", duel_id)
    await update_balance(duel["challenger_id"], duel["bet"])
    await callback.message.edit_text(f"⛔ {duel['target_name']} отклонил(а) дуэль. Ставка возвращена.")
    await callback.answer("Дуэль отклонена")

async def process_duel_shot(duel_id: str, shooter_id: int, message_or_cb):
    duel = active_duels.get(duel_id)
    if not duel:
        return False, "Дуэль не найдена!"
    if shooter_id != duel["current_turn_id"]:
        return False, "❌ Сейчас не ваш черед стрелять!"

    duel["timer_task"].cancel()
    is_hit = secrets.choice([True, False])
    shooter_name = duel["p1_name"] if shooter_id == duel["p1_id"] else duel["p2_name"]
    next_id = duel["p2_id"] if shooter_id == duel["p1_id"] else duel["p1_id"]
    next_name = duel["p2_name"] if shooter_id == duel["p1_id"] else duel["p1_name"]

    if is_hit:
        total_win = duel["bet"] * 2
        await update_balance(shooter_id, total_win)
        await add_history(shooter_id, "Победа в дуэли", total_win - duel["bet"])
        await add_history(next_id, "Поражение в дуэли", -duel["bet"])
        del active_duels[duel_id]
        await _remove_game("duel_active", duel_id)
        text = (
            f"💥 {shooter_name} делает выстрел и... *ПОПАДАЕТ!* 🎯\n"
            f"👑 Победитель: {shooter_name}\n"
            f"💰 Выигрыш: *{total_win:,.2f}* монет!"
        )
        if isinstance(message_or_cb, types.CallbackQuery):
            await message_or_cb.message.edit_text(text, parse_mode="Markdown")
            await message_or_cb.answer("🎯 Попадание!")
        else:
            await message_or_cb.reply(text, parse_mode="Markdown")
        return True, "Победа"
    else:
        duel["current_turn_id"] = next_id

        async def shot_timeout(current_duel_id):
            await asyncio.sleep(180)
            if current_duel_id in active_duels:
                ad = active_duels.pop(current_duel_id, None)
                if ad:
                    await update_balance(ad["p1_id"], ad["bet"])
                    await update_balance(ad["p2_id"], ad["bet"])
                    await _remove_game("duel_active", current_duel_id)
                    try:
                        await ad["msg"].edit_text("⏳ Время ожидания выстрела истекло (3 мин). Дуэль отменена, деньги возвращены.")
                    except Exception:
                        pass

        duel["timer_task"] = asyncio.create_task(shot_timeout(duel_id))
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔫 Выстрел", callback_data=f"duel_shot:{duel_id}")
        ]])
        text = (
            f"💨 {shooter_name} делает выстрел и... *ПРОМАХИВАЕТСЯ!*\n"
            f"{next_name}, вам дано право на выстрел."
        )
        if isinstance(message_or_cb, types.CallbackQuery):
            await message_or_cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            await message_or_cb.answer("💨 Промах!")
        else:
            sent = await message_or_cb.reply(text, reply_markup=kb, parse_mode="Markdown")
            duel["msg"] = sent
        return True, "Промах"

@dp.callback_query(F.data.startswith("duel_shot:"))
async def callback_duel_shot(callback: types.CallbackQuery):
    duel_id = callback.data.split(":")[1]
    success, msg = await process_duel_shot(duel_id, callback.from_user.id, callback)
    if not success:
        await callback.answer(msg, show_alert=True)

@dp.message(F.text.lower().in_(["выстрел", "🔫 выстрел", "/shot"]))
async def msg_duel_shot(message: types.Message):
    found_duel_id = None
    for d_id, d in active_duels.items():
        if d["chat_id"] == message.chat.id and d["current_turn_id"] == message.from_user.id:
            found_duel_id = d_id
            break
    if not found_duel_id:
        in_duel = any(d["chat_id"] == message.chat.id and message.from_user.id in (d["p1_id"], d["p2_id"]) for d in active_duels.values())
        if in_duel:
            await message.reply("❌ Сейчас не ваш черед стрелять!")
        return
    await process_duel_shot(found_duel_id, message.from_user.id, message)

# ----------------------------------------------------
# 7. МИНИ-ИГРЫ
# ----------------------------------------------------
# --- МИНЫ ---
@dp.message(F.text.lower().startswith("мины"))
async def game_mines(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("💣 Введите ставку: `мины [сумма]`")
        return
    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return
    game_key = (message.chat.id, message.from_user.id)
    if game_key in active_mines_games:
        await message.reply("❌ У вас уже есть активная игра в мины! Закончите её или напишите `/стоп` для отмены.")
        return

    await update_balance(user['tg_id'], -bet)
    sys_rand = secrets.SystemRandom()
    mines = set(sys_rand.sample(range(25), 6))
    active_mines_games[game_key] = {
        "bet": bet,
        "mines": mines,
        "opened": set(),
        "step": 0,
        "current_win": 0.0,
        "user_id": user['tg_id'],
        "username": user['username'] or "Игрок"
    }
    await _save_game("mines", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], bet)

    if user['tg_id'] in xray_users:
        mines_list_str = ", ".join(str(m + 1) for m in sorted(mines))
        try:
            await bot.send_message(
                user['tg_id'],
                f"👁 X-Ray: Мины находятся на клетках: {mines_list_str}."
            )
        except Exception:
            pass

    display_name = f"@{user['username']}" if user['username'] and user['username'] != "Неизвестно" else "Игрок"
    text = (
        f"{display_name}, вы начали игру минное поле (6 мин)!\n"
        f"💰 Ставка: {bet:,.0f} GRAM"
    )
    reply_markup = build_mines_keyboard(user['tg_id'], set(), mines, game_over=False)
    await message.reply(text, reply_markup=reply_markup)

@dp.callback_query(F.data.startswith("mine_c:"))
async def callback_mine_click(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    cell_idx = int(parts[1])
    owner_id = int(parts[2])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_mines_games.get(game_key)
    if not game:
        await callback.answer("Эта игра завершена!", show_alert=True)
        return
    if cell_idx in game["opened"]:
        await callback.answer("Эта клетка уже открыта!")
        return

    game["opened"].add(cell_idx)
    display_name = f"@{game['username']}" if game['username'] and game['username'] != "Неизвестно" else "Игрок"

    if cell_idx in game["mines"]:
        await add_history(game["user_id"], "Минное поле (Поражение)", -game["bet"])
        reply_markup = build_mines_keyboard(owner_id, game["opened"], game["mines"], game_over=True, is_win=False)
        text = (
            f"💥 {display_name}, вы подорвались на мине!\n"
            f"💰 Потеряно: {game['bet']:,.0f} GRAM"
        )
        del active_mines_games[game_key]
        await _remove_game("mines", f"{callback.message.chat.id}:{owner_id}")
        await callback.message.edit_text(text, reply_markup=reply_markup)
        await callback.answer("💣 БАМ! Поражение!")
        return

    game["step"] += 1
    if game["step"] == 1:
        game["current_win"] = round(game["bet"] * 1.30, 2)
    else:
        game["current_win"] = round(game["current_win"] * 1.15, 2)

    reply_markup = build_mines_keyboard(owner_id, game["opened"], game["mines"], game_over=False)
    text = (
        f"💎 {display_name}, отличный ход!\n"
        f"💰 Ставка: {game['bet']:,.0f} GRAM\n"
        f"🏆 Текущий выигрыш: {game['current_win']:,.2f} GRAM"
    )
    await callback.message.edit_text(text, reply_markup=reply_markup)
    await callback.answer(f"💎 Успешно! Выигрыш: {game['current_win']:,.2f} GRAM")

@dp.callback_query(F.data.startswith("mine_t:"))
async def callback_mine_take(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_mines_games.get(game_key)
    if not game:
        await callback.answer("Эта игра завершена!", show_alert=True)
        return
    if game["step"] == 0:
        await callback.answer("Откройте хотя бы одну клетку, чтобы забрать выигрыш!", show_alert=True)
        return

    win_amount = game["current_win"]
    await update_balance(game["user_id"], win_amount)
    await add_history(game["user_id"], "Минное поле (Забрал выигрыш)", win_amount - game["bet"])

    display_name = f"@{game['username']}" if game['username'] and game['username'] != "Неизвестно" else "Игрок"
    text = (
        f"🎉 {display_name} забирает выигрыш!\n"
        f"💰 Заработано: *{win_amount:,.2f}* GRAM"
    )
    reply_markup = build_mines_keyboard(owner_id, game["opened"], game["mines"], game_over=True, is_win=True)
    del active_mines_games[game_key]
    await _remove_game("mines", f"{callback.message.chat.id}:{owner_id}")
    await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    await callback.answer(f"✅ Вы забрали {win_amount:,.2f} GRAM!")

@dp.callback_query(F.data.startswith("mine_dis:"))
async def callback_mine_disabled(callback: types.CallbackQuery):
    await callback.answer("Эта игра уже завершена!")

# --- ДЖОКЕР ---
@dp.message(F.text.lower().startswith("джокер"))
async def game_joker(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("🃏 Введите ставку: `джокер [сумма]`")
        return
    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return
    game_key = (message.chat.id, message.from_user.id)
    if game_key in active_joker_games:
        await message.reply("❌ У вас уже есть активная игра в Джокер! Напишите `/стоп` для отмены.")
        return

    await update_balance(user['tg_id'], -bet)
    cards = ["🃏", "💎", "💥"]
    secrets.SystemRandom().shuffle(cards)
    active_joker_games[game_key] = {
        "bet": bet,
        "cards": cards,
        "user_id": user['tg_id'],
        "username": user['username'] or "Игрок"
    }
    await _save_game("joker", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], bet)

    display_name = f"@{user['username']}" if user['username'] and user['username'] != "Неизвестно" else "Игрок"
    text = (
        f"{display_name}, вы начали игру джокер!\n"
        f"💰 Ставка: {bet:,.0f} GRAM"
    )
    reply_markup = build_joker_keyboard(user['tg_id'])
    await message.reply(text, reply_markup=reply_markup)

@dp.callback_query(F.data.startswith("joker_c:"))
async def callback_joker_click(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    idx = int(parts[1])
    owner_id = int(parts[2])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_joker_games.get(game_key)
    if not game:
        await callback.answer("Эта игра завершена!", show_alert=True)
        return

    chosen_card = game["cards"][idx]
    bet = game["bet"]
    user_id = game["user_id"]
    display_name = f"@{game['username']}" if game['username'] and game['username'] != "Неизвестно" else "Игрок"
    is_win = False

    if chosen_card == "🃏":
        is_win = True
        win_amount = bet * 1.8
        await update_balance(user_id, win_amount)
        await add_history(user_id, "Джокер (ДЖОКЕР!)", win_amount - bet)
        result_text = f"🃏 *ДЖОКЕР!* {display_name}, вы сорвали куш и выиграли *{win_amount:,.0f}* GRAM!"
        alert_text = f"🃏 ДЖОКЕР! Выигрыш: {win_amount:,.0f} GRAM"
    elif chosen_card == "💎":
        is_win = True
        win_amount = bet * 1.2
        await update_balance(user_id, win_amount)
        await add_history(user_id, "Джокер (Удача)", win_amount - bet)
        result_text = f"💎 {display_name}, вам выпала карта удачи! Выигрыш: *{win_amount:,.0f}* GRAM!"
        alert_text = f"💎 Удача! Выигрыш: {win_amount:,.0f} GRAM"
    else:
        await add_history(user_id, "Джокер (Поражение)", -bet)
        result_text = f"💥 {display_name}, вы не угадали! Ставка *{bet:,.0f}* GRAM сгорела."
        alert_text = "💥 Неудача!"

    reply_markup = build_joker_keyboard(owner_id, cards=game["cards"], game_over=True, is_win=is_win)
    del active_joker_games[game_key]
    await _remove_game("joker", f"{callback.message.chat.id}:{owner_id}")
    await callback.message.edit_text(
        f"{display_name}, вы начали игру джокер!\n💰 Ставка: {bet:,.0f} GRAM\n{result_text}",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    await callback.answer(alert_text)

@dp.callback_query(F.data.startswith("joker_can:"))
async def callback_joker_cancel(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_joker_games.get(game_key)
    if not game:
        await callback.answer("Эта игра завершена!", show_alert=True)
        return
    await update_balance(game["user_id"], game["bet"])
    del active_joker_games[game_key]
    await _remove_game("joker", f"{callback.message.chat.id}:{owner_id}")
    display_name = f"@{game['username']}" if game['username'] and game['username'] != "Неизвестно" else "Игрок"
    await callback.message.edit_text(f"❌ {display_name} отменил игру «Джокер». Ставка возвращена.")
    await callback.answer("Игра отменена")

@dp.callback_query(F.data.startswith("joker_dis:"))
async def callback_joker_disabled(callback: types.CallbackQuery):
    await callback.answer("Эта игра уже завершена!")

# ----------------------------------------------------
# 8. КРЕДИТЫ И ЗАЙМЫ ПОД ПРОЦЕНТ 🏦
# ----------------------------------------------------
LOAN_INTEREST_RATE = 0.15  # 15% ставка по кредиту

async def get_user_loan(user_id: int):
    async with bot_db.execute("SELECT * FROM loans WHERE user_id = ?", (user_id,)) as cursor:
        return await cursor.fetchone()

@dp.message(F.text.lower().startswith(("кредит", "/credit", "займ", "/loan")))
async def process_loan_cmd(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    loan = await get_user_loan(user['tg_id'])

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() in ["инфо", "статус", "info"]):
        if loan and loan['repayment_amount'] > 0:
            text = (
                f"🏦 *Ваш Банковский Кредит*\n"
                f"👤 Заемщик: @{user['username']}\n"
                f"💵 Начальная сумма: `{loan['amount']:,.2f}` GHRAM\n"
                f"📈 Сумма к возврату (с 15%): `{loan['repayment_amount']:,.2f}` GHRAM\n"
                f"💡 Чтобы погасить кредит, напишите: `погасить [сумма]` или нажмите кнопку ниже."
            )
            await message.reply(text, parse_mode="Markdown", reply_markup=build_loan_keyboard(user['tg_id'], True))
        else:
            max_loan = 100000.0 + (user['hourly_income'] * 20)
            text = (
                f"🏦 *Банковский Центр Кредитования*\n"
                f"Вы можете взять заем под *15%* годовых!\n"
                f"📊 Ваш лимит кредита: `{max_loan:,.2f}` GHRAM\n"
                f"✍️ Чтобы взять кредит, напишите: `кредит [сумма]`\n"
                f"Например: `кредит 50000`"
            )
            await message.reply(text, parse_mode="Markdown", reply_markup=build_loan_keyboard(user['tg_id'], False))
        return

    if loan and loan['repayment_amount'] > 0:
        await message.reply(f"❌ У вас уже есть непогашенный заем на сумму `{loan['repayment_amount']:,.2f}` GHRAM! Сначала погасите его.", parse_mode="Markdown")
        return

    requested = parse_amount(parts[1])
    if not requested or requested <= 0:
        await message.reply("❌ Укажите корректную сумму кредита! Пример: `кредит 50000`")
        return

    max_loan = 100000.0 + (user['hourly_income'] * 20)
    if requested > max_loan and user['tg_id'] != OWNER_ID:
        await message.reply(f"❌ Максимально доступная вам сумма кредита: `{max_loan:,.2f}` GHRAM!", parse_mode="Markdown")
        return

    repay_amount = requested * (1.0 + LOAN_INTEREST_RATE)
    now = int(time.time())
    await bot_db.execute(
        "INSERT OR REPLACE INTO loans (user_id, amount, repayment_amount, created_at) VALUES (?, ?, ?, ?)",
        (user['tg_id'], requested, repay_amount, now)
    )
    await bot_db.commit()
    await update_balance(user['tg_id'], requested)
    await add_history(user['tg_id'], "Получение кредита", requested)

    text = (
        f"✅ *Кредит успешно одобрен!*\n"
        f"💰 Начислено на баланс: `+{requested:,.2f}` GHRAM\n"
        f"📊 Сумма к возврату (15%): `{repay_amount:,.2f}` GHRAM\n"
        f"Погасить можно в любое время командой `погасить`!"
    )
    await message.reply(text, parse_mode="Markdown")

@dp.message(F.text.lower().startswith(("погасить", "/repay")))
async def process_loan_repay_cmd(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    loan = await get_user_loan(user['tg_id'])
    if not loan or loan['repayment_amount'] <= 0:
        await message.reply("❌ У вас нет активных кредитов!")
        return
    parts = message.text.split()
    due = loan['repayment_amount']
    if len(parts) >= 2:
        pay_amount = parse_amount(parts[1], user['balance'])
        if not pay_amount or pay_amount <= 0:
            await message.reply("❌ Неверно указана сумма для погашения!")
            return
    else:
        pay_amount = due
    pay_amount = min(pay_amount, due)
    if not check_balance(user['tg_id'], user['balance'], pay_amount):
        await message.reply(f"❌ Недостаточно средств на балансе! Требуется: *`{pay_amount:,.2f}`* GHRAM.", parse_mode="Markdown")
        return

    await update_balance(user['tg_id'], -pay_amount)
    new_due = due - pay_amount
    if new_due <= 0.01:
        await bot_db.execute("DELETE FROM loans WHERE user_id = ?", (user['tg_id'],))
    else:
        await bot_db.execute("UPDATE loans SET repayment_amount = ? WHERE user_id = ?", (new_due, user['tg_id']))
    await bot_db.commit()
    await add_history(user['tg_id'], "Погашение кредита", -pay_amount)

    if new_due <= 0.01:
        await message.reply("🎉 *Вы полностью погасили свой кредит!* Ваш кредитный рейтинг укрепился.", parse_mode="Markdown")
    else:
        await message.reply(f"✅ Внесено *`{pay_amount:,.2f}`* GHRAM. Остаток по кредиту: *`{new_due:,.2f}`* GHRAM.", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("loan_pay_all:"))
async def callback_loan_pay_all(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваше меню!", show_alert=True)
        return
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    loan = await get_user_loan(user['tg_id'])
    if not loan or loan['repayment_amount'] <= 0:
        await callback.answer("У вас нет активного кредита!", show_alert=True)
        return
    due = loan['repayment_amount']
    if not check_balance(user['tg_id'], user['balance'], due):
        await callback.answer(f"❌ Недостаточно средств! Нужно: {due:,.2f} GHRAM", show_alert=True)
        return
    await update_balance(user['tg_id'], -due)
    await bot_db.execute("DELETE FROM loans WHERE user_id = ?", (user['tg_id'],))
    await bot_db.commit()
    await add_history(user['tg_id'], "Погашение кредита (Полное)", -due)
    await callback.message.edit_text("🎉 *Вы полностью погасили свой кредит!*", parse_mode="Markdown")
    await callback.answer("Кредит полностью погашен!")

@dp.callback_query(F.data.startswith("loan_take_fast:"))
async def callback_loan_take_fast(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваше меню!", show_alert=True)
        return
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    loan = await get_user_loan(user['tg_id'])
    if loan and loan['repayment_amount'] > 0:
        await callback.answer("У вас уже есть активный кредит!", show_alert=True)
        return
    requested = 50000.0
    repay_amount = requested * (1.0 + LOAN_INTEREST_RATE)
    now = int(time.time())
    await bot_db.execute(
        "INSERT OR REPLACE INTO loans (user_id, amount, repayment_amount, created_at) VALUES (?, ?, ?, ?)",
        (user['tg_id'], requested, repay_amount, now)
    )
    await bot_db.commit()
    await update_balance(user['tg_id'], requested)
    await add_history(user['tg_id'], "Получение кредита (Быстрый)", requested)
    text = (
        f"✅ *Быстрый заем оформлен!*\n"
        f"💰 Начислено: *`+{requested:,.2f}`* GHRAM\n"
        f"📊 Сумма к возврату: *`{repay_amount:,.2f}`* GHRAM"
    )
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer("Заем зачислен!")

@dp.callback_query(F.data.startswith("loan_refresh:"))
async def callback_loan_refresh(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваше меню!", show_alert=True)
        return
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    loan = await get_user_loan(user['tg_id'])
    if loan and loan['repayment_amount'] > 0:
        text = (
            f"🏦 *Ваш Банковский Кредит*\n"
            f"👤 Заемщик: @{user['username']}\n"
            f"💵 Начальная сумма: `{loan['amount']:,.2f}` GHRAM\n"
            f"📈 Сумма к возврату (с 15%): `{loan['repayment_amount']:,.2f}` GHRAM"
        )
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_loan_keyboard(user['tg_id'], True))
    else:
        max_loan = 100000.0 + (user['hourly_income'] * 20)
        text = (
            f"🏦 *Банковский Центр Кредитования*\n"
            f"Вы можете взять заем под *15%* годовых!\n"
            f"📊 Ваш лимит кредита: `{max_loan:,.2f}` GHRAM"
        )
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_loan_keyboard(user['tg_id'], False))
    await callback.answer("Обновлено")

# ----------------------------------------------------
# 9. БИЗНЕС МАЙНИНГ-ФЕРМА 💻⚡ (С ОБСЛУЖИВАНИЕМ И ИЗНОСОМ)
# ----------------------------------------------------
GPU_BASE_PRICE = 10000.0
LVL_BASE_PRICE = 25000.0

async def get_or_create_farm(user_id: int):
    now = int(time.time())
    await bot_db.execute(
        "INSERT OR IGNORE INTO mining_farms (user_id, level, gpu_count, last_collect) VALUES (?, 1, 1, ?)",
        (user_id, now)
    )
    await bot_db.commit()
    async with bot_db.execute("SELECT * FROM mining_farms WHERE user_id = ?", (user_id,)) as cursor:
        return await cursor.fetchone()

def calculate_farm_income(level: int, gpu_count: int, last_collect: int, broken: bool = False):
    now = int(time.time())
    elapsed_seconds = max(0, now - last_collect)
    income_per_hour = level * gpu_count * 350.0
    if broken:
        return income_per_hour, 0.0
    accumulated = (income_per_hour / 3600.0) * elapsed_seconds
    return income_per_hour, min(accumulated, income_per_hour * 24)

def farm_total_value(level: int, gpu_count: int) -> float:
    """Полная рыночная стоимость фермы (все купленные GPU + все уровни)."""
    gpu_val = sum(GPU_BASE_PRICE * (1.35 ** i) for i in range(gpu_count))
    lvl_val = sum(LVL_BASE_PRICE * (1.60 ** i) for i in range(level))
    return gpu_val + lvl_val

def farm_repair_cost(level: int, gpu_count: int) -> float:
    return round(farm_total_value(level, gpu_count) * FARM_REPAIR_RATE, 2)

def build_farm_text(user, farm, income_per_hour: float, uncollected: float, gpu_cost: float, lvl_cost: float) -> str:
    dur = farm['durability'] if farm['durability'] is not None else 100.0
    text = (
        f"🖥️ *Бизнес: Майнинг-Ферма*\n"
        f"👤 Владелец: @{user['username']}\n"
        f"⚡ Уровень системы: *{farm['level']}*\n"
        f"🧩 Видеокарт (GPU): *{farm['gpu_count']}* шт.\n"
        f"📈 Общий доход: *`{income_per_hour:,.2f}`* GHRAM/час\n"
        f"🔋 *Незабранный майнинг: `{uncollected:,.2f}`* GHRAM\n"
        f"🔩 Прочность оборудования: *`{dur:.0f}%`*\n"
    )
    if farm['broken']:
        rep_cost = farm_repair_cost(farm['level'], farm['gpu_count'])
        text += (
            f"⚠️ *ФЕРМА СЛОМАНА!* Доход не начисляется.\n"
            f"🔧 Стоимость ремонта: *`{rep_cost:,.2f}`* GHRAM ({int(FARM_REPAIR_RATE * 100)}% стоимости)\n"
        )
    text += (
        f"🛒 Цена новой GPU: *`{gpu_cost:,.0f}`* GHRAM\n"
        f"⬆️ Улучшение фермы: *`{lvl_cost:,.0f}`* GHRAM\n"
        f"💡 При каждом сборе удерживается {int(FARM_UPKEEP_RATE * 100)}% на электричество и аренду"
    )
    return text

@dp.message(F.text.lower().startswith(("майнинг", "/mining", "ферма", "бизнес")))
async def show_mining_farm(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    farm = await get_or_create_farm(user['tg_id'])
    is_broken = bool(farm['broken'])
    income_per_hour, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'], is_broken)
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    text = build_farm_text(user, farm, income_per_hour, uncollected, gpu_cost, lvl_cost)
    kb = build_mining_keyboard(user['tg_id'], gpu_cost, lvl_cost, broken=is_broken,
                               repair_cost=farm_repair_cost(farm['level'], farm['gpu_count']))
    await message.reply(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("farm_claim:"))
async def callback_farm_claim(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return
    farm = await get_or_create_farm(owner_id)
    if farm['broken']:
        await callback.answer("⚠️ Ферма сломана! Сначала выполните ремонт 🔧", show_alert=True)
        return
    _, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    if uncollected < 1.0:
        await callback.answer("⚡ Доход еще не успел накопиться! Подождите немного.", show_alert=True)
        return

    # --- ОБСЛУЖИВАНИЕ: удержание на электричество/аренду + износ прочности ---
    gross = uncollected
    upkeep = round(gross * FARM_UPKEEP_RATE, 2)
    net = round(gross - upkeep, 2)
    wear = random.randint(FARM_WEAR_MIN, FARM_WEAR_MAX)
    old_dur = farm['durability'] if farm['durability'] is not None else 100.0
    new_dur = max(0.0, old_dur - wear)
    now_broken = 1 if new_dur <= 0 else 0
    now = int(time.time())
    await bot_db.execute(
        "UPDATE mining_farms SET last_collect = ?, durability = ?, broken = ? WHERE user_id = ?",
        (now, new_dur, now_broken, owner_id)
    )
    await bot_db.commit()
    await update_balance(owner_id, net)
    await add_history(owner_id, f"Сбор майнинга (−{upkeep:,.2f} обслуживание)", net)

    alert = f"✅ Собрано {net:,.2f} GHRAM (−{upkeep:,.2f} на содержание)"
    if now_broken:
        alert += "\n⚠️ Оборудование износилось и СЛОМАЛОСЬ! Требуется ремонт."
    await callback.answer(alert, show_alert=True)

    farm = await get_or_create_farm(owner_id)
    user = await get_or_create_user(owner_id, callback.from_user.username)
    income_per_hour, new_uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'], bool(farm['broken']))
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    text = build_farm_text(user, farm, income_per_hour, new_uncollected, gpu_cost, lvl_cost)
    kb = build_mining_keyboard(owner_id, gpu_cost, lvl_cost, broken=bool(farm['broken']),
                               repair_cost=farm_repair_cost(farm['level'], farm['gpu_count']))
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data.startswith("farm_buy_gpu:"))
async def callback_farm_buy_gpu(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return
    user = await get_or_create_user(owner_id, callback.from_user.username)
    farm = await get_or_create_farm(owner_id)
    if farm['broken']:
        await callback.answer("⚠️ Ферма сломана! Сначала выполните ремонт 🔧", show_alert=True)
        return
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    if not check_balance(user['tg_id'], user['balance'], gpu_cost):
        await callback.answer(f"❌ Недостаточно средств! Нужно: {gpu_cost:,.0f} GHRAM", show_alert=True)
        return
    _, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    now = int(time.time())
    await update_balance(user['tg_id'], -gpu_cost + uncollected)
    await bot_db.execute(
        "UPDATE mining_farms SET gpu_count = gpu_count + 1, last_collect = ? WHERE user_id = ?",
        (now, owner_id)
    )
    await bot_db.commit()
    await callback.answer("🎉 Вы успешно купили новую видеокарту!", show_alert=True)

    farm = await get_or_create_farm(owner_id)
    income_per_hour, new_uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'], bool(farm['broken']))
    new_gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    text = build_farm_text(user, farm, income_per_hour, new_uncollected, new_gpu_cost, lvl_cost)
    kb = build_mining_keyboard(owner_id, new_gpu_cost, lvl_cost, broken=bool(farm['broken']),
                               repair_cost=farm_repair_cost(farm['level'], farm['gpu_count']))
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data.startswith("farm_upgrade_lvl:"))
async def callback_farm_upgrade_lvl(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return
    user = await get_or_create_user(owner_id, callback.from_user.username)
    farm = await get_or_create_farm(owner_id)
    if farm['broken']:
        await callback.answer("⚠️ Ферма сломана! Сначала выполните ремонт 🔧", show_alert=True)
        return
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    if not check_balance(user['tg_id'], user['balance'], lvl_cost):
        await callback.answer(f"❌ Недостаточно средств! Нужно: {lvl_cost:,.0f} GHRAM", show_alert=True)
        return
    _, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    now = int(time.time())
    await update_balance(user['tg_id'], -lvl_cost + uncollected)
    await bot_db.execute(
        "UPDATE mining_farms SET level = level + 1, last_collect = ? WHERE user_id = ?",
        (now, owner_id)
    )
    await bot_db.commit()
    await callback.answer("🚀 Ваша майнинг-ферма успешно улучшена!", show_alert=True)

    farm = await get_or_create_farm(owner_id)
    income_per_hour, new_uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'], bool(farm['broken']))
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    new_lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    text = build_farm_text(user, farm, income_per_hour, new_uncollected, gpu_cost, new_lvl_cost)
    kb = build_mining_keyboard(owner_id, gpu_cost, new_lvl_cost, broken=bool(farm['broken']),
                               repair_cost=farm_repair_cost(farm['level'], farm['gpu_count']))
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data.startswith("farm_repair:"))
async def callback_farm_repair(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return
    user = await get_or_create_user(owner_id, callback.from_user.username)
    farm = await get_or_create_farm(owner_id)
    if not farm['broken']:
        await callback.answer("✅ Ферма исправна, ремонт не требуется!")
        return
    cost = farm_repair_cost(farm['level'], farm['gpu_count'])
    if not check_balance(user['tg_id'], user['balance'], cost):
        await callback.answer(f"❌ Недостаточно средств! Ремонт: {cost:,.2f} GHRAM", show_alert=True)
        return
    now = int(time.time())
    await update_balance(user['tg_id'], -cost)
    await bot_db.execute(
        "UPDATE mining_farms SET broken = 0, durability = 100.0, last_collect = ? WHERE user_id = ?",
        (now, owner_id)
    )
    await bot_db.commit()
    await add_history(owner_id, "Ремонт майнинг-фермы", -cost)
    await callback.answer(f"🔧 Ферма полностью отремонтирована за {cost:,.2f} GHRAM!", show_alert=True)

    farm = await get_or_create_farm(owner_id)
    income_per_hour, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    text = build_farm_text(user, farm, income_per_hour, uncollected, gpu_cost, lvl_cost)
    kb = build_mining_keyboard(owner_id, gpu_cost, lvl_cost)
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data.startswith("farm_refresh:"))
async def callback_farm_refresh(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return
    user = await get_or_create_user(owner_id, callback.from_user.username)
    farm = await get_or_create_farm(owner_id)
    is_broken = bool(farm['broken'])
    income_per_hour, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'], is_broken)
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    text = build_farm_text(user, farm, income_per_hour, uncollected, gpu_cost, lvl_cost)
    kb = build_mining_keyboard(owner_id, gpu_cost, lvl_cost, broken=is_broken,
                               repair_cost=farm_repair_cost(farm['level'], farm['gpu_count']))
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        pass
    await callback.answer("Обновлено")

# ----------------------------------------------------
# 10. ИГРА КРАШ С РАСТУЩИМ КОЭФФИЦИЕНТОМ 🚀📈
# ----------------------------------------------------
@dp.message(F.text.lower().startswith(("краш", "/crash")))
async def game_crash(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("🚀 *Игра Краш*\nИспользование: `краш [ставка]` или `краш [ставка] [автовывод]`\nПример: `краш 1000` или `краш 1000 2.5`", parse_mode="Markdown")
        return
    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return
    auto_cashout = None
    if len(parts) >= 3:
        try:
            val = float(parts[2].replace(",", "."))
            if val > 1.01:
                auto_cashout = round(val, 2)
        except ValueError:
            pass
    game_key = (message.chat.id, message.from_user.id)
    if game_key in active_crash_games:
        await message.reply("❌ У вас уже есть запущенная игра в Краш! Напишите `/стоп` для отмены.")
        return

    await update_balance(user['tg_id'], -bet)
    sys_rand = secrets.SystemRandom()
    r = sys_rand.uniform(0.01, 0.99)
    crash_point = round(max(1.01, 0.98 / (1.0 - r)), 2)
    if crash_point > 100.0:
        crash_point = 100.0
    crash_id = secrets.token_hex(4)

    active_crash_games[game_key] = {
        "crash_id": crash_id,
        "user_id": user['tg_id'],
        "username": user['username'] or "Игрок",
        "bet": bet,
        "crash_point": crash_point,
        "current_multiplier": 1.00,
        "status": "flying",
        "auto_cashout": auto_cashout
    }
    await _save_game("crash", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], bet)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💰 ЗАБРАТЬ (1.00x)", callback_data=f"crash_out:{crash_id}:{user['tg_id']}")
    ]])
    display_name = f"@{user['username']}" if user['username'] and user['username'] != "Неизвестно" else "Игрок"
    msg = await message.reply(
        f"🚀 *Ракета вылетает!*\n"
        f"👤 Игрок: {display_name}\n"
        f"💰 Ставка: *`{bet:,.2f}`* GHRAM\n"
        f"📈 Коэффициент: *1.00x*\n"
        f"💵 Текущий выигрыш: *`{bet:,.2f}`* GHRAM",
        parse_mode="Markdown",
        reply_markup=kb
    )
    asyncio.create_task(run_crash_flight(message.chat.id, user['tg_id'], crash_id, msg))

async def run_crash_flight(chat_id: int, user_id: int, crash_id: str, msg: types.Message):
    game_key = (chat_id, user_id)
    current_mult = 1.00
    step_delay = 1.1
    try:
        while True:
            await asyncio.sleep(step_delay)
            game = active_crash_games.get(game_key)
            if not game or game['crash_id'] != crash_id or game['status'] != "flying":
                break
            increment = round(random.uniform(0.05, 0.15) if current_mult < 2.0 else random.uniform(0.15, 0.40), 2)
            current_mult = round(current_mult + increment, 2)
            game['current_multiplier'] = current_mult
            display_name = f"@{game['username']}" if game['username'] and game['username'] != "Неизвестно" else "Игрок"

            if game['auto_cashout'] and current_mult >= game['auto_cashout'] and current_mult < game['crash_point']:
                win = round(game['bet'] * game['auto_cashout'], 2)
                game['status'] = "cashed_out"
                await update_balance(user_id, win)
                await add_history(user_id, f"Краш (Автовывод {game['auto_cashout']}x)", win - game['bet'])
                text = (
                    f"✅ *АВТОВЫВОД СРАБОТАЛ!*\n"
                    f"👤 Игрок: {display_name}\n"
                    f"💰 Ставка: *`{game['bet']:,.2f}`* GHRAM\n"
                    f"🎯 Коэффициент: *{game['auto_cashout']:.2f}x*\n"
                    f"🎉 Выигрыш: *{win:,.2f}* GHRAM"
                )
                try:
                    await msg.edit_text(text, parse_mode="Markdown")
                except Exception:
                    pass
                active_crash_games.pop(game_key, None)
                await _remove_game("crash", f"{chat_id}:{user_id}")
                break

            if current_mult >= game['crash_point']:
                game['status'] = "crashed"
                await add_history(user_id, "Краш (Взрыв)", -game['bet'])
                text = (
                    f"💥 *РАКЕТА ВЗОРВАЛАСЬ НА {game['crash_point']:.2f}x!*\n"
                    f"👤 Игрок: {display_name}\n"
                    f"💰 Потеряно: *`{game['bet']:,.2f}`* GHRAM"
                )
                try:
                    await msg.edit_text(text, parse_mode="Markdown")
                except Exception:
                    pass
                active_crash_games.pop(game_key, None)
                await _remove_game("crash", f"{chat_id}:{user_id}")
                break

            curr_win = round(game['bet'] * current_mult, 2)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"💰 ЗАБРАТЬ ({current_mult:.2f}x)", callback_data=f"crash_out:{crash_id}:{user_id}")
            ]])
            text = (
                f"🚀 *Ракета летит...*\n"
                f"👤 Игрок: {display_name}\n"
                f"💰 Ставка: *`{game['bet']:,.2f}`* GHRAM\n"
                f"📈 Коэффициент: *{current_mult:.2f}x*\n"
                f"💵 Текущий выигрыш: *`{curr_win:,.2f}`* GHRAM"
            )
            try:
                await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"Error in crash flight: {e}")
        active_crash_games.pop(game_key, None)
        await _remove_game("crash", f"{chat_id}:{user_id}")

@dp.callback_query(F.data.startswith("crash_out:"))
async def callback_crash_out(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    crash_id = parts[1]
    owner_id = int(parts[2])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_crash_games.get(game_key)
    if not game or game['crash_id'] != crash_id or game['status'] != "flying":
        await callback.answer("Эта игра уже завершена!", show_alert=True)
        return
    mult = game['current_multiplier']
    win = round(game['bet'] * mult, 2)
    game['status'] = "cashed_out"
    await update_balance(owner_id, win)
    await add_history(owner_id, f"Краш (Выигрыш {mult:.2f}x)", win - game['bet'])
    display_name = f"@{game['username']}" if game['username'] and game['username'] != "Неизвестно" else "Игрок"
    text = (
        f"🎉 *ВЫ УСПЕШНО ЗАБРАЛИ ВЫИГРЫШ!*\n"
        f"👤 Игрок: {display_name}\n"
        f"💰 Ставка: *`{game['bet']:,.2f}`* GHRAM\n"
        f"📈 Зафиксировано: *{mult:.2f}x*\n"
        f"🏆 Итоговый выигрыш: *{win:,.2f}* GHRAM"
    )
    active_crash_games.pop(game_key, None)
    await _remove_game("crash", f"{callback.message.chat.id}:{owner_id}")
    try:
        await callback.message.edit_text(text, parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer(f"🎉 Вы забрали {win:,.2f} GHRAM!")

# ----------------------------------------------------
# 11. РУЛЕТКА (УМНЫЙ ФИЛЬТР) 🎰 (HOUSE EDGE: 1.95x НА РАВНЫЕ ШАНСЫ)
# ----------------------------------------------------
@dp.message(F.text.lower().in_(["отменить", "/отменить"]))
async def roulette_cancel(message: types.Message):
    key = (message.chat.id, message.from_user.id)
    if key in active_roulette_bets and active_roulette_bets[key]:
        total_refund = sum(b['bet'] for b in active_roulette_bets[key])
        await update_balance(message.from_user.id, total_refund)
        active_roulette_bets[key] = []
        await _remove_game("roulette", f"{message.chat.id}:{message.from_user.id}")
        await message.reply(f"🚫 Все ваши ставки на этот раунд отменены. Возвращено: *`{total_refund:,.2f}`* монет.")
    else:
        await message.reply("❌ У вас нет активных ставок.")

@dp.message(F.text.lower().in_(["ставки", "/ставки"]))
async def roulette_show_bets(message: types.Message):
    key = (message.chat.id, message.from_user.id)
    bets = active_roulette_bets.get(key, [])
    if not bets:
        await message.reply("🎰 В текущем раунде у вас нет сделанных ставок.")
        return
    text = "🎰 *Ваши ставки в текущем раунде:*\n"
    for idx, b in enumerate(bets, 1):
        text += f"{idx}. *`{b['bet']:,.2f}`* монет на *{b['type']}*\n"
    text += "\nНапишите `крутить` чтобы запустить рулетку!"
    await message.reply(text, parse_mode="Markdown")

@dp.message(F.text.lower().in_(["удвоить", "/удвоить"]))
async def roulette_double(message: types.Message):
    key = (message.chat.id, message.from_user.id)
    bets = active_roulette_bets.get(key, [])
    if not bets:
        await message.reply("❌ У вас нет ставок для удвоения.")
        return
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    add_req = sum(b['bet'] for b in bets)
    if not check_balance(user['tg_id'], user['balance'], add_req):
        await message.reply(f"❌ Недостаточно средств для удвоения! Нужно еще *`{add_req:,.2f}`* монет.")
        return
    await update_balance(user['tg_id'], -add_req)
    for b in bets:
        b['bet'] *= 2
    total_bet = sum(b['bet'] for b in bets)
    await _save_game("roulette", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], total_bet)
    await message.reply("⚡ Все ваши ставки удвоены!")

@dp.message(F.text.lower().in_(["повторить", "/повторить"]))
async def roulette_repeat(message: types.Message):
    key = (message.chat.id, message.from_user.id)
    last = last_roulette_bets.get(key, [])
    if not last:
        await message.reply("❌ У вас нет предыдущих ставок для повтора.")
        return
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    req = sum(b['bet'] for b in last)
    if not check_balance(user['tg_id'], user['balance'], req):
        await message.reply(f"❌ Недостаточно монет для повтора! Нужно *`{req:,.2f}`* монет.")
        return
    await update_balance(user['tg_id'], -req)
    import copy
    active_roulette_bets[key] = copy.deepcopy(last)
    await _save_game("roulette", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], req)
    await message.reply(f"🔄 Повторено {len(last)} ставок.")

def is_roulette_bet(message: types.Message) -> bool:
    if not message.text:
        return False
    parts = message.text.strip().lower().split(maxsplit=1)
    if len(parts) != 2:
        return False
    bet_str, bet_type = parts[0], parts[1]
    is_valid_type = False
    if bet_type in ["красное", "черное", "red", "black", "нечет", "чет", "odd", "even", "0"]:
        is_valid_type = True
    elif bet_type.isdigit() and 0 <= int(bet_type) <= 36:
        is_valid_type = True
    elif "-" in bet_type:
        p = bet_type.split("-")
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            if 0 <= int(p[0]) <= int(p[1]) <= 36:
                is_valid_type = True
    if not is_valid_type:
        return False
    amt = parse_amount(bet_str)
    return amt is not None and amt > 0

@dp.message(is_roulette_bet)
async def roulette_place_bet(message: types.Message):
    parts = message.text.strip().lower().split(maxsplit=1)
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    bet_str, bet_type = parts[0], parts[1]
    bet = parse_amount(bet_str, user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств для ставки!")
        return
    await update_balance(user['tg_id'], -bet)
    key = (message.chat.id, message.from_user.id)
    if key not in active_roulette_bets:
        active_roulette_bets[key] = []
    active_roulette_bets[key].append({"bet": bet, "type": bet_type})
    total_bet = sum(b['bet'] for b in active_roulette_bets[key])
    await _save_game("roulette", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], total_bet)
    await message.reply(f"✅ Принята ставка *`{bet:,.2f}`* монет на *{bet_type}*.\nНапишите `крутить` для запуска!", parse_mode="Markdown")

@dp.message(F.text.lower().in_(["крутить", "го", "вращать", "/spin"]))
async def roulette_spin(message: types.Message):
    key = (message.chat.id, message.from_user.id)
    bets = active_roulette_bets.get(key, [])
    if not bets:
        await message.reply("🎰 Сначала сделайте ставку! Пример: `100 красное`.")
        return
    num = secrets.randbelow(37)
    color = "🟢 Зеро" if num == 0 else ("🔴 Красное" if num in RED_NUMBERS else "⚫ Черное")
    total_win = 0.0
    total_bet = sum(b['bet'] for b in bets)
    for b in bets:
        bt = b['type']
        amt = b['bet']
        # HOUSE EDGE: равные шансы платят 1.95x вместо 2.0x
        if bt in ["красное", "red"] and num in RED_NUMBERS:
            total_win += amt * EVEN_PAYOUT
        elif bt in ["черное", "black"] and num in BLACK_NUMBERS:
            total_win += amt * EVEN_PAYOUT
        elif bt in ["чет", "even"] and num > 0 and num % 2 == 0:
            total_win += amt * EVEN_PAYOUT
        elif bt in ["нечет", "odd"] and num % 2 == 1:
            total_win += amt * EVEN_PAYOUT
        elif bt.isdigit() and int(bt) == num:
            total_win += amt * 36
        elif "-" in bt:
            p = bt.split("-")
            low, high = int(p[0]), int(p[1])
            if low <= num <= high:
                count = (high - low + 1)
                mult = 36.0 / count
                total_win += amt * mult

    import copy
    last_roulette_bets[key] = copy.deepcopy(bets)
    active_roulette_bets[key] = []
    await _remove_game("roulette", f"{message.chat.id}:{message.from_user.id}")

    if total_win > 0:
        await update_balance(message.from_user.id, total_win)
        await add_history(message.from_user.id, "Рулетка (Выигрыш)", total_win - total_bet)
        res_text = f"🎉 *Вы выиграли {total_win:,.2f} монет!*"
    else:
        await add_history(message.from_user.id, "Рулетка (Проигрыш)", -total_bet)
        res_text = f"❌ Вы потеряли {total_bet:,.2f} монет."

    await message.reply(
        f"🎰 *Рулетка крутится...*\n"
        f"Выпало число: *{num}* ({color})\n"
        f"{res_text}",
        parse_mode="Markdown"
    )

# ----------------------------------------------------
# 13. НОВЫЕ ИГРЫ: КОСТИ 🎲, МОНЕТКА 🪙, БЛЭКДЖЕК 🃏,
#     КРЕСТИКИ-НОЛИКИ ❌⭕, СЛОТЫ 🎰
# ----------------------------------------------------

# ==================== 🎲 КОСТИ (PvE) ====================
@dp.message(F.text.lower().startswith(("кости", "/кости", "/dice")))
async def game_dice_entry(message: types.Message):
    parts = message.text.split()
    if len(parts) >= 2 and parts[1].lower() in ("пвп", "pvp"):
        await start_dice_pvp(message, parts)
        return
    await start_dice_pve(message, parts)

async def start_dice_pve(message: types.Message, parts: list):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    if len(parts) < 2:
        await message.reply(
            "🎲 *КОСТИ*\n"
            "Использование:\n"
            "• `кости [сумма]` — игра против бота\n"
            "• `кости пвп [@username/ID] [сумма]` — дуэль с игроком",
            parse_mode="Markdown"
        )
        return
    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return
    game_key = (message.chat.id, user['tg_id'])
    if game_key in active_dice_games:
        await message.reply("❌ У вас уже есть активная игра в кости! Напишите `/стоп` для отмены.")
        return

    await update_balance(user['tg_id'], -bet)
    sys_rand = secrets.SystemRandom()
    bot_roll = [sys_rand.randint(1, 6), sys_rand.randint(1, 6)]
    active_dice_games[game_key] = {
        "bet": bet,
        "bot_roll": bot_roll,
        "user_id": user['tg_id'],
        "username": user['username'] or "Игрок"
    }
    await _save_game("dice", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], bet)

    if user['tg_id'] in xray_users:
        try:
            await bot.send_message(
                user['tg_id'],
                f"👁 X-Ray: у бота выпадет {DICE_FACES[bot_roll[0]]} и {DICE_FACES[bot_roll[1]]} (сумма {sum(bot_roll)})."
            )
        except Exception:
            pass

    display_name = display_name_of(user['username'])
    text = (
        f"🎲 *КОСТИ — ДУЭЛЬ С БОТОМ*\n"
        f"👤 Игрок: {display_name}\n"
        f"💰 Ставка: *`{bet:,.2f}`* GHRAM\n\n"
        f"У кого сумма двух костей больше — тот победил!\n"
        f"🏆 Выплата: *x{EVEN_PAYOUT}* | 🤝 Ничья — возврат ставки"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎲 БРОСИТЬ КОСТИ", callback_data=f"dice_roll:{user['tg_id']}")
    ]])
    await message.reply(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("dice_roll:"))
async def callback_dice_roll(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_dice_games.get(game_key)
    if not game:
        await callback.answer("Эта игра завершена!", show_alert=True)
        return

    sys_rand = secrets.SystemRandom()
    p_roll = [sys_rand.randint(1, 6), sys_rand.randint(1, 6)]
    b_roll = game["bot_roll"]
    p_sum, b_sum = sum(p_roll), sum(b_roll)
    bet = game["bet"]
    display_name = display_name_of(game["username"])

    if p_sum > b_sum:
        win = round(bet * EVEN_PAYOUT, 2)
        await update_balance(owner_id, win)
        await add_history(owner_id, "Кости (Победа)", win - bet)
        result = f"🎉 *ПОБЕДА!* {display_name}, ваш выигрыш: *{win:,.2f}* GHRAM"
        alert = f"🎉 Победа! +{win:,.2f} GHRAM"
    elif p_sum < b_sum:
        await add_history(owner_id, "Кости (Поражение)", -bet)
        result = f"💀 *ПОРАЖЕНИЕ!* Бот забрал *`{bet:,.2f}`* GHRAM"
        alert = "💀 Поражение!"
    else:
        await update_balance(owner_id, bet)
        await add_history(owner_id, "Кости (Ничья)", 0)
        result = f"🤝 *НИЧЬЯ!* Ставка *`{bet:,.2f}`* возвращена"
        alert = "🤝 Ничья, ставка возвращена"

    text = (
        f"🎲 *КОСТИ — РЕЗУЛЬТАТ*\n\n"
        f"👤 Вы: {DICE_FACES[p_roll[0]]} {DICE_FACES[p_roll[1]]} — *{p_sum}*\n"
        f"🤖 Бот: {DICE_FACES[b_roll[0]]} {DICE_FACES[b_roll[1]]} — *{b_sum}*\n\n"
        f"{result}"
    )
    del active_dice_games[game_key]
    await _remove_game("dice", f"{callback.message.chat.id}:{owner_id}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅", callback_data=f"dice_dis:{owner_id}")
    ]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer(alert)

@dp.callback_query(F.data.startswith("dice_dis:"))
async def callback_dice_disabled(callback: types.CallbackQuery):
    await callback.answer("Эта игра уже завершена!")

# ==================== 🎲 КОСТИ (PvP) ====================
async def start_dice_pvp(message: types.Message, parts: list):
    sender = await get_or_create_user(message.from_user.id, message.from_user.username)
    target_user = None
    bet = None

    if message.reply_to_message:
        if len(parts) >= 3:
            bet = parse_amount(parts[2], sender['balance'])
            target_id = message.reply_to_message.from_user.id
            target_user = await get_or_create_user(target_id, message.reply_to_message.from_user.username)
    elif len(parts) >= 4:
        target_user = await get_user_by_identifier(parts[2])
        bet = parse_amount(parts[3], sender['balance'])
        if not target_user:
            await message.reply("❌ Пользователь не найден в базе данных бота!")
            return
    else:
        await message.reply("🎲 Использование: `кости пвп [@username/ID] [сумма]` или ответьте на сообщение: `кости пвп [сумма]`", parse_mode="Markdown")
        return

    if not bet or bet <= 0:
        await message.reply("❌ Ставка должна быть больше 0.")
        return
    if target_user['tg_id'] == sender['tg_id']:
        await message.reply("❌ Нельзя вызывать на дуэль самого себя!")
        return
    if not check_balance(sender['tg_id'], sender['balance'], bet):
        await message.reply(f"❌ У вас недостаточно монет (нужно {bet:,.2f}).")
        return
    if not check_balance(target_user['tg_id'], target_user['balance'], bet):
        await message.reply(f"❌ У соперника недостаточно монет (нужно {bet:,.2f}).")
        return

    for d in list(pending_dice_pvp.values()) + list(active_dice_pvp.values()):
        ids = {d.get('p1_id'), d.get('p2_id'), d.get('challenger_id'), d.get('target_id')}
        if sender['tg_id'] in ids or target_user['tg_id'] in ids:
            await message.reply("❌ Один из участников уже находится в игре в кости!")
            return

    await update_balance(sender['tg_id'], -bet)
    dice_id = secrets.token_hex(4)

    target_mention = f"@{target_user['username']}" if target_user['username'] and target_user['username'] != "Неизвестно" else f"ID {target_user['tg_id']}"
    sender_mention = f"@{sender['username']}" if sender['username'] and sender['username'] != "Неизвестно" else f"ID {sender['tg_id']}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Согласиться ✅", callback_data=f"dicepvp_acc:{dice_id}"),
        InlineKeyboardButton(text="Отказаться ⛔", callback_data=f"dicepvp_dec:{dice_id}")
    ]])

    sent_msg = await message.answer(
        f"🎲 {target_mention}, вас вызывают на дуэль в кости!\n"
        f"👤 Инициатор: {sender_mention}\n"
        f"💰 Ставка: *{bet:,.2f}* GHRAM (банк {bet * 2:,.2f})\n"
        f"🏆 Победитель забирает банк (комиссия бота {PVP_RAKE*100:.1f}%)",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await _save_game("dicepvp_pending", dice_id, message.chat.id, sender['tg_id'], bet)

    async def invite_timeout():
        await asyncio.sleep(180)
        if dice_id in pending_dice_pvp:
            d = pending_dice_pvp.pop(dice_id, None)
            if d:
                await update_balance(d['challenger_id'], d['bet'])
                await _remove_game("dicepvp_pending", dice_id)
                try:
                    await sent_msg.edit_text("⏳ Время ожидания истекло (3 мин). Игра отменена, ставка возвращена.")
                except Exception:
                    pass

    timer_task = asyncio.create_task(invite_timeout())
    pending_dice_pvp[dice_id] = {
        "chat_id": message.chat.id,
        "challenger_id": sender['tg_id'],
        "challenger_name": sender_mention,
        "target_id": target_user['tg_id'],
        "target_name": target_mention,
        "bet": bet,
        "msg": sent_msg,
        "timer_task": timer_task
    }

@dp.callback_query(F.data.startswith("dicepvp_acc:"))
async def callback_dice_pvp_accept(callback: types.CallbackQuery):
    dice_id = callback.data.split(":")[1]
    d = pending_dice_pvp.get(dice_id)
    if not d:
        await callback.answer("Эта игра больше неактивна!", show_alert=True)
        return
    if callback.from_user.id != d["target_id"]:
        await callback.answer("❌ Принять вызов может только вызванный игрок!", show_alert=True)
        return
    target_user = await get_or_create_user(d["target_id"], callback.from_user.username)
    if not check_balance(target_user['tg_id'], target_user['balance'], d['bet']):
        await callback.answer("❌ У вас недостаточно монет для принятия дуэли!", show_alert=True)
        return

    d["timer_task"].cancel()
    del pending_dice_pvp[dice_id]
    await _remove_game("dicepvp_pending", dice_id)
    await update_balance(target_user['tg_id'], -d['bet'])

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎲 БРОСИТЬ КОСТИ", callback_data=f"dicepvp_roll:{dice_id}")
    ]])

    msg_text = (
        f"🎲 *ДУЭЛЬ В КОСТИ НАЧАЛАСЬ!*\n\n"
        f"👤 {d['challenger_name']} — против — {d['target_name']}\n"
        f"💰 Банк: *{d['bet'] * 2:,.2f}* GHRAM\n\n"
        f"Оба игрока бросают кости — жмите кнопку! У кого сумма больше, тот победил."
    )
    await callback.message.edit_text(msg_text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer("Дуэль принята! Бросайте кости!")

    await _save_game("dicepvp_active", dice_id, callback.message.chat.id, d['challenger_id'], d['bet'])
    await _save_game("dicepvp_active", dice_id, callback.message.chat.id, d['target_id'], d['bet'])

    async def roll_timeout(current_id):
        await asyncio.sleep(180)
        if current_id in active_dice_pvp:
            ad = active_dice_pvp.pop(current_id, None)
            if ad:
                await update_balance(ad["p1_id"], ad["bet"])
                await update_balance(ad["p2_id"], ad["bet"])
                await _remove_game("dicepvp_active", current_id)
                try:
                    await ad["msg"].edit_text("⏳ Время ожидания истекло (3 мин). Игра отменена, деньги возвращены.")
                except Exception:
                    pass

    timer_task = asyncio.create_task(roll_timeout(dice_id))
    active_dice_pvp[dice_id] = {
        "chat_id": callback.message.chat.id,
        "p1_id": d["challenger_id"],
        "p1_name": d["challenger_name"],
        "p2_id": d["target_id"],
        "p2_name": d["target_name"],
        "bet": d["bet"],
        "rolls": {"p1": None, "p2": None},
        "msg": callback.message,
        "timer_task": timer_task
    }

@dp.callback_query(F.data.startswith("dicepvp_dec:"))
async def callback_dice_pvp_decline(callback: types.CallbackQuery):
    dice_id = callback.data.split(":")[1]
    d = pending_dice_pvp.get(dice_id)
    if not d:
        await callback.answer("Эта игра больше неактивна!", show_alert=True)
        return
    if callback.from_user.id != d["target_id"]:
        await callback.answer("❌ Отклонить вызов может только вызванный игрок!", show_alert=True)
        return
    d["timer_task"].cancel()
    del pending_dice_pvp[dice_id]
    await _remove_game("dicepvp_pending", dice_id)
    await update_balance(d["challenger_id"], d["bet"])
    await callback.message.edit_text(f"⛔ {d['target_name']} отклонил(а) дуэль в кости. Ставка возвращена.")
    await callback.answer("Дуэль отклонена")

@dp.callback_query(F.data.startswith("dicepvp_roll:"))
async def callback_dice_pvp_roll(callback: types.CallbackQuery):
    dice_id = callback.data.split(":")[1]
    d = active_dice_pvp.get(dice_id)
    if not d:
        await callback.answer("Эта игра уже завершена!", show_alert=True)
        return
    uid = callback.from_user.id
    if uid not in (d["p1_id"], d["p2_id"]):
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    slot = "p1" if uid == d["p1_id"] else "p2"
    if d["rolls"][slot] is not None:
        await callback.answer("Вы уже бросили кости! Ждите соперника.")
        return

    sys_rand = secrets.SystemRandom()
    d["rolls"][slot] = [sys_rand.randint(1, 6), sys_rand.randint(1, 6)]

    if d["rolls"]["p1"] is None or d["rolls"]["p2"] is None:
        rolled_name = d["p1_name"] if slot == "p1" else d["p2_name"]
        # >>> ИСПРАВЛЕНИЕ: при редактировании сообщения ОБЯЗАТЕЛЬНО передаём клавиатуру,
        # >>> иначе Telegram удаляет инлайн-кнопку и второй игрок не может бросить кости.
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎲 БРОСИТЬ КОСТИ", callback_data=f"dicepvp_roll:{dice_id}")
        ]])
        try:
            await callback.message.edit_text(
                f"🎲 *ДУЭЛЬ В КОСТИ*\n\n"
                f"✅ {rolled_name} бросил(а) кости!\n"
                f"⏳ Ожидание второго игрока...",
                parse_mode="Markdown",
                reply_markup=kb
            )
        except Exception:
            pass
        await callback.answer("🎲 Вы бросили кости!")
        return

    # Оба бросили — определяем победителя
    d["timer_task"].cancel()
    r1, r2 = d["rolls"]["p1"], d["rolls"]["p2"]
    s1, s2 = sum(r1), sum(r2)
    bet = d["bet"]
    pot = bet * 2
    win = round(pot * (1 - PVP_RAKE), 2)

    if s1 > s2:
        winner_id, winner_name = d["p1_id"], d["p1_name"]
        loser_id, loser_name = d["p2_id"], d["p2_name"]
    elif s2 > s1:
        winner_id, winner_name = d["p2_id"], d["p2_name"]
        loser_id, loser_name = d["p1_id"], d["p1_name"]
    else:
        winner_id = None

    if winner_id is None:
        await update_balance(d["p1_id"], bet)
        await update_balance(d["p2_id"], bet)
        await add_history(d["p1_id"], "Кости PvP (Ничья)", 0)
        await add_history(d["p2_id"], "Кости PvP (Ничья)", 0)
        result = "🤝 *НИЧЬЯ!* Ставки возвращены обоим игрокам."
    else:
        await update_balance(winner_id, win)
        await add_history(winner_id, "Кости PvP (Победа)", win - bet)
        await add_history(loser_id, "Кости PvP (Поражение)", -bet)
        result = f"👑 *ПОБЕЖДАЕТ {winner_name}!*\n💰 Выигрыш: *{win:,.2f}* GHRAM"

    text = (
        f"🎲 *КОСТИ — ИТОГ ДУЭЛИ*\n\n"
        f"👤 {d['p1_name']}: {DICE_FACES[r1[0]]} {DICE_FACES[r1[1]]} — *{s1}*\n"
        f"👤 {d['p2_name']}: {DICE_FACES[r2[0]]} {DICE_FACES[r2[1]]} — *{s2}*\n\n"
        f"{result}"
    )
    del active_dice_pvp[dice_id]
    await _remove_game("dicepvp_active", dice_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅", callback_data="dicepvp_dis")
    ]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer("🎲 Дуэль завершена!")

@dp.callback_query(F.data == "dicepvp_dis")
async def callback_dice_pvp_disabled(callback: types.CallbackQuery):
    await callback.answer("Эта игра уже завершена!")

# ==================== 🪙 МОНЕТКА ====================
@dp.message(F.text.lower().startswith(("монетка", "/coin", "/монетка")))
async def game_coin(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("🪙 Введите ставку: `монетка [сумма]`", parse_mode="Markdown")
        return
    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return
    game_key = (message.chat.id, user['tg_id'])
    if game_key in active_coin_games:
        await message.reply("❌ У вас уже есть активная игра в монетку! Напишите `/стоп` для отмены.")
        return

    await update_balance(user['tg_id'], -bet)
    sys_rand = secrets.SystemRandom()
    flip = sys_rand.choice(["heads", "tails"])
    active_coin_games[game_key] = {
        "bet": bet,
        "flip": flip,
        "user_id": user['tg_id'],
        "username": user['username'] or "Игрок"
    }
    await _save_game("coin", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], bet)

    if user['tg_id'] in xray_users:
        try:
            await bot.send_message(user['tg_id'], f"👁 X-Ray: выпадет {'🦅 ОРЁЛ' if flip == 'heads' else '🪙 РЕШКА'}.")
        except Exception:
            pass

    display_name = display_name_of(user['username'])
    text = (
        f"🪙 *МОНЕТКА*\n"
        f"👤 Игрок: {display_name}\n"
        f"💰 Ставка: *`{bet:,.2f}`* GHRAM\n\n"
        f"Выберите сторону! Выплата: *x{EVEN_PAYOUT}*"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🦅 ОРЁЛ", callback_data=f"coin_pick:heads:{user['tg_id']}"),
        InlineKeyboardButton(text="🪙 РЕШКА", callback_data=f"coin_pick:tails:{user['tg_id']}")
    ]])
    await message.reply(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("coin_pick:"))
async def callback_coin_pick(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    choice = parts[1]
    owner_id = int(parts[2])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_coin_games.get(game_key)
    if not game:
        await callback.answer("Эта игра завершена!", show_alert=True)
        return

    flip = game["flip"]
    bet = game["bet"]
    display_name = display_name_of(game["username"])

    try:
        await callback.message.edit_text("🪙 Монетка взлетает... 🌀", parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer()
    await asyncio.sleep(1.0)

    choice_name = "🦅 ОРЁЛ" if choice == "heads" else "🪙 РЕШКА"
    flip_name = "🦅 ОРЁЛ" if flip == "heads" else "🪙 РЕШКА"

    if choice == flip:
        win = round(bet * EVEN_PAYOUT, 2)
        await update_balance(owner_id, win)
        await add_history(owner_id, "Монетка (Победа)", win - bet)
        result = f"🎉 *ВЫ УГАДАЛИ!* {display_name}, ваш выигрыш: *{win:,.2f}* GHRAM"
        alert = f"🎉 Победа! +{win:,.2f} GHRAM"
    else:
        await add_history(owner_id, "Монетка (Поражение)", -bet)
        result = f"💀 *МИМО!* {display_name}, ставка *`{bet:,.2f}`* сгорела"
        alert = "💀 Не повезло!"

    text = (
        f"🪙 *МОНЕТКА — РЕЗУЛЬТАТ*\n\n"
        f"🎯 Ваш выбор: {choice_name}\n"
        f"✨ Выпало: *{flip_name}*\n\n"
        f"{result}"
    )
    del active_coin_games[game_key]
    await _remove_game("coin", f"{callback.message.chat.id}:{owner_id}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅", callback_data=f"coin_dis:{owner_id}")
    ]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer(alert)

@dp.callback_query(F.data.startswith("coin_dis:"))
async def callback_coin_disabled(callback: types.CallbackQuery):
    await callback.answer("Эта игра уже завершена!")

# ==================== 🃏 БЛЭКДЖЕК ====================
BJ_SUITS = ["♠️", "♥️", "♦️", "♣️"]
BJ_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def bj_new_deck() -> list:
    deck = [(r, s) for r in BJ_RANKS for s in BJ_SUITS]
    secrets.SystemRandom().shuffle(deck)
    return deck

def bj_fmt(cards: list) -> str:
    return " ".join(f"{r}{s}" for r, s in cards)

def bj_value(cards: list) -> int:
    total = 0
    aces = 0
    for r, s in cards:
        if r == "A":
            total += 11
            aces += 1
        elif r in ("J", "Q", "K"):
            total += 10
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def build_blackjack_keyboard(user_id: int, game_over: bool = False):
    if game_over:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅", callback_data=f"bj_dis:{user_id}")
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ ЕЩЁ", callback_data=f"bj_hit:{user_id}"),
        InlineKeyboardButton(text="✋ ХВАТИТ", callback_data=f"bj_stand:{user_id}")
    ]])

def bj_render(game: dict, reveal_dealer: bool = False, result_line: str = "") -> str:
    dealer_cards = game["dealer"]
    if reveal_dealer:
        dealer_str = f"{bj_fmt(dealer_cards)} (*{bj_value(dealer_cards)}*)"
    else:
        dealer_str = f"{dealer_cards[0][0]}{dealer_cards[0][1]} 🎴"
    text = (
        f"🃏 *БЛЭКДЖЕК*\n"
        f"👤 Игрок: {display_name_of(game['username'])}\n"
        f"💰 Ставка: *`{game['bet']:,.2f}`* GHRAM\n\n"
        f"🤖 Дилер: {dealer_str}\n"
        f"👤 Вы: {bj_fmt(game['player'])} (*{bj_value(game['player'])}*)\n"
    )
    if result_line:
        text += f"\n{result_line}\n"
    return text

@dp.message(F.text.lower().in_(["21"]) | F.text.lower().startswith(("блэкджек", "блекджек", "/blackjack", "/21", "21 ")))
async def game_blackjack(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            "🃏 *БЛЭКДЖЕК (21)*\n"
            "Введите ставку: `блэкджек [сумма]`\n\n"
            f"Обычная победа — *x{BJ_WIN_PAYOUT}*, натуральный блэкджек — *x{BJ_BLACKJACK_PAYOUT}*",
            parse_mode="Markdown"
        )
        return
    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return
    game_key = (message.chat.id, user['tg_id'])
    if game_key in active_blackjack_games:
        await message.reply("❌ У вас уже есть активная игра в блэкджек! Напишите `/стоп` для отмены.")
        return

    await update_balance(user['tg_id'], -bet)
    deck = bj_new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    game = {
        "bet": bet,
        "deck": deck,
        "player": player,
        "dealer": dealer,
        "user_id": user['tg_id'],
        "username": user['username'] or "Игрок"
    }
    pv, dv = bj_value(player), bj_value(dealer)

    # Мгновенный исход при натуральном блэкджеке
    if pv == 21 or dv == 21:
        if pv == 21 and dv == 21:
            await update_balance(user['tg_id'], bet)
            await add_history(user['tg_id'], "Блэкджек (Ничья)", 0)
            result = "🤝 *ДВОЙНОЙ БЛЭКДЖЕК!* Ничья — ставка возвращена."
        elif pv == 21:
            win = round(bet * BJ_BLACKJACK_PAYOUT, 2)
            await update_balance(user['tg_id'], win)
            await add_history(user['tg_id'], "Блэкджек (Натуральный!)", win - bet)
            result = f"🃏 *БЛЭКДЖЕК!* Выигрыш: *{win:,.2f}* GHRAM (x{BJ_BLACKJACK_PAYOUT})"
        else:
            await add_history(user['tg_id'], "Блэкджек (Поражение)", -bet)
            result = f"💀 *У дилера блэкджек!* Ставка *`{bet:,.2f}`* сгорела."
        text = bj_render(game, reveal_dealer=True, result_line=result)
        await message.reply(text, parse_mode="Markdown", reply_markup=build_blackjack_keyboard(user['tg_id'], game_over=True))
        return

    active_blackjack_games[game_key] = game
    await _save_game("blackjack", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], bet)
    await message.reply(bj_render(game), parse_mode="Markdown", reply_markup=build_blackjack_keyboard(user['tg_id']))

async def do_bj_stand(callback: types.CallbackQuery, game: dict, game_key: tuple):
    while bj_value(game["dealer"]) < 17:
        game["dealer"].append(game["deck"].pop())
    pv, dv = bj_value(game["player"]), bj_value(game["dealer"])
    bet = game["bet"]
    owner_id = game["user_id"]

    if dv > 21 or pv > dv:
        win = round(bet * BJ_WIN_PAYOUT, 2)
        await update_balance(owner_id, win)
        await add_history(owner_id, "Блэкджек (Победа)", win - bet)
        result = f"🎉 *ПОБЕДА!* Выигрыш: *{win:,.2f}* GHRAM"
        alert = f"🎉 Победа! +{win:,.2f} GHRAM"
    elif pv < dv:
        await add_history(owner_id, "Блэкджек (Поражение)", -bet)
        result = f"💀 *ПОРАЖЕНИЕ!* Дилер забрал *`{bet:,.2f}`* GHRAM"
        alert = "💀 Поражение!"
    else:
        await update_balance(owner_id, bet)
        await add_history(owner_id, "Блэкджек (Ничья)", 0)
        result = f"🤝 *НИЧЬЯ!* Ставка *`{bet:,.2f}`* возвращена"
        alert = "🤝 Ничья"

    active_blackjack_games.pop(game_key, None)
    await _remove_game("blackjack", f"{game_key[0]}:{game_key[1]}")
    text = bj_render(game, reveal_dealer=True, result_line=result)
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_blackjack_keyboard(owner_id, game_over=True))
    await callback.answer(alert)

@dp.callback_query(F.data.startswith("bj_hit:"))
async def callback_bj_hit(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_blackjack_games.get(game_key)
    if not game:
        await callback.answer("Эта игра завершена!", show_alert=True)
        return

    game["player"].append(game["deck"].pop())
    pv = bj_value(game["player"])

    if pv > 21:
        await add_history(owner_id, "Блэкджек (Перебор)", -game["bet"])
        active_blackjack_games.pop(game_key, None)
        await _remove_game("blackjack", f"{game_key[0]}:{game_key[1]}")
        text = bj_render(game, reveal_dealer=True, result_line=f"💥 *ПЕРЕБОР ({pv})!* Ставка *`{game['bet']:,.2f}`* сгорела.")
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_blackjack_keyboard(owner_id, game_over=True))
        await callback.answer("💥 Перебор! Поражение!")
        return

    if pv == 21:
        await callback.answer("🔥 21! Автоматический стоп.")
        await do_bj_stand(callback, game, game_key)
        return

    await callback.message.edit_text(bj_render(game), parse_mode="Markdown", reply_markup=build_blackjack_keyboard(owner_id))
    await callback.answer(f"🃏 У вас {pv}")

@dp.callback_query(F.data.startswith("bj_stand:"))
async def callback_bj_stand(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    game = active_blackjack_games.get(game_key)
    if not game:
        await callback.answer("Эта игра завершена!", show_alert=True)
        return
    await do_bj_stand(callback, game, game_key)

@dp.callback_query(F.data.startswith("bj_dis:"))
async def callback_bj_disabled(callback: types.CallbackQuery):
    await callback.answer("Эта игра уже завершена!")

# ==================== ❌⭕ КРЕСТИКИ-НОЛИКИ ====================
TTT_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]

def ttt_check(board: list):
    for a, b, c in TTT_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return "draw"
    return None

def build_ttt_keyboard(game_id: str, board: list, game_over: bool = False):
    rows = []
    for r in range(3):
        line = []
        for c in range(3):
            i = r * 3 + c
            sym = board[i] if board[i] else "▫️"
            cb = f"ttt_dis:{game_id}" if (game_over or board[i]) else f"ttt_c:{game_id}:{i}"
            line.append(InlineKeyboardButton(text=sym, callback_data=cb))
        rows.append(line)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def ttt_bot_move(board: list, bot_sym: str) -> int:
    human_sym = "⭕" if bot_sym == "❌" else "❌"
    empty = [i for i, v in enumerate(board) if not v]
    for sym in (bot_sym, human_sym):  # 1) выиграть 2) заблокировать
        for i in empty:
            test = board.copy()
            test[i] = sym
            if ttt_check(test) == sym:
                return i
    if not board[4]:
        return 4
    corners = [i for i in (0, 2, 6, 8) if not board[i]]
    if corners:
        return secrets.choice(corners)
    return secrets.choice(empty)

@dp.message(F.text.lower().startswith(("ттт", "/ttt", "tictactoe", "крестики")))
async def game_ttt_entry(message: types.Message):
    parts = message.text.split()
    if len(parts) >= 2 and parts[1].lower() in ("бот", "bot"):
        await start_ttt_bot(message, parts)
    else:
        await start_ttt_pvp(message, parts)

async def start_ttt_bot(message: types.Message, parts: list):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    if len(parts) < 3:
        await message.reply("⭕ Введите ставку: `ттт бот [сумма]`", parse_mode="Markdown")
        return
    bet = parse_amount(parts[2], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return
    for g in active_ttt_games.values():
        if g["chat_id"] == message.chat.id and message.from_user.id in (g["x_id"], g["o_id"]):
            await message.reply("❌ Вы уже играете в крестики-нолики! Напишите `/стоп` для отмены.")
            return

    await update_balance(user['tg_id'], -bet)
    ttt_id = secrets.token_hex(4)
    game = {
        "chat_id": message.chat.id,
        "x_id": user['tg_id'],
        "x_name": display_name_of(user['username']),
        "o_id": 0,
        "o_name": "🤖 Бот",
        "turn": user['tg_id'],
        "board": [""] * 9,
        "bet": bet,
        "vs_bot": True,
        "msg": None
    }
    active_ttt_games[ttt_id] = game
    await _save_game("ttt_active", ttt_id, message.chat.id, user['tg_id'], bet)

    text = (
        f"❌⭕ *КРЕСТИКИ-НОЛИКИ — ПРОТИВ БОТА*\n\n"
        f"👤 Вы: {game['x_name']} (❌)\n"
        f"🤖 Соперник: Бот (⭕)\n"
        f"💰 Ставка: *`{bet:,.2f}`* GHRAM | Выплата *x{EVEN_PAYOUT}*\n\n"
        f"Ваш ход!"
    )
    sent = await message.reply(text, parse_mode="Markdown", reply_markup=build_ttt_keyboard(ttt_id, game["board"]))
    game["msg"] = sent

async def start_ttt_pvp(message: types.Message, parts: list):
    sender = await get_or_create_user(message.from_user.id, message.from_user.username)
    target_user = None
    bet = None

    if message.reply_to_message:
        if len(parts) >= 2:
            bet = parse_amount(parts[1], sender['balance'])
            target_id = message.reply_to_message.from_user.id
            target_user = await get_or_create_user(target_id, message.reply_to_message.from_user.username)
    elif len(parts) >= 3:
        target_user = await get_user_by_identifier(parts[1])
        bet = parse_amount(parts[2], sender['balance'])
        if not target_user:
            await message.reply("❌ Пользователь не найден в базе данных бота!")
            return
    else:
        await message.reply(
            "❌⭕ *КРЕСТИКИ-НОЛИКИ*\n"
            "Использование:\n"
            "• `ттт [@username/ID] [сумма]` — игра с игроком\n"
            "• `ттт бот [сумма]` — игра против бота\n"
            "• Или ответьте на сообщение: `ттт [сумма]`",
            parse_mode="Markdown"
        )
        return

    if not bet or bet <= 0:
        await message.reply("❌ Ставка должна быть больше 0.")
        return
    if target_user['tg_id'] == sender['tg_id']:
        await message.reply("❌ Нельзя играть с самим собой!")
        return
    if not check_balance(sender['tg_id'], sender['balance'], bet):
        await message.reply(f"❌ У вас недостаточно монет (нужно {bet:,.2f}).")
        return
    if not check_balance(target_user['tg_id'], target_user['balance'], bet):
        await message.reply(f"❌ У соперника недостаточно монет (нужно {bet:,.2f}).")
        return

    for g in list(pending_ttt.values()) + list(active_ttt_games.values()):
        ids = {g.get('x_id'), g.get('o_id'), g.get('challenger_id'), g.get('target_id')}
        if sender['tg_id'] in ids or target_user['tg_id'] in ids:
            await message.reply("❌ Один из участников уже играет в крестики-нолики!")
            return

    await update_balance(sender['tg_id'], -bet)
    ttt_id = secrets.token_hex(4)

    target_mention = f"@{target_user['username']}" if target_user['username'] and target_user['username'] != "Неизвестно" else f"ID {target_user['tg_id']}"
    sender_mention = f"@{sender['username']}" if sender['username'] and sender['username'] != "Неизвестно" else f"ID {sender['tg_id']}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Согласиться ✅", callback_data=f"ttt_acc:{ttt_id}"),
        InlineKeyboardButton(text="Отказаться ⛔", callback_data=f"ttt_dec:{ttt_id}")
    ]])

    sent_msg = await message.answer(
        f"❌⭕ {target_mention}, вас вызывают на дуэль в крестики-нолики!\n"
        f"👤 Инициатор: {sender_mention}\n"
        f"💰 Ставка: *{bet:,.2f}* GHRAM (банк {bet * 2:,.2f})",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await _save_game("ttt_pending", ttt_id, message.chat.id, sender['tg_id'], bet)

    async def invite_timeout():
        await asyncio.sleep(180)
        if ttt_id in pending_ttt:
            t = pending_ttt.pop(ttt_id, None)
            if t:
                await update_balance(t['challenger_id'], t['bet'])
                await _remove_game("ttt_pending", ttt_id)
                try:
                    await sent_msg.edit_text("⏳ Время ожидания истекло (3 мин). Игра отменена, ставка возвращена.")
                except Exception:
                    pass

    timer_task = asyncio.create_task(invite_timeout())
    pending_ttt[ttt_id] = {
        "chat_id": message.chat.id,
        "challenger_id": sender['tg_id'],
        "challenger_name": sender_mention,
        "target_id": target_user['tg_id'],
        "target_name": target_mention,
        "bet": bet,
        "msg": sent_msg,
        "timer_task": timer_task
    }

@dp.callback_query(F.data.startswith("ttt_acc:"))
async def callback_ttt_accept(callback: types.CallbackQuery):
    ttt_id = callback.data.split(":")[1]
    t = pending_ttt.get(ttt_id)
    if not t:
        await callback.answer("Эта игра больше неактивна!", show_alert=True)
        return
    if callback.from_user.id != t["target_id"]:
        await callback.answer("❌ Принять вызов может только вызванный игрок!", show_alert=True)
        return
    target_user = await get_or_create_user(t["target_id"], callback.from_user.username)
    if not check_balance(target_user['tg_id'], target_user['balance'], t['bet']):
        await callback.answer("❌ У вас недостаточно монет!", show_alert=True)
        return

    t["timer_task"].cancel()
    del pending_ttt[ttt_id]
    await _remove_game("ttt_pending", ttt_id)
    await update_balance(target_user['tg_id'], -t['bet'])

    game = {
        "chat_id": callback.message.chat.id,
        "x_id": t["challenger_id"],
        "x_name": t["challenger_name"],
        "o_id": t["target_id"],
        "o_name": t["target_name"],
        "turn": t["challenger_id"],
        "board": [""] * 9,
        "bet": t["bet"],
        "vs_bot": False,
        "msg": callback.message
    }
    active_ttt_games[ttt_id] = game
    await _save_game("ttt_active", ttt_id, callback.message.chat.id, t["challenger_id"], t['bet'])
    await _save_game("ttt_active", ttt_id, callback.message.chat.id, t["target_id"], t['bet'])

    text = (
        f"❌⭕ *КРЕСТИКИ-НОЛИКИ — ДУЭЛЬ*\n\n"
        f"👤 {t['challenger_name']} (❌) — против — {t['target_name']} (⭕)\n"
        f"💰 Банк: *{t['bet'] * 2:,.2f}* GHRAM\n\n"
        f"{t['challenger_name']}, ваш ход!"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_ttt_keyboard(ttt_id, game["board"]))
    await callback.answer("Игра началась!")

@dp.callback_query(F.data.startswith("ttt_dec:"))
async def callback_ttt_decline(callback: types.CallbackQuery):
    ttt_id = callback.data.split(":")[1]
    t = pending_ttt.get(ttt_id)
    if not t:
        await callback.answer("Эта игра больше неактивна!", show_alert=True)
        return
    if callback.from_user.id != t["target_id"]:
        await callback.answer("❌ Отклонить вызов может только вызванный игрок!", show_alert=True)
        return
    t["timer_task"].cancel()
    del pending_ttt[ttt_id]
    await _remove_game("ttt_pending", ttt_id)
    await update_balance(t["challenger_id"], t["bet"])
    await callback.message.edit_text(f"⛔ {t['target_name']} отклонил(а) игру. Ставка возвращена.")
    await callback.answer("Игра отклонена")

async def finish_ttt(ttt_id: str, game: dict, result: str, msg):
    bet = game["bet"]
    if result == "draw":
        await update_balance(game["x_id"], bet)
        await add_history(game["x_id"], "Крестики-нолики (Ничья)", 0)
        if not game["vs_bot"]:
            await update_balance(game["o_id"], bet)
            await add_history(game["o_id"], "Крестики-нолики (Ничья)", 0)
        text = "🤝 *НИЧЬЯ!* Ставки возвращены игрокам."
    elif game["vs_bot"]:
        if result == "❌":
            win = round(bet * EVEN_PAYOUT, 2)
            await update_balance(game["x_id"], win)
            await add_history(game["x_id"], "Крестики-нолики (Победа)", win - bet)
            text = f"🏆 *ВЫ ПОБЕДИЛИ БОТА!* Выигрыш: *{win:,.2f}* GHRAM"
        else:
            await add_history(game["x_id"], "Крестики-нолики (Поражение)", -bet)
            text = f"🤖 *БОТ ПОБЕДИЛ!* Ставка *`{bet:,.2f}`* сгорела."
    else:
        winner_id = game["x_id"] if result == "❌" else game["o_id"]
        loser_id = game["o_id"] if result == "❌" else game["x_id"]
        winner_name = game["x_name"] if result == "❌" else game["o_name"]
        win = round(bet * 2 * (1 - PVP_RAKE), 2)
        await update_balance(winner_id, win)
        await add_history(winner_id, "Крестики-нолики (Победа)", win - bet)
        await add_history(loser_id, "Крестики-нолики (Поражение)", -bet)
        text = (
            f"🏆 *ПОБЕЖДАЕТ {winner_name}!*\n"
            f"💰 Выигрыш: *{win:,.2f}* GHRAM (банк {bet * 2:,.2f} − комиссия бота {PVP_RAKE*100:.1f}%)"
        )

    active_ttt_games.pop(ttt_id, None)
    await _remove_game("ttt_active", ttt_id)
    kb = build_ttt_keyboard(ttt_id, game["board"], game_over=True)
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        pass

async def ttt_bot_turn(ttt_id: str):
    await asyncio.sleep(0.7)
    game = active_ttt_games.get(ttt_id)
    if not game or not game["vs_bot"]:
        return
    idx = ttt_bot_move(game["board"], "⭕")
    game["board"][idx] = "⭕"
    winner = ttt_check(game["board"])
    if winner:
        await finish_ttt(ttt_id, game, winner, game["msg"])
        return
    game["turn"] = game["x_id"]
    text = (
        f"❌⭕ *КРЕСТИКИ-НОЛИКИ — ПРОТИВ БОТА*\n\n"
        f"👤 Вы: {game['x_name']} (❌)\n"
        f"🤖 Соперник: Бот (⭕)\n"
        f"💰 Ставка: *`{game['bet']:,.2f}`* GHRAM\n\n"
        f"🤖 Бот сходил. Ваш ход!"
    )
    try:
        await game["msg"].edit_text(text, parse_mode="Markdown", reply_markup=build_ttt_keyboard(ttt_id, game["board"]))
    except Exception:
        pass

@dp.callback_query(F.data.startswith("ttt_c:"))
async def callback_ttt_move(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    ttt_id = parts[1]
    idx = int(parts[2])
    game = active_ttt_games.get(ttt_id)
    if not game:
        await callback.answer("Эта игра уже завершена!", show_alert=True)
        return
    uid = callback.from_user.id
    if uid not in (game["x_id"], game["o_id"]):
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    if uid != game["turn"]:
        await callback.answer("❌ Сейчас не ваш ход!", show_alert=True)
        return
    if game["board"][idx]:
        await callback.answer("Эта клетка уже занята!")
        return

    sym = "❌" if uid == game["x_id"] else "⭕"
    game["board"][idx] = sym
    winner = ttt_check(game["board"])
    if winner:
        await finish_ttt(ttt_id, game, winner, callback.message)
        await callback.answer("Игра окончена!")
        return

    game["turn"] = game["o_id"] if uid == game["x_id"] else game["x_id"]
    next_name = game["o_name"] if game["turn"] == game["o_id"] else game["x_name"]
    next_sym = "⭕" if game["turn"] == game["o_id"] else "❌"

    if game["vs_bot"]:
        header = (
            f"❌⭕ *КРЕСТИКИ-НОЛИКИ — ПРОТИВ БОТА*\n\n"
            f"👤 Вы: {game['x_name']} (❌)\n"
            f"🤖 Соперник: Бот (⭕)\n"
            f"💰 Ставка: *`{game['bet']:,.2f}`* GHRAM\n\n"
            f"🤖 Бот думает..."
        )
    else:
        header = (
            f"❌⭕ *КРЕСТИКИ-НОЛИКИ — ДУЭЛЬ*\n\n"
            f"👤 {game['x_name']} (❌) — против — {game['o_name']} (⭕)\n"
            f"💰 Банк: *{game['bet'] * 2:,.2f}* GHRAM\n\n"
            f"Ход: {next_name} ({next_sym})"
        )
    try:
        await callback.message.edit_text(header, parse_mode="Markdown", reply_markup=build_ttt_keyboard(ttt_id, game["board"]))
    except Exception:
        pass
    await callback.answer(f"{sym} Ход сделан!")

    if game["vs_bot"] and game["turn"] == game["o_id"]:
        asyncio.create_task(ttt_bot_turn(ttt_id))

@dp.callback_query(F.data.startswith("ttt_dis:"))
async def callback_ttt_disabled(callback: types.CallbackQuery):
    await callback.answer("Эта игра уже завершена!")

# ==================== 🎰 СЛОТЫ (RTP 91%) ====================
SLOT_WEIGHTS = {"🍒": 5, "🍋": 4, "🔔": 3, "⭐": 2, "💎": 1, "7️⃣": 1}

# (комбо, множитель, вероятность) — суммарный RTP = 91%
SLOT_PAYTABLE = [
    (("7️⃣", "7️⃣", "7️⃣"), 30.0, 0.004),
    (("💎", "💎", "💎"), 15.0, 0.008),
    (("⭐", "⭐", "⭐"), 10.0, 0.015),
    (("🔔", "🔔", "🔔"), 6.0, 0.025),
    (("🍋", "🍋", "🍋"), 4.0, 0.045),
    (("🍒", "🍒", "🍒"), 2.0, 0.07),
]
SLOT_CHERRY2_PAYOUT = 0.5
SLOT_CHERRY2_CHANCE = 0.10

def slots_spin_result() -> tuple:
    sys_rand = secrets.SystemRandom()
    r = sys_rand.random()
    acc = 0.0
    for combo, payout, chance in SLOT_PAYTABLE:
        acc += chance
        if r < acc:
            return list(combo), payout
    acc += SLOT_CHERRY2_CHANCE
    if r < acc:
        reels = ["🍒", "🍒"]
        others = [s for s in SLOT_WEIGHTS if s != "🍒"]
        reels.append(sys_rand.choice(others))
        sys_rand.shuffle(reels)
        return reels, SLOT_CHERRY2_PAYOUT
    # Проигрышная комбинация (без случайных выигрышей)
    symbols = list(SLOT_WEIGHTS.keys())
    weights = [SLOT_WEIGHTS[s] for s in symbols]
    while True:
        reels = sys_rand.choices(symbols, weights=weights, k=3)
        if reels[0] == reels[1] == reels[2]:
            continue
        if reels.count("🍒") == 2:
            continue
        return reels, 0.0

def slots_paytable_text() -> str:
    return (
        "📋 Таблица выплат:\n"
        "7️⃣7️⃣7️⃣ — x30 | 💎💎💎 — x15 | ⭐⭐⭐ — x10\n"
        "🔔🔔🔔 — x6 | 🍋🍋🍋 — x4 | 🍒🍒🍒 — x2 | 🍒🍒 — x0.5\n"
        f"📊 RTP: {SLOTS_RTP_NOTE}"
    )

@dp.message(F.text.lower().startswith(("слоты", "/slots", "слот")))
async def game_slots(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply(
            f"🎰 *СЛОТ-МАШИНА «GHRAM JACKPOT»*\n"
            f"Использование: `слоты [ставка]`\n\n{slots_paytable_text()}",
            parse_mode="Markdown"
        )
        return
    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return
    game_key = (message.chat.id, user['tg_id'])
    if game_key in active_slots_spins:
        await message.reply("❌ Дождитесь окончания текущего вращения!")
        return

    await update_balance(user['tg_id'], -bet)
    text = (
        f"🎰 *СЛОТ-МАШИНА «GHRAM JACKPOT»*\n"
        f"👤 Игрок: {display_name_of(user['username'])}\n"
        f"💰 Ставка: *`{bet:,.2f}`* GHRAM\n\n"
        f"[ ❓ | ❓ | ❓ ]"
    )
    msg = await message.reply(text, parse_mode="Markdown")
    asyncio.create_task(run_slots_spin(msg, message.chat.id, user['tg_id'], bet))

async def run_slots_spin(msg: types.Message, chat_id: int, user_id: int, bet: float):
    game_key = (chat_id, user_id)
    active_slots_spins.add(game_key)
    sys_rand = secrets.SystemRandom()
    symbols = list(SLOT_WEIGHTS.keys())
    weights = [SLOT_WEIGHTS[s] for s in symbols]
    try:
        for _ in range(4):
            frame = " | ".join(sys_rand.choices(symbols, weights=weights, k=3))
            try:
                await msg.edit_text(
                    f"🎰 *СЛОТ-МАШИНА «GHRAM JACKPOT»*\n"
                    f"💰 Ставка: *`{bet:,.2f}`* GHRAM\n\n"
                    f"[ {frame} ]",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            await asyncio.sleep(0.5)

        reels, payout = slots_spin_result()
        line = " | ".join(reels)
        if payout > 0:
            win = round(bet * payout, 2)
            await update_balance(user_id, win)
            await add_history(user_id, f"Слоты (x{payout:g})", win - bet)
            if payout >= 15:
                result = f"💥 *ДЖЕКПОТ x{payout:g}!* Выигрыш: *{win:,.2f}* GHRAM"
            else:
                result = f"🎉 *ВЫИГРЫШ x{payout:g}!* +*{win:,.2f}* GHRAM"
        else:
            await add_history(user_id, "Слоты (Проигрыш)", -bet)
            result = f"😔 Мимо... Ставка *`{bet:,.2f}`* GHRAM сгорела"

        text = (
            f"🎰 *СЛОТ-МАШИНА «GHRAM JACKPOT»*\n"
            f"💰 Ставка: *`{bet:,.2f}`* GHRAM\n\n"
            f"[ {line} ]\n\n"
            f"{result}\n\n"
            f"📊 RTP: {SLOTS_RTP_NOTE}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🔁 КРУТИТЬ ЕЩЁ ({bet:,.0f})", callback_data=f"slots_spin:{user_id}:{bet}")
        ]])
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logging.error(f"Error in slots spin: {e}")
    finally:
        active_slots_spins.discard(game_key)

@dp.callback_query(F.data.startswith("slots_spin:"))
async def callback_slots_spin(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    owner_id = int(parts[1])
    bet = float(parts[2])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return
    user = await get_or_create_user(owner_id, callback.from_user.username)
    if not check_balance(user['tg_id'], user['balance'], bet):
        await callback.answer("❌ Недостаточно средств для повторного спина!", show_alert=True)
        return
    game_key = (callback.message.chat.id, owner_id)
    if game_key in active_slots_spins:
        await callback.answer("❌ Дождитесь окончания вращения!", show_alert=True)
        return
    await update_balance(user['tg_id'], -bet)
    await callback.answer("🎰 Крутим!")
    asyncio.create_task(run_slots_spin(callback.message, callback.message.chat.id, owner_id, bet))

# ----------------------------------------------------
# 14. КЛАНЫ ⚔️🏰
# ----------------------------------------------------
def clan_upgrade_cost(level: int) -> float:
    return CLAN_CREATE_COST * level

def clan_member_limit(level: int) -> int:
    return 10 + level * 5

async def get_clan_by_id(clan_id: int):
    async with bot_db.execute("SELECT * FROM clans WHERE id = ?", (clan_id,)) as cursor:
        return await cursor.fetchone()

async def get_member_row(user_id: int):
    async with bot_db.execute("SELECT * FROM clan_members WHERE user_id = ?", (user_id,)) as cursor:
        return await cursor.fetchone()

async def get_user_clan_full(user_id: int):
    m = await get_member_row(user_id)
    if not m:
        return None, None
    clan = await get_clan_by_id(m['clan_id'])
    if not clan:
        return None, None
    return clan, m

async def get_clan_members(clan_id: int):
    async with bot_db.execute(
        """SELECT cm.user_id, cm.role, u.username FROM clan_members cm
           LEFT JOIN users u ON u.tg_id = cm.user_id
           WHERE cm.clan_id = ?
           ORDER BY CASE cm.role WHEN 'owner' THEN 0 WHEN 'elder' THEN 1 ELSE 2 END, cm.joined_at""",
        (clan_id,)
    ) as cursor:
        return await cursor.fetchall()

async def get_clan_member_count(clan_id: int) -> int:
    async with bot_db.execute("SELECT COUNT(*) FROM clan_members WHERE clan_id = ?", (clan_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0]

def build_clan_menu_keyboard(clan) -> InlineKeyboardMarkup:
    up_cost = clan_upgrade_cost(clan['level'])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Участники", callback_data=f"clan_members:{clan['id']}"),
            InlineKeyboardButton(text="💰 Казна", callback_data=f"clan_treasury:{clan['id']}")
        ],
        [InlineKeyboardButton(text="📨 Пригласить игрока", callback_data=f"clan_invite_help:{clan['id']}")],
        [
            InlineKeyboardButton(text=f"⬆️ Улучшить ({up_cost:,.0f})", callback_data=f"clan_up:{clan['id']}"),
            InlineKeyboardButton(text="🚪 Покинуть", callback_data=f"clan_leave:{clan['id']}")
        ]
    ])

@dp.message(F.text.lower().in_(["клан", "/clan", "/клан"]) | F.text.lower().startswith(("клан ", "/clan ")))
async def clan_main(message: types.Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    if sub in ("создать", "create"):
        await clan_create(message, parts)
    elif sub in ("инвайт", "invite", "пригласить"):
        await clan_invite(message, parts)
    elif sub in ("покинуть", "leave", "выйти"):
        await clan_leave_cmd(message)
    elif sub in ("расформировать", "disband"):
        await clan_disband_cmd(message)
    elif sub in ("внести", "донат", "deposit"):
        await clan_donate(message, parts)
    elif sub in ("кик", "исключить", "kick"):
        await clan_kick(message, parts)
    elif sub in ("повысить", "promote"):
        await clan_set_role(message, parts, "elder")
    elif sub in ("понизить", "demote"):
        await clan_set_role(message, parts, "member")
    elif sub in ("передать", "transfer"):
        await clan_transfer_owner(message, parts)
    elif sub in ("улучшить", "upgrade"):
        await clan_upgrade_cmd(message)
    elif sub in ("топ", "top"):
        await clan_top(message)
    else:
        await clan_show_menu(message)

async def clan_show_menu(message: types.Message):
    clan, member = await get_user_clan_full(message.from_user.id)
    if not clan:
        text = (
            "⚔️ *ВЫ НЕ СОСТОИТЕ В КЛАНЕ*\n\n"
            "🏰 Создайте собственный клан и соберите команду!\n"
            f"💵 Стоимость создания: *{CLAN_CREATE_COST:,.0f}* GHRAM\n\n"
            "✍️ Команда: `клан создать [название]`\n"
            "📨 Или примите приглашение от другого клана"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚔️ СОЗДАТЬ КЛАН — 1,000,000", callback_data="clan_create_help")
        ]])
        await message.reply(text, parse_mode="Markdown", reply_markup=kb)
        return

    count = await get_clan_member_count(clan['id'])
    owner_row = await get_or_create_user(clan['owner_id'])
    owner_name = display_name_of(owner_row['username']) if owner_row else f"ID {clan['owner_id']}"
    text = (
        f"🏰 *КЛАН «{clan['name']}»*\n"
        f"🏆 Уровень: *{clan['level']}*\n"
        f"🎖 Ваша роль: {CLAN_ROLES[member['role']]}\n"
        f"👑 Главарь: {owner_name}\n"
        f"👥 Участники: *{count}/{clan_member_limit(clan['level'])}*\n"
        f"💰 Казна: *{clan['balance']:,.2f}* GHRAM\n"
        f"📈 Всего вложено: {clan['total_donated']:,.2f} GHRAM"
    )
    await message.reply(text, parse_mode="Markdown", reply_markup=build_clan_menu_keyboard(clan))

async def clan_create(message: types.Message, parts: list):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    clan, _ = await get_user_clan_full(user['tg_id'])
    if clan:
        await message.reply(f"❌ Вы уже состоите в клане «{clan['name']}»! Сначала покиньте его.")
        return
    if len(parts) < 3:
        await message.reply("❌ Укажите название: `клан создать [название]`\nПример: `клан создать Ночные Волки`", parse_mode="Markdown")
        return
    name = " ".join(parts[2:]).strip()
    if len(name) < CLAN_NAME_MIN or len(name) > CLAN_NAME_MAX:
        await message.reply(f"❌ Название должно быть от {CLAN_NAME_MIN} до {CLAN_NAME_MAX} символов!")
        return
    if not check_balance(user['tg_id'], user['balance'], CLAN_CREATE_COST):
        await message.reply(f"❌ Недостаточно средств! Создание клана стоит *{CLAN_CREATE_COST:,.0f}* GHRAM.", parse_mode="Markdown")
        return
    async with bot_db.execute("SELECT id FROM clans WHERE LOWER(name) = LOWER(?)", (name,)) as cursor:
        if await cursor.fetchone():
            await message.reply("❌ Клан с таким названием уже существует!")
            return

    await update_balance(user['tg_id'], -CLAN_CREATE_COST)
    now = int(time.time())
    cursor = await bot_db.execute(
        "INSERT INTO clans (name, owner_id, balance, total_donated, level, created_at) VALUES (?, ?, 0, 0, 1, ?)",
        (name, user['tg_id'], now)
    )
    clan_id = cursor.lastrowid
    await bot_db.execute(
        "INSERT INTO clan_members (user_id, clan_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
        (user['tg_id'], clan_id, now)
    )
    await bot_db.commit()
    await add_history(user['tg_id'], f"Создание клана «{name}»", -CLAN_CREATE_COST)

    text = (
        f"🎉 *КЛАН «{name}» ОСНОВАН!*\n\n"
        f"👑 Главарь: {display_name_of(user['username'])}\n"
        f"🏆 Уровень: 1 | 👥 Лимит: {clan_member_limit(1)} участников\n"
        f"💰 Казна: 0 GHRAM\n\n"
        f"📨 Приглашайте бойцов: `клан инвайт [@username]`\n"
        f"💰 Пополняйте казну: `клан внести [сумма]`\n"
        f"⬆️ Улучшайте клан: `клан улучшить`"
    )
    await message.reply(text, parse_mode="Markdown")

async def clan_invite(message: types.Message, parts: list):
    inviter = await get_or_create_user(message.from_user.id, message.from_user.username)
    clan, member = await get_user_clan_full(inviter['tg_id'])
    if not clan:
        await message.reply("❌ Вы не состоите в клане!")
        return
    if member['role'] not in ("owner", "elder"):
        await message.reply("❌ Приглашать могут только 👑 главарь и ⭐ заместители!")
        return

    target_user = None
    if message.reply_to_message:
        target_user = await get_or_create_user(message.reply_to_message.from_user.id, message.reply_to_message.from_user.username)
    elif len(parts) >= 3:
        target_user = await get_user_by_identifier(parts[2])
        if not target_user:
            await message.reply("❌ Пользователь не найден в базе данных бота!")
            return
    else:
        await message.reply("❌ Использование: `клан инвайт [@username/ID]` или ответьте на сообщение игрока.", parse_mode="Markdown")
        return

    if target_user['tg_id'] == inviter['tg_id']:
        await message.reply("❌ Нельзя пригласить самого себя!")
        return
    t_clan, _ = await get_user_clan_full(target_user['tg_id'])
    if t_clan:
        await message.reply(f"❌ Этот игрок уже состоит в клане «{t_clan['name']}»!")
        return
    count = await get_clan_member_count(clan['id'])
    limit = clan_member_limit(clan['level'])
    if count >= limit:
        await message.reply(f"❌ Клан заполнен ({count}/{limit})! Улучшите клан, чтобы расширить лимит.")
        return

    await bot_db.execute("DELETE FROM clan_invites WHERE clan_id = ? AND target_id = ?", (clan['id'], target_user['tg_id']))
    invite_id = secrets.token_hex(6)
    now = int(time.time())
    await bot_db.execute(
        "INSERT INTO clan_invites (invite_id, clan_id, inviter_id, target_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (invite_id, clan['id'], inviter['tg_id'], target_user['tg_id'], now)
    )
    await bot_db.commit()

    target_name = display_name_of(target_user['username'])
    inviter_name = display_name_of(inviter['username'])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"clan_acc:{invite_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"clan_dec:{invite_id}")
    ]])
    await message.answer(
        f"📨 {target_name}, вас приглашают в клан *⚔️ «{clan['name']}»*!\n"
        f"👤 Приглашает: {inviter_name}\n"
        f"⏳ Приглашение действует 24 часа.",
        parse_mode="Markdown",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("clan_acc:"))
async def callback_clan_accept(callback: types.CallbackQuery):
    invite_id = callback.data.split(":")[1]
    async with bot_db.execute("SELECT * FROM clan_invites WHERE invite_id = ?", (invite_id,)) as cursor:
        inv = await cursor.fetchone()
    if not inv:
        await callback.answer("Это приглашение больше недействительно!", show_alert=True)
        return
    if callback.from_user.id != inv['target_id']:
        await callback.answer("❌ Это приглашение адресовано другому игроку!", show_alert=True)
        return
    if int(time.time()) - inv['created_at'] > CLAN_INVITE_TTL:
        await bot_db.execute("DELETE FROM clan_invites WHERE invite_id = ?", (invite_id,))
        await bot_db.commit()
        await callback.answer("⏳ Срок действия приглашения истёк!", show_alert=True)
        return
    clan = await get_clan_by_id(inv['clan_id'])
    if not clan:
        await bot_db.execute("DELETE FROM clan_invites WHERE invite_id = ?", (invite_id,))
        await bot_db.commit()
        await callback.answer("Клан расформирован!", show_alert=True)
        return
    existing = await get_member_row(callback.from_user.id)
    if existing:
        await callback.answer("❌ Вы уже состоите в клане!", show_alert=True)
        return
    count = await get_clan_member_count(clan['id'])
    if count >= clan_member_limit(clan['level']):
        await callback.answer("❌ Клан заполнен!", show_alert=True)
        return

    await get_or_create_user(callback.from_user.id, callback.from_user.username)
    await bot_db.execute(
        "INSERT INTO clan_members (user_id, clan_id, role, joined_at) VALUES (?, ?, 'member', ?)",
        (callback.from_user.id, clan['id'], int(time.time()))
    )
    await bot_db.execute("DELETE FROM clan_invites WHERE invite_id = ?", (invite_id,))
    await bot_db.commit()
    await callback.message.edit_text(
        f"✅ {display_name_of(callback.from_user.username)} вступил(а) в клан *⚔️ «{clan['name']}»*!",
        parse_mode="Markdown"
    )
    await callback.answer(f"🎉 Добро пожаловать в «{clan['name']}»!")

@dp.callback_query(F.data.startswith("clan_dec:"))
async def callback_clan_decline(callback: types.CallbackQuery):
    invite_id = callback.data.split(":")[1]
    async with bot_db.execute("SELECT * FROM clan_invites WHERE invite_id = ?", (invite_id,)) as cursor:
        inv = await cursor.fetchone()
    if not inv:
        await callback.answer("Это приглашение больше недействительно!", show_alert=True)
        return
    if callback.from_user.id != inv['target_id']:
        await callback.answer("❌ Это приглашение адресовано другому игроку!", show_alert=True)
        return
    clan = await get_clan_by_id(inv['clan_id'])
    await bot_db.execute("DELETE FROM clan_invites WHERE invite_id = ?", (invite_id,))
    await bot_db.commit()
    clan_name = clan['name'] if clan else "???"
    await callback.message.edit_text(
        f"⛔ {display_name_of(callback.from_user.username)} отклонил(а) приглашение в клан «{clan_name}»."
    )
    await callback.answer("Приглашение отклонено")

async def clan_leave_cmd(message: types.Message):
    clan, member = await get_user_clan_full(message.from_user.id)
    if not clan:
        await message.reply("❌ Вы не состоите в клане!")
        return
    if member['role'] == 'owner':
        await message.reply("❌ Главарь не может покинуть клан! Используйте `клан расформировать` или `клан передать [@username]`.", parse_mode="Markdown")
        return
    await bot_db.execute("DELETE FROM clan_members WHERE user_id = ?", (message.from_user.id,))
    await bot_db.commit()
    await message.reply(f"🚪 Вы покинули клан «{clan['name']}».")

@dp.callback_query(F.data.startswith("clan_leave:"))
async def callback_clan_leave(callback: types.CallbackQuery):
    clan_id = int(callback.data.split(":")[1])
    clan = await get_clan_by_id(clan_id)
    if not clan:
        await callback.answer("Клан не найден!", show_alert=True)
        return
    member = await get_member_row(callback.from_user.id)
    if not member or member['clan_id'] != clan_id:
        await callback.answer("❌ Вы не состоите в этом клане!", show_alert=True)
        return
    if member['role'] == 'owner':
        await callback.answer("❌ Главарь не может покинуть клан! Расформируйте его или передайте права.", show_alert=True)
        return
    await bot_db.execute("DELETE FROM clan_members WHERE user_id = ?", (callback.from_user.id,))
    await bot_db.commit()
    await callback.message.edit_text(f"🚪 Вы покинули клан «{clan['name']}».")
    await callback.answer("Вы покинули клан")

async def clan_disband_cmd(message: types.Message):
    clan, member = await get_user_clan_full(message.from_user.id)
    if not clan:
        await message.reply("❌ Вы не состоите в клане!")
        return
    if member['role'] != 'owner':
        await message.reply("❌ Только 👑 главарь может расформировать клан!")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="☠️ РАСФОРМИРОВАТЬ", callback_data=f"clan_disb:{clan['id']}"),
        InlineKeyboardButton(text="↩️ Отмена", callback_data="clan_disb_cancel")
    ]])
    await message.reply(
        f"⚠️ Вы уверены? Клан «{clan['name']}» будет удалён навсегда.\n"
        f"💰 Казна (*{clan['balance']:,.2f}* GHRAM) вернётся главарю.",
        parse_mode="Markdown",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("clan_disb:"))
async def callback_clan_disband(callback: types.CallbackQuery):
    clan_id = int(callback.data.split(":")[1])
    clan = await get_clan_by_id(clan_id)
    if not clan:
        await callback.answer("Клан уже удалён!", show_alert=True)
        return
    if callback.from_user.id != clan['owner_id']:
        await callback.answer("❌ Только главарь может расформировать клан!", show_alert=True)
        return
    refund = clan['balance']
    if refund > 0:
        await update_balance(clan['owner_id'], refund)
        await add_history(clan['owner_id'], f"Возврат казны клана «{clan['name']}»", refund)
    await bot_db.execute("DELETE FROM clan_invites WHERE clan_id = ?", (clan_id,))
    await bot_db.execute("DELETE FROM clan_members WHERE clan_id = ?", (clan_id,))
    await bot_db.execute("DELETE FROM clans WHERE id = ?", (clan_id,))
    await bot_db.commit()
    await callback.message.edit_text(
        f"☠️ Клан «{clan['name']}» расформирован. Казна *`{refund:,.2f}`* GHRAM возвращена главарю.",
        parse_mode="Markdown"
    )
    await callback.answer("Клан расформирован")

@dp.callback_query(F.data == "clan_disb_cancel")
async def callback_clan_disband_cancel(callback: types.CallbackQuery):
    await callback.message.edit_text("↩️ Расформирование клана отменено.")
    await callback.answer("Отменено")

async def clan_donate(message: types.Message, parts: list):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    clan, _ = await get_user_clan_full(user['tg_id'])
    if not clan:
        await message.reply("❌ Вы не состоите в клане!")
        return
    if len(parts) < 3:
        await message.reply("❌ Использование: `клан внести [сумма]`", parse_mode="Markdown")
        return
    amount = parse_amount(parts[2], user['balance'])
    if not amount or amount <= 0:
        await message.reply("❌ Укажите корректную сумму!")
        return
    if not check_balance(user['tg_id'], user['balance'], amount):
        await message.reply("❌ Недостаточно средств на балансе!")
        return
    await update_balance(user['tg_id'], -amount)
    await bot_db.execute(
        "UPDATE clans SET balance = balance + ?, total_donated = total_donated + ? WHERE id = ?",
        (amount, amount, clan['id'])
    )
    await bot_db.commit()
    await add_history(user['tg_id'], f"Взнос в клан «{clan['name']}»", -amount)
    await message.reply(
        f"💰 Вы внесли *{amount:,.2f}* GHRAM в казну клана «{clan['name']}»! Спасибо за службу ⚔️",
        parse_mode="Markdown"
    )

async def resolve_clan_target(message: types.Message, parts: list, min_parts: int):
    if message.reply_to_message:
        return await get_or_create_user(message.reply_to_message.from_user.id, message.reply_to_message.from_user.username)
    if len(parts) >= min_parts:
        return await get_user_by_identifier(parts[2])
    return None

async def clan_kick(message: types.Message, parts: list):
    clan, member = await get_user_clan_full(message.from_user.id)
    if not clan:
        await message.reply("❌ Вы не состоите в клане!")
        return
    if member['role'] not in ("owner", "elder"):
        await message.reply("❌ Исключать могут только 👑 главарь и ⭐ заместители!")
        return
    target = await resolve_clan_target(message, parts, 3)
    if not target:
        await message.reply("❌ Использование: `клан кик [@username/ID]` или ответьте на сообщение игрока.", parse_mode="Markdown")
        return
    t_member = await get_member_row(target['tg_id'])
    if not t_member or t_member['clan_id'] != clan['id']:
        await message.reply("❌ Этот игрок не состоит в вашем клане!")
        return
    if t_member['role'] == 'owner':
        await message.reply("❌ Нельзя исключить главаря!")
        return
    if member['role'] == 'elder' and t_member['role'] == 'elder':
        await message.reply("❌ Заместитель не может исключать заместителей!")
        return
    await bot_db.execute("DELETE FROM clan_members WHERE user_id = ?", (target['tg_id'],))
    await bot_db.commit()
    await message.reply(f"🚪 {display_name_of(target['username'])} исключён(а) из клана «{clan['name']}»!")

async def clan_set_role(message: types.Message, parts: list, new_role: str):
    clan, member = await get_user_clan_full(message.from_user.id)
    if not clan:
        await message.reply("❌ Вы не состоите в клане!")
        return
    if member['role'] != 'owner':
        await message.reply("❌ Только 👑 главарь может менять роли!")
        return
    target = await resolve_clan_target(message, parts, 3)
    if not target:
        verb = "повысить" if new_role == "elder" else "понизить"
        await message.reply(f"❌ Использование: `клан {verb} [@username/ID]` или ответьте на сообщение игрока.", parse_mode="Markdown")
        return
    if target['tg_id'] == message.from_user.id:
        await message.reply("❌ Нельзя менять роль самому себе!")
        return
    t_member = await get_member_row(target['tg_id'])
    if not t_member or t_member['clan_id'] != clan['id']:
        await message.reply("❌ Этот игрок не состоит в вашем клане!")
        return
    if t_member['role'] == 'owner':
        await message.reply("❌ Роль главаря меняется только через `клан передать`.")
        return
    await bot_db.execute("UPDATE clan_members SET role = ? WHERE user_id = ?", (new_role, target['tg_id']))
    await bot_db.commit()
    await message.reply(f"✅ Роль {display_name_of(target['username'])} изменена на {CLAN_ROLES[new_role]}!")

async def clan_transfer_owner(message: types.Message, parts: list):
    clan, member = await get_user_clan_full(message.from_user.id)
    if not clan:
        await message.reply("❌ Вы не состоите в клане!")
        return
    if member['role'] != 'owner':
        await message.reply("❌ Только 👑 главарь может передать власть!")
        return
    target = await resolve_clan_target(message, parts, 3)
    if not target:
        await message.reply("❌ Использование: `клан передать [@username/ID]` или ответьте на сообщение игрока.", parse_mode="Markdown")
        return
    if target['tg_id'] == message.from_user.id:
        await message.reply("❌ Вы уже главарь!")
        return
    t_member = await get_member_row(target['tg_id'])
    if not t_member or t_member['clan_id'] != clan['id']:
        await message.reply("❌ Этот игрок не состоит в вашем клане!")
        return
    await bot_db.execute("UPDATE clan_members SET role = 'owner' WHERE user_id = ?", (target['tg_id'],))
    await bot_db.execute("UPDATE clan_members SET role = 'elder' WHERE user_id = ?", (message.from_user.id,))
    await bot_db.execute("UPDATE clans SET owner_id = ? WHERE id = ?", (target['tg_id'], clan['id']))
    await bot_db.commit()
    await message.reply(
        f"👑 Власть передана! Новый главарь клана «{clan['name']}» — {display_name_of(target['username'])}!",
        parse_mode="Markdown"
    )

async def do_clan_upgrade(user_id: int):
    clan, member = await get_user_clan_full(user_id)
    if not clan:
        return False, "❌ Вы не состоите в клане!"
    if member['role'] != 'owner':
        return False, "❌ Только 👑 главарь может улучшать клан!"
    if clan['level'] >= CLAN_MAX_LEVEL:
        return False, "🏆 Клан уже максимального уровня!"
    cost = clan_upgrade_cost(clan['level'])
    if clan['balance'] < cost:
        return False, f"❌ В казне не хватает средств! Нужно {cost:,.0f} GHRAM (казна: {clan['balance']:,.2f}). Пополните: `клан внести [сумма]`"
    await bot_db.execute("UPDATE clans SET balance = balance - ?, level = level + 1 WHERE id = ?", (cost, clan['id']))
    await bot_db.commit()
    fresh = await get_clan_by_id(clan['id'])
    return True, f"🎉 Клан «{fresh['name']}» улучшен до уровня *{fresh['level']}*!\n👥 Новый лимит участников: {clan_member_limit(fresh['level'])}"

async def clan_upgrade_cmd(message: types.Message):
    ok, text = await do_clan_upgrade(message.from_user.id)
    await message.reply(text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("clan_up:"))
async def callback_clan_upgrade(callback: types.CallbackQuery):
    clan_id = int(callback.data.split(":")[1])
    clan = await get_clan_by_id(clan_id)
    if not clan:
        await callback.answer("Клан не найден!", show_alert=True)
        return
    ok, text = await do_clan_upgrade(callback.from_user.id)
    if ok:
        await callback.message.edit_text(text, parse_mode="Markdown")
        await callback.answer("🎉 Клан улучшен!")
    else:
        await callback.answer(text, show_alert=True)

async def clan_top(message: types.Message):
    async with bot_db.execute("SELECT * FROM clans ORDER BY balance DESC, level DESC LIMIT 10") as cursor:
        rows = await cursor.fetchall()
    if not rows:
        await message.reply("🏆 В мире пока нет ни одного клана! Станьте первым — `клан создать [название]`", parse_mode="Markdown")
        return
    medals = ["🥇", "🥈", "🥉"]
    text = f"🏆 *ЛИГА КЛАНОВ — ТОП-{len(rows)}*\n"
    for idx, c in enumerate(rows, start=1):
        icon = medals[idx-1] if idx <= 3 else f"{idx}."
        count = await get_clan_member_count(c['id'])
        text += f"{icon} «{c['name']}» — ур. {c['level']} | 💰 {c['balance']:,.0f} | 👥 {count}\n"
    await message.reply(text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("clan_members:"))
async def callback_clan_members(callback: types.CallbackQuery):
    clan_id = int(callback.data.split(":")[1])
    clan = await get_clan_by_id(clan_id)
    if not clan:
        await callback.answer("Клан не найден!", show_alert=True)
        return
    members = await get_clan_members(clan_id)
    text = f"👥 *Участники клана «{clan['name']}»* ({len(members)}/{clan_member_limit(clan['level'])}):\n"
    for m in members:
        name = display_name_of(m['username']) if m['username'] else f"ID {m['user_id']}"
        text += f"{CLAN_ROLES[m['role']]} {name}\n"
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_clan_menu_keyboard(clan))
    except TelegramBadRequest:
        pass
    await callback.answer("Список участников обновлён")

@dp.callback_query(F.data.startswith("clan_treasury:"))
async def callback_clan_treasury(callback: types.CallbackQuery):
    clan_id = int(callback.data.split(":")[1])
    clan = await get_clan_by_id(clan_id)
    if not clan:
        await callback.answer("Клан не найден!", show_alert=True)
        return
    text = (
        f"💰 *Казна клана «{clan['name']}»*\n\n"
        f"💵 Баланс: *{clan['balance']:,.2f}* GHRAM\n"
        f"📈 Всего вложено: {clan['total_donated']:,.2f} GHRAM\n\n"
        f"Пополнить: `клан внести [сумма]`\n"
        f"⬆️ Улучшение клана стоит {clan_upgrade_cost(clan['level']):,.0f} GHRAM"
    )
    try:
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_clan_menu_keyboard(clan))
    except TelegramBadRequest:
        pass
    await callback.answer("Казна обновлена")

@dp.callback_query(F.data.startswith("clan_invite_help:"))
async def callback_clan_invite_help(callback: types.CallbackQuery):
    await callback.answer(
        "📨 Чтобы пригласить игрока:\n• ответьте на его сообщение: клан инвайт\n• или: клан инвайт @username",
        show_alert=True
    )

@dp.callback_query(F.data == "clan_create_help")
async def callback_clan_create_help(callback: types.CallbackQuery):
    await callback.answer(
        f"✍️ Введите команду: клан создать [название]\n💵 Стоимость: {CLAN_CREATE_COST:,.0f} GHRAM",
        show_alert=True
    )

# ----------------------------------------------------
# 12. ЗАПУСК
# ----------------------------------------------------
async def main():
    global bot_db
    logging.basicConfig(level=logging.INFO)
    print("🚀 Запуск обновленного бота GHRAM...")
    await init_db()
    print("🧹 Очистка зависших игр и возврат ставок...")
    await cleanup_all_active_games()
    asyncio.create_task(periodic_cleanup_task())
    try:
        await dp.start_polling(bot)
    finally:
        if bot_db:
            await bot_db.close()

if __name__ == "__main__":
    asyncio.run(main())
