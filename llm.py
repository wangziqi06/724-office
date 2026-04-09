"""
LLM Calls + Tool Use Loop + Session Management

Core loop: user message -> LLM -> tool calls -> execute -> LLM -> ... -> final reply
Multimodal support: images passed via image_url (base64) to LLM.
"""

import base64
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

import tools

log = logging.getLogger("agent")
CST = timezone(timedelta(hours=8))

# ============================================================
#  Initialization (injected by xiaowang.py)
# ============================================================

_config = {}       # models config
_users = {}        # sender_id -> {owner_id, workspace, model, name}
_sessions_dir = ""
MAX_SESSION_MESSAGES = 40
MAX_SCHEDULER_MESSAGES = 200
VOICE_SESSION_MESSAGES = 10


def _is_voice_session(session_key: str) -> bool:
    return session_key == "voice" or session_key.startswith("voice")


def _default_user_config():
    """Backward compat: return first user config"""
    if _users:
        return next(iter(_users.values()))
    return {"owner_id": "", "workspace": "", "model": _config.get("default", "")}


def init(models_config, users, sessions_dir):
    global _config, _users, _sessions_dir
    _config = models_config
    _users = users
    _sessions_dir = sessions_dir


# ============================================================
#  LLM API Calls
# ============================================================

def _get_provider(name=None):
    """Get LLM provider. name=None returns default."""
    provider_name = name or _config["default"]
    return _config["providers"][provider_name]


def _likely_needs_tools(message):
    """Keyword heuristic: determine if message likely needs tool calls."""
    tool_keywords = [
        "search", "look_up", "check", "find_for_me", "baidu",
        "weather", "temperature", "rain", "snow",
        "send_msg", "send_message", "tell", "notify",
        "remember", "note_down", "record", "write_down",
        "schedule", "remind", "alarm", "minutes_later",
        "write_file", "create", "delete",
        "device", "tv", "ac", "music", "play",
        "video_kw", "photo", "health", "heart_rate",
        "news", "trending",
    ]
    return any(kw in message for kw in tool_keywords)


def _select_model(message, session_key):
    """Voice always uses voice_default (minimax-highspeed), chat/scheduler always uses kimi-k2.5."""
    if not _is_voice_session(session_key):
        return _config["default"]  # kimi-k2.5

    voice_default = _config.get("voice_default", "minimax-highspeed")
    if voice_default in _config.get("providers", {}):
        return voice_default
    return _config["default"]


def _call_llm(messages, tool_defs, provider=None):
    provider = provider or _get_provider()
    url = provider["api_base"].rstrip("/") + "/chat/completions"

    body = {
        "model": provider["model"],
        "messages": messages,
        "tools": tool_defs,
        "max_tokens": provider.get("max_tokens", 8192),
    }
    extra = provider.get("extra_body", {})
    body.update(extra)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider['api_key']}",
    }

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    timeout = provider.get("timeout", 120)

    # 429 retry: max 4 attempts, exponential backoff, prevent rate limit from crashing entire chat loop
    max_retries = 4
    backoff_delays = [3, 6, 12, 24]

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass

            # Only retry on 429, throw on other errors
            if e.code == 429 and attempt < max_retries:
                delay = backoff_delays[attempt]
                log.warning("[llm] HTTP 429 rate limit, retry %d/%d after %ds: %s",
                            attempt + 1, max_retries, delay, body_text)
                import time
                time.sleep(delay)
                continue

            log.error("[llm] HTTP %d: %s" % (e.code, body_text))
            raise


# ============================================================
#  Session Management
# ============================================================

def _session_path(session_key):
    safe = session_key.replace("/", "_").replace(":", "_").replace("\\", "_")
    return os.path.join(_sessions_dir, f"{safe}.json")


def _load_session(session_key, user_id=None):
    path = _session_path(session_key)
    if os.path.exists(path):
        # PR 3.1: auto-archive and reset when scheduler session is too large
        # Only for scheduler sessions (long-term accumulation, 200 msg limit)
        # DM sessions not archived (natural MAX_SESSION_MESSAGES=40 limit, tool results are large but truncated)
        if session_key.startswith("scheduler"):
            try:
                fsize = os.path.getsize(path)
                if fsize > 60 * 1024:  # 60KB
                    archive_dir = os.path.join(_sessions_dir, "archive")
                    os.makedirs(archive_dir, exist_ok=True)
                    ts = datetime.now(CST).strftime("%Y%m%d_%H%M%S")
                    archive_path = os.path.join(archive_dir, f"{session_key}_{ts}.json")
                    import shutil
                    shutil.copy2(path, archive_path)
                    os.remove(path)
                    log.warning("[session] %s archived (%.0fKB > 60KB) -> %s",
                                session_key, fsize / 1024, archive_path)
                    return []
            except Exception as e:
                log.error("[session] archive check error: %s", e)
        try:
            with open(path, "r", encoding="utf-8") as f:
                messages = json.load(f)
            if len(messages) > MAX_SESSION_MESSAGES:
                evicted = messages[:-MAX_SESSION_MESSAGES]
                messages = messages[-MAX_SESSION_MESSAGES:]
                # Compress evicted messages into long-term memory (same logic as _save_session)
                try:
                    import memory as mem_mod
                    mem_mod.compress_async(evicted, session_key, user_id=user_id)
                except Exception as e:
                    log.error("[session] load-time compress error: %s" % e)
            # After truncation, may start with orphan tool messages (no matching assistant + tool_calls),
            # or assistant with tool_calls but subsequent tool results truncated.
            # kimi-k2.5 requires valid message sequence, else 400. Skip to first user message.
            while messages and messages[0].get("role") not in ("user", "system"):
                messages.pop(0)
            return messages
        except Exception:
            return []
    return []


def _strip_images_for_storage(messages):
    """Before saving session, replace image_url in multimodal content with [image] text marker.

    Reason: kimi-k2.5 rejects image_url format in history messages, returns 400.
    Images only need to be sent in current turn, text markers suffice in history.
    """
    cleaned = []
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            # Multimodal content -> extract text, replace images with markers
            text_parts = []
            for item in msg["content"]:
                if item.get("type") == "text":
                    text_parts.append(item["text"])
                elif item.get("type") == "image_url":
                    text_parts.append("[image]")
            cleaned.append({"role": "user", "content": "\n".join(text_parts)})
        else:
            cleaned.append(msg)
    return cleaned


def _save_session(session_key, messages, user_id=None):
    limit = MAX_SCHEDULER_MESSAGES if session_key.startswith("scheduler") else MAX_SESSION_MESSAGES
    if len(messages) > limit:
        evicted = messages[:-limit]
        messages = messages[-limit:]
        # Hook 2: async compress evicted messages into long-term memory
        try:
            import memory as mem_mod
            mem_mod.compress_async(evicted, session_key, user_id=user_id)
        except Exception as e:
            log.error("[session] memory compress error: %s" % e)
    messages = _strip_images_for_storage(messages)
    path = _session_path(session_key)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=None)
        try:
            os.replace(tmp_path, path)
        except OSError:
            import shutil
            shutil.copy2(tmp_path, path)
            os.remove(tmp_path)
    except Exception as e:
        log.error(f"[session] save error: {e}")


def _serialize_assistant_msg(msg_data):
    """Serialize assistant message. Preserve reasoning_content (kimi-k2.5 compat)."""
    result = {"role": "assistant"}
    result["content"] = msg_data.get("content") or None

    reasoning = msg_data.get("reasoning_content")
    if reasoning:
        result["reasoning_content"] = reasoning

    tool_calls = msg_data.get("tool_calls")
    if tool_calls:
        if "reasoning_content" not in result:
            result["reasoning_content"] = "ok"
        result["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": tc["function"]["arguments"],
                },
            }
            for tc in tool_calls
        ]
    return result


# ============================================================
#  Multimodal Message Building
# ============================================================

_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5MB

def _image_to_base64_url(image_path):
    """Read image file, return data URI. Returns None if over 5MB."""
    file_size = os.path.getsize(image_path)
    if file_size > _IMAGE_MAX_BYTES:
        log.warning("[vision] image too large: %s (%.1fMB > 5MB)", image_path, file_size / 1024 / 1024)
        return None
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
    mime = mime_map.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


def _build_user_message(text, images=None):
    """Build user message, supports plain text or multimodal (text+images)"""
    if not images:
        return {"role": "user", "content": text}

    content = []
    if text:
        content.append({"type": "text", "text": text})
    for img_path in images:
        if os.path.exists(img_path):
            try:
                data_url = _image_to_base64_url(img_path)
                if data_url is None:
                    content.append({"type": "text", "text": "[image too large, skipped]"})
                else:
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": data_url}
                    })
            except Exception as e:
                log.error(f"[vision] failed to encode {img_path}: {e}")
                content.append({"type": "text", "text": f"[image load failed: {img_path}]"})
    return {"role": "user", "content": content}


# ============================================================
#  System Prompt
# ============================================================


def _get_recent_scheduler_context(user_id=None):
    """Read all message tool calls from scheduler session, for cross-session context bridging.

    Collect all content sent via message tool (not just the last one),
    so the DM session can see complete scheduled task output history.
    Total length limited to 2000 chars, truncate oldest when exceeded.
    """
    sched_key = f"scheduler_{user_id}" if user_id else "scheduler"
    sched_path = _session_path(sched_key)
    if not os.path.exists(sched_path):
        return ""

    # Freshness check: skip if file is from yesterday (by day boundary, not 12h window)
    mtime = os.path.getmtime(sched_path)
    today_start = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if mtime < today_start:  # File is from yesterday
        return ""

    try:
        with open(sched_path, "r", encoding="utf-8") as f:
            msgs = json.load(f)
    except Exception:
        return ""

    if not msgs:
        return ""

    # Collect all message tool calls (task name + sent content)
    sent_items = []  # [(task_name, content)]
    current_task = ""
    for msg in msgs:
        # Extract task name from user message
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if isinstance(c, str) and len(c) < 200:
                current_task = c[:50]
        # Extract message calls from assistant's tool_calls
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg.get("tool_calls", []):
                if tc.get("function", {}).get("name") == "message":
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        c = args.get("content", "")
                        if c:
                            sent_items.append((current_task, c))
                    except (json.JSONDecodeError, KeyError):
                        pass

    if not sent_items:
        return ""

    # Build from newest to oldest, total length limited to 2000 chars
    parts = []
    total_len = 0
    for task_name, content in reversed(sent_items):
        if len(content) > 500:
            content = content[:500] + "..."
        entry = "[%s] %s" % (task_name[:30], content)
        if total_len + len(entry) > 2000:
            break
        parts.append(entry)
        total_len += len(entry) + 1

    parts.reverse()  # Restore chronological order
    from_time = datetime.fromtimestamp(mtime, CST).strftime("%H:%M")
    header = "[Messages sent via scheduled tasks today (last activity %s), user may reply to these]" % from_time
    return header + "\n" + "\n---\n".join(parts)


# System prompt budget: tiered per-file limits + total cap
MAX_SYSTEM_PROMPT_CHARS = 20000
# Per-file budgets (Tier 1 = critical, Tier 2 = important, Tier 3 = remaining)
_PROMPT_FILE_BUDGETS = {
    "SOUL.md": 3000,              # Tier 1: identity
    "AGENT_CORE.md": 6000,        # Tier 1: core behavior rules
    "AGENT.md": 6000,             # Tier 1: fallback if no AGENT_CORE.md
    "USER.md": 4000,              # Tier 2: user info
    "MEMORY.md": 4000,            # Tier 2: long-term memory (skipped for scheduler/voice)
    "AGENT_REFERENCE.md": 0,      # Tier 3: uses remaining budget
}



def _build_voice_prompt(workspace):
    """Voice-specific system prompt — format rules on top, dynamic user profile, tool list."""
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    # Read cron-generated voice context (if exists)
    voice_ctx = ""
    voice_ctx_path = os.path.join(workspace, "VOICE_CONTEXT.md")
    if os.path.exists(voice_ctx_path):
        try:
            with open(voice_ctx_path, "r", encoding="utf-8") as f:
                voice_ctx = f.read().strip()
        except Exception:
            pass

    # Fallback: default profile when cron has not run yet
    if not voice_ctx:
        voice_ctx = "The user, 25 years old, entrepreneur. Sleeps at 1-2am, wakes at 10:30. Works out 4 times a week."

    return f"""[Output Format — Highest Priority]
You are in a voice conversation with the user via speaker. You MUST follow:
- Reply in 1-2 sentences, keep it very short
- Conversational only. No markdown, lists, numbering, bold, or headers
- Natural like chatting with a friend
Violating any of the above is a serious error.

You are the AI assistant. Brief, direct, warm, like a caring but not indulgent friend.
Current Beijing Time: {now_str}

[User Profile]
{voice_ctx}
If it is very late (past 1am), proactively suggest they get some rest.
If they mention going out/having plans, you can remind them of the time.

[Tools]
- recall(query) — Search memory/history
- read_file(path) — Read file (USER.md/diary/etc for details)
- exec(cmd) — Execute command (including curl to Jetson: devices/music/health/reminders)
- web_search(query) — Web search
- schedule/cancel_schedule — Scheduled tasks
- write_file — Write file / diary (use when user says "note this down")
- message — Send message

When details are needed, use tools first, then answer in one spoken sentence."""


def _build_system_prompt(session_key: str = "", workspace: str = ""):
    ws = workspace or _default_user_config().get("workspace", "")
    if _is_voice_session(session_key):
        return _build_voice_prompt(ws)
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST")
    is_scheduler = session_key.startswith("scheduler")

    # --- Tier 1: Identity (always included) ---
    identity = ("You are the 7x24 office AI assistant, managing the office to empower the user.\n"
                "Your name and personality are in SOUL.md, user info is in USER.md.\n"
                "Current Beijing Time: " + now_str + "\n\n"
                "Important: the user may send multiple messages (text+image+voice mixed), the system merges them automatically."
                "If the user message looks incomplete (e.g., "help me with these images" but no images),"
                "do not ask the user to resend. Reply "Got it, I'll process everything once you're done." More messages may follow shortly.\n"
                "Channel limitation: when the user quotes/replies to a message, you can only see the new text,"
                "not the quoted original. If the user seems to be responding to a specific message, proactively ask which one.")
    parts = [identity]
    budget_used = len(identity)

    # --- Tier 1+2: Load files by priority ---
    # Prefer AGENT_CORE.md (post-split), fallback to AGENT.md (pre-split)
    agent_core = "AGENT_CORE.md" if os.path.exists(os.path.join(ws, "AGENT_CORE.md")) else "AGENT.md"
    tier12_files = ["SOUL.md", agent_core, "USER.md"]
    # PR 2.2: scheduler session skips MEMORY.md (has independent context source)
    if not is_scheduler:
        tier12_files.append("MEMORY.md")

    for filename in tier12_files:
        fpath = os.path.join(ws, filename)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                file_content = f.read()
            file_budget = _PROMPT_FILE_BUDGETS.get(filename, 4000)
            if file_budget and len(file_content) > file_budget:
                file_content = (file_content[:file_budget] +
                    "\n\n[..." + filename + " truncated. Use read_file(\'" + filename + "\') for full content.]")
            parts.append(file_content)
            budget_used += len(file_content)
        except Exception:
            pass

    # --- Tier 3: AGENT_REFERENCE.md (remaining budget) ---
    remaining = MAX_SYSTEM_PROMPT_CHARS - budget_used - 5000  # reserve for diary + retrieval + scheduler
    if remaining > 1000:
        ref_path = os.path.join(ws, "AGENT_REFERENCE.md")
        if os.path.exists(ref_path):
            try:
                ref = open(ref_path, "r", encoding="utf-8").read()
                if len(ref) > remaining:
                    ref = ref[:remaining] + "\n[...reference truncated. Use read_file(\'AGENT_REFERENCE.md\')]"
                parts.append(ref)
                budget_used += len(ref)
            except Exception:
                pass

    # --- Diary injection (scheduler skips, it has preload_context)---
    if not is_scheduler:
        today_str = datetime.now(CST).strftime("%Y-%m-%d")
        yesterday_str = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
        diary_budget = min(4000, MAX_SYSTEM_PROMPT_CHARS - budget_used - 1000)
        diary_used = 0
        for date_str in [yesterday_str, today_str]:
            diary_path = os.path.join(ws, "memory", f"{date_str}.md")
            if os.path.exists(diary_path):
                try:
                    content_d = open(diary_path, "r", encoding="utf-8").read()
                    if content_d.strip():
                        avail = diary_budget - diary_used
                        if avail < 200:
                            break
                        if len(content_d) > avail:
                            content_d = content_d[:avail] + "\n[...diary truncated]"
                        if date_str == today_str:
                            parts.append("[today's diary " + date_str + "]\n" + content_d)
                        elif date_str == yesterday_str:
                            parts.append("[Yesterday's diary " + date_str + "(has passed)]\n" + content_d)
                        else:
                            parts.append("[Diary " + date_str + "]\n" + content_d)
                        diary_used += len(content_d)
                        budget_used += len(content_d)
                except Exception:
                    pass

    prompt = "\n\n---\n\n".join(parts)

    # First-week proactive mode detection
    first_week_file = os.path.join(ws, ".first_week_until")
    if os.path.exists(first_week_file):
        try:
            end_date = open(first_week_file).read().strip()
            today = datetime.now(CST).strftime("%Y-%m-%d")
            if today <= end_date:
                parts.append(_first_week_prompt())
                prompt = "\n\n---\n\n".join(parts)
            else:
                os.remove(first_week_file)
        except Exception:
            pass

    if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
        prompt = prompt[:MAX_SYSTEM_PROMPT_CHARS] + \
            "\n\n[...system prompt truncated. Use read_file for detailed guides.]"
    log.info("[prompt] size=%d chars (budget=%d)", len(prompt), MAX_SYSTEM_PROMPT_CHARS)
    return prompt




def _build_bootstrap_prompt(user_config, contact_hint=""):
    """New user first setup: pure info collection, 1-2 rounds. Capability demos deferred to first-week mode."""
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST")
    ws = user_config.get("workspace", "")

    contact_section = ""
    if contact_hint:
        contact_section = f"""
## Contact info auto-fetched by system
The following info comes from the contact list (not from user directly). Use it naturally but do not expose the source:
{contact_hint}
- If gender info is available, use appropriate pronouns
- If a nickname is available, you can reference it but still ask the user how they prefer to be addressed
"""

    return f"""You are an AI assistant, doing first-time setup with a new user.

Current Beijing Time: {now_str}
Working directory: {ws}
{contact_section}
## Your task: quickly collect basic information

This is initial setup, target 1-2 rounds. You need to collect:
1. How to address the user (required)
2. A name for you (required)
3. User's occupation/industry (best effort)
4. User's interests/hobbies (best effort)
5. User's city (best effort)

Once info is collected, immediately use write_file to update {ws}/bootstrap_state.json. 
The system automatically detects when info collection is complete and switches to normal mode. No manual marking needed.

bootstrap_state.json format:
{{"user_name": "", "assistant_name": "", "occupation": "", "interests": [], "city": ""}}

## First round script

[Initializing] Hi! I am your AI assistant. I can help with search, scheduling, note-taking, document creation, etc.
Let's do a 30-second quick setup:
1. How should I address you?
2. Give me a name
3. What do you do / what are you interested in?

Just these three questions, then you're all set.

## Second round (if first round was incomplete)

Confirm known info, ask for missing items. Then say "Setup complete!" and update bootstrap_state.json.

## What if the user makes a direct request

If the user ignores setup questions and directly says "help me with XXX":
- Do not refuse, do not say "complete setup first"
- Directly help with what the user wants
- At the same time, naturally ask "By the way, how should I address you? Give me a name"
- Write info inferred from conversation to bootstrap_state.json (e.g., if user sends industry-related requests, infer occupation)

## Absolutely forbidden
- Do not use any information not explicitly stated by the user in this conversation
- Do not guess or fabricate the user's name before they explicitly tell you
- Do not demonstrate features, do not give tours, do not proactively show capabilities
- Do not send emoji

## Style
- Plain text, no markdown
- Short and natural, chat style
- No more than 3-4 sentences per reply
- Include [Initializing] prefix so user knows current state"""


def _auto_complete_bootstrap(user_config, notify_user=False):
    """Complete bootstrap: write USER.md, mark done, create first-week file and seed tasks.

    Call scenarios:
    1. Code layer detects required info collected in bootstrap_state → notify_user=False(silent)
    2. Turn count safety net triggered → notify_user=True(notify user)
    """
    ws = user_config["workspace"]

    # Read bootstrap_state
    bs = {}
    bs_path = os.path.join(ws, "bootstrap_state.json")
    try:
        if os.path.exists(bs_path):
            bs = json.loads(open(bs_path, encoding="utf-8").read())
    except Exception:
        pass

    user_name = bs.get("user_name", "")
    assistant_name = bs.get("assistant_name", "")
    occupation = bs.get("occupation", "")
    interests = bs.get("interests", [])
    city = bs.get("city", "")

    # Write USER.md
    lines = ["# \u7528\u6237\u4fe1\u606f\n"]
    lines.append("- \u79f0\u547c\uff1a%s" % (user_name or "\u5f85\u4e86\u89e3"))
    lines.append("- AI\u52a9\u7406\u540d\u5b57\uff1a%s" % (assistant_name or "\u5c0f\u52a9\u7406"))
    lines.append("- \u804c\u4e1a\uff1a%s" % (occupation or "\u5f85\u4e86\u89e3"))
    lines.append("- \u5174\u8da3\uff1a%s" % (", ".join(interests) if interests else "\u5f85\u4e86\u89e3"))
    if city:
        lines.append("- \u57ce\u5e02\uff1a%s" % city)
    if not user_name or not assistant_name:
        lines.append("- \u5907\u6ce8\uff1a\u5f15\u5bfc\u81ea\u52a8\u5b8c\u6210\uff0c\u90e8\u5206\u4fe1\u606f\u5f85\u8865\u5168")
    with open(os.path.join(ws, "USER.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # Mark as complete
    open(os.path.join(ws, ".bootstrapped"), "w").close()

    # Create first_week marker
    end_date = (datetime.now(CST) + timedelta(days=7)).strftime("%Y-%m-%d")
    with open(os.path.join(ws, ".first_week_until"), "w") as f:
        f.write(end_date)

    # Clean up temp files
    for f_name in ["bootstrap_state.json", ".bootstrap_turns"]:
        p = os.path.join(ws, f_name)
        if os.path.exists(p):
            os.remove(p)

    # Create seed tasks
    import scheduler
    owner_id = user_config.get("owner_id", "")
    display_name = user_name or "\u7528\u6237"

    # General: evening greeting
    scheduler.add({
        "name": "[\u9996\u5468]\u665a\u95f4\u95ee\u5019",
        "message": "\u7528 message \u5de5\u5177\u95ee\u5019%s\uff0c\u95ee\u95ee\u4eca\u5929\u600e\u4e48\u6837\u3002\u4fdd\u6301\u7b80\u77ed\u81ea\u7136\u3002" % display_name,
        "cron_expr": "0 20 * * *",
        "once": False,
        "owner_id": owner_id
    })
    # Has occupation -> industry brief
    if occupation and occupation != "\u5f85\u4e86\u89e3":
        scheduler.add({
            "name": "[\u9996\u5468]\u884c\u4e1a\u65e9\u62a5",
            "message": "\u7528 web_search \u641c\u7d22%s\u6700\u65b0\u52a8\u6001\uff0c\u7528 send_link \u53d1\u7ed9\u7528\u6237\uff0c\u7b80\u77ed\u70b9\u8bc4\u4e00\u53e5" % occupation,
            "cron_expr": "30 9 * * *",
            "once": False,
            "owner_id": owner_id
        })
    # Cleanup task (7 days later)
    scheduler.add({
        "name": "[\u9996\u5468]\u6e05\u7406",
        "message": "\u9996\u5468\u4f53\u9a8c\u671f\u7ed3\u675f\u3002\u7528 list_schedules \u627e\u5230\u6240\u6709\u540d\u79f0\u4ee5[\u9996\u5468]\u5f00\u5934\u7684\u5b9a\u65f6\u4efb\u52a1\uff0c\u7528 remove_schedule \u9010\u4e00\u5220\u9664\u3002\u7136\u540e\u7528 message \u544a\u8bc9\u7528\u6237\uff1a\u52a9\u7406\u5df2\u9002\u5e94\u4f60\u7684\u4f7f\u7528\u4e60\u60ef\uff0c\u9996\u5468\u5c55\u793a\u4efb\u52a1\u5df2\u81ea\u52a8\u5173\u95ed\u3002\u5982\u679c\u89c9\u5f97\u54ea\u4e2a\u6709\u7528\u53ef\u4ee5\u544a\u8bc9\u6211\u91cd\u65b0\u5f00\u3002\u6700\u540e exec(command='rm -f %s/.first_week_until')" % ws,
        "delay_seconds": 604800,
        "owner_id": owner_id
    })
    # Infrastructure: daily_archive (permanent, no [first_week] prefix)
    scheduler.add({
        "name": "daily_archive",
        "message": "Call archive tool to perform daily archiving",
        "cron_expr": "0 1 * * *",
        "once": False,
        "owner_id": owner_id
    })
    # Infrastructure: daily_diary (permanent, no [first_week] prefix)
    scheduler.add({
        "name": "daily_diary",
        "message": "Review today's conversations with user, write key points to memory/ today's date md file",
        "cron_expr": "0 23 * * *",
        "once": False,
        "owner_id": owner_id
    })

    # Notify user (when safety net triggered)
    if notify_user and owner_id:
        try:
            import messaging
            msg = "\u521d\u59cb\u5316\u5df2\u81ea\u52a8\u5b8c\u6210\uff0c\u73b0\u5728\u53ef\u4ee5\u6b63\u5e38\u4f7f\u7528\u6240\u6709\u529f\u80fd\u4e86\u3002"
            if not user_name:
                msg += "\u5bf9\u4e86\uff0c\u8fd8\u4e0d\u77e5\u9053\u600e\u4e48\u79f0\u547c\u4f60\uff0c\u65b9\u4fbf\u544a\u8bc9\u6211\u5417\uff1f"
            messaging.send_text(owner_id, msg)
        except Exception as e:
            log.error("[bootstrap] notify user failed: %s", e)

    log.info("[bootstrap] completed for %s (user=%s, assistant=%s, trigger=%s)",
             user_config.get("name", "?"), user_name, assistant_name,
             "safety_net" if notify_user else "condition_met")


def _check_bootstrap_ready(workspace):
    """Check if required info has been collected in bootstrap_state.json.

    Required: both user_name and assistant_name are non-empty.
    Returns True when bootstrap can be auto-completed.
    """
    bs_path = os.path.join(workspace, "bootstrap_state.json")
    try:
        if not os.path.exists(bs_path):
            return False
        bs = json.loads(open(bs_path, encoding="utf-8").read())
        return bool(bs.get("user_name")) and bool(bs.get("assistant_name"))
    except Exception:
        return False


def _first_week_prompt():
    """First-week proactive mode system prompt appendix."""
    return """## \u9996\u5468\u4e3b\u52a8\u6a21\u5f0f\uff08\u81ea\u52a8\u6ce8\u5165\uff0c7\u5929\u540e\u81ea\u52a8\u5173\u95ed\uff09

\u4f60\u6b63\u5904\u4e8e\u65b0\u7528\u6237\u7684\u9996\u5468\u4f53\u9a8c\u671f\u3002\u76ee\u6807\uff1a\u8ba9\u7528\u6237\u611f\u53d7\u5230\u201c\u8fd9\u662f\u771f\u52a9\u7406\uff0c\u4e0d\u662f\u804a\u5929\u673a\u5668\u4eba\u201d\u3002

### \u4e3b\u52a8\u884c\u4e3a\u6307\u5f15
- \u5bf9\u8bdd\u4e2d\u53d1\u73b0\u7528\u6237\u65b0\u4fe1\u606f\uff08\u4e60\u60ef\u3001\u504f\u597d\u3001\u91cd\u8981\u65e5\u671f\u3001\u57ce\u5e02\uff09\uff0c\u4e3b\u52a8\u7528 write_file \u66f4\u65b0 USER.md
- \u5bf9\u8bdd\u7ed3\u675f\u65f6\uff0c\u8003\u8651\u662f\u5426\u503c\u5f97\u521b\u5efa\u4e00\u4e2a\u8ddf\u8fdb\u4efb\u52a1\uff08\u7528 schedule\uff09\uff0c\u4f46\u4e0d\u8981\u6bcf\u6b21\u90fd\u521b\u5efa
- \u4f7f\u7528\u4e30\u5bcc\u7684\u6d88\u606f\u7c7b\u578b\uff1asend_link \u53d1\u6709\u7528\u94fe\u63a5\u3001send_location \u53d1\u4f4d\u7f6e\u63a8\u8350\uff0c\u4e0d\u8981\u603b\u662f\u7eaf\u6587\u672c
- \u5982\u679c\u7528\u6237\u63d0\u5230\u67d0\u4e2a\u65e5\u671f\uff08\u751f\u65e5\u3001\u622a\u6b62\u65e5\u671f\u3001\u7ea6\u4f1a\uff09\uff0c\u4e3b\u52a8\u7528 schedule \u521b\u5efa\u63d0\u9192

### \u9891\u7387\u63a7\u5236
- \u5b9a\u65f6\u4efb\u52a1\u603b\u5171\u6bcf\u5929 2-3 \u6761\u6d88\u606f\uff0c\u4e0d\u8981\u8f70\u70b8
- \u7528\u6237\u8bf4\u201c\u522b\u53d1\u4e86/\u592a\u591a\u4e86/\u592a\u70e6\u4e86\u201d \u2192 \u7acb\u5373\u7528 list_schedules \u627e\u5230\u6240\u6709[\u9996\u5468]\u5f00\u5934\u7684\u4efb\u52a1\uff0c\u7528 remove_schedule \u9010\u4e00\u5220\u9664\uff0c\u5e76\u544a\u8bc9\u7528\u6237\u5df2\u5173\u95ed
- \u91cd\u8981\u7684\u900f\u660e\u544a\u77e5\uff08\u201c\u6211\u5e2e\u4f60\u8bbe\u4e86\u4e2aXX\u63d0\u9192\u201d\uff09\uff0c\u5c0f\u60ca\u559c\u53ef\u4ee5\u9759\u9ed8\u521b\u5efa

### \u79cd\u5b50\u4efb\u52a1\u8bf4\u660e
\u7cfb\u7edf\u5df2\u521b\u5efa\u4e86\u4e00\u4e9b[\u9996\u5468]\u524d\u7f00\u7684\u5c55\u793a\u4efb\u52a1\uff0c7\u5929\u540e\u81ea\u52a8\u6e05\u7406\u3002\u7528\u6237\u5acc\u591a\u5c31\u7acb\u5373\u5220\u9664\u3002"""


# ============================================================
#  Commitment Verification (prevent LLM verbal promises without tool execution)
# ============================================================

import re as _re

_COMMITMENT_CHECKS = [
    {
        "patterns": [
            r"(?:reminder|scheduled|alarm|task).*(?:has_set|arranged|created|set_up|configured|established)",
            r"(?:has_set|arranged|created|set_up|configured|established).*(?:reminder|scheduled|alarm|task)",
        ],
        "tool": "schedule",
        "nudge": "[system] You claimed to have set a reminder/schedule, but you did not call the schedule tool. Call the schedule tool immediately to complete the setup, do not just reply verbally.",
    },
    {
        # User wants to modify existing scheduled task behavior
        "patterns": [
            r"(?:from_now_on|from_tomorrow|next_time).*(?:should_include|should_add|change_to|add|adjust)",
            r"(?:briefing|research|news|weather|daily_report|report).*(?:format|content|add|contain|modify)",
            r"(?:received|understood|ok|got_it).*(?:from_tomorrow|from_now_on|next_time).*(?:start|execute|adjust)",
        ],
        "tool": "schedule",
        "nudge": "[system] User asked to modify an existing scheduled task behavior. Verbal agreement is useless--conversation memory will be lost, jobs.json is the source of truth. You must NOW use remove_schedule to delete the old task, then use schedule to rebuild it with the new requirements in the message field.",
    },
    {
        # Said "recorded/archived" but did not call write_file (verbal recording != persisted)
        "patterns": [
            r"(?:recorded|recorded|has_been_archived|inspiration_archive|has_been_noted)",
            r"(?:write_down|note_this|help_me_note).*(?:ok|received|recorded)",
        ],
        "tool": "write_file",
        "nudge": "[system] You claimed to have recorded/archived, but you did not call write_file. Writing in the conversation reply does not count--sessions get truncated. Immediately use write_file to write to memory/today's-date.md or memory/MEMORY.md.",
    },
]


def _check_unfulfilled_commitments(reply_text, tools_called):
    """Check if LLM reply contains unfulfilled commitments. Returns nudge message or None."""
    for check in _COMMITMENT_CHECKS:
        if check["tool"] in tools_called:
            continue
        # If read_file was called in current round, it means showing/querying content,
        # mentions of "recorded/noted here" describe existing records, not new write commitments
        if check["tool"] == "write_file" and "read_file" in tools_called:
            continue
        for pat in check["patterns"]:
            if _re.search(pat, reply_text):
                log.warning("[commitment] detected unfulfilled: claimed %s but never called %s",
                            pat, check["tool"])
                return check["nudge"]
    return None

# ============================================================
#  Tool Use Loop (Core)
# ============================================================

_chat_locks = {}
_chat_locks_lock = threading.Lock()


def _get_chat_lock(session_key):
    with _chat_locks_lock:
        if session_key not in _chat_locks:
            _chat_locks[session_key] = threading.Lock()
        return _chat_locks[session_key]


def _prepare_chat(user_msg, session_key, images, user_config, group_ctx=None):
    """Shared context preparation logic.

    Returns (uc, messages, system_prompt, tool_defs, ctx, provider, provider_key)
    """
    uc = user_config or _default_user_config()
    messages = _load_session(session_key, user_id=uc.get("owner_id"))

    # voice session historylimit
    if _is_voice_session(session_key) and len(messages) > VOICE_SESSION_MESSAGES:
        messages = messages[-VOICE_SESSION_MESSAGES:]

    # Build user message(maycontainimage)
    user_message = _build_user_message(user_msg, images)
    messages.append(user_message)

    # Bootstrap Check: new_userfirstconversationuse onboarding flow
    bootstrap_marker = os.path.join(uc["workspace"], ".bootstrapped")
    is_bootstrap = not os.path.exists(bootstrap_marker)

    if is_bootstrap:
        # Condition check: required info collected -> silently complete bootstrap
        if _check_bootstrap_ready(uc["workspace"]):
            log.info("[bootstrap] condition met, auto-completing for %s", uc.get("name", "?"))
            _auto_complete_bootstrap(uc, notify_user=False)
            is_bootstrap = False
        else:
            # Turn count safety net: independent counter file (not touched by LLM)
            turns_file = os.path.join(uc["workspace"], ".bootstrap_turns")
            turn_count = 0
            try:
                if os.path.exists(turns_file):
                    turn_count = int(open(turns_file).read().strip())
            except Exception:
                pass
            turn_count += 1
            with open(turns_file, "w") as f:
                f.write(str(turn_count))

            MAX_BOOTSTRAP_TURNS = 8
            if turn_count >= MAX_BOOTSTRAP_TURNS:
                log.warning("[bootstrap] safety net at %d turns for %s", turn_count, uc.get("name", "?"))
                _auto_complete_bootstrap(uc, notify_user=True)
                is_bootstrap = False

    # Infrastructure: auto-compact MEMORY.md (migrate large sections to independent files when over threshold)
    try:
        import memory as _mem
        _mem.auto_compact(uc.get("workspace", ""))
        _mem.auto_compact_guides(uc.get("workspace", ""))
    except Exception as e:
        log.error("[chat] auto_compact error: %s" % e)

    if is_bootstrap:
        # Auto-fetch contact info (gender/nickname), inject into bootstrap context
        _contact_hint = ""
        try:
            import messaging as _qw
            contacts = _msg.get_contact_info([uc["owner_id"]])
            if contacts:
                c = contacts[0]
                gender_map = {1: "male", 2: "female"}
                gender = gender_map.get(c.get("gender"), "")
                nickname = c.get("nickname", "")
                parts = []
                if nickname:
                    parts.append("Nickname: " + nickname)
                if gender:
                    parts.append("Gender: " + gender)
                if parts:
                    _contact_hint = "\n".join(parts)
                    log.info("[chat] bootstrap contact info: %s" % _contact_hint)
        except Exception as e:
            log.error("[chat] bootstrap contact fetch error: %s" % e)
        system_prompt = _build_bootstrap_prompt(uc, contact_hint=_contact_hint)
        log.info("[chat] bootstrap mode for %s" % uc.get("name", "?"))
    else:
        system_prompt = _build_system_prompt(session_key, uc.get("workspace", ""))

        # Hook: scheduler session inject reminder
        if session_key.startswith("scheduler"):
            system_prompt += "\n\n---\n\n⚠️ [Scheduled Task Mode — MUST follow]\nYou are being triggered by the scheduled task system, not a user conversation.\n\nAbsolutely forbidden:\n- Do not use schedule to create new scheduled tasks\n- Do not reply with "OK, I've set it up" or similar\n\nYou MUST:\n1. Execute the operation described in the task\n2. Use the message tool to send results to the user\n\nNot calling message = user receives nothing."
        # Hook 1: Retrieve related memory, inject into system prompt
        if not _is_voice_session(session_key):
            try:
                import memory as mem_mod
                query_text = user_msg if isinstance(user_msg, str) else ""
                mem_context = mem_mod.retrieve(query_text, session_key, user_id=uc.get("owner_id"))
                if mem_context:
                    system_prompt += "\n\n---\n\n" + mem_context
            except Exception as e:
                log.error("[chat] memory retrieve error: %s" % e)

        # Hook: Cross-session context bridging (scheduler session itself does not inject)
        if not session_key.startswith("scheduler"):
            sched_ctx = _get_recent_scheduler_context(uc.get("owner_id"))
            if sched_ctx:
                system_prompt += "\n\n---\n\n" + sched_ctx
                log.info("[chat] injected scheduler context (%d chars)" % len(sched_ctx))


        # Hook: AI Mirror — Future Self statusinject
        if not _is_voice_session(session_key) and not session_key.startswith("scheduler"):
            _user_text = user_msg if isinstance(user_msg, str) else ""
            if _re.search(r"exit|end_conversation|back_to_normal|exit", _user_text):
                try:
                    from tools_mirror import deactivate_future_self
                    if deactivate_future_self(uc.get("workspace", "")):
                        log.info("[chat] future_self deactivated by user")
                except Exception:
                    pass
            else:
                try:
                    from tools_mirror import check_future_self_state
                    _fs_prompt = check_future_self_state(uc.get("workspace", ""))
                    if _fs_prompt:
                        system_prompt += _fs_prompt
                        log.info("[chat] future_self mode active, injected persona prompt")
                except Exception:
                    pass

    # PR 2.1: filter tools by context profile (voice ~15, scheduler ~25, group ~20, default all)
    from tools_base import get_filtered_definitions
    is_group = bool(group_ctx)
    tool_defs = get_filtered_definitions(session_key, is_group=is_group)
    ctx = {"owner_id": uc["owner_id"], "workspace": uc["workspace"], "session_key": session_key}
    if group_ctx:
        ctx["is_group"] = True
        ctx["group_id"] = group_ctx["group_id"]
        system_prompt += (
            "\n\n---\n[Group Chat Mode]\n"
            "- You are in a group chat. Message format is [Name] content\n"
            "- Only answer questions directed at you, be concise\n"
            "- Do not add [the AI assistant] prefix, speakcontent\n"
            "- Do not use dangerous tools like exec/edit_file\n"
            "- After generating files, use send_file to send to the group\n"
        )
        if group_ctx.get("recent_context"):
            system_prompt += "\n" + group_ctx["recent_context"]

    # Multi-tenant: userspecified model takes priority (voice session excluded)
    if uc.get("model") and uc["model"] in _config.get("providers", {}) and not _is_voice_session(session_key):
        provider_key = uc["model"]
    else:
        provider_key = _select_model(user_msg, session_key)
    provider = _get_provider(provider_key)

    return uc, messages, system_prompt, tool_defs, ctx, provider, provider_key


def _execute_tools(tool_calls, ctx, tools_called, messages, tool_results=None):
    """Execute tool call list, append results to messages, update tools_called and tool_results."""
    for tc in tool_calls:
        try:
            func_args = json.loads(tc["function"]["arguments"])
        except json.JSONDecodeError:
            func_args = {}
        tool_name = tc["function"]["name"]
        try:
            result = tools.execute(tool_name, func_args, ctx)
        except Exception as e:
            log.error("[chat] tool %s crashed: %s" % (tool_name, e), exc_info=True)
            result = f"[error] toolexecutefailed: {e}"
        tools_called.add(tool_name)
        if tool_results is not None:
            tool_results[tool_name] = str(result)
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})


def chat(user_msg, session_key, images=None, user_config=None, group_ctx=None):
    """Tool use loop entry. Thread-safe (same session serialized)."""
    lock = _get_chat_lock(session_key)
    with lock:
        return _chat_inner(user_msg, session_key, images, user_config, group_ctx)




def _has_structured_data(text):
    lines = text.strip().splitlines()
    data_lines = 0
    for line in lines:
        if _re.search(r'[\d.]+\s*[CNY%￥$]|[\d.]+\s*/\s*[\d.]+', line):
            data_lines += 1
    return data_lines >= 3

def _chat_inner(user_msg, session_key, images=None, user_config=None, group_ctx=None):
    uc, messages, system_prompt, tool_defs, ctx, provider, provider_key = \
        _prepare_chat(user_msg, session_key, images, user_config, group_ctx)
    log.info(f"[router] {session_key} -> {provider_key}")

    tools_called = set()
    tool_results = {}  # name -> last result (for nudge)
    import nudge as _nudge_mod
    _nudge_mod.reset()

    for _ in range(20):
        api_messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            response = _call_llm(api_messages, tool_defs, provider)
        except Exception as e:
            # Fallback: On primary model failure, try fallback chain models sequentially
            fallback_chain = _config.get("fallback", [])
            fallback_ok = False
            for fb_key in fallback_chain:
                if fb_key == provider_key or fb_key not in _config.get("providers", {}):
                    continue
                log.warning("[fallback] %s failed (%s), trying %s", provider_key, e, fb_key)
                try:
                    fb_provider = _get_provider(fb_key)
                    response = _call_llm(api_messages, tool_defs, fb_provider)
                    provider, provider_key = fb_provider, fb_key
                    log.info("[fallback] %s succeeded", fb_key)
                    fallback_ok = True
                    break
                except Exception as fb_e:
                    log.warning("[fallback] %s also failed: %s", fb_key, fb_e)
                    continue
            if not fallback_ok:
                log.error(f"[chat] LLM error (all providers exhausted): {e}", exc_info=True)
                _save_session(session_key, messages, uc.get("owner_id"))
                log.error("[chat] all providers exhausted, detail: %s", e)
                return "Sorry, AI service is temporarily unavailable. Please try again later."

        msg = response["choices"][0]["message"]
        messages.append(_serialize_assistant_msg(msg))

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            reply_text = msg.get("content", "")
            # DeepSeek empty response fallback to Kimi
            if not reply_text.strip() and provider_key != _config["default"]:
                log.info("[router] Empty response, retrying Kimi K2.5")
                provider = _get_provider(_config["default"])
                provider_key = _config["default"]
                messages.pop()  # Remove empty reply
                continue
            # PR 4.1: Unified nudge system (replacing _check_unfulfilled_commitments + render_page nudge)
            nudge_msg = _nudge_mod.check_nudges(tools_called, reply_text, tool_results)
            if nudge_msg:
                log.info("[nudge] injecting: %s", nudge_msg[:80])
                messages.pop()  # Remove current reply, let LLM re-run
                messages.append({"role": "user", "content": nudge_msg})
                continue
            # Strip <think>...</think> tags (some models like MiniMax return thinking process)
            if "<think>" in reply_text:
                reply_text = _re.sub(r"<think>[\s\S]*?</think>\s*", "", reply_text).strip()
            _save_session(session_key, messages, uc.get("owner_id"))
            return reply_text

        _execute_tools(tool_calls, ctx, tools_called, messages, tool_results)

    _save_session(session_key, messages, uc.get("owner_id"))
    return "Processing timeout, please try again later."


# ============================================================
#  Streaming LLM API call + Tool Use Loop
# ============================================================

def _call_llm_stream(messages, tool_defs, provider=None):
    """Stream call to LLM. Parse SSE line by line, yield (event_type, data).

    event_type:
      "content"    -> data is str (text fragment)
      "tool_calls" -> data is list[dict] (accumulated complete tool_calls)
    """
    provider = provider or _get_provider()
    url = provider["api_base"].rstrip("/") + "/chat/completions"

    body = {
        "model": provider["model"],
        "messages": messages,
        "stream": True,
        "max_tokens": provider.get("max_tokens", 8192),
    }
    if tool_defs:
        body["tools"] = tool_defs
    extra = provider.get("extra_body", {})
    body.update(extra)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider['api_key']}",
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    timeout = provider.get("timeout", 120)

    # 429 retry (consistent with _call_llm)
    max_retries = 4
    backoff_delays = [3, 6, 12, 24]
    resp = None
    for attempt in range(max_retries + 1):
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                delay = backoff_delays[attempt]
                log.warning("[llm_stream] HTTP 429, retry %d/%d after %ds", attempt + 1, max_retries, delay)
                import time
                time.sleep(delay)
                continue
            raise
    if resp is None:
        raise RuntimeError("stream: all retries exhausted")

    # accumulate tool_calls (index -> {id, function: {name, arguments}})
    accumulated_tc = {}

    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:"):
                continue
            json_str = line[5:].strip()
            if not json_str or json_str == "[DONE]":
                continue
            try:
                chunk = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})

            # tool_calls accumulate
            if delta.get("tool_calls"):
                for tc in delta["tool_calls"]:
                    idx = tc["index"]
                    if idx not in accumulated_tc:
                        accumulated_tc[idx] = {
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {"name": "", "arguments": ""}
                        }
                    if tc.get("id"):
                        accumulated_tc[idx]["id"] = tc["id"]
                    fn = tc.get("function", {})
                    if fn.get("name"):
                        accumulated_tc[idx]["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        accumulated_tc[idx]["function"]["arguments"] += fn["arguments"]

            # content yield
            content = delta.get("content", "")
            if content:
                yield ("content", content)
    finally:
        resp.close()

    if accumulated_tc:
        yield ("tool_calls", list(accumulated_tc.values()))


def chat_stream(user_msg, session_key, images=None, user_config=None):
    """Streaming version of chat(). Yields text fragments (str).

    Inside tool loop: receive tool_calls -> execute synchronously -> next round
    Final reply: receive content -> yield per fragment
    """
    lock = _get_chat_lock(session_key)
    with lock:
        yield from _chat_stream_inner(user_msg, session_key, images, user_config)


def _chat_stream_inner(user_msg, session_key, images=None, user_config=None):
    uc, messages, system_prompt, tool_defs, ctx, provider, provider_key = \
        _prepare_chat(user_msg, session_key, images, user_config)
    log.info(f"[router] stream {session_key} -> {provider_key}")

    tools_called = set()

    for iteration in range(20):
        api_messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            got_tools = False
            round_content = ""
            tool_calls_data = None

            for event_type, data in _call_llm_stream(api_messages, tool_defs, provider):
                if event_type == "content":
                    round_content += data
                    yield data  # yield per token
                elif event_type == "tool_calls":
                    got_tools = True
                    tool_calls_data = data

            if got_tools and tool_calls_data:
                # Serialize assistant message (with tool_calls)
                assistant_msg = {"role": "assistant", "content": round_content or None,
                                 "tool_calls": tool_calls_data}
                if "reasoning_content" not in assistant_msg:
                    assistant_msg["reasoning_content"] = "ok"
                messages.append(assistant_msg)
                _execute_tools(tool_calls_data, ctx, tools_called, messages)
                continue  # next round tool loop

            # Final reply (no tool_calls)
            messages.append({"role": "assistant", "content": round_content})
            # Commitment check: log only, do not inject messages to disrupt conversation
            _check_unfulfilled_commitments(round_content, tools_called)
            break

        except Exception as e:
            log.error(f"[chat_stream] LLM error: {e}", exc_info=True)
            _save_session(session_key, messages, uc.get("owner_id"))
            yield "Sorry, AI service is temporarily unavailable. Please try again later."
            return

    _save_session(session_key, messages, uc.get("owner_id"))
