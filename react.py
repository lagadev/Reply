import os
import re
import asyncio
import threading
from flask import Flask, jsonify, request
from telethon import TelegramClient, errors

app = Flask(__name__)

# ══════════════════════════════════════════
# SECURE CREDENTIALS (Environment Variables এ সেভ করবেন)
# ══════════════════════════════════════════
API_ID = int(os.environ.get("API_ID", "12345678"))
API_HASH = os.environ.get("API_HASH", "your_api_hash_here")
SESSION_NAME = "reactor_session"

client = None
_loop = None

def _ensure_loop():
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
        threading.Thread(target=_loop.run_forever, daemon=True).start()
    return _loop

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop())

async def connect_client():
    global client
    if client and client.is_connected():
        return True
    try:
        client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        await client.connect()
        if await client.is_user_authorized():
            return True
        return False
    except Exception as e:
        print(f"Connection Error: {e}")
        return False

async def _send_reaction(post_url, emoji="👍"):
    if not await connect_client():
        return {"success": False, "error": "Telegram account not connected or session expired."}

    try:
        # URL Parse: https://t.me/channel/123 or https://t.me/c/1234567890/123
        match = re.match(r'https://t\.me/(?:c/)?(\w+|\+[\w]+)/(\d+)', post_url)
        if not match:
            return {"success": False, "error": "Invalid Telegram post URL format."}

        entity_id, msg_id = match.group(1), int(match.group(2))
        
        # Resolve Entity
        if entity_id.startswith('+'): # Private Invite Link (Not supported for messages)
            return {"success": False, "error": "Private invite links are not supported."}
        elif entity_id.isdigit(): # Private Channel
            entity = int('-100' + entity_id)
        else: # Public Channel
            entity = await client.get_entity(entity_id)

        # Send Reaction (Safe Mode)
        await client.send_reaction(entity, msg_id, emoji)
        return {"success": True, "message": f"Reacted {emoji} successfully!"}

    except errors.FloodWaitError as e:
        return {"success": False, "error": f"Flood wait! Try again after {e.seconds} seconds."}
    except errors.ChannelPrivateError:
        return {"success": False, "error": "Cannot react. The channel is private or you are not a member."}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ══════════════════════════════════════════
# API ROUTES
# ══════════════════════════════════════════
@app.route('/get/postlink=<path:post_url>', methods=['GET'])
def api_react(post_url):
    emoji = request.args.get('emoji', '👍') # Default: 👍
    future = run_async(_send_reaction(post_url, emoji))
    try:
        result = future.result(timeout=15)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": "Server timeout or error."})

if __name__ == '__main__':
    print("🚀 Reaction API Running on http://localhost:5001")
    # প্রথমবার লগইন করার জন্য (CLI দিয়ে একবার চালাতে হবে সেশন তৈরি করতে)
    app.run(host='0.0.0.0', port=5001, debug=False)
