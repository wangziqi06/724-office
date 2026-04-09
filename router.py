"""
Docker Router — Route by sender_id to corresponding container, auto-provision unknown users

Receive messaging platform callbacks, parse sender_id:
- Known user -> forward to corresponding container
- Unknown user -> auto-create container -> wait for health check -> forward
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import hashlib
import http.client
import json
import logging
import os
import socket
import threading
import time
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("router")

# -- Configuration --────────────────────────────────────────────────────
ROUTING_FILE = os.environ.get("ROUTING_FILE", "/data/router/routing.json")
DEFAULT_BACKEND = os.environ.get("DEFAULT_BACKEND", "")
GROUP_CHAT_BACKEND = os.environ.get("GROUP_CHAT_BACKEND", "")
HOST_DATA_DIR = os.environ.get("HOST_DATA_DIR", "/data/agent/containers")
APP_IMAGE = os.environ.get("APP_IMAGE", "agent-app:latest")
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "docker_agent-net")
ENV_FILE_PATH = os.environ.get("ENV_FILE_PATH", "/data/env/.env")
MAX_CONTAINERS = int(os.environ.get("MAX_CONTAINERS", "20"))
CONTAINER_MEMORY = os.environ.get("CONTAINER_MEMORY", "256m")
PROVISION_TIMEOUT = int(os.environ.get("PROVISION_TIMEOUT", "45"))

ROUTING = {}
_routing_lock = threading.Lock()
_provision_lock = threading.Lock()  # one provision at a time

# Callback dedup: same sender_id + same content processed only once within 2s (platform often sends multiple callbacks for same event)
# key = "sender_id:body_hash" -> timestamp
_recent_callbacks = {}
_recent_callbacks_lock = threading.Lock()

def _is_internal(addr):
    """Check if request is from Docker internal network (172.x) or localhost"""
    if not addr:
        return False
    return (addr.startswith("172.") or addr.startswith("127.") or
            addr == "::1" or addr == "localhost")

# Messaging API config (populated after loading .env)
MSG_TOKEN = ""
MSG_GUID = ""
MSG_API_URL = "http://manager.messaging-api.com/api/sendMessage"

# -- Routing Table Management --──────────────────────────────────────────────

def load_routing():
    global ROUTING
    try:
        with open(ROUTING_FILE, "r") as f:
            ROUTING = json.load(f)
        log.info("Loaded routing: %d entries", len(ROUTING))
    except Exception as e:
        log.error("Failed to load routing: %s", e)


def save_routing():
    """Write routing.json (bind mount does not support rename, overwrite directly)"""
    try:
        with open(ROUTING_FILE, "w") as f:
            json.dump(ROUTING, f, indent=2, ensure_ascii=False)
        log.info("Saved routing: %d entries", len(ROUTING))
    except Exception as e:
        log.error("Failed to save routing: %s", e)


# -- .env File Parsing --───────────────────────────────────────────

def load_env_file(path):
    """Parse .env file, return list of KEY=VALUE pairs"""
    envs = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    envs.append(line)
        log.info("Loaded %d env vars from %s", len(envs), path)
    except Exception as e:
        log.error("Failed to load env file %s: %s", path, e)
    return envs


SHARED_ENV = []  # Loaded at startup


# ── Docker Engine API（Unix Socket）────────────────────────────

class DockerConnection(http.client.HTTPConnection):
    """Connect to Docker Engine API via Unix Socket"""
    def __init__(self):
        super().__init__("localhost")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")
        self.sock.settimeout(30)


def docker_api(method, path, body=None):
    """Call Docker Engine API, return (status, response_dict)"""
    conn = DockerConnection()
    headers = {"Content-Type": "application/json"} if body else {}
    data = json.dumps(body).encode() if body else None
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read().decode()
        try:
            return resp.status, json.loads(resp_body)
        except json.JSONDecodeError:
            return resp.status, {"raw": resp_body}
    except Exception as e:
        log.error("Docker API %s %s failed: %s", method, path, e)
        return 500, {"error": str(e)}
    finally:
        conn.close()


def count_user_containers():
    """Count existing auto-provisioned containers (names starting with agent-u)"""
    status, data = docker_api(
        "GET",
        '/containers/json?all=true&filters={"name":["agent-u"]}',
    )
    if status == 200 and isinstance(data, list):
        return len(data)
    return 0


# -- Auto-Provisioning --────────────────────────────────────────────────

def _parse_memory_bytes(mem_str):
    """'256m' -> 268435456"""
    mem_str = mem_str.strip().lower()
    if mem_str.endswith("g"):
        return int(float(mem_str[:-1]) * 1024 * 1024 * 1024)
    if mem_str.endswith("m"):
        return int(float(mem_str[:-1]) * 1024 * 1024)
    return int(mem_str)


def provision_container(sender_id):
    """Create container for new user, return backend_url or None. Holds lock throughout, one provision at a time."""

    with _provision_lock:
        # Double-check (may have been provisioned by another thread while waiting for lock)
        if sender_id in ROUTING:
            return ROUTING[sender_id], False  # Already exists, not newly created

        # Check container count limit
        current_count = count_user_containers()
        if current_count >= MAX_CONTAINERS:
            log.warning("Container limit reached (%d/%d), rejecting sender_id=%s",
                       current_count, MAX_CONTAINERS, sender_id)
            return None, False

        try:
            short_id = sender_id[-8:]
            container_name = f"agent-u{short_id}"
            host_volume = f"{HOST_DATA_DIR}/{container_name}"
            backend_url = f"http://{container_name}:8080"

            log.info("[provision] Creating container %s for sender_id=%s", container_name, sender_id)

            # Build environment variables
            env_list = list(SHARED_ENV) + [
                f"OWNER_ID={sender_id}",
                "USER_NAME=new_user",
                "MODEL_DEFAULT=kimi-k2.5",
                "AGENT_DATA=/data",
                "AGENT_CONFIG=/data/config.json",
                "MCP_SERVERS={}",
                "TZ=Asia/Shanghai",
            ]

            # Create container
            create_body = {
                "Image": APP_IMAGE,
                "Env": env_list,
                "ExposedPorts": {"8080/tcp": {}},
                "HostConfig": {
                    "Binds": [f"{host_volume}:/data", "/data/agent/pages:/pages"],
                    "Memory": _parse_memory_bytes(CONTAINER_MEMORY),
                    "RestartPolicy": {"Name": "unless-stopped"},
                },
                "NetworkingConfig": {
                    "EndpointsConfig": {
                        DOCKER_NETWORK: {}
                    }
                },
                "Healthcheck": {
                    "Test": ["CMD", "curl", "-f", "http://localhost:8080/"],
                    "Interval": 5000000000,
                    "Timeout": 3000000000,
                    "Retries": 3,
                },
            }

            status, resp = docker_api(
                "POST",
                f"/containers/create?name={container_name}",
                create_body,
            )

            if status not in (200, 201):
                log.error("[provision] Create failed: %s %s", status, resp)
                return None, False

            container_id = resp.get("Id", "")
            log.info("[provision] Created container %s (%s)", container_name, container_id[:12])

            # Start container
            start_status, _ = docker_api("POST", f"/containers/{container_id}/start")
            if start_status not in (200, 204):
                log.error("[provision] Start failed: %s", start_status)
                docker_api("DELETE", f"/containers/{container_id}?force=true")
                return None, False

            log.info("[provision] Started %s, waiting for health...", container_name)

            # Wait for container health (HTTP reachable)
            healthy = False
            deadline = time.time() + PROVISION_TIMEOUT
            while time.time() < deadline:
                time.sleep(2)
                try:
                    req = urllib.request.Request(f"http://{container_name}:8080/", method="GET")
                    with urllib.request.urlopen(req, timeout=3) as r:
                        if r.status == 200:
                            healthy = True
                            break
                except Exception:
                    pass

            if not healthy:
                log.error("[provision] Container %s not healthy after %ds", container_name, PROVISION_TIMEOUT)
                log.warning("[provision] Adding route anyway, container may still be starting")

            # Update routing table
            with _routing_lock:
                ROUTING[sender_id] = backend_url
                save_routing()

            log.info("[provision] ✓ %s ready, route: %s -> %s", container_name, sender_id, backend_url)
            return backend_url, True  # Successfully created

        except Exception as e:
            log.error("[provision] Unexpected error: %s", e, exc_info=True)
            return None, False


# ── messaging platform API（Send messages directly）─────────────────────────────────────

def msg_send_text(to_id, content):
    """Send message to user directly via messaging API"""
    if not MSG_TOKEN or not MSG_GUID:
        log.warning("[msg] No token/guid configured, cannot send message")
        return False
    body = json.dumps({
        "method": "/msg/sendText",
        "params": {"guid": MSG_GUID, "toId": str(to_id), "content": content},
    }).encode("utf-8")
    req = urllib.request.Request(
        MSG_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-MSG-TOKEN": MSG_TOKEN,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 0:
                log.info("[msg] Greeting sent to %s", to_id)
                return True
            log.error("[msg] Send failed: %s", result)
    except Exception as e:
        log.error("[msg] Send error: %s", e)
    return False


GREETING_MESSAGE = "Hi! I am the assistant here. How should I address you?"  # Minimal greeting, detailed onboarding handled by in-container LLM


# -- HTTP Forwarding --───────────────────────────────────────────────

def forward(url, body, headers):
    """Forward request to backend container"""
    try:
        req = urllib.request.Request(
            url,
            data=body,
            headers={k: v for k, v in headers.items() if k.lower() not in ("host", "content-length")},
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except Exception as e:
        log.error("Forward to %s failed: %s", url, e)
        return 502, b""


# -- HTTP Handler --────────────────────────────────────────────

class RouterHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def do_GET(self):
        if self.path == "/health":
            user_containers = count_user_containers()
            body = json.dumps({
                "status": "ok",
                "routes": len(ROUTING),
                "auto_containers": user_containers,
                "max_containers": MAX_CONTAINERS,
            }).encode()
            self._respond(200, body)

        elif self.path == "/reload":
            if not _is_internal(self.client_address[0]):
                self._respond(403, b'{"error": "forbidden"}')
                return
            load_routing()
            body = json.dumps({"status": "reloaded", "routes": len(ROUTING)}).encode()
            self._respond(200, body)

        elif self.path == "/routes":
            if not _is_internal(self.client_address[0]):
                self._respond(403, b'{"error": "forbidden"}')
                return
            body = json.dumps(ROUTING, indent=2, ensure_ascii=False).encode()
            self._respond(200, body)

        else:
            self._respond(200, b"ok")

    _MAX_BODY = 10 * 1024 * 1024  # 10MB

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > self._MAX_BODY:
            self._respond(413, b'{"error": "body too large"}')
            return
        body = self.rfile.read(content_length) if content_length else b""

        # /api/chat — Jetson voice, sync proxy to DEFAULT_BACKEND
        if self.path == "/api/chat":
            if not DEFAULT_BACKEND:
                self._respond(503, b"")
                return
            status, resp_body = forward(DEFAULT_BACKEND + "/api/chat", body, dict(self.headers))
            self._respond(status, resp_body)
            return

        # Messaging callback paths: root + /message + /msg/callback all use routing logic
        CALLBACK_PATHS = ("/", "", "/message", "/msg/callback")
        if self.path not in CALLBACK_PATHS:
            if DEFAULT_BACKEND:
                status, resp_body = forward(DEFAULT_BACKEND + self.path, body, dict(self.headers))
                # Already returned 200, do not respond again
            self._respond(200, b"")
            return

        # Messaging callback — return 200 immediately, process in background
        self._respond(200, b"")

        if not body:
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            log.warning("Invalid JSON in callback")
            return

        # Handle data being dict or list
        data_list = payload.get("data", [])
        if isinstance(data_list, dict):
            data_list = [data_list]
        if not isinstance(data_list, list) or not data_list:
            return

        # Extract sender_id (handle various callback formats)
        first = data_list[0]
        if not isinstance(first, dict):
            return
        sender_id = str(first.get("senderId", "") or first.get("userId", "") or "")
        if not sender_id:
            return

        # Log callback source and type for debugging and monitoring
        cmd = first.get("cmd")
        msg_type = first.get("msgType", first.get("type", ""))
        client_ip = self.client_address[0] if self.client_address else "unknown"
        log.info("[callback] sender=%s cmd=%s type=%s from=%s", sender_id, cmd, msg_type, client_ip)

        # Skip messages from self
        user_id = str(first.get("userId", ""))
        if sender_id == user_id and cmd == 15000:
            return

        # Callback dedup: same sender_id + same content processed only once within 2s
        # Platform sends multiple callbacks for same event (same body), but user sending different messages quickly (different body) should not be deduped
        body_hash = hashlib.md5(body).hexdigest()[:10]
        dedup_key = "%s:%s" % (sender_id, body_hash)
        now = time.time()
        with _recent_callbacks_lock:
            last = _recent_callbacks.get(dedup_key, 0)
            if now - last < 2:
                log.info("[dedup] Ignoring duplicate callback for %s (%.1fs ago, hash=%s)", sender_id, now - last, body_hash)
                return
            _recent_callbacks[dedup_key] = now
            # Clean up entries older than 10s to prevent memory growth
            if len(_recent_callbacks) > 200:
                cutoff = now - 10
                stale = [k for k, v in _recent_callbacks.items() if v < cutoff]
                for k in stale:
                    del _recent_callbacks[k]

        # Extract group chat ID (0 or empty means direct message)
        from_room_id = str(first.get("fromRoomId", 0) or 0)

        # Background thread handles routing + forwarding
        threading.Thread(
            target=self._route_and_forward,
            args=(sender_id, body, dict(self.headers), from_room_id),
            daemon=True,
        ).start()

    def _route_and_forward(self, sender_id, body, headers, from_room_id="0"):
        """Route lookup -> auto-provision if needed -> forward

        Three cases:
        1. Known user (in routing table) -> forward directly
        2. Unknown user + system message (cmd!=15000) -> ignore
        3. Unknown user + user message (cmd=15000) -> send greeting + provision container + forward
        """
        # Group message: forward to GROUP_CHAT_BACKEND (dedicated group chat container), bypass sender_id routing/auto-provision
        if from_room_id != "0":
            backend = GROUP_CHAT_BACKEND or DEFAULT_BACKEND
            if backend:
                log.info("[group] room=%s sender=%s -> %s", from_room_id, sender_id, backend)
                forward(backend, body, headers)
                return
        # Parse cmd
        try:
            payload = json.loads(body)
            data_list = payload.get("data", [])
            if isinstance(data_list, dict):
                data_list = [data_list]
            first_item = data_list[0] if data_list else {}
            cmd = first_item.get("cmd")
            msg_type = str(first_item.get("msgType", first_item.get("type", "")))
        except Exception:
            cmd = None
            msg_type = ""
            first_item = {}

        # ── Claude Bridge routing (owner /c prefix messages)──
        _bridge_url = os.environ.get("CLAUDE_BRIDGE_BACKEND", "")
        _bridge_owner = os.environ.get("CLAUDE_BRIDGE_OWNER", "")
        if _bridge_url and sender_id == _bridge_owner:
            _content = first_item.get("msgData", {}).get("content", "")
            if _content.startswith("/xw ") or _content == "/xw":
                log.info("[claude-bridge] /xw bypass, routing to normal backend")
            else:
                log.info("[claude-bridge] routing to bridge")
                forward(_bridge_url, body, headers)
                return

        # -- Case 1: known user, forward directly --
        backend = ROUTING.get(sender_id)
        if backend:
            log.info("Routing sender_id=%s -> %s", sender_id, backend)
            forward(backend, body, headers)
            return

        # -- Unknown user --
        if cmd != 15000:
            # System message (contact changes etc.) -> fully ignore, no greeting, no provision
            return

        # Friend verification goes through cmd=15500, won't reach here; cmd=15000 is a real user message
        log.info("[provision] Provisioning container for %s...", sender_id)
        backend, newly_created = provision_container(sender_id)

        if not backend:
            log.warning("Provisioning failed for sender_id=%s", sender_id)
            msg_send_text(sender_id, "Sorry, the service is currently at capacity. Please try again later.")
            return

        # Provisioning complete, forward first message (with retry to ensure container receives it)
        log.info("[provision] Container ready, forwarding first message")
        for attempt in range(10):
            status, _ = forward(backend, body, headers)
            if status == 200:
                log.info("[provision] First message delivered (attempt %d)", attempt + 1)
                break
            log.warning("[provision] Forward attempt %d failed (status=%s), retrying...", attempt + 1, status)
            time.sleep(1)
        else:
            log.error("[provision] Failed to deliver first message after 10 attempts")

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# -- Self-healing at startup: scan existing containers, rebuild routes --──────────────────────

def reconcile_routes():
    """Scan agent-u* containers at startup to ensure routing table completeness"""
    status, containers = docker_api(
        "GET",
        '/containers/json?filters={"name":["agent-u"]}',
    )
    if status != 200 or not isinstance(containers, list):
        return

    updated = False
    for c in containers:
        name = c.get("Names", ["/unknown"])[0].lstrip("/")
        # Extract OWNER_ID from environment variables
        env_list = []
        # Need inspect to get env vars
        _, detail = docker_api("GET", f"/containers/{name}/json")
        if isinstance(detail, dict):
            env_list = detail.get("Config", {}).get("Env", [])

        owner_id = ""
        for e in env_list:
            if e.startswith("OWNER_ID="):
                owner_id = e.split("=", 1)[1]
                break

        if owner_id and owner_id not in ROUTING:
            backend = f"http://{name}:8080"
            ROUTING[owner_id] = backend
            log.info("[reconcile] Recovered route: %s -> %s", owner_id, backend)
            updated = True

    if updated:
        with _routing_lock:
            save_routing()

    # Reverse check: routes exist but container missing, auto-rebuild
    for sid, backend in list(ROUTING.items()):
        cname = backend.split("//")[1].split(":")[0]
        if not cname.startswith("agent-u"):
            continue
        check_status, _ = docker_api("GET", f"/containers/{cname}/json")
        if check_status == 200:
            continue
        log.warning("[reconcile] Container %s missing for sender_id=%s, rebuilding...", cname, sid)
        with _routing_lock:
            del ROUTING[sid]
            save_routing()
        result, is_new = provision_container(sid)
        if result:
            log.info("[reconcile] Rebuilt %s -> %s", sid, result)
        else:
            log.error("[reconcile] Failed to rebuild container for %s", sid)


# -- Main --────────────────────────────────────────────────────

if __name__ == "__main__":
    load_routing()
    SHARED_ENV = load_env_file(ENV_FILE_PATH)

    # Extract messaging platform credentials from .env
    for env_line in SHARED_ENV:
        if env_line.startswith("MSG_TOKEN="):
            MSG_TOKEN = env_line.split("=", 1)[1]
        elif env_line.startswith("MSG_GUID="):
            MSG_GUID = env_line.split("=", 1)[1]
    if MSG_TOKEN:
        log.info("Messaging API credentials loaded (token=%s...)", MSG_TOKEN[:8])
    else:
        log.warning("Messaging API credentials NOT found in .env, greeting disabled")

    reconcile_routes()

    port = int(os.environ.get("PORT", "8080"))
    server = ThreadedHTTPServer(("0.0.0.0", port), RouterHandler)
    log.info(
        "Router listening on :%d | %d routes | max=%d | image=%s | network=%s",
        port, len(ROUTING), MAX_CONTAINERS, APP_IMAGE, DOCKER_NETWORK,
    )
    server.serve_forever()
