# Run this ONCE (for example in Termux on your phone):
#   pip install spluslib pyaes rsa pysocks
#   python login.py
# It asks for your phone number and the login code that Soroush sends you,
# then creates a session file (userbot.session or similar) next to this script.
# Upload that file to the server, in the same folder as bot.py.
# NEVER put the session file on GitHub: whoever has it can use your account.
import asyncio

from spluslib import SplusClient


async def main():
    phone = input("Phone number (example +989123456789): ").strip()
    client = SplusClient("userbot")
    await client.start(phone)
    me = await client.get_me()
    name = me.get("first_name") if isinstance(me, dict) else me
    print("Logged in as:", name)
    try:
        await client.disconnect()
    except Exception:
        pass
    print("Done. Now upload the file(s) named userbot* to the server.")


asyncio.run(main())
