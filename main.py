import asyncio
import json
import os
import secrets
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

# === Ссылка на канал, которая будет добавляться под каждый пост ===
CHANNEL_LINK = "https://t.me/kitay_gorod_podsluchka"
CHANNEL_NAME = "Подслушано КГ"

DB = "bot.db"


# ---------- БАЗА ДАННЫХ ----------
def db_init():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        user_id INTEGER,
        username TEXT,
        type TEXT,
        content TEXT,
        media_ids TEXT,
        status TEXT DEFAULT 'pending',
        reject_reason TEXT,
        created_at TEXT,
        moderated_at TEXT,
        published_at TEXT,
        message_id_in_channel INTEGER,
        admin_msg_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        banned INTEGER DEFAULT 0,
        submissions_count INTEGER DEFAULT 0,
        last_submit_at TEXT
    );
    """)
    con.commit()
    con.close()


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


# ---------- УТИЛИТЫ ----------
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def gen_code():
    return "KG-" + "".join(secrets.choice(ALPHABET) for _ in range(5))


async def is_admin(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(ADMIN_CHAT_ID, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


# ---------- FSM ----------
class Form(StatesGroup):
    waiting_post = State()


# ---------- РОУТЕРЫ ----------
user_router = Router()
admin_router = Router()


def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Отправить пост", callback_data="send")],
        [InlineKeyboardButton(text="🔍 Проверить статус", callback_data="status")],
        [InlineKeyboardButton(text="📜 Правила", callback_data="rules")],
    ])


@user_router.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "Привет! Это бот подслушки Китай-города.\n"
        "Отправь пост — он уйдёт на модерацию, получишь код для отслеживания.",
        reply_markup=main_menu()
    )


@user_router.callback_query(F.data == "rules")
async def cb_rules(c: CallbackQuery):
    await c.message.answer(
        "📜 Правила:\n"
        "— без порно и жести\n"
        "— без личных данных (адреса, телефоны)\n"
        "— без спама и рекламы\n"
        "— антифлуд: 1 пост в 15 минут"
    )
    await c.answer()


@user_router.callback_query(F.data == "send")
async def cb_send(c: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_post)
    await c.message.answer("Отправь текст, фото или видео. Можно добавить подпись.")
    await c.answer()


@user_router.callback_query(F.data == "status")
async def cb_status(c: CallbackQuery):
    await c.message.answer("Введи команду:\n/status KG-XXXXX")
    await c.answer()


@user_router.message(Command("status"))
async def cmd_status(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Формат: /status KG-XXXXX")
    code = parts[1].strip().upper()
    if not code.startswith("KG-"):
        code = "KG-" + code
    con = db()
    row = con.execute("SELECT * FROM submissions WHERE code=?", (code,)).fetchone()
    con.close()
    if not row:
        return await m.answer("❌ Заявка с таким кодом не найдена.")
    status_map = {
        "pending": "🕐 На модерации",
        "approved": "✅ Опубликовано",
        "rejected": f"❌ Отклонено: {row['reject_reason'] or 'без причины'}",
        "scheduled": "🕐 Отложено на публикацию",
        "deleted": "🗑 Удалено из канала",
    }
    await m.answer(f"Код {code}: {status_map.get(row['status'], row['status'])}")


# ---------- КОМАНДА /delete (только для админов) ----------
@admin_router.message(Command("delete"))
async def cmd_delete(m: Message, bot: Bot):
    if not await is_admin(bot, m.from_user.id):
        return await m.answer("🚫 Команда только для админов.")

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.answer("Формат: /delete KG-XXXXX")

    code = parts[1].strip().upper()
    if not code.startswith("KG-"):
        code = "KG-" + code

    con = db()
    row = con.execute("SELECT * FROM submissions WHERE code=?", (code,)).fetchone()
    if not row:
        con.close()
        return await m.answer(f"❌ Заявка {code} не найдена.")

    msg_id = row["message_id_in_channel"]
    if not msg_id:
        con.close()
        return await m.answer(f"❌ Пост {code} не был опубликован в канале (статус: {row['status']}).")

    try:
        await bot.delete_message(CHANNEL_ID, msg_id)
    except Exception as e:
        con.close()
        return await m.answer(f"❌ Не удалось удалить сообщение из канала: {e}")

    uid = row["user_id"]
    con.execute("UPDATE submissions SET status='deleted', reject_reason='удалено' WHERE id=?", (row["id"],))
    con.commit()
    con.close()

    await m.answer(f"🗑 Пост {code} удалён из канала.")
    try:
        await bot.send_message(uid, f"🗑 Твой пост <code>{code}</code> удалён из канала.")
    except Exception:
        pass


# ---------- ПРИЁМ ПОСТОВ ----------
@user_router.message(Form.waiting_post, F.text | F.photo | F.video | F.animation)
async def receive_post(m: Message, state: FSMContext):
    con = db()
    u = con.execute("SELECT * FROM users WHERE user_id=?", (m.from_user.id,)).fetchone()

    if u and u["banned"]:
        con.close()
        return await m.answer("🚫 Ты забанен и не можешь отправлять посты.")

    now = datetime.utcnow()
    if u and u["last_submit_at"]:
        last = datetime.fromisoformat(u["last_submit_at"])
        if (now - last).total_seconds() < 60:
            wait_sec = int(60 - (now - last).total_seconds()) + 1
            con.close()
            return await m.answer(f"⏳ Подожди ещё {wait_sec} сек. перед следующим постом.")

    text = m.caption or m.text or ""

    if m.photo:
        typ, file_id = "photo", m.photo[-1].file_id
    elif m.video:
        typ, file_id = "video", m.video.file_id
    elif m.animation:
        typ, file_id = "animation", m.animation.file_id
    else:
        typ, file_id = "text", None

    code = gen_code()
    media_ids = json.dumps([file_id]) if file_id else "[]"

    con.execute(
        "INSERT INTO submissions (code,user_id,username,type,content,media_ids,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (code, m.from_user.id, m.from_user.username, typ, text, media_ids, now.isoformat())
    )
    con.execute(
        """INSERT INTO users (user_id, username, submissions_count, last_submit_at)
           VALUES (?,?,1,?)
           ON CONFLICT(user_id) DO UPDATE SET
             submissions_count = submissions_count + 1,
             last_submit_at = excluded.last_submit_at,
             username = excluded.username""",
        (m.from_user.id, m.from_user.username, now.isoformat())
    )
    con.commit()
    sub_id = con.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    con.close()

    await send_to_admin(m.bot, sub_id, code, m, typ, file_id, text)

    await state.clear()
    await m.answer(
        f"✅ Пост принят на модерацию.\n"
        f"Твой код: <code>{code}</code>\n"
        f"Проверить статус: /status {code}",
        reply_markup=main_menu()
    )


async def send_to_admin(bot: Bot, sub_id, code, m, typ, file_id, text):
    header = (
        f"🆕 Заявка <code>{code}</code>\n"
        f"👤 Автор: @{m.from_user.username or 'anon'} (id {m.from_user.id})\n"
        f"🕐 {datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC\n"
        f"📊 Тип: {typ}\n"
        f"---\n{text or '(без текста)'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"ap:{sub_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"rj:{sub_id}"),
        ],
        [
            InlineKeyboardButton(text="🚫 Бан", callback_data=f"bn:{sub_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"dl:{sub_id}"),
        ],
    ])

    try:
        if typ == "photo":
            sent = await bot.send_photo(ADMIN_CHAT_ID, file_id, caption=header, reply_markup=kb)
        elif typ == "video":
            sent = await bot.send_video(ADMIN_CHAT_ID, file_id, caption=header, reply_markup=kb)
        elif typ == "animation":
            sent = await bot.send_animation(ADMIN_CHAT_ID, file_id, caption=header, reply_markup=kb)
        else:
            sent = await bot.send_message(ADMIN_CHAT_ID, header, reply_markup=kb)
    except Exception as e:
        print(f"Ошибка отправки в админ-чат: {e}")
        return

    con = db()
    con.execute("UPDATE submissions SET admin_msg_id=? WHERE id=?", (sent.message_id, sub_id))
    con.commit()
    con.close()


# ---------- ДЕЙСТВИЯ АДМИНА (кнопки) ----------
@admin_router.callback_query(F.data.startswith("ap:"))
async def cb_approve(c: CallbackQuery, bot: Bot):
    sub_id = int(c.data.split(":")[1])
    con = db()
    row = con.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if not row or row["status"] != "pending":
        con.close()
        return await c.answer("Уже обработано", show_alert=True)

    media_ids = json.loads(row["media_ids"] or "[]")
    text = row["content"] or ""

    code_hash = "#" + row["code"].replace("-", "_")
    channel_link = f'📢 <a href="{CHANNEL_LINK}">{CHANNEL_NAME}</a>'
    footer = f"\n\n🔎 {code_hash}\n{channel_link}"
    full_text = (text + footer).strip() if text else footer.strip()

    published = None
    try:
        if row["type"] == "photo":
            published = await bot.send_photo(CHANNEL_ID, media_ids[0], caption=full_text)
        elif row["type"] == "video":
            published = await bot.send_video(CHANNEL_ID, media_ids[0], caption=full_text)
        elif row["type"] == "animation":
            published = await bot.send_animation(CHANNEL_ID, media_ids[0], caption=full_text)
        else:
            published = await bot.send_message(CHANNEL_ID, full_text)
    except Exception as e:
        con.close()
        return await c.answer(f"Ошибка публикации: {e}", show_alert=True)

    now = datetime.utcnow().isoformat()
    con.execute(
        "UPDATE submissions SET status='approved', moderated_at=?, published_at=?, "
        "message_id_in_channel=? WHERE id=?",
        (now, now, published.message_id, sub_id)
    )
    con.commit()
    uid = row["user_id"]
    code = row["code"]
    con.close()

    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"✅ Опубликовано: {code}")
    try:
        await bot.send_message(uid, f"✅ Твой пост <code>{code}</code> опубликован!")
    except Exception:
        pass
    await c.answer("Опубликовано")


@admin_router.callback_query(F.data.startswith("rj:"))
async def cb_reject(c: CallbackQuery):
    sub_id = int(c.data.split(":")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Мат/оскорбления", callback_data=f"rr:{sub_id}:мат")],
        [InlineKeyboardButton(text="Порно/NSFW", callback_data=f"rr:{sub_id}:nsfw")],
        [InlineKeyboardButton(text="Жесть/кровь", callback_data=f"rr:{sub_id}:жесть")],
        [InlineKeyboardButton(text="Спам/реклама", callback_data=f"rr:{sub_id}:спам")],
        [InlineKeyboardButton(text="Не по теме", callback_data=f"rr:{sub_id}:не по теме")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
    ])
    await c.message.answer("Причина отклонения:", reply_markup=kb)
    await c.answer()


@admin_router.callback_query(F.data.startswith("rr:"))
async def cb_reject_reason(c: CallbackQuery, bot: Bot):
    _, sub_id, reason = c.data.split(":", 2)
    sub_id = int(sub_id)
    con = db()
    row = con.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if not row:
        con.close()
        return await c.answer("Не найдено")
    con.execute(
        "UPDATE submissions SET status='rejected', reject_reason=?, moderated_at=? WHERE id=?",
        (reason, datetime.utcnow().isoformat(), sub_id)
    )
    con.commit()
    uid = row["user_id"]
    code = row["code"]
    con.close()
    await c.message.edit_text(f"❌ Отклонено: {code} ({reason})")
    try:
        await bot.send_message(uid, f"❌ Твой пост <code>{code}</code> отклонён. Причина: {reason}")
    except Exception:
        pass
    await c.answer()


@admin_router.callback_query(F.data.startswith("bn:"))
async def cb_ban(c: CallbackQuery, bot: Bot):
    sub_id = int(c.data.split(":")[1])
    con = db()
    row = con.execute("SELECT * FROM submissions WHERE id=?", (sub_id,)).fetchone()
    if not row:
        con.close()
        return await c.answer("Не найдено")
    con.execute("UPDATE users SET banned=1 WHERE user_id=?", (row["user_id"],))
    con.execute(
        "UPDATE submissions SET status='rejected', reject_reason='бан' WHERE id=?",
        (sub_id,)
    )
    con.commit()
    uid = row["user_id"]
    code = row["code"]
    con.close()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer(f"🚫 Забанен автор {code}")
    try:
        await bot.send_message(uid, "🚫 Ты забанен.")
    except Exception:
        pass
    await c.answer()


@admin_router.callback_query(F.data.startswith("dl:"))
async def cb_delete(c: CallbackQuery):
    sub_id = int(c.data.split(":")[1])
    con = db()
    con.execute(
        "UPDATE submissions SET status='rejected', reject_reason='удалено' WHERE id=?",
        (sub_id,)
    )
    con.commit()
    con.close()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("🗑 Удалено")
    await c.answer()


@admin_router.callback_query(F.data == "cancel")
async def cb_cancel(c: CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Отменено")


# ---------- СОЗДАНИЕ БОТА И ДИСПЕТЧЕРА (на уровне файла) ----------
db_init()
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(admin_router)
dp.include_router(user_router)


# ---------- ВЕБХУК ----------
async def handle_webhook(request):
    """Обрабатывает вебхуки от Telegram."""
    try:
        update_data = await request.json()
        from aiogram.types import Update
        update = Update.model_validate(update_data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception as e:
        print(f"Ошибка при обработке вебхука: {e}")
    return web.Response()


async def on_startup(app):
    """Устанавливает вебхук при запуске."""
    base_url = app['webhook_base_url']
    webhook_url = f"{base_url}/webhook"
    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    print(f"✅ Вебхук установлен: {webhook_url}")


async def on_shutdown(app):
    """Удаляет вебхук при выключении."""
    await bot.delete_webhook()
    print("Вебхук удалён")


async def main():
    port = int(os.environ.get("PORT", 8080))
    webhook_base_url = os.environ.get("WEBHOOK_URL", f"http://localhost:{port}")

    app = web.Application()
    app['webhook_base_url'] = webhook_base_url
    app.router.add_post('/webhook', handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    print(f"🚀 Сервер запущен на порту {port}")
    print(f"🌐 Вебхук: {webhook_base_url}/webhook")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())