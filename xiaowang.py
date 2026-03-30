"""
AI Agent - Entry Point

Start HTTP server, receive messaging platform callbacks, invoke tool use loop.
Module structure:
  agent.py  - Entry: config, HTTP, callbacks, debounce (this file)
  llm.py       - LLM calls + tool use loop + session management
  tools.py     - Tool registry (add tools only in this file)
  messaging.py - Messaging platform API wrapper (text/image/file/video/link/CDN)
  scheduler.py - Built-in scheduler (one-shot + cron)

Usage: python3 agent.py
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

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("AGENT_CONFIG", os.path.join(DATA_DIR, "config.json"))

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

OWNER_IDS = set(str(x) for x in CONFIG.get("owner_ids", []))
WORKSPACE = os.path.abspath(CONFIG.get("workspace", "./workspace"))
HOST = CONFIG.get("host","127.0.0.1")
PORT = CONFIG.get("port", 8080)
DEBOUNCE_SECONDS = CONFIG.get("debounce_seconds", 3.0)
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
FILES_DIR = os.path.join(WORKSPACE, "files")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agent")

# ============================================================
#  Initialize Modules
# ============================================================

import messaging
import llm
import scheduler

messaging.init(CONFIG["messaging"])
owner_id = next(iter(OWNER_IDS), "")
llm.init(CONFIG["models"], WORKSPACE, owner_id, SESSIONS_DIR)
scheduler.init(JOBS_FILE, llm.chat)

import tools
tools.init_extra(CONFIG)

# Initialize memory system
import memory as mem_mod
_mem_db = os.path.join(DATA_DIR, 'memory_db')
os.makedirs(_mem_db, exist_ok=True)
mem_mod.init(CONFIG, CONFIG.get('models', {}), _mem_db)

# ============================================================
#  File Persistent Storage
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


def save_media_file(tmp_path, media_type, filename=""):
    """Move temp file to persistent storage, return persistent path"""
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    now = datetime.now(CST)
    month_dir = os.path.join(FILES_DIR, now.strftime("%Y-%m"))
    os.makedirs(month_dir, exist_ok=True)

    ext = os.path.splitext(tmp_path)[1] or os.path.splitext(filename)[1] if filename else ".bin"
    if not ext:
        ext = ".bin"
    safe_name = filename.replace("/", "_").replace("\\", "_") if filename else ""
    stored_name = f"{int(now.timestamp())}_{safe_name}" if safe_name else f"{int(now.timestamp())}{ext}"
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
#  Speech-to-Text (WebSocket Streaming ASR)
# ============================================================

ASR_CONFIG = CONFIG.get("asr", {})


def asr_recognize(audio_path):
    """WebSocket ASR: audio file -> text"""
    if not ASR_CONFIG:
        return None

    # Transcode to PCM: silk uses pilk decoder, other formats use ffmpeg
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

    # Build auth URL
    from datetime import datetime
    from urllib.parse import urlencode
    import email.utils

    app_id = ASR_CONFIG["app_id"]
    api_key = ASR_CONFIG["api_key"]
    api_secret = ASR_CONFIG["api_secret"]

    url = ASR_CONFIG.get("ws_url", "wss://asr-api.example.com/v2/asr")
    now = datetime.utcnow()
    date = email.utils.formatdate(timeval=time.mktime(now.timetuple()), usegmt=True)

    signature_origin = f"host: asr-api.example.com\ndate: {date}\nGET /v2/asr HTTP/1.1"
    signature_sha = hmac.new(api_secret.encode(), signature_origin.encode(), hashlib.sha256).digest()
    signature = base64.b64encode(signature_sha).decode()

    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode()).decode()

    ws_url = url + "?" + urlencode({"authorization": authorization, "date": date, "host": "asr-api.example.com"})

    # WebSocket synchronous call
    result_text = []
    done_event = threading.Event()
    error_holder = [None]

    def on_message(ws, message):
        try:
            data = json.loads(message)
            code = data.get("code", 0)
            if code != 0:
                error_holder[0] = f"ASR error code={code}: {data.get('message', '')}"
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
                # Remove None entries
                d = {k: v for k, v in d.items() if v is not None}
                ws.send(json.dumps(d))

                if status == 0:
                    status = 1
                offset = end
                if status != 2:
                    time.sleep(0.04)  # simulate real-time streaming
        threading.Thread(target=send_audio, daemon=True).start()

    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_open=on_open,
    )
    wst = threading.Thread(target=lambda: ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}), daemon=True)
    wst.start()
    done_event.wait(timeout=30)
    ws.close()

    if error_holder[0]:
        log.error(f"[asr] {error_holder[0]}")
        return None

    text = "".join(result_text).strip()
    log.info(f"[asr] recognized: {text[:100]}")
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
_debounce_lock = threading.Lock()


def _debounce_flush(sender_id):
    with _debounce_lock:
        fragments = _debounce_buffers.pop(sender_id, [])
        _debounce_timers.pop(sender_id, None)

    if not fragments:
        return

    # 合并文本和image
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
    if len(fragments) > 1:
        log.info(f"[debounce] {sender_id}: merged {len(fragments)} messages")

    try:
        if str(sender_id) not in OWNER_IDS:
            messaging.send_text(sender_id, "Sorry, this agent is currently in single-user mode.")
            return

        log.info(f"[chat] {sender_id} -> tool use loop (images={len(images)})")
        session_key = f"dm_{sender_id}"
        reply = llm.chat(combined_text, session_key, images=images)

        if not reply or not reply.strip():
            log.warning(f"[chat] empty reply for {sender_id}")
            return

        for i, chunk in enumerate(split_message(reply, 1800)):
            messaging.send_text(sender_id, chunk)
            if i > 0:
                time.sleep(0.5)

    except Exception as e:
        log.error(f"[flush] error for {sender_id}: {e}", exc_info=True)
        try:
            messaging.send_text(sender_id, f"Sorry, error processing message: {e}")
        except Exception:
            pass


def debounce_message(sender_id, text, images=None):
    with _debounce_lock:
        frag = {"text": text, "images": images or []}
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

# ============================================================
#  Callback Handler
# ============================================================

def _download_media(msg_data, media_type="file"):
    """下载媒体file，返回本地路径或 None

    Three download paths (by priority):
    1. Has fileId -> platform download (enterprise format)
    2. 有 fileAuthKey → /cloud/wxDownload（个微格式，image常走这个）
    3. Has fileHttpUrl -> direct HTTP download (fallback)

    media_type 用于Infer file type: image=1 video=4 voice/file=5
    """
    file_id = msg_data.get("fileId", "")
    file_aes_key = msg_data.get("fileAeskey", msg_data.get("fileAesKey", ""))
    file_auth_key = msg_data.get("fileAuthkey", msg_data.get("fileAuthKey", ""))
    file_size = msg_data.get("fileSize", msg_data.get("fileBigSize", 0))

    # Infer file type
    ft_map = {"image": 1, "GIF": 1, "video": 4, "voice": 5, "file": 5}
    file_type = ft_map.get(media_type, 5)

    # Path 1: Enterprise format (has fileId)
    if file_id and file_aes_key:
        log.info(f"[media] trying wxWorkDownload (fileId={file_id[:20]}..., fileType={file_type})")
        path = messaging.download_enterprise(file_id, file_aes_key, file_size, file_type=file_type)
        if path:
            return path

    # Path 2: Personal format (has fileAuthKey + URL)
    if file_auth_key:
        file_url = (msg_data.get("fileBigHttpUrl") or msg_data.get("fileMiddleHttpUrl") or
                    msg_data.get("fileThumbHttpUrl") or "")
        if file_url:
            log.info(f"[media] trying wxDownload (authKey, fileType={file_type})")
            path = messaging.download_personal(file_aes_key, file_auth_key, file_url, file_size, file_type=file_type)
            if path:
                return path

    # Path 3: Direct HTTP download (fallback)
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
    """Handle received multimedia: download, persist, inform LLM"""
    if str(sender_id) not in OWNER_IDS:
        messaging.send_text(sender_id, "Sorry, this agent is currently in single-user mode.")
        return

    file_size = msg_data.get("fileSize", 0)

    desc_parts = [f"[用户发送了{media_type}]"]
    if filename:
        desc_parts.append(f"Filename: {filename}")
    if file_size:
        size_kb = file_size / 1024
        desc_parts.append(f"Size: {size_kb/1024:.1f}MB" if size_kb > 1024 else f"Size: {size_kb:.0f}KB")

    tmp_path = _download_media(msg_data, media_type)

    saved_path = None
    image_paths = []

    if tmp_path:
        saved_path = save_media_file(tmp_path, media_type, filename)
        desc_parts.append(f"Saved to: {saved_path}")
        # 如果是image，传给 vision
        if media_type == "image":
            image_paths.append(saved_path)
    else:
        desc_parts.append("(file download failed)")

    debounce_message(sender_id, "\n".join(desc_parts), images=image_paths)


def _handle_voice_message(sender_id, msg_data):
    """处理voice消息：下载 → ffmpeg → ASR -> text"""
    if str(sender_id) not in OWNER_IDS:
        messaging.send_text(sender_id, "Sorry, this agent is currently in single-user mode.")
        return

    tmp_path = _download_media(msg_data, "voice")
    if not tmp_path:
        debounce_message(sender_id, "[User sent voice message, but download failed]")
        return

    # 持久存储voicefile
    saved_path = save_media_file(tmp_path, "voice")

    # Attempt ASR
    text = asr_recognize(saved_path)
    if text:
        debounce_message(sender_id, f"[Voice-to-text] {text}")
    else:
        debounce_message(sender_id, f"[User sent voice message, ASR failed]\nSaved to: {saved_path}")


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

        # Skip messages sent by self
        if str(sender_id) == str(msg.get("userId")):
            continue

        if cmd == 15000:
            if msg_type in (0, 2):
                content = msg_data.get("content", "")
                if content:
                    log.info(f"[callback] text from {sender_id}: {content[:100]}")
                    debounce_message(sender_id, content)
            elif msg_type in (7, 14, 101):
                log.info(f"[callback] image from {sender_id}")
                _handle_media_message(sender_id, msg_data, "image")
            elif msg_type in (22, 23, 103):
                log.info(f"[callback] video from {sender_id}")
                _handle_media_message(sender_id, msg_data, "video")
            elif msg_type in (15, 20, 102):
                filename = msg_data.get("filename", msg_data.get("fileName", "unknown"))
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
                label = msg_data.get("label", msg_data.get("poiname", ""))
                log.info(f"[callback] location from {sender_id}: {label}")
                debounce_message(sender_id, f"[User sent location: {label}]")
            else:
                log.info(f"[callback] msgType={msg_type} from {sender_id}")
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

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"")

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as e:
            log.error(f"[http] parse error: {e}")
            return

        if self.path == "/test":
            msg = data.get("message", "")
            if msg:
                def _test():
                    reply = llm.chat(msg, "test")
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
    log.info(f"[agent] starting on {HOST}:{PORT}")
    log.info(f"[agent] workspace={WORKSPACE}")
    log.info(f"[agent] owners={OWNER_IDS}")
    log.info(f"[agent] model={CONFIG['models']['default']}")
    log.info(f"[agent] files_dir={FILES_DIR}")
    if ASR_CONFIG:
        log.info(f"[agent] asr ASR enabled (app_id={ASR_CONFIG.get('app_id', '?')})")

    # ThreadingMixIn: each request in its own thread, prevents blocking
    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer((HOST, PORT), Handler)
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
