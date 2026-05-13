import os
import json
import asyncio
import threading
from datetime import datetime
from flask import Flask, render_template, send_from_directory, request, jsonify, session
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError, FloodWaitError

DATA_DIR = os.environ.get("DATA_DIR", "user_data")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
COOLDOWN_SECONDS = 300

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

app = Flask(__name__, static_folder="public", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-key-change-me")

clients = {}
phone_code_hashes = {}
auto_reply_tasks = {}
cooldowns = {}
_loop = None

def _ensure_loop():
    global _loop
    if _loop is not None: return _loop
    _loop = asyncio.new_event_loop()
    threading.Thread(target=_loop.run_forever, daemon=True).start()
    return _loop

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop())

DEFAULT_SETTINGS = {"username": "", "api_id": "", "api_hash": "", "phone_number": "", "reply_message": "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅", "auto_reply_enabled": False, "connected": False}

def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f: return {**DEFAULT_SETTINGS, **json.load(f)}
    except: pass
    return {**DEFAULT_SETTINGS}

def save_settings(settings):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f: json.dump(settings, f, indent=2, ensure_ascii=False)

def _session_path(username): return os.path.join(SESSIONS_DIR, f"tg_{username}")

async def _create_client(username, api_id, api_hash):
    client = TelegramClient(_session_path(username), int(api_id), api_hash)
    clients[username] = client
    return client

async def _get_client(username):
    if username in clients: return clients[username]
    s = load_settings()
    if s.get("api_id") and s.get("api_hash"): return await _create_client(username, s["api_id"], s["api_hash"])
    return None

async def _start_auto_reply(username):
    client = await _get_client(username)
    if not client or not client.is_connected(): return False
    
    await _stop_auto_reply(username)
    cooldowns[username] = {}
    s = load_settings()
    reply_msg = s.get("reply_message", DEFAULT_SETTINGS["reply_message"])

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            if not event.is_private: return
            sender = await event.get_sender()
            if sender and sender.bot: return
            
            peer_id = str(event.sender_id)
            now = datetime.now().timestamp()
            if now - cooldowns.get(username, {}).get(peer_id, 0) < COOLDOWN_SECONDS: return
            
            current_s = load_settings()
            if not current_s.get("auto_reply_enabled"): return
            
            await event.reply(current_s.get("reply_message", reply_msg))
            cooldowns.setdefault(username, {})[peer_id] = now
        except FloodWaitError as e: print(f"Flood: {e.seconds}s")
        except Exception as e: print(f"Error: {e}")

    auto_reply_tasks[username] = handler
    s["auto_reply_enabled"] = True
    save_settings(s)
    return True

async def _stop_auto_reply(username):
    client = clients.get(username)
    handler = auto_reply_tasks.pop(username, None)
    if client and handler:
        try: client.remove_event_handler(handler)
        except: pass
    s = load_settings()
    s["auto_reply_enabled"] = False
    save_settings(s)
    cooldowns.pop(username, None)

@app.route("/")
def user_index(): return send_from_directory("public", "index.html")

@app.route("/admin")
def admin_index(): return send_from_directory(".", "index.html") # Root folder index.html

@app.route("/login", methods=["POST"])
def login():
    u = request.json.get("username", "").strip().replace("@", "").lower()
    if not u: return jsonify({"error": "Username required"}), 400
    session["username"] = u
    s = load_settings()
    if s.get("username") != u: save_settings({**DEFAULT_SETTINGS, "username": u})
    return jsonify({"success": True})

@app.route("/logout", methods=["POST"])
def logout(): session.pop("username", None); return jsonify({"success": True})

@app.route("/status", methods=["GET"])
def get_status():
    u = session.get("username")
    if not u: return jsonify({"logged_in": False})
    s = load_settings()
    client = clients.get(u)
    connected = s.get("connected", False) or (client is not None and client.is_connected())
    return jsonify({"logged_in": True, "username": u, "connected": connected, "auto_reply_enabled": s.get("auto_reply_enabled", False), "reply_message": s.get("reply_message", "")})

@app.route("/send-otp", methods=["POST"])
def send_otp():
    u = session.get("username")
    if not u: return jsonify({"error": "Not logged in"}), 401
    d = request.json
    s = load_settings()
    s.update({"api_id": d["api_id"], "api_hash": d["api_hash"], "phone_number": d["phone_number"]})
    save_settings(s)
    
    future = run_async(_async_send_otp(u, d["api_id"], d["api_hash"], d["phone_number"]))
    try: return jsonify(future.result(timeout=30))
    except Exception as e: return jsonify({"success": False, "error": str(e)})

async def _async_send_otp(u, api_id, api_hash, phone):
    try:
        old = clients.pop(u, None)
        if old: await old.disconnect()
        client = await _create_client(u, api_id, api_hash)
        await client.connect()
        result = await client.send_code_request(phone)
        phone_code_hashes[u] = result.phone_code_hash
        return {"success": True}
    except Exception as e: return {"success": False, "error": str(e)}

@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    u = session.get("username")
    if not u: return jsonify({"error": "Not logged in"}), 401
    d = request.json
    s = load_settings()
    future = run_async(_async_verify(u, s["phone_number"], d["code"], phone_code_hashes.get(u, ""), d.get("password", "")))
    try: return jsonify(future.result(timeout=30))
    except Exception as e: return jsonify({"success": False, "error": str(e)})

async def _async_verify(u, phone, code, code_hash, password):
    client = clients.get(u)
    if not client: return {"success": False, "error": "No client"}
    try:
        await client.sign_in(phone, code, phone_code_hash=code_hash)
        load_settings()["connected"] = True; save_settings(load_settings()); return {"success": True}
    except SessionPasswordNeededError:
        try: await client.sign_in(password=password); load_settings()["connected"]=True; save_settings(load_settings()); return {"success": True}
        except Exception as e: return {"success": False, "error": "2FA Failed"}
    except Exception as e: return {"success": False, "error": str(e)}

@app.route("/toggle-reply", methods=["POST"])
def toggle_reply():
    u = session.get("username")
    if not u: return jsonify({"error": "Not logged in"}), 401
    enabled = request.json.get("enabled")
    future = run_async(_start_auto_reply(u) if enabled else _stop_auto_reply(u))
    try: return jsonify({"success": True}) if enabled and future.result(timeout=15) else jsonify({"success": True})
    except: return jsonify({"success": False})

@app.route("/save-reply", methods=["POST"])
def save_reply():
    u = session.get("username")
    if not u: return jsonify({"error": "Not logged in"}), 401
    s = load_settings(); s["reply_message"] = request.json.get("message", ""); save_settings(s)
    return jsonify({"success": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=os.environ.get("PORT", 5000), debug=False)
