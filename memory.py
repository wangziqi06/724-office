"""
Memory System — Core algorithms extracted from SimpleMem

Three-stage pipeline:
1. Compress: dialogue -> LLM extracts structured memories (key facts + people + time + keywords)
2. Dedup: cosine similarity between new and existing memories, >threshold skip
3. Retrieve: user message -> embedding -> LanceDB vector search -> return relevant memories

Storage: LanceDB (embedded, file-level, no standalone service)
Vectorization: Zhipu Embedding-3 API (1024 dimensions)
"""

import json
import re
import logging
import os
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

log = logging.getLogger("agent")
CST = timezone(timedelta(hours=8))

# ============================================================
#  Module State
# ============================================================

_config = {}        # memory config section
_llm_config = {}    # models config (for calling LLM during compression)
_db = None          # LanceDB connection
_tables = {}        # table_name -> LanceDB table (multi-tenant: per-user tables)
_table_lock = threading.Lock()  # Protect _tables and table operations
_enabled = False
_context_cache = {} # session_key -> str (pre-computed memory summary, zero-latency for hardware channels)

# ============================================================
#  Public API (4 functions)
# ============================================================


def init(config, llm_config, db_path):
    """Initialize LanceDB connection + embedding config. Called once at startup by xiaowang.py."""
    global _config, _llm_config, _db, _table, _enabled

    mem_cfg = config.get("memory", {})
    if not mem_cfg.get("enabled", False):
        log.info("[memory] disabled in config")
        return

    embedding_cfg = mem_cfg.get("embedding_api", {})
    if not embedding_cfg.get("api_key"):
        log.error("[memory] no embedding API key, disabled")
        return

    _config = mem_cfg
    _llm_config = llm_config

    try:
        import lancedb
        _db = lancedb.connect(db_path)
        # Multi-tenant: each user has independent memories_{user_id} table, created on demand
        _enabled = True
        log.info("[memory] initialized, db_path=%s" % db_path)
    except Exception as e:
        log.error("[memory] init failed: %s" % e, exc_info=True)


def _get_table(user_id=None):
    """Get user LanceDB table (lazy-created, thread-safe). user_id=None returns default memories table."""
    table_name = f"memories_{user_id}" if user_id else "memories"
    if table_name in _tables:
        return _tables[table_name]
    with _table_lock:
        # Double check
        if table_name in _tables:
            return _tables[table_name]
        try:
            t = _db.open_table(table_name)
            count = t.count_rows()
            log.info("[memory] opened table '%s', %d memories" % (table_name, count))
            _tables[table_name] = t
            return t
        except Exception:
            import numpy as np
            seed = [{
                "id": "seed",
                "fact": "system_init",
                "keywords": "[]",
                "persons": "[]",
                "timestamp": "",
                "topic": "system",
                "session_key": "init",
                "created_at": time.time(),
                "vector": np.zeros(1024).tolist(),
            }]
            t = _db.create_table(table_name, seed)
            log.info("[memory] created new table '%s'" % table_name)
            _tables[table_name] = t
            return t



def _relative_time_label(ts_str, today_str):
    """Try to parse timestamp, return relative label. Returns empty string on failure."""
    try:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(ts_str.strip()[:len(fmt)+2], fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return ""
        today_dt = datetime.strptime(today_str, "%Y-%m-%d")
        delta_days = (today_dt.date() - dt.date()).days
        if delta_days == 0:
            return "today"
        elif delta_days == 1:
            return "yesterday"
        elif delta_days == -1:
            return "tomorrow"
        elif delta_days > 1:
            return "%d days ago" % delta_days
        elif delta_days < -1:
            return "in %d days" % (-delta_days)
        return ""
    except Exception:
        return ""


def retrieve(user_msg, session_key, top_k=None, user_id=None):
    """Retrieve relevant memories, return formatted text block. Synchronous call."""
    if not _enabled or not _db:
        return ""
    if top_k is None:
        top_k = _config.get("retrieve_top_k", 5)

    try:
        table = _get_table(user_id)
        if not table:
            return ""
        query_vec = _embed([user_msg])
        if not query_vec:
            return ""
        results = table.search(query_vec[0]).limit(top_k).to_list()
        if not results:
            return ""

        # Filter out seed data and low-quality results
        filtered = [r for r in results if r.get("id") != "seed" and r.get("fact", "") != "system_init"]
        if not filtered:
            return ""

        today_str = datetime.now(CST).strftime("%Y-%m-%d")
        lines = ["Related memories (today is %s)" % today_str]
        for r in filtered:
            line = "- " + r["fact"]
            ts = r.get("timestamp", "")
            if ts:
                rel = _relative_time_label(ts, today_str)
                if rel:
                    line += " (%s, %s)" % (ts, rel)
                else:
                    line += " (%s)" % ts
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        log.error("[memory] retrieve error: %s" % e)
        return ""


def compress_async(evicted_messages, session_key, user_id=None):
    """Start background thread to compress evicted messages into long-term memory."""
    if not _enabled:
        return
    # Filter: keep only user and assistant text messages
    msgs = []
    for m in evicted_messages:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        if not content or not isinstance(content, str):
            continue
        # Skip assistant messages with only tool_calls and no content
        if role == "assistant" and m.get("tool_calls"):
            continue
        msgs.append(m)

    if len(msgs) < 2:
        # Too few messages, not worth compressing
        return

    t = threading.Thread(target=_compress_worker, args=(msgs, session_key, user_id), daemon=True)
    t.start()
    log.info("[memory] compress started in background (%d messages)" % len(msgs))


def get_cached_context(session_key):
    """Zero-latency: return pre-computed memory summary. For hardware/voice channels."""
    return _context_cache.get(session_key, "")


def invalidate_stale_facts(old_text, user_id=None):
    """When facts in MEMORY.md are modified, delete similar entries in LanceDB.
    
    Called by edit_file tool when modifying MEMORY.md. Ensures vector memory is consistent with document memory.
    """
    if not _enabled or not _db or not old_text or len(old_text.strip()) < 10:
        return 0
    try:
        table = _get_table(user_id)
        if not table:
            return 0
        query_vec = _embed([old_text])
        if not query_vec:
            return 0
        results = table.search(query_vec[0]).limit(10).to_list()
        if not results:
            return 0
        # Find entries highly similar to old text (threshold 0.85)
        to_delete = []
        for r in results:
            sim = 1 - r.get("_distance", 1)
            if sim >= 0.85:
                to_delete.append(r["id"])
                log.info("[memory] invalidate: sim=%.3f fact=[%s]", sim, r.get("fact", "")[:80])
        if to_delete:
            table.delete("id IN (%s)" % ",".join("'%s'" % i for i in to_delete))
            log.info("[memory] invalidated %d stale facts from LanceDB", len(to_delete))
        return len(to_delete)
    except Exception as e:
        log.error("[memory] invalidate error: %s", e)
        return 0



# ============================================================
#  Tier Management: MEMORY.md Auto-compaction (Infrastructure)
# ============================================================

COMPACT_THRESHOLD = 3000   # Total chars exceeding this triggers compaction
SECTION_THRESHOLD = 500    # Section new content must exceed this to migrate
_compact_lock = threading.Lock()

# MEMORY.md section -> independent file mapping
SECTION_FILE_MAP = {
    "contacts": "contacts.md",
    "thoughts": "thoughts.md",
    "spending": "spending.md",
    "decisions": "decisions.md",
    "emotions": "emotions.md",
    "weaknesses": "weaknesses.md",
    "patterns": "patterns.md",
}


def auto_compact(workspace):
    """When MEMORY.md exceeds threshold, auto-migrate large sections to independent files.

    Pure file operations, zero data loss: cut-paste, not compression/summarization.
    Idempotent: already-migrated sections (starting with ->) will not be migrated again.
    Thread-safe: only one compact runs at a time.
    """
    if not _compact_lock.acquire(blocking=False):
        return {"status": "skip", "reason": "another compact in progress"}
    try:
        return _do_compact(workspace)
    finally:
        _compact_lock.release()


def _do_compact(workspace, force=False):
    memory_dir = os.path.join(workspace, "memory")
    memory_md = os.path.join(memory_dir, "MEMORY.md")

    if not os.path.exists(memory_md):
        return {"status": "skip", "reason": "MEMORY.md not found"}

    try:
        with open(memory_md, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return {"status": "error", "reason": "cannot read MEMORY.md"}

    if not force and len(content) < COMPACT_THRESHOLD:
        return {"status": "skip", "reason": "below threshold", "chars": len(content), "threshold": COMPACT_THRESHOLD}

    sections = _parse_md_sections(content)
    original_len = len(content)
    compacted = False
    moved = []

    for section_name in list(sections.keys()):
        target_file = SECTION_FILE_MAP.get(section_name)
        if not target_file:
            continue

        section_body = sections[section_name]
        new_content = _extract_new_content(section_body)
        if not new_content or len(new_content) < SECTION_THRESHOLD:
            continue

        target_path = os.path.join(memory_dir, target_file)
        _append_to_topic_file(target_path, section_name, new_content)

        entry_count = _count_topic_entries(target_path)
        pointer = "-> %d entries, see memory/%s" % (entry_count, target_file)
        sections[section_name] = pointer + "\n"
        moved.append({"section": section_name, "file": target_file, "chars": len(new_content)})
        compacted = True

    if compacted:
        new_md = _rebuild_md(sections)
        tmp_path = memory_md + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_md)
        os.replace(tmp_path, memory_md)
        log.info("[memory] auto_compact: %d -> %d chars" % (original_len, len(new_md)))
        return {"status": "compacted", "before": original_len, "after": len(new_md), "moved": moved}
    return {"status": "skip", "reason": "no section above threshold", "chars": original_len}



def manual_compact(workspace):
    """Manually trigger compact, skip threshold check, return detailed report."""
    if not _compact_lock.acquire(blocking=False):
        return {"status": "skip", "reason": "another compact in progress"}
    try:
        return _do_compact(workspace, force=True)
    finally:
        _compact_lock.release()

def _parse_md_sections(content):
    """Parse Markdown into OrderedDict: {section_name: section_body}."""
    from collections import OrderedDict
    sections = OrderedDict()
    current_name = "_preamble"
    current_lines = []

    for line in content.split("\n"):
        if line.startswith("## "):
            sections[current_name] = "\n".join(current_lines)
            current_name = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    sections[current_name] = "\n".join(current_lines)
    return sections


def _extract_new_content(section_body):
    """Extract non-migrated content (skip -> pointer lines and empty lines)"""
    lines = []
    for line in section_body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("->") or stripped.startswith(">"):
            continue
        if stripped:
            lines.append(line)
    return "\n".join(lines)


def _append_to_topic_file(path, section_name, content):
    """Append content to section file. Create if not exists. Avoid duplicate appends."""
    content_stripped = content.strip()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
        if content_stripped in existing:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + content_stripped + "\n")
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# %s\n\n%s\n" % (section_name, content_stripped))


def _count_topic_entries(path):
    """Count entries in section file"""
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    count = 0
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("- ") or s.startswith("**[") or s.startswith("["):
            count += 1
    return max(count, 1)


def _rebuild_md(sections):
    """Rebuild Markdown from OrderedDict"""
    parts = []
    for name, body in sections.items():
        if name == "_preamble":
            if body.strip():
                parts.append(body.rstrip())
        else:
            parts.append("## %s\n%s" % (name, body.rstrip()))
    return "\n\n".join(parts) + "\n"




# ============================================================
#  Tier Management: AGENT.md / SOUL.md Guide Compaction (Infrastructure)
# ============================================================

GUIDE_FILE_THRESHOLD = 8000    # File total chars exceeding this triggers compaction
GUIDE_SECTION_THRESHOLD = 600  # Section must exceed this to move to guides/
_guide_lock = threading.Lock()

# Files to compact (relative to workspace)
GUIDE_SOURCE_FILES = ["AGENT.md", "SOUL.md"]


def auto_compact_guides(workspace):
    """When AGENT.md/SOUL.md exceed threshold, migrate large sections to guides/ directory.

    Pure file operations, zero data loss: cut-paste to guides/, leave read_file pointer in place.
    Idempotent: already-migrated sections (starting with ->) will not be migrated again.
    """
    if not _guide_lock.acquire(blocking=False):
        return {"status": "skip", "reason": "another guide compact in progress"}
    try:
        return _do_compact_guides(workspace)
    finally:
        _guide_lock.release()


def manual_compact_guides(workspace):
    """Manually trigger guide compaction, skip file size threshold."""
    if not _guide_lock.acquire(blocking=False):
        return {"status": "skip", "reason": "another guide compact in progress"}
    try:
        return _do_compact_guides(workspace, force=True)
    finally:
        _guide_lock.release()


def _do_compact_guides(workspace, force=False):
    guides_dir = os.path.join(workspace, "guides")
    results = []

    for filename in GUIDE_SOURCE_FILES:
        filepath = os.path.join(workspace, filename)
        if not os.path.exists(filepath):
            continue

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                file_content = f.read()
        except Exception:
            continue

        if not force and len(file_content) < GUIDE_FILE_THRESHOLD:
            results.append({
                "file": filename,
                "status": "skip",
                "reason": "below threshold",
                "chars": len(file_content),
                "threshold": GUIDE_FILE_THRESHOLD,
            })
            continue

        sections = _parse_md_sections(file_content)
        original_len = len(file_content)
        moved = []
        compacted = False
        # Prefix for guide files: agent_ or soul_
        prefix = filename.replace(".md", "").lower() + "_"

        for section_name in list(sections.keys()):
            if section_name == "_preamble":
                continue

            section_body = sections[section_name]
            # Skip already-compacted sections (pointer lines)
            stripped_body = section_body.strip()
            if stripped_body.startswith("->"):
                continue

            if len(section_body) < GUIDE_SECTION_THRESHOLD:
                continue

            # Create guide file
            # Sanitize section name for filename
            safe_name = _sanitize_filename(section_name)
            guide_filename = prefix + safe_name + ".md"
            guide_path = os.path.join(guides_dir, guide_filename)

            os.makedirs(guides_dir, exist_ok=True)

            # Write section content to guide file
            guide_content = "# %s\n\n%s\n" % (section_name, section_body.strip())
            with open(guide_path, "w", encoding="utf-8") as f:
                f.write(guide_content)

            # Replace section with pointer
            pointer = "-> see guides/%s (use read_file to load on demand)" % guide_filename
            sections[section_name] = pointer + "\n"
            moved.append({
                "section": section_name,
                "file": guide_filename,
                "chars": len(section_body),
            })
            compacted = True

        if compacted:
            new_content = _rebuild_md(sections)
            tmp_path = filepath + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            os.replace(tmp_path, filepath)
            log.info("[memory] compact_guides: %s %d -> %d chars" % (filename, original_len, len(new_content)))
            results.append({
                "file": filename,
                "status": "compacted",
                "before": original_len,
                "after": len(new_content),
                "moved": moved,
            })
        else:
            results.append({
                "file": filename,
                "status": "skip",
                "reason": "no section above threshold",
                "chars": original_len,
            })

    if not results:
        return {"status": "skip", "reason": "no source files found"}

    # Summarize
    any_compacted = any(r["status"] == "compacted" for r in results)
    return {
        "status": "compacted" if any_compacted else "skip",
        "files": results,
    }


def _sanitize_filename(name):
    """Convert section name to safe filename segment."""
    # Remove special chars, keep Chinese + alphanumeric + hyphen
    safe = re.sub(r"[^\w\u4e00-\u9fff-]", "_", name)
    # Collapse multiple underscores
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe[:50]  # Limit length




# ============================================================
#  Daily Diary Material (Structured Extraction, No LLM Dependency)
# ============================================================

def daily_digest(workspace, sessions_dir, user_id):
    """Generate structured summary for today: conversations, files, tasks, memories. Pure data extraction, zero LLM calls."""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    today_start = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = today_start.timestamp()

    result = {"date": today, "conversations": [], "files": [], "tasks": [], "memories_count": 0}

    # 1. Today's conversations (archive .jsonl primary, live session supplementary)
    # Archive contains deduplicated accumulated messages from all snapshots, complete coverage;
    # Live session has only last 40 messages rolling window, but includes increment since last archive.
    session_msgs = {}  # session_name -> {hash -> msg}

    def _extract_text(msg):
        """Extract text summary from message (first 100 chars)."""
        c = msg.get("content", "")
        if isinstance(c, str) and c.strip():
            return c[:100]
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("text"):
                    return part["text"][:100]
        return None

    def _msg_hash(msg):
        """Message dedup hash: role + content first 200 chars."""
        c = msg.get("content", "")
        if isinstance(c, list):
            c = str(c)[:200]
        elif isinstance(c, str):
            c = c[:200]
        return hash((msg.get("role", ""), c))

    # 1a. Collect today's complete messages from archive .jsonl
    archive_dir = os.path.join(workspace, "archive", today)
    if os.path.isdir(archive_dir):
        for fname in os.listdir(archive_dir):
            if not fname.endswith(".jsonl"):
                continue
            # Skip scheduler session
            if fname.startswith("scheduler"):
                continue
            # Extract session name (strip _HHMM time tag)
            # Example: wecom_dm_SENDER_ID_REDACTED_0500.jsonl -> wecom_dm_SENDER_ID_REDACTED
            base = fname.rsplit(".", 1)[0]  # strip .jsonl
            parts_name = base.rsplit("_", 1)
            if len(parts_name) == 2 and parts_name[1].isdigit():
                session_name = parts_name[0]
            else:
                session_name = base
            fpath = os.path.join(archive_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        msg = json.loads(line)
                        h = _msg_hash(msg)
                        session_msgs.setdefault(session_name, {})[h] = msg
            except Exception:
                pass

    # 1b. Supplement from live session (increment since last archive)
    if os.path.isdir(sessions_dir):
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".json") or fname.startswith("scheduler_"):
                continue
            fpath = os.path.join(sessions_dir, fname)
            try:
                if os.path.getmtime(fpath) < today_ts:
                    continue
                with open(fpath, "r", encoding="utf-8") as f:
                    msgs = json.load(f)
                session_name = fname.replace(".json", "")
                for msg in msgs:
                    h = _msg_hash(msg)
                    session_msgs.setdefault(session_name, {})[h] = msg
            except Exception:
                pass

    # 1.5 Subtract yesterday's baseline: count only today's new messages
    yesterday = (today_start - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_dir = os.path.join(workspace, "archive", yesterday)
    baseline_hashes = {}  # session_name -> set of hashes
    if os.path.isdir(yesterday_dir):
        latest_files = {}  # session_name -> (filepath, tag)
        for fname in os.listdir(yesterday_dir):
            if not fname.endswith(".jsonl") or fname.startswith("scheduler"):
                continue
            base = fname.rsplit(".", 1)[0]
            parts_name = base.rsplit("_", 1)
            if len(parts_name) == 2 and parts_name[1].isdigit():
                sname = parts_name[0]
                tag = parts_name[1]
            else:
                sname = base
                tag = "0000"
            if sname not in latest_files or tag > latest_files[sname][1]:
                latest_files[sname] = (os.path.join(yesterday_dir, fname), tag)

        for sname, (fpath, _) in latest_files.items():
            hashes = set()
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        msg = json.loads(line)
                        hashes.add(_msg_hash(msg))
            except Exception:
                pass
            if hashes:
                baseline_hashes[sname] = hashes

    for sname in list(session_msgs.keys()):
        if sname in baseline_hashes:
            session_msgs[sname] = {
                h: msg for h, msg in session_msgs[sname].items()
                if h not in baseline_hashes[sname]
            }
        if not session_msgs.get(sname):
            session_msgs.pop(sname, None)

    # 1c. Aggregate stats for each session
    for session_name, msg_dict in session_msgs.items():
        all_msgs = list(msg_dict.values())
        user_msgs = [m for m in all_msgs if m.get("role") == "user"]
        asst_msgs = [m for m in all_msgs if m.get("role") == "assistant"]
        # Extract topic hints from user messages (up to 10, since we have complete data now)
        topics = []
        for m in user_msgs[-10:]:
            t = _extract_text(m)
            if t:
                topics.append(t)
        result["conversations"].append({
            "session": session_name,
            "user_messages": len(user_msgs),
            "assistant_messages": len(asst_msgs),
            "recent_topics": topics,
        })

    # 2. Today's received files (from index.json)
    files_index = os.path.join(workspace, "files", "index.json")
    if os.path.exists(files_index):
        try:
            with open(files_index, "r", encoding="utf-8") as f:
                all_files = json.load(f)
            for entry in all_files:
                ft = entry.get("time", "")
                if ft.startswith(today):
                    result["files"].append({
                        "filename": entry.get("filename", ""),
                        "type": entry.get("type", ""),
                        "size": entry.get("size", 0),
                        "time": ft,
                        "path": entry.get("path", ""),
                    })
        except Exception:
            pass

    # 3. Scheduled task status
    jobs_file = os.path.join(os.path.dirname(workspace), "jobs.json")
    if os.path.exists(jobs_file):
        try:
            with open(jobs_file, "r", encoding="utf-8") as f:
                jobs = json.load(f)
            for j in jobs:
                info = {"name": j.get("name", ""), "type": j.get("type", ""), "cron": j.get("cron_expr", "")}
                if j.get("created_ts", 0) >= today_ts:
                    info["action"] = "created_today"
                lr = j.get("last_run", 0)
                if lr >= today_ts:
                    info["last_run"] = datetime.fromtimestamp(lr, CST).strftime("%H:%M")
                result["tasks"].append(info)
        except Exception:
            pass

    # 4. Today's new memory count
    if _enabled and _db:
        try:
            table = _get_table(user_id)
            arrow_t = table.to_arrow()
            created_col = arrow_t.column("created_at").to_pylist()
            facts_col = arrow_t.column("fact").to_pylist()
            today_mems = [(c, f) for c, f in zip(created_col, facts_col) if c >= today_ts]
            result["memories_count"] = len(today_mems)
            result["memories_preview"] = [f[:150] for _, f in today_mems[:10]]
        except Exception as e:
            log.warning("[memory] daily_digest memories error: %s" % e)

    return result

# ============================================================
#  Embedding (Zhipu API)
# ============================================================


def _embed(texts):
    """Call Zhipu Embedding-3 API, return list of 1024-dim vectors."""
    if not texts:
        return []

    cfg = _config.get("embedding_api", {})
    api_base = cfg.get("api_base", "https://open.bigmodel.cn/api/paas/v4")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "embedding-3")
    dimension = cfg.get("dimension", 1024)

    body = json.dumps({
        "model": model,
        "input": texts,
        "dimensions": dimension,
    }).encode("utf-8")

    req = urllib.request.Request(
        api_base.rstrip("/") + "/embeddings",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
    )

    # Retry 2 times (avoid permanent memory loss on transient network failure)
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return [item["embedding"] for item in data["data"]]
        except Exception as e:
            last_err = e
            if attempt < 2:
                log.warning("[memory] embed retry %d/2: %s", attempt + 1, e)
                time.sleep(2)
    raise last_err


# ============================================================
#  Compression (SimpleMem Stage 1 Core)
# ============================================================

COMPRESS_PROMPT = """You are a memory compressor. Extract information worth remembering long-term from the conversation.

Conversation content (tag legend: [User] = user message, [AI] = AI assistant reply):
{dialogue}

Output a JSON array, each element:
{{
  "fact": "Complete factual statement (resolve pronouns, fill in timestamps)",
  "keywords": ["keyword1", "keyword2"],
  "persons": ["names of people involved"],
  "timestamp": "YYYY-MM-DD HH:MM or null",
  "topic": "topic category"
}}

Core principle: only record information actively disclosed by the user. [AI] messages are only for context, do not extract memories from them.

Record:
- User's personal information (name, occupation, habits, preferences)
- People mentioned by user (names, relationships, traits) — preserve ALL details about people including personality, stories, opinions, habits, abilities, interaction style, etc. Do NOT summarize or omit.
- User's plans, decisions, goals, commitments
- Items the user explicitly asked to remember

Do NOT record:
- Content returned by AI search/research (time-sensitive, user can re-search when needed)
- AI's feature introductions, operation demos, usage tutorials
- Operation confirmations ("task created", "file written", "reminder set")
- Small talk, greetings, jokes, repeated confirmations

Other rules:
- Replace "he/she/I" with specific names (extract from conversation)
- Replace "tomorrow/next week" with specific dates (infer from conversation time)
- All dates must use YYYY-MM-DD format (e.g., 2026-04-01). Do not accept relative expressions like "3/30" or "next Monday"
- timestamp field must be "YYYY-MM-DD HH:MM" format, no relative time
- If no information worth remembering, return empty array []
- Output only the JSON array, no other text"""


def _format_messages(messages):
    """Join messages into tagged dialogue text to help compression LLM distinguish sources"""
    lines = []
    for m in messages:
        tag = "[User]" if m["role"] == "user" else "[AI]"
        c = m.get("content", "")
        if not c:
            continue
        # Strip AI's <think>...</think> internal reasoning, keep only actual reply
        if m["role"] == "assistant":
            c = re.sub(r"<think>.*?</think>", "", c, flags=re.DOTALL).strip()
            if not c:
                continue
        lines.append("%s %s" % (tag, c))
    return "\n".join(lines)

def _call_compress_llm(prompt):
    """Use LLM to extract structured memories.
    
    NOTE: Intentionally hardcode deepseek-chat for compression (not following user main model config). Reasons:
    1. Compression is background batch processing, does not need kimi-k2.5 tool calling capability
    2. deepseek-chat is 10x+ cheaper, compression quality sufficient
    3. Avoid handling kimi thinking compatibility issues (reasoning_content etc.)
    """
    providers = _llm_config.get("providers", {})
    # Prefer deepseek-chat for compression (cheap and no kimi thinking compatibility issues)
    provider = providers.get("deepseek-chat") or providers.get(_llm_config.get("default", ""))
    if not provider:
        log.error("[memory] no LLM provider for compress")
        return []

    url = provider["api_base"].rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": provider["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + provider["api_key"],
    }

    req = urllib.request.Request(url, data=body, headers=headers)
    timeout = provider.get("timeout", 120)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())

    content = data["choices"][0]["message"].get("content", "")
    if not content:
        return []

    # Extract JSON array from content
    # LLM may wrap with ```json, need to clean
    text = content.strip()
    if text.startswith("```"):
        # Strip ```json and ```
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        # Try to find content between [ and ]
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass

    log.warning("[memory] compress LLM returned unparseable: %s" % content[:200])
    return []


def _cosine_similarity(a, b):
    """Calculate cosine similarity between two vectors"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0
    return dot / (norm_a * norm_b)



# Merge/update threshold: call LLM to decide when similarity >= this value, ADD directly when below
MERGE_SIMILARITY_THRESHOLD = 0.85

MERGE_PROMPT = """Existing memory: {old_fact}
New information: {new_fact}

Determine the relationship between these two pieces of information, output JSON only:
- SKIP: New info completely duplicates existing memory, no new content
- UPDATE: New information corrects or replaces existing memory (e.g., job change, relocation, plan change)
- MERGE: Two pieces complement each other, can merge into one more complete memory

{{"action": "SKIP or UPDATE or MERGE", "fact": "Complete fact after update/merge (leave empty for SKIP)"}}"""


def _decide_memory_action(old_fact, new_fact):
    """Call LLM to determine old/new memory relationship: SKIP/UPDATE/MERGE. Returns (action, merged_fact)."""
    prompt = MERGE_PROMPT.format(old_fact=old_fact, new_fact=new_fact)
    try:
        result = _call_compress_llm_short(prompt)
        if not result:
            return "ADD", new_fact
        action = result.get("action", "ADD").upper()
        if action not in ("SKIP", "UPDATE", "MERGE"):
            return "ADD", new_fact
        fact = result.get("fact", "") or new_fact
        return action, fact
    except Exception as e:
        log.warning("[memory] merge decision failed: %s, defaulting to ADD" % e)
        return "ADD", new_fact


def _call_compress_llm_short(prompt):
    """Lightweight LLM call, returns single JSON object (for merge decision). Reuses deepseek-chat."""
    providers = _llm_config.get("providers", {})
    provider = providers.get("deepseek-chat") or providers.get(_llm_config.get("default", ""))
    if not provider:
        return None

    url = provider["api_base"].rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": provider["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + provider["api_key"],
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    text = data["choices"][0]["message"].get("content", "").strip()
    # Clean markdown wrapping
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    # Try to parse JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
    return None


def _compress_worker(messages, session_key, user_id=None):
    """Background thread: LLM extracts structured memories -> embed -> dedup -> store in LanceDB"""
    # Skip scheduler session: scheduled task prompts are not user info, should not be extracted as memories
    if session_key.startswith("scheduler_"):
        log.info("[memory] skip compress for scheduler session")
        return
    try:
        dialogue = _format_messages(messages)
        if len(dialogue) < 20:
            log.info("[memory] dialogue too short, skip compress")
            return

        # 1. LLM structured extraction
        prompt = COMPRESS_PROMPT.format(dialogue=dialogue)
        memories = _call_compress_llm(prompt)
        if not memories:
            log.info("[memory] no memories extracted from %d messages" % len(messages))
            return

        log.info("[memory] extracted %d memories" % len(memories))

        # 2. Vectorize facts
        facts = [m.get("fact", "") for m in memories if m.get("fact")]
        if not facts:
            return
        embeddings = _embed(facts)
        if len(embeddings) != len(facts):
            log.error("[memory] embedding count mismatch: %d facts vs %d embeddings" % (len(facts), len(embeddings)))
            return

        # 3. Dedup/merge/update: cosine similarity with existing memories, LLM decides action on high similarity
        table = _get_table(user_id)
        new_records = []
        stats = {"add": 0, "skip": 0, "update": 0, "merge": 0}
        for i, (mem, vec) in enumerate(zip(memories, embeddings)):
            fact = mem.get("fact", "")
            if not fact:
                continue

            # Find most similar existing memory
            action = "ADD"
            try:
                existing = table.search(vec).limit(1).to_list()
                if existing and existing[0].get("id") != "seed":
                    sim = 1 - existing[0].get("_distance", 1)
                    old_id = existing[0].get("id", "")
                    old_fact = existing[0].get("fact", "")

                    if sim >= MERGE_SIMILARITY_THRESHOLD:
                        # High similarity: call LLM to decide SKIP/UPDATE/MERGE
                        action, merged_fact = _decide_memory_action(old_fact, fact)
                        log.info("[memory] sim=%.3f action=%s: [%s] vs [%s]" % (
                            sim, action, old_fact[:40], fact[:40]))

                        if action == "SKIP":
                            stats["skip"] += 1
                            continue
                        elif action in ("UPDATE", "MERGE"):
                            # Delete old memory, replace with merged/updated fact
                            try:
                                table.delete('id = "%s"' % old_id)
                            except Exception as e:
                                log.warning("[memory] delete old memory failed: %s" % e)
                            # Re-embed the merged fact
                            new_vecs = _embed([merged_fact])
                            if new_vecs:
                                vec = new_vecs[0]
                            fact = merged_fact
                            stats[action.lower()] += 1
            except Exception:
                pass  # Search failure does not block storage

            if action == "ADD":
                stats["add"] += 1

            new_records.append({
                "id": str(uuid.uuid4()),
                "fact": fact,
                "keywords": json.dumps(mem.get("keywords", []), ensure_ascii=False),
                "persons": json.dumps(mem.get("persons", []), ensure_ascii=False),
                "timestamp": mem.get("timestamp") or "",
                "topic": mem.get("topic", ""),
                "session_key": session_key,
                "created_at": time.time(),
                "vector": vec,
            })

        # 4. Store in LanceDB
        if new_records:
            table.add(new_records)
            log.info("[memory] stored %d memories (add=%d update=%d merge=%d skip=%d)" % (
                len(new_records), stats["add"], stats["update"], stats["merge"], stats["skip"]))
        else:
            log.info("[memory] all %d memories were skipped/duplicates" % len(facts))

    except Exception as e:
        log.error("[memory] compress error: %s" % e, exc_info=True)
