import os
import json
import asyncio
import threading
import re
import time
import random
from datetime import datetime
from flask import Flask, request, jsonify
from telethon import TelegramClient, errors
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji

# ═══════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════

DATA_DIR = os.environ.get("DATA_DIR", "user_data")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-admin-key-change-in-prod")

# ═══════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════

clients = {}         # { account_id: TelegramClient }
loop = None
loop_thread = None
active_tasks = {}    # { task_id: asyncio.Task }

def ensure_loop():
    global loop, loop_thread
    if loop is not None:
        return loop
    loop = asyncio.new_event_loop()
    def run_loop():
        asyncio.set_event_loop(loop)
        loop.run_forever()
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    return loop

def run_async(coro):
    l = ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, l)

# ═══════════════════════════════════════════
# PERSISTENCE HELPERS
# ═══════════════════════════════════════════

DEFAULT_SETTINGS = {
    "admin_password": "admin123", # Change this!
    "accounts": [] # [{ "id": "acc1", "api_id": "", "api_hash": "", "phone": "", "session_name": "" }]
}

def load_data(file_path, default):
    try:
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except: pass
    return default.copy()

def save_data(file_path, data):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ═══════════════════════════════════════════
# TELETHON CLIENT MANAGEMENT
# ═══════════════════════════════════════════

async def get_client(account):
    acc_id = account["id"]
    if acc_id in clients:
        if not clients[acc_id].is_connected():
            await clients[acc_id].connect()
        return clients[acc_id]
    
    session_path = os.path.join(SESSIONS_DIR, account["session_name"])
    client = TelegramClient(session_path, int(account["api_id"]), account["api_hash"])
    await client.connect()
    clients[acc_id] = client
    return client

async def restore_clients():
    settings = load_data(SETTINGS_FILE, DEFAULT_SETTINGS)
    for acc in settings.get("accounts", []):
        try:
            client = await get_client(acc)
            if not await client.is_user_authorized():
                print(f"[Startup] Account {acc['id']} not authorized.")
                await client.disconnect()
                del clients[acc["id"]]
            else:
                print(f"[Startup] Account {acc['id']} restored.")
        except Exception as e:
            print(f"[Startup] Failed to restore {acc['id']}: {e}")

# ═══════════════════════════════════════════
# REACTION CORE LOGIC (BAN PROOF)
# ═══════════════════════════════════════════

def parse_post_link(link):
    pattern = r"https://t.me/([^/]+)/(\d+)"
    match = re.match(pattern, link)
    if match:
        return match.group(1), int(match.group(2))
    return None, None

async def process_reaction_task(task_id, accounts, channel, msg_id, reaction_emoji):
    tasks_db = load_data(TASKS_FILE, {})
    tasks_db[task_id]["status"] = "RUNNING"
    save_data(TASKS_FILE, tasks_db)

    success = 0
    failed = 0

    for acc in accounts:
        try:
            client = await get_client(acc)
            if not await client.is_user_authorized():
                failed += 1
                continue

            # Anti-ban: Random delay between 3 to 8 seconds per account
            delay = random.uniform(3, 8)
            await asyncio.sleep(delay)

            # Send Reaction
            await client(SendReactionRequest(
                peer=channel,
                msg_id=msg_id,
                reaction=[ReactionEmoji(emoticon=reaction_emoji)]
            ))
            success += 1
            
            # Update DB progress
            tasks_db = load_data(TASKS_FILE, {})
            if task_id in tasks_db:
                tasks_db[task_id]["success"] = success
                tasks_db[task_id]["failed"] = failed
                save_data(TASKS_FILE, tasks_db)

        except errors.FloodWaitError as e:
            print(f"FloodWait! Sleeping for {e.seconds}s")
            # Respect Telegram's limits strictly
            await asyncio.sleep(e.seconds + 5)
            failed += 1
        except Exception as e:
            print(f"Error reacting with {acc['id']}: {e}")
            failed += 1

    # Task Complete
    tasks_db = load_data(TASKS_FILE, {})
    if task_id in tasks_db:
        tasks_db[task_id]["status"] = "COMPLETED"
        tasks_db[task_id]["success"] = success
        tasks_db[task_id]["failed"] = failed
        tasks_db[task_id]["completed_at"] = datetime.now().isoformat()
        save_data(TASKS_FILE, tasks_db)

    active_tasks.pop(task_id, None)

# ═══════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════

@app.route("/api/react", methods=["GET"])
def api_react():
    link = request.args.get("postlink")
    emoji = request.args.get("emoji", "👍") # Default reaction
    
    if not link:
        return jsonify({"success": False, "error": "postlink parameter is required"}), 400

    channel, msg_id = parse_post_link(link)
    if not channel or not msg_id:
        return jsonify({"success": False, "error": "Invalid Telegram post link format"}), 400

    settings = load_data(SETTINGS_FILE, DEFAULT_SETTINGS)
    accounts = settings.get("accounts", [])
    
    # Filter only authorized accounts
    valid_accounts = [acc for acc in accounts if acc["id"] in clients and clients[acc["id"]].is_connected()]
    
    if not valid_accounts:
        return jsonify({"success": False, "error": "No active/authorized accounts available in admin panel"}), 400

    task_id = f"task_{int(time.time())}_{random.randint(1000, 9999)}"
    
    # Save task to DB
    tasks_db = load_data(TASKS_FILE, {})
    tasks_db[task_id] = {
        "id": task_id,
        "link": link,
        "channel": channel,
        "msg_id": msg_id,
        "emoji": emoji,
        "status": "QUEUED",
        "success": 0,
        "failed": 0,
        "total_accounts": len(valid_accounts),
        "created_at": datetime.now().isoformat(),
        "completed_at": None
    }
    save_data(TASKS_FILE, tasks_db)

    # Start background task
    coro = process_reaction_task(task_id, valid_accounts, channel, msg_id, emoji)
    task_obj = run_async(coro)
    active_tasks[task_id] = task_obj

    return jsonify({
        "success": True, 
        "message": "Reaction task queued",
        "task_id": task_id,
        "accounts_engaged": len(valid_accounts)
    })

# Admin Authentication API
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.json
    password = data.get("password", "")
    settings = load_data(SETTINGS_FILE, DEFAULT_SETTINGS)
    
    if password == settings.get("admin_password"):
        session["admin"] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid password"}), 401

# Accounts Management API
@app.route("/api/admin/accounts", methods=["GET", "POST"])
def manage_accounts():
    if not session.get("admin"): return jsonify({"error": "Unauthorized"}), 401
    
    settings = load_data(SETTINGS_FILE, DEFAULT_SETTINGS)
    
    if request.method == "POST":
        data = request.json
        new_acc = {
            "id": f"acc_{int(time.time())}",
            "api_id": data["api_id"],
            "api_hash": data["api_hash"],
            "phone": data["phone"],
            "session_name": f"sess_{random.randint(1000, 9999)}"
        }
        settings["accounts"].append(new_acc)
        save_data(SETTINGS_FILE, settings)
        return jsonify({"success": True, "account": new_acc})
    
    return jsonify({"accounts": settings["accounts"]})

# Send OTP for specific account
@app.route("/api/admin/send-otp", methods=["POST"])
def admin_send_otp():
    if not session.get("admin"): return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    acc_id = data.get("acc_id")
    
    settings = load_data(SETTINGS_FILE, DEFAULT_SETTINGS)
    acc = next((a for a in settings["accounts"] if a["id"] == acc_id), None)
    if not acc: return jsonify({"error": "Account not found"}), 404

    def async_send_otp():
        client = await get_client(acc)
        result = await client.send_code_request(acc["phone"])
        session[f"code_hash_{acc_id}"] = result.phone_code_hash
        return True
    
    future = run_async(async_send_otp())
    try:
        future.result(timeout=15)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# Verify OTP
@app.route("/api/admin/verify-otp", methods=["POST"])
def admin_verify_otp():
    if not session.get("admin"): return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    acc_id = data.get("acc_id")
    code = data.get("code")
    code_hash = session.get(f"code_hash_{acc_id}")
    
    settings = load_data(SETTINGS_FILE, DEFAULT_SETTINGS)
    acc = next((a for a in settings["accounts"] if a["id"] == acc_id), None)

    def async_verify():
        client = await get_client(acc)
        await client.sign_in(acc["phone"], code, phone_code_hash=code_hash)
        return True
    
    future = run_async(async_verify())
    try:
        future.result(timeout=15)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# Tasks Status API
@app.route("/api/admin/tasks", methods=["GET"])
def get_tasks():
    if not session.get("admin"): return jsonify({"error": "Unauthorized"}), 401
    tasks_db = load_data(TASKS_FILE, {})
    return jsonify({"tasks": list(tasks_db.values())})

# Serve Admin UI
@app.route("/")
def index():
    return send_from_directory("public", "index.html")

# Startup Restoration
def on_startup():
    print("[Server] Restoring Telegram sessions...")
    future = run_async(restore_clients())
    try:
        future.result(timeout=20)
    except Exception as e:
        print(f"Error restoring: {e}")

startup_timer = threading.Timer(2.0, on_startup)
startup_timer.daemon = True
startup_timer.start()

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT, debug=False)
