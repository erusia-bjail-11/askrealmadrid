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
        
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT language FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row['language']:
                return row['language']
    return "ru"

# ----------------------------------------------------
# 3. РАБОТА С БАЗОЙ ДАННЫХ
# ----------------------------------------------------
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
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
        
        # Полная безопасная миграция всех колонок
        user_columns = [
            ("bank", "REAL DEFAULT 0.0"),
            ("hourly_income", "REAL DEFAULT 150.0"),
            ("last_claim", "INTEGER DEFAULT 0"),
            ("last_bonus", "INTEGER DEFAULT 0"),
            ("language", "TEXT DEFAULT 'ru'")
        ]
        for col_name, col_type in user_columns:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                amount REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица для кредитов и займов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS loans (
                user_id INTEGER PRIMARY KEY,
                amount REAL DEFAULT 0.0,
                repayment_amount REAL DEFAULT 0.0,
                created_at INTEGER DEFAULT 0
            )
        """)

        # Таблица для бизнеса: майнинг-ферма
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mining_farms (
                user_id INTEGER PRIMARY KEY,
                level INTEGER DEFAULT 1,
                gpu_count INTEGER DEFAULT 1,
                last_collect INTEGER DEFAULT 0
            )
        """)

        await db.commit()

async def get_or_create_user(tg_id: int, username: str | None = None):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
            user = await cursor.fetchone()
            
        now = int(time.time())
        if not user:
            initial_balance = 10**18 if tg_id == OWNER_ID else 1000.0
            await db.execute(
                "INSERT INTO users (tg_id, username, balance, last_claim, last_bonus, language) VALUES (?, ?, ?, ?, 0, 'ru')",
                (tg_id, username or "Неизвестно", initial_balance, now)
            )
            await db.commit()
            async with db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
                user = await cursor.fetchone()
        else:
            if tg_id == OWNER_ID and user['balance'] < 10**17:
                await db.execute("UPDATE users SET balance = ? WHERE tg_id = ?", (10**18, tg_id))
                await db.commit()
            if username and user['username'] != username:
                await db.execute("UPDATE users SET username = ? WHERE tg_id = ?", (username, tg_id))
                await db.commit()
            
        return user

async def get_user_by_identifier(identifier: str):
    identifier = identifier.strip()
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        if identifier.startswith("@"):
            username = identifier[1:]
            async with db.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (username,)) as cursor:
                return await cursor.fetchone()
        elif identifier.isdigit():
            tg_id = int(identifier)
            async with db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)) as cursor:
                return await cursor.fetchone()
        else:
            async with db.execute("SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (identifier,)) as cursor:
                return await cursor.fetchone()

async def update_balance(tg_id: int, amount: float):
    if tg_id == OWNER_ID and amount < 0:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, tg_id))
        await db.commit()

async def add_history(user_id: int, action: str, amount: float):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO history (user_id, action, amount) VALUES (?, ?, ?)",
            (user_id, action, amount)
        )
        await db.commit()

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

def build_mining_keyboard(user_id: int, gpu_cost: float, lvl_cost: float):
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

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE tg_id != ?", (parsed, OWNER_ID))
        await db.commit()

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
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET balance = 0 WHERE tg_id = ?", (target_user['tg_id'],))
            await db.commit()
            
        await message.reply(f"🔥 Баланс пользователя @{target_user['username']} (ID: {target_user['tg_id']}) аннулирован!")
    else:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET balance = 0 WHERE tg_id != ?", (OWNER_ID,))
            await db.commit()
            
        await message.reply("🔥 Баланс **всех игроков** был успешно аннулирован!", parse_mode="Markdown")

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

    # 1. Завершение Мин
    for key in list(active_mines_games.keys()):
        if key[1] == target_id:
            game = active_mines_games.pop(key)
            refund_amount += game['bet']
            cancelled_games += 1

    # 2. Завершение Джокера
    for key in list(active_joker_games.keys()):
        if key[1] == target_id:
            game = active_joker_games.pop(key)
            refund_amount += game['bet']
            cancelled_games += 1

    # 3. Завершение Краша
    for key in list(active_crash_games.keys()):
        if key[1] == target_id:
            game = active_crash_games.pop(key)
            game['status'] = 'cancelled'
            refund_amount += game['bet']
            cancelled_games += 1

    # 4. Отмена ставок в Рулетке
    for key in list(active_roulette_bets.keys()):
        if key[1] == target_id:
            bets = active_roulette_bets.pop(key)
            refund_amount += sum(b['bet'] for b in bets)
            cancelled_games += 1

    if refund_amount > 0:
        await update_balance(target_id, refund_amount)

    # 5. Отмена ожидающих дуэлей
    for d_id in list(pending_duels.keys()):
        d = pending_duels.get(d_id)
        if d and (d['challenger_id'] == target_id or d['target_id'] == target_id):
            d = pending_duels.pop(d_id)
            d['timer_task'].cancel()
            await update_balance(d['challenger_id'], d['bet'])
            cancelled_games += 1
            try:
                await d['msg'].edit_text("🚫 Дуэль отменена администратором.")
            except Exception:
                pass

    # 6. Отмена активных дуэлей
    for d_id in list(active_duels.keys()):
        d = active_duels.get(d_id)
        if d and (d['p1_id'] == target_id or d['p2_id'] == target_id):
            d = active_duels.pop(d_id)
            d['timer_task'].cancel()
            await update_balance(d['p1_id'], d['bet'])
            await update_balance(d['p2_id'], d['bet'])
            cancelled_games += 1
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
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET language = ? WHERE tg_id = ?", (new_lang, callback.from_user.id))
        await db.commit()

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
        
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET last_bonus = ? WHERE tg_id = ?", (now, user['tg_id']))
            await db.commit()
            
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
    
    if lang == "en":
        text = (
            f"👤 **User Profile:** @{user['username']}\n"
            f"🆔 ID: `{user['tg_id']}`\n"
            f"💰 Balance: `{bal_str}` coins\n"
            f"🏦 In Bank: `{bank_str}` coins\n"
            f"📈 Passive Income: `{user['hourly_income']:,.2f}`/hour"
        )
    else:
        text = (
            f"👤 **Профиль пользователя:** @{user['username']}\n"
            f"🆔 ID: `{user['tg_id']}`\n"
            f"💰 Баланс: `{bal_str}` монет\n"
            f"🏦 В банке: `{bank_str}` монет\n"
            f"📈 Пассивный доход: `{user['hourly_income']:,.2f}`/час"
        )
    await message.reply(text, parse_mode="Markdown")

# --- ИСТОРИЯ ---
@dp.message(F.text.lower().in_(["/история", "история", "history"]))
async def show_history(message: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
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

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
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

# --- ПЕРЕВОД ---
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

    await update_balance(sender['tg_id'], -amount)
    await update_balance(target_user['tg_id'], amount)

    target_name = f"@{target_user['username']}" if target_user['username'] != "Неизвестно" else f"ID {target_user['tg_id']}"

    await add_history(sender['tg_id'], f"Перевод для {target_name}", -amount)
    await add_history(target_user['tg_id'], f"Перевод от @{sender['username']}", amount)

    await message.reply(
        f"✅ Вы успешно перевели `{amount:,.2f}` монет пользователю **{target_name}**!", 
        parse_mode="Markdown"
    )

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

    async def invite_timeout():
        await asyncio.sleep(180)
        if duel_id in pending_duels:
            d = pending_duels.pop(duel_id, None)
            if d:
                await update_balance(d['challenger_id'], d['bet'])
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

    async def shot_timeout(current_duel_id):
        await asyncio.sleep(180)
        if current_duel_id in active_duels:
            ad = active_duels.pop(current_duel_id, None)
            if ad:
                await update_balance(ad["p1_id"], ad["bet"])
                await update_balance(ad["p2_id"], ad["bet"])
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
        await message.reply("❌ У вас уже есть активная игра в мины! Закончите её.")
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
        await message.reply("❌ У вас уже есть активная игра в Джокер!")
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
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM loans WHERE user_id = ?", (user_id,)) as cursor:
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

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO loans (user_id, amount, repayment_amount, created_at) VALUES (?, ?, ?, ?)",
            (user['tg_id'], requested, repay_amount, now)
        )
        await db.commit()

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

    async with aiosqlite.connect(DB_NAME) as db:
        if new_due <= 0.01:
            await db.execute("DELETE FROM loans WHERE user_id = ?", (user['tg_id'],))
        else:
            await db.execute("UPDATE loans SET repayment_amount = ? WHERE user_id = ?", (new_due, user['tg_id']))
        await db.commit()

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
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM loans WHERE user_id = ?", (user['tg_id'],))
        await db.commit()

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

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO loans (user_id, amount, repayment_amount, created_at) VALUES (?, ?, ?, ?)",
            (user['tg_id'], requested, repay_amount, now)
        )
        await db.commit()

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
# 9. БИЗНЕС МAЙНИНГ-ФЕРМА 💻⚡
# ----------------------------------------------------
GPU_BASE_PRICE = 10000.0
LVL_BASE_PRICE = 25000.0

async def get_or_create_farm(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM mining_farms WHERE user_id = ?", (user_id,)) as cursor:
            farm = await cursor.fetchone()

        now = int(time.time())
        if not farm:
            await db.execute(
                "INSERT INTO mining_farms (user_id, level, gpu_count, last_collect) VALUES (?, 1, 1, ?)",
                (user_id, now)
            )
            await db.commit()
            async with db.execute("SELECT * FROM mining_farms WHERE user_id = ?", (user_id,)) as cursor:
                farm = await cursor.fetchone()
        return farm

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
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{uncollected:,.2f}` GHRAM\n"
        f"🛒 Цена новой GPU: `{gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{lvl_cost:,.0f}` GHRAM"
    )

    kb = build_mining_keyboard(user['tg_id'], gpu_cost, lvl_cost)
    await message.reply(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("farm_claim:"))
async def callback_farm_claim(callback: types.CallbackQuery):
    owner_id = int(callback.data.split(":")[1])
    if callback.from_user.id != owner_id:
        await callback.answer("❌ Это не ваша ферма!", show_alert=True)
        return

    farm = await get_or_create_farm(owner_id)
    _, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])

    if uncollected < 1.0:
        await callback.answer("⚡ Доход еще не успел накопиться! Подождите немного.", show_alert=True)
        return

    now = int(time.time())
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE mining_farms SET last_collect = ? WHERE user_id = ?", (now, owner_id))
        await db.commit()

    await update_balance(owner_id, uncollected)
    await add_history(owner_id, "Сбор дохода с майнинга", uncollected)

    await callback.answer(f"✅ Успешно собрано {uncollected:,.2f} GHRAM!", show_alert=True)

    farm = await get_or_create_farm(owner_id)
    user = await get_or_create_user(owner_id, callback.from_user.username)
    income_per_hour, new_uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))

    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"👤 Владелец: @{user['username']}\n"
        f"⚡ Уровень системы: **{farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{farm['gpu_count']} шт.**\n"
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{new_uncollected:,.2f}` GHRAM\n"
        f"🛒 Цена новой GPU: `{gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{lvl_cost:,.0f}` GHRAM"
    )
    kb = build_mining_keyboard(owner_id, gpu_cost, lvl_cost)
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

    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    if not check_balance(user['tg_id'], user['balance'], gpu_cost):
        await callback.answer(f"❌ Недостаточно средств! Нужно: {gpu_cost:,.0f} GHRAM", show_alert=True)
        return

    _, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    now = int(time.time())

    await update_balance(user['tg_id'], -gpu_cost + uncollected)
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE mining_farms SET gpu_count = gpu_count + 1, last_collect = ? WHERE user_id = ?",
            (now, owner_id)
        )
        await db.commit()

    await callback.answer("🎉 Вы успешно купили новую видеокарту!", show_alert=True)

    farm = await get_or_create_farm(owner_id)
    income_per_hour, new_uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    new_gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))

    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"👤 Владелец: @{user['username']}\n"
        f"⚡ Уровень системы: **{farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{farm['gpu_count']} шт.**\n"
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{new_uncollected:,.2f}` GHRAM\n"
        f"🛒 Цена новой GPU: `{new_gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{lvl_cost:,.0f}` GHRAM"
    )
    kb = build_mining_keyboard(owner_id, new_gpu_cost, lvl_cost)
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

    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))
    if not check_balance(user['tg_id'], user['balance'], lvl_cost):
        await callback.answer(f"❌ Недостаточно средств! Нужно: {lvl_cost:,.0f} GHRAM", show_alert=True)
        return

    _, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    now = int(time.time())

    await update_balance(user['tg_id'], -lvl_cost + uncollected)

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE mining_farms SET level = level + 1, last_collect = ? WHERE user_id = ?",
            (now, owner_id)
        )
        await db.commit()

    await callback.answer("🚀 Ваша майнинг-ферма успешно улучшена!", show_alert=True)

    farm = await get_or_create_farm(owner_id)
    income_per_hour, new_uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    new_lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))

    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"👤 Владелец: @{user['username']}\n"
        f"⚡ Уровень системы: **{farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{farm['gpu_count']} шт.**\n"
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{new_uncollected:,.2f}` GHRAM\n"
        f"🛒 Цена новой GPU: `{gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{new_lvl_cost:,.0f}` GHRAM"
    )
    kb = build_mining_keyboard(owner_id, gpu_cost, new_lvl_cost)
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

    income_per_hour, uncollected = calculate_farm_income(farm['level'], farm['gpu_count'], farm['last_collect'])
    gpu_cost = GPU_BASE_PRICE * (1.35 ** (farm['gpu_count'] - 1))
    lvl_cost = LVL_BASE_PRICE * (1.60 ** (farm['level'] - 1))

    text = (
        f"🖥️ **Бизнес: Майнинг-Ферма**\n\n"
        f"👤 Владелец: @{user['username']}\n"
        f"⚡ Уровень системы: **{farm['level']}**\n"
        f"🧩 Видеокарт (GPU): **{farm['gpu_count']} шт.**\n"
        f"📈 Общий доход: `{income_per_hour:,.2f}` GHRAM/час\n\n"
        f"🔋 **Незабранный майнинг:** `{uncollected:,.2f}` GHRAM\n"
        f"🛒 Цена новой GPU: `{gpu_cost:,.0f}` GHRAM\n"
        f"⬆️ Улучшение фермы: `{lvl_cost:,.0f}` GHRAM"
    )
    kb = build_mining_keyboard(owner_id, gpu_cost, lvl_cost)
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
        await message.reply("🚀 **Игра Краш**\nИспользование: `краш [ставка]` или `краш [ставка] [автовывод]`\nПример: `краш 1000` или `краш 1000 2.5`", parse_mode="Markdown")
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
        await message.reply("❌ У вас уже есть запущенная игра в Краш!")
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

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💰 ЗАБРАТЬ (1.00x)", callback_data=f"crash_out:{crash_id}:{user['tg_id']}")
    ]])

    display_name = f"@{user['username']}" if user['username'] and user['username'] != "Неизвестно" else "Игрок"
    msg = await message.reply(
        f"🚀 **Ракета вылетает!**\n\n"
        f"👤 Игрок: {display_name}\n"
        f"💰 Ставка: `{bet:,.2f}` GHRAM\n"
        f"📈 Коэффициент: **1.00x**\n"
        f"💵 Текущий выигрыш: `{bet:,.2f}` GHRAM",
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
                    f"✅ **АВТОВЫВОД СРАБОТАЛ!**\n\n"
                    f"👤 Игрок: {display_name}\n"
                    f"💰 Ставка: `{game['bet']:,.2f}` GHRAM\n"
                    f"🎯 Коэффициент: **{game['auto_cashout']:.2f}x**\n"
                    f"🎉 Выигрыш: **{win:,.2f}** GHRAM"
                )
                try:
                    await msg.edit_text(text, parse_mode="Markdown")
                except Exception:
                    pass
                active_crash_games.pop(game_key, None)
                break

            if current_mult >= game['crash_point']:
                game['status'] = "crashed"
                await add_history(user_id, "Краш (Взрыв)", -game['bet'])

                text = (
                    f"💥 **РАКЕТА ВЗОРВАЛАСЬ НА {game['crash_point']:.2f}x!**\n\n"
                    f"👤 Игрок: {display_name}\n"
                    f"💰 Потеряно: `{game['bet']:,.2f}` GHRAM"
                )
                try:
                    await msg.edit_text(text, parse_mode="Markdown")
                except Exception:
                    pass
                active_crash_games.pop(game_key, None)
                break

            curr_win = round(game['bet'] * current_mult, 2)
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"💰 ЗАБРАТЬ ({current_mult:.2f}x)", callback_data=f"crash_out:{crash_id}:{user_id}")
            ]])

            text = (
                f"🚀 **Ракета летит...**\n\n"
                f"👤 Игрок: {display_name}\n"
                f"💰 Ставка: `{game['bet']:,.2f}` GHRAM\n"
                f"📈 Коэффициент: **{current_mult:.2f}x**\n"
                f"💵 Текущий выигрыш: `{curr_win:,.2f}` GHRAM"
            )
            try:
                await msg.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            except Exception:
                pass
    except Exception as e:
        logging.error(f"Error in crash flight: {e}")
        active_crash_games.pop(game_key, None)

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
        f"🎉 **ВЫ УСПЕШНО ЗАБРАЛИ ВЫИГРЫШ!**\n\n"
        f"👤 Игрок: {display_name}\n"
        f"💰 Ставка: `{game['bet']:,.2f}` GHRAM\n"
        f"📈 Зафиксировано: **{mult:.2f}x**\n"
        f"🏆 Итоговый выигрыш: **{win:,.2f}** GHRAM"
    )

    active_crash_games.pop(game_key, None)

    try:
        await callback.message.edit_text(text, parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer(f"🎉 Вы забрали {win:,.2f} GHRAM!")

# ----------------------------------------------------
# 11. РУЛЕТКА (УМНЫЙ ФИЛЬТР) 🎰
# ----------------------------------------------------
@dp.message(F.text.lower().in_(["отменить", "/отменить"]))
async def roulette_cancel(message: types.Message):
    key = (message.chat.id, message.from_user.id)
    if key in active_roulette_bets and active_roulette_bets[key]:
        total_refund = sum(b['bet'] for b in active_roulette_bets[key])
        await update_balance(message.from_user.id, total_refund)
        active_roulette_bets[key] = []
        await message.reply(f"🚫 Все ваши ставки на этот раунд отменены. Возвращено: `{total_refund:,.2f}` монет.")
    else:
        await message.reply("❌ У вас нет активных ставок.")

@dp.message(F.text.lower().in_(["ставки", "/ставки"]))
async def roulette_show_bets(message: types.Message):
    key = (message.chat.id, message.from_user.id)
    bets = active_roulette_bets.get(key, [])
    if not bets:
        await message.reply("🎰 В текущем раунде у вас нет сделанных ставок.")
        return
    text = "🎰 **Ваши ставки в текущем раунде:**\n\n"
    for idx, b in enumerate(bets, 1):
        text += f"{idx}. `{b['bet']:,.2f}` монет на **{b['type']}**\n"
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
        await message.reply(f"❌ Недостаточно средств для удвоения! Нужно еще `{add_req:,.2f}` монет.")
        return

    await update_balance(user['tg_id'], -add_req)
    for b in bets:
        b['bet'] *= 2
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
        await message.reply(f"❌ Недостаточно монет для повтора! Нужно `{req:,.2f}` монет.")
        return

    await update_balance(user['tg_id'], -req)
    import copy
    active_roulette_bets[key] = copy.deepcopy(last)
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
    await message.reply(f"✅ Принята ставка `{bet:,.2f}` монет на **{bet_type}**.\nНапишите `крутить` для запуска!", parse_mode="Markdown")

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
        
        if bt in ["красное", "red"] and num in RED_NUMBERS:
            total_win += amt * 2
        elif bt in ["черное", "black"] and num in BLACK_NUMBERS:
            total_win += amt * 2
        elif bt in ["чет", "even"] and num > 0 and num % 2 == 0:
            total_win += amt * 2
        elif bt in ["нечет", "odd"] and num % 2 == 1:
            total_win += amt * 2
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

    if total_win > 0:
        await update_balance(message.from_user.id, total_win)
        await add_history(message.from_user.id, "Рулетка (Выигрыш)", total_win - total_bet)
        res_text = f"🎉 **Вы выиграли {total_win:,.2f} монет!**"
    else:
        await add_history(message.from_user.id, "Рулетка (Проигрыш)", -total_bet)
        res_text = f"❌ Вы потеряли {total_bet:,.2f} монет."

    await message.reply(
        f"🎰 **Рулетка крутится...**\n\n"
        f"Выпало число: **{num}** ({color})\n\n"
        f"{res_text}",
        parse_mode="Markdown"
    )

# ----------------------------------------------------
# 12. ЗАПУСК
# ----------------------------------------------------
async def main():
    logging.basicConfig(level=logging.INFO)
    print("🚀 Запуск обновленного бота GHRAM...")
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
