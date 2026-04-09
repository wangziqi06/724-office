"""
Nudge Registry — Detect when LLM "has tools but doesn't use them", auto-inject hints

Code-level implementation of Principle 2: don't rely on prompts to constrain behavior,
use structure to make errors impossible.

When the LLM searched for results but forgot to send a location card, or said "noted"
but didn't call write_file, the system auto-injects a hint message so the LLM runs
one more iteration to execute.

Usage (at end of tool loop in llm.py):
    nudge_msg = check_nudges(tools_called, reply_text, tool_results)
    if nudge_msg:
        messages.append({"role": "user", "content": nudge_msg})
        continue  # Let LLM run one more iteration
"""

import logging
import re

log = logging.getLogger("agent")

# ============================================================
#  Nudge Rule Registry
# ============================================================

_nudge_rules = []


def register(name, trigger_fn, message, max_fires=1):
    """Register a nudge rule.

    trigger_fn(ctx) -> bool
      ctx = {"tools_called": set, "reply_text": str, "tool_results": dict}
    message: hint text injected to LLM
    max_fires: max triggers per chat session (prevents loops)
    """
    _nudge_rules.append({
        "name": name,
        "trigger": trigger_fn,
        "message": message,
        "max_fires": max_fires,
    })


# ============================================================
#  Built-in Nudge Rules
# ============================================================

def _search_nearby_no_location(ctx):
    """search_nearby returned results but LLM didn't call send_location"""
    if "search_nearby" not in ctx["tools_called"]:
        return False
    if "send_location" in ctx["tools_called"]:
        return False
    for name, result in ctx.get("tool_results", {}).items():
        if name == "search_nearby" and result and "[error]" not in str(result)[:50]:
            return True
    return False


def _said_recorded_no_write(ctx):
    """LLM said 'noted/recorded' but didn't call write_file"""
    if "write_file" in ctx["tools_called"]:
        return False
    if "read_file" in ctx["tools_called"]:
        return False
    text = ctx.get("reply_text", "")
    return bool(re.search(r"(?:noted|recorded|saved|got it|written down)", text, re.I))


def _said_scheduled_no_schedule(ctx):
    """LLM said 'scheduled/set up' a task but didn't call schedule"""
    if "schedule" in ctx["tools_called"]:
        return False
    text = ctx.get("reply_text", "")
    return bool(re.search(
        r"(?:reminder|alarm|task|schedule).*(?:set up|created|arranged|done)|"
        r"(?:set up|created|arranged|done).*(?:reminder|alarm|task|schedule)",
        text, re.I
    ))


def _structured_data_no_render(ctx):
    """Reply contains structured data but didn't call render_page"""
    if "render_page" in ctx["tools_called"]:
        return False
    text = ctx.get("reply_text", "")
    lines = text.strip().splitlines()
    data_lines = sum(1 for line in lines
                     if re.search(r'[\d.]+\s*[%$]|[\d.]+\s*/\s*[\d.]+', line))
    return data_lines >= 3


# Register 4 initial rules
register(
    "search_nearby->send_location",
    _search_nearby_no_location,
    "[system] search_nearby returned location results, but you did not send location cards. "
    "Please use the send_location tool to send each found location to the user. "
    "Users expect clickable location cards, not plain text.",
)

register(
    "said_recorded->write_file",
    _said_recorded_no_write,
    "[system] You said 'noted/recorded', but didn't call write_file to persist it. "
    "Conversation memory gets truncated — only writing to a file counts as truly recorded. "
    "Please immediately use write_file to save to the appropriate memory/ file.",
)

register(
    "said_scheduled->schedule",
    _said_scheduled_no_schedule,
    "[system] You said a reminder/task was set up, but didn't call the schedule tool. "
    "Please immediately call the schedule tool to create the task. "
    "A verbal promise is not execution.",
)

register(
    "structured_data->render_page",
    _structured_data_no_render,
    "[system] Your reply contains structured comparison data. "
    "Please call render_page to generate a visual table for easier reading.",
)


# ============================================================
#  Check Entry Point
# ============================================================

# Per-chat fire counts (reset at start of each chat)
_fire_counts = {}  # rule_name -> count


def reset():
    """Called at start of each new chat session to reset fire counts."""
    _fire_counts.clear()


def check_nudges(tools_called, reply_text, tool_results=None):
    """Check if any nudge rules trigger. Returns nudge message or None.

    tool_results: {tool_name: result_str} from the most recent tool loop iteration
    """
    ctx = {
        "tools_called": tools_called,
        "reply_text": reply_text,
        "tool_results": tool_results or {},
    }

    for rule in _nudge_rules:
        name = rule["name"]
        fired = _fire_counts.get(name, 0)
        if fired >= rule["max_fires"]:
            continue
        try:
            if rule["trigger"](ctx):
                _fire_counts[name] = fired + 1
                log.info("[nudge] triggered: %s (fire %d/%d)", name, fired + 1, rule["max_fires"])
                return rule["message"]
        except Exception as e:
            log.error("[nudge] error in rule %s: %s", name, e)

    return None


# -- AI Mirror: self-reflection -> soul_report --

def _self_reflect_no_report(ctx):
    """LLM is giving verbal behavior analysis but didn't use soul_report"""
    if "soul_report" in ctx["tools_called"]:
        return False
    text = ctx.get("reply_text", "")
    if len(text) < 60:
        return False
    return bool(re.search(
        r"I\'ve observed|I notice that you|looking at your conversations"
        r"|your habits|your patterns|your recent behavior"
        r"|I suggest you|you need to improve|you tend to",
        text, re.I
    ))


register(
    "self_reflect->soul_report",
    _self_reflect_no_report,
    "[system] The user seems to be seeking self-reflection/behavior analysis. "
    "You can use the soul_report tool to generate a data-driven behavioral profile report, "
    "which is more convincing than verbal analysis. Consider calling soul_report.",
)
