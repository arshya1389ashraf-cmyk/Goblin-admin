import asyncio
import html
import inspect
import json
import logging
import os
import re
import shutil
import sys
import time
from logging.handlers import RotatingFileHandler

import aiohttp

import aiosplus
import aiosplus.types as aitypes
from aiosplus import Bot, Dispatcher
from aiosplus.filters import Command
from aiosplus.types import Message

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(os.path.join(BASE_DIR, ".env"))
load_env(os.path.join(BASE_DIR, "env.txt"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "@User_ligo").strip()
INTERVAL_HOURS = float(os.environ.get("INTERVAL_HOURS", "2"))
CHECK_MINUTES = float(os.environ.get("CHECK_MINUTES", "20"))
INITIAL_BACKLOG = int(os.environ.get("INITIAL_BACKLOG", "15"))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "300"))
MAX_MB = int(os.environ.get("MAX_MB", "20"))
KEEP_CAPTION = os.environ.get("KEEP_CAPTION", "1") == "1"
HTTP_PROXY_URL = os.environ.get("HTTP_PROXY_URL", "").strip() or None
DEFAULT_SOURCES = [
    s.strip().lower()
    for s in re.split(r"[,\s]+", os.environ.get("SOURCES", "meme_ol_ski"))
    if s.strip()
]

FOOTER = (
    "Join us   "
    "\u261e\u2060\uffe3\u2060\u1d25\u2060\uffe3\u2060\u261e"
    " :  @Meme_Goblin"
)

STATE_FILE = os.path.join(BASE_DIR, "state.json")
TMP_DIR = os.path.join(BASE_DIR, "tmp")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
    )
}

log = logging.getLogger("cloner")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh = RotatingFileHandler(
    os.path.join(BASE_DIR, "bot.log"), maxBytes=300_000, backupCount=2, encoding="utf-8"
)
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_fh)
log.addHandler(_sh)

if not BOT_TOKEN or not CHANNEL_ID:
    print("BOT_TOKEN and CHANNEL_ID must be set in .env")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

SESSION = None
state = {
    "sources": {},
    "queue": [],
    "next_post": 0,
    "admin_chat": None,
    "posted_total": 0,
}


# ---------------------------------------------------------------- state

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            state.update(data)
        except Exception:
            log.exception("could not read state.json, starting fresh")
    for s in DEFAULT_SOURCES:
        state["sources"].setdefault(s, {"last_id": None})


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def clean_tmp():
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    os.makedirs(TMP_DIR, exist_ok=True)


# ---------------------------------------------------------------- telegram web preview

POST_RE = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
PHOTO_RE = re.compile(
    r'tgme_widget_message_photo_wrap[^>]*?style="[^"]*?background-image:url\(\'([^\']+)\'\)'
)
VIDEO_RE = re.compile(r'<video[^>]*?\ssrc="([^"]+)"')
TEXT_RE = re.compile(
    r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)


def html_to_text(s):
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def parse_posts(page):
    posts = []
    matches = list(POST_RE.finditer(page))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page)
        seg = page[m.end():end]
        kind = None
        url = None
        v = VIDEO_RE.search(seg)
        if v:
            kind, url = "video", html.unescape(v.group(1))
        else:
            p = PHOTO_RE.search(seg)
            if p:
                kind, url = "photo", html.unescape(p.group(1))
        t = TEXT_RE.search(seg)
        text = html_to_text(t.group(1)) if t else ""
        posts.append(
            {
                "src": m.group(1).lower(),
                "id": int(m.group(2)),
                "kind": kind,
                "url": url,
                "text": text,
            }
        )
    return posts


async def fetch_page(src, before=None):
    url = f"https://t.me/s/{src}"
    if before:
        url += f"?before={before}"
    async with SESSION.get(
        url,
        headers=HEADERS,
        proxy=HTTP_PROXY_URL,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return await r.text()


async def download(url, dest):
    limit = MAX_MB * 1024 * 1024
    async with SESSION.get(
        url,
        headers=HEADERS,
        proxy=HTTP_PROXY_URL,
        timeout=aiohttp.ClientTimeout(total=180),
    ) as r:
        if r.status != 200:
            raise RuntimeError(f"download HTTP {r.status}")
        size = int(r.headers.get("Content-Length") or 0)
        if size > limit:
            raise SkipPost("file too big")
        total = 0
        with open(dest, "wb") as f:
            async for chunk in r.content.iter_chunked(65536):
                total += len(chunk)
                if total > limit:
                    raise SkipPost("file too big")
                f.write(chunk)


class SkipPost(Exception):
    pass


# ---------------------------------------------------------------- queue

async def check_source(src):
    """Look at a channel and queue new media posts. Returns number queued."""
    info = state["sources"].setdefault(src, {"last_id": None})
    page = await fetch_page(src)
    posts = parse_posts(page)
    posts = [p for p in posts if p["src"] == src]
    if not posts:
        raise RuntimeError("no posts found (channel private, missing, or preview disabled)")
    max_id = max(p["id"] for p in posts)
    media = sorted((p for p in posts if p["kind"]), key=lambda p: p["id"])
    last_id = info.get("last_id")
    if last_id is None:
        new = media[-INITIAL_BACKLOG:] if INITIAL_BACKLOG > 0 else []
    else:
        new = [p for p in media if p["id"] > last_id]
    have = {(q["src"], q["id"]) for q in state["queue"]}
    added = 0
    for p in new:
        if (src, p["id"]) in have:
            continue
        state["queue"].append({"src": src, "id": p["id"], "tries": 0})
        added += 1
    info["last_id"] = max(max_id, last_id or 0)
    if len(state["queue"]) > MAX_QUEUE:
        del state["queue"][: len(state["queue"]) - MAX_QUEUE]
    save_state()
    return added


async def check_all_sources():
    for src in list(state["sources"]):
        try:
            n = await check_source(src)
            if n:
                log.info("queued %s new posts from %s", n, src)
        except Exception as e:
            log.warning("check %s failed: %s", src, e)


# ---------------------------------------------------------------- sending

def target_candidates():
    raw = CHANNEL_ID
    if re.fullmatch(r"-?\d+", raw):
        return [int(raw)]
    name = re.sub(r"^https?://", "", raw).rstrip("/").split("/")[-1].lstrip("@")
    out = ["@" + name, name]
    if raw not in out:
        out.append(raw)
    return out


def ordered_targets():
    cands = target_candidates()
    w = _working.get("target")
    if w is not None and w in cands:
        cands.remove(w)
        cands.insert(0, w)
    return cands


async def send_text_to_channel(text):
    last = None
    for tgt in ordered_targets():
        try:
            await bot.send_message(chat_id=tgt, text=text)
            _working["target"] = tgt
            return tgt
        except Exception as e:
            last = e
            log.warning("send_message to %r failed: %s", tgt, e)
    raise RuntimeError(f"could not send to channel: {last}")


def clean_caption(t):
    if not KEEP_CAPTION or not t:
        return ""
    t = re.sub(r"https?://\S+|t\.me/\S+|\S*@\w+\S*", "", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:800]


def build_caption(text):
    c = clean_caption(text)
    return (c + "\n\n" + FOOTER) if c else FOOTER


def file_variants(path):
    filename = os.path.basename(path)
    out = []
    for mod in (aitypes, aiosplus):
        mname = getattr(mod, "__name__", "mod")
        for name in ("FSInputFile", "InputFile"):
            cls = getattr(mod, name, None)
            if cls:
                out.append((f"{mname}.{name}", lambda c=cls: c(path)))
        cls = getattr(mod, "BufferedInputFile", None)
        if cls:
            out.append(
                (
                    f"{mname}.BufferedInputFile",
                    lambda c=cls: c(open(path, "rb").read(), filename=filename),
                )
            )
    out.append(("file_handle", lambda: open(path, "rb")))
    out.append(("path_str", lambda: path))
    return out


def pick_param(func, kind):
    try:
        params = inspect.signature(func).parameters
    except Exception:
        return kind, True
    for n in (kind, "file", "media", "document"):
        if n in params:
            return n, "caption" in params
    names = [n for n in params if n not in ("self", "chat_id")]
    return (names[0] if names else kind), "caption" in params


_working = {"variant": None, "target": None}


async def send_media(kind, path, caption):
    func = bot.send_photo if kind == "photo" else bot.send_video
    param, has_caption = pick_param(func, kind)
    last = None
    for tgt in ordered_targets():
        variants = file_variants(path)
        if _working["variant"]:
            variants.sort(key=lambda v: v[0] != _working["variant"])
        for label, make in variants:
            fobj = None
            try:
                fobj = make()
                kwargs = {"chat_id": tgt, param: fobj}
                if has_caption:
                    kwargs["caption"] = caption
                await func(**kwargs)
                _working["variant"] = label
                _working["target"] = tgt
                if not has_caption:
                    await bot.send_message(chat_id=tgt, text=caption)
                return
            except Exception as e:
                last = e
                log.warning("send to %r via %s failed: %s", tgt, label, e)
            finally:
                if fobj is not None and hasattr(fobj, "close"):
                    try:
                        fobj.close()
                    except Exception:
                        pass
    raise RuntimeError(f"send failed: {last}")


async def send_post(item):
    page = await fetch_page(item["src"], before=item["id"] + 1)
    p = next((x for x in parse_posts(page) if x["id"] == item["id"] and x["src"] == item["src"]), None)
    if not p or not p["kind"]:
        raise SkipPost("post has no downloadable media")
    ext = ".mp4" if p["kind"] == "video" else ".jpg"
    path = os.path.join(TMP_DIR, f"{item['src']}_{item['id']}{ext}")
    try:
        await download(p["url"], path)
        await send_media(p["kind"], path, build_caption(p["text"]))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def notify_admin(text):
    chat = state.get("admin_chat")
    if not chat:
        return
    try:
        await bot.send_message(chat_id=chat, text=text)
    except Exception:
        pass


async def post_next():
    """Try to post the first queued item. True if handled (sent or dropped)."""
    if not state["queue"]:
        return False
    item = state["queue"][0]
    try:
        await send_post(item)
        state["queue"].pop(0)
        state["posted_total"] += 1
        log.info("posted %s/%s", item["src"], item["id"])
        return True
    except SkipPost as e:
        state["queue"].pop(0)
        log.info("skipped %s/%s: %s", item["src"], item["id"], e)
        return True
    except Exception as e:
        item["tries"] = item.get("tries", 0) + 1
        log.warning("post %s/%s failed (%s): %s", item["src"], item["id"], item["tries"], e)
        if item["tries"] >= 3:
            state["queue"].pop(0)
            await notify_admin(f"Post {item['src']}/{item['id']} failed 3 times and was dropped: {e}")
        return False


async def scheduler():
    last_check = 0
    while True:
        try:
            now = time.time()
            if now - last_check >= CHECK_MINUTES * 60:
                await check_all_sources()
                last_check = time.time()
            if state["queue"] and time.time() >= state["next_post"]:
                ok = await post_next()
                state["next_post"] = time.time() + (INTERVAL_HOURS * 3600 if ok else 300)
                save_state()
        except Exception:
            log.exception("scheduler error")
        await asyncio.sleep(30)


# ---------------------------------------------------------------- bot commands

HELP = (
    "Channel cloner\n\n"
    "To add a source channel, just send me its link (one or several), e.g. https://t.me/channel_name\n\n"
    "/remove <channel name>\n"
    "/list\n"
    "/status\n"
    "/postnow\n/test (sends a test message to your channel)\n\n"
    "Forward a post from your own channel to see its chat id."
)


def is_admin(message):
    username = getattr(message.from_user, "username", None)
    if username and not username.startswith("@"):
        username = "@" + username
    return username is not None and username.lower() == ADMIN_USERNAME.lower()


async def reply(message, text):
    await bot.send_message(chat_id=message.chat.id, text=text)


def arg_of(message):
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def parse_source(s):
    s = s.strip()
    m = re.search(r"t\.me/(?:s/)?@?([A-Za-z0-9_]{4,})", s)
    if m:
        return m.group(1).lower()
    m = re.fullmatch(r"@?([A-Za-z0-9_]{4,})", s)
    return m.group(1).lower() if m else None


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message):
        return
    state["admin_chat"] = message.chat.id
    save_state()
    await reply(message, HELP)


BAD_NAMES = {"proxy", "joinchat", "addstickers", "share", "c", "socks", "s"}
LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:s/)?@?([A-Za-z0-9_]{4,})", re.I)


def extract_sources(text):
    out = []
    for name in LINK_RE.findall(text or ""):
        name = name.lower()
        if name not in BAD_NAMES and name not in out:
            out.append(name)
    return out


async def add_source(src):
    existed = src in state["sources"]
    try:
        n = await check_source(src)
    except Exception as e:
        if not existed:
            state["sources"].pop(src, None)
            save_state()
        return f"Could not read {src}: {e}"
    return f"Added {src}. Queued {n} posts. Queue size: {len(state['queue'])}"


@dp.message(Command("add"))
async def cmd_add(message: Message):
    if not is_admin(message):
        return
    srcs = extract_sources(arg_of(message))
    one = parse_source(arg_of(message))
    if not srcs and one:
        srcs = [one]
    if not srcs:
        await reply(message, "Send me a channel link like https://t.me/channel_name")
        return
    results = [await add_source(s) for s in srcs]
    await reply(message, "\n".join(results))


@dp.message(Command("remove"))
async def cmd_remove(message: Message):
    if not is_admin(message):
        return
    src = parse_source(arg_of(message))
    if not src or src not in state["sources"]:
        await reply(message, "Source not found. See /list")
        return
    state["sources"].pop(src, None)
    state["queue"] = [q for q in state["queue"] if q["src"] != src]
    save_state()
    await reply(message, f"Removed {src} and its queued posts.")


@dp.message(Command("list"))
async def cmd_list(message: Message):
    if not is_admin(message):
        return
    names = "\n".join(state["sources"]) or "(none)"
    await reply(message, "Sources:\n" + names)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    if not is_admin(message):
        return
    wait = max(0, int(state["next_post"] - time.time()))
    await reply(
        message,
        f"Sources: {len(state['sources'])}\nQueue: {len(state['queue'])}\n"
        f"Posted total: {state['posted_total']}\nNext post in: {wait // 60} min",
    )


@dp.message(Command("test"))
async def cmd_test(message: Message):
    if not is_admin(message):
        return
    try:
        tgt = await send_text_to_channel("test message")
        await reply(message, f"OK, sent to channel using: {tgt!r}")
    except Exception as e:
        await reply(message, f"Failed: {e}\nCheck that the bot is admin of the channel and CHANNEL_ID is right.")


@dp.message(Command("postnow"))
async def cmd_postnow(message: Message):
    if not is_admin(message):
        return
    state["next_post"] = 0
    save_state()
    await reply(message, "OK, posting the next item within 30 seconds.")


try:

    @dp.message()
    async def any_message(message: Message):
        if not is_admin(message):
            return
        if (message.text or "").startswith("/"):
            return
        if state.get("admin_chat") != message.chat.id:
            state["admin_chat"] = message.chat.id
            save_state()
        srcs = extract_sources(message.text)
        if srcs:
            results = [await add_source(s) for s in srcs]
            await reply(message, "\n".join(results))
            return
        lines = [f"chat id: {message.chat.id}"]
        fwd = getattr(message, "forward_from_chat", None) or getattr(message, "forward_origin", None)
        if fwd is not None:
            for attr in ("id", "title", "username", "chat"):
                val = getattr(fwd, attr, None)
                if val is not None:
                    lines.append(f"forwarded {attr}: {val}")
        await reply(message, "\n".join(lines))

except Exception as _e:
    log.warning("catch-all handler not registered: %s", _e)


# ---------------------------------------------------------------- main

async def main():
    global SESSION
    clean_tmp()
    load_state()
    SESSION = aiohttp.ClientSession()
    asyncio.create_task(scheduler())
    log.info("cloner bot started, sources: %s", ", ".join(state["sources"]))
    try:
        await dp.start_polling(bot)
    finally:
        await SESSION.close()


if __name__ == "__main__":
    asyncio.run(main())
