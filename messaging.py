"""
messaging.py - Discord Bot adapter for 724-office (discord.py edition)

Uses the discord.py library for reliable bot integration.
Same external interface as before:
  - init(config)
  - send_text(to_id, text)        -> {"code": 0} or {"code": -1, "msg": ...}
  - send_link(to_id, title, desc, link_url, icon_url="") -> {"code": 0/1}
  - upload_and_send(to_id, path_or_url, caption, workspace) -> {"code": 0/1}
  - download_enterprise(file_id, aes_key, file_size, file_type=5) -> local_path or None
  - download_personal(aes_key, auth_key, file_url, file_size, file_type=5) -> local_path or None
  - get_ext(url) -> ".jpg" etc.
  - start_gateway(on_message_callback)
  - get_bot_user_id()

config.json "messaging" block:
  {
    "messaging": {
      "provider": "discord",
      "bot_token": "YOUR_BOT_TOKEN",
      "channel_id": "YOUR_CHANNEL_ID",
      "guild_id":   "YOUR_SERVER_ID"
    }
  }

Discord Developer Portal requirements:
  - Bot tab -> enable MESSAGE CONTENT INTENT
  - OAuth2 -> invite with scopes: bot
  - Bot Permissions: Send Messages, Read Messages/View Channels,
                     Attach Files, Embed Links, Read Message History
"""

import asyncio
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import discord

log = logging.getLogger("agent")

# ============================================================
#  State
# ============================================================

_bot_token = ""
_default_channel_id = ""
_guild_id = ""
_client: discord.Client | None = None
_loop: asyncio.AbstractEventLoop | None = None
_ready_event = threading.Event()

# ============================================================
#  Discord Client
# ============================================================

class _AgentClient(discord.Client):
    def __init__(self, on_message_callback=None, **kwargs):
        super().__init__(**kwargs)
        self._on_message_callback = on_message_callback

    async def on_ready(self):
        log.info(f"[messaging] Discord READY as {self.user} (id={self.user.id})")
        _ready_event.set()

        if _default_channel_id:
            ch = self.get_channel(int(_default_channel_id))
            if ch:
                try:
                    await ch.send("🤖 Agent started successfully and is now online!")
                    log.info("[messaging] Greeting sent to Discord.")
                except Exception as e:
                    log.error(f"[messaging] Failed to send greeting: {e}")
            else:
                log.error(
                    f"[messaging] Channel {_default_channel_id} not found in cache. "
                    "Ensure the bot is invited to the server and channel_id is correct."
                )

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return
        if not self._on_message_callback:
            return

        user_id = str(message.author.id)
        # Use channel_id as senderId so replies go back to the same channel.
        # For DMs, channel_id == the DM channel which routes back to the user.
        reply_to = str(message.channel.id)
        messages = []

        if message.content:
            messages.append({
                "cmd":      15000,
                "senderId": reply_to,
                "userId":   str(self.user.id),
                "msgType":  0,
                "msgData":  {"content": message.content, "discordUserId": user_id},
            })

        for att in message.attachments:
            ct = att.content_type or ""
            if ct.startswith("image/"):
                msg_type = 7
            elif ct.startswith("video/"):
                msg_type = 22
            elif ct.startswith("audio/"):
                msg_type = 16
            else:
                msg_type = 20

            messages.append({
                "cmd":      15000,
                "senderId": reply_to,
                "userId":   str(self.user.id),
                "msgType":  msg_type,
                "msgData":  {
                    "fileId":       att.url,
                    "fileSize":     att.size,
                    "filename":     att.filename,
                    "fileAesKey":   "",
                    "discordUserId": user_id,
                },
            })

        if messages:
            log.info(
                f"[gateway] MESSAGE_CREATE from {user_id} in {reply_to}: "
                f"text={bool(message.content)}, attachments={len(message.attachments)}"
            )
            self._on_message_callback({"data": messages})

# ============================================================
#  Init
# ============================================================

def init(config: dict):
    global _bot_token, _default_channel_id, _guild_id

    _bot_token          = str(config.get("bot_token", ""))
    _default_channel_id = str(config.get("channel_id", ""))
    _guild_id           = str(config.get("guild_id", ""))

    if not _bot_token:
        log.warning("[messaging] Discord bot_token not configured")
        return

    log.info(f"[messaging] Discord configured (channel={_default_channel_id})")

# ============================================================
#  Internal helpers
# ============================================================

def _run_coroutine(coro):
    """Run an async coroutine from sync code on the bot's event loop."""
    if not _loop or not _loop.is_running():
        log.error("[messaging] Discord event loop not running")
        return None
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=15)
    except Exception as e:
        log.error(f"[messaging] Coroutine failed: {e}")
        return None


async def _resolve_channel_async(to_id: str) -> discord.abc.Messageable | None:
    """Resolve to_id to a guild channel or DM channel."""
    if not _client:
        return None
    target = to_id or _default_channel_id
    if not target:
        return None

    # Try as guild channel
    if target.isdigit():
        ch = _client.get_channel(int(target))
        if ch:
            return ch

    # Try as user DM
    if target.isdigit():
        user = _client.get_user(int(target))
        if user:
            try:
                return await user.create_dm()
            except Exception as e:
                log.error(f"[messaging] DM create failed for {target}: {e}")

    # Fall back to default channel
    if _default_channel_id and _default_channel_id != target:
        return _client.get_channel(int(_default_channel_id))

    return None


def _chunk_text(text: str, max_chars: int = 2000) -> list:
    if len(text) <= max_chars:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > max_chars:
            if current:
                chunks.append(current)
            while len(line) > max_chars:
                chunks.append(line[:max_chars])
                line = line[max_chars:]
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks

# ============================================================
#  Public API — send_text
# ============================================================

def send_text(to_id: str, text: str) -> dict:
    async def _send():
        ch = await _resolve_channel_async(to_id)
        if not ch:
            log.error(f"[messaging] Cannot resolve channel for '{to_id}'")
            return {"code": -1, "msg": "Channel not found"}
        chunks = _chunk_text(text, 2000)
        for chunk in chunks:
            await ch.send(chunk)
        log.info(f"[messaging] text sent ({len(text)} chars, {len(chunks)} part(s))")
        return {"code": 0}

    result = _run_coroutine(_send())
    return result if result else {"code": -1, "msg": "Event loop error"}

# ============================================================
#  Public API — send_link
# ============================================================

def send_link(to_id: str, title: str, desc: str, link_url: str, icon_url: str = "") -> dict:
    async def _send():
        ch = await _resolve_channel_async(to_id)
        if not ch:
            return {"code": 1, "msg": "Channel not found"}
        embed = discord.Embed(title=title, description=desc, url=link_url, color=0x5865F2)
        if icon_url:
            embed.set_thumbnail(url=icon_url)
        await ch.send(embed=embed)
        log.info(f"[messaging] link embed sent: {title}")
        return {"code": 0}

    result = _run_coroutine(_send())
    return result if result else {"code": 1, "msg": "Event loop error"}

# ============================================================
#  Public API — upload_and_send
# ============================================================

def upload_and_send(to_id: str, path_or_url: str, caption: str, workspace: str) -> dict:
    local_path = path_or_url
    tmp_created = False
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        ext = get_ext(path_or_url) or ".bin"
        local_path = f"/tmp/dc_send_{int(time.time())}{ext}"
        if not _download_url(path_or_url, local_path):
            return {"code": 1, "msg": "Failed to download URL"}
        tmp_created = True

    if not os.path.exists(local_path):
        return {"code": 1, "msg": f"File not found: {local_path}"}

    async def _send():
        ch = await _resolve_channel_async(to_id)
        if not ch:
            return {"code": 1, "msg": "Channel not found"}
        await ch.send(content=caption or None, file=discord.File(local_path))
        log.info(f"[messaging] file sent: {os.path.basename(local_path)}")
        return {"code": 0}

    result = _run_coroutine(_send())
    if tmp_created:
        try:
            os.unlink(local_path)
        except Exception:
            pass
    return result if result else {"code": 1, "msg": "Event loop error"}

# ============================================================
#  Public API — download helpers
# ============================================================

def download_enterprise(file_id: str, aes_key: str, file_size: int,
                        file_type: int = 5) -> str | None:
    """For Discord, file_id is the CDN attachment URL."""
    if not file_id:
        return None
    ext = get_ext(file_id) or ".bin"
    tmp_path = f"/tmp/dc_recv_{int(time.time())}{ext}"
    return tmp_path if _download_url(file_id, tmp_path) else None


def download_personal(aes_key: str, auth_key: str, file_url: str,
                      file_size: int, file_type: int = 5) -> str | None:
    if not file_url:
        return None
    ext = get_ext(file_url) or ".bin"
    tmp_path = f"/tmp/dc_recv_{int(time.time())}{ext}"
    return tmp_path if _download_url(file_url, tmp_path) else None

# ============================================================
#  Public API — get_ext
# ============================================================

def get_ext(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    _, ext = os.path.splitext(parsed.path)
    return ext.lower() if ext else ""

# ============================================================
#  Internal download helper
# ============================================================

def _download_url(url: str, dest_path: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "724-office-agent"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(dest_path, "wb") as f:
                f.write(resp.read())
        return True
    except Exception as e:
        log.error(f"[messaging] download {url[:80]} failed: {e}")
        return False

# ============================================================
#  Discord-specific: get bot user ID
# ============================================================

def get_bot_user_id() -> str:
    if _client and _client.user:
        return str(_client.user.id)
    _ready_event.wait(timeout=10)
    if _client and _client.user:
        return str(_client.user.id)
    return ""

# ============================================================
#  start_gateway — runs discord.py client in a background thread
# ============================================================

def start_gateway(on_message_callback):
    global _client, _loop

    intents = discord.Intents.default()
    intents.message_content = True

    _client = _AgentClient(on_message_callback=on_message_callback, intents=intents)

    def _run():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop.run_until_complete(_client.start(_bot_token))

    thread = threading.Thread(target=_run, daemon=True, name="discord-gateway")
    thread.start()
    log.info("[messaging] Discord Gateway listener started (discord.py)")
