"""Log your Telegram account in ONCE and print a session string.

This is the same login the dashboard's /userbot page does in the browser - use
this script instead if you'd rather run it on your own machine:

    pip install telethon
    python userbot_login.py

It asks for your api_id + api_hash (from https://my.telegram.org -> API
development tools), then your phone number and the login code Telegram sends
you. Copy the long string it prints into Settings -> Session string, or set it
as the TG_SESSION env var.

The session string is a full login for your account - keep it as secret as your
password. Never paste it in a chat and never commit it to a repo.
"""
import asyncio
import os
import sys

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("Telethon isn't installed. Run:  pip install telethon")


async def main():
    api_id = (os.environ.get("TG_API_ID") or "").strip() or input("Telegram api_id: ").strip()
    api_hash = (os.environ.get("TG_API_HASH") or "").strip() or input("Telegram api_hash: ").strip()
    if not api_id.isdigit() or not api_hash:
        sys.exit("api_id must be a number and api_hash must not be empty.")

    print("\nConnecting... (it will ask for your phone number, the login code "
          "Telegram sends you, and your 2FA password if you have one)\n")
    async with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        me = await client.get_me()
        name = getattr(me, "first_name", "") or ""
        uname = f" (@{me.username})" if getattr(me, "username", None) else ""
        print("\nLogged in as:", name, uname)
        print("\n=== copy EVERYTHING between the lines into TG_SESSION ===")
        print(client.session.save())
        print("==========================================================\n")


if __name__ == "__main__":
    asyncio.run(main())
