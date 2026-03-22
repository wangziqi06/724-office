"""
LLM Calls + Tool Use Loop + Session Management

Core loop: user message -> LLM -> tool calls -> execute -> LLM -> ... -> final reply
Supports multimodal: images via image_url (base64) to LLM.
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
_workspace = ""
_owner_id = ""
_sessions_dir = ""
MAX_SESSION_MESSAGES = 40


def init(models_config, workspace, owner_id, sessions_dir):
    global _config, _workspace, _owner_id, _sessions_dir
    _config = models_config
    _workspace = workspace
    _owner_id = owner_id
    _sessions_dir = sessions_dir


# ============================================================
#  LLM API Call
# ============================================================

def _get_provider():
    default_name = _config["default"]
    return _config["providers"][default_name]


def _is_minimax_provider(provider):
    """Check if the provider is MiniMax based on api_base URL."""
    api_base = provider.get("api_base", "")
    return "minimax" in api_base.lower()


def _strip_think_tags(text):
    """Strip <think>...</think> reasoning tags from model output.

    MiniMax M2.5/M2.7 models may include chain-of-thought reasoning
    wrapped in <think> tags. These should be removed from user-facing
    responses while preserving the actual content.
    """
    if not text or "<think>" not in text:
        return text
    import re
    return re.sub(r"<think>[\s\S]*?</think>\s*", "", text).strip()


def _call_llm(messages, tool_defs):
    provider = _get_provider()
    url = provider["api_base"].rstrip("/") + "/chat/completions"

    body = {
        "model": provider["model"],
        "messages": messages,
        "tools": tool_defs,
        "max_tokens": provider.get("max_tokens", 8192),
    }

    # MiniMax temperature: clamp to [0, 1.0] (MiniMax max is 1.0)
    if _is_minimax_provider(provider):
        temp = body.get("temperature")
        if temp is not None and temp > 1.0:
            body["temperature"] = 1.0

    extra = provider.get("extra_body", {})
    body.update(extra)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider['api_key']}",
    }

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    timeout = provider.get("timeout", 120)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Read response body for debugging 400/422 errors
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        log.error("[llm] HTTP %d: %s" % (e.code, body_text))
        raise

    # Strip think tags from MiniMax M2.5/M2.7 responses
    if _is_minimax_provider(provider):
        try:
            content = result["choices"][0]["message"].get("content", "")
            if content:
                result["choices"][0]["message"]["content"] = _strip_think_tags(content)
        except (KeyError, IndexError):
            pass

    return result


# ============================================================
#  Session Management
# ============================================================

def _session_path(session_key):
    safe = session_key.replace("/", "_").replace(":", "_").replace("\\", "_")
    return os.path.join(_sessions_dir, f"{safe}.json")


def _load_session(session_key):
    path = _session_path(session_key)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                messages = json.load(f)
            if len(messages) > MAX_SESSION_MESSAGES:
                evicted = messages[:-MAX_SESSION_MESSAGES]
                messages = messages[-MAX_SESSION_MESSAGES:]
                # Compress evicted messages into long-term memory
                try:
                    import memory as mem_mod
                    mem_mod.compress_async(evicted, session_key)
                except Exception as e:
                    log.error("[session] load-time compress error: %s" % e)
            # Truncation may leave orphan tool messages at the start (no matching
            # assistant + tool_calls), or assistant with tool_calls but truncated
            # tool results. Some LLMs require valid message sequences or return 400.
            # Skip to first user message.
            while messages and messages[0].get("role") not in ("user", "system"):
                messages.pop(0)
            return messages
        except Exception:
            return []
    return []


def _strip_images_for_storage(messages):
    """Before saving session, replace image_url in multimodal content with [image] text.

    Reason: some LLMs don't accept image_url format in history messages (400 error).
    Images only need to be sent to LLM in current turn; text markers suffice for history.
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


def _save_session(session_key, messages):
    if len(messages) > MAX_SESSION_MESSAGES:
        evicted = messages[:-MAX_SESSION_MESSAGES]
        messages = messages[-MAX_SESSION_MESSAGES:]
        # Hook 2: async compress evicted messages into long-term memory
        try:
            import memory as mem_mod
            mem_mod.compress_async(evicted, session_key)
        except Exception as e:
            log.error("[session] memory compress error: %s" % e)
    messages = _strip_images_for_storage(messages)
    path = _session_path(session_key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=None)
    except Exception as e:
        log.error(f"[session] save error: {e}")


def _serialize_assistant_msg(msg_data):
    """Serialize assistant message. Preserve reasoning_content for compatible LLMs."""
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

def _image_to_base64_url(image_path):
    """Read image file, return data URI"""
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
    mime = mime_map.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


def _build_user_message(text, images=None):
    """Build user message, supports plain text or multimodal (text + images)"""
    if not images:
        return {"role": "user", "content": text}

    content = []
    if text:
        content.append({"type": "text", "text": text})
    for img_path in images:
        if os.path.exists(img_path):
            try:
                data_url = _image_to_base64_url(img_path)
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


def _get_recent_scheduler_context():
    """Read recent scheduler session output for cross-session context bridging.

    Scheduled tasks (e.g. self-check reports) send messages to the user via
    the scheduler session, but user replies go through the DM session.
    This function extracts recent (2h) scheduler output and injects it
    into the system prompt so the agent knows what it just sent.
    """
    sched_path = _session_path("scheduler")
    if not os.path.exists(sched_path):
        return ""

    # Freshness check: skip if file modified more than 2 hours ago
    mtime = os.path.getmtime(sched_path)
    now_ts = datetime.now(CST).timestamp()
    if now_ts - mtime > 7200:  # 2 hours
        return ""

    try:
        with open(sched_path, "r", encoding="utf-8") as f:
            msgs = json.load(f)
    except Exception:
        return ""

    if not msgs:
        return ""

    # Find the last message tool call content (what was actually sent to user)
    sent_content = None
    for msg in reversed(msgs):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                if tc.get("function", {}).get("name") == "message":
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        sent_content = args.get("content", "")
                    except (json.JSONDecodeError, KeyError):
                        pass
                    if sent_content:
                        break
        if sent_content:
            break

    if not sent_content:
        return ""

    # Truncate overly long content
    if len(sent_content) > 800:
        sent_content = sent_content[:800] + "\n...(truncated)"

    from_time = datetime.fromtimestamp(mtime, CST).strftime("%H:%M")
    return (
        f"[Agent recently sent via scheduled task ({from_time}), user may be replying to this]\n"
        f"{sent_content}"
    )


def _build_system_prompt():
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST")
    parts = [f"You are the user's private AI assistant.\nCurrent time: {now_str}\n"]
    for filename in ["SOUL.md", "AGENT.md", "USER.md"]:
        fpath = os.path.join(_workspace, filename)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    parts.append(f.read())
            except Exception:
                pass
    return "\n\n---\n\n".join(parts)


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


def chat(user_msg, session_key, images=None):
    """Tool use loop entry point. Thread-safe (same session serialized)."""
    lock = _get_chat_lock(session_key)
    with lock:
        return _chat_inner(user_msg, session_key, images)


def _chat_inner(user_msg, session_key, images=None):
    import time as _time
    t0 = _time.monotonic()

    messages = _load_session(session_key)

    # Build user message (may include images)
    user_message = _build_user_message(user_msg, images)
    messages.append(user_message)

    system_prompt = _build_system_prompt()

    # Hook 1: retrieve relevant memories, inject into system prompt
    try:
        import memory as mem_mod
        # Extract plain text for retrieval (multimodal messages take text part)
        query_text = user_msg if isinstance(user_msg, str) else ""
        mem_context = mem_mod.retrieve(query_text, session_key)
        if mem_context:
            system_prompt += "\n\n---\n\n" + mem_context
    except Exception as e:
        log.error("[chat] memory retrieve error: %s" % e)

    # Hook: cross-session context bridge - let DM session see recent scheduled task output
    if session_key != "scheduler":
        sched_ctx = _get_recent_scheduler_context()
        if sched_ctx:
            system_prompt += "\n\n---\n\n" + sched_ctx
            log.info("[chat] injected scheduler context (%d chars)" % len(sched_ctx))

    t_prep = (_time.monotonic() - t0) * 1000

    tool_defs = tools.get_definitions()
    ctx = {"owner_id": _owner_id, "workspace": _workspace, "session_key": session_key}
    max_iterations = 20
    t_llm_total = 0
    tool_count = 0

    for _ in range(max_iterations):
        api_messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            t_llm_s = _time.monotonic()
            response = _call_llm(api_messages, tool_defs)
            t_llm_total += (_time.monotonic() - t_llm_s) * 1000
        except Exception as e:
            log.error(f"[chat] LLM error: {e}", exc_info=True)
            _save_session(session_key, messages)
            return f"Sorry, AI service temporarily unavailable: {e}"

        msg = response["choices"][0]["message"]
        messages.append(_serialize_assistant_msg(msg))

        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            _save_session(session_key, messages)
            log.info("[perf] sync | prep=%.0fms | llm=%.0fms | tools=%d | total=%.0fms",
                     t_prep, t_llm_total, tool_count, (_time.monotonic() - t0) * 1000)
            return msg.get("content", "")

        for tc in tool_calls:
            tool_count += 1
            try:
                func_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                func_args = {}
            try:
                result = tools.execute(tc["function"]["name"], func_args, ctx)
            except Exception as e:
                log.error("[chat] tool %s crashed: %s" % (tc["function"]["name"], e), exc_info=True)
                result = f"[error] tool execution failed: {e}"
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})

    _save_session(session_key, messages)
    log.info("[perf] sync | prep=%.0fms | llm=%.0fms | tools=%d | total=%.0fms (max_iter)",
             t_prep, t_llm_total, tool_count, (_time.monotonic() - t0) * 1000)
    return "Processing timed out, please try again later."
