"""
Telegram Auto Reply — Premium Web App
Flask backend with Telethon integration, session auth, and async auto-reply.
"""

import os
import json
import asyncio
import threading
from datetime import datetime

from flask import (
    Flask, render_template, send_from_directory,
    request, jsonify, session
)
from telethon import TelegramClient, events
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
COOLDOWN_SECONDS = 300  # 5 minutes per user

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ═══════════════════════════════════════════
# FLASK APP
# ═══════════════════════════════════════════

app = Flask(__name__, static_folder="public", static_url_path="/static")
app.secret_key = os.environ.get("SECRET_KEY", "tg-autoreply-secret-key-change-in-prod")

# ═══════════════════════════════════════════
# GLOBAL STATE
# ═══════════════════════════════════════════

# Per-user Telegram clients: { username: TelegramClient }
clients = {}
# Per-user phone code hashes: { username: str }
phone_code_hashes = {}
# Per-user auto-reply tasks: { username: asyncio.Task }
auto_reply_tasks = {}
# Per-user cooldown tracking: { username: { peer_id: timestamp } }
cooldowns = {}
# Background event loop for Telethon
_loop = None
_loop_thread = None


def _ensure_loop():
    """Create the background asyncio event loop in a dedicated thread (once)."""
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
    """Schedule a coroutine on the background loop and return a concurrent.futures Future."""
    loop = _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)


# ═══════════════════════════════════════════
# SETTINGS PERSISTENCE (JSON)
# ═══════════════════════════════════════════

DEFAULT_SETTINGS = {
    "username": "",
    "api_id": "",
    "api_hash": "",
    "phone_number": "",
    "reply_message": "▶️ আমি এখন ব্যস্ত আছি। একটু পরে রিপ্লাই দিব ✅🥺",
    "auto_reply_enabled": False,
    "connected": False,
}


def load_settings():
    """Load settings from JSON file."""
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Merge with defaults for any missing keys
                merged = {**DEFAULT_SETTINGS, **data}
                return merged
    except (json.JSONDecodeError, IOError):
        pass
    return {**DEFAULT_SETTINGS}


def save_settings(settings):
    """Save settings to JSON file."""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════
# TELETHON HELPERS
# ═══════════════════════════════════════════

def _session_path(username):
    """Get the session file path for a user."""
    return os.path.join(SESSIONS_DIR, f"tg_{username}")


async def _create_client(username, api_id, api_hash):
    """Create a new TelegramClient for a user."""
    session_path = _session_path(username)
    client = TelegramClient(session_path, int(api_id), api_hash)
    clients[username] = client
    return client


async def _get_client(username):
    """Get or create a TelegramClient for a user."""
    if username in clients:
        return clients[username]
    settings = load_settings()
    if settings.get("api_id") and settings.get("api_hash"):
        return await _create_client(username, settings["api_id"], settings["api_hash"])
    return None


async def _restore_client(username):
    """Try to restore an existing session on app startup."""
    settings = load_settings()
    if not settings.get("api_id") or not settings.get("api_hash"):
        return False
    session_path = _session_path(username)
    session_file = session_path + ".session"
    if not os.path.exists(session_file):
        return False
    try:
        client = await _create_client(username, settings["api_id"], settings["api_hash"])
        await client.connect()
        if await client.is_user_authorized():
            settings["connected"] = True
            save_settings(settings)
            # If auto reply was enabled, restart it
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
    """Start the auto-reply listener for a user."""
    client = await _get_client(username)
    if not client or not client.is_connected():
        return False

    # Remove old handler if any
    await _stop_auto_reply(username)

    # Initialize cooldown dict for this user
    cooldowns[username] = {}

    settings = load_settings()
    reply_msg = settings.get("reply_message", DEFAULT_SETTINGS["reply_message"])

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        """Handle incoming messages with auto-reply logic."""
        try:
            # Only private chats
            if not event.is_private:
                return
            # Ignore bots
            sender = await event.get_sender()
            if sender and sender.bot:
                return
            # Cooldown check
            peer_id = event.sender_id
            now = datetime.now().timestamp()
            user_cd = cooldowns.get(username, {})
            last_reply = user_cd.get(str(peer_id), 0)
            if now - last_reply < COOLDOWN_SECONDS:
                return
            # Send reply
            current_settings = load_settings()
            if not current_settings.get("auto_reply_enabled"):
                return
            msg = current_settings.get("reply_message", reply_msg)
            await event.reply(msg)
            # Update cooldown
            cooldowns.setdefault(username, {})[str(peer_id)] = now
        except FloodWaitError as e:
            print(f"[AutoReply] Flood wait: {e.seconds}s for user {username}")
        except Exception as e:
            print(f"[AutoReply] Error for user {username}: {e}")

    # Store reference so we can remove it later
    auto_reply_tasks[username] = handler
    settings["auto_reply_enabled"] = True
    save_settings(settings)
    print(f"[AutoReply] Started for {username}")
    return True


async def _stop_auto_reply(username):
    """Stop the auto-reply listener for a user."""
    client = clients.get(username)
    handler = auto_reply_tasks.pop(username, None)
    if client and handler:
        try:
            client.remove_event_handler(handler)
        except Exception:
            pass
    settings = load_settings()
    settings["auto_reply_enabled"] = False
    save_settings(settings)
    # Clear cooldowns
    cooldowns.pop(username, None)
    print(f"[AutoReply] Stopped for {username}")


# ═══════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════

@app.route("/")
def index():
    """Serve the main SPA."""
    return send_from_directory("public", "index.html")


@app.route("/login", methods=["POST"])
def login():
    """Username-only login. Stores username in Flask session."""
    data = request.get_json(force=True)
    username = data.get("username", "").strip().replace("@", "").lower()

    if not username:
        return jsonify({"success": False, "error": "Username is required"}), 400

    # Basic validation: Telegram usernames are 5-32 chars, alphanumeric + underscores
    if len(username) < 3:
        return jsonify({"success": False, "error": "Username too short"}), 400

    session["username"] = username

    # Update settings with username if different
    settings = load_settings()
    if settings.get("username") != username:
        # New user — reset settings
        settings = {**DEFAULT_SETTINGS, "username": username}
        save_settings(settings)

    return jsonify({"success": True, "username": username})


@app.route("/logout", methods=["POST"])
def logout():
    """Clear session."""
    session.pop("username", None)
    return jsonify({"success": True})


@app.route("/status", methods=["GET"])
def get_status():
    """Return current connection and auto-reply status."""
    username = session.get("username")
    if not username:
        return jsonify({"logged_in": False})

    settings = load_settings()
    # Check if client is actually connected
    client = clients.get(username)
    actually_connected = False
    if client:
        try:
            actually_connected = client.is_connected() and loop_run(lambda: client.is_user_authorized())
        except Exception:
            pass

    return jsonify({
        "logged_in": True,
        "username": username,
        "connected": settings.get("connected", False) or actually_connected,
        "auto_reply_enabled": settings.get("auto_reply_enabled", False),
        "reply_message": settings.get("reply_message", ""),
    })


def loop_run(coro):
    """Run a coroutine on the background loop synchronously (with timeout)."""
    future = run_async(coro)
    try:
        return future.result(timeout=15)
    except Exception:
        return None


@app.route("/send-otp", methods=["POST"])
def send_otp():
    """Send OTP to the user's Telegram app."""
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    api_id = data.get("api_id", "").strip()
    api_hash = data.get("api_hash", "").strip()
    phone_number = data.get("phone_number", "").strip()

    if not api_id or not api_hash or not phone_number:
        return jsonify({"success": False, "error": "All fields are required"}), 400

    try:
        int(api_id)
    except ValueError:
        return jsonify({"success": False, "error": "API ID must be a number"}), 400

    # Save credentials
    settings = load_settings()
    settings["api_id"] = api_id
    settings["api_hash"] = api_hash
    settings["phone_number"] = phone_number
    save_settings(settings)

    # Send OTP via Telethon on the background loop
    future = run_async(_async_send_otp(username, api_id, api_hash, phone_number))
    try:
        result = future.result(timeout=30)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": f"Connection error: {str(e)}"})


async def _async_send_otp(username, api_id, api_hash, phone_number):
    """Async: create client and send code request."""
    try:
        # Disconnect existing client if any
        old_client = clients.get(username)
        if old_client:
            try:
                await old_client.disconnect()
            except Exception:
                pass
            del clients[username]

        client = await _create_client(username, api_id, api_hash)
        await client.connect()
        result = await client.send_code_request(phone_number)
        phone_code_hashes[username] = result.phone_code_hash
        return {"success": True}
    except PhoneNumberInvalidError:
        return {"success": False, "error": "Invalid phone number format"}
    except FloodWaitError as e:
        return {"success": False, "error": f"Flood wait: try again in {e.seconds} seconds"}
    except Exception as e:
        return {"success": False, "error": f"Failed to send OTP: {str(e)}"}


@app.route("/verify-otp", methods=["POST"])
def verify_otp():
    """Verify the OTP code and complete login."""
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    code = data.get("code", "").strip()
    password = data.get("password", "")

    if not code:
        return jsonify({"success": False, "error": "OTP code is required"}), 400

    settings = load_settings()
    phone_number = settings.get("phone_number", "")
    code_hash = phone_code_hashes.get(username, "")

    if not phone_number:
        return jsonify({"success": False, "error": "No pending verification. Send OTP first."}), 400

    future = run_async(_async_verify_otp(username, phone_number, code, code_hash, password))
    try:
        result = future.result(timeout=30)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": f"Verification error: {str(e)}"})


async def _async_verify_otp(username, phone_number, code, code_hash, password):
    """Async: verify OTP code and sign in."""
    client = clients.get(username)
    if not client:
        return {"success": False, "error": "No active client. Send OTP first."}

    try:
        await client.sign_in(phone_number, code, phone_code_hash=code_hash)
        # Success
        settings = load_settings()
        settings["connected"] = True
        save_settings(settings)
        phone_code_hashes.pop(username, None)
        return {"success": True}
    except SessionPasswordNeededError:
        # 2FA is enabled — need password
        if not password:
            return {"success": False, "error": "Two-factor authentication is enabled. Please enter your 2FA password."}
        try:
            await client.sign_in(password=password)
            settings = load_settings()
            settings["connected"] = True
            save_settings(settings)
            phone_code_hashes.pop(username, None)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": f"2FA verification failed: {str(e)}"}
    except PhoneCodeInvalidError:
        return {"success": False, "error": "Invalid OTP code. Please try again."}
    except FloodWaitError as e:
        return {"success": False, "error": f"Flood wait: try again in {e.seconds} seconds"}
    except Exception as e:
        return {"success": False, "error": f"Verification failed: {str(e)}"}


@app.route("/save-reply", methods=["POST"])
def save_reply():
    """Save the auto-reply message."""
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    message = data.get("message", "").strip()

    if not message:
        return jsonify({"success": False, "error": "Reply message cannot be empty"}), 400

    settings = load_settings()
    settings["reply_message"] = message
    save_settings(settings)

    return jsonify({"success": True})


@app.route("/toggle-reply", methods=["POST"])
def toggle_reply():
    """Enable or disable auto-reply."""
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    enabled = data.get("enabled", False)

    settings = load_settings()

    if enabled and not settings.get("connected"):
        return jsonify({"success": False, "error": "Connect your Telegram account first"}), 400

    if enabled:
        future = run_async(_start_auto_reply(username))
        try:
            result = future.result(timeout=15)
            if result:
                return jsonify({"success": True})
            else:
                return jsonify({"success": False, "error": "Failed to start auto reply. Make sure you are connected."})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        future = run_async(_stop_auto_reply(username))
        try:
            future.result(timeout=15)
        except Exception:
            pass
        return jsonify({"success": True})


@app.route("/disconnect", methods=["POST"])
def disconnect():
    """Disconnect the Telegram account and delete session."""
    username = session.get("username")
    if not username:
        return jsonify({"success": False, "error": "Not logged in"}), 401

    # Stop auto reply first
    future = run_async(_stop_auto_reply(username))
    try:
        future.result(timeout=10)
    except Exception:
        pass

    # Disconnect client
    client = clients.pop(username, None)
    if client:
        future = run_async(client.disconnect())
        try:
            future.result(timeout=10)
        except Exception:
            pass

    # Delete session file
    session_path = _session_path(username)
    for ext in ["", ".session"]:
        f = session_path + ext
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    # Reset settings
    settings = load_settings()
    settings["connected"] = False
    settings["auto_reply_enabled"] = False
    settings["api_id"] = ""
    settings["api_hash"] = ""
    settings["phone_number"] = ""
    save_settings(settings)

    phone_code_hashes.pop(username, None)

    return jsonify({"success": True})


# ═══════════════════════════════════════════
# APP STARTUP
# ═══════════════════════════════════════════

def _on_startup():
    """Restore existing sessions on app startup."""
    settings = load_settings()
    username = settings.get("username", "")
    if username and settings.get("api_id") and settings.get("api_hash"):
        print(f"[Startup] Restoring session for {username}...")
        future = run_async(_restore_client(username))
        try:
            result = future.result(timeout=20)
            if result:
                print(f"[Startup] Session restored for {username}")
            else:
                print(f"[Startup] Could not restore session for {username}")
        except Exception as e:
            print(f"[Startup] Error restoring session: {e}")


# Run startup restoration after a short delay to let the loop start
import time
_startup_timer = threading.Timer(3.0, _on_startup)
_startup_timer.daemon = True
_startup_timer.start()

# ═══════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    print(f"[Server] Starting on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
