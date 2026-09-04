import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import random
import time
from pathlib import Path
from urllib.parse import parse_qsl

import asyncpg
from aiohttp import web
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
    InputMediaPhoto,
    Message,
    TelegramObject,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

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
ASTRAL_DRAW_CHANCE = 0.0005

AIRDROP_CHAT_STAGGER_MIN = 5
AIRDROP_CHAT_STAGGER_MAX = 30
AIRDROP_EXPIRE_CHECK_INTERVAL = 30

# Теги участников (setChatMemberTag): раз в сколько секунд обновляем теги топа
# и сколько первых мест их получают.
MEMBER_TAG_REFRESH_INTERVAL = 10 * 60
MEMBER_TAG_TOP_LIMIT = TOP_LIMIT


# ── Настраиваемые параметры (админка → ⚙️ Настройки) ─────────────────────
# key -> (раздел, подпись, единица, значение по умолчанию, минимум)
SETTINGS_SPEC = {
    "piggy_cooldown_min": ("piggy", "Кулдаун крутки", "мин", 20, 1),
    "class_cooldown_min": ("class", "Кулдаун крутки", "мин", 1, 1),
    "class_spin_cost": ("class", "Цена крутки", "монет", 30, 0),
    "airdrop_cycle_min": ("airdrop", "Интервал появления", "мин", 20, 1),
    "airdrop_expire_min": ("airdrop", "Время жизни дропа", "мин", 5, 1),
    "airdrop_max_claims": ("airdrop", "Лимит дропов на игрока за цикл", "шт", 3, 1),
    "airdrop_min_members": ("airdrop", "Минимум участников в группе", "чел", 5, 0),
    "energy_max": ("energy", "Максимум энергии", "⚡", 100, 1),
    "energy_regen_min": ("energy", "Восстановление 1⚡ раз в", "мин", 10, 1),
    "battle_cooldown_min": ("energy", "Кулдаун боя", "мин", 5, 1),
    "battle_energy_min": ("energy", "Энергии за бой (от)", "⚡", 3, 0),
    "battle_energy_max": ("energy", "Энергии за бой (до)", "⚡", 5, 0),
    "casino_spins_per_min": ("casino", "Ставок в минуту", "шт", 3, 1),
    "casino_min_bet": ("casino", "Минимальная ставка", "монет", 10, 1),
    "casino_max_bet": ("casino", "Максимальная ставка (0 — без лимита)", "монет", 0, 0),
    "multi_x2_chance": ("multi", "Шанс ×2 крутки", "%", 10, 0),
    "multi_x3_chance": ("multi", "Шанс ×3 крутки", "%", 5, 0),
}

SETTINGS_SECTIONS = {
    "piggy": "🐷 Копилка",
    "class": "🎓 Класс",
    "airdrop": "🎁 Аирдропы",
    "energy": "⚡ Энергия и бои",
    "casino": "🎰 Казино",
    "multi": "🎲 Множители круток",
}

_settings_cache: dict[str, int] = {}


def setting(key: str) -> int:
    """Текущее значение настройки: из кэша, иначе значение по умолчанию."""
    if key in _settings_cache:
        return _settings_cache[key]
    return SETTINGS_SPEC[key][3]


async def load_settings(pool: asyncpg.Pool) -> None:
    rows = await pool.fetch("SELECT key, value FROM settings")
    _settings_cache.clear()
    for row in rows:
        if row["key"] in SETTINGS_SPEC:
            _settings_cache[row["key"]] = row["value"]


async def save_setting(pool: asyncpg.Pool, key: str, value: int) -> None:
    await pool.execute(
        """
        INSERT INTO settings (key, value) VALUES ($1, $2)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """,
        key, value,
    )
    _settings_cache[key] = value

# code -> (название, "1 в N" -> вес выпадения, иконка)
AIRDROP_TIERS = (
    ("common", "Обычный", 1 / 2, "📦"),
    ("uncommon", "Необычный", 1 / 5, "🎁"),
    ("rare", "Редкий", 1 / 10, "💎"),
    ("epic", "Эпический", 1 / 20, "🔮"),
    ("legendary", "Легендарный", 1 / 100, "🌟"),
    ("mythic", "Мифический", 1 / 300, "🔥"),
    ("astral", "Астральный", 1 / 1000, "🌌"),
)
AIRDROP_TIER_LABELS = {code: label for code, label, _, _ in AIRDROP_TIERS}
AIRDROP_TIER_ICONS = {code: icon for code, _, _, icon in AIRDROP_TIERS}

# тип строки -> (мин, макс, шанс попадания строки в дроп)
AIRDROP_LOOT = {
    "common": [("power", 1, 10, 0.40), ("money", 1, 5, 0.40)],
    "uncommon": [("power", 5, 15, 0.40), ("money", 5, 10, 0.40)],
    "rare": [("power", 20, 30, 0.40), ("money", 10, 20, 0.40)],
    "epic": [("power", 30, 40, 0.70), ("money", 20, 40, 0.50)],
    "legendary": [
        ("power", 40, 60, 0.70),
        ("money", 40, 60, 0.50),
        ("tokens", 1, 1, 0.01),
        ("astral", 0, 0, 0.001),
    ],
    "mythic": [
        ("power", 60, 100, 1.00),
        ("money", 60, 100, 0.50),
        ("tokens", 1, 2, 0.01),
        ("astral", 0, 0, 0.01),
    ],
    "astral": [
        ("power", 100, 150, 1.00),
        ("money", 100, 200, 0.50),
        ("tokens", 2, 4, 1.00),
        ("astral", 0, 0, 0.10),
    ],
}

# Тир -> «1 в N»: во сколько раз реже он выпадает. Это лишь значения по
# умолчанию — рабочие лежат в airdrop_tier_weights и правятся из админки.
AIRDROP_TIER_ONE_IN = {code: round(1 / weight) for code, _l, weight, _i in AIRDROP_TIERS}

# Строки дропа: код -> (подпись, есть ли диапазон значений)
AIRDROP_KINDS = (
    ("power", "⚔️ Сила", True),
    ("money", "🪙 Монеты", True),
    ("tokens", "🐟 Токены", True),
    ("astral", "🌌 Астральная карточка", False),
)
AIRDROP_KIND_LABELS = {kind: label for kind, label, _ in AIRDROP_KINDS}
AIRDROP_KIND_HAS_RANGE = {kind: has_range for kind, _l, has_range in AIRDROP_KINDS}

_airdrop_loot_cache: dict[str, dict[str, tuple[int, int, float]]] = {}
_airdrop_weight_cache: dict[str, int] = {}


def default_loot_line(tier: str, kind: str) -> tuple[int, int, float]:
    for line_kind, lo, hi, chance in AIRDROP_LOOT.get(tier, ()):
        if line_kind == kind:
            return lo, hi, chance
    return 0, 0, 0.0


async def load_airdrop_config(pool: asyncpg.Pool) -> None:
    """Заливает дефолты при первом запуске и поднимает конфиг в кэш.
    В таблице есть строка на каждую пару тир+тип, у выключенных шанс 0 —
    так админ может включить в тире то, чего в нём изначально не было."""
    for tier, _label, _weight, _icon in AIRDROP_TIERS:
        await pool.execute(
            "INSERT INTO airdrop_tier_weights (tier, one_in) VALUES ($1, $2) ON CONFLICT (tier) DO NOTHING",
            tier, AIRDROP_TIER_ONE_IN[tier],
        )
        for kind, _kl, _kr in AIRDROP_KINDS:
            lo, hi, chance = default_loot_line(tier, kind)
            await pool.execute(
                "INSERT INTO airdrop_loot (tier, kind, lo, hi, chance) VALUES ($1, $2, $3, $4, $5) "
                "ON CONFLICT (tier, kind) DO NOTHING",
                tier, kind, lo, hi, chance,
            )
    await refresh_airdrop_config(pool)


async def refresh_airdrop_config(pool: asyncpg.Pool) -> None:
    _airdrop_weight_cache.clear()
    for row in await pool.fetch("SELECT tier, one_in FROM airdrop_tier_weights"):
        _airdrop_weight_cache[row["tier"]] = row["one_in"]
    _airdrop_loot_cache.clear()
    for row in await pool.fetch("SELECT tier, kind, lo, hi, chance FROM airdrop_loot"):
        _airdrop_loot_cache.setdefault(row["tier"], {})[row["kind"]] = (
            row["lo"], row["hi"], row["chance"],
        )


def airdrop_one_in(tier: str) -> int:
    return max(1, _airdrop_weight_cache.get(tier) or AIRDROP_TIER_ONE_IN.get(tier, 1))


def airdrop_line(tier: str, kind: str) -> tuple[int, int, float]:
    cached = _airdrop_loot_cache.get(tier, {}).get(kind)
    return cached if cached is not None else default_loot_line(tier, kind)


async def save_airdrop_line(
    pool: asyncpg.Pool, tier: str, kind: str, lo: int, hi: int, chance: float
) -> None:
    await pool.execute(
        "INSERT INTO airdrop_loot (tier, kind, lo, hi, chance) VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (tier, kind) DO UPDATE SET lo = EXCLUDED.lo, hi = EXCLUDED.hi, "
        "chance = EXCLUDED.chance",
        tier, kind, lo, hi, chance,
    )
    _airdrop_loot_cache.setdefault(tier, {})[kind] = (lo, hi, chance)


async def save_airdrop_weight(pool: asyncpg.Pool, tier: str, one_in: int) -> None:
    await pool.execute(
        "INSERT INTO airdrop_tier_weights (tier, one_in) VALUES ($1, $2) "
        "ON CONFLICT (tier) DO UPDATE SET one_in = EXCLUDED.one_in",
        tier, one_in,
    )
    _airdrop_weight_cache[tier] = one_in


def fmt_chance(chance: float) -> str:
    if chance <= 0:
        return "выкл"
    return f"{chance * 100:g}%"


def group_intro_text() -> str:
    """Строится на лету — цифры берутся из настроек, которые админ может менять."""
    return (
        "<b>🐦 Angry Копилка\n\n"
        f"Раз в {setting('piggy_cooldown_min')} мин можно крутануть копилку и получить силу и монеты.\n"
        f"За {setting('class_spin_cost')} монет (раз в {setting('class_cooldown_min')} мин) можно "
        "крутить класс и получить только силу.\n"
        "Ответом «кража» или /AngrySteal на чужое сообщение можно украсть монеты (раз в 30 минут).\n"
        f"На энергию ({setting('energy_max')}, восстанавливается 1⚡ раз в "
        f"{setting('energy_regen_min')} мин) можно биться с мобами — /AngryBattle.\n"
        f"Раз в {setting('airdrop_cycle_min')} мин в чате падает аирдроп — "
        "успей нажать «Забрать» первым!\n\n"
        "🎰 /AngryOpen — крутануть копилку\n"
        "🎓 /AngryClass — крутануть класс\n"
        "🥷 /AngrySteal — украсть монеты (в ответ на сообщение)\n"
        f"⚔️ /AngryBattle — сразиться с мобом ({setting('battle_energy_min')}-"
        f"{setting('battle_energy_max')}⚡, раз в {setting('battle_cooldown_min')} мин)\n"
        "ℹ️ /AngryInfo или «инфо» — профиль игрока (в ответ на сообщение)\n"
        "🎰 /AngryCasino 100 — поставить 100 монет (или «казик 100»)\n"
        "🎒 /AngryInv — инвентарь: классы и предметы\n"
        "🏆 /AngryTop — топ силы чата\n\n"
        f"🏰 /clancreate Название — создать клан ({CLAN_CREATE_COST} монет)\n"
        "📨 /claninvite — пригласить в клан (в ответ на сообщение)\n"
        "🚪 /clanleft — выйти из клана\n"
        "💥 /clandelete — удалить клан (дважды подряд)\n"
        "🏆 /clantop — топ кланов по силе\n"
        "ℹ️ /clan [название] — инфо о клане</b>"
    )
REFERRAL_HOWTO_TEXT = (
    "<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Как зарабатывать на своей группе\n\n"
    "1. Добавь меня в свою группу (кнопка ниже).\n"
    "2. Я сам определю создателя группы и начну начислять тебе 1% от всех монет, которые "
    "заработают игроки в этой группе — прямо к твоему балансу, без лишних уведомлений.\n"
    "3. Больше ничего делать не нужно — доход идёт автоматически, пока бот в группе.</b>"
)

CARD_FIELDS = ("name", "power", "money", "photo_id", "rarity", "bird")
FIELD_PROMPTS = {
    "photo_id": "<b>📸 Пришли новое фото карточки</b>",
    "name": "<b>✏️ Введи новое название карточки</b>",
    "power": "<b>⚔️ Введи новую силу карточки (число)</b>",
    "money": "<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Введи новое количество денег (число)</b>",
}

CLASS_CARD_FIELDS = ("name", "power", "photo_id", "rarity", "bird")
CLASS_FIELD_PROMPTS = {
    "photo_id": "<b>📸 Пришли новое фото карточки</b>",
    "name": "<b>✏️ Введи новое название карточки</b>",
    "power": "<b>⚔️ Введи новую силу карточки (число)</b>",
}

MOB_FIELD_PROMPTS = {
    "photo_id": "<b>📸 Пришли новое фото моба</b>",
    "name": "<b>✏️ Введи новое название моба</b>",
    "power": "<b>⚔️ Введи новую силу моба (число)</b>",
    "money": "<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Введи новую награду монет за убийство (число)</b>",
}

# ── Редкость карточек ────────────────────────────────────────────────────

REGULAR_STAR_EMOJI_ID = "5224378646688474051"
RAINBOW_STAR_EMOJI_ID = "5224307204202476701"
ASTRAL_STAR_EMOJI_ID = "5233626046284212790"

# Премиум-эмодзи монеты — та же, что и в остальных сообщениях бота.
COIN_EMOJI = "<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji>"
TOKEN_EMOJI = "<tg-emoji emoji-id='5233482504182212210'>🐟</tg-emoji>"

# rarity code -> (кол-во звёзд, тип звезды)
RARITY_INFO = {
    1: (1, "regular"),
    2: (2, "regular"),
    3: (3, "regular"),
    4: (3, "rainbow"),
    5: (3, "astral"),
}
RARITY_PICK_BUTTONS = (
    (1, "⭐"),
    (2, "⭐⭐"),
    (3, "⭐⭐⭐"),
    (4, "🌈⭐⭐⭐"),
    (5, "✨⭐⭐⭐"),
)


STAR_EMOJI_IDS = {
    "regular": REGULAR_STAR_EMOJI_ID,
    "rainbow": RAINBOW_STAR_EMOJI_ID,
    "astral": ASTRAL_STAR_EMOJI_ID,
}


def stars_html(count: int, kind: str) -> str:
    emoji_id = STAR_EMOJI_IDS.get(kind, REGULAR_STAR_EMOJI_ID)
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
    kb.adjust(3, 2, 1)
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
        last_class DOUBLE PRECISION NOT NULL DEFAULT 0,
        best_card_name TEXT,
        best_card_photo_id TEXT,
        best_card_power BIGINT,
        best_card_rarity SMALLINT,
        best_card_bird SMALLINT
    )
    """,
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_casino DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS casino_spins INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_card_name TEXT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_card_photo_id TEXT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_card_power BIGINT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_card_rarity SMALLINT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_card_bird SMALLINT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_class_name TEXT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_class_photo_id TEXT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_class_power BIGINT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_class_rarity SMALLINT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_class_bird SMALLINT",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS tokens BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS airdrop_cycle_id BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS airdrop_cycle_claims INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_open DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_class DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS energy INTEGER NOT NULL DEFAULT 100",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS energy_updated_at DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS last_battle DOUBLE PRECISION NOT NULL DEFAULT 0",
    """
    CREATE TABLE IF NOT EXISTS mobs (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        power BIGINT NOT NULL,
        money BIGINT NOT NULL,
        photo_id TEXT NOT NULL
    )
    """,
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
    """
    CREATE TABLE IF NOT EXISTS bot_chats (
        chat_id BIGINT PRIMARY KEY,
        title TEXT NOT NULL
    )
    """,
    # bot_chats раньше заполнялась только при добавлении бота в группу, поэтому
    # все группы, где бот оказался раньше появления таблицы, в неё не попали —
    # и рассылки (аирдропы, промо) их не видели. Добираем их из chat_owners.
    """
    INSERT INTO bot_chats (chat_id, title)
    SELECT chat_id, chat_title FROM chat_owners
    ON CONFLICT (chat_id) DO NOTHING
    """,
    """
    CREATE TABLE IF NOT EXISTS airdrops (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT NOT NULL,
        message_id BIGINT NOT NULL,
        tier TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL,
        claimed_by BIGINT,
        claimed_name TEXT,
        expired BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value BIGINT NOT NULL
    )
    """,
    # Теги, которые бот выдал сам. Нужны, чтобы отличать свой тег от тега,
    # который участнику поставили руками: чужой мы не трогаем и не затираем.
    """
    CREATE TABLE IF NOT EXISTS member_tags (
        chat_id BIGINT NOT NULL,
        user_id BIGINT NOT NULL,
        tag TEXT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    )
    """,
    # Инвентарь игрока: сколько раз ему выпадала каждая карточка. Глобальный,
    # как сила и токены — не делится по чатам. kind: 'card' (копилка) / 'class'.
    """
    CREATE TABLE IF NOT EXISTS airdrop_tier_weights (
        tier TEXT PRIMARY KEY,
        one_in INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS airdrop_loot (
        tier TEXT NOT NULL,
        kind TEXT NOT NULL,
        lo BIGINT NOT NULL,
        hi BIGINT NOT NULL,
        chance DOUBLE PRECISION NOT NULL,
        PRIMARY KEY (tier, kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cleardata_log (
        user_id BIGINT PRIMARY KEY,
        cleared_at DOUBLE PRECISION NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory (
        user_id BIGINT NOT NULL,
        kind TEXT NOT NULL,
        card_id INTEGER NOT NULL,
        qty INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, kind, card_id)
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


async def set_admin(pool: asyncpg.Pool, user_id: int) -> None:
    await pool.execute("UPDATE bot_state SET admin_id = $1 WHERE id = 1", user_id)


async def apply_admin_override(pool: asyncpg.Pool) -> None:
    """Закрепляет админом ADMIN_USER_ID, если он задан.

    Правило «админ = первый написавший боту» одноразовое: если им стал не тот
    человек, сменить его иначе нельзя. ID берём константой, а не резолвом по
    username, — чтобы не зависеть от доступности getChat.
    """
    if not ADMIN_USER_ID:
        return
    if await get_admin_id(pool) == ADMIN_USER_ID:
        return
    await set_admin(pool, ADMIN_USER_ID)
    logging.info("Админ назначен: id %s", ADMIN_USER_ID)


async def get_active_chat_ids(pool: asyncpg.Pool) -> list[int]:
    rows = await pool.fetch("SELECT chat_id FROM bot_chats")
    return [row["chat_id"] for row in rows]


async def get_active_chats(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT chat_id, title FROM bot_chats ORDER BY title")
    return [dict(row) for row in rows]


def group_link(chat_id: int) -> str | None:
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}"
    return None


def group_message_link(chat_id: int, message_id: int) -> str | None:
    base = group_link(chat_id)
    return f"{base}/{message_id}" if base else None


async def notify_admin(bot: Bot, pool: asyncpg.Pool, text: str) -> None:
    admin_id = await get_admin_id(pool)
    if admin_id is None:
        return
    try:
        await bot.send_message(admin_id, text)
    except TelegramAPIError as error:
        logging.warning("Не удалось отправить админ-уведомление: %s", error)


async def notify_admin_broadcast_results(
    bot: Bot, pool: asyncpg.Pool, title: str, results: list[tuple]
) -> None:
    if not results:
        return
    lines = [f"<b>{title} — рассылка ({len(results)} чат(ов))</b>", ""]
    for chat_title, link, ok in results:
        safe_title = html.escape(chat_title)
        if not ok:
            lines.append(f"❌ {safe_title} — не отправлено")
        elif link:
            lines.append(f"✅ <a href='{link}'>{safe_title}</a>")
        else:
            lines.append(f"✅ {safe_title} (ссылка недоступна для этого типа чата)")
    await notify_admin(bot, pool, "\n".join(lines))


# ── Аирдропы ─────────────────────────────────────────────────────────────

def roll_airdrop_tier() -> tuple[str, str, str]:
    codes = [t[0] for t in AIRDROP_TIERS]
    weights = [1 / airdrop_one_in(code) for code in codes]
    code = random.choices(codes, weights=weights, k=1)[0]
    return code, AIRDROP_TIER_LABELS[code], AIRDROP_TIER_ICONS[code]


def roll_airdrop_loot(tier: str) -> list[tuple]:
    lines = []
    for kind, _label, _has_range in AIRDROP_KINDS:
        lo, hi, chance = airdrop_line(tier, kind)
        if chance > 0:
            lines.append((kind, lo, hi, chance))
    if not lines:
        return []
    hits = [line for line in lines if random.random() < line[3]]
    if not hits:
        hits = [max(lines, key=lambda line: line[3])]
    if len(hits) > 5:
        hits = random.sample(hits, 5)
    return hits


async def try_claim_airdrop_slot(conn: asyncpg.Connection, user_id: int, name: str, cycle_id: int) -> bool:
    row = await conn.fetchrow(
        "SELECT airdrop_cycle_id, airdrop_cycle_claims FROM players WHERE user_id = $1 FOR UPDATE",
        user_id,
    )
    claims = row["airdrop_cycle_claims"] if row and row["airdrop_cycle_id"] == cycle_id else 0
    if claims >= setting("airdrop_max_claims"):
        return False
    await conn.execute(
        """
        INSERT INTO players (user_id, name, airdrop_cycle_id, airdrop_cycle_claims)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id) DO UPDATE SET
            name = EXCLUDED.name,
            airdrop_cycle_id = EXCLUDED.airdrop_cycle_id,
            airdrop_cycle_claims = EXCLUDED.airdrop_cycle_claims
        """,
        user_id, name, cycle_id, claims + 1,
    )
    return True


async def apply_airdrop_loot(
    conn: asyncpg.Connection, chat_id: int, user_id: int, name: str, hits: list[tuple]
) -> list[str]:
    lines: list[str] = []
    for kind, lo, hi, _ in hits:
        if kind == "power":
            amount = random.randint(lo, hi)
            await add_player_power(conn, user_id, name, amount)
            lines.append(f"⚔️ +{amount} силы")
        elif kind == "money":
            amount = random.randint(lo, hi)
            await conn.execute(
                """
                INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
                VALUES ($1, $2, $3, $4, 0)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name = EXCLUDED.name, money = chat_profiles.money + EXCLUDED.money
                """,
                chat_id, user_id, name, amount,
            )
            lines.append(f"<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> +{amount} монет")
        elif kind == "tokens":
            amount = random.randint(lo, hi)
            await add_player_tokens(conn, user_id, name, amount)
            lines.append(f"<tg-emoji emoji-id='5233482504182212210'>🐟</tg-emoji> +{amount} токенов")
        elif kind == "astral":
            pool_choice = random.choice(("piggy", "class"))
            if pool_choice == "piggy":
                card = await conn.fetchrow(
                    "SELECT id, name, power, money, photo_id, rarity, bird FROM cards WHERE rarity = 5 ORDER BY random() LIMIT 1"
                )
                if card is None:
                    card = await conn.fetchrow(
                        "SELECT id, name, power, photo_id, rarity, bird FROM class_cards WHERE rarity = 5 ORDER BY random() LIMIT 1"
                    )
                    if card is not None:
                        pool_choice = "class"
            else:
                card = await conn.fetchrow(
                    "SELECT id, name, power, photo_id, rarity, bird FROM class_cards WHERE rarity = 5 ORDER BY random() LIMIT 1"
                )
                if card is None:
                    card = await conn.fetchrow(
                        "SELECT id, name, power, money, photo_id, rarity, bird FROM cards WHERE rarity = 5 ORDER BY random() LIMIT 1"
                    )
                    if card is not None:
                        pool_choice = "piggy"

            if card is None:
                continue

            card = dict(card)
            await add_player_power(conn, user_id, name, card["power"])
            if pool_choice == "piggy":
                await conn.execute(
                    """
                    INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
                    VALUES ($1, $2, $3, $4, 0)
                    ON CONFLICT (chat_id, user_id) DO UPDATE SET
                        name = EXCLUDED.name, money = chat_profiles.money + EXCLUDED.money
                    """,
                    chat_id, user_id, name, card["money"],
                )
                await maybe_update_best_card(conn, user_id, card)
                await add_to_inventory(conn, user_id, "card", card)
                lines.append(
                    f"🌌 <b>Астральная карточка (копилка): {card_title(card)}</b>\n"
                    f"{rarity_display(card['rarity'])}\n"
                    f"   ⚔️ +{card['power']} силы · <tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> +{card['money']} монет"
                )
            else:
                await maybe_update_best_class(conn, user_id, card)
                await add_to_inventory(conn, user_id, "class", card)
                lines.append(
                    f"🌌 <b>Астральная карточка (класс): {card_title(card)}</b>\n"
                    f"{rarity_display(card['rarity'])}\n"
                    f"   ⚔️ +{card['power']} силы"
                )
    return lines


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


MOB_FIELDS = ("name", "power", "money", "photo_id")


async def list_mobs(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT id, name, power, money, photo_id FROM mobs ORDER BY id")
    return [dict(row) for row in rows]


async def mobs_count(pool: asyncpg.Pool) -> int:
    return await pool.fetchval("SELECT COUNT(*) FROM mobs")


async def get_mob(pool: asyncpg.Pool, mob_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, name, power, money, photo_id FROM mobs WHERE id = $1", mob_id
    )
    return dict(row) if row else None


async def add_mob(pool: asyncpg.Pool, name: str, power: int, money: int, photo_id: str) -> None:
    await pool.execute(
        "INSERT INTO mobs (name, power, money, photo_id) VALUES ($1, $2, $3, $4)",
        name, power, money, photo_id,
    )


async def update_mob_field(pool: asyncpg.Pool, mob_id: int, field: str, value) -> None:
    if field not in MOB_FIELDS:
        raise ValueError(f"Недопустимое поле моба: {field}")
    await pool.execute(f"UPDATE mobs SET {field} = $1 WHERE id = $2", value, mob_id)


async def delete_mob(pool: asyncpg.Pool, mob_id: int) -> str | None:
    return await pool.fetchval("DELETE FROM mobs WHERE id = $1 RETURNING name", mob_id)


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


async def add_to_inventory(conn: asyncpg.Connection, user_id: int, kind: str, card: dict) -> None:
    """Кладёт выпавшую карточку в инвентарь игрока (или увеличивает счётчик)."""
    card_id = card.get("id")
    if card_id is None:
        return
    await conn.execute(
        """
        INSERT INTO inventory (user_id, kind, card_id, qty) VALUES ($1, $2, $3, 1)
        ON CONFLICT (user_id, kind, card_id) DO UPDATE SET qty = inventory.qty + 1
        """,
        user_id, kind, card_id,
    )


async def maybe_update_best_card(conn: asyncpg.Connection, user_id: int, card: dict) -> None:
    row = await conn.fetchrow(
        "SELECT best_card_rarity, best_card_power FROM players WHERE user_id = $1", user_id
    )
    current_rarity = row["best_card_rarity"] if row else None
    current_power = row["best_card_power"] if row else None
    is_better = (
        current_rarity is None
        or card["rarity"] > current_rarity
        or (card["rarity"] == current_rarity and card["power"] > (current_power or 0))
    )
    if not is_better:
        return
    await conn.execute(
        """
        UPDATE players SET
            best_card_name = $2, best_card_photo_id = $3,
            best_card_power = $4, best_card_rarity = $5, best_card_bird = $6
        WHERE user_id = $1
        """,
        user_id, card["name"], card["photo_id"], card["power"], card["rarity"], card["bird"],
    )


async def get_player_best_card(pool: asyncpg.Pool, user_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT best_card_name AS name, best_card_photo_id AS photo_id,
               best_card_power AS power, best_card_rarity AS rarity, best_card_bird AS bird
        FROM players WHERE user_id = $1
        """,
        user_id,
    )
    if not row or row["photo_id"] is None:
        return None
    return dict(row)


async def get_player_power(pool: asyncpg.Pool, user_id: int) -> int:
    power = await pool.fetchval("SELECT power FROM players WHERE user_id = $1", user_id)
    return power or 0


async def maybe_update_best_class(conn: asyncpg.Connection, user_id: int, card: dict) -> None:
    row = await conn.fetchrow(
        "SELECT best_class_rarity, best_class_power FROM players WHERE user_id = $1", user_id
    )
    current_rarity = row["best_class_rarity"] if row else None
    current_power = row["best_class_power"] if row else None
    is_better = (
        current_rarity is None
        or card["rarity"] > current_rarity
        or (card["rarity"] == current_rarity and card["power"] > (current_power or 0))
    )
    if not is_better:
        return
    await conn.execute(
        """
        UPDATE players SET
            best_class_name = $2, best_class_photo_id = $3,
            best_class_power = $4, best_class_rarity = $5, best_class_bird = $6
        WHERE user_id = $1
        """,
        user_id, card["name"], card["photo_id"], card["power"], card["rarity"], card["bird"],
    )


async def get_player_best_class(pool: asyncpg.Pool, user_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT best_class_name AS name, best_class_photo_id AS photo_id,
               best_class_power AS power, best_class_rarity AS rarity, best_class_bird AS bird
        FROM players WHERE user_id = $1
        """,
        user_id,
    )
    if not row or row["photo_id"] is None:
        return None
    return dict(row)


async def add_player_tokens(conn: asyncpg.Connection, user_id: int, name: str, tokens: int) -> None:
    await conn.execute(
        """
        INSERT INTO players (user_id, name, tokens) VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name, tokens = players.tokens + EXCLUDED.tokens
        """,
        user_id, name, tokens,
    )


async def get_player_tokens(pool: asyncpg.Pool, user_id: int) -> int:
    tokens = await pool.fetchval("SELECT tokens FROM players WHERE user_id = $1", user_id)
    return tokens or 0


def _weighted_card_pick(rows: list) -> dict | None:
    if not rows:
        return None
    astral = [r for r in rows if r["rarity"] == 5]
    normal = [r for r in rows if r["rarity"] != 5]
    if not normal:
        return dict(random.choice(astral))
    if astral and random.random() < ASTRAL_DRAW_CHANCE:
        return dict(random.choice(astral))
    return dict(random.choice(normal))


async def draw_random_card(conn: asyncpg.Connection) -> dict | None:
    rows = await conn.fetch("SELECT id, name, power, money, photo_id, rarity, bird FROM cards")
    return _weighted_card_pick(rows)


async def draw_random_class_card(conn: asyncpg.Connection) -> dict | None:
    rows = await conn.fetch("SELECT id, name, power, photo_id, rarity, bird FROM class_cards")
    return _weighted_card_pick(rows)


def roll_spin_multiplier() -> int:
    """Сколько карточек выпадет за одну крутку: ×1, ×2 или ×3."""
    roll = random.random() * 100
    x3 = setting("multi_x3_chance")
    x2 = setting("multi_x2_chance")
    if roll < x3:
        return 3
    if roll < x3 + x2:
        return 2
    return 1


def multiplier_banner(multiplier: int) -> str:
    if multiplier >= 3:
        return "🔥 <b>×3 КРУТКА!</b>\n\n"
    if multiplier == 2:
        return "✨ <b>×2 крутка!</b>\n\n"
    return ""


def _regen_energy(energy: int, updated_at: float, now: float) -> tuple[int, float]:
    regen_seconds = setting("energy_regen_min") * 60
    regenerated = int((now - updated_at) // regen_seconds)
    if regenerated <= 0:
        return energy, updated_at
    return min(setting("energy_max"), energy + regenerated), updated_at + regenerated * regen_seconds


async def get_player_energy_display(pool: asyncpg.Pool, user_id: int) -> int:
    row = await pool.fetchrow("SELECT energy, energy_updated_at FROM players WHERE user_id = $1", user_id)
    if not row:
        return setting("energy_max")
    energy, _ = _regen_energy(row["energy"], row["energy_updated_at"], time.time())
    return energy


async def ensure_player_name(conn: asyncpg.Connection, user_id: int, name: str) -> None:
    await conn.execute(
        """
        INSERT INTO players (user_id, name, power) VALUES ($1, $2, 0)
        ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name
        """,
        user_id, name,
    )


async def set_player_cooldown(conn: asyncpg.Connection, user_id: int, name: str, field: str, timestamp: float) -> None:
    if field not in ("last_open", "last_class", "last_casino"):
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


async def add_chat_money(
    conn: asyncpg.Connection, chat_id: int, user_id: int, name: str, amount: int
) -> None:
    await conn.execute(
        """
        INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
        VALUES ($1, $2, $3, $4, 0)
        ON CONFLICT (chat_id, user_id) DO UPDATE SET
            name = EXCLUDED.name, money = chat_profiles.money + EXCLUDED.money
        """,
        chat_id, user_id, name, amount,
    )


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


class TrackChatMiddleware(BaseMiddleware):
    """Держит bot_chats в актуальном состоянии по любой активности в группе.

    my_chat_member срабатывает только в момент добавления бота, поэтому группы,
    где бот оказался раньше (или чьё событие потерялось), иначе выпадают из рассылок.
    """

    async def __call__(self, handler, event: Message, data):
        pool = data.get("pool")
        if pool is not None and event.chat.type in ("group", "supergroup"):
            await pool.execute(
                """
                INSERT INTO bot_chats (chat_id, title) VALUES ($1, $2)
                ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title
                """,
                event.chat.id, event.chat.title or "группа",
            )
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
                await notify_admin(
                    bot, pool,
                    f"<b>👑 Отложенное уведомление о 1% доставлено</b>\n"
                    f"Группа: {html.escape(chat['chat_title'])}\n"
                    f"Владелец: {html.escape(event.from_user.full_name)}",
                )
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


class AddMob(StatesGroup):
    photo = State()
    name = State()
    power = State()
    money = State()


class EditMob(StatesGroup):
    waiting_value = State()


class EditSetting(StatesGroup):
    waiting_value = State()


class EditDrop(StatesGroup):
    waiting_value = State()


# ── Клавиатуры ───────────────────────────────────────────────────────────

def admin_menu_text() -> str:
    return "<b>⚙️ Админ-панель</b>"


def admin_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🐷 Копилка", callback_data="piggy:menu")
    kb.button(text="🎓 Класс", callback_data="class:menu")
    kb.button(text="🐗 Мобы", callback_data="mob:menu")
    kb.button(text="🌐 Все группы", callback_data="chats:menu")
    kb.button(text="📣 Разослать рекламу", callback_data="admin:promo")
    kb.button(text="🎁 Запустить аирдроп", callback_data="admin:airdrop")
    kb.button(text="🎁 Дроп аирдропов", callback_data="drop:menu")
    kb.button(text="⚙️ Настройки", callback_data="settings:menu")
    kb.adjust(1)
    return kb.as_markup()


def admin_airdrop_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎲 Случайный", callback_data="airdrop:run:random")
    for code, label, _weight, icon in AIRDROP_TIERS:
        kb.button(text=f"{icon} {label}", callback_data=f"airdrop:run:{code}")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def drop_menu_kb():
    kb = InlineKeyboardBuilder()
    for code, label, _weight, icon in AIRDROP_TIERS:
        kb.button(text=f"{icon} {label} — 1 к {airdrop_one_in(code)}", callback_data=f"drop:tier:{code}")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def drop_line_summary(tier: str, kind: str) -> str:
    lo, hi, chance = airdrop_line(tier, kind)
    if chance <= 0:
        return "выкл"
    if AIRDROP_KIND_HAS_RANGE[kind]:
        return f"{lo}–{hi} · {fmt_chance(chance)}"
    return fmt_chance(chance)


def drop_tier_text(tier: str) -> str:
    lines = [
        f"<b>{AIRDROP_TIER_ICONS[tier]} {AIRDROP_TIER_LABELS[tier]} — содержимое дропа</b>",
        "",
        f"<b>Как часто выпадает тир:</b> 1 к {airdrop_one_in(tier)}",
        "",
    ]
    for kind, label, _has_range in AIRDROP_KINDS:
        lines.append(f"<b>{label}:</b> {drop_line_summary(tier, kind)}")
    lines += [
        "",
        "Каждая строка проверяется своим шансом отдельно. Если не выпала ни одна, "
        "игрок всё равно получает самую вероятную из них — пустых дропов не бывает.",
        "",
        "Нажми на строку, чтобы изменить.",
    ]
    return "\n".join(lines)


def drop_tier_kb(tier: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🎲 Частота тира: 1 к {airdrop_one_in(tier)}", callback_data=f"drop:weight:{tier}")
    for kind, label, _has_range in AIRDROP_KINDS:
        kb.button(text=f"{label}: {drop_line_summary(tier, kind)}", callback_data=f"drop:line:{tier}:{kind}")
    kb.button(text="⬅️ Назад", callback_data="drop:menu")
    kb.adjust(1)
    return kb.as_markup()


def drop_line_kb(tier: str, kind: str):
    kb = InlineKeyboardBuilder()
    if AIRDROP_KIND_HAS_RANGE[kind]:
        kb.button(text="🔢 Диапазон", callback_data=f"drop:edit:{tier}:{kind}:range")
    kb.button(text="🎯 Шанс", callback_data=f"drop:edit:{tier}:{kind}:chance")
    kb.button(text="⬅️ Назад", callback_data=f"drop:tier:{tier}")
    kb.adjust(1)
    return kb.as_markup()


def settings_menu_kb():
    kb = InlineKeyboardBuilder()
    for section, label in SETTINGS_SECTIONS.items():
        kb.button(text=label, callback_data=f"settings:section:{section}")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def settings_section_kb(section: str):
    kb = InlineKeyboardBuilder()
    for key, (sect, label, unit, _default, _minimum) in SETTINGS_SPEC.items():
        if sect == section:
            kb.button(text=f"{label}: {setting(key)} {unit}", callback_data=f"settings:edit:{key}")
    kb.button(text="⬅️ Назад", callback_data="settings:menu")
    kb.adjust(1)
    return kb.as_markup()


def settings_section_text(section: str) -> str:
    lines = [f"<b>{SETTINGS_SECTIONS[section]} — настройки</b>", ""]
    for key, (sect, label, unit, default, _minimum) in SETTINGS_SPEC.items():
        if sect == section:
            mark = "" if setting(key) == default else f" (по умолчанию {default})"
            lines.append(f"<b>{label}:</b> {setting(key)} {unit}{mark}")
    lines.append("")
    lines.append("Нажми на параметр, чтобы изменить.")
    return "\n".join(lines)


def chats_list_kb(chats: list[dict]):
    kb = InlineKeyboardBuilder()
    for chat in chats:
        link = group_link(chat["chat_id"])
        if link:
            kb.button(text=chat["title"], url=link)
        else:
            kb.button(text=chat["title"], callback_data=f"chats:info:{chat['chat_id']}")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
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


def mob_menu_text(mobs_total: int) -> str:
    return f"<b>🐗 Мобы\n\nМобов: {mobs_total}</b>"


def mob_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список мобов", callback_data="mob:list")
    kb.button(text="➕ Добавить моба", callback_data="mob:add")
    kb.button(text="✏️ Изменить моба", callback_data="mob:edit")
    kb.button(text="🗑 Удалить моба", callback_data="mob:remove")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def mob_edit_fields_kb(mob_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Фото", callback_data=f"mob:editfield:{mob_id}:photo_id")
    kb.button(text="✏️ Название", callback_data=f"mob:editfield:{mob_id}:name")
    kb.button(text="⚔️ Сила", callback_data=f"mob:editfield:{mob_id}:power")
    kb.button(text="💰 Награда", callback_data=f"mob:editfield:{mob_id}:money")
    kb.button(text="⬅️ Назад", callback_data="mob:edit")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def mob_caption(mob: dict, prefix: str) -> str:
    return (
        f"<b>{prefix}\n\n"
        f"🐗 {html.escape(mob['name'])}\n"
        f"⚔️ Сила: {mob['power']}\n"
        f"<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Награда за убийство: {mob['money']}</b>"
    )


def group_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎰 Открыть копилку", callback_data="group:open")
    kb.button(text="🎓 Крутить класс", callback_data="group:class")
    kb.button(text="⚔️ Сразиться с мобом", callback_data="group:battle")
    kb.button(text="🏆 Топ силы", callback_data="group:top")
    kb.adjust(1)
    return kb.as_markup()


def card_caption(card: dict, prefix: str) -> str:
    return (
        f"<b>{prefix}\n\n"
        f"🃏 {card_title(card)}</b>\n"
        f"{rarity_display(card['rarity'])}\n"
        f"<b>⚔️ Сила: {card['power']}\n"
        f"<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Денег: {card['money']}</b>"
    )


def class_card_caption(card: dict, prefix: str) -> str:
    return (
        f"<b>{prefix}\n\n"
        f"🃏 {card_title(card)}</b>\n"
        f"{rarity_display(card['rarity'])}\n"
        f"<b>⚔️ Сила: {card['power']}</b>"
    )


# ── Игровая логика ───────────────────────────────────────────────────────

async def reply_spin_result(message: Message, photos: list[str], text: str) -> None:
    """Результат крутки одним сообщением: фото, альбом на ×2/×3 или просто текст.

    У альбома подпись может быть только у первого элемента — туда и уходит
    описание всех выпавших карточек.
    """
    if not photos:
        await message.reply(text)
    elif len(photos) == 1:
        await message.reply_photo(photos[0], caption=text)
    else:
        media = [InputMediaPhoto(media=photos[0], caption=text)]
        media += [InputMediaPhoto(media=photo) for photo in photos[1:]]
        await message.reply_media_group(media)


def cards_summary(cards: list[dict], with_money: bool) -> str:
    """Список выпавших карточек — по строке на каждую."""
    lines = []
    for card in cards:
        line = f"<b>🃏 {card_title(card)}</b>\n{rarity_display(card['rarity'])}\n<b>⚔️ +{card['power']} силы"
        if with_money:
            line += (
                f" · <tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> "
                f"+{card['money']} монет"
            )
        lines.append(line + "</b>")
    return "\n\n".join(lines)


async def perform_open(pool: asyncpg.Pool, chat_id: int, user) -> tuple[list[str], str]:
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            player_row = await conn.fetchrow(
                "SELECT last_open FROM players WHERE user_id = $1 FOR UPDATE", user.id
            )
            last_open = player_row["last_open"] if player_row else 0
            remaining = setting("piggy_cooldown_min") * 60 - (now - last_open)
            if remaining > 0:
                minutes, seconds = divmod(int(remaining), 60)
                return [], f"<b>⏳ Копилка ещё не наполнилась. Попробуй через {minutes} мин {seconds} сек.</b>"

            multiplier = roll_spin_multiplier()
            cards = []
            for _ in range(multiplier):
                card = await draw_random_card(conn)
                if card is None:
                    return [], "<b>🕳 Копилка пока пуста — админ ещё не добавил карточки.</b>"
                cards.append(card)

            total_power = sum(card["power"] for card in cards)
            total_money = sum(card["money"] for card in cards)

            await conn.execute(
                """
                INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
                VALUES ($1, $2, $3, $4, 1)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    money = chat_profiles.money + EXCLUDED.money,
                    opens = chat_profiles.opens + 1
                """,
                chat_id, user.id, user.full_name, total_money,
            )
            await add_player_power(conn, user.id, user.full_name, total_power)
            await set_player_cooldown(conn, user.id, user.full_name, "last_open", now)
            for card in cards:
                await maybe_update_best_card(conn, user.id, card)
                await add_to_inventory(conn, user.id, "card", card)

            # Реферальные 1% владельцу группы — прибавляются прямо к его основным
            # деньгам (не отдельным счётчиком), молча, без уведомлений.
            referral_amount = round(total_money * 0.01)
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
        f"{multiplier_banner(multiplier)}"
        f"<b>🎉 <a href='tg://user?id={user.id}'>{html.escape(user.full_name)}</a> крутит копилку!</b>\n\n"
        f"{cards_summary(cards, with_money=True)}"
    )
    if multiplier > 1:
        caption += (
            f"\n\n<b>Итого: ⚔️ +{total_power} силы · "
            f"<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> +{total_money} монет</b>"
        )
    return [card["photo_id"] for card in cards], caption


async def perform_class(pool: asyncpg.Pool, chat_id: int, user) -> tuple[list[str], str]:
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            profile = await conn.fetchrow(
                "SELECT money FROM chat_profiles WHERE chat_id = $1 AND user_id = $2 FOR UPDATE",
                chat_id, user.id,
            )
            balance = profile["money"] if profile else 0

            player_row = await conn.fetchrow(
                "SELECT last_class FROM players WHERE user_id = $1 FOR UPDATE", user.id
            )
            last_class = player_row["last_class"] if player_row else 0

            remaining = setting("class_cooldown_min") * 60 - (now - last_class)
            if remaining > 0:
                minutes, seconds = divmod(int(remaining), 60)
                return [], f"<b>⏳ Класс ещё не готов. Попробуй через {minutes} мин {seconds} сек.</b>"

            if balance < setting("class_spin_cost"):
                return [], f"<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Не хватает монет. Нужно {setting('class_spin_cost')}, у тебя {balance}.</b>"

            multiplier = roll_spin_multiplier()
            cards = []
            for _ in range(multiplier):
                card = await draw_random_class_card(conn)
                if card is None:
                    return [], "<b>🕳 Класс пока пуст — админ ещё не добавил карточки.</b>"
                cards.append(card)

            total_power = sum(card["power"] for card in cards)

            await conn.execute(
                """
                INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
                VALUES ($1, $2, $3, $4, 0)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    money = chat_profiles.money + EXCLUDED.money
                """,
                chat_id, user.id, user.full_name, -setting("class_spin_cost"),
            )
            await add_player_power(conn, user.id, user.full_name, total_power)
            await set_player_cooldown(conn, user.id, user.full_name, "last_class", now)
            for card in cards:
                await maybe_update_best_class(conn, user.id, card)
                await add_to_inventory(conn, user.id, "class", card)

    caption = (
        f"{multiplier_banner(multiplier)}"
        f"<b>🎓 <a href='tg://user?id={user.id}'>{html.escape(user.full_name)}</a> крутит класс!</b>\n\n"
        f"{cards_summary(cards, with_money=False)}\n\n"
    )
    if multiplier > 1:
        caption += f"<b>Итого: ⚔️ +{total_power} силы · </b>"
    caption += (
        f"<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> "
        f"−{setting('class_spin_cost')} монет</b>"
    )
    return [card["photo_id"] for card in cards], caption


# ── Казино ───────────────────────────────────────────────────────────────

# Крутим настоящую слот-машину Telegram: бот шлёт 🎰, а в ответе приходит
# dice.value 1–64 — это три барабана, записанные числом по основанию 4.
SLOT_SYMBOLS = ("bar", "grape", "lemon", "seven")
SLOT_LABELS = {"bar": "BAR", "grape": "🍇", "lemon": "🍋", "seven": "7️⃣"}

# Все множители — в сотых, чтобы считать выплату на целых числах.
# Лесенка: чем «старше» символ, тем слабее он поодиночке и тем жирнее тройка.
SLOT_SINGLE = {"grape": 15, "lemon": 20, "bar": 15, "seven": 10}
SLOT_PAIR = {"grape": 100, "lemon": 100, "bar": 53, "seven": 20}
SLOT_TRIPLE = {"grape": 167, "lemon": 233, "bar": 333, "seven": 667}

# Барабаны крутятся у игрока около двух секунд. Значение бот знает сразу,
# поэтому ждём анимацию — иначе результат приходит раньше, чем слот встанет.
SLOT_ANIMATION_SECONDS = 2

# Лимит ставок: сколько их разрешено за это окно, задаётся в настройках.
CASINO_WINDOW_SECONDS = 60


def decode_slot(value: int) -> tuple[str, str, str]:
    v = value - 1
    return SLOT_SYMBOLS[v % 4], SLOT_SYMBOLS[(v // 4) % 4], SLOT_SYMBOLS[(v // 16) % 4]


def slot_multiplier(reels: tuple[str, str, str]) -> int:
    """Множитель ставки в сотых: 100 = вернуть ставку."""
    counts = {symbol: reels.count(symbol) for symbol in set(reels)}
    top = max(counts, key=counts.get)
    if counts[top] == 3:
        return SLOT_TRIPLE[top]
    if counts[top] == 2:
        odd = next(symbol for symbol in reels if symbol != top)
        return SLOT_PAIR[top] + SLOT_SINGLE[odd]
    return sum(SLOT_SINGLE[symbol] for symbol in reels)


async def casino_take_bet(pool: asyncpg.Pool, chat_id: int, user, bet: int) -> str | None:
    """Проверяет ставку и списывает её. Возвращает текст ошибки или None."""
    now = time.time()
    min_bet = max(1, setting("casino_min_bet"))
    max_bet = setting("casino_max_bet")
    if bet < min_bet:
        return f"<b>🎰 Минимальная ставка — {min_bet} {COIN_EMOJI}</b>"
    if max_bet and bet > max_bet:
        return f"<b>🎰 Максимальная ставка — {max_bet} {COIN_EMOJI}</b>"

    async with pool.acquire() as conn:
        async with conn.transaction():
            profile = await conn.fetchrow(
                "SELECT money FROM chat_profiles WHERE chat_id = $1 AND user_id = $2 FOR UPDATE",
                chat_id, user.id,
            )
            balance = profile["money"] if profile else 0

            player_row = await conn.fetchrow(
                "SELECT last_casino, casino_spins FROM players WHERE user_id = $1 FOR UPDATE", user.id
            )
            window_start = player_row["last_casino"] if player_row else 0
            spins = player_row["casino_spins"] if player_row else 0
            if now - window_start >= CASINO_WINDOW_SECONDS:
                window_start, spins = now, 0

            limit = max(1, setting("casino_spins_per_min"))
            if spins >= limit:
                wait = int(CASINO_WINDOW_SECONDS - (now - window_start)) + 1
                return f"<b>⏳ Не больше {limit} ставок в минуту. Подожди {wait} сек.</b>"

            if balance < bet:
                return f"<b>{COIN_EMOJI} Не хватает монет. Ставка {bet}, у тебя {balance}.</b>"

            await add_chat_money(conn, chat_id, user.id, user.full_name, -bet)
            await conn.execute(
                """
                INSERT INTO players (user_id, name, last_casino, casino_spins)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    last_casino = EXCLUDED.last_casino,
                    casino_spins = EXCLUDED.casino_spins
                """,
                user.id, user.full_name, window_start, spins + 1,
            )
    return None


async def casino_refund(pool: asyncpg.Pool, chat_id: int, user, bet: int) -> None:
    """Возврат ставки, если слот не удалось прокрутить."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await add_chat_money(conn, chat_id, user.id, user.full_name, bet)
            await conn.execute(
                "UPDATE players SET casino_spins = GREATEST(casino_spins - 1, 0) WHERE user_id = $1",
                user.id,
            )


async def casino_pay_out(pool: asyncpg.Pool, chat_id: int, user, bet: int, value: int) -> str:
    reels = decode_slot(value)
    multiplier = slot_multiplier(reels)
    payout = bet * multiplier // 100

    if payout:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await add_chat_money(conn, chat_id, user.id, user.full_name, payout)

    if reels[0] == reels[1] == reels[2]:
        banner = "🎉 <b>ДЖЕКПОТ!</b>\n\n" if reels[0] == "seven" else "✨ <b>ТРОЙКА!</b>\n\n"
    else:
        banner = ""
    combo = " ".join(SLOT_LABELS[symbol] for symbol in reels)
    return (
        f"{banner}"
        f"<b>Вам выпало {combo} (×{multiplier / 100:g})</b>\n\n"
        f"<b>Ваш выигрыш {payout} {COIN_EMOJI}</b>"
    )


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
                        f"<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Украдено: {amount}</b>"
                    )
                return (
                    f"<b>🤏 Частично удачная кража!\n\n"
                    f"{thief_mention} стащил немного у {target_mention}\n"
                    f"<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Украдено: {amount}</b>"
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
                f"<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Потеряно: {loss}</b>"
            )


async def perform_battle(pool: asyncpg.Pool, chat_id: int, user) -> tuple[str | None, str]:
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await ensure_player_name(conn, user.id, user.full_name)
            row = await conn.fetchrow(
                "SELECT power, energy, energy_updated_at, last_battle FROM players WHERE user_id = $1 FOR UPDATE",
                user.id,
            )

            remaining = setting("battle_cooldown_min") * 60 - (now - row["last_battle"])
            if remaining > 0:
                minutes, seconds = divmod(int(remaining), 60)
                return None, f"<b>⏳ Бой ещё не готов. Попробуй через {minutes} мин {seconds} сек.</b>"

            mob = await conn.fetchrow("SELECT name, power, money, photo_id FROM mobs ORDER BY random() LIMIT 1")
            if mob is None:
                return None, "<b>🐗 Мобы ещё не добавлены — админ пока не заселил арену.</b>"

            energy, energy_updated_at = _regen_energy(row["energy"], row["energy_updated_at"], now)
            cost = random.randint(setting("battle_energy_min"), setting("battle_energy_max"))
            if energy < cost:
                return None, (
                    f"<b>🔋 Не хватает энергии. Нужно {cost}, у тебя {energy}/{setting('energy_max')}.\n"
                    "Восстанавливается 1⚡ раз в 10 минут.</b>"
                )
            energy -= cost

            player_power = row["power"]
            mob_power = mob["power"]
            win_chance = 1.0 if player_power >= mob_power else player_power / mob_power
            won = random.random() < win_chance

            await conn.execute(
                "UPDATE players SET energy = $2, energy_updated_at = $3, last_battle = $4 WHERE user_id = $1",
                user.id, energy, energy_updated_at, now,
            )

            if won:
                await conn.execute(
                    """
                    INSERT INTO chat_profiles (chat_id, user_id, name, money, opens)
                    VALUES ($1, $2, $3, $4, 0)
                    ON CONFLICT (chat_id, user_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        money = chat_profiles.money + EXCLUDED.money
                    """,
                    chat_id, user.id, user.full_name, mob["money"],
                )

    mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.full_name)}</a>"
    mob_name = html.escape(mob["name"])
    if won:
        caption = (
            f"<b>⚔️ {mention} сразился с «{mob_name}» и одержал победу!\n\n"
            f"<tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Награда: {mob['money']} монет\n"
            f"🔋 Потрачено энергии: {cost} (осталось {energy}/{setting('energy_max')})</b>"
        )
    else:
        caption = (
            f"<b>💀 {mention} сразился с «{mob_name}» и проиграл...\n\n"
            f"🔋 Потрачено энергии: {cost} (осталось {energy}/{setting('energy_max')})</b>"
        )
    return mob["photo_id"], caption


# Лидерборды по валютам: код -> (подпись кнопки, заголовок, откуда берём число,
# чем помечаем значение, что писать, когда всё по нулям).
# Сила и токены у игрока общие на все чаты, монеты — свои в каждом; ранжируем
# в любом случае только участников этого чата.
TOP_METRICS = {
    "power": ("⚔️ Сила", "🏆 Топ силы чата", "pl.power", "⚔️",
              "📭 Пока никто не крутил копилку в этом чате."),
    "money": ("🪙 Монеты", "💰 Топ богачей чата", "cp.money", COIN_EMOJI,
              "📭 Пока ни у кого нет монет в этом чате."),
    "tokens": ("🐟 Токены", "🐟 Топ по токенам", "pl.tokens", TOKEN_EMOJI,
               "📭 Пока ни у кого нет токенов."),
}


def top_kb(metric: str, owner_id: int):
    kb = InlineKeyboardBuilder()
    for code, (label, *_rest) in TOP_METRICS.items():
        mark = "• " if code == metric else ""
        kb.button(text=f"{mark}{label}", callback_data=f"top:{code}:{owner_id}")
    kb.adjust(3)
    return kb.as_markup()


async def perform_top(pool: asyncpg.Pool, chat_id: int, metric: str = "power") -> str:
    _label, title, column, mark, empty = TOP_METRICS[metric]
    rows = await pool.fetch(
        f"""
        SELECT cp.name, {column} AS value
        FROM chat_profiles cp
        JOIN players pl ON pl.user_id = cp.user_id
        WHERE cp.chat_id = $1
        ORDER BY {column} DESC
        LIMIT $2
        """,
        chat_id, TOP_LIMIT,
    )
    if not rows or rows[0]["value"] <= 0:
        return f"<b>{empty}</b>"

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"<b>{title}", "━━━━━━━━━━━━━━", ""]
    for i, row in enumerate(rows):
        name = html.escape(row["name"])
        place = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{place} {name}  {mark} {row['value']}")
    return "\n".join(lines) + "</b>"


async def safe_edit_text(message: Message, text: str, reply_markup=None) -> None:
    """edit_text не может превратить фото-сообщение в текстовое — в этом случае
    пересоздаём сообщение вместо падения (отсюда были нерабочие кнопки)."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await message.delete()
        await message.answer(text, reply_markup=reply_markup)


# ── Удаление своих данных (/cleardata) ───────────────────────────────────

CLEARDATA_COOLDOWN_SECONDS = 3 * 24 * 60 * 60


async def cleardata_remaining(pool: asyncpg.Pool, user_id: int) -> float:
    last = await pool.fetchval("SELECT cleared_at FROM cleardata_log WHERE user_id = $1", user_id)
    if not last:
        return 0.0
    return max(0.0, CLEARDATA_COOLDOWN_SECONDS - (time.time() - last))


async def player_chat_ids(pool: asyncpg.Pool, user_id: int) -> list[int]:
    rows = await pool.fetch("SELECT chat_id FROM chat_profiles WHERE user_id = $1", user_id)
    return [row["chat_id"] for row in rows]


async def player_is_in_any_top(pool: asyncpg.Pool, user_id: int, chat_ids: list[int]) -> bool:
    """Попал ли игрок хоть в один лидерборд хоть одного своего чата."""
    for chat_id in chat_ids:
        for _label, _title, column, _mark, _empty in TOP_METRICS.values():
            hit = await pool.fetchval(
                f"""
                SELECT 1 FROM (
                    SELECT cp.user_id
                    FROM chat_profiles cp
                    JOIN players pl ON pl.user_id = cp.user_id
                    WHERE cp.chat_id = $1 AND {column} > 0
                    ORDER BY {column} DESC
                    LIMIT {TOP_LIMIT}
                ) top
                WHERE top.user_id = $2
                """,
                chat_id, user_id,
            )
            if hit:
                return True
    return False


async def wipe_player_data(pool: asyncpg.Pool, user_id: int) -> None:
    """Стирает игрока целиком. Клан, который он создал, удаляется вместе с ним
    (участники отвязываются каскадом) — иначе остался бы клан без владельца."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM clans WHERE creator_id = $1", user_id)
            await conn.execute("DELETE FROM clan_members WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM inventory WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM member_tags WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM chat_profiles WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM players WHERE user_id = $1", user_id)
            await conn.execute(
                """
                INSERT INTO cleardata_log (user_id, cleared_at) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET cleared_at = EXCLUDED.cleared_at
                """,
                user_id, time.time(),
            )


async def announce_wipe(bot: Bot, chat_ids: list[int], name: str) -> None:
    text = (
        f"<b>🧹 {html.escape(name)} стёр все свои данные.</b>\n"
        "<b>Сила, монеты, токены и инвентарь обнулены.</b>"
    )
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text)
        except TelegramAPIError as error:
            logging.warning("Не удалось сообщить об очистке в чат %s: %s", chat_id, error)


def cleardata_kb(step: int):
    """Два шага подтверждения. На втором кнопка согласия стоит в другом месте,
    чтобы её не нажали второй раз по инерции."""
    kb = InlineKeyboardBuilder()
    if step == 1:
        kb.button(text="❌ Нет, оставить", callback_data="clear:cancel")
        kb.button(text="✅ Да, стереть", callback_data="clear:step2")
        kb.button(text="❌ Нет, оставить", callback_data="clear:cancel")
    else:
        kb.button(text="↩️ Передумал", callback_data="clear:cancel")
        kb.button(text="↩️ Передумал", callback_data="clear:cancel")
        kb.button(text="🗑 Стереть навсегда", callback_data="clear:confirm")
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command("cleardata", ignore_case=True), F.chat.type == "private")
async def cmd_cleardata(message: Message, state: FSMContext, pool: asyncpg.Pool):
    await state.clear()
    remaining = await cleardata_remaining(pool, message.from_user.id)
    if remaining > 0:
        hours, minutes = divmod(int(remaining) // 60, 60)
        days, hours = divmod(hours, 24)
        await message.answer(
            "<b>⏳ Стирать данные можно раз в 3 дня.</b>\n"
            f"<b>Осталось ждать: {days} д {hours} ч {minutes} мин</b>"
        )
        return
    await message.answer(
        "<b>🗑 Удаление данных</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>Будет стёрто НАВСЕГДА:</b>\n"
        "<b>• сила, монеты и токены во всех чатах</b>\n"
        "<b>• инвентарь и лучшие карточки</b>\n"
        "<b>• клан, если ты его создал — вместе с участниками</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>Отменить это будет нельзя. Уверен?</b>",
        reply_markup=cleardata_kb(1),
    )


@router.callback_query(F.data == "clear:cancel", F.message.chat.type == "private")
async def cleardata_cancel_cb(callback: CallbackQuery):
    await safe_edit_text(callback.message, "<b>✅ Отменено — данные на месте.</b>")
    await callback.answer()


@router.callback_query(F.data == "clear:step2", F.message.chat.type == "private")
async def cleardata_step2_cb(callback: CallbackQuery):
    await safe_edit_text(
        callback.message,
        "<b>🗑 Последнее подтверждение</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>Прогресс не восстановить — резервных копий нет.</b>\n"
        "<b>Стереть данные снова можно будет только через 3 дня.</b>\n"
        "━━━━━━━━━━━━━━\n"
        "<b>Точно стираем?</b>",
        reply_markup=cleardata_kb(2),
    )
    await callback.answer()


@router.callback_query(F.data == "clear:confirm", F.message.chat.type == "private")
async def cleardata_confirm_cb(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool):
    user = callback.from_user
    if await cleardata_remaining(pool, user.id) > 0:
        await callback.answer("Стирать данные можно раз в 3 дня.", show_alert=True)
        return

    chat_ids = await player_chat_ids(pool, user.id)
    was_in_top = await player_is_in_any_top(pool, user.id, chat_ids)
    await wipe_player_data(pool, user.id)

    if was_in_top and chat_ids:
        spawn_background_task(announce_wipe(bot, chat_ids, user.full_name))

    await safe_edit_text(
        callback.message,
        "<b>🗑 Данные стёрты.</b>\n\n"
        "<b>Ты начинаешь с нуля — крути /AngryOpen в любой группе.</b>",
    )
    await callback.answer("Готово")


# ── Индекс карточек ──────────────────────────────────────────────────────

INDEX_PAGE_SIZE = 8
INDEX_KINDS = {
    "item": ("🐷 Предметы", "🐷 Индекс предметов"),
    "class": ("🎓 Классы", "🎓 Индекс классов"),
}


async def owned_card_ids(pool: asyncpg.Pool, user_id: int, kind: str) -> set[int]:
    rows = await pool.fetch(
        "SELECT card_id FROM inventory WHERE user_id = $1 AND kind = $2 AND qty > 0",
        user_id, "card" if kind == "item" else "class",
    )
    return {row["card_id"] for row in rows}


async def index_text(pool: asyncpg.Pool, kind: str, page: int, user_id: int) -> tuple[str, int, int]:
    cards = await (list_cards(pool) if kind == "item" else list_class_cards(pool))
    # От слабых к сильным: сначала редкость, внутри неё — сила (id только чтобы
    # порядок не прыгал между страницами у одинаковых карточек).
    cards.sort(key=lambda card: (card["rarity"], card["power"], card["id"]))
    pages = max(1, (len(cards) + INDEX_PAGE_SIZE - 1) // INDEX_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    if not cards:
        return f"<b>{INDEX_KINDS[kind][1]}</b>\n\n<b>📭 Пока пусто.</b>", page, pages

    owned = await owned_card_ids(pool, user_id, kind)
    lines = [
        f"<b>{INDEX_KINDS[kind][1]} — открыто {len(owned)} из {len(cards)}</b>",
        "━━━━━━━━━━━━━━",
        "",
    ]
    chunk = cards[page * INDEX_PAGE_SIZE:(page + 1) * INDEX_PAGE_SIZE]
    for number, card in enumerate(chunk, start=page * INDEX_PAGE_SIZE + 1):
        # У невыбитых прячем название и цифры, но оставляем звёзды и птицу —
        # чтобы было видно, за чем именно охотиться.
        if card["id"] in owned:
            title = f"✅ {card_title(card)}"
            stats = f"⚔️ {card['power']}"
            if kind == "item":
                stats += f" · {COIN_EMOJI} {card['money']}"
        else:
            title = f"🔒 ??? ({bird_html(card['bird'])})"
            stats = "⚔️ ?"
            if kind == "item":
                stats += f" · {COIN_EMOJI} ?"
        lines.append(f"<b>{number}. {title}</b> {rarity_display(card['rarity'])}")
        lines.append(f"<b>     {stats}</b>")
    return "\n".join(lines), page, pages


def index_kb(kind: str, page: int, pages: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️", callback_data=f"index:{kind}:{max(0, page - 1)}")
    kb.button(text=f"{page + 1}/{pages}", callback_data="index:noop")
    kb.button(text="▶️", callback_data=f"index:{kind}:{min(pages - 1, page + 1)}")
    for code, (label, _title) in INDEX_KINDS.items():
        mark = "• " if code == kind else ""
        kb.button(text=f"{mark}{label}", callback_data=f"index:{code}:0")
    kb.adjust(3, 2)
    return kb.as_markup()


@router.message(Command("index", ignore_case=True), F.chat.type == "private")
async def cmd_index(message: Message, state: FSMContext, pool: asyncpg.Pool):
    await state.clear()
    text, page, pages = await index_text(pool, "item", 0, message.from_user.id)
    await message.answer(text, reply_markup=index_kb("item", page, pages))


@router.callback_query(F.data == "index:noop", F.message.chat.type == "private")
async def index_noop_cb(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("index:"), F.message.chat.type == "private")
async def index_page_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    _, kind, raw_page = callback.data.split(":")
    if kind not in INDEX_KINDS:
        await callback.answer("Неизвестный раздел", show_alert=True)
        return
    text, page, pages = await index_text(pool, kind, int(raw_page), callback.from_user.id)
    markup = index_kb(kind, page, pages)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "not modified" in str(error).lower():
            pass  # ткнули в край списка или в уже открытую вкладку
        else:
            # Индекс могли открыть кнопкой из профиля, а профиль — это фото:
            # такое сообщение edit_text превратить в текстовое не может.
            await safe_edit_text(callback.message, text, reply_markup=markup)
    await callback.answer()


# ── Приватные сообщения (админ-панель) ───────────────────────────────────

@router.message(Command("whoami", ignore_case=True), F.chat.type == "private")
async def cmd_whoami(message: Message, pool: asyncpg.Pool):
    admin_id = await get_admin_id(pool)
    user = message.from_user
    username = f"@{user.username}" if user.username else "— (username не задан)"
    await message.answer(
        f"<b>🪪 Твои данные</b>\n\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: {html.escape(username)}\n"
        f"Админ бота: {'да ✅' if user.id == admin_id else 'нет ❌'}"
    )


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start_private(message: Message, command: CommandObject, state: FSMContext, bot: Bot, pool: asyncpg.Pool):
    if (command.args or "") == "referral":
        me = await bot.get_me()
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Добавить бота в группу", url=f"https://t.me/{me.username}?startgroup=ref")
        kb.adjust(1)
        await message.answer(REFERRAL_HOWTO_TEXT, reply_markup=kb.as_markup())
        return

    admin_id = await get_admin_id(pool)
    is_admin = message.from_user.id == admin_id
    if is_admin:
        await state.clear()
    await send_own_profile(message, pool, is_admin=is_admin)


@router.callback_query(F.data == "admin:menu", IsAdminPrivate())
async def admin_menu_cb(callback: CallbackQuery):
    await safe_edit_text(callback.message, admin_menu_text(), reply_markup=admin_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "chats:menu", IsAdminPrivate())
async def chats_menu_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    chats = await get_active_chats(pool)
    if not chats:
        await callback.answer("Бот пока не состоит ни в одной группе", show_alert=True)
        return
    await safe_edit_text(
        callback.message,
        f"<b>🌐 Группы бота ({len(chats)})</b>\n\nНажми на группу, чтобы перейти в неё.",
        reply_markup=chats_list_kb(chats),
    )
    await callback.answer()


@router.callback_query(F.data == "admin:promo", IsAdminPrivate())
async def admin_promo_cb(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool):
    chats = await get_active_chats(pool)
    if not chats:
        await callback.answer("Бот пока не состоит ни в одной группе", show_alert=True)
        return
    await callback.answer(f"Рассылаю рекламу в {len(chats)} чат(ов)…")
    me = await bot.get_me()
    spawn_background_task(broadcast_referral_promo(bot, pool, me.username))


@router.callback_query(F.data == "admin:airdrop", IsAdminPrivate())
async def admin_airdrop_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(
        callback.message,
        "<b>🎁 Запустить аирдроп</b>\n\n"
        "Выбери редкость — дроп такой редкости уйдёт во все группы сразу. "
        "«Случайный» разыгрывает тир по обычным шансам, как в автоматическом цикле.",
        reply_markup=admin_airdrop_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("airdrop:run:"), IsAdminPrivate())
async def admin_airdrop_run_cb(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool):
    tier = callback.data.split(":")[2]
    if tier != "random" and tier not in AIRDROP_TIER_LABELS:
        await callback.answer("Неизвестная редкость", show_alert=True)
        return
    chats = await get_active_chats(pool)
    if not chats:
        await callback.answer("Бот пока не состоит ни в одной группе", show_alert=True)
        return
    name = "случайной редкости" if tier == "random" else AIRDROP_TIER_LABELS[tier].lower()
    await callback.answer(
        f"Запускаю аирдроп {name} в {len(chats)} чат(ов). Дропы появятся в течение "
        f"{AIRDROP_CHAT_STAGGER_MIN}-{AIRDROP_CHAT_STAGGER_MAX} сек."
    )
    spawn_background_task(spawn_airdrop_cycle(bot, pool, None if tier == "random" else tier))


@router.callback_query(F.data == "drop:menu", IsAdminPrivate())
async def drop_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(
        callback.message,
        "<b>🎁 Дроп аирдропов</b>\n\n"
        "Выбери тир — внутри настраивается, как часто он выпадает и что в нём лежит.",
        reply_markup=drop_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("drop:tier:"), IsAdminPrivate())
async def drop_tier_cb(callback: CallbackQuery, state: FSMContext):
    tier = callback.data.split(":")[2]
    if tier not in AIRDROP_TIER_LABELS:
        await callback.answer("Неизвестный тир", show_alert=True)
        return
    await state.clear()
    await safe_edit_text(callback.message, drop_tier_text(tier), reply_markup=drop_tier_kb(tier))
    await callback.answer()


@router.callback_query(F.data.startswith("drop:line:"), IsAdminPrivate())
async def drop_line_cb(callback: CallbackQuery, state: FSMContext):
    _, _, tier, kind = callback.data.split(":")
    if tier not in AIRDROP_TIER_LABELS or kind not in AIRDROP_KIND_LABELS:
        await callback.answer("Неизвестная строка", show_alert=True)
        return
    await state.clear()
    lo, hi, chance = airdrop_line(tier, kind)
    text = [
        f"<b>{AIRDROP_KIND_LABELS[kind]}</b> в тире {AIRDROP_TIER_LABELS[tier].lower()}",
        "",
        f"<b>Шанс попасть в дроп:</b> {fmt_chance(chance)}",
    ]
    if AIRDROP_KIND_HAS_RANGE[kind]:
        text.append(f"<b>Сколько выдаётся:</b> {lo}–{hi}")
    text += ["", "Что меняем?"]
    await safe_edit_text(callback.message, "\n".join(text), reply_markup=drop_line_kb(tier, kind))
    await callback.answer()


@router.callback_query(F.data.startswith("drop:weight:"), IsAdminPrivate())
async def drop_weight_cb(callback: CallbackQuery, state: FSMContext):
    tier = callback.data.split(":")[2]
    if tier not in AIRDROP_TIER_LABELS:
        await callback.answer("Неизвестный тир", show_alert=True)
        return
    await state.set_state(EditDrop.waiting_value)
    await state.update_data(drop_tier=tier, drop_kind=None, drop_mode="weight")
    await safe_edit_text(
        callback.message,
        f"<b>🎲 Частота тира «{AIRDROP_TIER_LABELS[tier]}»</b>\n\n"
        f"Сейчас: 1 к {airdrop_one_in(tier)}\n"
        f"По умолчанию: 1 к {AIRDROP_TIER_ONE_IN[tier]}\n\n"
        "Введи число N — вес тира станет 1/N: чем больше N, тем реже он выпадает.\n"
        "Точная доля зависит и от остальных тиров, ведь они делят 100% между собой.",
        reply_markup=cancel_kb(f"drop:tier:{tier}"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("drop:edit:"), IsAdminPrivate())
async def drop_edit_cb(callback: CallbackQuery, state: FSMContext):
    _, _, tier, kind, mode = callback.data.split(":")
    if tier not in AIRDROP_TIER_LABELS or kind not in AIRDROP_KIND_LABELS:
        await callback.answer("Неизвестная строка", show_alert=True)
        return
    await state.set_state(EditDrop.waiting_value)
    await state.update_data(drop_tier=tier, drop_kind=kind, drop_mode=mode)
    lo, hi, chance = airdrop_line(tier, kind)
    if mode == "chance":
        body = (
            f"Сейчас: {fmt_chance(chance)}\n\n"
            "Введи шанс в процентах (например 40, 0.1 или 100).\n"
            "0 — выключить строку совсем."
        )
    else:
        body = f"Сейчас: {lo}–{hi}\n\nВведи диапазон в виде «10-20»:"
    await safe_edit_text(
        callback.message,
        f"<b>{AIRDROP_KIND_LABELS[kind]} — {AIRDROP_TIER_LABELS[tier].lower()} тир</b>\n\n{body}",
        reply_markup=cancel_kb(f"drop:line:{tier}:{kind}"),
    )
    await callback.answer()


@router.message(EditDrop.waiting_value, IsAdminPrivate())
async def drop_edit_value(message: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    tier, kind, mode = data["drop_tier"], data.get("drop_kind"), data["drop_mode"]
    raw = (message.text or "").strip()

    if mode == "weight":
        try:
            one_in = int(raw)
        except ValueError:
            await message.answer("<b>Нужно целое число. Попробуй ещё раз.</b>")
            return
        if one_in < 1:
            await message.answer("<b>Минимум 1. Попробуй ещё раз.</b>")
            return
        await save_airdrop_weight(pool, tier, one_in)
        done = f"✅ {AIRDROP_TIER_LABELS[tier]}: 1 к {one_in}"
    elif mode == "chance":
        try:
            percent = float(raw.replace(",", ".").rstrip("%").strip())
        except ValueError:
            await message.answer("<b>Нужно число в процентах, например 40 или 0.1. Попробуй ещё раз.</b>")
            return
        if not 0 <= percent <= 100:
            await message.answer("<b>Шанс должен быть от 0 до 100. Попробуй ещё раз.</b>")
            return
        lo, hi, _chance = airdrop_line(tier, kind)
        await save_airdrop_line(pool, tier, kind, lo, hi, percent / 100)
        done = f"✅ {AIRDROP_KIND_LABELS[kind]}: {fmt_chance(percent / 100)}"
    else:
        parts = "".join(ch if ch.isdigit() else " " for ch in raw).split()
        if len(parts) != 2:
            await message.answer("<b>Введи диапазон в виде «10-20». Попробуй ещё раз.</b>")
            return
        lo, hi = int(parts[0]), int(parts[1])
        if lo > hi:
            lo, hi = hi, lo
        _lo, _hi, chance = airdrop_line(tier, kind)
        await save_airdrop_line(pool, tier, kind, lo, hi, chance)
        done = f"✅ {AIRDROP_KIND_LABELS[kind]}: {lo}–{hi}"

    await state.clear()
    await message.answer(f"<b>{done}</b>")
    await message.answer(drop_tier_text(tier), reply_markup=drop_tier_kb(tier))


@router.callback_query(F.data == "settings:menu", IsAdminPrivate())
async def settings_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit_text(
        callback.message,
        "<b>⚙️ Настройки</b>\n\nВыбери раздел, чтобы изменить параметры игры.",
        reply_markup=settings_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:section:"), IsAdminPrivate())
async def settings_section_cb(callback: CallbackQuery, state: FSMContext):
    section = callback.data.split(":")[2]
    if section not in SETTINGS_SECTIONS:
        await callback.answer("Неизвестный раздел", show_alert=True)
        return
    await state.clear()
    await safe_edit_text(
        callback.message, settings_section_text(section), reply_markup=settings_section_kb(section)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:edit:"), IsAdminPrivate())
async def settings_edit_cb(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":")[2]
    if key not in SETTINGS_SPEC:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return
    section, label, unit, default, minimum = SETTINGS_SPEC[key]
    await state.set_state(EditSetting.waiting_value)
    await state.update_data(setting_key=key)
    await safe_edit_text(
        callback.message,
        f"<b>{label}</b>\n\n"
        f"Сейчас: {setting(key)} {unit}\n"
        f"По умолчанию: {default} {unit}\n"
        f"Минимум: {minimum}\n\n"
        f"Введи новое значение ({unit}):",
        reply_markup=cancel_kb(f"settings:section:{section}"),
    )
    await callback.answer()


@router.message(EditSetting.waiting_value, IsAdminPrivate())
async def settings_edit_value(message: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    key = data["setting_key"]
    section, label, unit, _default, minimum = SETTINGS_SPEC[key]

    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("<b>Нужно целое число, попробуй ещё раз.</b>")
        return
    if value < minimum:
        await message.answer(f"<b>Значение не может быть меньше {minimum}. Попробуй ещё раз.</b>")
        return

    await save_setting(pool, key, value)
    await state.clear()
    await message.answer(f"<b>✅ {label}: {value} {unit}</b>")
    await message.answer(settings_section_text(section), reply_markup=settings_section_kb(section))


@router.callback_query(F.data.startswith("chats:info:"), IsAdminPrivate())
async def chats_info_cb(callback: CallbackQuery):
    chat_id = callback.data.split(":")[2]
    await callback.answer(
        f"ID чата: {chat_id}\n(обычная группа — прямая ссылка недоступна, только супергруппы)",
        show_alert=True,
    )


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
            f"<b>    ⚔️{card['power']} · <tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji>{card['money']}</b>"
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
    await message.answer("<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Введи количество денег (число)</b>", reply_markup=cancel_kb())


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


# — Мобы (для /angrybattle) —

@router.callback_query(F.data == "mob:menu", IsAdminPrivate())
async def mob_menu_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    total = await mobs_count(pool)
    await safe_edit_text(callback.message, mob_menu_text(total), reply_markup=mob_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "mob:cancel", IsAdminPrivate())
async def mob_cancel(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool):
    await state.clear()
    total = await mobs_count(pool)
    await safe_edit_text(callback.message, mob_menu_text(total), reply_markup=mob_menu_kb())
    await callback.answer("Отменено")


@router.callback_query(F.data == "mob:list", IsAdminPrivate())
async def mob_list_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    mobs = await list_mobs(pool)
    if not mobs:
        await callback.answer("Мобов пока нет", show_alert=True)
        return
    lines = ["<b>📋 Список мобов</b>", ""]
    for i, mob in enumerate(mobs, start=1):
        lines.append(
            f"<b>{i}. {html.escape(mob['name'])}</b>\n"
            f"<b>    ⚔️{mob['power']} · <tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji>{mob['money']}</b>"
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="mob:menu")
    await safe_edit_text(callback.message, "\n".join(lines), reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "mob:add", IsAdminPrivate())
async def mob_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddMob.photo)
    await safe_edit_text(callback.message, "<b>📸 Пришли фото моба</b>", reply_markup=cancel_kb("mob:cancel"))
    await callback.answer()


@router.message(AddMob.photo, IsAdminPrivate())
async def mob_add_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("<b>Пришли именно фото 🙂</b>")
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddMob.name)
    await message.answer("<b>✏️ Введи название моба</b>", reply_markup=cancel_kb("mob:cancel"))


@router.message(AddMob.name, IsAdminPrivate())
async def mob_add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("<b>Название не может быть пустым, попробуй ещё раз.</b>")
        return
    await state.update_data(name=name)
    await state.set_state(AddMob.power)
    await message.answer("<b>⚔️ Введи силу моба (число)</b>", reply_markup=cancel_kb("mob:cancel"))


@router.message(AddMob.power, IsAdminPrivate())
async def mob_add_power(message: Message, state: FSMContext):
    try:
        power = int((message.text or "").strip())
    except ValueError:
        await message.answer("<b>Нужно целое число, попробуй ещё раз.</b>")
        return
    await state.update_data(power=power)
    await state.set_state(AddMob.money)
    await message.answer("<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Введи награду монет за убийство (число)</b>", reply_markup=cancel_kb("mob:cancel"))


@router.message(AddMob.money, IsAdminPrivate())
async def mob_add_money(message: Message, state: FSMContext, pool: asyncpg.Pool):
    try:
        money = int((message.text or "").strip())
    except ValueError:
        await message.answer("<b>Нужно целое число, попробуй ещё раз.</b>")
        return

    data = await state.get_data()
    await add_mob(pool, data["name"], data["power"], money, data["photo_id"])
    await state.clear()

    mob = {"name": data["name"], "power": data["power"], "money": money}
    await message.answer_photo(data["photo_id"], caption=mob_caption(mob, "✅ Моб добавлен!"))
    total = await mobs_count(pool)
    await message.answer(mob_menu_text(total), reply_markup=mob_menu_kb())


@router.callback_query(F.data == "mob:edit", IsAdminPrivate())
async def mob_edit_list(callback: CallbackQuery, pool: asyncpg.Pool):
    mobs = await list_mobs(pool)
    if not mobs:
        await callback.answer("Мобов пока нет", show_alert=True)
        return
    await safe_edit_text(
        callback.message, "<b>✏️ Выбери моба для изменения:</b>", reply_markup=cards_pick_kb(mobs, "edit", "mob")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mob:edit:"), IsAdminPrivate())
async def mob_edit_pick(callback: CallbackQuery, pool: asyncpg.Pool):
    mob_id = int(callback.data.split(":")[2])
    mob = await get_mob(pool, mob_id)
    if not mob:
        await callback.answer("Моб не найден", show_alert=True)
        return
    await safe_edit_text(callback.message, mob_caption(mob, "Что изменить?"), reply_markup=mob_edit_fields_kb(mob_id))
    await callback.answer()


@router.callback_query(F.data.startswith("mob:editfield:"), IsAdminPrivate())
async def mob_editfield_start(callback: CallbackQuery, state: FSMContext):
    _, _, mob_id, field = callback.data.split(":")
    await state.set_state(EditMob.waiting_value)
    await state.update_data(mob_id=int(mob_id), field=field)
    await safe_edit_text(callback.message, MOB_FIELD_PROMPTS[field], reply_markup=cancel_kb("mob:cancel"))
    await callback.answer()


@router.message(EditMob.waiting_value, IsAdminPrivate())
async def mob_editfield_value(message: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    field = data["field"]
    mob_id = data["mob_id"]

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

    await update_mob_field(pool, mob_id, field, value)
    await state.clear()

    mob = await get_mob(pool, mob_id)
    await message.answer_photo(
        mob["photo_id"],
        caption=mob_caption(mob, "✅ Моб обновлён!"),
        reply_markup=mob_edit_fields_kb(mob_id),
    )


@router.callback_query(F.data == "mob:remove", IsAdminPrivate())
async def mob_remove_list(callback: CallbackQuery, pool: asyncpg.Pool):
    mobs = await list_mobs(pool)
    if not mobs:
        await callback.answer("Мобов пока нет", show_alert=True)
        return
    await safe_edit_text(
        callback.message, "<b>🗑 Выбери моба для удаления:</b>", reply_markup=cards_pick_kb(mobs, "remove", "mob")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mob:remove:"), IsAdminPrivate())
async def mob_remove_pick(callback: CallbackQuery, pool: asyncpg.Pool):
    mob_id = int(callback.data.split(":")[2])
    name = await delete_mob(pool, mob_id)
    await callback.answer(f"Удалено: {name}" if name else "Уже удалено")

    mobs = await list_mobs(pool)
    if mobs:
        await safe_edit_text(
            callback.message, "<b>🗑 Выбери моба для удаления:</b>", reply_markup=cards_pick_kb(mobs, "remove", "mob")
        )
    else:
        await safe_edit_text(callback.message, mob_menu_text(0), reply_markup=mob_menu_kb())


# — Профиль игрока —

def render_profile(
    name: str, power: int, money, tokens: int, energy: int, clan_name: str | None, best_class: dict | None
) -> str:
    lines = [f"<b>👤 {html.escape(name)}</b>", "━━━━━━━━━━━━━━", ""]
    if best_class:
        lines.append(f"<b>🎓 Лучший класс:</b> {card_title(best_class)}")
        lines.append(rarity_display(best_class["rarity"]))
    else:
        lines.append("<b>🎓 Лучший класс:</b> пока нет — крути /AngryClass!")
    lines += [
        "",
        f"<b>⚔️ Сила:</b> {power}",
        f"<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Монет:</b> {money}",
        f"<b><tg-emoji emoji-id='5233482504182212210'>🐟</tg-emoji> Токенов:</b> {tokens}",
        f"<b>🔋 Энергия:</b> {energy}/{setting('energy_max')}",
        f"<b>🏰 Клан:</b> {html.escape(clan_name) if clan_name else 'нет'}",
    ]
    return "\n".join(lines)


def profile_kb(is_admin: bool):
    """Кнопки под профилем в ЛС: инвентарь (Mini App) и, для админа, панель.
    Кнопку web_app Telegram разрешает только в личке — в группах её нет."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🎒 Инвентарь", web_app=WebAppInfo(url=WEBAPP_URL))
    builder.button(text="📖 Индекс", callback_data="index:item:0")
    if is_admin:
        builder.button(text="⚙️ Админ-панель", callback_data="admin:menu")
    builder.adjust(1)
    return builder.as_markup()


async def send_own_profile(message: Message, pool: asyncpg.Pool, is_admin: bool = False) -> None:
    user = message.from_user
    reply_markup = profile_kb(is_admin)
    summary = await get_player_summary(pool, user.id)
    if summary["chats"] == 0:
        await message.answer(
            "<b>👤 Профиль\n\n"
            "Ты пока не крутил копилку ни в одной группе.\n"
            "Добавь бота в чат и используй там 🎰 /AngryOpen!</b>",
            reply_markup=reply_markup,
        )
        return

    tokens = await get_player_tokens(pool, user.id)
    energy = await get_player_energy_display(pool, user.id)
    clan = await get_user_clan(pool, user.id)
    best_class = await get_player_best_class(pool, user.id)

    text = render_profile(
        user.full_name, summary["power"], summary["money"], tokens, energy,
        clan["name"] if clan else None, best_class,
    )
    if best_class:
        await message.answer_photo(best_class["photo_id"], caption=text, reply_markup=reply_markup)
    else:
        await message.answer(text, reply_markup=reply_markup)


@router.message(F.chat.type == "private")
async def show_profile(message: Message, pool: asyncpg.Pool):
    admin_id = await get_admin_id(pool)
    await send_own_profile(message, pool, is_admin=message.from_user.id == admin_id)


# ── Групповые команды ─────────────────────────────────────────────────────

GROUP_CHATS = F.chat.type.in_({"group", "supergroup"})
# У CallbackQuery нет .chat — чат лежит в .message, поэтому для кнопок нужен
# отдельный фильтр. С GROUP_CHATS он молча резолвился в None, и хендлер вообще
# не вызывался: Telegram крутил «загрузку» на кнопке до таймаута.
GROUP_CALLBACKS = F.message.chat.type.in_({"group", "supergroup"})


@router.message(CommandStart(), GROUP_CHATS)
async def cmd_start_group(message: Message):
    await message.reply(group_intro_text(), reply_markup=group_menu_kb())


@router.message(Command("angryinv", ignore_case=True), GROUP_CHATS)
async def cmd_angry_inv(message: Message, bot: Bot):
    me = await bot.get_me()
    builder = InlineKeyboardBuilder()
    builder.button(text="🎒 Открыть инвентарь", url=f"https://t.me/{me.username}?start=inv")
    builder.adjust(1)
    await message.reply(
        "<b>🎒 Инвентарь открывается в личке с ботом.</b>\n"
        "Там все твои классы и предметы — со звёздами и количеством.",
        reply_markup=builder.as_markup(),
    )


@router.message(Command("angryopen", ignore_case=True), GROUP_CHATS)
async def cmd_angry_open(message: Message, pool: asyncpg.Pool):
    photos, text = await perform_open(pool, message.chat.id, message.from_user)
    await reply_spin_result(message, photos, text)


@router.message(Command("angryclass", ignore_case=True), GROUP_CHATS)
async def cmd_angry_class(message: Message, pool: asyncpg.Pool):
    photos, text = await perform_class(pool, message.chat.id, message.from_user)
    await reply_spin_result(message, photos, text)


@router.message(Command("angrybattle", ignore_case=True), GROUP_CHATS)
async def cmd_angry_battle(message: Message, pool: asyncpg.Pool):
    photo_id, text = await perform_battle(pool, message.chat.id, message.from_user)
    if photo_id:
        await message.reply_photo(photo_id, caption=text)
    else:
        await message.reply(text)


@router.message(Command("angrytop", ignore_case=True), GROUP_CHATS)
async def cmd_angry_top(message: Message, bot: Bot, pool: asyncpg.Pool):
    await message.reply(
        await perform_top(pool, message.chat.id, "power"),
        reply_markup=top_kb("power", message.from_user.id),
    )
    spawn_background_task(refresh_chat_tags(bot, pool, message.chat.id))


@router.callback_query(F.data.startswith("top:"), GROUP_CALLBACKS)
async def top_switch_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    _, metric, owner_id = callback.data.split(":")
    if metric not in TOP_METRICS:
        await callback.answer("Неизвестный лидерборд", show_alert=True)
        return
    if callback.from_user.id != int(owner_id):
        await callback.answer("Это чужой лидерборд — вызови /AngryTop сам.", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            await perform_top(pool, callback.message.chat.id, metric),
            reply_markup=top_kb(metric, owner_id),
        )
    except TelegramBadRequest:
        # Нажали на уже открытую вкладку — Telegram ругается на «не изменилось».
        pass
    await callback.answer()


def is_info_word(text: str | None) -> bool:
    """Слово «инфо» первым в сообщении. Именно первое слово целиком, иначе
    триггер ловил бы «информация», «инфографика» и прочие безобидные фразы."""
    parts = (text or "").strip().lower().split()
    return bool(parts) and parts[0] == "инфо"


async def handle_angry_info(message: Message, bot: Bot, pool: asyncpg.Pool, raw_arg: str) -> None:
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        target = reply.from_user
    else:
        arg = (raw_arg or "").strip().lstrip("@")
        if arg:
            try:
                target = await bot.get_chat(f"@{arg}")
            except TelegramAPIError:
                await message.reply("<b>Не нашёл такого игрока.</b>")
                return
        else:
            target = message.from_user

    if getattr(target, "is_bot", False):
        await message.reply("<b>У ботов нет профиля игрока.</b>")
        return

    target_id = target.id
    target_name = target.full_name

    power = await get_player_power(pool, target_id)
    money = await get_chat_money(pool, message.chat.id, target_id)
    tokens = await get_player_tokens(pool, target_id)
    energy = await get_player_energy_display(pool, target_id)
    clan = await get_user_clan(pool, target_id)
    best_class = await get_player_best_class(pool, target_id)

    text = render_profile(
        target_name, power, money, tokens, energy, clan["name"] if clan else None, best_class
    )
    if best_class:
        await message.reply_photo(best_class["photo_id"], caption=text)
    else:
        await message.reply(text)


@router.message(Command("angryinfo", ignore_case=True), GROUP_CHATS)
async def cmd_angry_info(message: Message, command: CommandObject, bot: Bot, pool: asyncpg.Pool):
    await handle_angry_info(message, bot, pool, command.args or "")


@router.message(GROUP_CHATS, F.text.func(is_info_word))
async def cmd_angry_info_word(message: Message, bot: Bot, pool: asyncpg.Pool):
    await handle_angry_info(message, bot, pool, (message.text or "").strip()[len("инфо"):])


async def handle_steal(message: Message, pool: asyncpg.Pool) -> None:
    reply = message.reply_to_message
    if reply is None or reply.from_user is None:
        await message.reply(
            "<b>🕵️ Чтобы украсть монеты, ответь этой командой на сообщение игрока.</b>"
        )
        return
    target = reply.from_user
    if target.is_bot:
        await message.reply("<b>🤖 У ботов красть нечего.</b>")
        return
    if target.id == message.from_user.id:
        await message.reply("<b>🙃 Нельзя красть у самого себя.</b>")
        return
    await message.reply(await perform_steal(pool, message.chat.id, message.from_user, target))


@router.message(Command("angrysteal", ignore_case=True), GROUP_CHATS)
async def cmd_steal(message: Message, pool: asyncpg.Pool):
    await handle_steal(message, pool)


@router.message(GROUP_CHATS, F.reply_to_message, F.text.func(lambda t: (t or "").strip().lower() == "кража"))
async def cmd_steal_word(message: Message, pool: asyncpg.Pool):
    await handle_steal(message, pool)


async def handle_casino(message: Message, pool: asyncpg.Pool, raw_bet: str) -> None:
    raw = (raw_bet or "").strip()
    if not raw.isdigit():
        await message.reply(
            "<b>🎰 Укажи ставку: /AngryCasino 100 или «казик 100»</b>"
        )
        return

    bet = int(raw)
    user = message.from_user
    error = await casino_take_bet(pool, message.chat.id, user, bet)
    if error:
        await message.reply(error)
        return

    try:
        slot = await message.reply_dice(emoji="🎰")
    except TelegramAPIError:
        await casino_refund(pool, message.chat.id, user, bet)
        await message.reply("<b>🎰 Слот заело — ставка возвращена, попробуй ещё раз.</b>")
        return

    await asyncio.sleep(SLOT_ANIMATION_SECONDS)
    await message.reply(await casino_pay_out(pool, message.chat.id, user, bet, slot.dice.value))


@router.message(Command("angrycasino", ignore_case=True), GROUP_CHATS)
async def cmd_casino(message: Message, command: CommandObject, pool: asyncpg.Pool):
    await handle_casino(message, pool, command.args or "")


@router.message(GROUP_CHATS, F.text.func(lambda t: (t or "").strip().lower().startswith("казик")))
async def cmd_casino_word(message: Message, pool: asyncpg.Pool):
    await handle_casino(message, pool, (message.text or "").strip()[len("казик"):])


@router.callback_query(F.data == "group:open", GROUP_CALLBACKS)
async def cb_group_open(callback: CallbackQuery, pool: asyncpg.Pool):
    photos, text = await perform_open(pool, callback.message.chat.id, callback.from_user)
    await reply_spin_result(callback.message, photos, text)
    await callback.answer()


@router.callback_query(F.data == "group:class", GROUP_CALLBACKS)
async def cb_group_class(callback: CallbackQuery, pool: asyncpg.Pool):
    photos, text = await perform_class(pool, callback.message.chat.id, callback.from_user)
    await reply_spin_result(callback.message, photos, text)
    await callback.answer()


@router.callback_query(F.data == "group:battle", GROUP_CALLBACKS)
async def cb_group_battle(callback: CallbackQuery, pool: asyncpg.Pool):
    photo_id, text = await perform_battle(pool, callback.message.chat.id, callback.from_user)
    if photo_id:
        await callback.message.reply_photo(photo_id, caption=text)
    else:
        await callback.message.reply(text)
    await callback.answer()


@router.callback_query(F.data == "group:top", GROUP_CALLBACKS)
async def cb_group_top(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool):
    await callback.message.reply(await perform_top(pool, callback.message.chat.id))
    spawn_background_task(refresh_chat_tags(bot, pool, callback.message.chat.id))
    await callback.answer()


@router.callback_query(F.data.startswith("airdrop:claim:"), GROUP_CALLBACKS)
async def airdrop_claim_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    airdrop_id = int(callback.data.split(":")[2])
    user = callback.from_user
    cycle_id = int(time.time() // (setting("airdrop_cycle_min") * 60))

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT chat_id, tier, claimed_by, expired FROM airdrops WHERE id = $1 FOR UPDATE",
                airdrop_id,
            )
            if row is None or row["claimed_by"] is not None or row["expired"]:
                await callback.answer("Этот дроп уже забрали или он исчез", show_alert=True)
                return

            allowed = await try_claim_airdrop_slot(conn, user.id, user.full_name, cycle_id)
            if not allowed:
                await callback.answer(
                    f"Лимит {setting('airdrop_max_claims')} дропов за цикл исчерпан", show_alert=True
                )
                return

            loot_lines = await apply_airdrop_loot(
                conn, row["chat_id"], user.id, user.full_name, roll_airdrop_loot(row["tier"])
            )

            await conn.execute(
                "UPDATE airdrops SET claimed_by = $1, claimed_name = $2 WHERE id = $3",
                user.id, user.full_name, airdrop_id,
            )

    label = AIRDROP_TIER_LABELS[row["tier"]]
    icon = AIRDROP_TIER_ICONS[row["tier"]]
    mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.full_name)}</a>"
    body = "\n".join(loot_lines) if loot_lines else "— в этот раз пусто"
    text = f"<b>{icon} {mention} забрал {label.lower()} дроп!</b>\n\n" + body
    await safe_edit_text(callback.message, text)
    await callback.answer("Забрано!")


# ── Кланы ─────────────────────────────────────────────────────────────────

CLAN_NAME_MAX_LEN = 40
_pending_clan_delete: dict[int, float] = {}


@router.message(Command("clancreate", ignore_case=True), GROUP_CHATS)
async def cmd_clan_create(message: Message, command: CommandObject, pool: asyncpg.Pool):
    name = (command.args or "").strip()
    if not name or not message.photo:
        await message.reply(
            "<b>✏️ Пришли фото клана с подписью: /clancreate Название</b>"
        )
        return
    if len(name) > CLAN_NAME_MAX_LEN:
        await message.reply(f"<b>Слишком длинное название (максимум {CLAN_NAME_MAX_LEN} символов).</b>")
        return
    if await get_user_clan(pool, message.from_user.id):
        await message.reply("<b>Ты уже состоишь в клане.</b>")
        return
    if await clan_name_taken(pool, name):
        await message.reply("<b>Клан с таким названием уже существует.</b>")
        return

    money = await get_chat_money(pool, message.chat.id, message.from_user.id)
    if money < CLAN_CREATE_COST:
        await message.reply(f"<b><tg-emoji emoji-id='5224237406688944529'>🪙</tg-emoji> Не хватает монет. Нужно {CLAN_CREATE_COST}, у тебя {money}.</b>")
        return

    photo_id = message.photo[-1].file_id
    await create_clan(pool, name, photo_id, message.from_user.id, message.from_user.full_name, message.chat.id)
    await message.reply_photo(
        photo_id,
        caption=f"<b>🏰 Клан «{html.escape(name)}» создан!\n\n👑 Основатель: {html.escape(message.from_user.full_name)}</b>",
    )


@router.message(Command("claninvite", ignore_case=True), GROUP_CHATS)
async def cmd_clan_invite(message: Message, pool: asyncpg.Pool):
    clan = await get_user_clan(pool, message.from_user.id)
    if not clan:
        await message.reply("<b>У тебя нет клана.</b>")
        return
    if clan["creator_id"] != message.from_user.id:
        await message.reply("<b>Приглашать в клан может только его создатель.</b>")
        return

    reply = message.reply_to_message
    if reply is None or reply.from_user is None:
        await message.reply("<b>Ответь этой командой на сообщение игрока, которого хочешь пригласить.</b>")
        return
    target = reply.from_user
    if target.is_bot:
        await message.reply("<b>Ботов приглашать нельзя.</b>")
        return
    if target.id == message.from_user.id:
        await message.reply("<b>Нельзя пригласить самого себя.</b>")
        return
    if await get_user_clan(pool, target.id):
        await message.reply("<b>Этот игрок уже состоит в клане.</b>")
        return
    if await clan_member_count(pool, clan["id"]) >= CLAN_MAX_MEMBERS:
        await message.reply(f"<b>В клане уже максимум участников ({CLAN_MAX_MEMBERS}).</b>")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"clan:accept:{clan['id']}:{target.id}")
    kb.button(text="❌ Отклонить", callback_data=f"clan:decline:{clan['id']}:{target.id}")
    kb.adjust(2)
    await message.reply(
        f"<b>📨 {html.escape(target.full_name)}, тебя приглашают в клан «{html.escape(clan['name'])}»!</b>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("clan:accept:"), GROUP_CALLBACKS)
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


@router.callback_query(F.data.startswith("clan:decline:"), GROUP_CALLBACKS)
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
        await message.reply("<b>У тебя нет клана.</b>")
        return
    if clan["creator_id"] != message.from_user.id:
        await message.reply("<b>Удалить клан может только его создатель.</b>")
        return

    now = time.time()
    last_attempt = _pending_clan_delete.get(message.from_user.id)
    if last_attempt is not None and now - last_attempt <= CLAN_DELETE_CONFIRM_WINDOW:
        _pending_clan_delete.pop(message.from_user.id, None)
        await delete_clan(pool, clan["id"])
        await message.reply(f"<b>💥 Клан «{html.escape(clan['name'])}» удалён.</b>")
        return

    _pending_clan_delete[message.from_user.id] = now
    await message.reply(
        f"<b>⚠️ Чтобы удалить клан «{html.escape(clan['name'])}», отправь /clandelete ещё раз в течение минуты.</b>"
    )


@router.message(Command("clanleft", ignore_case=True), GROUP_CHATS)
async def cmd_clan_left(message: Message, pool: asyncpg.Pool):
    clan = await get_user_clan(pool, message.from_user.id)
    if not clan:
        await message.reply("<b>Ты не состоишь в клане.</b>")
        return
    if clan["creator_id"] == message.from_user.id:
        await message.reply(
            "<b>Ты создатель клана — сначала удали его через /clandelete (дважды подряд в течение минуты).</b>"
        )
        return
    await remove_clan_member(pool, message.from_user.id)
    await message.reply(f"<b>👋 Ты покинул клан «{html.escape(clan['name'])}».</b>")


@router.message(Command("clantop", ignore_case=True), GROUP_CHATS)
async def cmd_clan_top(message: Message, pool: asyncpg.Pool):
    clans = await get_clan_top(pool, CLAN_TOP_LIMIT)
    if not clans:
        await message.reply("<b>📭 Пока нет ни одного клана.</b>")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["<b>🏆 Топ кланов по силе", "━━━━━━━━━━━━━━", ""]
    for i, clan in enumerate(clans):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(
            f"{prefix} {html.escape(clan['name'])} — ⚔️ {clan['total_power']} "
            f"({clan['members']}/{CLAN_MAX_MEMBERS})"
        )
    await message.reply("\n".join(lines) + "</b>")


@router.message(Command("clan", ignore_case=True), GROUP_CHATS)
async def cmd_clan_info(message: Message, command: CommandObject, pool: asyncpg.Pool):
    query = (command.args or "").strip()
    if query:
        clan = await get_clan_by_name(pool, query)
        if not clan:
            await message.reply("<b>Клан с таким названием не найден.</b>")
            return
    else:
        clan = await get_user_clan(pool, message.from_user.id)
        if not clan:
            await message.reply("<b>Ты не состоишь в клане. Чтобы посмотреть другой: /clan Название</b>")
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

    await message.reply_photo(clan["photo_id"], caption="\n".join(lines))


# ── Реферальная программа (1% создателю группы) ───────────────────────────

@router.my_chat_member(GROUP_CHATS)
async def on_bot_membership_change(event: ChatMemberUpdated, bot: Bot, pool: asyncpg.Pool):
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    title = event.chat.title or "группа"

    if new_status in ("left", "kicked"):
        await pool.execute("DELETE FROM bot_chats WHERE chat_id = $1", event.chat.id)
        return

    if new_status in ("member", "administrator"):
        await pool.execute(
            """
            INSERT INTO bot_chats (chat_id, title) VALUES ($1, $2)
            ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title
            """,
            event.chat.id, title,
        )

    if old_status not in ("left", "kicked") or new_status not in ("member", "administrator"):
        return

    creator = await get_chat_creator(bot, event.chat.id)
    if creator is None:
        return
    owner_id, owner_name = creator

    is_new = await register_chat_owner(pool, event.chat.id, owner_id, owner_name, title)
    if not is_new:
        return

    try:
        await bot.send_message(owner_id, owner_notify_text(title))
    except TelegramForbiddenError:
        await notify_admin(
            bot, pool,
            f"<b>👑 Уведомление о 1% НЕ доставлено</b>\n"
            f"Группа: {html.escape(title)}\n"
            f"Владелец ещё не писал боту в ЛС — уведомление встало в очередь на потом.",
        )
        return
    await mark_owner_notified(pool, event.chat.id)
    await notify_admin(
        bot, pool,
        f"<b>👑 Уведомление о 1% доставлено сразу</b>\n"
        f"Группа: {html.escape(title)}\nВладелец: {html.escape(owner_name)}",
    )


# ── Промо реферальной программы ─────────────────────────────────────────

REFERRAL_PROMO_INTERVAL = 1 * 60 * 60

REFERRAL_PROMO_TEXT = (
    "<b>🎁 Зарабатывай на своей группе!\n\n"
    "Добавь Angry Копилку в свою группу — и получай 1% от всех монет, которые заработают "
    "игроки в этой группе. Автоматически, пассивно, без каких-либо усилий.</b>"
)


def referral_promo_kb(bot_username: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📖 Как подключить", url=f"https://t.me/{bot_username}?start=referral")
    kb.adjust(1)
    return kb.as_markup()


async def broadcast_referral_promo(bot: Bot, pool: asyncpg.Pool, bot_username: str) -> None:
    kb = referral_promo_kb(bot_username)
    results = []
    for chat in await get_active_chats(pool):
        try:
            sent = await bot.send_message(chat["chat_id"], REFERRAL_PROMO_TEXT, reply_markup=kb)
            results.append((chat["title"], group_message_link(chat["chat_id"], sent.message_id), True))
        except TelegramAPIError as error:
            logging.warning("Не удалось отправить промо в чат %s: %s", chat["chat_id"], error)
            results.append((chat["title"], None, False))
    await notify_admin_broadcast_results(bot, pool, "🎁 Промо 1%", results)


async def referral_promo_loop(bot: Bot, pool: asyncpg.Pool, bot_username: str) -> None:
    while True:
        await asyncio.sleep(REFERRAL_PROMO_INTERVAL)
        try:
            await broadcast_referral_promo(bot, pool, bot_username)
        except Exception:
            logging.exception("Ошибка в цикле промо 1%")


# ── Планировщик аирдропов ────────────────────────────────────────────────

def airdrop_spawn_text(label: str, icon: str) -> str:
    return f"<b>{icon} Появился {label.lower()} аирдроп!</b>"


def airdrop_claim_kb(airdrop_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Забрать", callback_data=f"airdrop:claim:{airdrop_id}")
    kb.adjust(1)
    return kb.as_markup()


async def count_human_members(bot: Bot, chat_id: int) -> int | None:
    """Участники чата без ботов, или None если посчитать не удалось.

    Telegram не отдаёт список всех участников — видны только админы, поэтому
    вычесть можно лишь ботов-админов (включая самого себя). Боты-неадмины
    в счёт всё же попадут.
    """
    try:
        total = await bot.get_chat_member_count(chat_id)
    except TelegramAPIError as error:
        logging.warning("Не удалось получить число участников чата %s: %s", chat_id, error)
        return None

    bots = 0
    try:
        admins = await bot.get_chat_administrators(chat_id)
        bots = sum(1 for admin in admins if admin.user.is_bot)
    except TelegramAPIError:
        pass
    return max(0, total - bots)


async def spawn_airdrop_in_chat(
    bot: Bot, pool: asyncpg.Pool, chat_id: int, chat_title: str, forced_tier: str | None = None
) -> None:
    await asyncio.sleep(random.uniform(AIRDROP_CHAT_STAGGER_MIN, AIRDROP_CHAT_STAGGER_MAX))

    members = await count_human_members(bot, chat_id)
    if members is not None and members < setting("airdrop_min_members"):
        logging.info(
            "Аирдроп пропущен: в чате %s (%s) только %s участник(ов), нужно %s",
            chat_id, chat_title, members, setting("airdrop_min_members"),
        )
        return

    if forced_tier:
        code = forced_tier
        label, icon = AIRDROP_TIER_LABELS[code], AIRDROP_TIER_ICONS[code]
    else:
        code, label, icon = roll_airdrop_tier()
    airdrop_id = await pool.fetchval(
        "INSERT INTO airdrops (chat_id, message_id, tier, created_at) VALUES ($1, 0, $2, $3) RETURNING id",
        chat_id, code, time.time(),
    )
    try:
        sent = await bot.send_message(
            chat_id, airdrop_spawn_text(label, icon), reply_markup=airdrop_claim_kb(airdrop_id)
        )
    except TelegramAPIError as error:
        logging.warning("Не удалось отправить аирдроп в чат %s: %s", chat_id, error)
        await pool.execute("DELETE FROM airdrops WHERE id = $1", airdrop_id)
        await notify_admin(
            bot, pool,
            f"<b>🌌 Аирдроп НЕ отправлен</b>\n{label} · {html.escape(chat_title)}\nОшибка: {html.escape(str(error))}",
        )
        return
    await pool.execute("UPDATE airdrops SET message_id = $1 WHERE id = $2", sent.message_id, airdrop_id)

    link = group_message_link(chat_id, sent.message_id)
    safe_title = html.escape(chat_title)
    if link:
        text = f"<b>🌌 Аирдроп отправлен</b>\n{label} · <a href='{link}'>{safe_title}</a>"
    else:
        text = f"<b>🌌 Аирдроп отправлен</b>\n{label} · {safe_title} (ссылка недоступна для этого типа чата)"
    await notify_admin(bot, pool, text)


async def spawn_airdrop_cycle(bot: Bot, pool: asyncpg.Pool, forced_tier: str | None = None) -> None:
    for chat in await get_active_chats(pool):
        spawn_background_task(
            spawn_airdrop_in_chat(bot, pool, chat["chat_id"], chat["title"], forced_tier)
        )


async def airdrop_spawn_loop(bot: Bot, pool: asyncpg.Pool) -> None:
    while True:
        await asyncio.sleep(setting("airdrop_cycle_min") * 60)
        try:
            await spawn_airdrop_cycle(bot, pool)
        except Exception:
            logging.exception("Ошибка в цикле спавна аирдропов")


async def expire_airdrops_loop(bot: Bot, pool: asyncpg.Pool) -> None:
    while True:
        await asyncio.sleep(AIRDROP_EXPIRE_CHECK_INTERVAL)
        try:
            await _expire_airdrops_once(bot, pool)
        except Exception:
            logging.exception("Ошибка в цикле протухания аирдропов")


async def _expire_airdrops_once(bot: Bot, pool: asyncpg.Pool) -> None:
    cutoff = time.time() - setting("airdrop_expire_min") * 60
    rows = await pool.fetch(
        "SELECT id, chat_id, message_id FROM airdrops "
        "WHERE claimed_by IS NULL AND expired = FALSE AND created_at < $1",
        cutoff,
    )
    for row in rows:
        await pool.execute("UPDATE airdrops SET expired = TRUE WHERE id = $1", row["id"])
        try:
            await bot.edit_message_text(
                "<b>💨 Аирдроп исчез — никто не успел его забрать.</b>",
                chat_id=row["chat_id"], message_id=row["message_id"],
            )
        except TelegramAPIError:
            pass


# ── Теги участников ──────────────────────────────────────────────────────

def format_compact(value: int) -> str:
    """1 -> «1», 1300 -> «1.3к», 1000 -> «1к», 2_500_000 -> «2.5м».

    Округляем вниз: тег не должен показывать больше, чем у игрока есть.
    Десятая доля только одна и только если она ненулевая.
    """
    for limit, suffix in ((1_000_000_000, "млрд"), (1_000_000, "м"), (1_000, "к")):
        if abs(value) >= limit:
            tenths = int(abs(value) / limit * 10)
            head, tail = divmod(tenths, 10)
            sign = "-" if value < 0 else ""
            return f"{sign}{head}.{tail}{suffix}" if tail else f"{sign}{head}{suffix}"
    return str(value)


def member_tag_text(power: int, money: int) -> str:
    """Тег вида «сила/монеты». У тега лимит в 16 символов — режем с запасом."""
    return f"{format_compact(power)}/{format_compact(money)}"[:16]


async def get_tag_candidates(pool: asyncpg.Pool, chat_id: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT cp.user_id, pl.power, cp.money
        FROM chat_profiles cp
        JOIN players pl ON pl.user_id = cp.user_id
        WHERE cp.chat_id = $1
        ORDER BY pl.power DESC
        LIMIT $2
        """,
        chat_id, MEMBER_TAG_TOP_LIMIT,
    )
    return [dict(row) for row in rows]


async def get_own_tags(pool: asyncpg.Pool, chat_id: int) -> dict[int, str]:
    rows = await pool.fetch(
        "SELECT user_id, tag FROM member_tags WHERE chat_id = $1", chat_id
    )
    return {row["user_id"]: row["tag"] for row in rows}


async def remember_own_tag(pool: asyncpg.Pool, chat_id: int, user_id: int, tag: str) -> None:
    await pool.execute(
        "INSERT INTO member_tags (chat_id, user_id, tag) VALUES ($1, $2, $3) "
        "ON CONFLICT (chat_id, user_id) DO UPDATE SET tag = EXCLUDED.tag",
        chat_id, user_id, tag,
    )


async def forget_own_tag(pool: asyncpg.Pool, chat_id: int, user_id: int) -> None:
    await pool.execute(
        "DELETE FROM member_tags WHERE chat_id = $1 AND user_id = $2", chat_id, user_id
    )


async def refresh_chat_tags(bot: Bot, pool: asyncpg.Pool, chat_id: int) -> None:
    """Проставляет топу чата тег «сила/монеты».

    Чужие теги не трогаем: свой опознаём по записи в member_tags. Если текущий
    тег участника не совпадает с тем, что мы ставили, значит его поменяли руками
    — оставляем как есть и забываем про него. Кто выпал из топа, тег теряет.
    """
    own_tags = await get_own_tags(pool, chat_id)
    candidates = await get_tag_candidates(pool, chat_id)
    in_top = {row["user_id"] for row in candidates}

    for row in candidates:
        user_id = row["user_id"]
        desired = member_tag_text(row["power"], row["money"])
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except TelegramAPIError:
            continue

        # Тег есть только у обычных участников: у админов и владельца вместо
        # него custom_title, его через setChatMemberTag не поставить.
        current = getattr(member, "tag", None)
        if not hasattr(member, "tag"):
            continue

        if current and current != own_tags.get(user_id):
            await forget_own_tag(pool, chat_id, user_id)
            continue
        if current == desired:
            continue

        try:
            await bot.set_chat_member_tag(chat_id, user_id, desired)
        except TelegramAPIError:
            # Нет права can_manage_tags или чат не поддерживает теги — дальше
            # в этом чате пробовать нечего.
            return
        await remember_own_tag(pool, chat_id, user_id, desired)

    for user_id, tag in own_tags.items():
        if user_id in in_top:
            continue
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except TelegramAPIError:
            continue
        if getattr(member, "tag", None) == tag:
            try:
                await bot.set_chat_member_tag(chat_id, user_id, None)
            except TelegramAPIError:
                pass
        await forget_own_tag(pool, chat_id, user_id)


async def refresh_tags_loop(bot: Bot, pool: asyncpg.Pool) -> None:
    while True:
        await asyncio.sleep(MEMBER_TAG_REFRESH_INTERVAL)
        for chat in await get_active_chats(pool):
            try:
                await refresh_chat_tags(bot, pool, chat["chat_id"])
            except Exception:
                logging.exception("Ошибка обновления тегов в чате %s", chat["chat_id"])


_background_tasks: set[asyncio.Task] = set()


def spawn_background_task(coro) -> asyncio.Task:
    """asyncio хранит только слабую ссылку на задачи из create_task — без сильной
    ссылки где-то ещё сборщик мусора может тихо оборвать задачу посреди работы,
    без единой ошибки в логах. Поэтому держим ссылки в module-level set."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ── Инвентарь: Mini App ──────────────────────────────────────────────────

# Публичный адрес, по которому bothost отдаёт наш порт наружу. Telegram
# открывает Mini App только по https, поэтому здесь именно https-домен.
WEBAPP_URL = "https://bot-1788356307-5467-limedemon-2.bothost.tech"
WEBAPP_PORT = 3000

WEBAPP_DIR = Path(__file__).with_name("webapp")
WEBAPP_CACHE_DIR = WEBAPP_DIR / "cache"

# Файл текстуры звезды под каждый тип редкости (см. RARITY_INFO).
STAR_FILES = {
    "regular": "star-common.png",
    "rainbow": "star-rainbow.png",
    "astral": "star-astral.png",
}


INVENTORY_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Инвентарь</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    background: radial-gradient(circle at 30% 20%, #3a6ea5, #1b2f4b 60%, #101c2e);
    padding: 12px;
  }
  .window {
    max-width: 720px; margin: 0 auto;
    background: linear-gradient(#f6e3c5, #e8cfa8);
    border: 5px solid #7a4a21; border-radius: 20px; overflow: hidden;
    box-shadow: 0 14px 40px rgba(0,0,0,.45), inset 0 2px 0 rgba(255,255,255,.6);
  }
  header {
    background: linear-gradient(#9a5b26, #7a4319);
    padding: 12px 18px; display: flex; align-items: center;
    justify-content: space-between; gap: 10px;
    border-bottom: 4px solid #5e3312;
  }
  h1 {
    margin: 0; font-size: 21px; letter-spacing: 2px; color: #ffe9c4;
    text-transform: uppercase; text-shadow: 0 3px 0 #4b2a0e;
  }
  .total {
    background: #ffcf6b; color: #6b3d12; font-weight: 700; font-size: 12px;
    padding: 6px 12px; border-radius: 999px; border: 2px solid #b9832f;
    white-space: nowrap;
  }
  .tabs { display: flex; gap: 8px; padding: 12px 16px 0; }
  .tab {
    flex: 1; padding: 10px; text-align: center; cursor: pointer;
    user-select: none; font-weight: 700; font-size: 14px; color: #7a4a21;
    background: #dcbb8d; border: 3px solid #b98a52; border-bottom: none;
    border-radius: 12px 12px 0 0;
  }
  .tab.on { background: #fff6e6; color: #4b2a0e; box-shadow: inset 0 3px 0 #ffcf6b; }
  .panel {
    margin: 0 16px 16px; padding: 14px; background: #fff6e6;
    border: 3px solid #b98a52; border-top: none; border-radius: 0 0 14px 14px;
    min-height: 180px;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); gap: 12px; }
  .cell {
    position: relative; background: linear-gradient(#ffffff, #ffeed3);
    border: 3px solid #c79a5f; border-radius: 14px; padding: 7px;
    display: flex; flex-direction: column; align-items: center; gap: 5px;
    box-shadow: 0 3px 0 #c79a5f;
  }
  .pic { width: 100%; aspect-ratio: 1; border-radius: 10px; overflow: hidden; background: #f0e0c6; }
  .pic img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .stars { display: flex; gap: 2px; height: 20px; }
  .stars img { width: 20px; height: 20px; object-fit: contain; }
  .nm { font-size: 11px; font-weight: 700; color: #5e3312; text-align: center; line-height: 1.2; }
  .qty {
    position: absolute; top: -7px; right: -7px; z-index: 2;
    background: #e8562a; color: #fff; font-weight: 800; font-size: 12px;
    padding: 3px 8px; border-radius: 999px; border: 2px solid #fff6e6;
    box-shadow: 0 2px 4px rgba(0,0,0,.3);
  }
  .empty { text-align: center; color: #9a7345; font-weight: 700; padding: 40px 10px; line-height: 1.5; white-space: pre-line; }
  .hidden { display: none; }
</style>
</head>
<body>
  <div class="window">
    <header>
      <h1>Инвентарь</h1>
      <div class="total" id="total">…</div>
    </header>
    <div class="tabs">
      <div class="tab on" data-t="classes">🐦 Классы</div>
      <div class="tab" data-t="items">⚔️ Предметы</div>
    </div>
    <div class="panel">
      <div id="body" class="empty">Загружаю…</div>
    </div>
  </div>
<script>
  const tg = window.Telegram ? window.Telegram.WebApp : null;
  if (tg) { tg.ready(); tg.expand(); }

  let data = { classes: [], items: [] };
  let tab = "classes";
  const LABELS = { classes: "Птиц", items: "Предметов" };
  const EMPTY = {
    classes: "Пока пусто.\\nКрути /AngryClass в группе!",
    items: "Пока пусто.\\nКрути /AngryOpen в группе!"
  };

  function esc(s) {
    return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function render() {
    document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x.dataset.t === tab));
    const list = data[tab] || [];
    const total = list.reduce((a, i) => a + i.qty, 0);
    document.getElementById("total").textContent = LABELS[tab] + ": " + total;
    const box = document.getElementById("body");
    if (!list.length) {
      box.className = "empty";
      box.textContent = EMPTY[tab];
      return;
    }
    box.className = "grid";
    box.innerHTML = list.map(i => `
      <div class="cell">
        <div class="qty">×${i.qty}</div>
        <div class="pic"><img src="${esc(i.img)}" alt="" loading="lazy"></div>
        <div class="stars">${'<img src="/static/' + esc(i.star) + '" alt="">'.repeat(i.stars)}</div>
        <div class="nm">${esc(i.name)}</div>
      </div>`).join("");
  }

  document.querySelectorAll(".tab").forEach(x => x.onclick = () => { tab = x.dataset.t; render(); });

  fetch("/api/inventory", { headers: { "X-Init-Data": tg ? tg.initData : "" } })
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(j => { data = j; render(); })
    .catch(e => {
      const box = document.getElementById("body");
      box.className = "empty";
      box.textContent = e === 403
        ? "Открой инвентарь через кнопку в боте."
        : "Не удалось загрузить инвентарь.";
      document.getElementById("total").textContent = "—";
    });
</script>
</body>
</html>
"""


def verify_webapp_init_data(token: str, init_data: str) -> dict | None:
    """Проверяет подпись initData от Telegram. Без этого любой мог бы
    попросить чужой инвентарь, просто подставив user_id в запрос."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None
    try:
        if time.time() - float(parsed.get("auth_date", 0)) > 86400:
            return None
    except (TypeError, ValueError):
        return None
    return parsed


async def get_inventory(pool: asyncpg.Pool, user_id: int) -> list[dict]:
    rows = await pool.fetch(
        """
        SELECT i.kind, i.qty, c.name, c.photo_id, c.rarity
        FROM inventory i JOIN cards c ON c.id = i.card_id
        WHERE i.user_id = $1 AND i.kind = 'card'
        UNION ALL
        SELECT i.kind, i.qty, cc.name, cc.photo_id, cc.rarity
        FROM inventory i JOIN class_cards cc ON cc.id = i.card_id
        WHERE i.user_id = $1 AND i.kind = 'class'
        ORDER BY rarity DESC, qty DESC, name
        """,
        user_id,
    )
    items = []
    for row in rows:
        count, star_kind = RARITY_INFO.get(row["rarity"], (1, "regular"))
        items.append({
            "kind": row["kind"],
            "name": row["name"],
            "qty": row["qty"],
            "stars": count,
            "star": STAR_FILES.get(star_kind, "star-common.png"),
            "img": f"/img/{row['photo_id']}",
        })
    return items


async def webapp_index(request: web.Request) -> web.Response:
    return web.Response(text=INVENTORY_PAGE, content_type="text/html")


async def webapp_api_inventory(request: web.Request) -> web.Response:
    init_data = request.headers.get("X-Init-Data") or ""
    parsed = verify_webapp_init_data(request.app["token"], init_data)
    if parsed is None:
        return web.json_response({"error": "bad_signature"}, status=403)
    try:
        user = json.loads(parsed.get("user", "{}"))
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "no_user"}, status=400)

    pool = request.app["pool"]
    items = await get_inventory(pool, user_id)
    return web.json_response({
        "name": user.get("first_name") or "Игрок",
        "classes": [i for i in items if i["kind"] == "class"],
        "items": [i for i in items if i["kind"] == "card"],
    })


async def webapp_image(request: web.Request) -> web.StreamResponse:
    """Отдаёт фото карточки браузеру: у него нет доступа к file_id, поэтому
    один раз качаем файл из Telegram и дальше раздаём с диска."""
    file_id = request.match_info["file_id"]
    path = WEBAPP_CACHE_DIR / (hashlib.sha256(file_id.encode()).hexdigest() + ".jpg")
    if not path.exists():
        pool = request.app["pool"]
        known = await pool.fetchval(
            """
            SELECT 1 FROM cards WHERE photo_id = $1
            UNION ALL SELECT 1 FROM class_cards WHERE photo_id = $1 LIMIT 1
            """,
            file_id,
        )
        if not known:
            raise web.HTTPNotFound()
        bot: Bot = request.app["bot"]
        try:
            tg_file = await bot.get_file(file_id)
            buffer = await bot.download_file(tg_file.file_path)
        except TelegramAPIError:
            raise web.HTTPNotFound()
        WEBAPP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.read())
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=604800"})


async def start_webapp(bot: Bot, pool: asyncpg.Pool, token: str) -> web.AppRunner:
    app = web.Application()
    app["bot"] = bot
    app["pool"] = pool
    app["token"] = token
    app.router.add_get("/", webapp_index)
    app.router.add_get("/api/inventory", webapp_api_inventory)
    app.router.add_get("/img/{file_id}", webapp_image)
    app.router.add_static("/static", WEBAPP_DIR)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
    await site.start()
    logging.info("Mini App слушает порт %s", WEBAPP_PORT)
    return runner


# ── Запуск ───────────────────────────────────────────────────────────────

async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть меню"),
            BotCommand(command="index", description="индекс предметов и классов"),
            BotCommand(command="cleardata", description="стереть все свои данные"),
        ],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="angryopen", description="крутануть копилку"),
            BotCommand(command="angryclass", description="крутануть класс"),
            BotCommand(command="angrysteal", description="украсть монеты (в ответ на сообщение)"),
            BotCommand(command="clancreate", description="создать клан (500 монет)"),
            BotCommand(command="claninvite", description="пригласить в клан (в ответ на сообщение)"),
            BotCommand(command="clandelete", description="удалить клан (дважды подряд)"),
            BotCommand(command="clanleft", description="выйти из клана"),
            BotCommand(command="clantop", description="топ кланов по силе"),
            BotCommand(command="clan", description="инфо о клане"),
            BotCommand(command="angryinfo", description="профиль игрока (в ответ на сообщение)"),
            BotCommand(command="angrybattle", description="сразиться с мобом"),
            BotCommand(command="angrycasino", description="поставить монеты в казино"),
            BotCommand(command="angryinv", description="инвентарь: классы и предметы"),
            BotCommand(command="angrytop", description="открыть лидерборд"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


BOT_TOKEN = "8822713742:AAHYx6SzmdiOyrESTgnrDcNoTYpxzrlP5K4"
# Кто админ бота. Если задан — перебивает правило «админ = первый написавший»
# (по нему админом мог случайно стать посторонний).
ADMIN_USER_ID = 1018561747
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
    await load_settings(pool)
    await load_airdrop_config(pool)
    try:
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())
        dp.message.outer_middleware(AdminAssignMiddleware())
        dp.message.outer_middleware(TrackChatMiddleware())
        dp.message.outer_middleware(OwnerNotifyMiddleware())
        dp.include_router(router)

        await set_bot_commands(bot)
        await bot.delete_webhook(drop_pending_updates=True)
        await apply_admin_override(pool)

        me = await bot.get_me()
        spawn_background_task(referral_promo_loop(bot, pool, me.username))
        spawn_background_task(airdrop_spawn_loop(bot, pool))
        spawn_background_task(expire_airdrops_loop(bot, pool))
        spawn_background_task(refresh_tags_loop(bot, pool))

        runner = await start_webapp(bot, pool, token)
        try:
            await dp.start_polling(bot, pool=pool)
        finally:
            await runner.cleanup()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
