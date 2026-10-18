"""Generate a Telethon session string for the user account (optional).

The dashboard has a browser login at /userbot, so this script is only a
convenience for running the login on your own machine:

    pip install telethon
    TG_API_ID=12345 TG_API_HASH=abc... python userbot_login.py

It prints a session string - paste it into Settings -> Session string, or set it
as TG_SESSION. Treat it like your password.
"""
import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = os.environ.get("TG_API_ID") or input("api_id: ").strip()
    api_hash = os.environ.get("TG_API_HASH") or input("api_hash: ").strip()
    phone = os.environ.get("TG_PHONE") or input("phone (+countrycode): ").strip()
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start(phone=phone)
    print("\nYour session string (keep it secret):\n")
    print(client.session.save())
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
