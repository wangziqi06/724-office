"""
AI Mirror -- Behavioral Profile Tools

Three-layer architecture code layer:
1. Soul Report -- soul_report tool, analyzes behavioral data to generate HTML report
2. Future Self -- future_self tool, starts "future self" conversation mode

Agent Diary is implemented via cron + existing tools, not in this file.
Removal: delete this file + the import in tools.py + the rule in nudge.py.
"""

import json
import logging
import os
import re
import time
import uuid
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from tools_base import tool

log = logging.getLogger("agent")

CST = timezone(timedelta(hours=8))

# Chinese stop words (functional for Chinese NLP text processing)
_STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看",
    "好", "自己", "这", "他", "她", "那", "吗", "什么", "怎么", "可以", "这个",
    "一个", "一下", "已经", "没", "把", "用", "做", "让", "被", "对", "但",
    "还", "如果", "因为", "所以", "这样", "那个", "应该", "可能", "知道",
}

# ============================================================
#  LLM call (reuses deepseek-chat, independent from llm.py)
# ============================================================

def _get_llm_provider():
    """Read deepseek-chat provider from config.json"""
    config_path = os.environ.get("AGENT_CONFIG", "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        providers = cfg.get("models", {}).get("providers", {})
        default_key = cfg.get("models", {}).get("default", "")
        return providers.get("deepseek-chat") or providers.get(default_key)
    except Exception as e:
        log.error("[mirror] config read failed: %s", e)
        return None


def _call_llm(prompt, max_tokens=2000):
    """Call deepseek-chat to get a text response"""
    provider = _get_llm_provider()
    if not provider:
        return None

    url = provider["api_base"].rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": provider["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + provider["api_key"],
    }

    try:
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"].get("content", "")
    except Exception as e:
        log.error("[mirror] LLM call failed: %s", e)
        return None


# ============================================================
#  Data analysis pipeline (each step has logging)
# ============================================================

def _get_data_dir(workspace):
    """workspace=/data/workspace -> data_dir=/data"""
    return os.path.dirname(workspace.rstrip("/"))


def _analyze_sessions(workspace, days=30):
    """Analyze session files: message volume, tool usage, topic keywords"""
    sessions_dir = os.path.join(_get_data_dir(workspace), "sessions")
    if not os.path.isdir(sessions_dir):
        log.warning("[mirror] sessions dir not found: %s", sessions_dir)
        return {"total_user_msgs": 0, "days_with_data": 0}

    tool_counts = Counter()
    topic_keywords = Counter()
    total_user_msgs = 0
    total_assistant_msgs = 0
    msg_lengths = []
    files_scanned = 0
    files_skipped = 0

    def _scan_messages(messages):
        nonlocal total_user_msgs, total_assistant_msgs
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""
            if role == "user" and isinstance(content, str) and content.strip():
                total_user_msgs += 1
                msg_lengths.append(len(content))
                # Extract Chinese 2-4 char words and English 3+ char words
                words = re.findall(r'[\u4e00-\u9fff]{2,4}|[a-zA-Z]{3,}', content)
                for w in words:
                    if w.lower() not in _STOP_WORDS and len(w) >= 2:
                        topic_keywords[w] += 1
            elif role == "assistant":
                total_assistant_msgs += 1
                for tc in msg.get("tool_calls") or []:
                    fn_name = tc.get("function", {}).get("name", "")
                    if fn_name:
                        tool_counts[fn_name] += 1

    # Scan current session files (file by file, not bulk load)
    for fname in os.listdir(sessions_dir):
        if not fname.endswith(".json") or fname.startswith("."):
            continue
        if os.path.isdir(os.path.join(sessions_dir, fname)):
            continue
        try:
            with open(os.path.join(sessions_dir, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                _scan_messages(data)
                files_scanned += 1
        except Exception:
            files_skipped += 1

    # Scan archive/ (only read recent N days)
    archive_dir = os.path.join(sessions_dir, "archive")
    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp()
    if os.path.isdir(archive_dir):
        for fname in os.listdir(archive_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(archive_dir, fname)
            try:
                if os.path.getmtime(fpath) < cutoff_ts:
                    continue
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    _scan_messages(data)
                    files_scanned += 1
            except Exception:
                files_skipped += 1

    # Extract daily message counts from diary files
    diary_dir = os.path.join(workspace, "memory")
    daily_msgs = {}
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    if os.path.isdir(diary_dir):
        for fname in sorted(os.listdir(diary_dir)):
            m = re.match(r"(\d{4}-\d{2}-\d{2})\.md", fname)
            if not m or m.group(1) < cutoff_date:
                continue
            try:
                with open(os.path.join(diary_dir, fname), "r", encoding="utf-8") as f:
                    header = f.read(2000)
                # Match pattern like "N user messages" in diary headers
                dm = re.search(r'(\d+)\s*user messages', header)
                if not dm:
                    # Also try Chinese pattern for backward compatibility
                    dm = re.search(r'(\d+)\s*条用户消息', header)
                daily_msgs[m.group(1)] = int(dm.group(1)) if dm else 0
            except Exception:
                pass

    log.info("[mirror] analyzed %d sessions (skipped %d), user_msgs=%d, diary_days=%d",
             files_scanned, files_skipped, total_user_msgs, len(daily_msgs))

    return {
        "total_user_msgs": total_user_msgs,
        "total_assistant_msgs": total_assistant_msgs,
        "days_with_data": len(daily_msgs),
        "daily_msgs": daily_msgs,
        "tool_counts": dict(tool_counts.most_common(15)),
        "avg_msg_length": round(sum(msg_lengths) / max(len(msg_lengths), 1)),
        "topic_keywords": dict(topic_keywords.most_common(20)),
    }


def _analyze_commitments(workspace, days=30):
    """Analyze commitments and follow-through from diary entries"""
    diary_dir = os.path.join(workspace, "memory")
    if not os.path.isdir(diary_dir):
        return {"total": 0, "fulfilled": 0, "rate": 0, "recent": []}

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    # Match commitment patterns in Chinese text (functional regex for Chinese content analysis)
    commit_re = re.compile(
        r'(?:说要|计划|打算|承诺|答应|准备|目标是|决定|要去|要做|下一步)(.*?)(?:\n|$|。|；)'
    )

    commitments = []
    diary_texts = {}

    for fname in sorted(os.listdir(diary_dir)):
        m = re.match(r"(\d{4}-\d{2}-\d{2})\.md", fname)
        if not m or m.group(1) < cutoff:
            continue
        try:
            with open(os.path.join(diary_dir, fname), "r", encoding="utf-8") as f:
                text = f.read()
            diary_texts[m.group(1)] = text
            for cm in commit_re.finditer(text):
                ct = cm.group(1).strip()
                if len(ct) >= 4:
                    commitments.append({"date": m.group(1), "text": ct})
        except Exception:
            continue

    # Fulfillment check: whether related keywords appear in subsequent diary entries
    fulfilled = 0
    for c in commitments:
        # Extract Chinese 2+ char keywords for matching
        kws = re.findall(r'[\u4e00-\u9fff]{2,}', c["text"])[:3]
        if not kws:
            continue
        for d, t in diary_texts.items():
            if d > c["date"] and any(kw in t for kw in kws):
                fulfilled += 1
                break

    rate = round(fulfilled / max(len(commitments), 1) * 100)
    log.info("[mirror] commitments: %d found, %d fulfilled (%d%%)",
             len(commitments), fulfilled, rate)

    return {"total": len(commitments), "fulfilled": fulfilled, "rate": rate,
            "recent": commitments[-5:]}


def _analyze_priorities(workspace, sessions_data):
    """Compare claimed priorities in MEMORY.md vs actual behavior"""
    claimed = []
    memory_md = os.path.join(workspace, "memory", "MEMORY.md")
    if os.path.isfile(memory_md):
        try:
            with open(memory_md, "r", encoding="utf-8") as f:
                text = f.read()
            for pm in re.finditer(r'###?\s*(P\d)\s*[—\-]+\s*(.+?)(?:\n|$)', text):
                claimed.append({"priority": pm.group(1), "name": pm.group(2).strip()})
        except Exception:
            pass

    return {
        "claimed": claimed,
        "actual_top_tools": list(sessions_data.get("tool_counts", {}).items())[:8],
        "actual_top_topics": list(sessions_data.get("topic_keywords", {}).items())[:10],
    }


# ============================================================
#  LLM insight synthesis
# ============================================================

def _synthesize_insights(sessions, commitments, priorities):
    """Use LLM to synthesize 5-7 behavioral insights"""
    summary = json.dumps({
        "user_msgs": sessions.get("total_user_msgs"),
        "days": sessions.get("days_with_data"),
        "avg_msg_len": sessions.get("avg_msg_length"),
        "top_tools": dict(list(sessions.get("tool_counts", {}).items())[:10]),
        "top_topics": dict(list(sessions.get("topic_keywords", {}).items())[:10]),
        "commitments": commitments.get("total"),
        "fulfilled": commitments.get("fulfilled"),
        "commitment_rate": commitments.get("rate"),
        "recent_commitments": commitments.get("recent", []),
        "claimed_priorities": priorities.get("claimed", []),
    }, ensure_ascii=False)[:8000]

    prompt = (
        "You are an honest mirror. Based on the following user behavioral data, generate 5-7 behavioral insights.\n\n"
        "Requirements:\n"
        "1. Each insight must be supported by specific data (cite numbers)\n"
        "2. Be honest but not harsh, like a caring friend\n"
        "3. Point out gaps between what the user says and what they actually do\n"
        "4. Discover patterns the user might not notice themselves\n"
        "5. Be specific, not generic\n\n"
        "Return a JSON array, each item:\n"
        '{"title": "4-8 word title", "insight": "30-60 word insight", "data_point": "brief data evidence"}\n\n'
        f"Data:\n{summary}"
    )

    result = _call_llm(prompt, max_tokens=1500)
    if not result:
        return []

    # Extract JSON array
    text = result.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed[:7]
    except json.JSONDecodeError:
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())[:7]
            except Exception:
                pass
    log.warning("[mirror] failed to parse LLM insights")
    return []


# ============================================================
#  HTML generation (mirror-specific, does not register _TEMPLATES)
# ============================================================

_MIRROR_CSS = """
.big-number {
    text-align: center; padding: 32px 16px; background: #ffffff;
    border-radius: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    margin-bottom: 24px;
}
.big-number .value { font-size: 64px; font-weight: 700; line-height: 1; }
.big-number .label { font-size: 14px; color: #6b7280; margin-top: 8px; }
.big-number .sub { font-size: 12px; color: #9ca3af; margin-top: 4px; }
.insight-card {
    background: #ffffff; border-radius: 12px; padding: 16px;
    margin-bottom: 12px; border-left: 3px solid #c8952e;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
.insight-card .title { font-weight: 600; color: #0f2b5b; margin-bottom: 4px; }
.insight-card .body { color: #374151; }
.insight-card .data { font-size: 12px; color: #9ca3af; margin-top: 6px; }
"""


def _generate_report_html(sessions, commitments, priorities, insights):
    """Generate Soul Report HTML page"""
    from tools_page import _html_wrap, _chart_init_js, _COLORS, _CSS

    parts = []

    # Inject mirror-specific CSS
    parts.append(f"<style>{_MIRROR_CSS}</style>\n")

    # -- 1. Big number card: commitment fulfillment rate --
    rate = commitments.get("rate", 0)
    rate_color = "#22c55e" if rate >= 70 else "#eab308" if rate >= 40 else "#ef4444"
    total_c = commitments.get("total", 0)
    fulfilled_c = commitments.get("fulfilled", 0)
    days_n = sessions.get("days_with_data", 0)
    total_msgs = sessions.get("total_user_msgs", 0)

    parts.append(
        f'<div class="big-number" style="border-top:4px solid {rate_color};">'
        f'<div class="value" style="color:{rate_color};">{rate}%</div>'
        f'<div class="label">Commitment Fulfillment Rate ({total_c} commitments, {fulfilled_c} followed through)</div>'
        f'<div class="sub">Past {days_n} days &middot; {total_msgs} messages</div>'
        f'</div>\n'
    )

    # -- 2. Radar chart: multi-dimensional behavioral profile --
    tool_counts = sessions.get("tool_counts", {})
    topics = sessions.get("topic_keywords", {})

    def _dim(keywords, tools):
        s = sum(topics.get(k, 0) for k in keywords)
        s += sum(tool_counts.get(t, 0) * 3 for t in tools)
        return s

    raw = {
        "Work": _dim(["project", "code", "deploy", "develop", "work", "deliver", "server",
                       # Chinese keywords for Chinese content analysis
                       "\u9879\u76ee", "\u4ee3\u7801", "\u90e8\u7f72", "\u5f00\u53d1", "\u5de5\u4f5c", "\u4ea4\u4ed8", "\u670d\u52a1\u5668"],
                     ["exec", "edit_file", "code_audit"]),
        "Learning": _dim(["learn", "paper", "course", "research", "read", "analyze",
                          "\u5b66\u4e60", "\u8bba\u6587", "\u8bfe\u7a0b", "\u7814\u7a76", "\u9605\u8bfb", "\u5206\u6790"],
                     ["web_search"]),
        "Social": _dim(["meet", "chat", "friend", "contact", "network",
                        "\u7ea6", "\u804a\u5929", "\u670b\u53cb", "\u8054\u7cfb", "\u89c1\u9762", "\u4eba\u8109", "\u7fa4"],
                     ["send_namecard", "search_nearby", "message"]),
        "Health": _dim(["fitness", "exercise", "sleep", "health", "run", "diet", "weight",
                        "\u5065\u8eab", "\u8fd0\u52a8", "\u7761\u7720", "\u5065\u5eb7", "\u8dd1\u6b65", "\u996e\u98df", "\u4f53\u91cd"],
                     []),
        "Records": _dim(["record", "diary", "note", "archive", "memory", "file",
                         "\u8bb0\u5f55", "\u65e5\u8bb0", "\u7b14\u8bb0", "\u5f52\u6863", "\u8bb0\u5fc6", "\u6587\u4ef6"],
                     ["write_file", "compact_memory"]),
    }
    # Normalize to 0-100
    max_raw = max(raw.values()) if raw.values() else 1
    radar_values = [min(round(v / max(max_raw, 1) * 100), 100) for v in raw.values()]
    # Floor: at least 5 (avoid all zeros)
    radar_values = [max(v, 5) for v in radar_values]

    radar_data = {
        "indicators": [{"name": n, "max": 100} for n in raw.keys()],
        "series": [{"name": "Behavioral Distribution", "values": radar_values}],
    }
    radar_setup = (
        "var ind = d.indicators.map(function(i){return {name:i.name,max:i.max};});\n"
        "chart.setOption({\n"
        "  backgroundColor:'transparent',\n"
        "  radar:{indicator:ind, axisName:{color:'#374151',fontSize:13},\n"
        "    splitArea:{areaStyle:{color:['#f8fafc','#fff']}},\n"
        "    splitLine:{lineStyle:{color:'#e5e7eb'}}},\n"
        "  series:[{type:'radar',data:[{value:d.series[0].values,\n"
        "    areaStyle:{opacity:0.2,color:'#1a56db'},\n"
        "    lineStyle:{color:'#1a56db',width:2},\n"
        "    itemStyle:{color:'#1a56db'}}]}]\n"
        "});\n"
    )
    parts.append('<h2>Behavioral Profile</h2>\n')
    parts.append('<div id="radar" class="chart-box"><div class="loading">Loading...</div></div>\n')
    parts.append(_chart_init_js("radar", radar_data, radar_setup))

    # -- 3. Horizontal bar chart: tool usage Top 10 --
    top_tools = list(tool_counts.items())[:10]
    if top_tools:
        tool_data = {
            "x": [t[0] for t in top_tools],
            "values": [t[1] for t in top_tools],
        }
        tool_setup = (
            "chart.setOption({\n"
            "  backgroundColor:'transparent',\n"
            "  tooltip:{trigger:'axis'},\n"
            "  grid:{left:110,right:20,top:10,bottom:20},\n"
            "  xAxis:{type:'value',axisLabel:{color:'#6b7280'},splitLine:{lineStyle:{color:'#e5e7eb'}}},\n"
            "  yAxis:{type:'category',data:d.x,axisLabel:{color:'#374151',fontSize:11},inverse:true},\n"
            "  series:[{type:'bar',data:d.values,barMaxWidth:22,\n"
            "    itemStyle:{color:new echarts.graphic.LinearGradient(0,0,1,0,"
            "[{offset:0,color:'#1a56db'},{offset:1,color:'#6b9bd2'}])}}]\n"
            "});\n"
        )
        parts.append('<h2>Tool Usage Distribution</h2>\n')
        parts.append('<div id="tools" class="chart-box"><div class="loading">Loading...</div></div>\n')
        parts.append(_chart_init_js("tools", tool_data, tool_setup))

    # -- 4. Daily activity line chart --
    daily = sessions.get("daily_msgs", {})
    if daily:
        dates = sorted(daily.keys())[-14:]
        daily_data = {
            "x": [d[5:] for d in dates],
            "values": [daily.get(d, 0) for d in dates],
        }
        daily_setup = (
            "chart.setOption({\n"
            "  backgroundColor:'transparent',\n"
            "  tooltip:{trigger:'axis'},\n"
            "  grid:{left:40,right:20,top:10,bottom:30},\n"
            "  xAxis:{type:'category',data:d.x,axisLabel:{color:'#6b7280',fontSize:11}},\n"
            "  yAxis:{type:'value',axisLabel:{color:'#6b7280'},splitLine:{lineStyle:{color:'#e5e7eb'}}},\n"
            "  series:[{type:'line',data:d.values,smooth:true,symbolSize:6,\n"
            "    areaStyle:{opacity:0.15,color:'#1a56db'},\n"
            "    lineStyle:{color:'#1a56db',width:2},\n"
            "    itemStyle:{color:'#1a56db'}}]\n"
            "});\n"
        )
        parts.append('<h2>Daily Activity</h2>\n')
        parts.append('<div id="daily" class="chart-box" style="height:240px;">'
                     '<div class="loading">Loading...</div></div>\n')
        parts.append(_chart_init_js("daily", daily_data, daily_setup))

    # -- 5. Topic pie chart --
    top_topics = list(topics.items())[:8]
    if top_topics:
        pie_data = {"items": [{"name": t[0], "value": t[1]} for t in top_topics]}
        pie_setup = (
            "chart.setOption({\n"
            "  backgroundColor:'transparent',\n"
            "  tooltip:{trigger:'item',formatter:'{b}: {c} ({d}%)'},\n"
            "  series:[{type:'pie',radius:['35%','65%'],\n"
            "    label:{color:'#374151',fontSize:12},\n"
            "    data:d.items,\n"
            "    emphasis:{itemStyle:{shadowBlur:10,shadowColor:'rgba(0,0,0,0.15)'}}}],\n"
            f"  color:{_COLORS}\n"
            "});\n"
        )
        parts.append('<h2>Topic Distribution</h2>\n')
        parts.append('<div id="topics" class="chart-box"><div class="loading">Loading...</div></div>\n')
        parts.append(_chart_init_js("topics", pie_data, pie_setup))

    # -- 6. Priority comparison --
    claimed = priorities.get("claimed", [])
    if claimed:
        parts.append('<h2>Claimed Priorities vs Actual Topics</h2>\n')
        parts.append('<div class="section">\n')
        for c in claimed:
            parts.append(f'<p><strong>{c["priority"]}</strong>: {c["name"]}</p>\n')
        actual = priorities.get("actual_top_topics", [])[:5]
        if actual:
            parts.append('<p style="margin-top:12px;color:#6b7280;font-size:13px;">'
                         'Most frequently discussed:</p>\n')
            for t, cnt in actual:
                parts.append(f'<p style="color:#374151;">&bull; {t} ({cnt} times)</p>\n')
        parts.append('</div>\n')

    # -- 7. AI insight cards --
    if insights:
        parts.append('<h2>AI Insights</h2>\n')
        for ins in insights:
            t = ins.get("title", "")
            body = ins.get("insight", "")
            dp = ins.get("data_point", "")
            parts.append(
                f'<div class="insight-card">'
                f'<div class="title">{t}</div>'
                f'<div class="body">{body}</div>'
                f'<div class="data">{dp}</div>'
                f'</div>\n'
            )

    now = datetime.now(CST)
    title = "AI Mirror \u00b7 Soul Report \u00b7 " + now.strftime("%Y.%m.%d")
    return _html_wrap(title, "\n".join(parts))


# ============================================================
#  Soul Report tool
# ============================================================

@tool("soul_report",
      "Generate a user behavioral profile report (AI Mirror - Soul Report). Analyzes conversation, memory, and commitment data from the past N days "
      "to produce an honest behavioral analysis page. Suitable when user asks 'how am I doing lately' or wants to understand their behavioral patterns.",
      {"days": {"type": "integer", "description": "Number of days to analyze (default 30)"}},
      [])
def tool_soul_report(args, ctx):
    workspace = ctx.get("workspace", "")
    days = args.get("days", 30)
    owner_id = ctx.get("owner_id")

    if not workspace:
        return "[error] workspace not found"

    log.info("[mirror] generating soul report, days=%d", days)

    # Data analysis pipeline
    sessions = _analyze_sessions(workspace, days)

    # Insufficient data protection
    if sessions.get("days_with_data", 0) < 7:
        n = sessions.get("days_with_data", 0)
        return (f"Still accumulating data. At least 7 days of interaction records are needed to generate a meaningful report. "
                f"Currently have {n} days of data. Chat for a few more days and I can give you a truly insightful analysis.")

    commitments = _analyze_commitments(workspace, days)
    priorities = _analyze_priorities(workspace, sessions)

    # LLM insight synthesis
    insights = _synthesize_insights(sessions, commitments, priorities)

    # Generate HTML
    html = _generate_report_html(sessions, commitments, priorities, insights)

    # Write file (full UUID to prevent brute-force guessing)
    import tools_page
    page_id = uuid.uuid4().hex  # 32 chars
    filename = f"mirror_{page_id}.html"
    filepath = os.path.join(tools_page.PAGES_DIR, filename)

    os.makedirs(tools_page.PAGES_DIR, exist_ok=True)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        log.error("[mirror] write HTML failed: %s", e)
        return f"[error] Failed to write report file: {e}"

    # Send link card
    base_url = tools_page._get_base_url()
    page_url = f"{base_url}/{filename}"

    if owner_id:
        try:
            import messaging
            messaging.send_link(owner_id, "AI Mirror \u00b7 Soul Report",
                           f"Behavioral profile analysis for the past {days} days",
                           page_url)
        except Exception as e:
            log.warning("[mirror] send_link failed: %s", e)
            return f"Report generated: {page_url}\n(Link send failed: {e})"

    log.info("[mirror] soul report generated: %s", page_url)
    return (f"Soul report generated and sent: {page_url}\n"
            "Note: report link is publicly accessible for 24 hours, share with caution.")


# ============================================================
#  Future Self tool
# ============================================================

FUTURE_SELF_TIMEOUT = 1800  # 30 minutes


def _get_mirror_dir(workspace):
    d = os.path.join(workspace, "mirror")
    os.makedirs(d, exist_ok=True)
    return d


@tool("future_self",
      "Start 'future self' conversation mode. Simulates what the user would be like at a specified age based on real accumulated behavioral data. Supports multi-turn conversation. "
      "Say 'exit' or 'end' to return to normal mode.",
      {"age": {"type": "integer", "description": "Age to converse with (30/40/50/60)"}},
      ["age"])
def tool_future_self(args, ctx):
    workspace = ctx.get("workspace", "")
    age = args["age"]

    if not workspace:
        return "[error] workspace not found"
    if age < 25 or age > 80:
        return "Age range is 25-80. What age would you like to talk to?"

    log.info("[mirror] starting future_self, age=%d", age)

    # Collect behavioral data
    sessions = _analyze_sessions(workspace, days=30)
    commitments = _analyze_commitments(workspace, days=30)
    priorities = _analyze_priorities(workspace, sessions)

    # LLM persona summary synthesis
    data_summary = json.dumps({
        "total_msgs": sessions.get("total_user_msgs", 0),
        "top_tools": list(sessions.get("tool_counts", {}).items())[:5],
        "top_topics": list(sessions.get("topic_keywords", {}).items())[:8],
        "commitment_rate": commitments.get("rate", 0),
        "claimed_priorities": priorities.get("claimed", []),
        "recent_commitments": commitments.get("recent", []),
    }, ensure_ascii=False)[:3000]

    persona_raw = _call_llm(
        f"You need to role-play as the user's future version at age {age}. Based on the following behavioral data, write a character profile.\n\n"
        f"Requirements:\n"
        f"1. Extrapolate from current behavioral patterns to what they'd be like at age {age}\n"
        f"2. Be honest -- if current patterns continue, what would their state be at {age}?\n"
        f"3. Speak in first person 'I', warm but direct\n"
        f"4. Include 3-5 specific life turning points (extrapolated from current behavior)\n"
        f"5. Keep it under 400 words\n\n"
        f"Current behavioral data:\n{data_summary}\n\n"
        f"Output the character profile directly, no explanations.",
        max_tokens=1000,
    )

    if not persona_raw:
        return "[error] Unable to generate future self persona, please try again later"

    # Build persona prompt (injected into system prompt)
    persona_prompt = (
        f"\n\n---\n"
        f"[Future Self Mode -- Active]\n"
        f"You are now the user at age {age}. Here is your persona:\n\n"
        f"{persona_raw.strip()}\n\n"
        f"Rules:\n"
        f"- Speak in first person 'I', you ARE the {age}-year-old version of them\n"
        f"- Speak based on real data, do not fabricate experiences\n"
        f"- You can express regret, pride, confusion, and other genuine emotions\n"
        f"- If the user says 'exit' or 'end conversation', say goodbye and inform them the mode has ended\n"
        f"- Do not use any tools, only converse\n"
    )

    # Write state file
    mirror_dir = _get_mirror_dir(workspace)
    state_file = os.path.join(mirror_dir, ".future_self_active")
    state = {
        "age": age,
        "persona_prompt": persona_prompt,
        "started_at": time.time(),
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    log.info("[mirror] future_self state saved, age=%d", age)

    # Generate opening line
    opening = _call_llm(
        f"You are the user's future version at age {age}. Persona:\n\n{persona_raw.strip()}\n\n"
        f"The user just found you. Greet them in 2-3 sentences, warm with a touch of nostalgia. "
        f"Hint that you know what they're currently doing, but don't list specifics. Just open the conversation.",
        max_tokens=300,
    )

    return opening or f"Hey, long time no see. I'm the {age}-year-old you. What would you like to ask me?"


def check_future_self_state(workspace):
    """Called by llm.py: check future_self state, return persona_prompt or None.

    Automatically cleans up timed-out state files.
    """
    state_file = os.path.join(workspace, "mirror", ".future_self_active")
    if not os.path.isfile(state_file):
        return None

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Timeout cleanup
        if time.time() - state.get("started_at", 0) > FUTURE_SELF_TIMEOUT:
            os.remove(state_file)
            log.info("[mirror] future_self timed out, cleaned up")
            return None
        return state.get("persona_prompt")
    except Exception:
        return None


def deactivate_future_self(workspace):
    """Deactivate future_self mode, delete state file"""
    state_file = os.path.join(workspace, "mirror", ".future_self_active")
    try:
        if os.path.isfile(state_file):
            os.remove(state_file)
            log.info("[mirror] future_self deactivated")
            return True
    except Exception:
        pass
    return False
