import asyncio
import logging
import os
import time

import asyncpg
from aiogram import Bot, BaseMiddleware, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    Message,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(level=logging.INFO)

COOLDOWN_SECONDS = 60 * 60
TOP_LIMIT = 10

GROUP_INTRO_TEXT = (
    "🐦 <b>Angry Копилка</b>\n\n"
    "Раз в час можно крутануть копилку и получить силу и монеты.\n\n"
    "🎰 /AngryOpen — крутануть копилку\n"
    "🏆 /AngryTop — топ силы чата"
)
NON_ADMIN_PRIVATE_TEXT = (
    "🐦 Привет! Я работаю в группах.\n\n"
    "Добавь меня в чат и используй там команды /AngryOpen и /AngryTop."
)

CARD_FIELDS = ("name", "power", "money", "photo_id")
FIELD_PROMPTS = {
    "photo_id": "📸 Пришли новое фото карточки",
    "name": "✏️ Введи новое название карточки",
    "power": "⚡ Введи новую силу карточки (число)",
    "money": "💰 Введи новое количество денег (число)",
}

router = Router()


# ── База данных (PostgreSQL) ────────────────────────────────────────────

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS bot_state (
        id SMALLINT PRIMARY KEY,
        admin_id BIGINT
    )
    """,
    "INSERT INTO bot_state (id, admin_id) VALUES (1, NULL) ON CONFLICT (id) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS cards (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        power BIGINT NOT NULL,
        money BIGINT NOT NULL,
        photo_id TEXT NOT NULL
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
        PRIMARY KEY (chat_id, user_id)
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


async def list_cards(pool: asyncpg.Pool) -> list[dict]:
    rows = await pool.fetch("SELECT id, name, power, money, photo_id FROM cards ORDER BY id")
    return [dict(row) for row in rows]


async def cards_count(pool: asyncpg.Pool) -> int:
    return await pool.fetchval("SELECT COUNT(*) FROM cards")


async def get_card(pool: asyncpg.Pool, card_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT id, name, power, money, photo_id FROM cards WHERE id = $1", card_id
    )
    return dict(row) if row else None


async def add_card(pool: asyncpg.Pool, name: str, power: int, money: int, photo_id: str) -> None:
    await pool.execute(
        "INSERT INTO cards (name, power, money, photo_id) VALUES ($1, $2, $3, $4)",
        name, power, money, photo_id,
    )


async def update_card_field(pool: asyncpg.Pool, card_id: int, field: str, value) -> None:
    if field not in CARD_FIELDS:
        raise ValueError(f"Недопустимое поле карточки: {field}")
    await pool.execute(f"UPDATE cards SET {field} = $1 WHERE id = $2", value, card_id)


async def delete_card(pool: asyncpg.Pool, card_id: int) -> str | None:
    return await pool.fetchval("DELETE FROM cards WHERE id = $1 RETURNING name", card_id)


# ── Админ ────────────────────────────────────────────────────────────────

class AdminAssignMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        pool = data.get("pool")
        if pool is not None and event.from_user is not None:
            await ensure_admin(pool, event.from_user.id)
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


class EditCard(StatesGroup):
    waiting_value = State()


# ── Клавиатуры ───────────────────────────────────────────────────────────

def admin_menu_text() -> str:
    return "⚙️ <b>Админ-панель</b>"


def admin_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🐷 Копилка", callback_data="piggy:menu")
    kb.adjust(1)
    return kb.as_markup()


def piggy_menu_text(cards_total: int) -> str:
    return f"🐷 <b>Копилка</b>\n\nКарточек: {cards_total}"


def piggy_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список карточек", callback_data="piggy:list")
    kb.button(text="➕ Добавить карточку", callback_data="piggy:add")
    kb.button(text="✏️ Изменить карточку", callback_data="piggy:edit")
    kb.button(text="🗑 Удалить карточку", callback_data="piggy:remove")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="piggy:cancel")
    return kb.as_markup()


def cards_pick_kb(cards: list, action: str):
    kb = InlineKeyboardBuilder()
    for card in cards:
        kb.button(text=card["name"], callback_data=f"piggy:{action}:{card['id']}")
    kb.button(text="⬅️ Назад", callback_data="piggy:menu")
    kb.adjust(1)
    return kb.as_markup()


def edit_fields_kb(card_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Фото", callback_data=f"piggy:editfield:{card_id}:photo_id")
    kb.button(text="✏️ Название", callback_data=f"piggy:editfield:{card_id}:name")
    kb.button(text="⚡ Сила", callback_data=f"piggy:editfield:{card_id}:power")
    kb.button(text="💰 Деньги", callback_data=f"piggy:editfield:{card_id}:money")
    kb.button(text="⬅️ Назад", callback_data="piggy:edit")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def group_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎰 Открыть копилку", callback_data="group:open")
    kb.button(text="🏆 Топ силы", callback_data="group:top")
    kb.adjust(1)
    return kb.as_markup()


def card_caption(card: dict, prefix: str) -> str:
    return (
        f"{prefix}\n\n"
        f"🃏 <b>{card['name']}</b>\n"
        f"⚡ Сила: {card['power']}\n"
        f"💰 Денег: {card['money']}"
    )


# ── Игровая логика ───────────────────────────────────────────────────────

async def perform_open(pool: asyncpg.Pool, chat_id: int, user) -> tuple[str | None, str]:
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            card = await conn.fetchrow(
                "SELECT name, power, money, photo_id FROM cards ORDER BY random() LIMIT 1"
            )
            if card is None:
                return None, "🕳 Копилка пока пуста — админ ещё не добавил карточки."

            profile = await conn.fetchrow(
                "SELECT last_open FROM chat_profiles WHERE chat_id = $1 AND user_id = $2 FOR UPDATE",
                chat_id, user.id,
            )
            last_open = profile["last_open"] if profile else 0
            remaining = COOLDOWN_SECONDS - (now - last_open)
            if remaining > 0:
                minutes, seconds = divmod(int(remaining), 60)
                return None, f"⏳ Копилка ещё не наполнилась. Попробуй через {minutes} мин {seconds} сек."

            await conn.execute(
                """
                INSERT INTO chat_profiles (chat_id, user_id, name, power, money, opens, last_open)
                VALUES ($1, $2, $3, $4, $5, 1, $6)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    power = chat_profiles.power + EXCLUDED.power,
                    money = chat_profiles.money + EXCLUDED.money,
                    opens = chat_profiles.opens + 1,
                    last_open = EXCLUDED.last_open
                """,
                chat_id, user.id, user.full_name, card["power"], card["money"], now,
            )

    caption = (
        f"🎉 <a href='tg://user?id={user.id}'>{user.full_name}</a> крутит копилку!\n\n"
        f"🃏 <b>{card['name']}</b>\n"
        f"⚡ +{card['power']} силы\n"
        f"💰 +{card['money']} монет"
    )
    return card["photo_id"], caption


async def perform_top(pool: asyncpg.Pool, chat_id: int) -> str:
    rows = await pool.fetch(
        "SELECT name, power, money FROM chat_profiles WHERE chat_id = $1 ORDER BY power DESC LIMIT $2",
        chat_id, TOP_LIMIT,
    )
    if not rows:
        return "📭 Пока никто не крутил копилку в этом чате."

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ силы чата</b>", ""]
    for i, row in enumerate(rows):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{prefix} {row['name']} — ⚡ {row['power']} · 💰 {row['money']}")
    return "\n".join(lines)


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
    await callback.message.edit_text(admin_menu_text(), reply_markup=admin_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "piggy:menu", IsAdminPrivate())
async def piggy_menu_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    total = await cards_count(pool)
    await callback.message.edit_text(piggy_menu_text(total), reply_markup=piggy_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "piggy:cancel", IsAdminPrivate())
async def piggy_cancel(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool):
    await state.clear()
    total = await cards_count(pool)
    await callback.message.edit_text(piggy_menu_text(total), reply_markup=piggy_menu_kb())
    await callback.answer("Отменено")


# — Список —

@router.callback_query(F.data == "piggy:list", IsAdminPrivate())
async def piggy_list_cb(callback: CallbackQuery, pool: asyncpg.Pool):
    cards = await list_cards(pool)
    if not cards:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    lines = ["📋 <b>Список карточек</b>", ""]
    for i, card in enumerate(cards, start=1):
        lines.append(f"{i}. {card['name']} — ⚡{card['power']} · 💰{card['money']}")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="piggy:menu")
    await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())
    await callback.answer()


# — Добавление —

@router.callback_query(F.data == "piggy:add", IsAdminPrivate())
async def piggy_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddCard.photo)
    await callback.message.edit_text("📸 Пришли фото карточки", reply_markup=cancel_kb())
    await callback.answer()


@router.message(AddCard.photo, IsAdminPrivate())
async def add_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("Пришли именно фото 🙂")
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCard.name)
    await message.answer("✏️ Введи название карточки", reply_markup=cancel_kb())


@router.message(AddCard.name, IsAdminPrivate())
async def add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым, попробуй ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(AddCard.power)
    await message.answer("⚡ Введи силу карточки (число)", reply_markup=cancel_kb())


@router.message(AddCard.power, IsAdminPrivate())
async def add_power(message: Message, state: FSMContext):
    try:
        power = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число, попробуй ещё раз.")
        return
    await state.update_data(power=power)
    await state.set_state(AddCard.money)
    await message.answer("💰 Введи количество денег (число)", reply_markup=cancel_kb())


@router.message(AddCard.money, IsAdminPrivate())
async def add_money(message: Message, state: FSMContext, pool: asyncpg.Pool):
    try:
        money = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число, попробуй ещё раз.")
        return

    data = await state.get_data()
    await add_card(pool, data["name"], data["power"], money, data["photo_id"])
    await state.clear()

    card = {"name": data["name"], "power": data["power"], "money": money}
    await message.answer_photo(data["photo_id"], caption=card_caption(card, "✅ Карточка добавлена!"))
    total = await cards_count(pool)
    await message.answer(piggy_menu_text(total), reply_markup=piggy_menu_kb())


# — Изменение —

@router.callback_query(F.data == "piggy:edit", IsAdminPrivate())
async def piggy_edit_list(callback: CallbackQuery, pool: asyncpg.Pool):
    cards = await list_cards(pool)
    if not cards:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    await callback.message.edit_text(
        "✏️ Выбери карточку для изменения:", reply_markup=cards_pick_kb(cards, "edit")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("piggy:edit:"), IsAdminPrivate())
async def piggy_edit_pick(callback: CallbackQuery, pool: asyncpg.Pool):
    card_id = int(callback.data.split(":")[2])
    card = await get_card(pool, card_id)
    if not card:
        await callback.answer("Карточка не найдена", show_alert=True)
        return
    await callback.message.edit_text(card_caption(card, "Что изменить?"), reply_markup=edit_fields_kb(card_id))
    await callback.answer()


@router.callback_query(F.data.startswith("piggy:editfield:"), IsAdminPrivate())
async def piggy_editfield_start(callback: CallbackQuery, state: FSMContext):
    _, _, card_id, field = callback.data.split(":")
    await state.set_state(EditCard.waiting_value)
    await state.update_data(card_id=int(card_id), field=field)
    await callback.message.edit_text(FIELD_PROMPTS[field], reply_markup=cancel_kb())
    await callback.answer()


@router.message(EditCard.waiting_value, IsAdminPrivate())
async def piggy_editfield_value(message: Message, state: FSMContext, pool: asyncpg.Pool):
    data = await state.get_data()
    field = data["field"]
    card_id = data["card_id"]

    if field == "photo_id":
        if not message.photo:
            await message.answer("Пришли именно фото 🙂")
            return
        value = message.photo[-1].file_id
    elif field in ("power", "money"):
        try:
            value = int((message.text or "").strip())
        except ValueError:
            await message.answer("Нужно целое число, попробуй ещё раз.")
            return
    else:
        value = (message.text or "").strip()
        if not value:
            await message.answer("Название не может быть пустым, попробуй ещё раз.")
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
    await callback.message.edit_text(
        "🗑 Выбери карточку для удаления:", reply_markup=cards_pick_kb(cards, "remove")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("piggy:remove:"), IsAdminPrivate())
async def piggy_remove_pick(callback: CallbackQuery, pool: asyncpg.Pool):
    card_id = int(callback.data.split(":")[2])
    name = await delete_card(pool, card_id)
    await callback.answer(f"Удалено: {name}" if name else "Уже удалено")

    cards = await list_cards(pool)
    if cards:
        await callback.message.edit_text(
            "🗑 Выбери карточку для удаления:", reply_markup=cards_pick_kb(cards, "remove")
        )
    else:
        await callback.message.edit_text(piggy_menu_text(0), reply_markup=piggy_menu_kb())


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


@router.message(Command("angrytop", ignore_case=True), GROUP_CHATS)
async def cmd_angry_top(message: Message, pool: asyncpg.Pool):
    await message.answer(await perform_top(pool, message.chat.id))


@router.callback_query(F.data == "group:open", GROUP_CHATS)
async def cb_group_open(callback: CallbackQuery, bot: Bot, pool: asyncpg.Pool):
    photo_id, text = await perform_open(pool, callback.message.chat.id, callback.from_user)
    if photo_id:
        await bot.send_photo(callback.message.chat.id, photo_id, caption=text)
    else:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "group:top", GROUP_CHATS)
async def cb_group_top(callback: CallbackQuery, pool: asyncpg.Pool):
    await callback.message.answer(await perform_top(pool, callback.message.chat.id))
    await callback.answer()


# ── Запуск ───────────────────────────────────────────────────────────────

async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть меню")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [
            BotCommand(command="angryopen", description="крутануть копилку"),
            BotCommand(command="angrytop", description="открыть лидерборд"),
        ],
        scope=BotCommandScopeAllGroupChats(),
    )


BOT_TOKEN = "8822713742:AAH_AWB1yhYl1UY1GvwctUg5sz2RltzyeW0"
DATABASE_URL = (
    "postgresql://bothost_db_070b39e25784:-IpoMUbOGfL-gKUZj9kDRhD7RrJ02C7NOcrrvFBIxWo"
    "@node1.pghost.ru:16036/bothost_db_070b39e25784"
)


async def main() -> None:
    token = os.environ.get("BOT_TOKEN") or BOT_TOKEN
    dsn = os.environ.get("DATABASE_URL") or DATABASE_URL
    if not token:
        raise RuntimeError("Не найден токен бота в переменной окружения BOT_TOKEN")

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    await init_db(pool)
    try:
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())
        dp.message.outer_middleware(AdminAssignMiddleware())
        dp.include_router(router)

        await set_bot_commands(bot)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, pool=pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
