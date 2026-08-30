import asyncio
import hashlib
import html
import logging
import os
import random
import subprocess
import time
from pathlib import Path

import asyncpg
from aiogram import Bot, BaseMiddleware, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    ChatMemberUpdated,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

COOLDOWN_SECONDS = 20 * 60
CLASS_COOLDOWN_SECONDS = 1 * 60
CLASS_SPIN_COST = 30
STEAL_COOLDOWN_SECONDS = 30 * 60
STEAL_OUTCOME_WEIGHTS = (("success", 20), ("partial", 50), ("fail", 30))
STEAL_SUCCESS_SHARE = 0.10
STEAL_PARTIAL_SHARE = 0.05
STEAL_FAIL_LOSS_SHARE = 0.05
CLAN_CREATE_COST = 500
CLAN_MAX_MEMBERS = 20
CLAN_DELETE_CONFIRM_WINDOW = 60
CLAN_TOP_LIMIT = 10
TOP_LIMIT = 10

GROUP_INTRO_TEXT = (
    "<b>🐦 Angry Копилка\n\n"
    "Раз в 20 минут можно крутануть копилку и получить силу и монеты.\n"
    f"За {CLASS_SPIN_COST} монет (раз в минуту) можно крутить класс и получить только силу.\n"
    "Ответом «кража» или /steal на чужое сообщение можно украсть монеты (раз в 30 минут).\n\n"
    "🎰 /AngryOpen — крутануть копилку\n"
    "🎓 /AngryClass — крутануть класс\n"
    "🥷 /steal — украсть монеты (в ответ на сообщение)\n"
    "🏆 /AngryTop — топ силы чата\n\n"
    f"🏰 /clancreate Название — создать клан ({CLAN_CREATE_COST} монет)\n"
    "📨 /claninvite — пригласить в клан (в ответ на сообщение)\n"
    "🚪 /clanleft — выйти из клана\n"
    "💥 /clandelete — удалить клан (дважды подряд)\n"
    "🏆 /clantop — топ кланов по силе\n"
    "ℹ️ /clan [название] — инфо о клане</b>"
)
NON_ADMIN_PRIVATE_TEXT = (
    "<b>🐦 Привет! Я работаю в группах.\n\n"
    "Добавь меня в чат и используй там команды /AngryOpen, /AngryClass и /AngryTop.</b>"
)

CARD_FIELDS = ("name", "power", "money", "photo_id", "rarity", "bird")
FIELD_PROMPTS = {
    "photo_id": "<b>📸 Пришли новое фото карточки</b>",
    "name": "<b>✏️ Введи новое название карточки</b>",
    "power": "<b>⚔️ Введи новую силу карточки (число)</b>",
    "money": "<b>💰 Введи новое количество денег (число)</b>",
}

CLASS_CARD_FIELDS = ("name", "power", "photo_id", "rarity", "bird")
CLASS_FIELD_PROMPTS = {
    "photo_id": "<b>📸 Пришли новое фото карточки</b>",
    "name": "<b>✏️ Введи новое название карточки</b>",
    "power": "<b>⚔️ Введи новую силу карточки (число)</b>",
}

# ── Редкость карточек ────────────────────────────────────────────────────

REGULAR_STAR_EMOJI_ID = "5224378646688474051"
RAINBOW_STAR_EMOJI_ID = "5224307204202476701"

# rarity code -> (кол-во звёзд, тип звезды)
RARITY_INFO = {
    1: (1, "regular"),
    2: (2, "regular"),
    3: (3, "regular"),
    4: (3, "rainbow"),
}
RARITY_PICK_BUTTONS = (
    (1, "⭐"),
    (2, "⭐⭐"),
    (3, "⭐⭐⭐"),
    (4, "🌈⭐⭐⭐"),
)


def stars_html(count: int, kind: str) -> str:
    emoji_id = RAINBOW_STAR_EMOJI_ID if kind == "rainbow" else REGULAR_STAR_EMOJI_ID
    star = f'<tg-emoji emoji-id="{emoji_id}">⭐️</tg-emoji>'
    return star * count


def rarity_display(rarity: int) -> str:
    count, kind = RARITY_INFO[rarity]
    return stars_html(count, kind)


def rarity_pick_kb(callback_prefix: str, back_callback: str):
    kb = InlineKeyboardBuilder()
    for code, label in RARITY_PICK_BUTTONS:
        kb.button(text=label, callback_data=f"{callback_prefix}:{code}")
    kb.button(text="⬅️ Назад", callback_data=back_callback)
    kb.adjust(4, 1)
    return kb.as_markup()


# ── Птица карточки ───────────────────────────────────────────────────────

DEFAULT_BIRD = 3  # 🔴 — для карточек, у которых птица ещё не задана

BIRD_EMOJI = {
    1: ("5224291536161781723", "🔵"),
    2: ("5226789141248778924", "⚪️"),
    3: ("5224255866458384390", "🔴"),
    4: ("5224309386045857033", "🟡"),
    5: ("5226870591008581314", "⚫️"),
}


def bird_html(bird: int) -> str:
    emoji_id, fallback = BIRD_EMOJI[bird]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def card_title(card: dict) -> str:
    return f"{html.escape(card['name'])} ({bird_html(card['bird'])})"


def bird_pick_kb(callback_prefix: str, back_callback: str):
    kb = InlineKeyboardBuilder()
    for code, (_, fallback) in BIRD_EMOJI.items():
        kb.button(text=fallback, callback_data=f"{callback_prefix}:{code}")
    kb.button(text="⬅️ Назад", callback_data=back_callback)
    kb.adjust(5, 1)
    return kb.as_markup()


router = Router()


# ── База данных (PostgreSQL) ────────────────────────────────────────────

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS bot_state (
        id SMALLINT PRIMARY KEY,
        admin_id BIGINT,
        last_deploy_version TEXT
    )
    """,
    "INSERT INTO bot_state (id, admin_id) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
    "ALTER TABLE bot_state ADD COLUMN IF NOT EXISTS last_deploy_version TEXT",
    """
    CREATE TABLE IF NOT EXISTS cards (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        power BIGINT NOT NULL,
        money BIGINT NOT NULL,
        photo_id TEXT NOT NULL,
        rarity SMALLINT NOT NULL DEFAULT 1,
        bird SMALLINT NOT NULL DEFAULT 3
    )
    """,
    "ALTER TABLE cards ADD COLUMN IF NOT EXISTS rarity SMALLINT NOT NULL DEFAULT 1",
    "ALTER TABLE cards ADD COLUMN IF NOT EXISTS bird SMALLINT NOT NULL DEFAULT 3",
    """
    CREATE TABLE IF NOT EXISTS class_cards (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        power BIGINT NOT NULL,
        photo_id TEXT NOT NULL,
        rarity SMALLINT NOT NULL DEFAULT 1,
        bird SMALLINT NOT NULL DEFAULT 3
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_profiles (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        power BIGINT NOT NULL DEFAULT 0,
        money BIGINT NOT NULL DEFAULT 0,
        opens INTEGER NOT NULL DEFAULT 0,
        last_open DOUBLE PRECISION NOT NULL DEFAULT 0,
        last_class DOUBLE PRECISION NOT NULL DEFAULT 0,
        last_steal DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    )
    """,
    "ALTER TABLE chat_profiles ADD COLUMN IF NOT EXISTS last_class DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE chat_profiles ADD COLUMN IF NOT EXISTS last_steal DOUBLE PRECISION NOT NULL DEFAULT 0",
    # Сила и кулдауны копилки/класса — общие показатели игрока на все чаты (лидерборд
    # при этом остаётся своим для каждого чата: ранжирует участников чата по общей силе).
    """
    CREATE TABLE IF NOT EXISTS players (
        user_id BIGINT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        power BIGINT NOT NULL DEFAULT 0,
        last_open DOUBLE PRECISION NOT NULL DEFAULT 0,
        last_class DOUBLE PRECISION NOT NULL DEFAULT 0
    )
    """,
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_open DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_class DOUBLE PRECISION NOT NULL DEFAULT 0",
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'chat_profiles' AND column_name = 'power'
        ) THEN
            INSERT INTO players (user_id, power)
            SELECT user_id, SUM(power) FROM chat_profiles GROUP BY user_id
            ON CONFLICT (user_id) DO NOTHING;
        END IF;
    END $$;
    """,
    "ALTER TABLE chat_profiles DROP COLUMN IF EXISTS power",
    """
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'chat_profiles' AND column_name = 'last_open'
        ) THEN
            INSERT INTO players (user_id, last_open)
            SELECT user_id, MAX(last_open) FROM chat_profiles GROUP BY user_id
            ON CONFLICT (user_id) DO UPDATE SET last_open = GREATEST(players.last_open, EXCLUDED.last_open);
        END IF;
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'chat_profiles' AND column_name = 'last_class'
        ) THEN
            INSERT INTO players (user_id, last_class)
            SELECT user_id, MAX(last_class) FROM chat_profiles GROUP BY user_id
            ON CONFLICT (user_id) DO UPDATE SET last_class = GREATEST(players.last_class, EXCLUDED.last_class);
        END IF;
    END $$;
    """,
    "ALTER TABLE chat_profiles DROP COLUMN IF EXISTS last_open",
    "ALTER TABLE chat_profiles DROP COLUMN IF EXISTS last_class",
    """
    CREATE TABLE IF NOT EXISTS chat_owners (
        chat_id BIGINT PRIMARY KEY,
        owner_id BIGINT NOT NULL,
        owner_name TEXT NOT NULL,
        chat_title TEXT NOT NULL,
        notified BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    "ALTER TABLE chat_owners ADD COLUMN IF NOT EXISTS owner_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chat_owners DROP COLUMN IF EXISTS earned",
    """
    CREATE TABLE IF NOT EXISTS clans (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        photo_id TEXT NOT NULL,
        creator_id BIGINT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clan_members (
        user_id BIGINT PRIMARY KEY,
        clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
        joined_at DOUBLE PRECISION NOT NULL
    )
    """,
)


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        for statement in SCHEMA_STATEMENTS:
            await conn.execute(statement)


async def get_admin_id(pool: asyncpg.Pool) -> int | None:
    return await pool.fetchval("SELECT admin_id FROM bot_state WHERE id = 1")


async def ensure_admin(pool: asyncpg.Pool, user_id: int) -> None:
    await pool.execute(
        "UPDATE bot_state SET admin_id = $1 WHERE id = 1 AND admin_id IS NULL", user_id
    )


async def get_last_deploy_version(pool: asyncpg.Pool) -> str | None:
    return await pool.fetchval("SELECT last_deploy_version FROM bot_state WHERE id = 1")


async def set_last_deploy_version(pool: asyncpg.Pool, version: str) -> None:
    await pool.execute("UPDATE bot_state SET last_deploy_version = $1 WHERE id = 1", version)


async def list_cards(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT id, name, power, money, photo_id, rarity, bird FROM cards ORDER BY id")
    return [dict(row) for row in rows]


async def cards_count(pool: asyncpg.Pool) -> int:
    return await pool.fetchval("SELECT COUNT(*) FROM cards")


async def get_card(pool: asyncpg.Pool, card_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, name, power, money, photo_id, rarity, bird FROM cards WHERE id = $1", card_id
    )
    return dict(row) if row else None


async def add_card(
    pool: asyncpg.Pool, name: str, power: int, money: int, photo_id: str, rarity: int, bird: int
) -> None:
    await pool.execute(
        "INSERT INTO cards (name, power, money, photo_id, rarity, bird) VALUES ($1, $2, $3, $4, $5, $6)",
        name, power, money, photo_id, rarity, bird,
    )


async def update_card_field(pool: asyncpg.Pool, card_id: int, field: str, value) -> None:
    if field not in CARD_FIELDS:
        raise ValueError(f"Недопустимое поле карточки: {field}")
    await pool.execute(f"UPDATE cards SET {field} = $1 WHERE id = $2", value, card_id)


async def delete_card(pool: asyncpg.Pool, card_id: int) -> str | None:
    return await pool.fetchval("DELETE FROM cards WHERE id = $1 RETURNING name", card_id)


async def list_class_cards(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT id, name, power, photo_id, rarity, bird FROM class_cards ORDER BY id")
    return [dict(row) for row in rows]


async def class_cards_count(pool: asyncpg.Pool) -> int:
    return await pool.fetchval("SELECT COUNT(*) FROM class_cards")


async def get_class_card(pool: asyncpg.Pool, card_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, name, power, photo_id, rarity, bird FROM class_cards WHERE id = $1", card_id
    )
    return dict(row) if row else None


async def add_class_card(
    pool: asyncpg.Pool, name: str, power: int, photo_id: str, rarity: int, bird: int
) -> None:
    await pool.execute(
        "INSERT INTO class_cards (name, power, photo_id, rarity, bird) VALUES ($1, $2, $3, $4, $5)",
        name, power, photo_id, rarity, bird,
    )


async def update_class_card_field(pool: asyncpg.Pool, card_id: int, field: str, value) -> None:
    if field not in CLASS_CARD_FIELDS:
        raise ValueError(f"Недопустимое поле карточки: {field}")
    await pool.execute(f"UPDATE class_cards SET {field} = $1 WHERE id = $2", value, card_id)


async def delete_class_card(pool: asyncpg.Pool, card_id: int) -> str | None:
    return await pool.fetchval("DELETE FROM class_cards WHERE id = $1 RETURNING name", card_id)


async def register_chat_owner(
    pool: asyncpg.Pool, chat_id: int, owner_id: int, owner_name: str, chat_title: str
) -> bool:
    """Возвращает True, если запись создана впервые (группа ещё не была зарегистрирована)."""
    inserted = await pool.fetchval(
        """
        INSERT INTO chat_owners (chat_id, owner_id, owner_name, chat_title)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (chat_id) DO NOTHING
        RETURNING chat_id
        """,
        chat_id, owner_id, owner_name, chat_title,
    )
    return inserted is not None


async def mark_owner_notified(pool: asyncpg.Pool, chat_id: int) -> None:
    await pool.execute("UPDATE chat_owners SET notified = TRUE WHERE chat_id = $1", chat_id)


async def get_pending_owner_chats(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT chat_id, chat_title FROM chat_owners WHERE owner_id = $1 AND notified = FALSE",
        owner_id,
    )
    return [dict(row) for row in rows]


async def get_player_summary(pool: asyncpg.Pool, user_id: int) -> dict:
    row = await pool.fetchrow(
        """
        SELECT
            COALESCE((SELECT power FROM players WHERE user_id = $1), 0) AS power,
            COALESCE(SUM(money), 0) AS money,
            COALESCE(SUM(opens), 0) AS opens,
            COUNT(*) AS chats
        FROM chat_profiles
        WHERE user_id = $1
        """,
        user_id,
    )
    return dict(row)


async def add_player_power(conn: asyncpg.Connection, user_id: int, name: str, power: int) -> None:
    await conn.execute(
        """
        INSERT INTO players (user_id, name, power) VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name, power = players.power + EXCLUDED.power
        """,
        user_id, name, power,
    )


async def ensure_player_name(conn: asyncpg.Connection, user_id: int, name: str) -> None:
    await conn.execute(
        """
        INSERT INTO players (user_id, name, power) VALUES ($1, $2, 0)
        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name
        """,
        user_id, name,
    )


async def set_player_cooldown(conn: asyncpg.Connection, user_id: int, name: str, field: str, timestamp: float) -> None:
    if field not in ("last_open", "last_class"):
        raise ValueError(f"Недопустимое поле кулдауна: {field}")
    await conn.execute(
        f"""
        INSERT INTO players (user_id, name, {field}) VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET {field} = EXCLUDED.{field}
        """,
        user_id, name, timestamp,
    )


async def get_chat_creator(bot: Bot, chat_id: int) -> tuple[int, str] | None:
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramAPIError:
        return None
    for member in admins:
        if member.status == "creator":
            return member.user.id, member.user.full_name
    return None


def owner_notify_text(chat_title: str) -> str:
    return (
        f"<b>🎉 Ты подключил Angry Копилку к группе «{html.escape(chat_title)}»!\n\n"
        "Теперь тебе будет начисляться 1% от всех монет, которые заработают игроки в этой группе.</b>"
    )


# ── Кланы ────────────────────────────────────────────────────────────────

async def get_user_clan(pool: asyncpg.Pool, user_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT c.id, c.name, c.photo_id, c.creator_id
        FROM clan_members cm
        JOIN clans c ON c.id = cm.clan_id
        WHERE cm.user_id = $1
        """,
        user_id,
    )
    return dict(row) if row else None


async def get_clan(pool: asyncpg.Pool, clan_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, name, photo_id, creator_id FROM clans WHERE id = $1", clan_id
    )
    return dict(row) if row else None


async def get_clan_by_name(pool: asyncpg.Pool, name: str) -> dict | None:
    # SQL lower() не приводит кириллицу в нижний регистр на серверах с локалью C,
    # поэтому сравниваем регистронезависимо на стороне Python.
    rows = await pool.fetch("SELECT id, name, photo_id, creator_id FROM clans")
    target = name.strip().lower()
    for row in rows:
        if row["name"].lower() == target:
            return dict(row)
    return None


async def clan_name_taken(pool: asyncpg.Pool, name: str) -> bool:
    return await get_clan_by_name(pool, name) is not None


async def get_chat_money(pool: asyncpg.Pool, chat_id: int, user_id: int) -> int:
    money = await pool.fetchval(
        "SELECT money FROM chat_profiles WHERE chat_id = $1 AND user_id = $2", chat_id, user_id
    )
    return money or 0


async def clan_member_count(pool: asyncpg.Pool, clan_id: int) -> int:
    return await pool.fetchval("SELECT COUNT(*) FROM clan_members WHERE clan_id = $1", clan_id)


async def create_clan(
    pool: asyncpg.Pool, name: str, photo_id: str, creator_id: int, creator_name: str, chat_id: int
) -> int:
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE chat_profiles SET money = money - $1 WHERE chat_id = $2 AND user_id = $3",
                CLAN_CREATE_COST, chat_id, creator_id,
            )
            await ensure_player_name(conn, creator_id, creator_name)
            clan_id = await conn.fetchval(
                "INSERT INTO clans (name, photo_id, creator_id, created_at) VALUES ($1, $2, $3, $4) RETURNING id",
                name, photo_id, creator_id, now,
            )
            await conn.execute(
                "INSERT INTO clan_members (user_id, clan_id, joined_at) VALUES ($1, $2, $3)",
                creator_id, clan_id, now,
            )
    return clan_id


async def add_clan_member(pool: asyncpg.Pool, clan_id: int, user_id: int, user_name: str) -> None:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_player_name(conn, user_id, user_name)
            await conn.execute(
                "INSERT INTO clan_members (user_id, clan_id, joined_at) VALUES ($1, $2, $3)",
                user_id, clan_id, time.time(),
            )


async def remove_clan_member(pool: asyncpg.Pool, user_id: int) -> None:
    await pool.execute("DELETE FROM clan_members WHERE user_id = $1", user_id)


async def delete_clan(pool: asyncpg.Pool, clan_id: int) -> None:
    await pool.execute("DELETE FROM clans WHERE id = $1", clan_id)


async def get_clan_members(pool: asyncpg.Pool, clan_id: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT cm.user_id, COALESCE(p.name, '') AS name, COALESCE(p.power, 0) AS power
        FROM clan_members cm
        LEFT JOIN players p ON p.user_id = cm.user_id
        WHERE cm.clan_id = $1
        ORDER BY power DESC
        """,
        clan_id,
    )
    return [dict(row) for row in rows]


async def get_clan_top(pool: asyncpg.Pool, limit: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT c.name, COUNT(cm.user_id) AS members, COALESCE(SUM(p.power), 0) AS total_power
        FROM clans c
        LEFT JOIN clan_members cm ON cm.clan_id = c.id
        LEFT JOIN players p ON p.user_id = cm.user_id
        GROUP BY c.id
        ORDER BY total_power DESC
        LIMIT $1
        """,
        limit,
    )
    return [dict(row) for row in rows]


# ── Админ ────────────────────────────────────────────────────────────────

class AdminAssignMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        pool = data.get("pool")
        if pool is not None and event.from_user is not None:
            await ensure_admin(pool, event.from_user.id)
        return await handler(event, data)


class OwnerNotifyMiddleware(BaseMiddleware):
    """Досылает создателю группы отложенное уведомление о 1%, когда он сам пишет боту в ЛС."""

    async def __call__(self, handler, event: Message, data):
        pool = data.get("pool")
        bot = data.get("bot")
        if pool is not None and bot is not None and event.chat.type == "private" and event.from_user is not None:
            for chat in await get_pending_owner_chats(pool, event.from_user.id):
                try:
                    await bot.send_message(event.from_user.id, owner_notify_text(chat["chat_title"]))
                except TelegramForbiddenError:
                    continue
                await mark_owner_notified(pool, chat["chat_id"])
        return await handler(event, data)


class IsAdminPrivate(BaseFilter):
    async def __call__(self, event: TelegramObject, pool: asyncpg.Pool) -> bool:
        message = event.message if isinstance(event, CallbackQuery) else event
        if message.chat.type != "private" or event.from_user is None:
            return False
        admin_id = await get_admin_id(pool)
        return admin_id == event.from_user.id


# ── FSM ──────────────────────────────────────────────────────────────────

class AddCard(StatesGroup):
    photo = State()
    name = State()
    power = State()
    money = State()
    rarity = State()
    bird = State()


class EditCard(StatesGroup):
    waiting_value = State()


class AddClassCard(StatesGroup):
    photo = State()
    name = State()
    power = State()
    rarity = State()
    bird = State()


class EditClassCard(StatesGroup):
    waiting_value = State()


class CreateClan(StatesGroup):
    waiting_photo = State()


# ── Клавиатуры ───────────────────────────────────────────────────────────

def admin_menu_text() -> str:
    return "<b>⚙️ Админ-панель</b>"


def admin_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🐷 Копилка", callback_data="piggy:menu")
    kb.button(text="🎓 Класс", callback_data="class:menu")
    kb.adjust(1)
    return kb.as_markup()


def piggy_menu_text(cards_total: int) -> str:
    return f"<b>🐷 Копилка\n\nКарточек: {cards_total}</b>"


def piggy_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список карточек", callback_data="piggy:list")
    kb.button(text="➕ Добавить карточку", callback_data="piggy:add")
    kb.button(text="✏️ Изменить карточку", callback_data="piggy:edit")
    kb.button(text="🗑 Удалить карточку", callback_data="piggy:remove")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def cancel_kb(callback_data: str = "piggy:cancel"):
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=callback_data)
    return kb.as_markup()


def cards_pick_kb(cards: list, action: str, namespace: str = "piggy"):
    kb = InlineKeyboardBuilder()
    for card in cards:
        kb.button(text=card["name"], callback_data=f"{namespace}:{action}:{card['id']}")
    kb.button(text="⬅️ Назад", callback_data=f"{namespace}:menu")
    kb.adjust(1)
    return kb.as_markup()


def edit_fields_kb(card_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Фото", callback_data=f"piggy:editfield:{card_id}:photo_id")
    kb.button(text="✏️ Название", callback_data=f"piggy:editfield:{card_id}:name")
    kb.button(text="⚔️ Сила", callback_data=f"piggy:editfield:{card_id}:power")
    kb.button(text="💰 Деньги", callback_data=f"piggy:editfield:{card_id}:money")
    kb.button(text="⭐ Звёзды", callback_data=f"piggy:editfield:{card_id}:rarity")
    kb.button(text="🐦 Птица", callback_data=f"piggy:editfield:{card_id}:bird")
    kb.button(text="⬅️ Назад", callback_data="piggy:edit")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def class_menu_text(cards_total: int) -> str:
    return f"<b>🎓 Класс\n\nКарточек: {cards_total}</b>"


def class_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список карточек", callback_data="class:list")
    kb.button(text="➕ Добавить карточку", callback_data="class:add")
    kb.button(text="✏️ Изменить карточку", callback_data="class:edit")
    kb.button(text="🗑 Удалить карточку", callback_data="class:remove")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def class_edit_fields_kb(card_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Фото", callback_data=f"class:editfield:{card_id}:photo_id")
    kb.button(text="✏️ Название", callback_data=f"class:editfield:{card_id}:name")
    kb.button(text="⚔️ Сила", callback_data=f"class:editfield:{card_id}:power")
    kb.button(text="⭐ Звёзды", callback_data=f"class:editfield:{card_id}:rarity")
    kb.button(text="🐦 Птица", callback_data=f"class:editfield:{card_id}:bird")
    kb.button(text="⬅️ Назад", callback_data="class:edit")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def group_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎰 Открыть копилку", callback_data="group:open")
    kb.button(text="🎓 Крутить класс", callback_data="group:class")
    kb.button(text="🏆 Топ силы", callback_data="group:top")
    kb.adjust(1)
    return kb.as_markup()


def card_caption(card: dict, prefix: str) -> str:
    return (
        f"<b>{prefix}\n\n"
        f"🃏 {card_title(card)}</b>\n"
        f"{rarity_display(card['rarity'])}\n"
        f"<b>⚔️ Сила: {card['power']}\n"
        f"💰 Денег: {card['money']}</b>"
    )


def class_card_caption(card: dict, prefix: str) -> str:
    return (
        f"<b>{prefix}\n\n"
        f"🃏 {card_title(card)}</b>\n"
        f"{rarity_display(card['rarity'])}\n"
        f"<b>⚔️ Сила: {card['power']}</b>"
    )


# ── Игровая логика ───────────────────────────────────────────────────────

async def perform_open(pool: asyncpg.Pool, chat_id: int, user) -> tuple[str | None, str]:
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            card = await conn.fetchrow(
                "SELECT name, power, money, photo_id, rarity, bird FROM cards ORDER BY random() LIMIT 1"
            )
            if card is None:
                return None, "<b>🕳 Копилка пока пуста — админ ещё не добавил карточки.</b>"

            player_row = await conn.fetchrow(
                "SELECT last_open FROM players WHERE user_id = $1 FOR UPDATE", user.id
            )
            last_open = player_row["last_open"] if player_row else 0
            remaining = COOLDOWN_SECONDS - (now - last_open)
            if remaining > 0:
                minutes, seconds = divmod(int(remaining), 60)
                return None, f"<b>⏳ Копилка ещё не наполнилась. Попробуй через {minutes} мин {seconds} сек.</b>"

            await conn.execute(
                """
                INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
                VALUES ($1, $2, $3, $4, 1)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    money = chat_profiles.money + EXCLUDED.money,
                    opens = chat_profiles.opens + 1
                """,
                chat_id, user.id, user.full_name, card["money"],
            )
            await add_player_power(conn, user.id, user.full_name, card["power"])
            await set_player_cooldown(conn, user.id, user.full_name, "last_open", now)

            # Реферальные 1% владельцу группы — прибавляются прямо к его основным
            # деньгам (не отдельным счётчиком), молча, без уведомлений.
            referral_amount = round(card["money"] * 0.01)
            if referral_amount > 0:
                await conn.execute(
                    """
                    INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
                    SELECT $2, o.owner_id, o.owner_name, $1, 0
                    FROM chat_owners o
                    WHERE o.chat_id = $2
                    ON CONFLICT (chat_id, user_id) DO UPDATE SET
                        money = chat_profiles.money + EXCLUDED.money
                    """,
                    referral_amount, chat_id,
                )

    caption = (
        f"<b>🎉 <a href='tg://user?id={user.id}'>{html.escape(user.full_name)}</a> крутит копилку!\n\n"
        f"🃏 {card_title(card)}</b>\n"
        f"{rarity_display(card['rarity'])}\n\n"
        f"<b>⚔️ +{card['power']} силы\n"
        f"💰 +{card['money']} монет</b>"
    )
    return card["photo_id"], caption


async def perform_class(pool: asyncpg.Pool, chat_id: int, user) -> tuple[str | None, str]:
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            card = await conn.fetchrow(
                "SELECT name, power, photo_id, rarity, bird FROM class_cards ORDER BY random() LIMIT 1"
            )
            if card is None:
                return None, "<b>🕳 Класс пока пуст — админ ещё не добавил карточки.</b>"

            profile = await conn.fetchrow(
                "SELECT money FROM chat_profiles WHERE chat_id = $1 AND user_id = $2 FOR UPDATE",
                chat_id, user.id,
            )
            balance = profile["money"] if profile else 0

            player_row = await conn.fetchrow(
                "SELECT last_class FROM players WHERE user_id = $1 FOR UPDATE", user.id
            )
            last_class = player_row["last_class"] if player_row else 0

            remaining = CLASS_COOLDOWN_SECONDS - (now - last_class)
            if remaining > 0:
                minutes, seconds = divmod(int(remaining), 60)
                return None, f"<b>⏳ Класс ещё не готов. Попробуй через {minutes} мин {seconds} сек.</b>"

            if balance < CLASS_SPIN_COST:
                return None, f"<b>💰 Не хватает монет. Нужно {CLASS_SPIN_COST}, у тебя {balance}.</b>"

            await conn.execute(
                """
                INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
                VALUES ($1, $2, $3, $4, 0)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    money = chat_profiles.money + EXCLUDED.money
                """,
                chat_id, user.id, user.full_name, -CLASS_SPIN_COST,
            )
            await add_player_power(conn, user.id, user.full_name, card["power"])
            await set_player_cooldown(conn, user.id, user.full_name, "last_class", now)

    caption = (
        f"<b>🎓 <a href='tg://user?id={user.id}'>{html.escape(user.full_name)}</a> крутит класс!\n\n"
        f"🃏 {card_title(card)}</b>\n"
        f"{rarity_display(card['rarity'])}\n\n"
        f"<b>⚔️ +{card['power']} силы\n"
        f"💰 −{CLASS_SPIN_COST} монет</b>"
    )
    return card["photo_id"], caption


async def perform_steal(pool: asyncpg.Pool, chat_id: int, thief, target) -> str:
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            thief_row = await conn.fetchrow(
                "SELECT money, last_steal FROM chat_profiles WHERE chat_id = $1 AND user_id = $2 FOR UPDATE",
                chat_id, thief.id,
            )
            thief_money = thief_row["money"] if thief_row else 0
            last_steal = thief_row["last_steal"] if thief_row else 0

            remaining = STEAL_COOLDOWN_SECONDS - (now - last_steal)
            if remaining > 0:
                minutes, seconds = divmod(int(remaining), 60)
                return f"<b>⏳ Кража ещё не готова. Попробуй через {minutes} мин {seconds} сек.</b>"

            target_row = await conn.fetchrow(
                "SELECT money FROM chat_profiles WHERE chat_id = $1 AND user_id = $2 FOR UPDATE",
                chat_id, target.id,
            )
            target_money = target_row["money"] if target_row else 0
            if target_money <= 0:
                return "<b>🕳 У этого игрока нечего воровать.</b>"

            await conn.execute(
                """
                INSERT INTO chat_profiles (chat_id, user_id, name, money, opens, last_steal)
                VALUES ($1, $2, $3, 0, 0, $4)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    last_steal = EXCLUDED.last_steal
                """,
                chat_id, thief.id, thief.full_name, now,
            )

            outcome = random.choices(
                [code for code, _ in STEAL_OUTCOME_WEIGHTS],
                weights=[weight for _, weight in STEAL_OUTCOME_WEIGHTS],
                k=1,
            )[0]

            thief_mention = f"<a href='tg://user?id={thief.id}'>{html.escape(thief.full_name)}</a>"
            target_mention = f"<a href='tg://user?id={target.id}'>{html.escape(target.full_name)}</a>"

            if outcome in ("success", "partial"):
                share = STEAL_SUCCESS_SHARE if outcome == "success" else STEAL_PARTIAL_SHARE
                amount = round(target_money * share)
                await conn.execute(
                    "UPDATE chat_profiles SET money = money - $1 WHERE chat_id = $2 AND user_id = $3",
                    amount, chat_id, target.id,
                )
                await conn.execute(
                    "UPDATE chat_profiles SET money = money + $1 WHERE chat_id = $2 AND user_id = $3",
                    amount, chat_id, thief.id,
                )
                if outcome == "success":
                    return (
                        f"<b>🥷 Удачная кража!\n\n"
                        f"{thief_mention} обчистил {target_mention}\n"
                        f"💰 Украдено: {amount}</b>"
                    )
                return (
                    f"<b>🤏 Частично удачная кража!\n\n"
                    f"{thief_mention} стащил немного у {target_mention}\n"
                    f"💰 Украдено: {amount}</b>"
                )

            loss = round(thief_money * STEAL_FAIL_LOSS_SHARE)
            if loss > 0:
                await conn.execute(
                    "UPDATE chat_profiles SET money = money - $1 WHERE chat_id = $2 AND user_id = $3",
                    loss, chat_id, thief.id,
                )
            return (
                f"<b>🚨 Неудачная кража!\n\n"
                f"{thief_mention} попался, пытаясь обокрасть {target_mention}\n"
                f"💰 Потеряно: {loss}</b>"
            )


async def perform_top(pool: asyncpg.Pool, chat_id: int) -> str:
    rows = await pool.fetch(
        """
        SELECT cp.name, pl.power
        FROM chat_profiles cp
        JOIN players pl ON pl.user_id = cp.user_id
        WHERE cp.chat_id = $1
        ORDER BY pl.power DESC
        LIMIT $2
        """,
        chat_id, TOP_LIMIT,
    )
    if not rows:
        return "<b>📭 Пока никто не крутил копилку в этом чате.</b>"

    medals = ["🥇", "🥈", "🥉"]
    lines = ["<b>🏆 Топ силы чата", "━━━━━━━━━━━━━━", ""]
    for i, row in enumerate(rows):
        name = html.escape(row["name"])
        if i < len(medals):
            lines.append(f"{medals[i]} {name}  ⚔️ {row['power']}")
        else:
            lines.append(f"{i + 1}.  {name}  ⚔️ {row['power']}")
    return "\n".join(lines) + "</b>"


async def safe_edit_text(message: Message, text: str, reply_markup=None) -> None:
    """edit_text не может превратить фото-сообщение в текстовое — в этом случае
    пересоздаём сообщение вместо падения (отсюда были нерабочие кнопки)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.delete()
        await message.answer(text, reply_markup=reply_markup)


# ── Приватные сообщения (админ-панель) ───────────────────────────────────

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start_private(message: Message, state: FSMContext, pool: asyncpg.Pool):
    admin_id = await get_admin_id(pool)
    if message.from_user.id == admin_id:
        await state.clear()
        await message.answer(admin_menu_text(), reply_markup=admin_menu_kb())
    else:
        await message.answer(NON_ADMIN_PRIVATE_TEXT)


@router.callback_query(F.data == "admin:menu", IsAdminPrivate())
async def admin_menu_cb(callback: CallbackQuery):
    await safe_edit_text(callback.message, admin_menu_text(), reply_markup=admin_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "piggy:menu", IsAdminPrivate())
async def piggy_menu_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    total = await cards_count(pool)
    await safe_edit_text(callback.message, piggy_menu_text(total), reply_markup=piggy_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "piggy:cancel", IsAdminPrivate())
async def piggy_cancel(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool):
    await state.clear()
    total = await cards_count(pool)
    await safe_edit_text(callback.message, piggy_menu_text(total), reply_markup=piggy_menu_kb())
    await callback.answer("Отменено")


# — Список —

@router.callback_query(F.data == "piggy:list", IsAdminPrivate())
async def piggy_list_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    cards = await list_cards(pool)
    if not cards:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    lines = ["<b>📋 Список карточек</b>", ""]
    for i, card in enumerate(cards, start=1):
        lines.append(
            f"<b>{i}. {card_title(card)}</b> — {rarity_display(card['rarity'])}\n"
            f"<b>    ⚔️{card['power']} · 💰{card['money']}</b>"
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="piggy:menu")
    await safe_edit_text(callback.message, "\n".join(lines), reply_markup=kb.as_markup())
    await callback.answer()


# — Добавление —

@router.callback_query(F.data == "piggy:add", IsAdminPrivate())
async def piggy_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddCard.photo)
    await safe_edit_text(callback.message, "<b>📸 Пришли фото карточки</b>", reply_markup=cancel_kb())
    await callback.answer()


@router.message(AddCard.photo, IsAdminPrivate())
async def add_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("<b>Пришли именно фото 🙂</b>")
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCard.name)
    await message.answer("<b>✏️ Введи название карточки</b>", reply_markup=cancel_kb())


@router.message(AddCard.name, IsAdminPrivate())
async def add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("<b>Название не может быть пустым, попробуй ещё раз.</b>")
        return
    await state.update_data(name=name)
    await state.set_state(AddCard.power)
    await message.answer("<b>⚔️ Введи силу карточки (число)</b>", reply_markup=cancel_kb())


@router.message(AddCard.power, IsAdminPrivate())
async def add_power(message: Message, state: FSMContext):
    try:
        power = int((message.text or "").strip())
    except ValueError:
        await message.answer("<b>Нужно целое число, попробуй ещё раз.</b>")
        return
    await state.update_data(power=power)
    await state.set_state(AddCard.money)
    await message.answer("<b>💰 Введи количество денег (число)</b>", reply_markup=cancel_kb())


@router.message(AddCard.money, IsAdminPrivate())
async def add_money(message: Message, state: FSMContext, pool: asyncpg.Pool):
    try:
        money = int((message.text or "").strip())
    except ValueError:
        await message.answer("<b>Нужно целое число, попробуй ещё раз.</b>")
        return

    await state.update_data(money=money)
    await state.set_state(AddCard.rarity)
    await message.answer(
        "<b>⭐ Выбери количество звёзд карточки:</b>",
        reply_markup=rarity_pick_kb("piggy:addrarity", "piggy:cancel"),
    )


@router.callback_query(AddCard.rarity, F.data.startswith("piggy:addrarity:"), IsAdminPrivate())
async def add_rarity_pick(callback: CallbackQuery, state: FSMContext):
    rarity = int(callback.data.split(":")[2])
    await state.update_data(rarity=rarity)
    await state.set_state(AddCard.bird)
    await safe_edit_text(callback.message, 
        "<b>🐦 Выбери птицу карточки:</b>",
        reply_markup=bird_pick_kb("piggy:addbird", "piggy:cancel"),
    )
    await callback.answer()


@router.callback_query(AddCard.bird, F.data.startswith("piggy:addbird:"), IsAdminPrivate())
async def add_bird_pick(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool):
    bird = int(callback.data.split(":")[2])
    data = await state.get_data()
    await add_card(pool, data["name"], data["power"], data["money"], data["photo_id"], data["rarity"], bird)
    await state.clear()

    card = {
        "name": data["name"],
        "power": data["power"],
        "money": data["money"],
        "rarity": data["rarity"],
        "bird": bird,
    }
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer_photo(data["photo_id"], caption=card_caption(card, "✅ Карточка добавлена!"))
    total = await cards_count(pool)
    await callback.message.answer(piggy_menu_text(total), reply_markup=piggy_menu_kb())
    await callback.answer()


# — Изменение —

@router.callback_query(F.data == "piggy:edit", IsAdminPrivate())
async def piggy_edit_list(callback: CallbackQuery, pool: asyncpg.Pool):
    cards = await list_cards(pool)
    if not cards:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    await safe_edit_text(callback.message, 
        "<b>✏️ Выбери карточку для изменения:</b>", reply_markup=cards_pick_kb(cards, "edit")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("piggy:edit:"), IsAdminPrivate())
async def piggy_edit_pick(callback: CallbackQuery, pool: asyncpg.Pool):
    card_id = int(callback.data.split(":")[2])
    card = await get_card(pool, card_id)
    if not card:
        await callback.answer("Карточка не найдена", show_alert=True)
        return
    await safe_edit_text(callback.message, card_caption(card, "Что изменить?"), reply_markup=edit_fields_kb(card_id))
    await callback.answer()


@router.callback_query(F.data.startswith("piggy:editfield:"), IsAdminPrivate())
async def piggy_editfield_start(callback: CallbackQuery, state: FSMContext):
    _, _, card_id, field = callback.data.split(":")
    if field == "rarity":
        await safe_edit_text(callback.message, 
            "<b>⭐ Выбери количество звёзд:</b>",
            reply_markup=rarity_pick_kb(f"piggy:setrarity:{card_id}", f"piggy:edit:{card_id}"),
        )
        await callback.answer()
        return
    if field == "bird":
        await safe_edit_text(callback.message, 
            "<b>🐦 Выбери птицу:</b>",
            reply_markup=bird_pick_kb(f"piggy:setbird:{card_id}", f"piggy:edit:{card_id}"),
        )
        await callback.answer()
        return
    await state.set_state(EditCard.waiting_value)
    await state.update_data(card_id=int(card_id), field=field)
    await safe_edit_text(callback.message, FIELD_PROMPTS[field], reply_markup=cancel_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("piggy:setrarity:"), IsAdminPrivate())
async def piggy_set_rarity(callback: CallbackQuery, pool: asyncpg.Pool):
    _, _, card_id, rarity = callback.data.split(":")
    await update_card_field(pool, int(card_id), "rarity", int(rarity))

    card = await get_card(pool, int(card_id))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer_photo(
        card["photo_id"],
        caption=card_caption(card, "✅ Карточка обновлена!"),
        reply_markup=edit_fields_kb(int(card_id)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("piggy:setbird:"), IsAdminPrivate())
async def piggy_set_bird(callback: CallbackQuery, pool: asyncpg.Pool):
    _, _, card_id, bird = callback.data.split(":")
    await update_card_field(pool, int(card_id), "bird", int(bird))

    card = await get_card(pool, int(card_id))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer_photo(
        card["photo_id"],
        caption=card_caption(card, "✅ Карточка обновлена!"),
        reply_markup=edit_fields_kb(int(card_id)),
    )
    await callback.answer()


@router.message(EditCard.waiting_value, IsAdminPrivate())
async def piggy_editfield_value(message: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    field = data["field"]
    card_id = data["card_id"]

    if field == "photo_id":
        if not message.photo:
            await message.answer("<b>Пришли именно фото 🙂</b>")
            return
        value = message.photo[-1].file_id
    elif field in ("power", "money"):
        try:
            value = int((message.text or "").strip())
        except ValueError:
            await message.answer("<b>Нужно целое число, попробуй ещё раз.</b>")
            return
    else:
        value = (message.text or "").strip()
        if not value:
            await message.answer("<b>Название не может быть пустым, попробуй ещё раз.</b>")
            return

    await update_card_field(pool, card_id, field, value)
    await state.clear()

    card = await get_card(pool, card_id)
    await message.answer_photo(
        card["photo_id"],
        caption=card_caption(card, "✅ Карточка обновлена!"),
        reply_markup=edit_fields_kb(card_id),
    )


# — Удаление —

@router.callback_query(F.data == "piggy:remove", IsAdminPrivate())
async def piggy_remove_list(callback: CallbackQuery, pool: asyncpg.Pool):
    cards = await list_cards(pool)
    if not cards:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    await safe_edit_text(callback.message, 
        "<b>🗑 Выбери карточку для удаления:</b>", reply_markup=cards_pick_kb(cards, "remove")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("piggy:remove:"), IsAdminPrivate())
async def piggy_remove_pick(callback: CallbackQuery, pool: asyncpg.Pool):
    card_id = int(callback.data.split(":")[2])
    name = await delete_card(pool, card_id)
    await callback.answer(f"Удалено: {name}" if name else "Уже удалено")

    cards = await list_cards(pool)
    if cards:
        await safe_edit_text(callback.message, 
            "<b>🗑 Выбери карточку для удаления:</b>", reply_markup=cards_pick_kb(cards, "remove")
        )
    else:
        await safe_edit_text(callback.message, piggy_menu_text(0), reply_markup=piggy_menu_kb())


# — Класс (та же структура, что копилка, только без денег) —

@router.callback_query(F.data == "class:menu", IsAdminPrivate())
async def class_menu_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    total = await class_cards_count(pool)
    await safe_edit_text(callback.message, class_menu_text(total), reply_markup=class_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "class:cancel", IsAdminPrivate())
async def class_cancel(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool):
    await state.clear()
    total = await class_cards_count(pool)
    await safe_edit_text(callback.message, class_menu_text(total), reply_markup=class_menu_kb())
    await callback.answer("Отменено")


@router.callback_query(F.data == "class:list", IsAdminPrivate())
async def class_list_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    cards = await list_class_cards(pool)
    if not cards:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    lines = ["<b>📋 Список карточек</b>", ""]
    for i, card in enumerate(cards, start=1):
        lines.append(f"<b>{i}. {card_title(card)}</b> — {rarity_display(card['rarity'])}\n<b>    ⚔️{card['power']}</b>")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="class:menu")
    await safe_edit_text(callback.message, "\n".join(lines), reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "class:add", IsAdminPrivate())
async def class_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddClassCard.photo)
    await safe_edit_text(callback.message, "<b>📸 Пришли фото карточки</b>", reply_markup=cancel_kb("class:cancel"))
    await callback.answer()


@router.message(AddClassCard.photo, IsAdminPrivate())
async def class_add_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("<b>Пришли именно фото 🙂</b>")
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddClassCard.name)
    await message.answer("<b>✏️ Введи название карточки</b>", reply_markup=cancel_kb("class:cancel"))


@router.message(AddClassCard.name, IsAdminPrivate())
async def class_add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("<b>Название не может быть пустым, попробуй ещё раз.</b>")
        return
    await state.update_data(name=name)
    await state.set_state(AddClassCard.power)
    await message.answer("<b>⚔️ Введи силу карточки (число)</b>", reply_markup=cancel_kb("class:cancel"))


@router.message(AddClassCard.power, IsAdminPrivate())
async def class_add_power(message: Message, state: FSMContext):
    try:
        power = int((message.text or "").strip())
    except ValueError:
        await message.answer("<b>Нужно целое число, попробуй ещё раз.</b>")
        return
    await state.update_data(power=power)
    await state.set_state(AddClassCard.rarity)
    await message.answer(
        "<b>⭐ Выбери количество звёзд карточки:</b>",
        reply_markup=rarity_pick_kb("class:addrarity", "class:cancel"),
    )


@router.callback_query(AddClassCard.rarity, F.data.startswith("class:addrarity:"), IsAdminPrivate())
async def class_add_rarity_pick(callback: CallbackQuery, state: FSMContext):
    rarity = int(callback.data.split(":")[2])
    await state.update_data(rarity=rarity)
    await state.set_state(AddClassCard.bird)
    await safe_edit_text(callback.message, 
        "<b>🐦 Выбери птицу карточки:</b>",
        reply_markup=bird_pick_kb("class:addbird", "class:cancel"),
    )
    await callback.answer()


@router.callback_query(AddClassCard.bird, F.data.startswith("class:addbird:"), IsAdminPrivate())
async def class_add_bird_pick(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool):
    bird = int(callback.data.split(":")[2])
    data = await state.get_data()
    await add_class_card(pool, data["name"], data["power"], data["photo_id"], data["rarity"], bird)
    await state.clear()

    card = {"name": data["name"], "power": data["power"], "rarity": data["rarity"], "bird": bird}
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer_photo(data["photo_id"], caption=class_card_caption(card, "✅ Карточка добавлена!"))
    total = await class_cards_count(pool)
    await callback.message.answer(class_menu_text(total), reply_markup=class_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "class:edit", IsAdminPrivate())
async def class_edit_list(callback: CallbackQuery, pool: asyncpg.Pool):
    cards = await list_class_cards(pool)
    if not cards:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    await safe_edit_text(callback.message, 
        "<b>✏️ Выбери карточку для изменения:</b>", reply_markup=cards_pick_kb(cards, "edit", "class")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("class:edit:"), IsAdminPrivate())
async def class_edit_pick(callback: CallbackQuery, pool: asyncpg.Pool):
    card_id = int(callback.data.split(":")[2])
    card = await get_class_card(pool, card_id)
    if not card:
        await callback.answer("Карточка не найдена", show_alert=True)
        return
    await safe_edit_text(callback.message, class_card_caption(card, "Что изменить?"), reply_markup=class_edit_fields_kb(card_id))
    await callback.answer()


@router.callback_query(F.data.startswith("class:editfield:"), IsAdminPrivate())
async def class_editfield_start(callback: CallbackQuery, state: FSMContext):
    _, _, card_id, field = callback.data.split(":")
    if field == "rarity":
        await safe_edit_text(callback.message, 
            "<b>⭐ Выбери количество звёзд:</b>",
            reply_markup=rarity_pick_kb(f"class:setrarity:{card_id}", f"class:edit:{card_id}"),
        )
        await callback.answer()
        return
    if field == "bird":
        await safe_edit_text(callback.message, 
            "<b>🐦 Выбери птицу:</b>",
            reply_markup=bird_pick_kb(f"class:setbird:{card_id}", f"class:edit:{card_id}"),
        )
        await callback.answer()
        return
    await state.set_state(EditClassCard.waiting_value)
    await state.update_data(card_id=int(card_id), field=field)
    await safe_edit_text(callback.message, CLASS_FIELD_PROMPTS[field], reply_markup=cancel_kb("class:cancel"))
    await callback.answer()


@router.callback_query(F.data.startswith("class:setrarity:"), IsAdminPrivate())
async def class_set_rarity(callback: CallbackQuery, pool: asyncpg.Pool):
    _, _, card_id, rarity = callback.data.split(":")
    await update_class_card_field(pool, int(card_id), "rarity", int(rarity))

    card = await get_class_card(pool, int(card_id))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer_photo(
        card["photo_id"],
        caption=class_card_caption(card, "✅ Карточка обновлена!"),
        reply_markup=class_edit_fields_kb(int(card_id)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("class:setbird:"), IsAdminPrivate())
async def class_set_bird(callback: CallbackQuery, pool: asyncpg.Pool):
    _, _, card_id, bird = callback.data.split(":")
    await update_class_card_field(pool, int(card_id), "bird", int(bird))

    card = await get_class_card(pool, int(card_id))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer_photo(
        card["photo_id"],
        caption=class_card_caption(card, "✅ Карточка обновлена!"),
        reply_markup=class_edit_fields_kb(int(card_id)),
    )
    await callback.answer()


@router.message(EditClassCard.waiting_value, IsAdminPrivate())
async def class_editfield_value(message: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    field = data["field"]
    card_id = data["card_id"]

    if field == "photo_id":
        if not message.photo:
            await message.answer("<b>Пришли именно фото 🙂</b>")
            return
        value = message.photo[-1].file_id
    elif field == "power":
        try:
            value = int((message.text or "").strip())
        except ValueError:
            await message.answer("<b>Нужно целое число, попробуй ещё раз.</b>")
            return
    else:
        value = (message.text or "").strip()
        if not value:
            await message.answer("<b>Название не может быть пустым, попробуй ещё раз.</b>")
            return

    await update_class_card_field(pool, card_id, field, value)
    await state.clear()

    card = await get_class_card(pool, card_id)
    await message.answer_photo(
        card["photo_id"],
        caption=class_card_caption(card, "✅ Карточка обновлена!"),
        reply_markup=class_edit_fields_kb(card_id),
    )


@router.callback_query(F.data == "class:remove", IsAdminPrivate())
async def class_remove_list(callback: CallbackQuery, pool: asyncpg.Pool):
    cards = await list_class_cards(pool)
    if not cards:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    await safe_edit_text(callback.message, 
        "<b>🗑 Выбери карточку для удаления:</b>", reply_markup=cards_pick_kb(cards, "remove", "class")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("class:remove:"), IsAdminPrivate())
async def class_remove_pick(callback: CallbackQuery, pool: asyncpg.Pool):
    card_id = int(callback.data.split(":")[2])
    name = await delete_class_card(pool, card_id)
    await callback.answer(f"Удалено: {name}" if name else "Уже удалено")

    cards = await list_class_cards(pool)
    if cards:
        await safe_edit_text(callback.message, 
            "<b>🗑 Выбери карточку для удаления:</b>", reply_markup=cards_pick_kb(cards, "remove", "class")
        )
    else:
        await safe_edit_text(callback.message, class_menu_text(0), reply_markup=class_menu_kb())


# — Профиль игрока —

def profile_text(user, summary: dict) -> str:
    if summary["chats"] == 0:
        return (
            "<b>👤 Профиль\n\n"
            "Ты пока не крутил копилку ни в одной группе.\n"
            "Добавь бота в чат и используй там 🎰 /AngryOpen!</b>"
        )

    lines = [
        f"👤 Профиль — {html.escape(user.full_name)}",
        "",
        f"⚔️ Сила (всего): {summary['power']}",
        f"💰 Монет (всего): {summary['money']}",
        f"🎰 Круток копилки: {summary['opens']}",
        f"👥 Групп с игрой: {summary['chats']}",
    ]
    return "<b>" + "\n".join(lines) + "</b>"


@router.message(F.chat.type == "private")
async def show_profile(message: Message, pool: asyncpg.Pool):
    summary = await get_player_summary(pool, message.from_user.id)
    await message.answer(profile_text(message.from_user, summary))


# ── Групповые команды ─────────────────────────────────────────────────────

GROUP_CHATS = F.chat.type.in_({"group", "supergroup"})


@router.message(CommandStart(), GROUP_CHATS)
async def cmd_start_group(message: Message):
    await message.answer(GROUP_INTRO_TEXT, reply_markup=group_menu_kb())


@router.message(Command("angryopen", ignore_case=True), GROUP_CHATS)
async def cmd_angry_open(message: Message, bot: Bot, pool: asyncpg.Pool):
    photo_id, text = await perform_open(pool, message.chat.id, message.from_user)
    if photo_id:
        await bot.send_photo(message.chat.id, photo_id, caption=text)
    else:
        await message.answer(text)


@router.message(Command("angryclass", ignore_case=True), GROUP_CHATS)
async def cmd_angry_class(message: Message, bot: Bot, pool: asyncpg.Pool):
    photo_id, text = await perform_class(pool, message.chat.id, message.from_user)
    if photo_id:
        await bot.send_photo(message.chat.id, photo_id, caption=text)
    else:
        await message.answer(text)


@router.message(Command("angrytop", ignore_case=True), GROUP_CHATS)
async def cmd_angry_top(message: Message, pool: asyncpg.Pool):
    await message.answer(await perform_top(pool, message.chat.id))


async def handle_steal(message: Message, pool: asyncpg.Pool) -> None:
    reply = message.reply_to_message
    if reply is None or reply.from_user is None:
        await message.answer(
            "<b>🕵️ Чтобы украсть монеты, ответь этой командой на сообщение игрока.</b>"
        )
        return
    target = reply.from_user
    if target.is_bot:
        await message.answer("<b>🤖 У ботов красть нечего.</b>")
        return
    if target.id == message.from_user.id:
        await message.answer("<b>🙃 Нельзя красть у самого себя.</b>")
        return
    await message.answer(await perform_steal(pool, message.chat.id, message.from_user, target))


@router.message(Command("steal", ignore_case=True), GROUP_CHATS)
async def cmd_steal(message: Message, pool: asyncpg.Pool):
    await handle_steal(message, pool)


@router.message(GROUP_CHATS, F.reply_to_message, F.text.func(lambda t: (t or "").strip().lower() == "кража"))
async def cmd_steal_word(message: Message, pool: asyncpg.Pool):
    await handle_steal(message, pool)


@router.callback_query(F.data == "group:open", GROUP_CHATS)
async def cb_group_open(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool):
    photo_id, text = await perform_open(pool, callback.message.chat.id, callback.from_user)
    if photo_id:
        await bot.send_photo(callback.message.chat.id, photo_id, caption=text)
    else:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "group:class", GROUP_CHATS)
async def cb_group_class(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool):
    photo_id, text = await perform_class(pool, callback.message.chat.id, callback.from_user)
    if photo_id:
        await bot.send_photo(callback.message.chat.id, photo_id, caption=text)
    else:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "group:top", GROUP_CHATS)
async def cb_group_top(callback: CallbackQuery, pool: asyncpg.Pool):
    await callback.message.answer(await perform_top(pool, callback.message.chat.id))
    await callback.answer()


# ── Кланы ─────────────────────────────────────────────────────────────────

CLAN_NAME_MAX_LEN = 40
_pending_clan_delete: dict[int, float] = {}


@router.message(Command("clancreate", ignore_case=True), GROUP_CHATS)
async def cmd_clan_create(message: Message, command: CommandObject, state: FSMContext, pool: asyncpg.Pool):
    name = (command.args or "").strip()
    if not name:
        await message.answer("<b>✏️ Укажи название клана: /clancreate Название</b>")
        return
    if len(name) > CLAN_NAME_MAX_LEN:
        await message.answer(f"<b>Слишком длинное название (максимум {CLAN_NAME_MAX_LEN} символов).</b>")
        return
    if await get_user_clan(pool, message.from_user.id):
        await message.answer("<b>Ты уже состоишь в клане.</b>")
        return
    if await clan_name_taken(pool, name):
        await message.answer("<b>Клан с таким названием уже существует.</b>")
        return

    await state.update_data(clan_name=name)
    await state.set_state(CreateClan.waiting_photo)
    await message.answer("<b>📸 Пришли фото клана</b>")


@router.message(CreateClan.waiting_photo, GROUP_CHATS)
async def clan_create_photo(message: Message, state: FSMContext, pool: asyncpg.Pool):
    if not message.photo:
        await message.answer("<b>Пришли именно фото 🙂</b>")
        return

    data = await state.get_data()
    name = data["clan_name"]

    if await get_user_clan(pool, message.from_user.id):
        await state.clear()
        await message.answer("<b>Ты уже состоишь в клане.</b>")
        return
    if await clan_name_taken(pool, name):
        await state.clear()
        await message.answer("<b>Пока ты думал, название заняли. Попробуй снова: /clancreate Название</b>")
        return

    money = await get_chat_money(pool, message.chat.id, message.from_user.id)
    if money < CLAN_CREATE_COST:
        await state.clear()
        await message.answer(f"<b>💰 Не хватает монет. Нужно {CLAN_CREATE_COST}, у тебя {money}.</b>")
        return

    photo_id = message.photo[-1].file_id
    await create_clan(pool, name, photo_id, message.from_user.id, message.from_user.full_name, message.chat.id)
    await state.clear()
    await message.answer_photo(
        photo_id,
        caption=f"<b>🏰 Клан «{html.escape(name)}» создан!\n\n👑 Основатель: {html.escape(message.from_user.full_name)}</b>",
    )


@router.message(Command("claninvite", ignore_case=True), GROUP_CHATS)
async def cmd_clan_invite(message: Message, pool: asyncpg.Pool):
    clan = await get_user_clan(pool, message.from_user.id)
    if not clan:
        await message.answer("<b>У тебя нет клана.</b>")
        return
    if clan["creator_id"] != message.from_user.id:
        await message.answer("<b>Приглашать в клан может только его создатель.</b>")
        return

    reply = message.reply_to_message
    if reply is None or reply.from_user is None:
        await message.answer("<b>Ответь этой командой на сообщение игрока, которого хочешь пригласить.</b>")
        return
    target = reply.from_user
    if target.is_bot:
        await message.answer("<b>Ботов приглашать нельзя.</b>")
        return
    if target.id == message.from_user.id:
        await message.answer("<b>Нельзя пригласить самого себя.</b>")
        return
    if await get_user_clan(pool, target.id):
        await message.answer("<b>Этот игрок уже состоит в клане.</b>")
        return
    if await clan_member_count(pool, clan["id"]) >= CLAN_MAX_MEMBERS:
        await message.answer(f"<b>В клане уже максимум участников ({CLAN_MAX_MEMBERS}).</b>")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"clan:accept:{clan['id']}:{target.id}")
    kb.button(text="❌ Отклонить", callback_data=f"clan:decline:{clan['id']}:{target.id}")
    kb.adjust(2)
    await message.answer(
        f"<b>📨 {html.escape(target.full_name)}, тебя приглашают в клан «{html.escape(clan['name'])}»!</b>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("clan:accept:"), GROUP_CHATS)
async def clan_invite_accept(callback: CallbackQuery, pool: asyncpg.Pool):
    _, _, clan_id_str, user_id_str = callback.data.split(":")
    clan_id, invited_id = int(clan_id_str), int(user_id_str)
    if callback.from_user.id != invited_id:
        await callback.answer("Это приглашение не тебе", show_alert=True)
        return
    if await get_user_clan(pool, invited_id):
        await callback.answer("Ты уже в клане", show_alert=True)
        await safe_edit_text(callback.message, "<b>Приглашение больше не активно.</b>")
        return
    clan = await get_clan(pool, clan_id)
    if not clan:
        await callback.answer("Этот клан уже не существует", show_alert=True)
        await safe_edit_text(callback.message, "<b>Этот клан больше не существует.</b>")
        return
    if await clan_member_count(pool, clan_id) >= CLAN_MAX_MEMBERS:
        await callback.answer("В клане уже нет мест", show_alert=True)
        await safe_edit_text(callback.message, "<b>В клане уже нет свободных мест.</b>")
        return

    await add_clan_member(pool, clan_id, invited_id, callback.from_user.full_name)
    await safe_edit_text(
        callback.message,
        f"<b>🎉 {html.escape(callback.from_user.full_name)} вступил в клан «{html.escape(clan['name'])}»!</b>",
    )
    await callback.answer("Добро пожаловать в клан!")


@router.callback_query(F.data.startswith("clan:decline:"), GROUP_CHATS)
async def clan_invite_decline(callback: CallbackQuery):
    _, _, _clan_id_str, user_id_str = callback.data.split(":")
    if callback.from_user.id != int(user_id_str):
        await callback.answer("Это приглашение не тебе", show_alert=True)
        return
    await safe_edit_text(callback.message, "<b>Приглашение отклонено.</b>")
    await callback.answer()


@router.message(Command("clandelete", ignore_case=True), GROUP_CHATS)
async def cmd_clan_delete(message: Message, pool: asyncpg.Pool):
    clan = await get_user_clan(pool, message.from_user.id)
    if not clan:
        await message.answer("<b>У тебя нет клана.</b>")
        return
    if clan["creator_id"] != message.from_user.id:
        await message.answer("<b>Удалить клан может только его создатель.</b>")
        return

    now = time.time()
    last_attempt = _pending_clan_delete.get(message.from_user.id)
    if last_attempt is not None and now - last_attempt <= CLAN_DELETE_CONFIRM_WINDOW:
        _pending_clan_delete.pop(message.from_user.id, None)
        await delete_clan(pool, clan["id"])
        await message.answer(f"<b>💥 Клан «{html.escape(clan['name'])}» удалён.</b>")
        return

    _pending_clan_delete[message.from_user.id] = now
    await message.answer(
        f"<b>⚠️ Чтобы удалить клан «{html.escape(clan['name'])}», отправь /clandelete ещё раз в течение минуты.</b>"
    )


@router.message(Command("clanleft", ignore_case=True), GROUP_CHATS)
async def cmd_clan_left(message: Message, pool: asyncpg.Pool):
    clan = await get_user_clan(pool, message.from_user.id)
    if not clan:
        await message.answer("<b>Ты не состоишь в клане.</b>")
        return
    if clan["creator_id"] == message.from_user.id:
        await message.answer(
            "<b>Ты создатель клана — сначала удали его через /clandelete (дважды подряд в течение минуты).</b>"
        )
        return
    await remove_clan_member(pool, message.from_user.id)
    await message.answer(f"<b>👋 Ты покинул клан «{html.escape(clan['name'])}».</b>")


@router.message(Command("clantop", ignore_case=True), GROUP_CHATS)
async def cmd_clan_top(message: Message, pool: asyncpg.Pool):
    clans = await get_clan_top(pool, CLAN_TOP_LIMIT)
    if not clans:
        await message.answer("<b>📭 Пока нет ни одного клана.</b>")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["<b>🏆 Топ кланов по силе", "━━━━━━━━━━━━━━", ""]
    for i, clan in enumerate(clans):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(
            f"{prefix} {html.escape(clan['name'])} — ⚔️ {clan['total_power']} "
            f"({clan['members']}/{CLAN_MAX_MEMBERS})"
        )
    await message.answer("\n".join(lines) + "</b>")


@router.message(Command("clan", ignore_case=True), GROUP_CHATS)
async def cmd_clan_info(message: Message, command: CommandObject, pool: asyncpg.Pool):
    query = (command.args or "").strip()
    if query:
        clan = await get_clan_by_name(pool, query)
        if not clan:
            await message.answer("<b>Клан с таким названием не найден.</b>")
            return
    else:
        clan = await get_user_clan(pool, message.from_user.id)
        if not clan:
            await message.answer("<b>Ты не состоишь в клане. Чтобы посмотреть другой: /clan Название</b>")
            return

    members = await get_clan_members(pool, clan["id"])
    total_power = sum(member["power"] for member in members)

    lines = [
        f"<b>🏰 {html.escape(clan['name'])}\n\n"
        f"⚔️ Общая сила: {total_power}\n"
        f"👥 Участников: {len(members)}/{CLAN_MAX_MEMBERS}\n\n"
        "Состав:</b>"
    ]
    for i, member in enumerate(members, start=1):
        crown = "👑 " if member["user_id"] == clan["creator_id"] else ""
        name = html.escape(member["name"] or "Игрок")
        lines.append(f"<b>{i}. {crown}{name} — ⚔️ {member['power']}</b>")

    await message.answer_photo(clan["photo_id"], caption="\n".join(lines))


# ── Реферальная программа (1% создателю группы) ───────────────────────────

@router.my_chat_member(GROUP_CHATS)
async def on_bot_added(event: ChatMemberUpdated, bot: Bot, pool: asyncpg.Pool):
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if old_status not in ("left", "kicked") or new_status not in ("member", "administrator"):
        return

    creator = await get_chat_creator(bot, event.chat.id)
    if creator is None:
        return
    owner_id, owner_name = creator

    title = event.chat.title or "группа"
    is_new = await register_chat_owner(pool, event.chat.id, owner_id, owner_name, title)
    if not is_new:
        return

    try:
        await bot.send_message(owner_id, owner_notify_text(title))
    except TelegramForbiddenError:
        return
    await mark_owner_notified(pool, event.chat.id)


# ── Лог обновлений ───────────────────────────────────────────────────────

DEPLOY_NOTIFY_CHAT = "@L1meYT"


def get_deploy_version() -> tuple[str, str]:
    """Возвращает (version_id, описание) — по git-коммиту, либо по хешу файла, если git недоступен."""
    base_dir = Path(__file__).resolve().parent
    try:
        commit_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=base_dir, text=True, stderr=subprocess.DEVNULL
        ).strip()
        commit_msg = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s"], cwd=base_dir, text=True, stderr=subprocess.DEVNULL
        ).strip()
        commit_date = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%cd", "--date=format:%d.%m.%Y %H:%M"],
            cwd=base_dir, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        details = f"📝 {html.escape(commit_msg)}\n🕐 {commit_date}"
        return commit_hash, details
    except Exception:
        digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:8]
        return digest, "📝 Локальная версия (git недоступен)"


async def notify_deploy(bot: Bot, pool: asyncpg.Pool) -> None:
    version_id, details = get_deploy_version()
    if await get_last_deploy_version(pool) == version_id:
        return

    text = f"<b>🚀 Angry Bank обновлён и запущен</b>\n\n{details}\n🔖 <code>{version_id}</code>"
    try:
        await bot.send_message(DEPLOY_NOTIFY_CHAT, text)
    except TelegramAPIError as error:
        logging.warning("Не удалось отправить лог обновления: %s", error)
        return
    await set_last_deploy_version(pool, version_id)


# ── Запуск ───────────────────────────────────────────────────────────────

async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть меню")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="angryopen", description="крутануть копилку"),
            BotCommand(command="angryclass", description="крутануть класс"),
            BotCommand(command="steal", description="украсть монеты (в ответ на сообщение)"),
            BotCommand(command="clancreate", description="создать клан (500 монет)"),
            BotCommand(command="claninvite", description="пригласить в клан (в ответ на сообщение)"),
            BotCommand(command="clandelete", description="удалить клан (дважды подряд)"),
            BotCommand(command="clanleft", description="выйти из клана"),
            BotCommand(command="clantop", description="топ кланов по силе"),
            BotCommand(command="clan", description="инфо о клане"),
            BotCommand(command="angrytop", description="открыть лидерборд"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


BOT_TOKEN = "8822713742:AAHYx6SzmdiOyrESTgnrDcNoTYpxzrlP5K4"
DATABASE_URL = (
    "postgresql://bothost_db_070b39e25784:-IpoMUbOGfL-gKUZj9kDRhD7RrJ02C7NOcrrvFBIxWo"
    "@node1.pghost.ru:16036/bothost_db_070b39e25784"
)


async def main() -> None:
    token = BOT_TOKEN or os.environ.get("BOT_TOKEN")
    dsn = os.environ.get("DATABASE_URL") or DATABASE_URL
    if not token:
        raise RuntimeError("Не найден токен бота в переменной окружения BOT_TOKEN")

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    await init_db(pool)
    try:
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())
        dp.message.outer_middleware(AdminAssignMiddleware())
        dp.message.outer_middleware(OwnerNotifyMiddleware())
        dp.include_router(router)

        await set_bot_commands(bot)
        await bot.delete_webhook(drop_pending_updates=True)
        await notify_deploy(bot, pool)
        await dp.start_polling(bot, pool=pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
