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

# Игровые состояния для Блэкджека, Крестиков-Ноликов и Костей PvP
active_bj_games = {}
active_ttt_games = {}
pending_dice_pvp = {}
active_dice_pvp = {}

RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
BLACK_NUMBERS = {2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35}

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
            language TEXT DEFAULT 'ru',
            clan_id INTEGER DEFAULT 0
        )
    """)
    
    # Безопасная миграция
    user_columns = [
        ("bank", "REAL DEFAULT 0.0"),
        ("hourly_income", "REAL DEFAULT 150.0"),
        ("last_claim", "INTEGER DEFAULT 0"),
        ("last_bonus", "INTEGER DEFAULT 0"),
        ("language", "TEXT DEFAULT 'ru'"),
        ("clan_id", "INTEGER DEFAULT 0")
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
            last_collect INTEGER DEFAULT 0,
            durability INTEGER DEFAULT 100
        )
    """)

    farm_columns = [
        ("durability", "INTEGER DEFAULT 100")
    ]
    for col_name, col_type in farm_columns:
        try:
            await bot_db.execute(f"ALTER TABLE mining_farms ADD COLUMN {col_name} {col_type}")
        except Exception:
            pass

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS clans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            balance REAL DEFAULT 0.0,
            created_at INTEGER DEFAULT 0
        )
    """)

    await bot_db.execute("""
        CREATE TABLE IF NOT EXISTS clan_invites (
            clan_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY(clan_id, user_id)
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

    # --- ИНДЕКСЫ ДЛЯ МГНОВЕННОЙ РАБОТЫ ---
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, id DESC)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_loans_user ON loans(user_id)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_farms_user ON mining_farms(user_id)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_clans_balance ON clans(balance DESC)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_active_games_type_key ON active_games(game_type, game_key)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_active_games_user ON active_games(user_id)")
    await bot_db.execute("CREATE INDEX IF NOT EXISTS idx_active_games_created ON active_games(created_at)")

    await bot_db.commit()

async def get_or_create_user(tg_id: int, username: str | None = None):
    now = int(time.time())
    initial_balance = 10**18 if tg_id == OWNER_ID else 1000.0
    
    # INSERT OR IGNORE защищает от гонки состояний при одновременной регистрации
    await bot_db.execute(
        "INSERT OR IGNORE INTO users (tg_id, username, balance, last_claim, last_bonus, language) VALUES (?, ?, ?, ?, 0, 'ru')",
        (tg_id, username or "Неизвестно", initial_balance, now)
    )
    
    if tg_id == OWNER_ID:
        await bot_db.execute("UPDATE users SET balance = ? WHERE tg_id = ? AND balance < ?", (10**18, tg_id, 10**17))
        
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
            elif game_type == "blackjack":
                active_bj_games.pop((chat_id, user_id), None)
            elif game_type == "ttt":
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

def build_mining_keyboard(user_id: int, gpu_cost: float, lvl_cost: float, durability: int = 100):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Собрать прибыль", callback_data=f"farm_claim:{user_id}")],
        [
            InlineKeyboardButton(text=f"🛒 +1 GPU ({gpu_cost:,.0f})", callback_data=f"farm_buy_gpu:{user_id}"),
            InlineKeyboardButton(text=f"⬆️ Уровень ({lvl_cost:,.0f})", callback_data=f"farm_upgrade_lvl:{user_id}")
        ],
        [
            InlineKeyboardButton(text=f"🛠 Ремонт ({durability}%)", callback_data=f"farm_repair:{user_id}"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"farm_refresh:{user_id}")
        ]
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

def build_coinflip_keyboard(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🦅 Орёл", callback_data=f"coin_choice:heads:{user_id}"),
            InlineKeyboardButton(text="🪙 Решка", callback_data=f"coin_choice:tails:{user_id}")
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"coin_choice:cancel:{user_id}")]
    ])

def build_bj_keyboard(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Взять карту", callback_data=f"bj_act:hit:{user_id}"),
            InlineKeyboardButton(text="🛑 Хватит", callback_data=f"bj_act:stand:{user_id}")
        ],
        [InlineKeyboardButton(text="🚪 Сдаться (-50%)", callback_data=f"bj_act:fold:{user_id}")]
    ])

def build_ttt_keyboard(game_id: str, board: list, active: bool = True):
    keyboard = []
    symbols = {0: "➖", 1: "❌", 2: "⭕"}
    for row in range(3):
        line = []
        for col in range(3):
            idx = row * 3 + col
            val = board[idx]
            text = symbols.get(val, "➖")
            cb = f"ttt_move:{game_id}:{idx}" if active and val == 0 else "ttt_dis"
            line.append(InlineKeyboardButton(text=text, callback_data=cb))
        keyboard.append(line)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
            
        await message.reply("🔥 Баланс **всех игроков** был успешно аннулирован!", parse_mode="Markdown")

@dp.message(Command("annf"))
async def cmd_annf(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and message.from_user.id != OWNER_ID:
        return

    parts = message.text.split()
    if len(parts) > 1:
        target_str = parts[1]
        target_user = await get_user_by_identifier(target_str)
        if not target_user:
            await message.reply("❌ Пользователь не найден!")
            return

        await bot_db.execute("DELETE FROM mining_farms WHERE user_id = ?", (target_user['tg_id'],))
        await bot_db.commit()

        await message.reply(f"🔥 Майнинг-ферма пользователя @{target_user['username']} (ID: {target_user['tg_id']}) полностью аннулирована!")
    else:
        await bot_db.execute("DELETE FROM mining_farms")
        await bot_db.commit()

        await message.reply("🔥 Майнинг-фермы **всех пользователей** были успешно аннулированы!", parse_mode="Markdown")

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
            "👋 Welcome!\n\n"
            "GHRAM is an entertainment bot for your chat:\n\n"
            "• ⚔️ Create your own clan\n"
            "• 🏆 Participate in tournaments\n"
            "• 🎮 Mini-games\n"
            "• 🤺 Duels\n\n"
            "By starting the bot, you automatically agree to the terms of use."
        )
        menu_text = "Select the section you need in the menu below 👇"
    else:
        welcome_text = (
            "👋 Добро пожаловать!\n\n"
            "GHRAM — это развлекательный бот для вашего чата:\n\n"
            "• ⚔️ Создание собственного клана\n"
            "• 🏆 Участие в турнирах\n"
            "• 🎮 Мини-игры\n"
            "• 🤺 Дуэли\n\n"
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
        "🌐 **Выберите язык интерфейса:**\n\n"
        "*(Настройка языка применяется только в личных сообщениях с ботом. В групповых чатах всегда используется русский язык)*"
    ) if lang == "ru" else (
        "🌐 **Select interface language:**\n\n"
        "*(Language setting applies only to direct messages with the bot. In group chats, the language is always Russian)*"
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
        text_msg = "✅ **Language changed to English!**\nNow bot will answer in English in DMs."
        kb = main_reply_keyboard("en")
    else:
        alert_msg = "✅ Язык успешно изменен на Русский!"
        text_msg = "✅ **Язык успешно изменен на Русский!**"
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
        return f"Вам начислено: {int(BONUS_AMOUNT)} GHRAM\n\nСледующий бонус будет доступен через {next_time_str}"
    else:
        remaining = BONUS_COOLDOWN - time_passed
        time_str = format_time_remaining(remaining)
        return f"❌ Вы уже забирали бонус!\n\nСледующий бонус будет доступен через {time_str}"

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
        f"💰 Ваш баланс: **{bal_str}** монет", 
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
    
    clan_name = "Нет"
    if user['clan_id'] and user['clan_id'] > 0:
        async with bot_db.execute("SELECT name FROM clans WHERE id = ?", (user['clan_id'],)) as cursor:
            clan_row = await cursor.fetchone()
            if clan_row:
                clan_name = clan_row['name']

    if lang == "en":
        text = (
            f"👤 **User Profile:** @{user['username']}\n"
            f"🆔 ID: `{user['tg_id']}`\n"
            f"⚔️ Clan: `{clan_name}`\n"
            f"💰 Balance: `{bal_str}` coins\n"
            f"🏦 In Bank: `{bank_str}` coins\n"
            f"📈 Passive Income: `{user['hourly_income']:,.2f}`/hour"
        )
    else:
        text = (
            f"👤 **Профиль пользователя:** @{user['username']}\n"
            f"🆔 ID: `{user['tg_id']}`\n"
            f"⚔️ Клан: `{clan_name}`\n"
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

    text = "📜 **Последние операции:**\n\n"
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

    text = f"🏆 **Глобальный топ-{len(users)} игроков:**\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for idx, u in enumerate(users, start=1):
        icon = medals[idx-1] if idx <= 3 else f"{idx}."
        bal_str = get_balance_str(u['tg_id'], u['balance'])
        text += f"{icon} @{u['username']} — `{bal_str}` монет\n"

    await message.reply(text, parse_mode="Markdown")

# --- ПЕРЕВОД (С НАЛОГОМ НА ПЕРЕВОДЫ P2P TAX) ---
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

    # Прогрессивный налог на переводы (P2P Tax: 5%, 10%, или 15% от 500k)
    if amount < 100_000:
        tax_rate = 0.05
    elif amount < 500_000:
        tax_rate = 0.10
    else:
        tax_rate = 0.15

    tax_amount = amount * tax_rate
    final_amount = amount - tax_amount

    await update_balance(sender['tg_id'], -amount)
    await update_balance(target_user['tg_id'], final_amount)

    target_name = f"@{target_user['username']}" if target_user['username'] != "Неизвестно" else f"ID {target_user['tg_id']}"

    await add_history(sender['tg_id'], f"Перевод для {target_name}", -amount)
    await add_history(target_user['tg_id'], f"Перевод от @{sender['username']}", final_amount)

    await message.reply(
        f"✅ Вы успешно перевели `{amount:,.2f}` монет пользователю **{target_name}**!\n"
        f"🏛 Налог на перевод ({int(tax_rate * 100)}%): `{tax_amount:,.2f}` GHRAM\n"
        f"📥 Получателю зачислено: `{final_amount:,.2f}` GHRAM", 
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

    if key in active_bj_games:
        game = active_bj_games.pop(key)
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
            await message.reply("✅ Активная игра/дуэль отменена. Ставки возвращены.")
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
        f"💰 Ставка: **{bet:,.2f}** монет",
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
        f"⚔️ **Дуэль началась!**\n"
        f"💰 Ставка: **{duel['bet']:,.2f}** монет (банк {duel['bet'] * 2:,.2f})\n\n"
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
            f"💥 {shooter_name} делает выстрел и... **ПОПАДАЕТ!** 🎯\n\n"
            f"👑 Победитель: {shooter_name}\n"
            f"💰 Выигрыш: **{total_win:,.2f}** монет!"
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
            f"💨 {shooter_name} делает выстрел и... **ПРОМАХИВАЕТСЯ!**\n\n"
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
        f"🎉 {display_name} забирает выигрыш!\n\n"
        f"💰 Заработано: **{win_amount:,.2f}** GRAM"
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
        result_text = f"🃏 **ДЖОКЕР!** {display_name}, вы сорвали куш и выиграли **{win_amount:,.0f}** GRAM!"
        alert_text = f"🃏 ДЖОКЕР! Выигрыш: {win_amount:,.0f} GRAM"
    elif chosen_card == "💎":
        is_win = True
        win_amount = bet * 1.2
        await update_balance(user_id, win_amount)
        await add_history(user_id, "Джокер (Удача)", win_amount - bet)
        result_text = f"💎 {display_name}, вам выпала карта удачи! Выигрыш: **{win_amount:,.0f}** GRAM!"
        alert_text = f"💎 Удача! Выигрыш: {win_amount:,.0f} GRAM"
    else:
        await add_history(user_id, "Джокер (Поражение)", -bet)
        result_text = f"💥 {display_name}, вы не угадали! Ставка **{bet:,.0f}** GRAM сгорела."
        alert_text = "💥 Неудача!"

    reply_markup = build_joker_keyboard(owner_id, cards=game["cards"], game_over=True, is_win=is_win)
    del active_joker_games[game_key]
    await _remove_game("joker", f"{callback.message.chat.id}:{owner_id}")

    await callback.message.edit_text(
        f"{display_name}, вы начали игру джокер!\n💰 Ставка: {bet:,.0f} GRAM\n\n{result_text}",
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


# --- 🎲 КОСТИ (PvE И PvP С HOUSE EDGE) ---
@dp.message(F.text.lower().startswith(("кости", "/dice")))
async def game_dice(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()

    if len(parts) < 2:
        await message.reply("🎲 Введите: `кости [сумма]` для игры с ботом или `кости [@username] [сумма]` для PvP дуэли!")
        return

    # Проверяем, указан ли игрок для PvP
    target_user = None
    bet = None

    if message.reply_to_message:
        bet = parse_amount(parts[1], user['balance'])
        target_id = message.reply_to_message.from_user.id
        target_user = await get_or_create_user(target_id, message.reply_to_message.from_user.username)
    elif len(parts) >= 3:
        target_user = await get_user_by_identifier(parts[1])
        if target_user:
            bet = parse_amount(parts[2], user['balance'])
    
    if not target_user and not bet:
        bet = parse_amount(parts[1], user['balance'])

    if not bet or bet <= 0:
        await message.reply("❌ Неверно указана сумма ставки!")
        return

    if not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств!")
        return

    # Если PvP режим
    if target_user:
        if target_user['tg_id'] == user['tg_id']:
            await message.reply("❌ Нельзя играть в кости с самим собой!")
            return
        if not check_balance(target_user['tg_id'], target_user['balance'], bet):
            await message.reply("❌ У соперника недостаточно монет!")
            return

        await update_balance(user['tg_id'], -bet)
        game_id = secrets.token_hex(4)

        pending_dice_pvp[game_id] = {
            "chat_id": message.chat.id,
            "p1_id": user['tg_id'],
            "p1_name": f"@{user['username']}" if user['username'] else "Игрок 1",
            "p2_id": target_user['tg_id'],
            "p2_name": f"@{target_user['username']}" if target_user['username'] else "Игрок 2",
            "bet": bet
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Бросить кости 🎲", callback_data=f"dice_acc:{game_id}"),
            InlineKeyboardButton(text="Отклонить ⛔", callback_data=f"dice_dec:{game_id}")
        ]])

        target_mention = f"@{target_user['username']}" if target_user['username'] else f"ID {target_user['tg_id']}"
        await message.reply(
            f"🎲 **PvP Кости!**\n"
            f"{target_mention}, вам бросили вызов в кости!\n"
            f"💰 Ставка: **{bet:,.2f}** GHRAM",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return

    # PvE Режим против Бота (House edge 1.95x payout)
    await update_balance(user['tg_id'], -bet)

    p_roll1, p_roll2 = random.randint(1, 6), random.randint(1, 6)
    b_roll1, b_roll2 = random.randint(1, 6), random.randint(1, 6)

    # Легкое смещение вероятности в сторону бота (House Edge)
    if random.random() < 0.05:
        b_roll1 = min(6, b_roll1 + 1)

    p_total = p_roll1 + p_roll2
    b_total = b_roll1 + b_roll2

    display_name = f"@{user['username']}" if user['username'] else "Игрок"

    if p_total > b_total:
        win = bet * 1.95  # House Edge выплата
        await update_balance(user['tg_id'], win)
        await add_history(user['tg_id'], "Кости PvE (Победа)", win - bet)
        res = f"🎉 **Победа! Вы выиграли `{win:,.2f}` GHRAM!**"
    elif p_total < b_total:
        await add_history(user['tg_id'], "Кости PvE (Поражение)", -bet)
        res = f"💥 **Поражение! Бот забрал ставку.**"
    else:
        await update_balance(user['tg_id'], bet)
        res = f"🤝 **Ничья! Ставка `{bet:,.2f}` GHRAM возвращена.**"

    text = (
        f"🎲 **Игра в Кости (PvE)**\n\n"
        f"👤 {display_name}: 🎲 [{p_roll1}] + [{p_roll2}] = **{p_total}**\n"
        f"🤖 Бот: 🎲 [{b_roll1}] + [{b_roll2}] = **{b_total}**\n\n"
        f"{res}"
    )
    await message.reply(text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("dice_acc:"))
async def cb_dice_pvp_accept(callback: types.CallbackQuery):
    game_id = callback.data.split(":")[1]
    game = pending_dice_pvp.pop(game_id, None)

    if not game:
        await callback.answer("Эта игра в кости больше неактивна!", show_alert=True)
        return

    if callback.from_user.id != game["p2_id"]:
        await callback.answer("❌ Принять вызов может только приглашенный игрок!", show_alert=True)
        return

    p2_user = await get_or_create_user(game["p2_id"], callback.from_user.username)
    if not check_balance(p2_user['tg_id'], p2_user['balance'], game['bet']):
        await update_balance(game["p1_id"], game["bet"])
        await callback.answer("❌ Недостаточно средств для участия!", show_alert=True)
        return

    await update_balance(game["p2_id"], -game['bet'])

    p1_r1, p1_r2 = random.randint(1, 6), random.randint(1, 6)
    p2_r1, p2_r2 = random.randint(1, 6), random.randint(1, 6)

    p1_tot = p1_r1 + p1_r2
    p2_tot = p2_r1 + p2_r2

    bet = game["bet"]
    total_bank = bet * 2

    if p1_tot > p2_tot:
        await update_balance(game["p1_id"], total_bank)
        res = f"👑 Победитель: {game['p1_name']} (+{total_bank:,.2f} GHRAM)!"
    elif p2_tot > p1_tot:
        await update_balance(game["p2_id"], total_bank)
        res = f"👑 Победитель: {game['p2_name']} (+{total_bank:,.2f} GHRAM)!"
    else:
        await update_balance(game["p1_id"], bet)
        await update_balance(game["p2_id"], bet)
        res = f"🤝 Ничья! Ставки возвращены всем игрокам."

    text = (
        f"🎲 **PvP Дуэль в Кости Завершена!**\n\n"
        f"👤 {game['p1_name']}: 🎲 [{p1_r1}] + [{p1_r2}] = **{p1_tot}**\n"
        f"👤 {game['p2_name']}: 🎲 [{p2_r1}] + [{p2_r2}] = **{p2_tot}**\n\n"
        f"{res}"
    )
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer("🎲 Кости брошены!")

@dp.callback_query(F.data.startswith("dice_dec:"))
async def cb_dice_pvp_decline(callback: types.CallbackQuery):
    game_id = callback.data.split(":")[1]
    game = pending_dice_pvp.pop(game_id, None)

    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if callback.from_user.id != game["p2_id"]:
        await callback.answer("❌ Отклонить может только вызванный игрок!", show_alert=True)
        return

    await update_balance(game["p1_id"], game["bet"])
    await callback.message.edit_text(f"⛔ {game['p2_name']} отклонил игра в кости. Ставка возвращена.")
    await callback.answer("Вызов отклонен")


# --- 🪙 МОНЕТКА (COINFLIP С HOUSE EDGE) ---
@dp.message(F.text.lower().startswith(("монетка", "монета", "/coin")))
async def game_coinflip(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()

    if len(parts) < 2:
        await message.reply("🪙 Введите ставку: `монетка [сумма]`")
        return

    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств или неверная сумма!")
        return

    display_name = f"@{user['username']}" if user['username'] else "Игрок"
    kb = build_coinflip_keyboard(user['tg_id'])

    # Сохраняем активный выбор
    active_mines_games[(message.chat.id, user['tg_id'], "coin")] = {"bet": bet}

    await message.reply(
        f"🪙 {display_name}, сделайте выбор:\n"
        f"💰 Ставка: **{bet:,.2f}** GHRAM\n"
        f"📈 Выплата: **1.95x**",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("coin_choice:"))
async def cb_coin_choice(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    choice = parts[1]
    owner_id = int(parts[2])

    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return

    key = (callback.message.chat.id, owner_id, "coin")
    game_data = active_mines_games.pop(key, None)

    if not game_data:
        await callback.answer("Игра отменена или не найдена!", show_alert=True)
        return

    if choice == "cancel":
        await callback.message.edit_text("❌ Игра в монетку отменена.")
        await callback.answer("Отменено")
        return

    bet = game_data["bet"]
    user = await get_or_create_user(owner_id, callback.from_user.username)

    if not check_balance(owner_id, user['balance'], bet):
        await callback.answer("❌ Недостаточно монет на балансе!", show_alert=True)
        return

    await update_balance(owner_id, -bet)

    # House edge: вероятность выигрыша 48%
    is_win = random.random() < 0.48
    result_coin = choice if is_win else ("tails" if choice == "heads" else "heads")
    coin_icon = "🦅 Орёл" if result_coin == "heads" else "🪙 Решка"

    if is_win:
        win_amt = bet * 1.95
        await update_balance(owner_id, win_amt)
        await add_history(owner_id, "Монетка (Победа)", win_amt - bet)
        res_text = f"🎉 **ВЫ УГАДАЛИ!** Выпал: {coin_icon}\n🏆 Выиграно: `{win_amt:,.2f}` GHRAM!"
    else:
        await add_history(owner_id, "Монетка (Поражение)", -bet)
        res_text = f"💥 **УВЫ!** Выпал: {coin_icon}\n💰 Потеряно: `{bet:,.2f}` GHRAM"

    await callback.message.edit_text(
        f"🪙 **Результат броска монетки:**\n\n{res_text}",
        parse_mode="Markdown"
    )
    await callback.answer()


# --- 🃏 БЛЭКДЖЕК (BLACKJACK С HOUSE EDGE) ---
def get_card_value(cards):
    total = 0
    aces = 0
    for card in cards:
        val = card[:-1]
        if val in ["J", "Q", "K"]:
            total += 10
        elif val == "A":
            aces += 1
            total += 11
        else:
            total += int(val)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

@dp.message(F.text.lower().startswith(("блэкджек", "бд", "/bj")))
async def game_blackjack(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()

    if len(parts) < 2:
        await message.reply("🃏 Введите ставку: `блэкджек [сумма]`")
        return

    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств!")
        return

    game_key = (message.chat.id, user['tg_id'])
    if game_key in active_bj_games:
        await message.reply("❌ У вас уже есть активная игра в Блэкджек!")
        return

    await update_balance(user['tg_id'], -bet)

    suits = ["♠️", "♥️", "♦️", "♣️"]
    ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    deck = [f"{r}{s}" for r in ranks for s in suits]
    random.shuffle(deck)

    player_hand = [deck.pop(), deck.pop()]
    dealer_hand = [deck.pop(), deck.pop()]

    active_bj_games[game_key] = {
        "bet": bet,
        "deck": deck,
        "player": player_hand,
        "dealer": dealer_hand,
        "user_id": user['tg_id']
    }

    await _save_game("blackjack", f"{message.chat.id}:{user['tg_id']}", message.chat.id, user['tg_id'], bet)

    p_score = get_card_value(player_hand)
    
    text = (
        f"🃏 **Блэкджек**\n\n"
        f"👤 Ваши карты: {' '.join(player_hand)} (**{p_score}**)\n"
        f"🤖 Дилер: {dealer_hand[0]} 🎴 (**?**)\n\n"
        f"💰 Ставка: `{bet:,.2f}` GHRAM"
    )

    kb = build_bj_keyboard(user['tg_id'])
    await message.reply(text, reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("bj_act:"))
async def cb_bj_action(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    action = parts[1]
    owner_id = int(parts[2])

    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return

    game_key = (callback.message.chat.id, owner_id)
    game = active_bj_games.get(game_key)

    if not game:
        await callback.answer("Игра завершена!", show_alert=True)
        return

    bet = game["bet"]
    deck = game["deck"]
    player = game["player"]
    dealer = game["dealer"]

    if action == "fold":
        refund = bet * 0.5
        await update_balance(owner_id, refund)
        del active_bj_games[game_key]
        await _remove_game("blackjack", f"{callback.message.chat.id}:{owner_id}")
        await callback.message.edit_text(f"🚪 Вы сдались. Возвращено 50% ставки: `{refund:,.2f}` GHRAM.", parse_mode="Markdown")
        await callback.answer("Сдались")
        return

    if action == "hit":
        player.append(deck.pop())
        p_score = get_card_value(player)

        if p_score > 21:
            del active_bj_games[game_key]
            await _remove_game("blackjack", f"{callback.message.chat.id}:{owner_id}")
            await add_history(owner_id, "Блэкджек (Перебор)", -bet)
            await callback.message.edit_text(
                f"💥 **Перебор! ({p_score})**\nВаши карты: {' '.join(player)}\n💰 Потеряно: `{bet:,.2f}` GHRAM",
                parse_mode="Markdown"
            )
            await callback.answer("💥 Перебор!")
            return

        text = (
            f"🃏 **Блэкджек**\n\n"
            f"👤 Ваши карты: {' '.join(player)} (**{p_score}**)\n"
            f"🤖 Дилер: {dealer[0]} 🎴 (**?**)\n\n"
            f"💰 Ставка: `{bet:,.2f}` GHRAM"
        )
        await callback.message.edit_text(text, reply_markup=build_bj_keyboard(owner_id), parse_mode="Markdown")
        await callback.answer()
        return

    if action == "stand":
        d_score = get_card_value(dealer)
        while d_score < 17:
            dealer.append(deck.pop())
            d_score = get_card_value(dealer)

        p_score = get_card_value(player)
        del active_bj_games[game_key]
        await _remove_game("blackjack", f"{callback.message.chat.id}:{owner_id}")

        if d_score > 21 or p_score > d_score:
            win = bet * 1.95  # House edge 1.95x
            await update_balance(owner_id, win)
            await add_history(owner_id, "Блэкджек (Победа)", win - bet)
            res = f"🎉 **ПОБЕДА!** Вы выиграли `{win:,.2f}` GHRAM!"
        elif p_score < d_score:
            await add_history(owner_id, "Блэкджек (Поражение)", -bet)
            res = f"💥 **ПОРАЖЕНИЕ!** Дилер забирает банк."
        else:
            await update_balance(owner_id, bet)
            res = f"🤝 **НИЧЬЯ!** Ставка `{bet:,.2f}` GHRAM возвращена."

        text = (
            f"🃏 **Итог Игры в Блэкджек**\n\n"
            f"👤 Ваши карты: {' '.join(player)} (**{p_score}**)\n"
            f"🤖 Дилер: {' '.join(dealer)} (**{d_score}**)\n\n"
            f"{res}"
        )
        await callback.message.edit_text(text, parse_mode="Markdown")
        await callback.answer()


# --- ❌⭕ КРЕСТИКИ-НОЛИКИ (PvE / PvP) ---
@dp.message(F.text.lower().startswith(("крестики", "/ttt")))
async def game_ttt(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()

    if len(parts) < 2:
        await message.reply("❌ Введите: `крестики [сумма]` для игры с ботом!")
        return

    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств!")
        return

    await update_balance(user['tg_id'], -bet)

    game_id = secrets.token_hex(4)
    board = [0] * 9

    active_ttt_games[game_id] = {
        "chat_id": message.chat.id,
        "p1_id": user['tg_id'],
        "p1_name": f"@{user['username']}" if user['username'] else "Игрок",
        "bet": bet,
        "board": board,
        "turn": user['tg_id']
    }

    kb = build_ttt_keyboard(game_id, board, True)
    await message.reply(
        f"❌⭕ **Крестики-Нолики (PvE)**\n"
        f"💰 Ставка: `{bet:,.2f}` GHRAM\n"
        f"Ваш ход! (Вы играете за ❌)",
        reply_markup=kb,
        parse_mode="Markdown"
    )

def check_ttt_winner(board):
    lines = [
        [0, 1, 2], [3, 4, 5], [6, 7, 8],
        [0, 3, 6], [1, 4, 7], [2, 5, 8],
        [0, 4, 8], [2, 4, 6]
    ]
    for a, b, c in lines:
        if board[a] != 0 and board[a] == board[b] == board[c]:
            return board[a]
    if 0 not in board:
        return -1
    return 0

@dp.callback_query(F.data.startswith("ttt_move:"))
async def cb_ttt_move(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    game_id = parts[1]
    cell = int(parts[2])

    game = active_ttt_games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if callback.from_user.id != game["p1_id"]:
        await callback.answer("❌ Это не ваша игра!", show_alert=True)
        return

    board = game["board"]
    if board[cell] != 0:
        await callback.answer("Клетка уже занята!")
        return

    board[cell] = 1  # ❌ Ход игрока
    win_status = check_ttt_winner(board)

    bet = game["bet"]
    owner_id = game["p1_id"]

    if win_status == 1:
        win_amt = bet * 1.90  # House edge 1.9x
        await update_balance(owner_id, win_amt)
        await add_history(owner_id, "Крестики-Нолики (Победа)", win_amt - bet)
        del active_ttt_games[game_id]
        await callback.message.edit_text(
            f"🎉 **ПОБЕДА!** Вы выиграли `{win_amt:,.2f}` GHRAM!",
            reply_markup=build_ttt_keyboard(game_id, board, False),
            parse_mode="Markdown"
        )
        return
    elif win_status == -1:
        await update_balance(owner_id, bet)
        del active_ttt_games[game_id]
        await callback.message.edit_text(
            f"🤝 **НИЧЬЯ!** Ставка `{bet:,.2f}` GHRAM возвращена.",
            reply_markup=build_ttt_keyboard(game_id, board, False),
            parse_mode="Markdown"
        )
        return

    # Ход Бота (⭕ = 2)
    empty_cells = [i for i, v in enumerate(board) if v == 0]
    if empty_cells:
        bot_choice = random.choice(empty_cells)
        board[bot_choice] = 2

    win_status = check_ttt_winner(board)
    if win_status == 2:
        await add_history(owner_id, "Крестики-Нолики (Поражение)", -bet)
        del active_ttt_games[game_id]
        await callback.message.edit_text(
            f"💥 **ПОРАЖЕНИЕ!** Бот победил.",
            reply_markup=build_ttt_keyboard(game_id, board, False),
            parse_mode="Markdown"
        )
        return
    elif win_status == -1:
        await update_balance(owner_id, bet)
        del active_ttt_games[game_id]
        await callback.message.edit_text(
            f"🤝 **НИЧЬЯ!** Ставка `{bet:,.2f}` GHRAM возвращена.",
            reply_markup=build_ttt_keyboard(game_id, board, False),
            parse_mode="Markdown"
        )
        return

    await callback.message.edit_text(
        f"❌⭕ **Крестики-Нолики (PvE)**\n💰 Ставка: `{bet:,.2f}` GHRAM\nВаш ход!",
        reply_markup=build_ttt_keyboard(game_id, board, True),
        parse_mode="Markdown"
    )
    await callback.answer()


# --- 🎰 СЛОТЫ (SLOTS С RTP 92%) ---
@dp.message(F.text.lower().startswith(("слоты", "слот", "/slots", "с ")))
async def game_slots(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split()

    if len(parts) < 2:
        await message.reply("🎰 Введите ставку: `слоты [сумма]`")
        return

    bet = parse_amount(parts[1], user['balance'])
    if not bet or bet <= 0 or not check_balance(user['tg_id'], user['balance'], bet):
        await message.reply("❌ Недостаточно средств!")
        return

    await update_balance(user['tg_id'], -bet)

    symbols = ["🍇", "🍒", "🍋", "💎", "7️⃣"]
    
    # Распределение RTP ~92%
    roll = random.random()
    if roll < 0.005:
        res_symbols = ["7️⃣", "7️⃣", "7️⃣"]
        multiplier = 10.0
    elif roll < 0.02:
        res_symbols = ["💎", "💎", "💎"]
        multiplier = 5.0
    elif roll < 0.07:
        s = random.choice(["🍇", "🍒", "🍋"])
        res_symbols = [s, s, s]
        multiplier = 3.0
    elif roll < 0.22:
        s = random.choice(symbols)
        other = random.choice([x for x in symbols if x != s])
        res_symbols = [s, s, other]
        random.shuffle(res_symbols)
        multiplier = 1.5
    else:
        res_symbols = random.sample(symbols, 3)
        multiplier = 0.0

    display_name = f"@{user['username']}" if user['username'] else "Игрок"
    slot_line = " | ".join(res_symbols)

    if multiplier > 0:
        win_amt = bet * multiplier
        await update_balance(user['tg_id'], win_amt)
        await add_history(user['tg_id'], "Слоты (Победа)", win_amt - bet)
        res_msg = f"🎉 **ДЖЕКПОТ!** Вы выиграли `{win_amt:,.2f}` GHRAM! ({multiplier}x)"
    else:
        await add_history(user['tg_id'], "Слоты (Поражение)", -bet)
        res_msg = f"💥 **УВЫ!** Вы ничего не выиграли."

    text = (
        f"🎰 **Игровой Автомат GHRAM Slots**\n\n"
        f"🎰 [ {slot_line} ]\n\n"
        f"👤 Игрок: {display_name}\n"
        f"💰 Ставка: `{bet:,.2f}` GHRAM\n"
        f"{res_msg}"
    )
    await message.reply(text, parse_mode="Markdown")

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
                f"🏦 **Ваш Банковский Кредит**\n\n"
                f"👤 Заемщик: @{user['username']}\n"
                f"💵 Начальная сумма: `{loan['amount']:,.2f}` GHRAM\n"
                f"📈 Сумма к возврату (с 15% %): `{loan['repayment_amount']:,.2f}` GHRAM\n\n"
                f"💡 Чтобы погасить кредит, напишите: `погасить [сумма]` или нажмите кнопку ниже."
            )
            await message.reply(text, parse_mode="Markdown", reply_markup=build_loan_keyboard(user['tg_id'], True))
        else:
            max_loan = 100000.0 + (user['hourly_income'] * 20)
            text = (
                f"🏦 **Банковский Центр Кредитования**\n\n"
                f"Вы можете взять заем под **15%** годовых!\n"
                f"📊 Ваш лимит кредита: `{max_loan:,.2f}` GHRAM\n\n"
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
        f"✅ **Кредит успешно одобрен!**\n\n"
        f"💰 Начислено на баланс: `+{requested:,.2f}` GHRAM\n"
        f"📊 Сумма к возврату (15%): `{repay_amount:,.2f}` GHRAM\n\n"
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
        await message.reply(f"❌ Недостаточно средств на балансе! Требуется: `{pay_amount:,.2f}` GHRAM.", parse_mode="Markdown")
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
        await message.reply("🎉 **Вы полностью погасили свой кредит!** Ваш кредитный рейтинг укрепился.", parse_mode="Markdown")
    else:
        await message.reply(f"✅ Внесено `{pay_amount:,.2f}` GHRAM. Остаток по кредиту: `{new_due:,.2f}` GHRAM.", parse_mode="Markdown")

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
    await callback.message.edit_text("🎉 **Вы полностью погасили свой кредит!**", parse_mode="Markdown")
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
        f"✅ **Быстрый заем оформлен!**\n\n"
        f"💰 Начислено: `+{requested:,.2f}` GHRAM\n"
        f"📊 Сумма к возврату: `{repay_amount:,.2f}` GHRAM"
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
            f"🏦 **Ваш Банковский Кредит**\n\n"
            f"👤 Заемщик: @{user['username']}\n"
            f"💵 Начальная сумма: `{loan['amount']:,.2f}` GHRAM\n"
            f"📈 Сумма к возврату (с 15% %): `{loan['repayment_amount']:,.2f}` GHRAM"
        )
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_loan_keyboard(user['tg_id'], True))
    else:
        max_loan = 100000.0 + (user['hourly_income'] * 20)
        text = (
            f"🏦 **Банковский Центр Кредитования**\n\n"
            f"Вы можете взять заем под **15%** годовых!\n"
            f"📊 Ваш лимит кредита: `{max_loan:,.2f}` GHRAM"
        )
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_loan_keyboard(user['tg_id'], False))
    await callback.answer("Обновлено")

# ----------------------------------------------------
# 9. БИЗНЕС МAЙНИНГ-ФЕРМА 💻⚡ (С ИЗНОСОМ И ОБСЛУЖИВАНИЕМ)
# ----------------------------------------------------
GPU_BASE_PRICE = 10000.0
LVL_BASE_PRICE = 25000.0

async def get_or_create_farm(user_id: int):
    now = int(time.time())
    await bot_db.execute(
        "INSERT OR IGNORE INTO mining_farms (user_id, level, gpu_count, last_collect, durability) VALUES (?, 1, 1, ?, 100)",
        (user_id, now)
    )
    await bot_db.commit()
    async with bot_db.execute("SELECT * FROM mining_farms WHERE user_id = ?", (user_id,)) as cursor:
        return await cursor.fetchone()

def calculate_farm_income(level: int, gpu_count: int, last_collect: int):
    now = int(time.time())
    elapsed_seconds = max(0, now - last_collect)
    income_per_hour = level * gpu_count * 350.0
    accumulated = (income_per_hour / 3600.0) * elapsed_seconds
    return income_per_hour, min(accumulated, income_per_hour * 24)

@dp.message(F.text.lower().startswith(("майнинг", "/mining", "ферма", "бизнес")))
async def show_mining_farm(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    farm = await get_or_create_farm(user['tg_id'])

    income_per_hour, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))

    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"👤 Владелец: @{user['username']}\n"
        f"⚡ Уровень системы: **{farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{farm['gpu_count']} шт.**\n"
        f"🛠 Износ оборудования: **{farm['durability']}%**\n"
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{uncollected:,.2f}` GHRAM\n"
        f"💡 *(С удержанием 8% на электричество/аренду)*\n\n"
        f"🛒 Цена новой GPU: `{gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{lvl_cost:,.0f}` GHRAM"
    )

    kb = build_mining_keyboard(user['tg_id'], gpu_cost, lvl_cost, farm['durability'])
    await message.reply(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("farm_claim:"))
async def cb_farm_claim(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return

    farm = await get_or_create_farm(owner_id)
    if farm['durability'] <= 0:
        await callback.answer("🚨 Ваше оборудование сломано (0% прочности)! Отремонтируйте его.", show_alert=True)
        return

    _, accumulated = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])

    if accumulated < 1.0:
        await callback.answer("⏳ Слишком мало накопилось для сбора!", show_alert=True)
        return

    # Автоматическое удержание 8% на электричество и обслуживание
    maintenance_tax = accumulated * 0.08
    net_income = accumulated - maintenance_tax

    # Износ прочности на 5% за каждый сбор
    new_durability = max(0, farm['durability'] - 5)
    now = int(time.time())

    await bot_db.execute(
        "UPDATE mining_farms SET last_collect = ?, durability = ? WHERE user_id = ?",
        (now, new_durability, owner_id)
    )
    await bot_db.commit()

    await update_balance(owner_id, net_income)
    await add_history(owner_id, "Сбор дохода фермы", net_income)

    await callback.answer(f"⚡ Собрано `{net_income:,.2f}` GHRAM (Удержан налог {maintenance_tax:,.2f} GHRAM)!", show_alert=True)
    
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    
    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"⚡ Уровень системы: **{farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{farm['gpu_count']} шт.**\n"
        f"🛠 Износ оборудования: **{new_durability}%**\n\n"
        f"🔋 **Незабранный майнинг:** `0.00` GHRAM\n"
        f"🛒 Цена новой GPU: `{gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{lvl_cost:,.0f}` GHRAM"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_mining_keyboard(owner_id, gpu_cost, lvl_cost, new_durability))

@dp.callback_query(F.data.startswith("farm_repair:"))
async def cb_farm_repair(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return

    farm = await get_or_create_farm(owner_id)
    if farm['durability'] >= 100:
        await callback.answer("Ваша ферма в идеальном состоянии (100%)!", show_alert=True)
        return

    user = await get_or_create_user(owner_id, callback.from_user.username)
    
    # Стоимость ремонта = 25% от общей стоимости улучшения фермы
    total_farm_val = (farm['level'] * LVL_BASE_PRICE) + (farm['gpu_count'] * GPU_BASE_PRICE)
    repair_cost = total_farm_val * 0.25

    if not check_balance(owner_id, user['balance'], repair_cost):
        await callback.answer(f"❌ Недостаточно монет для ремонта! Нужно: {repair_cost:,.0f} GHRAM", show_alert=True)
        return

    await update_balance(owner_id, -repair_cost)
    await bot_db.execute("UPDATE mining_farms SET durability = 100 WHERE user_id = ?", (owner_id,))
    await bot_db.commit()

    await callback.answer(f"🛠 Оборудование отремонтировано до 100% за {repair_cost:,.0f} GHRAM!", show_alert=True)

    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    
    await callback.message.edit_reply_markup(reply_markup=build_mining_keyboard(owner_id, gpu_cost, lvl_cost, 100))

@dp.callback_query(F.data.startswith("farm_buy_gpu:"))
async def cb_farm_buy_gpu(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return

    farm = await get_or_create_farm(owner_id)
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    
    user = await get_or_create_user(owner_id, callback.from_user.username)
    if not check_balance(owner_id, user['balance'], gpu_cost):
        await callback.answer(f"❌ Недостаточно средств! Нужно: {gpu_cost:,.0f} GHRAM", show_alert=True)
        return

    await update_balance(owner_id, -gpu_cost)
    await bot_db.execute("UPDATE mining_farms SET gpu_count = gpu_count + 1 WHERE user_id = ?", (owner_id,))
    await bot_db.commit()

    await callback.answer("🛒 Куплена 1 новая видеокарта!", show_alert=True)
    
    new_farm = await get_or_create_farm(owner_id)
    new_gpu_cost = GPU_BASE_PRICE * (1.35 ** (new_farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (new_farm['level'] - 1))
    
    income_per_hour, uncollected = calculate_farm_income(new_farm['level'], new_farm['gpu_count'], new_farm['last_collect'])

    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"⚡ Уровень системы: **{new_farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{new_farm['gpu_count']} шт.**\n"
        f"🛠 Износ оборудования: **{new_farm['durability']}%**\n"
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{uncollected:,.2f}` GHRAM\n"
        f"🛒 Цена новой GPU: `{new_gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{lvl_cost:,.0f}` GHRAM"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_mining_keyboard(owner_id, new_gpu_cost, lvl_cost, new_farm['durability']))

@dp.callback_query(F.data.startswith("farm_upgrade_lvl:"))
async def cb_farm_upgrade_lvl(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return

    farm = await get_or_create_farm(owner_id)
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    
    user = await get_or_create_user(owner_id, callback.from_user.username)
    if not check_balance(owner_id, user['balance'], lvl_cost):
        await callback.answer(f"❌ Недостаточно средств! Нужно: {lvl_cost:,.0f} GHRAM", show_alert=True)
        return

    await update_balance(owner_id, -lvl_cost)
    await bot_db.execute("UPDATE mining_farms SET level = level + 1 WHERE user_id = ?", (owner_id,))
    await bot_db.commit()

    await callback.answer("⬆️ Уровень майнинг-системы повышен!", show_alert=True)
    
    new_farm = await get_or_create_farm(owner_id)
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (new_farm['gpu_count'] - 1))
    new_lvl_cost = LVL_BASE_PRICE * (1.60 ** (new_farm['level'] - 1))
    
    income_per_hour, uncollected = calculate_farm_income(new_farm['level'], new_farm['gpu_count'], new_farm['last_collect'])

    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"⚡ Уровень системы: **{new_farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{new_farm['gpu_count']} шт.**\n"
        f"🛠 Износ оборудования: **{new_farm['durability']}%**\n"
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{uncollected:,.2f}` GHRAM\n"
        f"🛒 Цена новой GPU: `{gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{new_lvl_cost:,.0f}` GHRAM"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_mining_keyboard(owner_id, gpu_cost, new_lvl_cost, new_farm['durability']))

@dp.callback_query(F.data.startswith("farm_refresh:"))
async def cb_farm_refresh(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return

    farm = await get_or_create_farm(owner_id)
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    
    income_per_hour, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])

    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"⚡ Уровень системы: **{farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{farm['gpu_count']} шт.**\n"
        f"🛠 Износ оборудования: **{farm['durability']}%**\n"
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{uncollected:,.2f}` GHRAM\n"
        f"🛒 Цена новой GPU: `{gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{lvl_cost:,.0f}` GHRAM"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=build_mining_keyboard(owner_id, gpu_cost, lvl_cost, farm['durability']))
    await callback.answer("Обновлено!")

# ----------------------------------------------------
# 10. КЛАНЫ (СОЗДАНИЕ ЗА 1 МЛН GHRAM, УПРАВЛЕНИЕ)
# ----------------------------------------------------
CLAN_CREATION_PRICE = 1_000_000.0

@dp.message(F.text.lower().startswith(("создать клан", "/clan_create")))
async def cmd_create_clan(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    parts = message.text.split(maxsplit=2)

    if len(parts) < 2 if message.text.startswith("/clan_create") else len(parts) < 3:
        await message.reply("⚔️ Использование: `создать клан [название]` (Стоимость: 1,000,000 GHRAM)")
        return

    clan_name = parts[-1].strip()
    if len(clan_name) < 3 or len(clan_name) > 20:
        await message.reply("❌ Название клана должно быть от 3 до 20 символов!")
        return

    if user['clan_id'] and user['clan_id'] > 0:
        await message.reply("❌ Вы уже состоите в клане! Сначала покиньте текущий клан.")
        return

    if not check_balance(user['tg_id'], user['balance'], CLAN_CREATION_PRICE):
        await message.reply(f"❌ Для создания клана требуется `{CLAN_CREATION_PRICE:,.0f}` GHRAM!")
        return

    try:
        now = int(time.time())
        cursor = await bot_db.execute(
            "INSERT INTO clans (name, owner_id, balance, created_at) VALUES (?, ?, 0.0, ?)",
            (clan_name, user['tg_id'], now)
        )
        clan_id = cursor.lastrowid
        
        await bot_db.execute("UPDATE users SET clan_id = ? WHERE tg_id = ?", (clan_id, user['tg_id']))
        await bot_db.commit()

        await update_balance(user['tg_id'], -CLAN_CREATION_PRICE)
        await add_history(user['tg_id'], "Создание клана", -CLAN_CREATION_PRICE)

        await message.reply(f"🎉 **Клан «{clan_name}» успешно создан!**\nВы стали его главой.", parse_mode="Markdown")
    except aiosqlite.IntegrityError:
        await message.reply("❌ Клан с таким названием уже существует!")

@dp.message(F.text.lower().in_(["клан", "мой клан", "/clan", "кланы"]))
async def show_clan_info(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    
    if not user['clan_id'] or user['clan_id'] == 0:
        text = (
            "⚔️ **Клановая Система GHRAM**\n\n"
            "Вы пока не состоите ни в одном клане.\n\n"
            "• Создать свой клан: `создать клан [название]` (1,000,000 GHRAM)\n"
            "• Посмотреть топ кланов: `топ кланов`"
        )
        await message.reply(text, parse_mode="Markdown")
        return

    async with bot_db.execute("SELECT * FROM clans WHERE id = ?", (user['clan_id'],)) as cursor:
        clan = await cursor.fetchone()

    if not clan:
        await bot_db.execute("UPDATE users SET clan_id = 0 WHERE tg_id = ?", (user['tg_id'],))
        await bot_db.commit()
        await message.reply("❌ Ваш клан был распущен.")
        return

    async with bot_db.execute("SELECT tg_id, username FROM users WHERE clan_id = ?", (user['clan_id'],)) as cursor:
        members = await cursor.fetchall()

    owner = await get_or_create_user(clan['owner_id'])
    owner_name = f"@{owner['username']}" if owner['username'] else f"ID {owner['tg_id']}"

    members_str = "\n".join([f"• @{m['username']}" if m['username'] else f"• ID {m['tg_id']}" for m in members[:15]])

    text = (
        f"⚔️ **Клан: «{clan['name']}»**\n\n"
        f"👑 Глава: {owner_name}\n"
        f"💰 Казна клана: `{clan['balance']:,.2f}` GHRAM\n"
        f"👥 Участников: **{len(members)}**\n\n"
        f"📋 **Состав клана:**\n{members_str}\n\n"
        f"💡 Команды клана:\n"
        f"• `положить [сумма]` — пополнить казну клана\n"
        f"• `пригласить [@username]` — пригласить игрока\n"
        f"• `покинуть клан` — выйти из клана"
    )
    await message.reply(text, parse_mode="Markdown")

@dp.message(F.text.lower().startswith(("положить", "депозит клан", "/clan_deposit")))
async def clan_deposit(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    if not user['clan_id'] or user['clan_id'] == 0:
        await message.reply("❌ Вы не состоите в клане!")
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("✍️ Использование: `положить [сумма]`")
        return

    amount = parse_amount(parts[1], user['balance'])
    if not amount or amount <= 0 or not check_balance(user['tg_id'], user['balance'], amount):
        await message.reply("❌ Недостаточно средств на балансе!")
        return

    await update_balance(user['tg_id'], -amount)
    await bot_db.execute("UPDATE clans SET balance = balance + ? WHERE id = ?", (amount, user['clan_id']))
    await bot_db.commit()

    await add_history(user['tg_id'], "Взнос в казну клана", -amount)
    await message.reply(f"✅ Вы внесли `{amount:,.2f}` GHRAM в казну клана!", parse_mode="Markdown")

@dp.message(F.text.lower().startswith(("пригласить", "/clan_invite")))
async def clan_invite(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    if not user['clan_id'] or user['clan_id'] == 0:
        await message.reply("❌ Вы не состоите в клане!")
        return

    parts = message.text.split()
    target_user = None

    if message.reply_to_message:
        target_user = await get_or_create_user(message.reply_to_message.from_user.id, message.reply_to_message.from_user.username)
    elif len(parts) >= 2:
        target_user = await get_user_by_identifier(parts[1])

    if not target_user:
        await message.reply("❌ Укажите игрока для приглашения!")
        return

    if target_user['clan_id'] and target_user['clan_id'] > 0:
        await message.reply("❌ Игрок уже состоит в другом клане!")
        return

    async with bot_db.execute("SELECT name FROM clans WHERE id = ?", (user['clan_id'],)) as cursor:
        clan = await cursor.fetchone()

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Принять ✅", callback_data=f"clan_acc:{user['clan_id']}:{target_user['tg_id']}"),
        InlineKeyboardButton(text="Отклонить ⛔", callback_data=f"clan_dec:{target_user['tg_id']}")
    ]])

    target_mention = f"@{target_user['username']}" if target_user['username'] else f"ID {target_user['tg_id']}"
    await message.reply(
        f"✉️ {target_mention}, вас приглашают вступить в клан **«{clan['name']}»**!",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("clan_acc:"))
async def cb_clan_accept(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    clan_id = int(parts[1])
    target_id = int(parts[2])

    if callback.from_user.id != target_id:
        await callback.answer("❌ Это приглашение не для вас!", show_alert=True)
        return

    await bot_db.execute("UPDATE users SET clan_id = ? WHERE tg_id = ?", (clan_id, target_id))
    await bot_db.commit()

    await callback.message.edit_text("🎉 Вы успешно вступили в клан!")
    await callback.answer("Добро пожаловать в клан!")

@dp.callback_query(F.data.startswith("clan_dec:"))
async def cb_clan_decline(callback: types.CallbackQuery):
    target_id = int(callback.data.split(":")[1])
    if callback.from_user.id != target_id:
        await callback.answer("❌ Это приглашение не для вас!", show_alert=True)
        return

    await callback.message.edit_text("⛔ Приглашение в клан отклонено.")
    await callback.answer("Отклонено")

@dp.message(F.text.lower().in_(["покинуть клан", "/clan_leave", "выйти из клана"]))
async def clan_leave(message: types.Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    if not user['clan_id'] or user['clan_id'] == 0:
        await message.reply("❌ Вы не состоите в клане!")
        return

    async with bot_db.execute("SELECT owner_id FROM clans WHERE id = ?", (user['clan_id'],)) as cursor:
        clan = await cursor.fetchone()

    if clan and clan['owner_id'] == user['tg_id']:
        await message.reply("❌ Глава клана не может покинуть клан! Вы можете только распустить его.")
        return

    await bot_db.execute("UPDATE users SET clan_id = 0 WHERE tg_id = ?", (user['tg_id'],))
    await bot_db.commit()

    await message.reply("🚪 Вы успешно покинули клан.")

@dp.message(F.text.lower().startswith(("топ кланов", "/topclans")))
async def top_clans(message: types.Message):
    async with bot_db.execute("SELECT name, balance FROM clans ORDER BY balance DESC LIMIT 10") as cursor:
        clans = await cursor.fetchall()

    if not clans:
        await message.reply("🏆 Пока нет ни одного созданного клана!")
        return

    text = "🏆 **Топ-10 Кланов по казне:**\n\n"
    for idx, c in enumerate(clans, start=1):
        text += f"{idx}. **{c['name']}** — `{c['balance']:,.2f}` GHRAM\n"

    await message.reply(text, parse_mode="Markdown")

# ----------------------------------------------------
# 11. ЗАПУСК БОТА
# ----------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await cleanup_all_active_games()
    asyncio.create_task(periodic_cleanup_task())
    logging.info("Бот запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
