import os
import json
import asyncio
import threading
from datetime import datetime
import sqlite3

from flask import (
    Flask, render_template, send_from_directory,
    request, jsonify, session
)
from flask_cors import CORS
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError
)

# ═══════════ CONFIGURATION ═══════════
DATA_DIR = os.environ.get("DATA_DIR", "user_data")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")
DB_FILE = os.path.join(DATA_DIR, "database.db")
COOLDOWN_SECONDS = 300

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ═══════════ DATABASE SETUP ═══════════
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                 username TEXT PRIMARY KEY,
                 api_id TEXT,
                 api_hash TEXT,
                 phone_number TEXT,
                 reply_message TEXT,
                 auto_reply_enabled INTEGER,
                 connected INTEGER
                 )''')
    conn.commit()
    conn.close()

init_db()

def get_user(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username=?', (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "username": row[0], "api_id": row[1], "api_hash": row[2],
            "phone_number": row[3], "reply_message": row[4],
            "auto_reply_enabled": bool(row[5]), "connected": bool(row[6])
        }
    return None

def save_user(data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO users 
                 (username, api_id, api_hash, phone_number, reply_message, auto_reply_enabled, connected) 
                 VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                 (data.get("username"), data.get("api_id"), data.get("api_hash"),
                  data.get("phone_number"), data.get("reply_message", "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺"),
                  int(data.get("auto_reply_enabled", False)), int(data.get("connected", False))))
    conn.commit()
    conn.close()

# ═══════════ FLASK APP ═══════════
app = Flask(__name__, static_folder="public", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-key-change-in-prod")

# Frontend আলাদা হোস্ট করার জন্য CORS এবং Cookie সেটআপ
CORS(app, supports_credentials=True)
app.config['SESSION_COOKIE_SAMESITE'] = 'None'
app.config['SESSION_COOKIE_SECURE'] = True # Production এ HTTPS এর জন্য

# ═══════════ GLOBAL STATE ═══════════
clients = {}
phone_code_hashes = {}
auto_reply_tasks = {}
cooldowns = {}
_loop = None
_loop_thread = None

def _ensure_loop():
    global _loop, _loop_thread
    if _loop is not None: return _loop
    _loop = asyncio.new_event_loop()
    def _run_loop():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()
    _loop_thread = threading.Thread(target=_run_loop, daemon=True)
    _loop_thread.start()
    return _loop

def run_async(coro):
    loop = _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)

def loop_run(coro):
    future = run_async(coro)
    try: return future.result(timeout=30)
    except Exception as e: print(e); return None

def _session_path(username):
    return os.path.join(SESSIONS_DIR, f"tg_{username}")

async def _create_client(username, api_id, api_hash):
    client = TelegramClient(_session_path(username), int(api_id), api_hash)
    clients[username] = client
    return client

async def _get_client(username):
    if username in clients: return clients[username]
    user = get_user(username)
    if user and user.get("api_id") and user.get("api_hash"):
        return await _create_client(username, user["api_id"], user["api_hash"])
    return None

# ═══════════ AUTO REPLY SYSTEM ═══════════
async def _start_auto_reply(username):
    client = await _get_client(username)
    if not client or not client.is_connected(): return False
    await _stop_auto_reply(username)
    cooldowns[username] = {}
    user = get_user(username)
    reply_msg = user.get("reply_message", "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺")

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
            msg = current_user.get("reply_message", reply_msg)
            await event.reply(msg)
            cooldowns.setdefault(username, {})[str(peer_id)] = now
        except FloodWaitError as e: print(f"Flood wait: {e.seconds}s")
        except Exception as e: print(f"Error: {e}")

    auto_reply_tasks[username] = handler
    user["auto_reply_enabled"] = True
    save_user(user)
    return True

async def _stop_auto_reply(username):
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

# ═══════════ ROUTES (AUTH & DASHBOARD) ═══════════
@app.route("/")
def index(): return jsonify({"status": "Backend is running"})

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip().replace("@", "").lower()
    if not username: return jsonify({"success": False, "error": "Username required"}), 400
    session["username"] = username
    if not get_user(username):
        save_user({"username": username})
    return jsonify({"success": True, "username": username})

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    return jsonify({"success": True})

@app.route("/status", methods=["GET"])
def get_status():
    username = session.get("username")
    if not username: return jsonify({"logged_in": False})
    user = get_user(username)
    if not user: return jsonify({"logged_in": False})
    client = clients.get(username)
    actually_connected = False
    if client:
        try: actually_connected = client.is_connected() and loop_run(client.is_user_authorized())
        except: pass
    return jsonify({
        "logged_in": True, "username": username,
        "connected": user.get("connected", False) or actually_connected,
        "auto_reply_enabled": user.get("auto_reply_enabled", False),
        "reply_message": user.get("reply_message", "")
    })

@app.route("/send-otp", methods=["POST"])
def send_otp():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    data = request.get_json(force=True)
    api_id, api_hash, phone_number = data.get("api_id", "").strip(), data.get("api_hash", "").strip(), data.get("phone_number", "").strip()
    if not api_id or not api_hash or not phone_number: return jsonify({"success": False, "error": "All fields required"}), 400
    
    user = get_user(username)
    user["api_id"], user["api_hash"], user["phone_number"] = api_id, api_hash, phone_number
    save_user(user)
    
    result = loop_run(_async_send_otp(username, api_id, api_hash, phone_number))
    return jsonify(result or {"success": False, "error": "Timeout"})

async def _async_send_otp(username, api_id, api_hash, phone_number):
    try:
        old_client = clients.get(username)
        if old_client:
            try: await old_client.disconnect()
            except: pass
            del clients[username]
        client = await _create_client(username, api_id, api_hash)
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
    code, password = data.get("code", "").strip(), data.get("password", "")
    if not code: return jsonify({"success": False, "error": "OTP required"}), 400
    user = get_user(username)
    result = loop_run(_async_verify_otp(username, user["phone_number"], code, phone_code_hashes.get(username, ""), password))
    return jsonify(result or {"success": False, "error": "Timeout"})

async def _async_verify_otp(username, phone_number, code, code_hash, password):
    client = clients.get(username)
    if not client: return {"success": False, "error": "No active client"}
    try:
        await client.sign_in(phone_number, code, phone_code_hash=code_hash)
        user = get_user(username); user["connected"] = True; save_user(user)
        phone_code_hashes.pop(username, None)
        return {"success": True}
    except SessionPasswordNeededError:
        if not password: return {"success": False, "error": "2FA password required"}
        try:
            await client.sign_in(password=password)
            user = get_user(username); user["connected"] = True; save_user(user)
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
    if enabled and not user.get("connected"): return jsonify({"success": False, "error": "Connect first"}), 400
    if enabled:
        result = loop_run(_start_auto_reply(username))
        return jsonify({"success": bool(result)})
    else:
        loop_run(_stop_auto_reply(username))
        return jsonify({"success": True})

@app.route("/disconnect", methods=["POST"])
def disconnect():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    loop_run(_stop_auto_reply(username))
    client = clients.pop(username, None)
    if client: loop_run(client.disconnect())
    # Delete session file
    sp = _session_path(username)
    for ext in ["", ".session"]:
        f = sp + ext
        if os.path.exists(f):
            try: os.remove(f)
            except: pass
    user = get_user(username)
    user["connected"], user["auto_reply_enabled"], user["api_id"], user["api_hash"], user["phone_number"] = False, False, "", "", ""
    save_user(user)
    return jsonify({"success": True})

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT, debug=False)
