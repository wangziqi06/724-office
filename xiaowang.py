"""
AI Assistant — Entry Point

Start HTTP server, receive messaging platform callbacks, invoke tool use loop.
Module structure:
  xiaowang.py  — Entry: config, HTTP, callbacks, debounce (this file)
  llm.py       — LLM calls + tool use loop + session management
  tools.py     — Tool registry (add tools here only)
  messaging.py      — Messaging API wrapper (text/image/file/video/link/CDN)
  scheduler.py — Built-in scheduler (one-shot + cron)

Usage: python3 xiaowang.py
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import subprocess
import threading
import time
import urllib.request
import websocket
import ssl

# ============================================================
#  Configuration
# ============================================================

DATA_DIR = os.environ.get("AGENT_DATA", os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("AGENT_CONFIG", os.path.join(DATA_DIR, "config.json"))

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

# Multi-tenant: build USERS routing table
USERS = {}
for _uid, _ucfg in CONFIG.get("users", {}).items():
    _ws = os.path.abspath(_ucfg.get("workspace", f"./users/{_uid}"))
    os.makedirs(_ws, exist_ok=True)
    USERS[str(_uid)] = {
        "owner_id": str(_uid),
        "name": _ucfg.get("name", "user"),
        "workspace": _ws,
        "model": _ucfg.get("model", CONFIG["models"]["default"]),
    }
WORKSPACE = os.path.abspath(CONFIG.get("workspace", "./workspace"))
# Backward compatibility with owner_ids
for _oid in CONFIG.get("owner_ids", []):
    _sid = str(_oid)
    if _sid not in USERS:
        USERS[_sid] = {"owner_id": _sid, "name": "owner", "workspace": WORKSPACE, "model": CONFIG["models"]["default"]}
PORT = CONFIG.get("port", 8080)
DEBOUNCE_SECONDS = CONFIG.get("debounce_seconds", 1.5)
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
# Docker single-tenant: use first user workspace/files as default files dir
_first_user_ws = next(iter(USERS.values()))["workspace"] if USERS else WORKSPACE
FILES_DIR = os.path.join(_first_user_ws, "files")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agent")

# ============================================================
#  Module Initialization
# ============================================================

import messaging
import llm
import scheduler

# ============================================================
#  Group Chat Support — Name Cache + Helpers
# ============================================================

_name_cache = {}  # sender_id -> (name, timestamp)

# Group chat context buffer: non-@ messages stored silently, injected into LLM context when @-mentioned
from collections import deque
_group_context_buffers = {}  # group_id -> deque
GROUP_CONTEXT_MAX = 20


def _format_group_context(group_id):
    """Format group chat context buffer for LLM reference"""
    buf = _group_context_buffers.get(group_id, [])
    if not buf:
        return ""
    lines = ["[Recent group messages (not @-mentioning you, for context only)]"]
    for item in buf:
        lines.append("[%s] %s" % (item["sender"], item["text"]))
    return "\n".join(lines)


def _resolve_sender_name(sender_id):
    """Query sender nickname with 1-hour cache"""
    cached = _name_cache.get(sender_id)
    if cached and time.time() - cached[1] < 3600:
        return cached[0]
    try:
        info = messaging.get_contact_info([sender_id])
        if info:
            name = info[0].get("nickname") or info[0].get("remark") or ""
            if name:
                _name_cache[sender_id] = (name, time.time())
                return name
    except Exception:
        pass
    fallback = "user%s" % str(sender_id)[-6:]
    _name_cache[sender_id] = (fallback, time.time())
    return fallback


def _strip_at_mention(content):
    """Strip @xxx mention text from message start"""
    import re as _re2
    return _re2.sub(r'^@\S+\s*', '', content).strip()

messaging.init(CONFIG["messaging"])
llm.init(CONFIG["models"], USERS, SESSIONS_DIR)
scheduler.init(JOBS_FILE, llm.chat, USERS, sessions_dir=SESSIONS_DIR)

import tools
tools.init_extra(CONFIG)

# Initialize memory system
import memory as mem_mod
_mem_db = os.path.join(DATA_DIR, 'memory_db')
os.makedirs(_mem_db, exist_ok=True)
mem_mod.init(CONFIG, CONFIG.get('models', {}), _mem_db)

# ============================================================
#  Persistent File Storage
# ============================================================

FILES_INDEX = os.path.join(FILES_DIR, "index.json")


def _load_files_index():
    if os.path.exists(FILES_INDEX):
        try:
            with open(FILES_INDEX, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_files_index(index):
    with open(FILES_INDEX, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def save_media_file(tmp_path, media_type, filename="", files_dir=None):
    """Move temp file to persistent storage, return persistent path"""
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    now = datetime.now(CST)
    _fdir = files_dir or FILES_DIR
    month_dir = os.path.join(_fdir, now.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)

    ext = os.path.splitext(tmp_path)[1] or os.path.splitext(filename)[1] if filename else ".bin"
    if not ext:
        ext = ".bin"
    safe_name = filename.replace("/", "_").replace("\\", "_") if filename else ""
    import random as _rnd
    _ts_ms = int(now.timestamp() * 1000)
    _rand = '%04x' % _rnd.randint(0, 0xFFFF)
    stored_name = f"{_ts_ms}_{_rand}_{safe_name}" if safe_name else f"{_ts_ms}_{_rand}{ext}"
    dest = os.path.join(month_dir, stored_name)

    try:
        os.rename(tmp_path, dest)
    except OSError:
        import shutil
        shutil.move(tmp_path, dest)

    entry = {
        "path": dest,
        "type": media_type,
        "filename": filename or os.path.basename(dest),
        "size": os.path.getsize(dest),
        "time": now.isoformat(),
    }
    index = _load_files_index()
    index.append(entry)
    _save_files_index(index)
    log.info(f"[files] saved {media_type} to {dest}")
    return dest

# ============================================================
#  ASR (WebSocket Streaming Recognition)
# ============================================================

XFYUN_CONFIG = CONFIG.get("xfyun", {})


def xfyun_asr(audio_path):
    """WebSocket ASR: audio file -> text"""
    if not XFYUN_CONFIG:
        return None
    _asr_start = time.time()

    # Transcode to PCM: silk via pilk, other formats via ffmpeg
    pcm_path = audio_path + ".pcm"
    try:
        with open(audio_path, "rb") as f:
            header = f.read(10)
        if b"SILK" in header:
            import pilk
            pilk.decode(audio_path, pcm_path, pcm_rate=16000)
            log.info("[asr] silk -> pcm via pilk")
        else:
            subprocess.run(
                ["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", "-f", "s16le", pcm_path],
                capture_output=True, timeout=30
            )
            log.info("[asr] audio -> pcm via ffmpeg")
    except Exception as e:
        log.error(f"[asr] transcode error: {e}")
        return None

    if not os.path.exists(pcm_path) or os.path.getsize(pcm_path) == 0:
        log.error("[asr] transcode produced empty PCM")
        return None

    try:
        with open(pcm_path, "rb") as f:
            audio_data = f.read()
    finally:
        try:
            os.unlink(pcm_path)
        except Exception:
            pass

    # Build authentication URL
    from datetime import datetime
    from urllib.parse import urlencode
    import email.utils

    app_id = XFYUN_CONFIG["app_id"]
    api_key = XFYUN_CONFIG["api_key"]
    api_secret = XFYUN_CONFIG["api_secret"]

    url = "wss://iat-api.xfyun.cn/v2/iat"
    now = datetime.utcnow()
    date = email.utils.formatdate(timeval=time.mktime(now.timetuple()), usegmt=True)

    signature_origin = f"host: iat-api.xfyun.cn\ndate: {date}\nGET /v2/iat HTTP/1.1"
    signature_sha = hmac.new(api_secret.encode(), signature_origin.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(signature_sha).decode()

    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode()).decode()

    ws_url = url + "?" + urlencode({"authorization": authorization, "date": date, "host": "iat-api.xfyun.cn"})

    # WebSocket synchronous call
    result_text = []
    done_event = threading.Event()
    error_holder = [None]

    def on_message(ws, message):
        try:
            data = json.loads(message)
            code = data.get("code", 0)
            if code != 0:
                error_holder[0] = f"xfyun error code={code}: {data.get('message', '')}"
                done_event.set()
                return
            result = data.get("data", {}).get("result", {})
            ws_list = result.get("ws", [])
            for ws_item in ws_list:
                for cw in ws_item.get("cw", []):
                    result_text.append(cw.get("w", ""))
            if data.get("data", {}).get("status") == 2:
                done_event.set()
        except Exception as e:
            error_holder[0] = str(e)
            done_event.set()

    def on_error(ws, error):
        error_holder[0] = str(error)
        done_event.set()

    def on_open(ws):
        def send_audio():
            frame_size = 8000  # bytes per frame
            status = 0  # 0=first, 1=continue, 2=last
            offset = 0
            while offset < len(audio_data):
                end = min(offset + frame_size, len(audio_data))
                chunk = audio_data[offset:end]
                if offset + frame_size >= len(audio_data):
                    status = 2

                d = {
                    "common": {"app_id": app_id} if status == 0 else None,
                    "business": {
                        "language": "zh_cn",
                        "domain": "iat",
                        "accent": "mandarin",
                        "vad_eos": 3000,
                    } if status == 0 else None,
                    "data": {
                        "status": status,
                        "format": "audio/L16;rate=16000",
                        "encoding": "raw",
                        "audio": base64.b64encode(chunk).decode(),
                    },
                }
                # Remove None values
                d = {k: v for k, v in d.items() if v is not None}
                ws.send(json.dumps(d))

                if status == 0:
                    status = 1
                offset = end
                if status != 2:
                    time.sleep(0.04)  # Simulate real-time
        threading.Thread(target=send_audio, daemon=True).start()

    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_open=on_open,
    )
    wst = threading.Thread(target=lambda: ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}), daemon=True)
    wst.start()
    done_event.wait(timeout=15)
    ws.close()

    if error_holder[0]:
        _asr_elapsed = time.time() - _asr_start
        log.error(f"[asr] failed in {_asr_elapsed:.1f}s: {error_holder[0]}")
        return None

    text = "".join(result_text).strip()
    _asr_elapsed = time.time() - _asr_start
    if text:
        log.info(f"[asr] completed in {_asr_elapsed:.1f}s, recognized: {text[:100]}")
    else:
        log.warning(f"[asr] failed in {_asr_elapsed:.1f}s, no text recognized")
    return text if text else None


# ============================================================
#  Message Splitting
# ============================================================

def split_message(text, max_bytes=1800):
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        test = current + "\n" + line if current else line
        if len(test.encode("utf-8")) > max_bytes:
            if current:
                chunks.append(current)
            current = line
        else:
            current = test
    if current:
        chunks.append(current)
    return chunks

# ============================================================
#  Debounce
# ============================================================

_debounce_buffers = {}   # sender_id -> [{"text": str, "images": [path, ...]}]
_debounce_timers = {}
_debounce_pending = {}   # sender_id -> int (pending download count)
_debounce_pending_since = {}  # sender_id -> timestamp (first pending registered)
_debounce_lock = threading.Lock()
_PENDING_MAX_WAIT = 30  # Max wait 30s for download, force flush on timeout


def _debounce_flush(sender_id):
    with _debounce_lock:
        pending = _debounce_pending.get(sender_id, 0)
        if pending > 0:
            # Check if max wait time exceeded
            since = _debounce_pending_since.get(sender_id, time.time())
            waited = time.time() - since
            if waited < _PENDING_MAX_WAIT:
                log.info(f"[debounce] {sender_id}: {pending} downloads pending ({waited:.0f}s), deferring flush")
                timer = threading.Timer(DEBOUNCE_SECONDS, _debounce_flush, args=[sender_id])
                timer.daemon = True
                timer.start()
                _debounce_timers[sender_id] = timer
                return
            else:
                log.warning(f"[debounce] {sender_id}: force flush after {waited:.0f}s, {pending} downloads still pending")

        fragments = _debounce_buffers.pop(sender_id, [])
        _debounce_timers.pop(sender_id, None)
        _debounce_pending.pop(sender_id, None)
        _debounce_pending_since.pop(sender_id, None)

    if not fragments:
        return

    # Merge text and images
    texts = []
    images = []
    for frag in fragments:
        if isinstance(frag, dict):
            if frag.get("text"):
                texts.append(frag["text"])
            images.extend(frag.get("images", []))
        else:
            texts.append(str(frag))

    combined_text = "\n".join(texts)
    # Diagnostic log: record merged fragment count and content preview
    preview = combined_text[:80].replace("\n", " ")
    log.info(f"[debounce] flush {sender_id}: {len(fragments)} fragments, images={len(images)}, preview=\"{preview}\"")

    # Detect if group chat
    group_ctx = None
    for frag in fragments:
        if isinstance(frag, dict) and frag.get("group_ctx"):
            group_ctx = frag["group_ctx"]
            break

    if group_ctx:
        # ===== Group chat path =====
        group_id = group_ctx["group_id"]
        session_key = "wecom_group_%s" % group_id
        user_config = next(iter(USERS.values()), None)
        if not user_config:
            return
        try:
            log.info("[group] chat for room=%s, text=%s", group_id, combined_text[:100])
            reply = llm.chat(combined_text, session_key, images=images,
                           user_config=user_config, group_ctx=group_ctx)
            if reply and reply.strip():
                from tools_base import _strip_markdown, _split_message
                for chunk in _split_message(_strip_markdown(reply), 1800):
                    messaging.send_text(group_id, chunk)
        except Exception as e:
            log.error("[group] chat error for %s: %s", group_id, e, exc_info=True)
        return

    # ===== Direct message path (original logic unchanged) =====
    try:
        user_config = USERS.get(str(sender_id))
        if not user_config:
            messaging.send_text(sender_id, "Sorry, you have not activated the AI assistant service.")
            return

        # === Record user activity timestamp (for scheduler inactivity guard) ===
        try:
            activity_file = os.path.join(user_config.get("workspace", ""), ".last_user_message")
            with open(activity_file, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass

        # === Dormant recovery: user came back, remove dormant marker ===
        dormant_file = os.path.join(user_config.get("workspace", ""), ".dormant_since")
        if os.path.exists(dormant_file):
            try:
                os.remove(dormant_file)
                log.info("[dormant] user %s is back, removed dormant marker", sender_id)
            except Exception:
                pass

                log.info(f"[chat] {sender_id} -> tool use loop (images={len(images)})")
        session_key = f"wecom_dm_{sender_id}"
        reply = llm.chat(combined_text, session_key, images=images, user_config=user_config)

        if not reply or not reply.strip():
            log.warning(f"[chat] empty reply for {sender_id}")
            return

        # Pre-send buffer check: did new messages arrive during LLM processing?
        # Note: no longer discarding current reply. Side effects during LLM processing (write_file/schedule) already executed,
        # Discarding reply would hide results from user while state has changed.
        # New messages will be handled naturally by the debounce timer.
        with _debounce_lock:
            new_fragments = _debounce_buffers.get(sender_id, [])
            has_new = len(new_fragments) > 0

        if has_new:
            log.info(f"[pre-send-check] {sender_id}: {len(new_fragments)} new messages arrived during LLM, will be handled by next flush")

        from tools import _strip_markdown, _split_message
        for i, chunk in enumerate(_split_message(_strip_markdown(reply), 1800)):
            messaging.send_text(sender_id, chunk)
            if i > 0:
                time.sleep(0.5)

    except Exception as e:
        log.error(f"[flush] error for {sender_id}: {e}", exc_info=True)
        try:
            messaging.send_text(sender_id, f"Sorry, an error occurred while processing the message：{e}")
        except Exception:
            pass


def debounce_message(sender_id, text, images=None, group_ctx=None):
    with _debounce_lock:
        frag = {"text": text, "images": images or []}
        if group_ctx:
            frag["group_ctx"] = group_ctx
        _debounce_buffers.setdefault(sender_id, []).append(frag)
        old_timer = _debounce_timers.get(sender_id)
        if old_timer:
            old_timer.cancel()
        timer = threading.Timer(DEBOUNCE_SECONDS, _debounce_flush, args=[sender_id])
        timer.daemon = True
        timer.start()
        _debounce_timers[sender_id] = timer
        count = len(_debounce_buffers[sender_id])
    log.info(f"[debounce] {sender_id}: buffered #{count}")


def _register_pending(sender_id):
    """Register a pending download. Reset debounce timer, flush waits for all pending."""
    with _debounce_lock:
        _debounce_pending[sender_id] = _debounce_pending.get(sender_id, 0) + 1
        if sender_id not in _debounce_pending_since:
            _debounce_pending_since[sender_id] = time.time()
        # Reset timer
        old_timer = _debounce_timers.get(sender_id)
        if old_timer:
            old_timer.cancel()
        timer = threading.Timer(DEBOUNCE_SECONDS, _debounce_flush, args=[sender_id])
        timer.daemon = True
        timer.start()
        _debounce_timers[sender_id] = timer
        pending = _debounce_pending[sender_id]
    log.info(f"[debounce] {sender_id}: registered pending download (total pending: {pending})")


def _resolve_pending(sender_id, text, images=None):
    """After download: add result to buffer, decrement pending count, reset timer."""
    with _debounce_lock:
        _debounce_pending[sender_id] = max(0, _debounce_pending.get(sender_id, 0) - 1)
        frag = {"text": text, "images": images or []}
        _debounce_buffers.setdefault(sender_id, []).append(frag)
        # Reset timer(shorter delay after download, faster response)
        old_timer = _debounce_timers.get(sender_id)
        if old_timer:
            old_timer.cancel()
        pending = _debounce_pending[sender_id]
        flush_delay = 0.5 if pending == 0 else DEBOUNCE_SECONDS
        timer = threading.Timer(flush_delay, _debounce_flush, args=[sender_id])
        timer.daemon = True
        timer.start()
        _debounce_timers[sender_id] = timer
        count = len(_debounce_buffers[sender_id])
    log.info(f"[debounce] {sender_id}: resolved pending, buffered #{count} (remaining pending: {pending}, flush_delay={flush_delay}s)")

# ============================================================
#  Callback Processing
# ============================================================

def _download_media(msg_data, media_type="file"):
    """Download media file, return local path or None

    Three download paths (by priority):
    1. Has fileId -> /cloud/wxWorkDownload (work messaging format)
    2. Has fileAuthKey -> /cloud/wxDownload (personal format, images often use this)
    3. Has fileHttpUrl -> direct HTTP download (fallback)

    media_type used to infer messaging platform fileType: image=1 video=4 voice/file=5
    """
    file_id = msg_data.get("fileId", "")
    file_aes_key = msg_data.get("fileAeskey", msg_data.get("fileAesKey", ""))
    file_auth_key = msg_data.get("fileAuthkey", msg_data.get("fileAuthKey", ""))
    file_size = msg_data.get("fileSize", msg_data.get("fileBigSize", 0))

    # Infer messaging platform fileType
    ft_map = {"image": 1, "GIF": 1, "video_kw": 4, "voice": 5, "file": 5}
    file_type = ft_map.get(media_type, 5)

    # Path 1: work messaging format (has fileId)
    if file_id and file_aes_key:
        log.info(f"[media] trying wxWorkDownload (fileId={file_id[:20]}..., fileType={file_type})")
        path = messaging.download_wx_work(file_id, file_aes_key, file_size, file_type=file_type)
        if path:
            return path

    # Path 2: personal format (has fileAuthKey + URL)
    if file_auth_key:
        file_url = (msg_data.get("fileBigHttpUrl") or msg_data.get("fileMiddleHttpUrl") or
                    msg_data.get("fileThumbHttpUrl") or msg_data.get("fileHttpUrl") or "")
        if file_url:
            log.info(f"[media] trying wxDownload (authKey, fileType={file_type})")
            path = messaging.download_wx(file_aes_key, file_auth_key, file_url, file_size, file_type=file_type)
            if path:
                return path

    # Path 3: direct HTTP download (fallback)
    direct_url = (msg_data.get("fileHttpUrl") or msg_data.get("fileUrl") or "")
    if direct_url:
        log.info(f"[media] trying direct HTTP download")
        ext = messaging.get_ext(direct_url) or ".bin"
        tmp_path = f"/tmp/agent-recv-{int(time.time())}{ext}"
        try:
            urllib.request.urlretrieve(direct_url, tmp_path)
            return tmp_path
        except Exception as e:
            log.error(f"[media] direct download failed: {e}")

    log.error(f"[media] all download methods failed, keys={list(msg_data.keys())}")
    return None


def _handle_media_message(sender_id, msg_data, media_type, filename=""):
    """Handle received multimedia message: register pending immediately, async download, persist, notify LLM"""
    if str(sender_id) not in USERS:
        messaging.send_text(sender_id, "Sorry, you have not activated the AI assistant service.")
        return

    # Register pending immediately to prevent premature debounce flush
    _register_pending(sender_id)
    threading.Thread(
        target=_async_media_download,
        args=(sender_id, msg_data, media_type, filename),
        daemon=True
    ).start()


def _async_media_download(sender_id, msg_data, media_type, filename=""):
    """Async download media file, resolve pending on completion"""
    try:
        user_config = USERS[str(sender_id)]
        user_files_dir = os.path.join(user_config["workspace"], "files")
        os.makedirs(user_files_dir, exist_ok=True)
        file_size = msg_data.get("fileSize", 0)

        desc_parts = [f"[User sent {media_type}]"]
        if filename:
            desc_parts.append(f"Filename: {filename}")
        if file_size:
            size_kb = file_size / 1024
            desc_parts.append(f"Size: {size_kb/1024:.1f}MB" if size_kb > 1024 else f"Size: {size_kb:.0f}KB")

        tmp_path = _download_media(msg_data, media_type)
        image_paths = []

        if tmp_path:
            saved_path = save_media_file(tmp_path, media_type, filename, files_dir=user_files_dir)
            desc_parts.append(f"Saved to: {saved_path}")
            if media_type == "image":
                image_paths.append(saved_path)
        else:
            desc_parts.append("(file download failed)")

        _resolve_pending(sender_id, "\n".join(desc_parts), images=image_paths)
    except Exception as e:
        log.error(f"[media] async download error for {sender_id}: {e}", exc_info=True)
        _resolve_pending(sender_id, f"[User sent {media_type}, processing failed: {e}]")


def _handle_voice_message(sender_id, msg_data):
    """Handle voice message: register pending immediately, async download + ASR"""
    if str(sender_id) not in USERS:
        messaging.send_text(sender_id, "Sorry, you have not activated the AI assistant service.")
        return

    # Register pending immediately to prevent premature debounce flush
    _register_pending(sender_id)
    threading.Thread(
        target=_async_voice_process,
        args=(sender_id, msg_data),
        daemon=True
    ).start()


def _async_voice_process(sender_id, msg_data):
    """Async download voice + ASR, resolve pending on completion"""
    try:
        user_config = USERS[str(sender_id)]
        user_files_dir = os.path.join(user_config["workspace"], "files")
        os.makedirs(user_files_dir, exist_ok=True)

        tmp_path = _download_media(msg_data, "voice")
        if not tmp_path:
            _resolve_pending(sender_id, "[User sent voice message, but download failed]")
            return

        saved_path = save_media_file(tmp_path, "voice", files_dir=user_files_dir)

        text = xfyun_asr(saved_path)
        if text:
            _resolve_pending(sender_id, f"[voice-to-text] {text}")
        else:
            # Immediately notify user voice was unclear
            try:
                messaging.send_text(sender_id, "Could not understand the voice message. Please try again or type it.")
            except Exception as e:
                log.error(f"[asr] failed to send tip: {e}")
            _resolve_pending(sender_id, f"[User sent voice message，ASR failed, user has been notified to resend]\nSaved to: {saved_path}")
    except Exception as e:
        log.error(f"[voice] async process error for {sender_id}: {e}", exc_info=True)
        _resolve_pending(sender_id, f"[User sent voice message, processing failed: {e}]")


def handle_callback(data):
    if isinstance(data, dict) and "testMsg" in data:
        log.info(f"[callback] test: {data['testMsg']}")
        return
    if not isinstance(data, dict):
        return

    messages = data.get("data", [])
    if isinstance(messages, dict):
        messages = [messages]
    elif not isinstance(messages, list):
        return

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        cmd = msg.get("cmd")
        sender_id = msg.get("senderId")
        msg_type = msg.get("msgType")
        msg_data = msg.get("msgData", {})
        if not isinstance(msg_data, dict):
            msg_data = {}

        # Skip messages from self
        if str(sender_id) == str(msg.get("userId")):
            continue

        # Group chat detection
        from_room_id = str(msg.get("fromRoomId", 0) or 0)
        is_group = from_room_id != "0"

        if is_group:
            # Gate 1: config opt-in
            if not CONFIG.get("group_chat", {}).get("enabled", False):
                continue
            # Gate 2: only respond to @ mentions (non-empty atList = @-mentioned)
            at_list = msg_data.get("atList", [])
            if not at_list:
                # Non-@ message: silently store in context buffer
                if cmd == 15000 and msg_type in (0, 2, 1011):
                    msg_content = msg_data.get("content", "")
                    if msg_content:
                        sender_name = _resolve_sender_name(sender_id)
                        buf = _group_context_buffers.setdefault(
                            from_room_id, deque(maxlen=GROUP_CONTEXT_MAX))
                        buf.append({"sender": sender_name, "text": msg_content[:200]})
                        log.info("[group] buffered context in room %s from %s", from_room_id, sender_name)
                continue
            # Optional: exact match AI wechat_id
            ai_id = CONFIG.get("messaging", {}).get("wechat_id", "")
            if ai_id and not any(str(a.get("wxid", a.get("userId", ""))) == ai_id for a in at_list):
                continue
            log.info("[group] room=%s sender=%s", from_room_id, sender_id)

        if cmd == 15000:
            if msg_type in (0, 2, 1011):
                content = msg_data.get("content", "")
                if content:
                    log.info(f"[callback] text from {sender_id}: {content[:100]}")
                    if is_group:
                        sender_name = _resolve_sender_name(sender_id)
                        content = _strip_at_mention(content)
                        if not content:
                            continue
                        debounce_key = "group_%s" % from_room_id
                        group_ctx = {"group_id": from_room_id, "sender_id": sender_id}
                        context_str = _format_group_context(from_room_id)
                        if context_str:
                            group_ctx["recent_context"] = context_str
                        debounce_message(debounce_key, "[%s] %s" % (sender_name, content),
                                         group_ctx=group_ctx)
                    else:
                        debounce_message(sender_id, content)
            elif msg_type in (7, 14, 101):
                log.info(f"[callback] image from {sender_id}")
                if is_group:
                    sender_name = _resolve_sender_name(sender_id)
                    debounce_key = "group_%s" % from_room_id
                    debounce_message(debounce_key, "[%s] [sent an image]" % sender_name,
                                     group_ctx={"group_id": from_room_id, "sender_id": sender_id})
                else:
                    _handle_media_message(sender_id, msg_data, "image")
            elif msg_type in (22, 23, 103):
                log.info(f"[callback] video from {sender_id}")
                _handle_media_message(sender_id, msg_data, "video_kw")
            elif msg_type in (15, 20, 102):
                filename = msg_data.get("filename", msg_data.get("fileName", "unknown file"))
                log.info(f"[callback] file from {sender_id}: {filename}")
                _handle_media_message(sender_id, msg_data, "file", filename)
            elif msg_type in (29, 104):
                log.info(f"[callback] gif from {sender_id}")
                _handle_media_message(sender_id, msg_data, "GIF")
            elif msg_type == 16:
                log.info(f"[callback] voice from {sender_id}")
                _handle_voice_message(sender_id, msg_data)
            elif msg_type == 13:
                title = msg_data.get("title", "")
                url = msg_data.get("linkUrl", msg_data.get("url", ""))
                log.info(f"[callback] link from {sender_id}: {title}")
                debounce_message(sender_id, f"[User shared a link]\nTitle: {title}\nURL: {url}")
            elif msg_type == 6:
                # Extract all possible location fields
                label = msg_data.get("label", msg_data.get("poiname", ""))
                lat = msg_data.get("latitude", msg_data.get("lat", ""))
                lng = msg_data.get("longitude", msg_data.get("lng", ""))
                address = msg_data.get("address", msg_data.get("addr", ""))
                poiname = msg_data.get("poiname", msg_data.get("poiName", ""))
                log.info(f"[callback] location from {sender_id}: label={label}, lat={lat}, lng={lng}, addr={address}, poi={poiname}, keys={list(msg_data.keys())}")
                # Build complete location description for LLM
                parts = []
                if label or poiname:
                    parts.append(f"Name: {label or poiname}")
                if address:
                    parts.append(f"Address: {address}")
                if lat and lng:
                    parts.append(f"Coordinates: {lat},{lng}")
                if parts:
                    loc_desc = "; ".join(parts)
                    debounce_message(sender_id, f"[User sent location] {loc_desc}")
                else:
                    # Hardcoded reply, bypass LLM (same as ASR failure pattern)
                    messaging.send_text(sender_id, "I cannot see the exact location you sent. Please tell me where you are in text.")
                    log.info(f"[callback] location from {sender_id}: all fields empty, replied directly")
            elif msg_type == 26:
                # Red envelope
                log.info(f"[callback] red packet from {sender_id}")
                debounce_message(sender_id, "[User sent a red envelope. You cannot view or claim it. Just express thanks.]")
            elif msg_type == 78:
                # Mini program
                title = msg_data.get("title", msg_data.get("sourcedisplayname", ""))
                log.info(f"[callback] miniprogram(78) from {sender_id}: {title}")
                debounce_message(sender_id, f"[User shared a mini program: {title}，You cannot open it, only see the title]")
            elif msg_type == 123:
                # Rich text
                content = msg_data.get("content", "")
                log.info(f"[callback] richtext from {sender_id}")
                debounce_message(sender_id, f"[User sent rich text message, you can only see the text part]\n{content[:300]}" if content else "[User sent rich text message, you can only see the text part]")
            elif msg_type == 141:
                # Video channel
                title = msg_data.get("title", msg_data.get("desc", ""))
                log.info(f"[callback] video_channel from {sender_id}: {title}")
                debounce_message(sender_id, f"[User shared video channel content: {title}，You cannot play or view the video, only see the title]")
            elif msg_type == 146:
                # Livestream
                title = msg_data.get("title", "")
                log.info(f"[callback] livestream from {sender_id}: {title}")
                debounce_message(sender_id, f"[User shared a livestream: {title}，You cannot watch the livestream, only see the title]")
            elif msg_type in (47, 8):
                # Sticker / custom emoji
                log.info(f"[callback] sticker from {sender_id}")
                debounce_message(sender_id, "[User sent a sticker, you cannot see the specific content]")
            elif msg_type == 49:
                # App message (quoted reply / mini program / article etc.)
                title = msg_data.get("title", "")
                desc = msg_data.get("desc", msg_data.get("description", ""))
                url = msg_data.get("url", msg_data.get("linkUrl", ""))
                content = msg_data.get("content", "")
                parts = [f"[User sent an app message]"]
                if title:
                    parts.append(f"Title: {title}")
                if desc:
                    parts.append(f"Description: {desc}")
                if url:
                    parts.append(f"URL: {url}")
                if content and not title and not desc:
                    # May be a quoted reply, content contains XML
                    parts.append(f"Content: {content[:200]}")
                log.info(f"[callback] appmsg from {sender_id}: title={title}")
                debounce_message(sender_id, "\n".join(parts))
            elif msg_type in (33, 36):
                # Mini program
                title = msg_data.get("title", msg_data.get("sourcedisplayname", ""))
                log.info(f"[callback] miniprogram from {sender_id}: {title}")
                debounce_message(sender_id, f"[User shared a mini program: {title}，You cannot open it, only see the title]")
            elif msg_type in (41, 42):
                # Name card (41=document confirm, 42=compat)
                nickname = msg_data.get("nickname", msg_data.get("nickName", "unknown"))
                log.info(f"[callback] namecard from {sender_id}: {nickname}")
                debounce_message(sender_id, f"[User sent a contact card: {nickname}]")
            else:
                # Unknown type — do not silently drop, notify LLM
                content = msg_data.get("content", msg_data.get("title", ""))
                preview = f"，Content: {content[:80]}" if content else ""
                debounce_message(sender_id, f"[Received a message (type {msg_type}), cannot parse yet{preview}]")
        elif cmd == 15500:
            log.info(f"[callback] sys cmd=15500 type={msg_type}")
        elif cmd == 11016:
            log.info(f"[callback] account status: {msg_data.get('code', 0)}")

# ============================================================
#  HTTP Server
# ============================================================

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "service": "agent"}).encode())

    _MAX_BODY = 10 * 1024 * 1024  # 10MB

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > self._MAX_BODY:
            self.send_response(413)
            self.end_headers()
            return
        body = self.rfile.read(length)

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            log.error(f"[http] parse error: {e}")
            self.send_response(400)
            self.end_headers()
            return

        # /api/chat — LLM reply for Jetson voice (sync or SSE stream)
        if self.path == "/api/chat":
            msg = data.get("message", "")
            session_key = data.get("session_key", "voice")
            stream = data.get("stream", False)
            if not msg:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"message required"}')
                return

            from datetime import datetime, timezone, timedelta
            cst = timezone(timedelta(hours=8))
            now = datetime.now(cst).strftime("%Y-%m-%d %H:%M:%S")
            tagged_msg = f"[Source: voice assistant | Beijing time: {now}]" + chr(10) + msg

            if stream:
                # SSE streaming response
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    for chunk in llm.chat_stream(tagged_msg, session_key, user_config=USERS.get(next(iter(USERS), ""))):
                        sse = json.dumps({"choices":[{"delta":{"content": chunk}}]},
                                         ensure_ascii=False)
                        sse_line = "data: " + sse + chr(10) + chr(10)
                        self.wfile.write(sse_line.encode())
                        self.wfile.flush()
                    done_line = "data: [DONE]" + chr(10) + chr(10)
                    self.wfile.write(done_line.encode())
                    self.wfile.flush()
                except Exception as e:
                    log.error(f"[api/chat] stream error: {e}", exc_info=True)
                return
            else:
                # Synchronous response (original path)
                try:
                    reply = llm.chat(tagged_msg, session_key, user_config=USERS.get(next(iter(USERS), "")))
                    result = json.dumps({"reply": reply}, ensure_ascii=False)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(result.encode("utf-8"))
                except Exception as e:
                    log.error(f"[api/chat] error: {e}")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode())
                return

        # Other routes: send 200 immediately, process async
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"")

        if self.path == "/test":
            msg = data.get("message", "")
            if msg:
                def _test():
                    reply = llm.chat(msg, "test", user_config=USERS.get(next(iter(USERS), "")))
                    log.info(f"[test] reply: {reply[:200]}")
                threading.Thread(target=_test, daemon=True).start()
            return

        threading.Thread(target=handle_callback, args=(data,), daemon=True).start()

    def log_message(self, format, *args):
        pass

# ============================================================
#  Main
# ============================================================

def main():
    scheduler.start()
    log.info(f"[agent] starting on port {PORT}")
    log.info(f"[agent] workspace={WORKSPACE}")
    log.info(f"[agent] users={list(USERS.keys())}")
    log.info(f"[agent] model={CONFIG['models']['default']}")
    log.info(f"[agent] files_dir={FILES_DIR}")
    if XFYUN_CONFIG:
        log.info(f"[agent] xfyun ASR enabled (app_id={XFYUN_CONFIG.get('app_id', '?')})")

    # ThreadingMixIn: each request in independent thread, prevents single connection from blocking
    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("[agent] shutting down")
        try:
            import mcp_client
            mcp_client.shutdown()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
