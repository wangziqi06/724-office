#!/usr/bin/env python3
"""
Agent code audit script — 11 automated checks

Pure stdlib, < 60s, < 50MB RAM, outputs structured JSON.
Usage: python3 audit.py [--checks syntax,config_schema,...]
"""

import ast
import json
import os
import re
import stat
import subprocess
import sys
import time

BASE_DIR = os.environ.get("AGENT_DATA", os.path.dirname(os.path.abspath(__file__)))
CODE_DIR = os.path.dirname(os.path.abspath(__file__))  # /app in container, same as BASE_DIR on host
WORKSPACE = os.path.join(BASE_DIR, "workspace")
AUDIT_DIR = os.path.join(WORKSPACE, "audit")

# Last audit result cache path (used for disk growth comparison)
_LAST_AUDIT = os.path.join(AUDIT_DIR, ".last_audit.json")


def _find_py_files():
    """Find all .py files (excluding node_modules, __pycache__)"""
    result = []
    for root, dirs, files in os.walk(CODE_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git", "memory_db")]
        for f in files:
            if f.endswith(".py"):
                result.append(os.path.join(root, f))
    return result


# ============================================================
#  Check 1: syntax — AST syntax check
# ============================================================
def check_syntax():
    details = []
    for path in _find_py_files():
        rel = os.path.relpath(path, CODE_DIR)
        try:
            with open(path, "r", encoding="utf-8") as f:
                ast.parse(f.read(), filename=rel)
        except SyntaxError as e:
            details.append({"file": rel, "line": e.lineno, "msg": str(e.msg)})
    return {"status": "fail" if details else "pass", "details": details}


# ============================================================
#  Check 2: config_schema — config.json structural integrity
# ============================================================
def check_config_schema():
    config_path = os.path.join(BASE_DIR, "config.json")
    details = []
    if not os.path.exists(config_path):
        return {"status": "fail", "details": [{"msg": "config.json does not exist"}]}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        return {"status": "fail", "details": [{"msg": "config.json JSON parse failed: %s" % e}]}

    # Required top-level keys
    required_keys = ["models", "messaging", "owner_ids", "workspace", "port"]
    for k in required_keys:
        if k not in cfg:
            details.append({"msg": "Missing required key: %s" % k})
        elif not cfg[k]:
            details.append({"msg": "Key is empty: %s" % k})

    # models structure
    models = cfg.get("models", {})
    if "default" not in models:
        details.append({"msg": "models.default not set"})
    providers = models.get("providers", {})
    for name, p in providers.items():
        for field in ("api_base", "api_key", "model"):
            if not p.get(field):
                details.append({"msg": "models.providers.%s.%s missing or empty" % (name, field)})

    has_critical = any("Missing" in d["msg"] or "does not exist" in d["msg"] for d in details)
    status = "fail" if has_critical else ("warn" if details else "pass")
    return {"status": status, "details": details}


# ============================================================
#  Check 3: permissions — file permission check
# ============================================================
def check_permissions():
    details = []
    checks = [
        ("config.json", 0o600, "should be 0600 (contains API keys)"),
        ("sessions", 0o700, "should be 0700"),
    ]
    for name, expected, hint in checks:
        path = os.path.join(BASE_DIR, name)
        if not os.path.exists(path):
            continue
        actual = stat.S_IMODE(os.stat(path).st_mode)
        if actual != expected:
            details.append({
                "file": name,
                "actual": "0%o" % actual,
                "expected": "0%o" % expected,
                "msg": "%s permission 0%o, %s" % (name, actual, hint),
            })
    return {"status": "warn" if details else "pass", "details": details}


# ============================================================
#  Check 4: tool_registry — tool registration and doc consistency
# ============================================================
def check_tool_registry():
    details = []
    # AST extract tool names registered via @tool
    tools_path = os.path.join(CODE_DIR, "tools.py")
    registered = set()
    try:
        with open(tools_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "tool":
                        if dec.args and isinstance(dec.args[0], ast.Constant):
                            registered.add(dec.args[0].value)
    except Exception as e:
        details.append({"msg": "Unable to parse tools.py: %s" % e})
        return {"status": "fail", "details": details}

    # Extract documented tools from AGENT.md (match "- tool_name -- " format)
    agent_path = os.path.join(WORKSPACE, "AGENT.md")
    documented = set()
    if os.path.exists(agent_path):
        with open(agent_path, "r", encoding="utf-8") as f:
            content = f.read()
        for m in re.finditer(r"^- ([a-z_][a-z0-9_]*)\s*—", content, re.MULTILINE):
            documented.add(m.group(1))

    # Compare (exclude MCP dynamic tools)
    only_code = registered - documented
    only_doc = documented - registered
    # Filter MCP-prefixed tools
    builtin_only_doc = {t for t in only_doc if "__" not in t}

    if only_code:
        details.append({"msg": "Registered in code but not documented in AGENT.md: %s" % ", ".join(sorted(only_code))})
    if builtin_only_doc:
        details.append({"msg": "Documented in AGENT.md but not registered in code: %s" % ", ".join(sorted(builtin_only_doc))})

    return {"status": "warn" if details else "pass", "details": details}


# ============================================================
#  Check 5: session_health — session file health
# ============================================================
def check_session_health():
    details = []
    sessions_dir = os.path.join(BASE_DIR, "sessions")
    if not os.path.isdir(sessions_dir):
        return {"status": "pass", "details": [{"msg": "sessions/ directory does not exist"}]}

    total_size = 0
    for fname in os.listdir(sessions_dir):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(sessions_dir, fname)
        size = os.path.getsize(path)
        total_size += size

        if size > 500 * 1024:
            details.append({"file": fname, "size_kb": size // 1024,
                            "msg": "%s is %dKB, consider cleanup" % (fname, size // 1024)})

        # Check orphan messages
        try:
            with open(path, "r", encoding="utf-8") as f:
                msgs = json.load(f)
            if msgs and isinstance(msgs, list) and len(msgs) > 0:
                first = msgs[0]
                if isinstance(first, dict) and first.get("role") not in ("user", "system"):
                    details.append({"file": fname,
                                    "msg": "%s starts with orphan %s message" % (fname, first.get("role", "?"))})
        except (json.JSONDecodeError, Exception):
            details.append({"file": fname, "msg": "%s JSON parse failed" % fname})

    if total_size > 10 * 1024 * 1024:
        details.append({"msg": "sessions/ total size %dMB, consider cleaning old sessions" % (total_size // (1024 * 1024))})

    return {"status": "warn" if details else "pass", "details": details}


# ============================================================
#  Check 6: disk_usage — disk usage
# ============================================================
def check_disk_usage():
    details = []
    dirs_to_check = [
        ("sessions/", os.path.join(BASE_DIR, "sessions")),
        ("memory_db/", os.path.join(BASE_DIR, "memory_db")),
        ("workspace/", WORKSPACE),
        ("mcp_servers/", os.path.join(BASE_DIR, "mcp_servers")),
    ]
    for label, path in dirs_to_check:
        if not os.path.isdir(path):
            details.append({"dir": label, "size_mb": 0})
            continue
        total = 0
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d != "node_modules"]
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        mb = round(total / (1024 * 1024), 1)
        details.append({"dir": label, "size_mb": mb})

    # Compare with last audit
    if os.path.exists(_LAST_AUDIT):
        try:
            with open(_LAST_AUDIT, "r") as f:
                last = json.load(f)
            last_disk = {}
            for d in last.get("checks", {}).get("disk_usage", {}).get("details", []):
                last_disk[d["dir"]] = d.get("size_mb", 0)
            for d in details:
                prev = last_disk.get(d["dir"], 0)
                if prev > 0:
                    growth = d["size_mb"] - prev
                    if abs(growth) > 0.1:
                        d["growth_mb"] = round(growth, 1)
        except Exception:
            pass

    return {"status": "pass", "details": details}


# ============================================================
#  Check 7: process_health — process/service status
# ============================================================
def check_process_health():
    details = []
    # Container environment: no systemd, check if main process is alive
    if os.path.exists("/.dockerenv"):
        try:
            with open("/proc/1/cmdline", "r") as f:
                cmdline = f.read().replace(chr(0), " ").strip()
            details.append({"msg": "Container mode, main process: %s" % cmdline[:80]})
            return {"status": "pass", "details": details}
        except Exception:
            return {"status": "pass", "details": [{"msg": "Container mode, process running"}]}
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "agent"],
            capture_output=True, text=True, timeout=5,
        )
        status_text = result.stdout.strip()
        if status_text != "active":
            details.append({"msg": "agent service status: %s" % status_text})
    except Exception as e:
        details.append({"msg": "Unable to check service status: %s" % e})

    return {"status": "fail" if details else "pass", "details": details}


# ============================================================
#  Check 8: git_status — Git repository status
# ============================================================
def check_git_status():
    details = []
    git_dir = os.path.join(BASE_DIR, ".git")
    if not os.path.isdir(git_dir):
        return {"status": "warn", "details": [{"msg": "Git repository not initialized"}]}

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=BASE_DIR, timeout=10,
        )
        lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        if lines:
            details.append({"msg": "%d uncommitted changes" % len(lines), "files": lines[:10]})
    except Exception as e:
        details.append({"msg": "git status failed: %s" % e})

    return {"status": "warn" if details else "pass", "details": details}


# ============================================================
#  Check 9: stale_files — stale/orphan files
# ============================================================
def check_stale_files():
    details = []
    now = time.time()
    seven_days = 7 * 24 * 3600

    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git", "memory_db")]
        for f in files:
            path = os.path.join(root, f)
            rel = os.path.relpath(path, BASE_DIR)
            if f.endswith(".bak"):
                age = now - os.path.getmtime(path)
                if age > seven_days:
                    details.append({"file": rel, "age_days": int(age / 86400),
                                    "msg": "%s has existed for %d days" % (rel, int(age / 86400))})
            if f.endswith(".tmp"):
                details.append({"file": rel, "msg": "%s is a temp file, should be cleaned up" % rel})

    return {"status": "warn" if details else "pass", "details": details}


# ============================================================
#  Check 10: anti_patterns — known anti-pattern scan
# ============================================================
def check_anti_patterns():
    details = []
    patterns = [
        (r"except\s*:", "bare except (should specify exception type)"),
        (r"from\s+\w+\s+import\s+\*", "import * (should use explicit imports)"),
        (r"(?<!['\"\w])print\s*\(", "print() (should use log)"),
    ]
    core_files = ["xiaowang.py", "llm.py", "tools.py", "scheduler.py", "messaging.py", "memory.py", "mcp_client.py"]
    for fname in core_files:
        path = os.path.join(CODE_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pat, desc in patterns:
                if re.search(pat, line):
                    details.append({"file": fname, "line": i, "pattern": desc, "code": stripped[:80]})

    return {"status": "warn" if details else "pass", "details": details}


# ============================================================
#  Check 11: jobs_health — scheduled jobs health
# ============================================================
def check_jobs_health():
    details = []
    jobs_path = os.path.join(BASE_DIR, "jobs.json")
    if not os.path.exists(jobs_path):
        return {"status": "pass", "details": [{"msg": "jobs.json does not exist"}]}

    try:
        with open(jobs_path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
    except json.JSONDecodeError as e:
        return {"status": "fail", "details": [{"msg": "jobs.json parse failed: %s" % e}]}

    now = time.time()
    for job in jobs:
        name = job.get("name", "?")
        cron = job.get("cron_expr")
        last_run = job.get("last_run")

        # Basic cron expression format validation
        if cron:
            parts = cron.strip().split()
            if len(parts) != 5:
                details.append({"job": name, "msg": "Cron expression format error: %s (expected 5 fields)" % cron})

        # Cron job not run for 25+ hours = possibly stuck
        if cron and last_run:
            age_hours = (now - last_run) / 3600
            if age_hours > 25:
                details.append({"job": name, "msg": "Last run was %.1f hours ago, possibly stuck" % age_hours})

    return {"status": "warn" if details else "pass", "details": details}


# ============================================================
#  Main entry
# ============================================================
ALL_CHECKS = {
    "syntax": check_syntax,
    "config_schema": check_config_schema,
    "permissions": check_permissions,
    "tool_registry": check_tool_registry,
    "session_health": check_session_health,
    "disk_usage": check_disk_usage,
    "process_health": check_process_health,
    "git_status": check_git_status,
    "stale_files": check_stale_files,
    "anti_patterns": check_anti_patterns,
    "jobs_health": check_jobs_health,
}


def run_audit(checks=None):
    """Run audit and return structured results"""
    if checks:
        to_run = {k: ALL_CHECKS[k] for k in checks if k in ALL_CHECKS}
    else:
        to_run = ALL_CHECKS

    t0 = time.time()
    results = {}
    summary = {"total_checks": len(to_run), "pass": 0, "warn": 0, "fail": 0}

    for name, fn in to_run.items():
        try:
            result = fn()
        except Exception as e:
            result = {"status": "fail", "details": [{"msg": "Check exception: %s" % e}]}
        results[name] = result
        summary[result["status"]] = summary.get(result["status"], 0) + 1

    duration_ms = int((time.time() - t0) * 1000)

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": duration_ms,
        "checks": results,
        "summary": summary,
    }

    # Save as last audit result (for disk_usage growth comparison)
    os.makedirs(AUDIT_DIR, exist_ok=True)
    try:
        with open(_LAST_AUDIT, "w") as f:
            json.dump(report, f, ensure_ascii=False)
    except Exception:
        pass

    return report


if __name__ == "__main__":
    checks = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg.startswith("--checks="):
            checks = arg.split("=", 1)[1].split(",")
        elif arg == "--checks" and i < len(sys.argv) - 1:
            checks = sys.argv[i + 1].split(",")

    report = run_audit(checks)
    print(json.dumps(report, ensure_ascii=False, indent=2))
