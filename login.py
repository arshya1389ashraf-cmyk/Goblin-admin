# Run this ONCE, for example in Termux on your phone:
#   pip install spluslib pyaes rsa pysocks
#   python login.py
# It asks for your phone number and the login code Soroush sends you.
# Then it writes session_b64.txt: a long text that you paste into the server's
# environment variable USERBOT_SESSION_B64. Do NOT put it on GitHub:
# whoever has it can use your account.
import asyncio
import base64
import glob
import json
import os
import zlib


def bundle_session(folder="."):
    files = {}
    for path in glob.glob(os.path.join(folder, "userbot*")):
        if os.path.isfile(path) and not path.endswith((".py", ".txt")):
            with open(path, "rb") as f:
                files[os.path.basename(path)] = base64.b64encode(f.read()).decode()
    raw = json.dumps(files).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 9)).decode(), list(files)


async def main():
    from spluslib import SplusClient

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
    text, names = bundle_session(".")
    if not names:
        print("No session file found (userbot*). Tell Claude which files were created here.")
        return
    with open("session_b64.txt", "w") as f:
        f.write(text)
    print("Session files:", ", ".join(names))
    print("Saved session_b64.txt with", len(text), "characters.")


if __name__ == "__main__":
    asyncio.run(main())
