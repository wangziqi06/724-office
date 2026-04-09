"""
Admin / diagnostic / plugin / MCP tools
"""

import json
import os
import subprocess
import time

from tools_base import tool, _registry, log


# ============================================================
#  Plugin system
# ============================================================

_plugins_dir = os.path.join(os.environ.get("AGENT_DATA", os.path.dirname(os.path.abspath(__file__))), "plugins")

def _exec_plugin(code, source="<plugin>"):
    """Execute plugin code in a controlled environment. Plugins can use @tool to register tools.
    Restricted builtins: removes eval/exec/compile/__import__ and other dangerous functions."""
    import builtins as _bi
    safe_builtins = {k: getattr(_bi, k) for k in dir(_bi)
                     if k not in ("eval", "exec", "compile", "__import__",
                                  "globals", "locals", "breakpoint", "exit", "quit")}
    # Provide safe import: only allow whitelisted modules
    _IMPORT_WHITELIST = {"json", "os", "os.path", "re", "math", "time", "datetime",
                         "urllib.request", "urllib.parse", "hashlib", "base64",
                         "imaplib", "email", "email.header", "email.utils", "email.mime.text",
                         "email.mime.multipart", "smtplib"}
    def _safe_import(name, *a, **kw):
        if name not in _IMPORT_WHITELIST:
            raise ImportError("Plugin import not allowed: %s" % name)
        return _bi.__import__(name, *a, **kw)
    safe_builtins["__import__"] = _safe_import
    exec(compile(code, source, "exec"), {
        "__builtins__": safe_builtins,
        "tool": tool,
        "log": log,
    })


def _load_plugins():
    """Scan plugins/ directory at startup and load all custom tools"""
    if not os.path.isdir(_plugins_dir):
        return
    loaded = 0
    for fname in sorted(os.listdir(_plugins_dir)):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(_plugins_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                code = f.read()
            _exec_plugin(code, fname)
            loaded += 1
        except Exception as e:
            log.error("[plugins] failed to load %s: %s" % (fname, e))
    if loaded:
        log.info("[plugins] loaded %d custom tools" % loaded)



# --- Self-check tool ---

@tool("self_check", "System self-check: collect today's conversation stats, system health, error logs, scheduled task status, etc. Used for generating daily self-check reports.", {})
def tool_self_check(args, ctx):
    from datetime import datetime, timezone, timedelta

    CST = timezone(timedelta(hours=8))
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    report = []

    # 1. Today's conversations (from diary file, daily_digest already does incremental stats)
    report.append("== Today's Conversations (%s) ==" % today)
    diary_path = os.path.join(ctx["workspace"], "memory", "%s.md" % today)
    if os.path.exists(diary_path):
        report.append("See today's diary memory/%s.md" % today)
    else:
        report.append("Today's diary not yet generated (auto-created at 23:00)")

    # 2. Today's error logs
    report.append("\n== Error Logs ==")
    if os.path.exists("/.dockerenv"):
        report.append("Container environment, check error logs via docker logs")
    else:
        try:
            err_cmd = 'journalctl -u agent --since today --no-pager | grep -ci "error"'
            err_result = subprocess.run(["bash", "-c", err_cmd], capture_output=True, text=True, timeout=10)
            err_count = err_result.stdout.strip() or "0"
            report.append("Today's errors: %s" % err_count)
            if int(err_count) > 0:
                last_cmd = 'journalctl -u agent --since today --no-pager | grep -i "error" | tail -5'
                last_errs = subprocess.run(["bash", "-c", last_cmd], capture_output=True, text=True, timeout=10).stdout.strip()
                if last_errs:
                    report.append("Last 5:\n" + last_errs)
        except Exception as e:
            report.append("Read failed: %s" % e)

    # 3. Service uptime
    report.append("\n== System Status ==")
    if os.path.exists("/.dockerenv"):
        report.append("Container mode, process PID 1 running")
    else:
        try:
            uptime_result = subprocess.run(
                ["systemctl", "show", "agent", "--property=ActiveEnterTimestamp", "--value"],
                capture_output=True, text=True, timeout=5
            )
            report.append("Service start time: %s" % uptime_result.stdout.strip())
        except Exception:
            pass

    # 4. Memory and disk
    try:
        mem = subprocess.run(["bash", "-c", "free -h | grep Mem"], capture_output=True, text=True, timeout=5).stdout.strip()
        disk = subprocess.run(["bash", "-c", "df -h /data | tail -1"], capture_output=True, text=True, timeout=5).stdout.strip()
        report.append("Memory: %s" % mem)
        report.append("Disk: %s" % disk)
    except Exception:
        pass

    # 5. Scheduled task status
    try:
        jobs_file = os.path.join(os.path.dirname(ctx["workspace"]), "jobs.json")
        with open(jobs_file, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        report.append("\n== Scheduled Tasks (%d total) ==" % len(jobs))
        for j in jobs:
            cron = j.get("cron_expr", "")
            last = j.get("last_run")
            last_str = datetime.fromtimestamp(last, CST).strftime("%H:%M") if last else "never"
            report.append("  - %s (%s) last: %s" % (j["name"], cron, last_str))
    except Exception as e:
        report.append("\n== Scheduled Tasks ==\nRead failed: %s" % e)

    # 6. Voice ASR health
    report.append("\n== Voice ASR ==")
    voice_dir = os.path.join(ctx["workspace"], "files")
    voice_total = 0
    voice_today = 0
    if os.path.isdir(voice_dir):
        for root, dirs, files in os.walk(voice_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                # Voice files are typically .silk, .amr, .mp3, .wav
                if any(fname.endswith(ext) for ext in ('.silk', '.amr', '.mp3', '.wav', '.slk')):
                    voice_total += 1
                    try:
                        mtime = datetime.fromtimestamp(os.path.getmtime(fpath), CST)
                        if mtime.strftime("%Y-%m-%d") == today:
                            voice_today += 1
                    except Exception:
                        pass
    # Estimate ASR success rate from sessions (check voice-to-text markers)
    asr_success = 0
    asr_fail = 0
    sessions_dir_asr = os.path.join(os.path.dirname(ctx["workspace"]), "sessions")
    if os.path.isdir(sessions_dir_asr):
        for fname in os.listdir(sessions_dir_asr):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(sessions_dir_asr, fname)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), CST)
                if mtime.strftime("%Y-%m-%d") != today:
                    continue
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                asr_success += content.count("[voice-to-text]")
                asr_fail += content.count("voice recognition failed")
            except Exception:
                pass
    asr_total_attempts = asr_success + asr_fail
    if asr_total_attempts > 0:
        rate = asr_success / asr_total_attempts * 100
        report.append("Today's voice: %d succeeded, %d failed, success rate %.0f%%" % (asr_success, asr_fail, rate))
    else:
        report.append("Today's voice: no ASR records")
    report.append("Voice files total: %d (today %d)" % (voice_total, voice_today))

    # 7. Memory file status
    report.append("")
    memory_dir = os.path.join(ctx["workspace"], "memory")
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    today_log = os.path.join(memory_dir, "%s.md" % today)
    report.append("\n== Memory Files ==")
    if os.path.exists(memory_md):
        mtime = datetime.fromtimestamp(os.path.getmtime(memory_md), CST)
        size_kb = os.path.getsize(memory_md) / 1024
        report.append("MEMORY.md: %.1fKB, last updated %s" % (size_kb, mtime.strftime("%Y-%m-%d %H:%M")))
    if os.path.exists(today_log):
        size_kb = os.path.getsize(today_log) / 1024
        report.append("Today's log: %.1fKB" % size_kb)
    else:
        report.append("Today's log: not created")

    return "\n".join(report)

# --- Diagnostic tool ---

@tool("diagnose", "Diagnose system issues. Check session file health, MCP server connection status, recent error log details. "
      "Call this tool first when encountering 400 errors, MCP tool unavailability, or any anomalies.",
      {"target": {"type": "string", "description": "Diagnostic target: 'session' check session files, 'mcp' check MCP servers, 'errors' view recent error details, 'all' check everything"}},
      ["target"])
def tool_diagnose(args, ctx):
    target = args.get("target", "all")
    report = []

    if target in ("session", "all"):
        report.append("== Session File Health Check ==")
        sessions_dir = os.path.join(os.path.dirname(ctx["workspace"]), "sessions")
        if os.path.isdir(sessions_dir):
            for fname in sorted(os.listdir(sessions_dir)):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(sessions_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        msgs = json.load(f)
                    issues = []
                    if msgs and msgs[0].get("role") == "tool":
                        issues.append("Starts with orphan tool message (will cause LLM 400)")
                    if msgs and msgs[0].get("role") == "assistant" and msgs[0].get("tool_calls"):
                        issues.append("Starts with assistant with tool_calls (missing tool results, will cause 400)")
                    # Check if tool messages have matching tool_call_id
                    tc_ids = set()
                    for m in msgs:
                        for tc in m.get("tool_calls", []):
                            tc_ids.add(tc.get("id", ""))
                    orphan_tools = 0
                    for m in msgs:
                        if m.get("role") == "tool" and m.get("tool_call_id") not in tc_ids:
                            orphan_tools += 1
                    if orphan_tools:
                        issues.append("%d tool messages without matching tool_call_id" % orphan_tools)
                    total_bytes = sum(len(json.dumps(m)) for m in msgs)
                    status = "issues found" if issues else "OK"
                    report.append("  %s: %d messages, %d bytes, %s" % (fname, len(msgs), total_bytes, status))
                    for issue in issues:
                        report.append("    WARNING: %s" % issue)
                        report.append("    Fix: use edit_file or write_file to clean the session file, or delete it to let the system rebuild")
                except Exception as e:
                    report.append("  %s: read failed (%s)" % (fname, e))
        else:
            report.append("  sessions directory does not exist")

    if target in ("mcp", "all"):
        report.append("\n== MCP Server Status ==")
        try:
            import mcp_client
            config_path = os.environ.get("AGENT_CONFIG",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            configured = config.get("mcp_servers", {})
            connected = set(mcp_client._servers.keys())

            for name, srv_config in configured.items():
                if name in connected:
                    server = mcp_client._servers[name]
                    tools_count = len(server._tools)
                    alive = "process running" if (server._proc and server._proc.poll() is None) else "no process (HTTP)" if server.transport == "http" else "process exited"
                    report.append("  %s: connected, %d tools, %s" % (name, tools_count, alive))
                else:
                    transport = srv_config.get("transport", "stdio")
                    cmd = srv_config.get("command", "")
                    srv_args = srv_config.get("args", [])
                    report.append("  %s: NOT connected!" % name)
                    report.append("    Config: %s %s %s" % (transport, cmd, " ".join(str(a) for a in srv_args)))
                    # Provide troubleshooting suggestions
                    if transport == "stdio":
                        report.append("    Troubleshooting steps:")
                        report.append("      1. Run exec: which %s  -- verify command exists" % cmd)
                        report.append("      2. Run exec: timeout 5 %s %s 2>&1 | head -5  -- check startup errors" % (cmd, " ".join(str(a) for a in srv_args)))
                        if "playwright" in " ".join(str(a) for a in srv_args).lower():
                            report.append("      3. Playwright needs browser: exec run npx playwright install chromium")
                            report.append("      4. May need system deps: exec run npx playwright install-deps")
                        if "npx" in cmd:
                            report.append("      3. npm package may not exist: exec run npm view %s version to verify" % (srv_args[1] if len(srv_args) > 1 else "?"))

            if not configured:
                report.append("  No mcp_servers configured in config.json")
        except ImportError:
            report.append("  mcp_client module not loaded")
        except Exception as e:
            report.append("  Check failed: %s" % e)

    if target in ("errors", "all"):
        report.append("\n== Recent Error Details ==")
        try:
            # Last 10 ERROR-level log entries with context
            cmd = 'journalctl -u agent --no-pager -n 500 --since "1 hour ago" | grep -B 1 -A 2 "ERROR\\|400\\|Bad Request" | tail -30'
            result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=10)
            errors = result.stdout.strip()
            if errors:
                report.append(errors)
            else:
                report.append("  No errors in the last hour")
        except Exception as e:
            report.append("  Read failed: %s" % e)

    return "\n".join(report)

# --- Task history query ---

@tool("task_history", "Query scheduled task execution history. Extracts tasks executed today and their sent content from the scheduler session. "
      "Use when user asks whether a scheduled task ran and what it sent.",
      {"name": {"type": "string", "description": "Task name keyword (optional, returns all if empty)"}})
def tool_task_history(args, ctx):
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))

    name_filter = args.get("name", "")
    sessions_dir = os.path.join(os.path.dirname(ctx["workspace"]), "sessions")
    owner_id = ctx.get("owner_id", "")
    sched_name = f"scheduler_{owner_id}" if owner_id else "scheduler"
    sched_path = os.path.join(sessions_dir, f"{sched_name}.json")

    results = []

    # 1. Get task list and last_run from jobs.json
    jobs_file = os.path.join(os.path.dirname(ctx["workspace"]), "jobs.json")
    jobs_info = {}
    try:
        with open(jobs_file, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        for j in jobs:
            jname = j.get("name", "")
            lr = j.get("last_run")
            lr_str = datetime.fromtimestamp(lr, CST).strftime("%Y-%m-%d %H:%M") if lr else "never executed"
            jobs_info[jname] = lr_str
    except Exception:
        pass

    if jobs_info:
        results.append("== Scheduled Task List ==")
        for jname, lr_str in jobs_info.items():
            if name_filter and name_filter not in jname:
                continue
            results.append("  %s -- last run: %s" % (jname, lr_str))

    # 2. Extract execution history from scheduler session (task triggers + sent content)
    if not os.path.exists(sched_path):
        results.append("\nScheduler session file does not exist, no execution history.")
        return "\n".join(results)

    try:
        with open(sched_path, "r", encoding="utf-8") as f:
            msgs = json.load(f)
    except Exception as e:
        results.append("\nScheduler session read failed: %s" % e)
        return "\n".join(results)

    # Extract each task round: user message (trigger instruction) + assistant's message tool call (sent content)
    history = []  # [(trigger_text, sent_content)]
    current_trigger = ""
    for msg in msgs:
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if isinstance(c, str):
                current_trigger = c[:100]
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                if fn.get("name") == "message":
                    try:
                        fargs = json.loads(fn.get("arguments", "{}"))
                        sent = fargs.get("content", "")
                        if sent:
                            history.append((current_trigger, sent[:300]))
                    except (json.JSONDecodeError, KeyError):
                        pass

    if name_filter:
        history = [(t, s) for t, s in history if name_filter in t]

    results.append("\n== Execution History (scheduler session, %d send records) ==" % len(history))
    if not history:
        results.append("  No matching execution records found.")
    else:
        # Show most recent 10
        for i, (trigger, sent) in enumerate(history[-10:], 1):
            results.append("  %d. Trigger: %s" % (i, trigger[:60]))
            results.append("     Sent: %s" % sent[:200])

    return "\n".join(results)

# --- Plugin management tools (self-extension) ---

@tool("create_tool", "Create a new custom tool plugin. Code is saved to plugins/ directory and hot-loaded immediately, also auto-loaded on restart. "
      "Use @tool decorator to register tools in the code. Any standard library can be imported. "
      "Injected variables: tool (decorator), log (logger). "
      "Example code:\nimport urllib.request\nimport json\n\n@tool(\"my_tool\", \"Tool description\", {\"param\": {\"type\": \"string\", \"description\": \"Parameter description\"}}, [\"param\"])\ndef tool_my_tool(args, ctx):\n    return \"result\"",
      {"name": {"type": "string", "description": "Tool name (used as filename, e.g. 'weather' generates plugins/weather.py)"},
       "code": {"type": "string", "description": "Complete Python plugin code including imports, @tool decorator, and function definition"}},
      ["name", "code"])
def tool_create_tool(args, ctx):
    name = args["name"]
    code = args["code"]

    # Validate tool name
    if not name.replace("_", "").isalnum():
        return "[error] Tool name can only contain letters, digits, and underscores"

    # Protect built-in tools: only allow overwriting if it already exists in plugins/
    plugin_path = os.path.join(_plugins_dir, "%s.py" % name)
    if name in _registry and not os.path.exists(plugin_path):
        return "[error] Cannot overwrite built-in tool '%s'" % name

    # Try loading first to validate code executes correctly
    try:
        _exec_plugin(code, "%s.py" % name)
    except Exception as e:
        return "[error] Code execution failed: %s" % e

    # Validation passed, persist to disk
    os.makedirs(_plugins_dir, exist_ok=True)
    with open(plugin_path, "w", encoding="utf-8") as f:
        f.write(code)

    log.info("[plugins] created: %s.py" % name)
    return "Created and loaded custom tool '%s', saved to plugins/%s.py" % (name, name)


@tool("list_custom_tools", "List all custom tool plugins (tools in plugins/ directory)", {})
def tool_list_custom_tools(args, ctx):
    if not os.path.isdir(_plugins_dir):
        return "No custom tools yet. plugins/ directory does not exist."
    plugins = [f for f in sorted(os.listdir(_plugins_dir)) if f.endswith(".py")]
    if not plugins:
        return "No custom tools yet."
    lines = ["Custom tools (%d total):" % len(plugins)]
    for fname in plugins:
        tool_name = fname[:-3]
        fpath = os.path.join(_plugins_dir, fname)
        size = os.path.getsize(fpath)
        status = "loaded" if tool_name in _registry else "not loaded"
        lines.append("  - %s (%s, %d bytes)" % (tool_name, status, size))
    return "\n".join(lines)


@tool("remove_tool", "Remove a custom tool plugin. Can only remove custom tools in plugins/, not built-in tools.",
      {"name": {"type": "string", "description": "Name of the tool to remove"}},
      ["name"])
def tool_remove_tool(args, ctx):
    name = args["name"]
    plugin_path = os.path.join(_plugins_dir, "%s.py" % name)

    if not os.path.exists(plugin_path):
        return "[error] Custom tool '%s' does not exist (can only remove tools in plugins/)" % name

    os.remove(plugin_path)
    # Remove from registry
    if name in _registry:
        del _registry[name]
    log.info("[plugins] removed: %s" % name)
    return "Removed custom tool '%s'" % name








# --- Daily digest tool ---

@tool("daily_digest", "Generate today's diary material: summarize today's conversations, received files, task execution status, new memories, etc. as structured data. "
      "Used for the daily diary scheduled task. Returns structured summary for you to write into a natural language diary.", {})
def tool_daily_digest(args, ctx):
    import memory as _mem
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))

    workspace = ctx.get("workspace", "")
    sessions_dir = os.path.join(os.path.dirname(workspace), "sessions")
    owner_id = ctx.get("owner_id", "")

    digest = _mem.daily_digest(workspace, sessions_dir, owner_id)

    lines = ["== Today's Diary Material (%s) ==" % digest["date"]]

    # Conversations
    convos = digest.get("conversations", [])
    if convos:
        total_user = sum(c["user_messages"] for c in convos)
        total_asst = sum(c["assistant_messages"] for c in convos)
        lines.append("\nConversation records (%d sessions, %d user messages, %d replies):" % (len(convos), total_user, total_asst))
        for c in convos:
            lines.append("  %s: %d user messages" % (c["session"], c["user_messages"]))
            for t in c.get("recent_topics", [])[:3]:
                lines.append("    - %s" % t[:80])
    else:
        lines.append("\nConversation records: no conversations today")

    # Files
    files = digest.get("files", [])
    if files:
        lines.append("\nReceived files (%d):" % len(files))
        for f in files:
            size_kb = f["size"] / 1024
            lines.append("  - %s (%s, %.0fKB) -> %s" % (f["filename"], f["type"], size_kb, f.get("path", "")))
    else:
        lines.append("\nReceived files: none")

    # Tasks
    tasks = digest.get("tasks", [])
    if tasks:
        new_tasks = [t for t in tasks if t.get("action") == "created_today"]
        ran_tasks = [t for t in tasks if t.get("last_run")]
        if new_tasks:
            lines.append("\nTasks created today (%d):" % len(new_tasks))
            for t in new_tasks:
                lines.append("  - %s" % t["name"])
        if ran_tasks:
            lines.append("\nScheduled tasks executed today (%d):" % len(ran_tasks))
            for t in ran_tasks:
                lines.append("  - %s (executed at %s)" % (t["name"], t.get("last_run", "?")))

    # Memories
    mem_count = digest.get("memories_count", 0)
    if mem_count:
        lines.append("\nNew memories today: %d" % mem_count)
        for m in digest.get("memories_preview", []):
            lines.append("  - %s" % m)
    else:
        lines.append("\nNew memories today: none")

    return "\n".join(lines)


# ============================================================

#  Daily archive tool
# ============================================================

@tool("archive", "Run daily black-box archive: snapshot conversation records, scheduled tasks, system logs, file index to workspace/archive/YYYY-MM-DD/.",
      {})
def tool_archive(args, ctx):
    cmd = ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive.py")]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=os.path.dirname(os.path.abspath(__file__)))
        if result.returncode != 0:
            return "[error] Archive script failed: " + (result.stderr or result.stdout)[:500]
        report = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return "[error] Archive script timed out (60s)"
    except Exception as e:
        return "[error] %s" % e

    lines = ["Archive completed (%s, took %dms)" % (report["timestamp"], report["duration_ms"])]
    lines.append("Directory: %s" % report["archive_dir"])

    sessions = report.get("sessions", {})
    if sessions.get("status") == "ok":
        total_new = sum(s.get("new", 0) for s in sessions.get("sessions", []))
        lines.append("Conversation records: %d sessions, %d new messages" % (len(sessions["sessions"]), total_new))

    journald = report.get("journald", {})
    if journald.get("status") == "ok":
        lines.append("System logs: %d lines" % journald.get("lines", 0))

    files_idx = report.get("files_index", {})
    if files_idx.get("status") == "ok":
        lines.append("File index: %d files (today %d)" % (files_idx.get("total", 0), files_idx.get("today", 0)))

    return "\n".join(lines)


# ============================================================

#  Code audit tool
# ============================================================

@tool("code_audit", "Run code audit script: check syntax, configuration, permissions, tool registration, session health, disk, processes, Git, stale files, anti-patterns, scheduled tasks. Returns structured report.",
      {"checks": {"type": "string", "description": "Comma-separated check items (optional, runs all if empty). Options: syntax,config_schema,permissions,tool_registry,session_health,disk_usage,process_health,git_status,stale_files,anti_patterns,jobs_health"}})
def tool_code_audit(args, ctx):
    checks_arg = args.get("checks", "").strip()
    cmd = ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.py")]
    if checks_arg:
        cmd.append("--checks=" + checks_arg)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=os.path.dirname(os.path.abspath(__file__)))
        if result.returncode != 0:
            return "[error] Audit script execution failed: " + (result.stderr or result.stdout)[:500]
        report = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return "[error] Audit script timed out (60s)"
    except json.JSONDecodeError:
        return "[error] Audit script output is not JSON: " + result.stdout[:300]
    except Exception as e:
        return "[error] %s" % e

    # Format as human-readable report
    summary = report.get("summary", {})
    lines = [
        "Code Audit Report (%s, took %dms)" % (report["timestamp"], report["duration_ms"]),
        "Total %d checks: %d passed / %d warnings / %d failed" % (
            summary.get("total_checks", 0), summary.get("pass", 0),
            summary.get("warn", 0), summary.get("fail", 0)),
        "",
    ]
    for name, check in report.get("checks", {}).items():
        icon = {"pass": "OK", "warn": "WARN", "fail": "FAIL"}.get(check["status"], "?")
        lines.append("[%s] %s" % (icon, name))
        for d in check.get("details", []):
            msg = d.get("msg", json.dumps(d, ensure_ascii=False))
            lines.append("  - " + msg)

    # Save raw JSON report to audit directory
    import time as _time
    audit_dir = os.path.join(ctx.get("workspace", "./workspace"), "audit")
    os.makedirs(audit_dir, exist_ok=True)
    report_path = os.path.join(audit_dir, _time.strftime("%Y-%m-%d") + ".json")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        lines.append("")
        lines.append("Raw report saved: " + os.path.relpath(report_path, os.path.dirname(os.path.abspath(__file__))))
    except Exception:
        pass

    return "\n".join(lines)


# ============================================================

#  MCP hot-reload tool
# ============================================================

@tool('reload_mcp', 'Hot-reload MCP servers: re-read mcp_servers config from config.json, connect new servers, disconnect removed servers. Call this tool after modifying config.json to apply changes without restarting the service.', {})
def _reload_mcp(args, ctx):
    import mcp_client
    # Re-read config.json (get latest config)
    import os as _os
    config_path = _os.environ.get('AGENT_CONFIG', _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'config.json'))
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # Remove old MCP tools from _registry first
    old_mcp_keys = [k for k in _registry if '__' in k and k.split('__')[0] in mcp_client._servers]
    for k in old_mcp_keys:
        del _registry[k]

    # Hot reload
    added, removed, total = mcp_client.reload(config)

    # Re-register new tools
    for tool_def in mcp_client.get_all_tool_defs():
        name = tool_def['function']['name']
        _registry[name] = {
            'fn': lambda a, c, _name=name: mcp_client.execute(_name, a),
            'definition': tool_def,
        }

    parts = []
    if added:
        parts.append('Added servers: %s' % ', '.join(added))
    if removed:
        parts.append('Removed servers: %s' % ', '.join(removed))
    tools_count = len(mcp_client.get_all_tool_defs())
    parts.append('Current MCP tool count: %d (from %d servers)' % (tools_count, total))
    result = "\n".join(parts)
    log.info('[mcp] reloaded: %s' % result)
    return result


# ============================================================
#  MCP Server tool loading
# ============================================================


@tool("compact_memory", "Organize memory: move large sections in MEMORY.md to independent topic files (zero data loss). "
      "force=true ignores threshold and forces compaction, default only compacts when threshold is exceeded.",
      {"force": {"type": "boolean", "description": "true=force compaction (ignore threshold), false=only compact when above threshold. Default false"}})
def tool_compact_memory(args, ctx):
    import memory as _mem
    workspace = ctx.get("workspace", "")
    force = args.get("force", False)
    if force:
        result = _mem.manual_compact(workspace)
    else:
        result = _mem.auto_compact(workspace)
    if not result:
        return "compact completed (no details)"
    status = result.get("status", "unknown")
    if status == "skip":
        reason = result.get("reason", "")
        chars = result.get("chars", "?")
        threshold = result.get("threshold", "?")
        if reason == "below threshold":
            return "Below compaction threshold. Current MEMORY.md: %s chars, threshold: %s chars. Use force=true to force compaction." % (chars, threshold)
        if reason == "no section above threshold":
            return "All sections already compacted or below section threshold (%d chars). Current MEMORY.md: %s chars, no action needed." % (500, chars)
        return "Skipped compaction: %s" % reason
    elif status == "compacted":
        lines = ["Memory compaction completed! MEMORY.md: %d -> %d chars" % (result["before"], result["after"])]
        for m in result.get("moved", []):
            lines.append("  - %s (%d chars) -> memory/%s" % (m["section"], m["chars"], m["file"]))
        return "\n".join(lines)
    elif status == "error":
        return "Compaction error: %s" % result.get("reason", "unknown")
    return str(result)


@tool("compact_guides", "Compact guide files: move large sections in AGENT.md/SOUL.md to guides/ directory (zero data loss). "
      "After compaction, a read_file reference is left in place so agent loads details on demand. "
      "force=true ignores threshold and forces compaction.",
      {"force": {"type": "boolean", "description": "true=force compaction (ignore threshold), false=only compact when above threshold. Default false"}})
def tool_compact_guides(args, ctx):
    import memory as _mem
    workspace = ctx.get("workspace", "")
    force = args.get("force", False)
    if force:
        result = _mem.manual_compact_guides(workspace)
    else:
        result = _mem.auto_compact_guides(workspace)
    if not result:
        return "Compaction completed (no details)"
    status = result.get("status", "unknown")
    if status == "skip":
        files = result.get("files", [])
        if files:
            parts = []
            for f in files:
                if f["status"] == "skip" and f.get("reason") == "below threshold":
                    parts.append("%s: %d chars, below threshold %d" % (f["file"], f["chars"], f["threshold"]))
                elif f["status"] == "skip":
                    parts.append("%s: %s" % (f["file"], f.get("reason", "skipped")))
            return "No compaction needed.\n" + "\n".join(parts) if parts else "No compaction needed."
        return "Skipped: %s" % result.get("reason", "")
    elif status == "compacted":
        lines = ["Guide compaction completed!"]
        for f in result.get("files", []):
            if f["status"] == "compacted":
                lines.append("%s: %d -> %d chars" % (f["file"], f["before"], f["after"]))
                for m in f.get("moved", []):
                    lines.append("  - %s (%d chars) -> guides/%s" % (m["section"], m["chars"], m["file"]))
            elif f["status"] == "skip":
                lines.append("%s: no compaction needed" % f["file"])
        return "\n".join(lines)
    return str(result)



# --- ASR diagnostic tool ---

@tool("asr_check", "Voice recognition diagnostics: count today's voice files, ASR success/failure counts, success rate, output diagnostic report. "
      "Used for troubleshooting voice message loss issues.", {})
def tool_asr_check(args, ctx):
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")
    report = ["== ASR Diagnostic Report (%s) ==" % today]

    # 1. Count voice files
    voice_dir = os.path.join(ctx["workspace"], "files")
    voice_files_today = []
    voice_files_total = 0
    if os.path.isdir(voice_dir):
        for root, dirs, files in os.walk(voice_dir):
            for fname in files:
                if any(fname.endswith(ext) for ext in ('.silk', '.amr', '.mp3', '.wav', '.slk')):
                    voice_files_total += 1
                    fpath = os.path.join(root, fname)
                    try:
                        mtime = datetime.fromtimestamp(os.path.getmtime(fpath), CST)
                        if mtime.strftime("%Y-%m-%d") == today:
                            size_kb = os.path.getsize(fpath) / 1024
                            voice_files_today.append((mtime.strftime("%H:%M:%S"), fname, size_kb))
                    except Exception:
                        pass
    report.append("Voice files: today %d, total %d" % (len(voice_files_today), voice_files_total))

    # 2. Count ASR success/failure from sessions
    asr_success = 0
    asr_fail = 0
    sessions_dir = os.path.join(os.path.dirname(ctx["workspace"]), "sessions")
    if os.path.isdir(sessions_dir):
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(sessions_dir, fname)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), CST)
                if mtime.strftime("%Y-%m-%d") != today:
                    continue
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                asr_success += content.count("[voice-to-text]")
                asr_fail += content.count("voice recognition failed")
            except Exception:
                pass

    total_attempts = asr_success + asr_fail
    if total_attempts > 0:
        rate = asr_success / total_attempts * 100
        report.append("ASR results: %d succeeded, %d failed, success rate %.0f%%" % (asr_success, asr_fail, rate))
    else:
        report.append("ASR results: no voice recognition records today")

    # 3. Today's voice file details
    if voice_files_today:
        report.append("\nToday's voice file details:")
        for t, fname, size in sorted(voice_files_today):
            report.append("  %s  %s  (%.0fKB)" % (t, fname, size))

    # 4. ASR provider config check
    report.append("\n== ASR Config ==")
    config_path = os.environ.get("AGENT_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        asr_cfg = config.get("asr", {})
        if asr_cfg:
            report.append("app_id: %s" % asr_cfg.get("app_id", "not configured"))
            report.append("api_key: %s...%s" % (asr_cfg.get("api_key", "?")[:4], asr_cfg.get("api_key", "?")[-4:]))
            report.append("Status: configured")
        else:
            report.append("Status: not configured (voice recognition unavailable)")
    except Exception as e:
        report.append("Config read failed: %s" % e)

    # 5. ffmpeg / pilk check
    report.append("\n== Dependency Check ==")
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        ver = result.stdout.split("\n")[0] if result.stdout else "unknown"
        report.append("ffmpeg: %s" % ver[:60])
    except Exception:
        report.append("ffmpeg: not installed!")
    try:
        import pilk
        report.append("pilk: installed")
    except ImportError:
        report.append("pilk: not installed (SILK format voice cannot be decoded)")

    return "\n".join(report)


def _load_mcp_servers(config):
    """Connect MCP servers and register their tools into _registry"""
    if not config.get("mcp_servers"):
        return
    import mcp_client
    mcp_client.init(config)
    for tool_def in mcp_client.get_all_tool_defs():
        name = tool_def["function"]["name"]
        _registry[name] = {
            "fn": lambda args, ctx, _name=name: mcp_client.execute(_name, args),
            "definition": tool_def,
        }
    count = len(mcp_client.get_all_tool_defs())
    if count:
        log.info("[mcp] registered %d MCP tools" % count)
