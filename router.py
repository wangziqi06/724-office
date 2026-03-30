"""
Multi-Tenant Docker Router - Route by sender_id to corresponding container, auto-provision unknown users

Receives messaging platform callbacks, parses sender_id:
- Known user -> forward to corresponding container
- Unknown user -> auto-create container -> wait for health check -> forward
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
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

# -- Configuration --
ROUTING_FILE = os.environ.get("ROUTING_FILE", "/data/router/routing.json")
DEFAULT_BACKEND = os.environ.get("DEFAULT_BACKEND", "")
HOST_DATA_DIR = os.environ.get("HOST_DATA_DIR", "/data/agent/containers")
APP_IMAGE = os.environ.get("APP_IMAGE", "agent-app:latest")
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "docker_agent-net")
ENV_FILE_PATH = os.environ.get("ENV_FILE_PATH", "/data/env/.env")
MAX_CONTAINERS = int(os.environ.get("MAX_CONTAINERS", "20"))
CONTAINER_MEMORY = os.environ.get("CONTAINER_MEMORY", "256m")
PROVISION_TIMEOUT = int(os.environ.get("PROVISION_TIMEOUT", "45"))

ROUTING = {}
_routing_lock = threading.Lock()
_provision_lock = threading.Lock()  # Only provision one user at a time

# Messaging API config (loaded from .env)
MSG_API_TOKEN = ""
MSG_API_GUID = ""
MSG_API_URL = "http://api.messaging-platform.example.com/api/send"

# -- Routing Table Management --

def load_routing():
    global ROUTING
    try:
        with open(ROUTING_FILE, "r") as f:
            ROUTING = json.load(f)
        log.info("Loaded routing: %d entries", len(ROUTING))
    except Exception as e:
        log.error("Failed to load routing: %s", e)


def save_routing():
    """Write routing.json (bind mount doesn't support rename, direct overwrite)"""
    try:
        with open(ROUTING_FILE, "w") as f:
            json.dump(ROUTING, f, indent=2, ensure_ascii=False)
        log.info("Saved routing: %d entries", len(ROUTING))
    except Exception as e:
        log.error("Failed to save routing: %s", e)


# -- .env File Parsing --

def load_env_file(path):
    """Parse .env file, return KEY=VALUE list"""
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


# -- Docker Engine API (Unix Socket) --

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
    """Count existing auto-provisioned containers"""
    status, data = docker_api(
        "GET",
        '/containers/json?all=true&filters={"name":["agent-u"]}',
    )
    if status == 200 and isinstance(data, list):
        return len(data)
    return 0


# -- Auto-Provisioning --

def _parse_memory_bytes(mem_str):
    """'256m' -> 268435456"""
    mem_str = mem_str.strip().lower()
    if mem_str.endswith("g"):
        return int(float(mem_str[:-1]) * 1024 * 1024 * 1024)
    if mem_str.endswith("m"):
        return int(float(mem_str[:-1]) * 1024 * 1024)
    return int(mem_str)


def provision_container(sender_id):
    """Create container for new user, return backend_url or None. Holds lock, one at a time."""

    with _provision_lock:
        # Double-check (may have been provisioned while waiting for lock)
        if sender_id in ROUTING:
            return ROUTING[sender_id]

        # Check container count limit
        current_count = count_user_containers()
        if current_count >= MAX_CONTAINERS:
            log.warning("Container limit reached (%d/%d), rejecting sender_id=%s",
                       current_count, MAX_CONTAINERS, sender_id)
            return None

    # Below executes outside lock (time-consuming, but same sender won't reach here concurrently)
    try:
        short_id = sender_id[-8:]
        container_name = f"agent-u{short_id}"
        host_volume = f"{HOST_DATA_DIR}/{container_name}"
        backend_url = f"http://{container_name}:8080"

        log.info("[provision] Creating container %s for sender_id=%s", container_name, sender_id)

        # Build environment variables
        env_list = list(SHARED_ENV) + [
            f"OWNER_ID={sender_id}",
            "USER_NAME=New User",
            "MODEL_DEFAULT=minimax-chat",
            "AGENT_DATA=/data",
            "AGENT_CONFIG=/data/config.json",
            "MCP_SERVERS={}",
        ]

        # Create container
        create_body = {
            "Image": APP_IMAGE,
            "Env": env_list,
            "ExposedPorts": {"8080/tcp": {}},
            "HostConfig": {
                "Binds": [f"{host_volume}:/data"],
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
                "Interval": 5000000000,  # 5s in nanoseconds
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
            return None

        container_id = resp.get("Id", "")
        log.info("[provision] Created container %s (%s)", container_name, container_id[:12])

        # Start container
        start_status, _ = docker_api("POST", f"/containers/{container_id}/start")
        if start_status not in (200, 204):
            log.error("[provision] Start failed: %s", start_status)
            # Cleanup
            docker_api("DELETE", f"/containers/{container_id}?force=true")
            return None

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

        log.info("[provision] %s ready, route: %s -> %s", container_name, sender_id, backend_url)
        return backend_url

    except Exception as e:
        log.error("[provision] Unexpected error: %s", e, exc_info=True)
        return None


# -- Messaging API (send messages directly) --

def send_text(to_id, content):
    """Send message to user via messaging platform API"""
    if not MSG_API_TOKEN or not MSG_API_GUID:
        log.warning("[messaging] No token/guid configured, cannot send message")
        return False
    body = json.dumps({
        "method": "/msg/sendText",
        "params": {"guid": MSG_API_GUID, "toId": str(to_id), "content": content},
    }).encode("utf-8")
    req = urllib.request.Request(
        MSG_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-API-TOKEN": MSG_API_TOKEN,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 0:
                log.info("[messaging] Greeting sent to %s", to_id)
                return True
            log.error("[messaging] Send failed: %s", result)
    except Exception as e:
        log.error("[messaging] Send error: %s", e)
    return False


GREETING_MESSAGE = (
    "Hello! I'm your AI assistant.\n"
    "Nice to meet you! Let's chat a bit so I can learn how to help you best."
)


# -- HTTP Forwarding --

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


# -- HTTP Handler --

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
            load_routing()
            body = json.dumps({"status": "reloaded", "routes": len(ROUTING)}).encode()
            self._respond(200, body)

        elif self.path == "/routes":
            # View current routing table (debug)
            body = json.dumps(ROUTING, indent=2, ensure_ascii=False).encode()
            self._respond(200, body)

        else:
            self._respond(200, b"ok")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        # /api/chat -- voice channel, sync proxy to DEFAULT_BACKEND
        if self.path == "/api/chat":
            if not DEFAULT_BACKEND:
                self._respond(503, b"")
                return
            status, resp_body = forward(DEFAULT_BACKEND + "/api/chat", body, dict(self.headers))
            self._respond(status, resp_body)
            return

        # Non-callback paths, passthrough to default backend
        if self.path not in ("/", ""):
            if DEFAULT_BACKEND:
                status, resp_body = forward(DEFAULT_BACKEND + self.path, body, dict(self.headers))
            self._respond(200, b"")
            return

        # Messaging callback -- return 200 immediately, process in background
        self._respond(200, b"")

        if not body:
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            log.warning("Invalid JSON in callback")
            return

        # Compatible with data as dict or list
        data_list = payload.get("data", [])
        if isinstance(data_list, dict):
            data_list = [data_list]
        if not isinstance(data_list, list) or not data_list:
            return

        # Extract sender_id (compatible with various callback formats)
        first = data_list[0]
        if not isinstance(first, dict):
            return
        sender_id = str(first.get("senderId", "") or first.get("userId", "") or "")
        if not sender_id:
            return

        # Skip messages sent by self
        user_id = str(first.get("userId", ""))
        if sender_id == user_id and first.get("cmd") == 15000:
            return

        # Background thread handles routing + forwarding
        threading.Thread(
            target=self._route_and_forward,
            args=(sender_id, body, dict(self.headers)),
            daemon=True,
        ).start()

    def _route_and_forward(self, sender_id, body, headers):
        """Route lookup -> auto-provision if needed -> forward"""
        backend = ROUTING.get(sender_id)
        is_new_user = backend is None

        if is_new_user:
            log.info("Unknown sender_id=%s, auto-provisioning...", sender_id)
            backend = provision_container(sender_id)

            if not backend:
                log.warning("Provisioning failed for sender_id=%s", sender_id)
                send_text(sender_id, "Sorry, the service is currently at capacity. Please try again later.")
                return

            # New user provisioned -> send greeting
            log.info("[greeting] Sending welcome to new user %s", sender_id)
            send_text(sender_id, GREETING_MESSAGE)
            return

        log.info("Routing sender_id=%s -> %s", sender_id, backend)
        forward(backend, body, headers)

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# -- Startup Self-Heal: scan existing containers, rebuild routing --

def reconcile_routes():
    """On startup, scan agent-u* containers to ensure routing table is complete"""
    status, containers = docker_api(
        "GET",
        '/containers/json?filters={"name":["agent-u"]}',
    )
    if status != 200 or not isinstance(containers, list):
        return

    updated = False
    for c in containers:
        name = c.get("Names", ["/unknown"])[0].lstrip("/")
        env_list = []
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


# -- Main --

if __name__ == "__main__":
    load_routing()
    SHARED_ENV = load_env_file(ENV_FILE_PATH)

    # Extract messaging API credentials from .env
    for env_line in SHARED_ENV:
        if env_line.startswith("MSG_API_TOKEN="):
            MSG_API_TOKEN = env_line.split("=", 1)[1]
        elif env_line.startswith("MSG_API_GUID="):
            MSG_API_GUID = env_line.split("=", 1)[1]
    if MSG_API_TOKEN:
        log.info("Messaging API credentials loaded (token=%s...)", MSG_API_TOKEN[:8])
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
