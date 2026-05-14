import os
import asyncio
import sqlite3
import random
from flask import Flask, request, jsonify
from telethon import TelegramClient
from telethon.errors import FloodWaitError, ChatAdminRequiredError, MessageIdInvalidError
import threading

app = Flask(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "user_data")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")
DB_FILE = os.path.join(DATA_DIR, "database.db")

_loop = None

def _ensure_loop():
    global _loop
    if _loop is not None: return _loop
    _loop = asyncio.new_event_loop()
    def _run_loop():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()
    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()
    return _loop

def run_async(coro):
    loop = _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)

def loop_run(coro):
    future = run_async(coro)
    try: return future.result(timeout=120) # Reaction দিতে সময় লাগতে পারে
    except Exception as e: print(f"Error: {e}"); return None

def parse_link(link):
    """Extract channel username and message id from link"""
    try:
        parts = link.split("/")
        channel = parts[-2]
        msg_id = int(parts[-1])
        return channel, msg_id
    except:
        return None, None

async def _send_reaction(username, api_id, api_hash, channel, msg_id, emoji):
    session_path = os.path.join(SESSIONS_DIR, f"tg_{username}")
    client = TelegramClient(session_path, int(api_id), api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"User {username} not authorized.")
            return False
        
        # Reaction দেওয়া
        await client.send_reaction(channel, msg_id, emoji)
        print(f"✅ Reaction sent by {username}")
        return True
    except FloodWaitError as e:
        print(f"⚠️ Flood wait for {username}: {e.seconds}s")
        # ব্যান এড়ানোর জন্য Telegram যত সেকেন্ড বলবে ততক্ষণ অপেক্ষা করবে
        await asyncio.sleep(e.seconds + 5) 
        return False
    except ChatAdminRequiredError:
        print(f"❌ No permission to react in this chat for {username}")
        return False
    except Exception as e:
        print(f"❌ Error for {username}: {e}")
        return False
    finally:
        await client.disconnect()

@app.route("/get/postlink=", methods=["GET"])
def react_api():
    link = request.url.split("postlink=")[-1]
    if not link:
        return jsonify({"success": False, "error": "No link provided"}), 400
    
    channel, msg_id = parse_link(link)
    if not channel or not msg_id:
        return jsonify({"success": False, "error": "Invalid Telegram post link"}), 400

    emoji = request.args.get("emoji", "👍") # Default 👍

    # ডাটাবেস থেকে সব ইউজারের API নিয়ে আসা
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT username, api_id, api_hash FROM users WHERE connected=1 AND api_id != ""')
    users = c.fetchall()
    conn.close()

    if not users:
        return jsonify({"success": False, "error": "No active users in database"}), 404

    success_count = 0
    
    # ব্যান এড়ানোর জন্য একসাথে সবাইকে দেবে না, একটি একটি করে রিয়্যাকশন দেবে র‍্যান্ডম ডিলে সহ
    for user in users:
        username, api_id, api_hash = user
        print(f"Trying reaction from {username}...")
        result = loop_run(_send_reaction(username, api_id, api_hash, channel, msg_id, emoji))
        if result:
            success_count += 1
        
        # Human behavior সিমুলেট করতে র‍্যান্ডম ডিলে (৩ থেকে ১০ সেকেন্ড)
        delay = random.uniform(3, 10)
        print(f"Waiting {delay:.1f} seconds...")
        import time
        time.sleep(delay)

    return jsonify({
        "success": True, 
        "total_accounts": len(users), 
        "successful_reactions": success_count,
        "post": link
    })

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=PORT, debug=False)
