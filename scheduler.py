"""
Built-in Scheduler — One-shot Delayed Tasks + Cron Recurring Tasks

Persisted in jobs.json, background thread checks every 10s.
On trigger, calls chat_fn(message, "scheduler") -> LLM processing -> sends messages via tools.

Dependencies: stdlib + croniter (pip install croniter)
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

log = logging.getLogger("agent")
CST = timezone(timedelta(hours=8))

# ============================================================
#  State
# ============================================================

_jobs_lock = threading.Lock()
_jobs = []
_jobs_file = ""
_chat_fn = None  # injected by init()
_users = {}      # Multi-tenant: sender_id -> user_config
_sessions_dir = "" 

# Silent task keywords: when task prompt contains these words, LLM not calling message is expected, skip fallback
SILENT_KEYWORDS = ["silent", "no_notify", "no_disturb", "skip_notify", "on_failure_only", "on_archive_failure", "sync"]

# Complex task keywords: these tasks need multi-step tool calls, shared session context noise causes hallucination
# Give them clean temporary sessions, bridge results back to main session after execution
COMPLEX_TASK_KEYWORDS = ["self_check", "review", "review_task", "audit_task", "audit", "patrol", "diary", "digest"]

# Inactivity guard: auto-skip user-facing cron tasks when user is silent > 3 days
INACTIVITY_THRESHOLD = 3 * 86400  # 3 days

# Internal tasks: never skip even if user is inactive (housekeeping)
INTERNAL_TASK_KEYWORDS = [
    "archive", "diary", "review", "self_check", "digest", "compact", "sync", "archive",
    "audit", "review_task", "audit_task", "patrol"
]


def _is_internal_task(job):
    """Determine if task is internal maintenance (not user-facing, should execute even when user is inactive)."""
    name = job.get("name", "")
    msg = job.get("message", "")[:200]
    return any(kw in name or kw in msg for kw in INTERNAL_TASK_KEYWORDS)


def _get_last_user_activity(workspace):
    """Get user last activity time: reads .last_user_message file (written by xiaowang.py on user DM).
    Does not rely on session file mtime, because scheduled tasks also modify session files."""
    marker = os.path.join(workspace, ".last_user_message")
    if os.path.exists(marker):
        try:
            with open(marker, "r") as f:
                return float(f.read().strip())
        except (ValueError, OSError):
            pass
    return None


def _is_complex_task(job):
    """Determine if task needs a clean independent session."""
    name = job.get("name", "")
    msg = job.get("message", "")[:100]
    return any(kw in name or kw in msg for kw in COMPLEX_TASK_KEYWORDS)


def init(jobs_file, chat_fn, users=None, sessions_dir=None):
    """Initialize scheduler. chat_fn signature: chat_fn(message, session_key, images=None, user_config=None)"""
    global _jobs_file, _chat_fn, _users, _sessions_dir
    _jobs_file = jobs_file
    _chat_fn = chat_fn
    _users = users or {}
    _sessions_dir = sessions_dir or os.path.dirname(jobs_file)
    _load_jobs()
    log.info(f"[scheduler] loaded {len(_jobs)} jobs")


def start():
    """Start background check thread"""
    t = threading.Thread(target=_loop, daemon=True)
    t.start()


# ============================================================
#  CRUD
# ============================================================

def add(args):
    """Create a scheduled task. args: name, message, delay_seconds?, cron_expr?, once?"""
    name = args["name"]
    message = args["message"]
    delay = args.get("delay_seconds")
    cron_expr = args.get("cron_expr")
    once = args.get("once", True)

    target_time = args.get("target_time")
    year_corrected = False
    if target_time:
        try:
            target_dt = datetime.strptime(target_time, "%Y-%m-%d %H:%M").replace(tzinfo=CST)
            parsed_trigger_at = target_dt.timestamp()
            if parsed_trigger_at <= time.time():
                # Auto-correct year: LLM training data may use expired year
                now_dt = datetime.now(CST)
                corrected_dt = target_dt.replace(year=now_dt.year)
                if corrected_dt.timestamp() <= time.time():
                    corrected_dt = target_dt.replace(year=now_dt.year + 1)
                if corrected_dt.timestamp() > time.time():
                    log.info("[scheduler] auto-corrected year: %s -> %s", target_time, corrected_dt.strftime("%Y-%m-%d %H:%M"))
                    target_dt = corrected_dt
                    parsed_trigger_at = corrected_dt.timestamp()
                    target_time = corrected_dt.strftime("%Y-%m-%d %H:%M")
                    year_corrected = True
                else:
                    return f"[error] Target time {target_time} has passed"
            delay = int(parsed_trigger_at - time.time())
        except ValueError:
            return "[error] Invalid time format, expected 'YYYY-MM-DD HH:MM', got: " + target_time

    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST")
    job = {"name": name, "message": message, "created": now_str, "created_ts": time.time()}
    if args.get("owner_id"):
        job["owner_id"] = args["owner_id"]
    if args.get("group_id"):
        job["group_id"] = args["group_id"]

    if delay:
        if target_time:
            trigger_at = parsed_trigger_at
        else:
            trigger_at = time.time() + delay
        job["trigger_at"] = trigger_at
        job["type"] = "once"
        trigger_str = datetime.fromtimestamp(trigger_at, CST).strftime("%Y-%m-%d %H:%M:%S CST")
        desc = f"One-shot task, will trigger at {trigger_str} trigger"
    elif cron_expr:
        job["cron_expr"] = cron_expr
        job["type"] = "once_cron" if once else "cron"
        desc = f"{'one-shot' if once else 'recurring'}scheduled task, cron: {cron_expr}"
    else:
        return "[error] need delay_seconds or cron_expr"

    with _jobs_lock:
        _jobs[:] = [j for j in _jobs if j["name"] != name]
        _jobs.append(job)
        _save_jobs()

    log.info(f"[scheduler] added: {name} — {desc}")
    return f"Created scheduled task"{name}"— {desc}" + (f"(auto-corrected year -> {target_time})" if year_corrected else "")


def list_all(owner_id=None):
    with _jobs_lock:
        jobs = _jobs if not owner_id else [j for j in _jobs if j.get("owner_id", "") == owner_id]
        jobs = [j for j in jobs if j.get("status") != "completed"]
        if not jobs:
            return "No scheduled tasks currently"
        lines = []
        for j in jobs:
            if j.get("type") == "once":
                trigger_str = datetime.fromtimestamp(j["trigger_at"], CST).strftime("%m-%d %H:%M")
                lines.append(f"- {j['name']}(one-shot, {trigger_str} trigger): {j['message'][:50]}")
            else:
                lines.append(f"- {j['name']}({j.get('cron_expr', '?')}): {j['message'][:50]}")
        return "\n".join(lines)


def remove(name, owner_id=None):
    with _jobs_lock:
        before = len(_jobs)
        _jobs[:] = [j for j in _jobs if not (j["name"] == name and (not owner_id or j.get("owner_id", "") == owner_id))]
        _save_jobs()
        if len(_jobs) < before:
            return f"Deleted scheduled task"{name}""
        return f"Task not found"{name}""


# ============================================================
#  Internal
# ============================================================

def _load_jobs():
    global _jobs
    if os.path.exists(_jobs_file):
        try:
            with open(_jobs_file, "r", encoding="utf-8") as f:
                _jobs = json.load(f)
        except Exception:
            _jobs = []
    else:
        _jobs = []


def _save_jobs():
    # Atomic write: write to temp file then rename, to prevent corruption on crash
    tmp = _jobs_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_jobs, f, ensure_ascii=False, indent=2)
    try:
        os.replace(tmp, _jobs_file)
    except OSError:
        import shutil
        shutil.copy2(tmp, _jobs_file)
        os.remove(tmp)


def _check():
    """Check and trigger due tasks"""
    now = time.time()
    to_trigger = []

    with _jobs_lock:
        remaining = []
        for job in _jobs:
            if job.get("type") == "once" and job.get("status") != "completed" and now >= job.get("trigger_at", 0):
                to_trigger.append(job)
                job["status"] = "completed"
                job["completed_at"] = now
                remaining.append(job)
            elif job.get("type") == "once" and job.get("status") == "completed":
                remaining.append(job)
            elif job.get("type") in ("cron", "once_cron"):
                try:
                    from croniter import croniter
                    # Fix 1: Pass CST datetime to croniter to avoid UTC-based cron expression parsing
                    last_run = job.get("last_run") or job.get("created_ts", now - 60)
                    if isinstance(last_run, str):
                        last_run = now - 60
                    last_run_dt = datetime.fromtimestamp(last_run, CST)
                    cron = croniter(job["cron_expr"], last_run_dt)
                    next_dt = cron.get_next(datetime)
                    next_time = next_dt.timestamp()
                    if now >= next_time:
                        to_trigger.append(job)
                        if job["type"] == "cron":
                            job["last_run"] = now
                            remaining.append(job)
                        continue
                except Exception as e:
                    log.error(f"[scheduler] cron error for {job['name']}: {e}")
                remaining.append(job)
            else:
                remaining.append(job)
        # Clean up one-shot tasks completed more than 24h ago
        cutoff = now - 86400
        remaining = [j for j in remaining if not (j.get("status") == "completed" and j.get("completed_at", 0) < cutoff)]
        _jobs[:] = remaining
        if to_trigger:
            _save_jobs()

    for job in to_trigger:
        log.info(f"[scheduler] triggering: {job['name']}")
        threading.Thread(target=_trigger, args=(job,), daemon=True).start()


def _fallback_send(reply, owner_id, job_name, session_key=None):
    """Fallback send: check if message tool was called in most recent scheduler session round, send directly if not."""
    try:
        if session_key is None:
            session_key = f"scheduler_{owner_id}"
        session_file = os.path.join(_sessions_dir, f"{session_key}.json")
        if os.path.exists(session_file):
            with open(session_file, "r", encoding="utf-8") as f:
                msgs = json.load(f)
            # Scan from end to find most recent assistant message, check for message tool call
            for m in reversed(msgs):
                if m.get("role") == "assistant" and m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        if tc.get("function", {}).get("name") == "message":
                            log.info("[scheduler] %s: message tool was called, skip fallback", job_name)
                            return
                    break  # Only check the most recent assistant message with tool_calls
                if m.get("role") == "user":
                    break  # Reached user message without finding tool_calls, means none were called
        # LLM did not call message tool, fallback sending
        import messaging
        log.warning("[scheduler] %s: LLM didn't call message tool, fallback sending", job_name)
        messaging.send_text(owner_id, reply)
    except Exception as e:
        log.error("[scheduler] fallback send failed for %s: %s", job_name, e)


def _bridge_temp_session(temp_key, main_key, job_name):
    """Bridge message tool calls from temporary session back to main scheduler session.

    Only bridge valid message sequences (user prompt + assistant with message tool_call + tool result),
    ensuring main session message format is valid for _get_recent_scheduler_context to read correctly.
    """
    temp_path = os.path.join(_sessions_dir, f"{temp_key}.json")
    main_path = os.path.join(_sessions_dir, f"{main_key}.json")

    if not os.path.exists(temp_path):
        return

    try:
        with open(temp_path, "r", encoding="utf-8") as f:
            temp_msgs = json.load(f)

        if not temp_msgs:
            os.remove(temp_path)
            return

        # Extract bridge messages: task prompt + complete sequence with message tool calls
        bridge_msgs = []

        # 1. First user message (task prompt)
        for msg in temp_msgs:
            if msg.get("role") == "user":
                bridge_msgs.append(msg)
                break

        # 2. Find assistant messages with message tool calls + corresponding tool results
        for i, msg in enumerate(temp_msgs):
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue
            has_message_call = any(
                tc.get("function", {}).get("name") == "message"
                for tc in msg.get("tool_calls", [])
            )
            if not has_message_call:
                continue

            bridge_msgs.append(msg)
            # Collect immediately following tool result messages
            for j in range(i + 1, len(temp_msgs)):
                if temp_msgs[j].get("role") == "tool":
                    bridge_msgs.append(temp_msgs[j])
                else:
                    break

        # 3. Final text reply (if exists and not already included)
        if temp_msgs and temp_msgs[-1].get("role") == "assistant" and not temp_msgs[-1].get("tool_calls"):
            if temp_msgs[-1] not in bridge_msgs:
                bridge_msgs.append(temp_msgs[-1])

        # If only task prompt without useful output, also bridge prompt + final reply
        # So bridge at least knows this task was executed
        if len(bridge_msgs) <= 1:
            for msg in reversed(temp_msgs):
                if msg.get("role") == "assistant" and msg.get("content"):
                    bridge_msgs.append(msg)
                    break

        # Append to main session
        main_msgs = []
        if os.path.exists(main_path):
            try:
                with open(main_path, "r", encoding="utf-8") as f:
                    main_msgs = json.load(f)
            except Exception:
                main_msgs = []

        main_msgs.extend(bridge_msgs)

        # Atomic write
        tmp = main_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(main_msgs, f, ensure_ascii=False, indent=2)
        try:
            os.replace(tmp, main_path)
        except OSError:
            import shutil
            shutil.copy2(tmp, main_path)
            os.remove(tmp)

        # Clean up temporary session
        log.info("[scheduler] bridged %d msgs from temp session for %s", len(bridge_msgs), job_name)

    except Exception as e:
        log.error("[scheduler] bridge failed for %s: %s", job_name, e)
    finally:
        # Always clean up temporary session files regardless of success or failure
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


def _preload_context(job, workspace):
    """Preload relevant files for complex tasks, inject into prompt.

    LLM does not need to guess filenames or call read_file, all input data is directly in the prompt.
    LLM only needs to analyze and decide (edit_file/write_file/message).
    """
    name = job.get("name", "")
    msg = job.get("message", "")

    # Review task: preload diary + lessons learned
    if "review" in name or "review_task" in name:
        return _preload_review(msg, workspace)

    # Diary task: preload daily_digest does not need files (digest tool generates its own data)
    # But check if today diary already exists
    if "diary" in name or "digest" in name:
        return _preload_diary(msg, workspace)

    # Self-check task: preload today's diary (if exists)
    if "self_check" in name:
        return _preload_selfcheck(msg, workspace)

    return msg


def _preload_review(original_msg, workspace):
    """Preload all files needed for review, concatenate into prompt."""
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))

    sections = [original_msg, ""]
    sections.append("=" * 40)
    sections.append("The following file contents were automatically preloaded. You do not need to call read_file for these.")
    sections.append("=" * 40)

    # 1. lessons_learned
    lessons_path = os.path.join(workspace, "guides", "agent_lessons_learned.md")
    if os.path.exists(lessons_path):
        try:
            content = open(lessons_path, "r", encoding="utf-8").read()
            sections.append("\n--- guides/agent_lessons_learned.md ---")
            sections.append(content)
        except Exception:
            pass

    # 2. MEMORY.md current content
    mem_path = os.path.join(workspace, "memory", "MEMORY.md")
    if os.path.exists(mem_path):
        try:
            content = open(mem_path, "r", encoding="utf-8").read()
            sections.append("\n--- memory/MEMORY.md(Current content, compare with diary to see what needs updating)---")
            sections.append(content)
        except Exception:
            pass

    # 3. Diary files from past 3 days
    today = datetime.now(CST)
    diary_found = 0
    for days_ago in range(0, 4):  # 0,1,2,3 days ago
        date_str = (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        diary_path = os.path.join(workspace, "memory", f"{date_str}.md")
        if os.path.exists(diary_path):
            try:
                content = open(diary_path, "r", encoding="utf-8").read()
                if content.strip():
                    sections.append(f"\n--- memory/{date_str}.md ---")
                    sections.append(content)
                    diary_found += 1
            except Exception:
                pass

    if diary_found == 0:
        sections.append("\n(No diary files from the past 3 days)")

    # 4. List logs older than 7 days
    old_diaries = []
    cutoff = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    mem_dir = os.path.join(workspace, "memory")
    if os.path.isdir(mem_dir):
        import re
        for fname in sorted(os.listdir(mem_dir)):
            m = re.match(r"(\d{4}-\d{2}-\d{2})\.md$", fname)
            if m and m.group(1) < cutoff:
                old_diaries.append(fname)

    if old_diaries:
        sections.append(f"\n--- Logs older than 7 days({len(old_diaries)}files)---")
        for fname in old_diaries:
            sections.append(f"- {fname}")
        sections.append("Before deleting these, confirm that key information has been captured in MEMORY.md.")

    sections.append("\n" + "=" * 40)
    sections.append("Above is preloaded content. Now follow the steps in the prompt to analyze and update.")
    sections.append("Note: steps 0-2 have been completed by the system (file contents above), start from step 3.")

    log.info("[scheduler] preloaded review context: %d diary files, %d old diaries", diary_found, len(old_diaries))
    return "\n".join(sections)


def _preload_diary(original_msg, workspace):
    """Preload context needed for diary tasks."""
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))

    sections = [original_msg]

    date_str = datetime.now(CST).strftime("%Y-%m-%d")
    diary_path = os.path.join(workspace, "memory", f"{date_str}.md")
    if os.path.exists(diary_path):
        try:
            content = open(diary_path, "r", encoding="utf-8").read()
            sections.append(f"\n[system preload] memory/{date_str}.md already exists, current content:")
            sections.append(content)
            sections.append("Please append to end of file, do not overwrite existing content.")
        except Exception:
            pass
    else:
        sections.append(f"\n[system preload] memory/{date_str}.md does not exist yet, create directly.")

    return "\n".join(sections)


def _preload_selfcheck(original_msg, workspace):
    """Preload context needed for self-check tasks."""
    from datetime import datetime, timezone, timedelta
    CST = timezone(timedelta(hours=8))

    sections = [original_msg]

    date_str = datetime.now(CST).strftime("%Y-%m-%d")
    diary_path = os.path.join(workspace, "memory", f"{date_str}.md")
    if os.path.exists(diary_path):
        try:
            content = open(diary_path, "r", encoding="utf-8").read()
            sections.append(f"\n[system preload] today's diary memory/{date_str}.md: ")
            sections.append(content)
        except Exception:
            pass
    else:
        sections.append(f"\n[system preload] No diary file for today.")

    return "\n".join(sections)


def _trigger(job):
    """Trigger task, notify user on failure.
    Complex tasks (review/audit/patrol etc.) use clean temporary sessions to avoid context pollution,
    bridging message tool calls back to main session for cross-session context."""
    # Group chat scheduled tasks: independent processing path
    group_id = job.get("group_id")
    if group_id:
        session_key = "scheduler_group_%s" % group_id
        user_config = next(iter(_users.values())) if _users else None
        try:
            task_message = job["message"]
            group_ctx = {"group_id": group_id}
            reply = _chat_fn(task_message, session_key, images=None,
                            user_config=user_config, group_ctx=group_ctx)
            log.info("[scheduler] group task %s OK: %s", job["name"], (reply or "")[:100])
            # Group task fallback: if LLM did not call message, send directly to group
            if reply and reply.strip():
                _fallback_send(reply, group_id, job["name"], session_key)
        except Exception as e:
            log.error("[scheduler] group task %s failed: %s", job["name"], e, exc_info=True)
        return

    owner_id = job.get("owner_id", "")
    main_session_key = f"scheduler_{owner_id}" if owner_id else "scheduler"
    user_config = _users.get(owner_id) if owner_id else None

    # === Inactivity guard: skip user-facing cron tasks if user is silent ===
    if job.get("type") == "cron" and not _is_internal_task(job):
        last_active = _get_last_user_activity(user_config.get("workspace", "")) if user_config else None
        if last_active:
            inactive_seconds = time.time() - last_active
            if inactive_seconds > INACTIVITY_THRESHOLD:
                days = inactive_seconds / 86400
                log.info("[scheduler] SKIP %s: user inactive %.0f days (threshold: %d days)",
                         job["name"], days, INACTIVITY_THRESHOLD // 86400)
                # Write dormant marker (first time only) for graceful UX on return
                if user_config:
                    dormant_file = os.path.join(user_config.get("workspace", ""), ".dormant_since")
                    if not os.path.exists(dormant_file):
                        try:
                            with open(dormant_file, "w") as f:
                                f.write(str(time.time()))
                            log.info("[scheduler] marked user as dormant: %s", dormant_file)
                        except Exception as e:
                            log.error("[scheduler] failed to write dormant file: %s", e)
                return

    # Complex task: clean independent session, avoid 80+ irrelevant messages polluting context
    is_complex = _is_complex_task(job)
    if is_complex:
        session_key = f"{main_session_key}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        log.info("[scheduler] %s: complex task, using clean session %s", job["name"], session_key)
    else:
        session_key = main_session_key

    try:
        # Complex task: preload relevant files, inject into prompt (LLM does not need to read_file itself)
        task_message = job["message"]
        # Date anchor: let LLM know task creation and current time, avoid executing stale instructions
        created_str = job.get("created", "")
        now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        task_message = "[Scheduled task triggered | Created: %s | Now: %s]\n%s" % (created_str, now_str, task_message)
        if is_complex and user_config:
            try:
                task_message = _preload_context(job, user_config.get("workspace", ""))
            except Exception as e:
                log.error("[scheduler] preload failed for %s: %s", job["name"], e)
        reply = _chat_fn(task_message, session_key, images=None, user_config=user_config)
        log.info(f"[scheduler] {job['name']} OK: {reply[:100] if reply else '(empty)'}")
        # Fallback: if LLM returned text but did not call message tool, send directly to user
        if reply and reply.strip() and owner_id:
            if any(kw in job.get("name", "") or kw in job.get("message", "") for kw in SILENT_KEYWORDS):
                log.info("[scheduler] %s: silent task, skip fallback", job["name"])
            else:
                _fallback_send(reply, owner_id, job["name"], session_key)
        # Bridge: write complex task message tool calls back to main session
        if is_complex:
            _bridge_temp_session(session_key, main_session_key, job["name"])
    except Exception as e:
        log.error(f"[scheduler] {job['name']} FAILED: {e}", exc_info=True)
        try:
            _chat_fn(
                f"Scheduled task "{job['name']}" failed with error: {e}. Please use the message tool to notify the user.",
                main_session_key, images=None, user_config=user_config
            )
        except Exception:
            pass  # Notification also failed, can only wait for next heartbeat


def _log_heartbeat():
    """Print heartbeat log: task count + next trigger time for each cron task"""
    with _jobs_lock:
        if not _jobs:
            return
        lines = []
        for job in _jobs:
            if job.get("cron_expr"):
                try:
                    from croniter import croniter
                    lr = job.get("last_run") or job.get("created_ts", time.time() - 60)
                    lr_dt = datetime.fromtimestamp(lr, CST)
                    c = croniter(job["cron_expr"], lr_dt)
                    nxt = c.get_next(datetime)
                    lines.append(f"{job['name']}→{nxt.strftime('%H:%M')}")
                except Exception:
                    lines.append(f"{job['name']}→?")
        log.info(f"[scheduler] heartbeat: {len(_jobs)} jobs, next: {', '.join(lines)}")


def _loop():
    check_count = 0
    while True:
        try:
            _check()
        except Exception as e:
            log.error(f"[scheduler] loop error: {e}", exc_info=True)
        check_count += 1
        if check_count % 180 == 0:  # every 180 checks x 10s = 30 minutes
            _log_heartbeat()
        time.sleep(10)
