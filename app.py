import os
import json
import asyncio
import threading
import re
import time
import random
from datetime import datetime

from flask import (
    Flask, send_from_directory,
    request, jsonify, session
)
from telethon import TelegramClient, events, errors
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    FloodWaitError
)

# ═══════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════

DATA_DIR = os.environ.get("DATA_DIR", "user_data")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
COOLDOWN_SECONDS = 300  # 5 minutes per user for auto-reply

# List of POSITIVE reactions to randomize (Anti-ban measure)
POSITIVE_REACTIONS = ["👍", "❤️", "🔥", "🎉", "🥂", "💯", "💖", "🤩"]

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

app = Flask(__name__, static_folder="public", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "tg-premium-secret-key-change-in-prod")

# ═══════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════

clients = {}          
phone_code_hashes = {}
auto_reply_handlers = {}
cooldowns = {}

_loop = None
_loop_thread = None

# ═══════════════════════════════════════════
# ASYNC LOOP MANAGER
# ═══════════════════════════════════════════

def _ensure_loop():
    global _loop, _loop_thread
    if _loop is not None:
        return _loop
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

# ═══════════════════════════════════════════
# PERSISTENCE
# ═══════════════════════════════════════════

DEFAULT_SETTINGS = {
    "username": "",
    "api_id": "",
    "api_hash": "",
    "phone_number": "",
    "reply_message": "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺",
    "auto_reply_enabled": False,
    "connected": False,
    "admin_password": "admin123"
}

def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_SETTINGS, **data}
    except: pass
    return {**DEFAULT_SETTINGS}

def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

def load_tasks():
    try:
        if os.path.exists(TASKS_FILE):
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except: pass
    return {}

def save_tasks(tasks):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)

# ═══════════════════════════════════════════
# TELETHON HELPERS
# ═══════════════════════════════════════════

def _session_path(username):
    return os.path.join(SESSIONS_DIR, f"tg_{username}")

async def _create_client(username, api_id, api_hash):
    session_path = _session_path(username)
    client = TelegramClient(session_path, int(api_id), api_hash)
    clients[username] = client
    return client

async def _get_client(username):
    if username in clients:
        if not clients[username].is_connected():
            await clients[username].connect()
        return clients[username]
    settings = load_settings()
    if settings.get("api_id") and settings.get("api_hash"):
        return await _create_client(username, settings["api_id"], settings["api_hash"])
    return None

async def _restore_client(username):
    settings = load_settings()
    if not settings.get("api_id") or not settings.get("api_hash"): return False
    session_path = _session_path(username)
    if not os.path.exists(session_path + ".session"): return False
    try:
        client = await _create_client(username, settings["api_id"], settings["api_hash"])
        await client.connect()
        if await client.is_user_authorized():
            settings["connected"] = True
            save_settings(settings)
            if settings.get("auto_reply_enabled"):
                await _start_auto_reply(username)
            return True
        else:
            await client.disconnect()
            del clients[username]
            settings["connected"] = False
            save_settings(settings)
            return False
    except Exception:
        settings["connected"] = False
        save_settings(settings)
        return False

# ═══════════════════════════════════════════
# AUTO REPLY SYSTEM
# ═══════════════════════════════════════════

async def _start_auto_reply(username):
    client = await _get_client(username)
    if not client or not client.is_connected(): return False

    await _stop_auto_reply(username)
    cooldowns[username] = {}
    settings = load_settings()
    reply_msg = settings.get("reply_message", DEFAULT_SETTINGS["reply_message"])

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
            
            current_settings = load_settings()
            if not current_settings.get("auto_reply_enabled"): return
            
            msg = current_settings.get("reply_message", reply_msg)
            await event.reply(msg)
            cooldowns.setdefault(username, {})[str(peer_id)] = now
        except FloodWaitError as e:
            print(f"[AutoReply] Flood wait: {e.seconds}s")
        except Exception as e:
            print(f"[AutoReply] Error: {e}")

    auto_reply_handlers[username] = handler
    settings["auto_reply_enabled"] = True
    save_settings(settings)
    return True

async def _stop_auto_reply(username):
    client = clients.get(username)
    handler = auto_reply_handlers.pop(username, None)
    if client and handler:
        try: client.remove_event_handler(handler)
        except: pass
    settings = load_settings()
    settings["auto_reply_enabled"] = False
    save_settings(settings)
    cooldowns.pop(username, None)

# ═══════════════════════════════════════════
# POSITIVE REACTION SYSTEM (CORE LOGIC)
# ═══════════════════════════════════════════

def parse_post_link(link):
    pattern = r"https://t.me/([^/]+)/(\d+)"
    match = re.match(pattern, link)
    if match:
        return match.group(1), int(match.group(2))
    return None, None

async def process_reaction_task(task_id, channel, msg_id):
    tasks_db = load_tasks()
    tasks_db[task_id]["status"] = "RUNNING"
    save_tasks(tasks_db)

    success = 0
    failed = 0
    total_accounts = len(clients)

    for username, client in list(clients.items()):
        try:
            if not client.is_connected() or not await client.is_user_authorized():
                failed += 1
                continue

            # ANTI-BAN: Random delay between 3 to 8 seconds per account
            delay = random.uniform(3.0, 8.0)
            await asyncio.sleep(delay)

            # Select a random POSITIVE reaction for this account
            chosen_emoji = random.choice(POSITIVE_REACTIONS)

            await client(SendReactionRequest(
                peer=channel,
                msg_id=msg_id,
                reaction=[ReactionEmoji(emoticon=chosen_emoji)]
            ))
            success += 1
            
            # Update Live Progress
            tasks_db = load_tasks()
            if task_id in tasks_db:
                tasks_db[task_id]["success"] = success
                tasks_db[task_id]["failed"] = failed
                save_tasks(tasks_db)

        except FloodWaitError as e:
            print(f"[Reaction] Flood wait: {e.seconds}s")
            await asyncio.sleep(e.seconds + 5) # Strict respect for Telegram limits
            failed += 1
        except Exception as e:
            print(f"[Reaction] Error: {e}")
            failed += 1

    tasks_db = load_tasks()
    if task_id in tasks_db:
        tasks_db[task_id]["status"] = "COMPLETED"
        tasks_db[task_id]["success"] = success
        tasks_db[task_id]["failed"] = failed
        tasks_db[task_id]["completed_at"] = datetime.now().isoformat()
        save_tasks(tasks_db)

# ═══════════════════════════════════════════
# ROUTES (AUTH & AUTO REPLY)
# ═══════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("public", "index.html")

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip().replace("@", "").lower()
    if not username: return jsonify({"success": False, "error": "Username required"}), 400
    
    session["username"] = username
    settings = load_settings()
    if settings.get("username") != username:
        settings = {**DEFAULT_SETTINGS, "username": username}
        save_settings(settings)
    return jsonify({"success": True})

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    return jsonify({"success": True})

@app.route("/send-otp", methods=["POST"])
def send_otp():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    api_id = data.get("api_id", "").strip()
    api_hash = data.get("api_hash", "").strip()
    phone_number = data.get("phone_number", "").strip()

    if not api_id or not api_hash or not phone_number:
        return jsonify({"success": False, "error": "All fields are required"}), 400

    settings = load_settings()
    settings["api_id"] = api_id
    settings["api_hash"] = api_hash
    settings["phone_number"] = phone_number
    save_settings(settings)

    future = run_async(_async_send_otp(username, api_id, api_hash, phone_number))
    try: return jsonify(future.result(timeout=30))
    except Exception as e: return jsonify({"success": False, "error": str(e)})

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
    code = data.get("code", "").strip()
    password = data.get("password", "")

    settings = load_settings()
    phone_number = settings.get("phone_number", "")
    code_hash = phone_code_hashes.get(username, "")

    future = run_async(_async_verify_otp(username, phone_number, code, code_hash, password))
    try: return jsonify(future.result(timeout=30))
    except Exception as e: return jsonify({"success": False, "error": str(e)})

async def _async_verify_otp(username, phone_number, code, code_hash, password):
    client = clients.get(username)
    if not client: return {"success": False, "error": "No active client"}
    try:
        await client.sign_in(phone_number, code, phone_code_hash=code_hash)
        settings = load_settings()
        settings["connected"] = True
        save_settings(settings)
        return {"success": True}
    except SessionPasswordNeededError:
        if not password: return {"success": False, "error": "2FA password required"}
        try:
            await client.sign_in(password=password)
            settings = load_settings()
            settings["connected"] = True
            save_settings(settings)
            return {"success": True}
        except Exception as e: return {"success": False, "error": f"2FA failed: {str(e)}"}
    except PhoneCodeInvalidError: return {"success": False, "error": "Invalid OTP code"}
    except Exception as e: return {"success": False, "error": str(e)}

@app.route("/toggle-reply", methods=["POST"])
def toggle_reply():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    data = request.get_json(force=True)
    enabled = data.get("enabled", False)
    
    if enabled:
        future = run_async(_start_auto_reply(username))
        try:
            if future.result(timeout=15): return jsonify({"success": True})
            else: return jsonify({"success": False, "error": "Failed. Connect first."})
        except Exception as e: return jsonify({"success": False, "error": str(e)})
    else:
        future = run_async(_stop_auto_reply(username))
        try: future.result(timeout=15)
        except: pass
        return jsonify({"success": True})

@app.route("/save-reply", methods=["POST"])
def save_reply():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    data = request.get_json(force=True)
    settings = load_settings()
    settings["reply_message"] = data.get("message", "").strip()
    save_settings(settings)
    return jsonify({"success": True})

@app.route("/disconnect", methods=["POST"])
def disconnect():
    username = session.get("username")
    if not username: return jsonify({"success": False, "error": "Not logged in"}), 401
    run_async(_stop_auto_reply(username))
    client = clients.pop(username, None)
    if client: run_async(client.disconnect())
    
    session_path = _session_path(username)
    for ext in ["", ".session"]:
        f = session_path + ext
        if os.path.exists(f): os.remove(f)
            
    settings = load_settings()
    settings.update({"connected": False, "auto_reply_enabled": False, "api_id": "", "api_hash": "", "phone_number": ""})
    save_settings(settings)
    return jsonify({"success": True})

# ═══════════════════════════════════════════
# ROUTES (REACTION API & STATUS)
# ═══════════════════════════════════════════

@app.route("/status", methods=["GET"])
def get_status():
    username = session.get("username")
    if not username: return jsonify({"logged_in": False})
    settings = load_settings()
    client = clients.get(username)
    actually_connected = False
    if client:
        try: actually_connected = client.is_connected()
        except: pass
    return jsonify({
        "logged_in": True,
        "username": username,
        "connected": settings.get("connected", False) or actually_connected,
        "auto_reply_enabled": settings.get("auto_reply_enabled", False),
        "reply_message": settings.get("reply_message", ""),
        "accounts_connected": len(clients)
    })

@app.route("/api/react", methods=["GET"])
def api_react():
    link = request.args.get("postlink")
    if not link: return jsonify({"success": False, "error": "postlink parameter required"}), 400

    channel, msg_id = parse_post_link(link)
    if not channel or not msg_id: return jsonify({"success": False, "error": "Invalid link format"}), 400
    if not clients: return jsonify({"success": False, "error": "No active sessions in panel"}), 400

    task_id = f"task_{int(time.time())}_{random.randint(1000, 9999)}"
    
    tasks_db = load_tasks()
    tasks_db[task_id] = {
        "id": task_id,
        "link": link,
        "channel": channel,
        "msg_id": msg_id,
        "status": "QUEUED",
        "success": 0,
        "failed": 0,
        "total_accounts": len(clients),
        "created_at": datetime.now().isoformat(),
        "completed_at": None
    }
    save_tasks(tasks_db)

    run_async(process_reaction_task(task_id, channel, msg_id))
    return jsonify({"success": True, "message": "Positive reaction task queued", "task_id": task_id})

@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    tasks_db = load_tasks()
    return jsonify({"tasks": list(tasks_db.values())})

# ═══════════════════════════════════════════
# APP STARTUP
# ═══════════════════════════════════════════

def _on_startup():
    settings = load_settings()
    username = settings.get("username", "")
    if username and settings.get("api_id") and settings.get("api_hash"):
        print(f"[Startup] Restoring session for {username}...")
        future = run_async(_restore_client(username))
        try:
            if future.result(timeout=20): print(f"[Startup] Session restored for {username}")
            else: print(f"[Startup] Could not restore session")
        except Exception as e: print(f"[Startup] Error: {e}")

_startup_timer = threading.Timer(3.0, _on_startup)
_startup_timer.daemon = True
_startup_timer.start()

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    print(f"[Server] Starting on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
