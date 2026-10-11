import asyncio
import html
import logging
import os

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "shop.db")

DEFAULT_GREETING = (
    "⚽ <b>Добро пожаловать в магазин футболок с футболистами!</b>\n\n"
    "Нажмите кнопку ниже, чтобы посмотреть все футболки."
)
MAX_DESCRIPTION = 800  # лимит подписи к фото в Telegram — 1024 символа

admin_router = Router()
user_router = Router()


# ───────────────────────── База данных ─────────────────────────
async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price TEXT,
                photo_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS screens(chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
            """
        )
        await db.execute("INSERT OR IGNORE INTO settings VALUES('greeting', ?)", (DEFAULT_GREETING,))
        await db.commit()


async def get_greeting() -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key='greeting'")
        row = await cur.fetchone()
    return row[0] if row else DEFAULT_GREETING


async def set_greeting(text: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("REPLACE INTO settings VALUES('greeting', ?)", (text,))
        await db.commit()


async def list_products():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, title, price FROM products ORDER BY id")
        return await cur.fetchall()


async def get_product(pid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, title, description, price, photo_id FROM products WHERE id=?", (pid,)
        )
        return await cur.fetchone()


async def add_product(title: str, description: str, price: str | None, photo_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO products(title, description, price, photo_id) VALUES(?,?,?,?)",
            (title, description, price, photo_id),
        )
        await db.commit()


async def delete_product(pid: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM products WHERE id=?", (pid,))
        await db.commit()


async def get_screen(chat_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT message_id FROM screens WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
    return row[0] if row else None


async def save_screen(chat_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("REPLACE INTO screens VALUES(?, ?)", (chat_id, message_id))
        await db.commit()


# ───────────────────── «Чистый чат»: один экран ─────────────────────
async def show(bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup, photo: str | None = None) -> None:
    """Отправляет новый экран и удаляет предыдущее сообщение бота."""
    old_id = await get_screen(chat_id)
    if photo:
        sent = await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=kb)
    await save_screen(chat_id, sent.message_id)
    if old_id:
        try:
            await bot.delete_message(chat_id, old_id)
        except TelegramBadRequest:
            pass


class CleanUserMessages(BaseMiddleware):
    """Удаляет любое сообщение пользователя после обработки."""

    async def __call__(self, handler, event: Message, data):
        try:
            return await handler(event, data)
        finally:
            try:
                await event.delete()
            except TelegramBadRequest:
                pass


# ───────────────────────── Экраны ─────────────────────────
async def screen_home(bot: Bot, chat_id: int, user_id: int) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="👕 Все футболки", callback_data="catalog")
    if user_id in ADMIN_IDS:
        kb.button(text="⚙️ Админ-панель", callback_data="adm:menu")
    kb.adjust(1)
    await show(bot, chat_id, await get_greeting(), kb.as_markup())


async def screen_catalog(bot: Bot, chat_id: int) -> None:
    products = await list_products()
    kb = InlineKeyboardBuilder()
    for pid, title, price in products:
        label = f"{title} — {price}" if price else title
        kb.button(text=label, callback_data=f"p:{pid}")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    text = "👕 <b>Все футболки</b>\nВыберите футболку:" if products else "Пока здесь пусто — товары скоро появятся 🙌"
    await show(bot, chat_id, text, kb.as_markup())


async def screen_product(bot: Bot, chat_id: int, pid: int) -> None:
    product = await get_product(pid)
    if not product:
        await screen_catalog(bot, chat_id)
        return
    _, title, description, price, photo_id = product
    caption = f"<b>{html.escape(title)}</b>\n\n{description}"
    if price:
        caption += f"\n\n💰 <b>{html.escape(price)}</b>"
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить", callback_data=f"buy:{pid}")
    kb.button(text="⬅️ К списку", callback_data="catalog")
    kb.adjust(1)
    await show(bot, chat_id, caption, kb.as_markup(), photo=photo_id)


async def screen_admin(bot: Bot, chat_id: int, note: str = "") -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить футболку", callback_data="adm:add")
    kb.button(text="🗂 Товары / удаление", callback_data="adm:list")
    kb.button(text="✏️ Изменить приветствие", callback_data="adm:greet")
    kb.button(text="⬅️ В магазин", callback_data="home")
    kb.adjust(1)
    text = (note + "\n\n" if note else "") + "⚙️ <b>Админ-панель</b>"
    await show(bot, chat_id, text, kb.as_markup())


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="adm:menu")
    return kb.as_markup()


# ───────────────────────── Покупатель ─────────────────────────
@user_router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    await screen_home(bot, message.chat.id, message.from_user.id)


@user_router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.clear()
    await screen_home(bot, cb.message.chat.id, cb.from_user.id)


@user_router.callback_query(F.data == "catalog")
async def cb_catalog(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await screen_catalog(bot, cb.message.chat.id)


@user_router.callback_query(F.data.startswith("p:"))
async def cb_product(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await screen_product(bot, cb.message.chat.id, int(cb.data.split(":")[1]))


@user_router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    product = await get_product(int(cb.data.split(":")[1]))
    if not product:
        await screen_catalog(bot, cb.message.chat.id)
        return
    _, title, _, price, _ = product
    user = cb.from_user
    who = f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'
    if user.username:
        who += f" (@{user.username})"
    order = f"🛒 <b>Новая заявка</b>\nТовар: {html.escape(title)}"
    if price:
        order += f"\nЦена: {html.escape(price)}"
    order += f"\nПокупатель: {who}"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, order)
        except TelegramBadRequest:
            pass
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К списку", callback_data="catalog")
    await show(
        bot, cb.message.chat.id,
        "✅ Заявка отправлена! Менеджер скоро свяжется с вами в Telegram.",
        kb.as_markup(),
    )


# ───────────────────────── Админ-панель ─────────────────────────
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class AddProduct(StatesGroup):
    photo = State()
    title = State()
    description = State()
    price = State()


class EditGreeting(StatesGroup):
    text = State()


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    await screen_admin(bot, message.chat.id)


@admin_router.callback_query(F.data == "adm:menu")
async def cb_admin_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.clear()
    await screen_admin(bot, cb.message.chat.id)


# --- добавление товара ---
@admin_router.callback_query(F.data == "adm:add")
async def cb_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.set_state(AddProduct.photo)
    await show(bot, cb.message.chat.id, "📸 Пришлите <b>фото футболки</b>", cancel_kb())


@admin_router.message(AddProduct.photo, F.photo)
async def add_photo(message: Message, bot: Bot, state: FSMContext):
    await state.update_data(photo_id=message.photo[-1].file_id)
    await state.set_state(AddProduct.title)
    await show(bot, message.chat.id, "✏️ Пришлите <b>название</b> (например: «Месси — Барселона»)", cancel_kb())


@admin_router.message(AddProduct.title, F.text)
async def add_title(message: Message, bot: Bot, state: FSMContext):
    await state.update_data(title=message.text.strip()[:60])
    await state.set_state(AddProduct.description)
    await show(
        bot, message.chat.id,
        "📝 Пришлите <b>описание</b> (ткань, размеры и т.д.). Можно с форматированием Telegram.",
        cancel_kb(),
    )


@admin_router.message(AddProduct.description, F.text)
async def add_description(message: Message, bot: Bot, state: FSMContext):
    text = message.html_text
    if len(text) > MAX_DESCRIPTION:
        await show(
            bot, message.chat.id,
            f"⚠️ Описание слишком длинное ({len(text)} симв., максимум {MAX_DESCRIPTION}). Пришлите короче.",
            cancel_kb(),
        )
        return
    await state.update_data(description=text)
    await state.set_state(AddProduct.price)
    await show(bot, message.chat.id, "💰 Пришлите <b>цену</b> (например: 2490 ₽) или «-», если без цены", cancel_kb())


@admin_router.message(AddProduct.price, F.text)
async def add_price(message: Message, bot: Bot, state: FSMContext):
    price = message.text.strip()
    price = None if price == "-" else price[:30]
    data = await state.get_data()
    await add_product(data["title"], data["description"], price, data["photo_id"])
    await state.clear()
    await screen_admin(bot, message.chat.id, note=f"✅ Футболка «{html.escape(data['title'])}» добавлена")


# --- список и удаление ---
@admin_router.callback_query(F.data == "adm:list")
async def cb_admin_list(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    products = await list_products()
    kb = InlineKeyboardBuilder()
    for pid, title, _ in products:
        kb.button(text=f"🗑 {title}", callback_data=f"adm:del:{pid}")
    kb.button(text="⬅️ Назад", callback_data="adm:menu")
    kb.adjust(1)
    text = "🗂 Нажмите на товар, чтобы удалить его:" if products else "Товаров пока нет."
    await show(bot, cb.message.chat.id, text, kb.as_markup())


@admin_router.callback_query(F.data.startswith("adm:del:"))
async def cb_admin_del(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    pid = int(cb.data.split(":")[2])
    product = await get_product(pid)
    if not product:
        await cb_admin_list(cb, bot)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"adm:delok:{pid}")
    kb.button(text="✖️ Нет", callback_data="adm:list")
    kb.adjust(2)
    await show(bot, cb.message.chat.id, f"Удалить «{html.escape(product[1])}»?", kb.as_markup())


@admin_router.callback_query(F.data.startswith("adm:delok:"))
async def cb_admin_delok(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await delete_product(int(cb.data.split(":")[2]))
    await screen_admin(bot, cb.message.chat.id, note="🗑 Товар удалён")


# --- приветствие ---
@admin_router.callback_query(F.data == "adm:greet")
async def cb_greet(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.set_state(EditGreeting.text)
    await show(
        bot, cb.message.chat.id,
        "✏️ Пришлите <b>новый текст приветствия</b>. Можно с форматированием Telegram.\n\n"
        "Текущее:\n" + await get_greeting(),
        cancel_kb(),
    )


@admin_router.message(EditGreeting.text, F.text)
async def greet_save(message: Message, bot: Bot, state: FSMContext):
    await set_greeting(message.html_text)
    await state.clear()
    await screen_admin(bot, message.chat.id, note="✅ Приветствие обновлено")


# ───────────────────────── Запуск ─────────────────────────
async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    await db_init()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(CleanUserMessages())
    dp.include_router(admin_router)  # админ раньше — чтобы FSM-шаги ловились первыми
    dp.include_router(user_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
