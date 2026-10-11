import asyncio
import html
import logging
import os

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, Message
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
DEFAULT_REQUISITES = (
    "Реквизиты для оплаты пока не заданы администратором.\n\n"
    "После оплаты нажмите кнопку «✅ Я оплатил»."
)
DEFAULT_SIZE_HINT = "Сверьтесь с таблицей размеров и пришлите нужный размер сообщением (например: M)."
DELIVERY_PROMPT = (
    "🚚 <b>Способ доставки и адрес</b>\n\n"
    "Одним сообщением напишите:\n"
    "• <b>СДЭК</b> — адрес пункта СДЭК, ФИО получателя\n"
    "• <b>Яндекс Доставка</b> — номер телефона, адрес получателя, ФИО получателя"
)
MAX_PHOTOS = 3
MAX_DESCRIPTION = 800  # лимит подписи к фото в Telegram — 1024 символа
MAX_REQUISITES = 700
MAX_SIZE = 30
MAX_DELIVERY = 350

STATUS_LABEL = {
    "unpaid": "⚪ Не оплачен",
    "awaiting_receipt": "⏳ Ждём чек от покупателя",
    "pending": "🟡 Чек получен — проверьте оплату",
    "paid": "🟢 Оплачен",
    "shipped": "📦 Отправлен",
    "done": "🏁 Завершён",
    "cancelled": "✖️ Отменён",
}
ACTIVE_MARK = {"pending": "🟡", "paid": "🟢", "shipped": "📦"}

admin_router = Router()
user_router = Router()
FSM_LOCK = asyncio.Lock()  # альбомы приходят пачкой сообщений — обрабатываем по очереди


class Checkout(StatesGroup):
    size = State()
    delivery = State()


class AddProduct(StatesGroup):
    photos = State()
    title = State()
    description = State()
    price = State()


class EditGreeting(StatesGroup):
    text = State()


class EditRequisites(StatesGroup):
    text = State()
    photo = State()


class EditSizes(StatesGroup):
    photo = State()


class SendToBuyer(StatesGroup):
    active = State()


# ───────────────────────── База данных ─────────────────────────
async def q(sql: str, params=(), *, one: bool = False, write: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        if write:
            await db.commit()
            return cur.lastrowid
        return await (cur.fetchone() if one else cur.fetchall())


async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price TEXT,
                photo_id TEXT NOT NULL,
                photo_ids TEXT
            );
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS screen_msgs(chat_id INTEGER PRIMARY KEY, message_ids TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                product_id INTEGER,
                product_title TEXT,
                price TEXT,
                size TEXT,
                delivery TEXT,
                receipt_file_id TEXT,
                receipt_type TEXT,
                status TEXT NOT NULL DEFAULT 'unpaid',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # миграции со старых версий
        cur = await db.execute("PRAGMA table_info(products)")
        cols = {r[1] for r in await cur.fetchall()}
        if "photo_ids" not in cols:
            await db.execute("ALTER TABLE products ADD COLUMN photo_ids TEXT")
            await db.execute("UPDATE products SET photo_ids = photo_id")
        cur = await db.execute("PRAGMA table_info(orders)")
        cols = {r[1] for r in await cur.fetchall()}
        for col in ("size", "delivery", "receipt_file_id", "receipt_type"):
            if col not in cols:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    row = await q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await q("REPLACE INTO settings VALUES(?, ?)", (key, value), write=True)


async def list_products():
    return await q("SELECT id, title, price FROM products ORDER BY id")


async def get_product(pid: int):
    return await q(
        "SELECT id, title, description, price, COALESCE(photo_ids, photo_id) AS photos FROM products WHERE id=?",
        (pid,), one=True,
    )


async def add_product(title: str, description: str, price: str | None, photos: list[str]) -> None:
    await q(
        "INSERT INTO products(title, description, price, photo_id, photo_ids) VALUES(?,?,?,?,?)",
        (title, description, price, photos[0], ",".join(photos)), write=True,
    )


async def delete_product(pid: int) -> None:
    await q("DELETE FROM products WHERE id=?", (pid,), write=True)


async def get_order(oid: int):
    return await q("SELECT * FROM orders WHERE id=?", (oid,), one=True)


async def set_status(oid: int, status: str) -> None:
    await q("UPDATE orders SET status=? WHERE id=?", (status, oid), write=True)


async def create_order(user, product, size: str, delivery: str) -> int:
    return await q(
        "INSERT INTO orders(user_id, username, full_name, product_id, product_title, price, size, delivery) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user.id, user.username, user.full_name, product["id"], product["title"], product["price"], size, delivery),
        write=True,
    )


async def get_awaiting_order(user_id: int):
    return await q(
        "SELECT * FROM orders WHERE user_id=? AND status='awaiting_receipt' ORDER BY id DESC LIMIT 1",
        (user_id,), one=True,
    )


async def get_screen(chat_id: int) -> list[int]:
    row = await q("SELECT message_ids FROM screen_msgs WHERE chat_id=?", (chat_id,), one=True)
    return [int(x) for x in row["message_ids"].split(",") if x] if row else []


async def save_screen(chat_id: int, ids: list[int]) -> None:
    await q("REPLACE INTO screen_msgs VALUES(?, ?)", (chat_id, ",".join(map(str, ids))), write=True)


# ───────────────────── «Чистый чат»: один экран ─────────────────────
async def drop_messages(bot: Bot, chat_id: int, ids: list[int]) -> None:
    if not ids:
        return
    try:
        await bot.delete_messages(chat_id, ids)
    except TelegramAPIError:
        for mid in ids:
            try:
                await bot.delete_message(chat_id, mid)
            except TelegramAPIError:
                pass


async def show(
    bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup,
    photo: str | None = None, document: str | None = None,
) -> None:
    """Отправляет новый экран и удаляет предыдущие сообщения бота."""
    old = await get_screen(chat_id)
    if photo:
        sent = await bot.send_photo(chat_id, photo, caption=text, reply_markup=kb)
    elif document:
        sent = await bot.send_document(chat_id, document, caption=text, reply_markup=kb)
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=kb)
    await save_screen(chat_id, [sent.message_id])
    await drop_messages(bot, chat_id, old)


class CleanUserMessages(BaseMiddleware):
    """Удаляет любое сообщение пользователя после обработки."""

    async def __call__(self, handler, event: Message, data):
        try:
            return await handler(event, data)
        finally:
            try:
                await event.delete()
            except TelegramAPIError:
                pass


async def notify(bot: Bot, user_id: int, text: str) -> bool:
    """Обычное сообщение (не «экран»): остаётся в чате, не удаляется."""
    try:
        await bot.send_message(user_id, text)
        return True
    except TelegramAPIError:
        return False


def buyer_link(o) -> str:
    s = f'<a href="tg://user?id={o["user_id"]}">{html.escape(o["full_name"] or "Покупатель")}</a>'
    if o["username"]:
        s += f' (@{o["username"]})'
    return s


# ───────────────────────── Экраны покупателя ─────────────────────────
async def screen_home(bot: Bot, chat_id: int, user_id: int) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="👕 Все футболки", callback_data="catalog")
    if user_id in ADMIN_IDS:
        kb.button(text="⚙️ Админ-панель", callback_data="adm:menu")
    kb.adjust(1)
    await show(bot, chat_id, await get_setting("greeting", DEFAULT_GREETING), kb.as_markup())


async def screen_catalog(bot: Bot, chat_id: int) -> None:
    products = await list_products()
    kb = InlineKeyboardBuilder()
    for p in products:
        label = f"{p['title']} — {p['price']}" if p["price"] else p["title"]
        kb.button(text=label, callback_data=f"p:{p['id']}")
    kb.button(text="⬅️ Назад", callback_data="home")
    kb.adjust(1)
    text = "👕 <b>Все футболки</b>\nВыберите футболку:" if products else "Пока здесь пусто — товары скоро появятся 🙌"
    await show(bot, chat_id, text, kb.as_markup())


async def screen_product(bot: Bot, chat_id: int, pid: int) -> None:
    """Фото товара (до 3) и кнопка «Оформить заказ»."""
    product = await get_product(pid)
    if not product:
        await screen_catalog(bot, chat_id)
        return
    photos = [p for p in product["photos"].split(",") if p][:MAX_PHOTOS]
    caption = f"<b>{html.escape(product['title'])}</b>\n\n{product['description']}"
    if product["price"]:
        caption += f"\n\n💰 <b>{html.escape(product['price'])}</b>"
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Оформить заказ", callback_data=f"order:{pid}")
    kb.button(text="⬅️ К списку", callback_data="catalog")
    kb.adjust(1)

    old = await get_screen(chat_id)
    ids: list[int] = []
    if len(photos) == 1:
        sent = await bot.send_photo(chat_id, photos[0], caption=caption, reply_markup=kb.as_markup())
        ids.append(sent.message_id)
    else:
        media = [InputMediaPhoto(media=p, caption=caption if i == 0 else None) for i, p in enumerate(photos)]
        ids += [m.message_id for m in await bot.send_media_group(chat_id, media)]
        sent = await bot.send_message(chat_id, "Нравится футболка? Оформляйте заказ 👇", reply_markup=kb.as_markup())
        ids.append(sent.message_id)
    await save_screen(chat_id, ids)
    await drop_messages(bot, chat_id, old)


async def screen_size(bot: Bot, chat_id: int, pid: int) -> None:
    photo = await get_setting("size_photo", "")
    hint = await get_setting("size_text", "") or DEFAULT_SIZE_HINT
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="cancel_flow")
    await show(bot, chat_id, f"📏 <b>Введите размер</b>\n\n{hint}", kb.as_markup(), photo=photo or None)


async def screen_delivery(bot: Bot, chat_id: int, note: str = "") -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Изменить размер", callback_data="back_size")
    kb.button(text="✖️ Отмена", callback_data="cancel_flow")
    kb.adjust(1)
    await show(bot, chat_id, (note + "\n\n" if note else "") + DELIVERY_PROMPT, kb.as_markup())


async def screen_payment(bot: Bot, chat_id: int, oid: int) -> None:
    o = await get_order(oid)
    requisites = await get_setting("requisites_text", DEFAULT_REQUISITES)
    photo = await get_setting("requisites_photo", "")
    head = f"💳 <b>Оплата заказа</b>\n{html.escape(o['product_title'] or '')}"
    if o["price"]:
        head += f" — <b>{html.escape(o['price'])}</b>"
    head += f"\nРазмер: <b>{html.escape(o['size'] or '—')}</b>"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил", callback_data=f"pay:{oid}")
    kb.button(text="✖️ Отменить заказ", callback_data=f"cancel_order:{oid}")
    kb.adjust(1)
    await show(bot, chat_id, f"{head}\n\n{requisites}", kb.as_markup(), photo=photo or None)


async def screen_receipt(bot: Bot, chat_id: int, note: str = "") -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В начало", callback_data="home")
    text = (
        "🧾 <b>Пришлите чек об оплате</b>\n\n"
        "Отправьте сюда <b>PDF-файл</b> или <b>скриншот</b> чека — после этого заказ уйдёт на проверку."
    )
    await show(bot, chat_id, (note + "\n\n" if note else "") + text, kb.as_markup())


# ───────────────────────── Экраны админа ─────────────────────────
async def screen_admin(bot: Bot, chat_id: int, note: str = "") -> None:
    pending = (await q("SELECT COUNT(*) AS n FROM orders WHERE status='pending'", one=True))["n"]
    kb = InlineKeyboardBuilder()
    kb.button(text=f"📦 Заказы ({pending} новых)" if pending else "📦 Заказы", callback_data="adm:orders")
    kb.button(text="➕ Добавить футболку", callback_data="adm:add")
    kb.button(text="🗂 Товары / удаление", callback_data="adm:list")
    kb.button(text="💳 Реквизиты оплаты", callback_data="adm:requisites")
    kb.button(text="📏 Таблица размеров", callback_data="adm:sizes")
    kb.button(text="✏️ Изменить приветствие", callback_data="adm:greet")
    kb.button(text="⬅️ В магазин", callback_data="home")
    kb.adjust(1)
    text = (note + "\n\n" if note else "") + "⚙️ <b>Админ-панель</b>"
    await show(bot, chat_id, text, kb.as_markup())


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="adm:menu")
    return kb.as_markup()


async def screen_orders(bot: Bot, chat_id: int, note: str = "") -> None:
    rows = await q(
        "SELECT id, status, product_title, size, full_name FROM orders "
        "WHERE status IN ('pending','paid','shipped') ORDER BY id DESC LIMIT 30"
    )
    kb = InlineKeyboardBuilder()
    for r in rows:
        label = f"{ACTIVE_MARK[r['status']]} №{r['id']} · {r['product_title']} · {r['size'] or '—'} · {r['full_name'] or ''}"
        kb.button(text=label[:55], callback_data=f"adm:order:{r['id']}")
    kb.button(text="⬅️ Назад", callback_data="adm:menu")
    kb.adjust(1)
    text = (
        "📦 <b>Активные заказы</b>\n🟡 проверить оплату · 🟢 оплачен · 📦 отправлен"
        if rows else "📦 Активных заказов нет."
    )
    await show(bot, chat_id, (note + "\n\n" if note else "") + text, kb.as_markup())


async def screen_order(bot: Bot, chat_id: int, oid: int, note: str = "") -> None:
    o = await get_order(oid)
    if not o:
        await screen_orders(bot, chat_id)
        return
    text = (
        f"📦 <b>Заказ №{o['id']}</b>\n"
        f"Статус: {STATUS_LABEL.get(o['status'], o['status'])}\n"
        f"Товар: {html.escape(o['product_title'] or '—')}"
        + (f" — {html.escape(o['price'])}" if o["price"] else "")
        + f"\nРазмер: <b>{html.escape(o['size'] or '—')}</b>"
        f"\nДоставка и адрес: {html.escape(o['delivery'] or '—')}"
        f"\nПокупатель: {buyer_link(o)}"
        f"\nСоздан: {o['created_at'][:16]} (UTC)"
    )
    if note:
        text = note + "\n\n" + text
    kb = InlineKeyboardBuilder()
    if o["status"] == "pending":
        kb.button(text="✅ Оплата прошла", callback_data=f"adm:ok:{oid}")
        kb.button(text="❌ Оплата не найдена", callback_data=f"adm:no:{oid}")
    elif o["status"] in ("paid", "shipped"):
        kb.button(text="📨 Отправить трек-номер / информацию", callback_data=f"adm:send:{oid}")
        kb.button(text="🏁 Завершить заказ", callback_data=f"adm:done:{oid}")
    kb.button(text="⬅️ К заказам", callback_data="adm:orders")
    kb.adjust(1)
    fid, kind = o["receipt_file_id"], o["receipt_type"]
    await show(
        bot, chat_id, text, kb.as_markup(),
        photo=fid if fid and kind == "photo" else None,
        document=fid if fid and kind == "document" else None,
    )


# ───────────────────────── Общие команды ─────────────────────────
@admin_router.message(CommandStart())
@user_router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    await screen_home(bot, message.chat.id, message.from_user.id)


# ───────────────────────── Покупатель ─────────────────────────
@user_router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.clear()
    await screen_home(bot, cb.message.chat.id, cb.from_user.id)


@user_router.callback_query(F.data == "catalog")
async def cb_catalog(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.clear()
    await screen_catalog(bot, cb.message.chat.id)


@user_router.callback_query(F.data.startswith("p:"))
async def cb_product(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.clear()
    await screen_product(bot, cb.message.chat.id, int(cb.data.split(":")[1]))


# --- шаг 1: размер ---
@user_router.callback_query(F.data.startswith("order:"))
async def cb_order(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    pid = int(cb.data.split(":")[1])
    if not await get_product(pid):
        await screen_catalog(bot, cb.message.chat.id)
        return
    await state.set_state(Checkout.size)
    await state.update_data(product_id=pid)
    await screen_size(bot, cb.message.chat.id, pid)


@user_router.callback_query(F.data == "cancel_flow")
async def cb_cancel_flow(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    await state.clear()
    if data.get("product_id"):
        await screen_product(bot, cb.message.chat.id, data["product_id"])
    else:
        await screen_catalog(bot, cb.message.chat.id)


@user_router.callback_query(Checkout.delivery, F.data == "back_size")
async def cb_back_size(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    await state.set_state(Checkout.size)
    await screen_size(bot, cb.message.chat.id, data["product_id"])


@user_router.message(Checkout.size, F.text)
async def got_size(message: Message, bot: Bot, state: FSMContext):
    await state.update_data(size=message.text.strip()[:MAX_SIZE])
    await state.set_state(Checkout.delivery)
    await screen_delivery(bot, message.chat.id)


# --- шаг 2: доставка и адрес одним сообщением ---
@user_router.message(Checkout.delivery, F.text)
async def got_delivery(message: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    product = await get_product(data["product_id"])
    if not product:
        await state.clear()
        await screen_catalog(bot, message.chat.id)
        return
    oid = await create_order(message.from_user, product, data["size"], message.text.strip()[:MAX_DELIVERY])
    await state.clear()
    await screen_payment(bot, message.chat.id, oid)  # шаг 3: реквизиты + «Я оплатил»


# --- шаг 3-4: оплата и чек ---
@user_router.callback_query(F.data.startswith("cancel_order:"))
async def cb_cancel_order(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    o = await get_order(int(cb.data.split(":")[1]))
    if o and o["user_id"] == cb.from_user.id and o["status"] == "unpaid":
        await set_status(o["id"], "cancelled")
    await screen_catalog(bot, cb.message.chat.id)


@user_router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    o = await get_order(int(cb.data.split(":")[1]))
    if not o or o["user_id"] != cb.from_user.id:
        return
    if o["status"] == "unpaid":
        await set_status(o["id"], "awaiting_receipt")
        await screen_receipt(bot, cb.message.chat.id)
    elif o["status"] == "awaiting_receipt":
        await screen_receipt(bot, cb.message.chat.id)
    elif o["status"] == "pending":
        kb = InlineKeyboardBuilder()
        kb.button(text="👕 К футболкам", callback_data="catalog")
        await show(bot, cb.message.chat.id, "⏳ Чек уже на проверке. Напишем сюда, как только подтвердим оплату.", kb.as_markup())
    else:
        await screen_catalog(bot, cb.message.chat.id)


@user_router.message(F.photo | F.document)
async def got_receipt(message: Message, bot: Bot):
    o = await get_awaiting_order(message.from_user.id)
    if not o:
        return
    if message.photo:
        kind, fid = "photo", message.photo[-1].file_id
    else:
        mime = message.document.mime_type or ""
        if mime != "application/pdf" and not mime.startswith("image/"):
            await screen_receipt(bot, message.chat.id, "⚠️ Нужен PDF-файл или скриншот чека.")
            return
        kind, fid = "document", message.document.file_id
    await q(
        "UPDATE orders SET receipt_file_id=?, receipt_type=?, status='pending' WHERE id=?",
        (fid, kind, o["id"]), write=True,
    )
    text = (
        f"🧾 <b>Чек получен — проверьте оплату</b>\n"
        f"Заказ №{o['id']} · {html.escape(o['product_title'] or '')} · {html.escape(o['size'] or '—')}\n"
        f"Покупатель: {buyer_link(o)}"
    )
    akb = InlineKeyboardBuilder()
    akb.button(text="📦 Открыть заказ", callback_data=f"adm:order:{o['id']}")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=akb.as_markup())
        except TelegramAPIError:
            pass
    kb = InlineKeyboardBuilder()
    kb.button(text="👕 К футболкам", callback_data="catalog")
    await show(
        bot, message.chat.id,
        "⏳ <b>Чек получен!</b>\nПроверяем оплату. Как только подтвердим — пришлём номер заказа, "
        "а затем трек-номер и информацию о заказе.",
        kb.as_markup(),
    )


@user_router.message(F.text)
async def other_text(message: Message, bot: Bot):
    # покупатель пишет текст, пока мы ждём чек — подсказываем, что нужен файл
    if await get_awaiting_order(message.from_user.id):
        await screen_receipt(bot, message.chat.id, "⚠️ Нужен PDF-файл или скриншот чека.")


# ───────────────────────── Админ-панель ─────────────────────────
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    await screen_admin(bot, message.chat.id)


@admin_router.callback_query(F.data == "adm:menu")
async def cb_admin_menu(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.clear()
    await screen_admin(bot, cb.message.chat.id)


# --- заказы ---
@admin_router.callback_query(F.data == "adm:orders")
async def cb_orders(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.clear()
    await screen_orders(bot, cb.message.chat.id)


@admin_router.callback_query(F.data.startswith("adm:order:"))
async def cb_order_open(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.clear()
    await screen_order(bot, cb.message.chat.id, int(cb.data.split(":")[2]))
    try:  # push-уведомление о заказе, если открыли из него
        await cb.message.delete()
    except TelegramAPIError:
        pass


@admin_router.callback_query(F.data.startswith("adm:ok:"))
async def cb_pay_ok(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    oid = int(cb.data.split(":")[2])
    o = await get_order(oid)
    note = ""
    if o and o["status"] == "pending":
        await set_status(oid, "paid")
        delivered = await notify(
            bot, o["user_id"],
            f"✅ <b>Оплата подтверждена!</b>\nНомер вашего заказа: <b>№{oid}</b>\n\n"
            "Трек-номер и информацию о заказе пришлём в этом чате.",
        )
        note = (
            "✅ Оплата подтверждена, номер заказа отправлен покупателю"
            if delivered
            else "✅ Оплата подтверждена, но доставить сообщение покупателю не удалось (заблокировал бота?)"
        )
    await screen_order(bot, cb.message.chat.id, oid, note)


@admin_router.callback_query(F.data.startswith("adm:no:"))
async def cb_pay_no(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    oid = int(cb.data.split(":")[2])
    o = await get_order(oid)
    note = ""
    if o and o["status"] == "pending":
        await set_status(oid, "awaiting_receipt")  # покупатель может прислать чек заново
        await notify(
            bot, o["user_id"],
            f"❌ По заказу №{oid} оплату найти не удалось. Проверьте перевод и пришлите чек "
            "(PDF или скриншот) в этот чат ещё раз.",
        )
        note = "❌ Покупателю отправлено, что оплата не найдена. Ждём новый чек."
    await screen_orders(bot, cb.message.chat.id, note=note)


@admin_router.callback_query(F.data.startswith("adm:done:"))
async def cb_done(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await set_status(int(cb.data.split(":")[2]), "done")
    await screen_orders(bot, cb.message.chat.id, note="🏁 Заказ завершён")


# --- отправка трек-номера и информации покупателю (свободный текст + любое число фото) ---
async def show_send_screen(bot: Bot, chat_id: int, oid: int, sent: int, note: str = "") -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"adm:order:{oid}")
    text = (
        f"📨 <b>Отправка покупателю · заказ №{oid}</b>\n\n"
        "Присылайте сюда трек-номер, информацию о заказе — любой текст и сколько угодно фото. "
        "Всё сразу уходит покупателю.\n\n"
        f"Отправлено сообщений: <b>{sent}</b>"
    )
    if note:
        text = note + "\n\n" + text
    await show(bot, chat_id, text, kb.as_markup())


@admin_router.callback_query(F.data.startswith("adm:send:"))
async def cb_send(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    oid = int(cb.data.split(":")[2])
    await state.set_state(SendToBuyer.active)
    await state.update_data(order_id=oid, sent=0)
    await show_send_screen(bot, cb.message.chat.id, oid, 0)


@admin_router.message(SendToBuyer.active)
async def send_to_buyer(message: Message, bot: Bot, state: FSMContext):
    async with FSM_LOCK:
        data = await state.get_data()
        if "order_id" not in data:
            return
        o = await get_order(data["order_id"])
        if not o:
            return
        sent, note = data.get("sent", 0), ""
        try:
            await bot.copy_message(o["user_id"], message.chat.id, message.message_id)
            sent += 1
            if o["status"] == "paid":
                await set_status(o["id"], "shipped")
        except TelegramAPIError:
            note = "⚠️ Не удалось доставить сообщение покупателю"
        await state.update_data(sent=sent)
        await show_send_screen(bot, message.chat.id, o["id"], sent, note)


# --- добавление товара (до 3 фото) ---
def photos_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="adm:photos_done")
    kb.button(text="✖️ Отмена", callback_data="adm:menu")
    kb.adjust(1)
    return kb.as_markup()


@admin_router.callback_query(F.data == "adm:add")
async def cb_add(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.set_state(AddProduct.photos)
    await state.update_data(photos=[])
    await show(
        bot, cb.message.chat.id,
        f"📸 Пришлите <b>от 1 до {MAX_PHOTOS} фото</b> футболки (по одному или альбомом).",
        cancel_kb(),
    )


async def ask_title(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await state.set_state(AddProduct.title)
    await show(bot, chat_id, "✏️ Пришлите <b>название</b> (например: «Месси — Барселона»)", cancel_kb())


@admin_router.message(AddProduct.photos, F.photo)
async def add_photo(message: Message, bot: Bot, state: FSMContext):
    async with FSM_LOCK:
        if await state.get_state() != AddProduct.photos.state:
            return
        data = await state.get_data()
        photos = data.get("photos", [])
        if len(photos) >= MAX_PHOTOS:
            return
        photos.append(message.photo[-1].file_id)
        await state.update_data(photos=photos)
        if len(photos) >= MAX_PHOTOS:
            await ask_title(bot, message.chat.id, state)
        else:
            await show(
                bot, message.chat.id,
                f"📸 Получено фото: <b>{len(photos)}/{MAX_PHOTOS}</b>. Пришлите ещё или нажмите «Готово».",
                photos_kb(),
            )


@admin_router.callback_query(AddProduct.photos, F.data == "adm:photos_done")
async def add_photos_done(cb: CallbackQuery, bot: Bot, state: FSMContext):
    async with FSM_LOCK:
        data = await state.get_data()
        if not data.get("photos"):
            await cb.answer("Пришлите хотя бы одно фото", show_alert=True)
            return
        await cb.answer()
        await ask_title(bot, cb.message.chat.id, state)


@admin_router.message(AddProduct.title, F.text)
async def add_title(message: Message, bot: Bot, state: FSMContext):
    await state.update_data(title=message.text.strip()[:60])
    await state.set_state(AddProduct.description)
    await show(
        bot, message.chat.id,
        "📝 Пришлите <b>описание</b> (ткань, материал и т.д.). Можно с форматированием Telegram.",
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
    await add_product(data["title"], data["description"], price, data["photos"])
    await state.clear()
    await screen_admin(bot, message.chat.id, note=f"✅ Футболка «{html.escape(data['title'])}» добавлена")


# --- список и удаление товаров ---
@admin_router.callback_query(F.data == "adm:list")
async def cb_admin_list(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    products = await list_products()
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=f"🗑 {p['title']}", callback_data=f"adm:del:{p['id']}")
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
    await show(bot, cb.message.chat.id, f"Удалить «{html.escape(product['title'])}»?", kb.as_markup())


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
        "Текущее:\n" + await get_setting("greeting", DEFAULT_GREETING),
        cancel_kb(),
    )


@admin_router.message(EditGreeting.text, F.text)
async def greet_save(message: Message, bot: Bot, state: FSMContext):
    await set_setting("greeting", message.html_text)
    await state.clear()
    await screen_admin(bot, message.chat.id, note="✅ Приветствие обновлено")


# --- реквизиты оплаты (текст + необязательное фото) ---
@admin_router.callback_query(F.data == "adm:requisites")
async def cb_requisites(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.set_state(EditRequisites.text)
    await show(
        bot, cb.message.chat.id,
        "💳 Пришлите <b>текст с реквизитами</b>: карта/счёт, сумма, инструкция. "
        f"Можно с форматированием Telegram (до {MAX_REQUISITES} символов). "
        "Он показывается покупателю под кнопкой «Я оплатил».\n\n"
        "Сейчас:\n" + await get_setting("requisites_text", DEFAULT_REQUISITES),
        cancel_kb(),
    )


@admin_router.message(EditRequisites.text, F.text)
async def requisites_text(message: Message, bot: Bot, state: FSMContext):
    text = message.html_text
    if len(text) > MAX_REQUISITES:
        await show(
            bot, message.chat.id,
            f"⚠️ Слишком длинно ({len(text)} симв., максимум {MAX_REQUISITES}). Пришлите короче.",
            cancel_kb(),
        )
        return
    await state.update_data(requisites_text=text)
    await state.set_state(EditRequisites.photo)
    kb = InlineKeyboardBuilder()
    kb.button(text="Без фото", callback_data="adm:rq_nophoto")
    kb.button(text="✖️ Отмена", callback_data="adm:menu")
    kb.adjust(1)
    await show(
        bot, message.chat.id,
        "📷 Пришлите <b>фото</b> к реквизитам (QR-код и т.п.) или нажмите «Без фото».",
        kb.as_markup(),
    )


async def save_requisites(bot: Bot, chat_id: int, state: FSMContext, photo_id: str) -> None:
    data = await state.get_data()
    await set_setting("requisites_text", data["requisites_text"])
    await set_setting("requisites_photo", photo_id)
    await state.clear()
    await screen_admin(bot, chat_id, note="✅ Реквизиты обновлены")


@admin_router.message(EditRequisites.photo, F.photo)
async def requisites_photo(message: Message, bot: Bot, state: FSMContext):
    await save_requisites(bot, message.chat.id, state, message.photo[-1].file_id)


@admin_router.callback_query(EditRequisites.photo, F.data == "adm:rq_nophoto")
async def requisites_nophoto(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await save_requisites(bot, cb.message.chat.id, state, "")


# --- таблица размеров (фото; подпись к фото = текст под «Введите размер») ---
@admin_router.callback_query(F.data == "adm:sizes")
async def cb_sizes(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await state.set_state(EditSizes.photo)
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Убрать таблицу", callback_data="adm:sz_none")
    kb.button(text="✖️ Отмена", callback_data="adm:menu")
    kb.adjust(1)
    await show(
        bot, cb.message.chat.id,
        "📏 Пришлите <b>фото таблицы размеров</b>. Она прикрепится к окну «Введите размер».\n"
        "Если добавите подпись к фото, она станет текстом под заголовком (иначе — стандартная подсказка).",
        kb.as_markup(),
    )


@admin_router.message(EditSizes.photo, F.photo)
async def sizes_save(message: Message, bot: Bot, state: FSMContext):
    await set_setting("size_photo", message.photo[-1].file_id)
    await set_setting("size_text", message.html_text if message.caption else "")
    await state.clear()
    await screen_admin(bot, message.chat.id, note="✅ Таблица размеров обновлена")


@admin_router.callback_query(F.data == "adm:sz_none")
async def sizes_none(cb: CallbackQuery, bot: Bot, state: FSMContext):
    await cb.answer()
    await set_setting("size_photo", "")
    await set_setting("size_text", "")
    await state.clear()
    await screen_admin(bot, cb.message.chat.id, note="🗑 Таблица размеров убрана")


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
