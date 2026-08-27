import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

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

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "db.json"

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

router = Router()


# ── Хранилище ────────────────────────────────────────────────────────────

def _default_db() -> dict:
    return {"admin_id": None, "cards": [], "chats": {}}


def _load_db_sync() -> dict:
    if not DB_PATH.exists():
        return _default_db()
    try:
        with DB_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_db()


def _save_db_sync(db: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = DB_PATH.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    tmp_path.replace(DB_PATH)


_db_lock = asyncio.Lock()


@asynccontextmanager
async def db_session():
    async with _db_lock:
        db = _load_db_sync()
        yield db
        _save_db_sync(db)


async def get_db_readonly() -> dict:
    async with _db_lock:
        return _load_db_sync()


# ── Админ ────────────────────────────────────────────────────────────────

class AdminAssignMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if event.from_user is not None:
            async with db_session() as db:
                if db["admin_id"] is None:
                    db["admin_id"] = event.from_user.id
        return await handler(event, data)


class IsAdminPrivate(BaseFilter):
    async def __call__(self, event: TelegramObject) -> bool:
        message = event.message if isinstance(event, CallbackQuery) else event
        if message.chat.type != "private" or event.from_user is None:
            return False
        db = await get_db_readonly()
        return db["admin_id"] == event.from_user.id


# ── FSM добавления карточки ──────────────────────────────────────────────

class AddCard(StatesGroup):
    photo = State()
    name = State()
    power = State()
    money = State()


# ── Клавиатуры ───────────────────────────────────────────────────────────

def admin_menu_text(cards_count: int) -> str:
    return f"⚙️ <b>Админ-панель</b>\n\nКарточек в копилке: {cards_count}"


def admin_menu_kb(cards_count: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить карточку", callback_data="admin:add")
    kb.button(text=f"📋 Список карточек ({cards_count})", callback_data="admin:list")
    kb.adjust(1)
    return kb.as_markup()


def cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="admin:cancel")
    return kb.as_markup()


def cards_list_kb(cards: list):
    kb = InlineKeyboardBuilder()
    for i, card in enumerate(cards):
        kb.button(text=f"🗑 {card['name']}", callback_data=f"admin:del:{i}")
    kb.button(text="⬅️ Назад", callback_data="admin:menu")
    kb.adjust(1)
    return kb.as_markup()


def group_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎰 Открыть копилку", callback_data="group:open")
    kb.button(text="🏆 Топ силы", callback_data="group:top")
    kb.adjust(1)
    return kb.as_markup()


# ── Игровая логика ───────────────────────────────────────────────────────

async def perform_open(chat_id: int, user) -> tuple[str | None, str]:
    now = time.time()
    async with db_session() as db:
        if not db["cards"]:
            return None, "🕳 Копилка пока пуста — админ ещё не добавил карточки."

        chat_users = db["chats"].setdefault(str(chat_id), {})
        profile = chat_users.setdefault(
            str(user.id),
            {"name": user.full_name, "power": 0, "money": 0, "opens": 0, "last_open": 0},
        )
        profile["name"] = user.full_name

        remaining = COOLDOWN_SECONDS - (now - profile["last_open"])
        if remaining > 0:
            minutes, seconds = divmod(int(remaining), 60)
            return None, f"⏳ Копилка ещё не наполнилась. Попробуй через {minutes} мин {seconds} сек."

        card = random.choice(db["cards"])
        profile["power"] += card["power"]
        profile["money"] += card["money"]
        profile["opens"] += 1
        profile["last_open"] = now

        caption = (
            f"🎉 <a href='tg://user?id={user.id}'>{user.full_name}</a> крутит копилку!\n\n"
            f"🃏 <b>{card['name']}</b>\n"
            f"⚡ +{card['power']} силы\n"
            f"💰 +{card['money']} монет"
        )
        return card["photo_id"], caption


async def perform_top(chat_id: int) -> str:
    db = await get_db_readonly()
    chat_users = db["chats"].get(str(chat_id), {})
    if not chat_users:
        return "📭 Пока никто не крутил копилку в этом чате."

    ranking = sorted(chat_users.values(), key=lambda p: p["power"], reverse=True)[:TOP_LIMIT]
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ силы чата</b>", ""]
    for i, profile in enumerate(ranking):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{prefix} {profile['name']} — ⚡ {profile['power']} · 💰 {profile['money']}")
    return "\n".join(lines)


# ── Приватные сообщения (админ-панель) ───────────────────────────────────

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start_private(message: Message, state: FSMContext):
    db = await get_db_readonly()
    if message.from_user.id == db["admin_id"]:
        await state.clear()
        await message.answer(admin_menu_text(len(db["cards"])), reply_markup=admin_menu_kb(len(db["cards"])))
    else:
        await message.answer(NON_ADMIN_PRIVATE_TEXT)


@router.callback_query(F.data == "admin:add", IsAdminPrivate())
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddCard.photo)
    await callback.message.edit_text("📸 Пришли фото карточки", reply_markup=cancel_kb())
    await callback.answer()


@router.message(AddCard.photo, IsAdminPrivate())
async def admin_add_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("Пришли именно фото 🙂")
        return
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddCard.name)
    await message.answer("✏️ Введи название карточки", reply_markup=cancel_kb())


@router.message(AddCard.name, IsAdminPrivate())
async def admin_add_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым, попробуй ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(AddCard.power)
    await message.answer("⚡ Введи силу карточки (число)", reply_markup=cancel_kb())


@router.message(AddCard.power, IsAdminPrivate())
async def admin_add_power(message: Message, state: FSMContext):
    try:
        power = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число, попробуй ещё раз.")
        return
    await state.update_data(power=power)
    await state.set_state(AddCard.money)
    await message.answer("💰 Введи количество денег (число)", reply_markup=cancel_kb())


@router.message(AddCard.money, IsAdminPrivate())
async def admin_add_money(message: Message, state: FSMContext):
    try:
        money = int((message.text or "").strip())
    except ValueError:
        await message.answer("Нужно целое число, попробуй ещё раз.")
        return

    data = await state.get_data()
    card = {"name": data["name"], "power": data["power"], "money": money, "photo_id": data["photo_id"]}
    async with db_session() as db:
        db["cards"].append(card)
        cards_count = len(db["cards"])
    await state.clear()

    await message.answer_photo(
        card["photo_id"],
        caption=(
            f"✅ Карточка добавлена!\n\n"
            f"🃏 <b>{card['name']}</b>\n"
            f"⚡ Сила: {card['power']}\n"
            f"💰 Денег: {card['money']}"
        ),
    )
    await message.answer("Что дальше?", reply_markup=admin_menu_kb(cards_count))


@router.callback_query(F.data == "admin:cancel", IsAdminPrivate())
async def admin_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    db = await get_db_readonly()
    await callback.message.edit_text(admin_menu_text(len(db["cards"])), reply_markup=admin_menu_kb(len(db["cards"])))
    await callback.answer("Отменено")


@router.callback_query(F.data == "admin:menu", IsAdminPrivate())
async def admin_menu_cb(callback: CallbackQuery):
    db = await get_db_readonly()
    await callback.message.edit_text(admin_menu_text(len(db["cards"])), reply_markup=admin_menu_kb(len(db["cards"])))
    await callback.answer()


@router.callback_query(F.data == "admin:list", IsAdminPrivate())
async def admin_list_cb(callback: CallbackQuery):
    db = await get_db_readonly()
    if not db["cards"]:
        await callback.answer("Карточек пока нет", show_alert=True)
        return
    await callback.message.edit_text(
        "📋 Список карточек (нажми, чтобы удалить):", reply_markup=cards_list_kb(db["cards"])
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:del:"), IsAdminPrivate())
async def admin_del_cb(callback: CallbackQuery):
    idx = int(callback.data.split(":")[2])
    async with db_session() as db:
        removed = db["cards"].pop(idx) if 0 <= idx < len(db["cards"]) else None
        cards = db["cards"]

    await callback.answer(f"Удалено: {removed['name']}" if removed else "Уже удалено")
    if cards:
        await callback.message.edit_text(
            "📋 Список карточек (нажми, чтобы удалить):", reply_markup=cards_list_kb(cards)
        )
    else:
        await callback.message.edit_text(admin_menu_text(0), reply_markup=admin_menu_kb(0))


# ── Групповые команды ─────────────────────────────────────────────────────

GROUP_CHATS = F.chat.type.in_({"group", "supergroup"})


@router.message(CommandStart(), GROUP_CHATS)
async def cmd_start_group(message: Message):
    await message.answer(GROUP_INTRO_TEXT, reply_markup=group_menu_kb())


@router.message(Command("angryopen", ignore_case=True), GROUP_CHATS)
async def cmd_angry_open(message: Message, bot: Bot):
    photo_id, text = await perform_open(message.chat.id, message.from_user)
    if photo_id:
        await bot.send_photo(message.chat.id, photo_id, caption=text)
    else:
        await message.answer(text)


@router.message(Command("angrytop", ignore_case=True), GROUP_CHATS)
async def cmd_angry_top(message: Message):
    await message.answer(await perform_top(message.chat.id))


@router.callback_query(F.data == "group:open", GROUP_CHATS)
async def cb_group_open(callback: CallbackQuery, bot: Bot):
    photo_id, text = await perform_open(callback.message.chat.id, callback.from_user)
    if photo_id:
        await bot.send_photo(callback.message.chat.id, photo_id, caption=text)
    else:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "group:top", GROUP_CHATS)
async def cb_group_top(callback: CallbackQuery):
    await callback.message.answer(await perform_top(callback.message.chat.id))
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


async def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Не найден токен бота в переменной окружения BOT_TOKEN")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AdminAssignMiddleware())
    dp.include_router(router)

    await set_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
