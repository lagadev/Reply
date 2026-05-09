from telethon import TelegramClient, events
import asyncio
import time
import os

# ===== ENV INFO =====
api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")

client = TelegramClient("laga_session", api_id, api_hash)

OFFLINE_TIME = 300  # 5 minutes

last_activity = time.time()
replied_users = set()

AUTO_REPLY = """
⏩আমি এখন অফলাইনে আছি। অনলাইনে আসলে আপনাকে রিপ্লাই দিব। কারেন্ট থাকে না তাই একটু দেরি হতে পারে ✅🥺

✅ Social Media যে কোনো Service নিতে এই বট ব্যবহার করুন💙💙

🔵 @lagasmmbot

⏩এখানে সবকিছু কম মূল্যে পেয়ে যাবেন।⏩

✉️Join My Channel 🪙✅💎
@lagatech
"""

# ===== Update Activity =====
@client.on(events.NewMessage(outgoing=True))
async def update_activity(event):
    global last_activity
    last_activity = time.time()

# ===== Auto Reply =====
@client.on(events.NewMessage(incoming=True))
async def auto_reply(event):
    global last_activity

    if event.sender and event.sender.bot:
        return

    if event.is_group or event.is_channel:
        return

    user_id = event.sender_id

    if time.time() - last_activity > OFFLINE_TIME:

        if user_id not in replied_users:
            try:
                await event.reply(AUTO_REPLY)
                replied_users.add(user_id)
            except:
                pass


# ===== Clear cache when online =====
async def clear_replied():
    global replied_users

    while True:
        if time.time() - last_activity < OFFLINE_TIME:
            replied_users.clear()

        await asyncio.sleep(60)


async def main():
    asyncio.create_task(clear_replied())
    print("Bot Running...")
    await client.start()
    await client.run_until_disconnected()


with client:
    client.loop.run_until_complete(main())
