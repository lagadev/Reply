import os
import asyncio
import sqlite3
import random
import time
from flask import Flask, request, jsonify
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    ChatAdminRequiredError,
    MessageIdInvalidError
)
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
import threading

app = Flask(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "user_data")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "sessions")
DB_FILE = os.path.join(DATA_DIR, "database.db")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

_loop = None

# =========================
# Async Loop System
# =========================
def _ensure_loop():
    global _loop

    if _loop is not None:
        return _loop

    _loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    threading.Thread(target=_run, daemon=True).start()

    return _loop


def run_async(coro):
    loop = _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)


def loop_run(coro):
    future = run_async(coro)

    try:
        return future.result(timeout=120)
    except Exception as e:
        print(f"Loop Error: {e}")
        return False


# =========================
# Telegram Link Parser
# =========================
def parse_link(link):
    try:
        link = link.strip()

        if "t.me/" not in link:
            return None, None

        parts = link.split("/")

        channel = parts[-2]
        msg_id = int(parts[-1])

        return channel, msg_id

    except Exception as e:
        print("Parse Error:", e)
        return None, None


# =========================
# Positive Reactions Pool
# =========================
POSITIVE_REACTIONS = [
    "👍",
    "❤️",
    "🔥",
    "🥰",
    "👏",
    "😁",
    "🎉",
    "⚡",
    "😍",
    "💯"
]


# =========================
# Send Reaction
# =========================
async def _send_reaction(
    username,
    api_id,
    api_hash,
    channel,
    msg_id
):
    session_path = os.path.join(
        SESSIONS_DIR,
        f"tg_{username}"
    )

    client = TelegramClient(
        session_path,
        int(api_id),
        api_hash
    )

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print(f"❌ {username} not authorized")
            return False

        emoji = random.choice(POSITIVE_REACTIONS)

        await client(
            SendReactionRequest(
                peer=channel,
                msg_id=msg_id,
                reaction=[
                    ReactionEmoji(
                        emoticon=emoji
                    )
                ],
                big=random.choice([True, False])
            )
        )

        print(f"✅ {username} reacted with {emoji}")

        return True

    except FloodWaitError as e:
        print(f"⚠️ FloodWait {username}: {e.seconds}s")
        await asyncio.sleep(e.seconds + 5)
        return False

    except ChatAdminRequiredError:
        print(f"❌ No permission: {username}")
        return False

    except MessageIdInvalidError:
        print(f"❌ Invalid message id")
        return False

    except Exception as e:
        print(f"❌ Error for {username}: {e}")
        return False

    finally:
        await client.disconnect()


# =========================
# API Endpoint
# Example:
# /get?link=https://t.me/lagatech/67
# =========================
@app.route("/get", methods=["GET"])
def react_api():

    link = request.args.get("link")

    if not link:
        return jsonify({
            "success": False,
            "error": "No Telegram link provided"
        }), 400

    channel, msg_id = parse_link(link)

    if not channel or not msg_id:
        return jsonify({
            "success": False,
            "error": "Invalid Telegram post link"
        }), 400

    # Database থেকে user load
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute("""
        SELECT username, api_id, api_hash
        FROM users
        WHERE connected=1
        AND api_id != ''
        AND api_hash != ''
    """)

    users = c.fetchall()

    conn.close()

    if not users:
        return jsonify({
            "success": False,
            "error": "No active users found"
        }), 404

    success_count = 0

    random.shuffle(users)

    for user in users:

        username, api_id, api_hash = user

        print(f"➡️ Trying from {username}")

        result = loop_run(
            _send_reaction(
                username,
                api_id,
                api_hash,
                channel,
                msg_id
            )
        )

        if result:
            success_count += 1

        # Human-like delay
        delay = random.uniform(4, 12)

        print(f"⏳ Waiting {delay:.1f}s")

        time.sleep(delay)

    return jsonify({
        "success": True,
        "post": link,
        "total_accounts": len(users),
        "successful_reactions": success_count,
        "reaction_type": "auto positive"
    })


# =========================
# Home Route
# =========================
@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "endpoint": "/get?link=https://t.me/channel/1"
    })


# =========================
# Start App
# =========================
if __name__ == "__main__":

    PORT = int(
        os.environ.get("PORT", 5001)
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False
    )
