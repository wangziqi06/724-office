"""
Memory System - Three-Stage Pipeline

1. Compress: conversation -> LLM extracts structured memories (key facts + people + time + keywords)
2. Deduplicate: new memory vs existing memories cosine similarity, >threshold skip
3. Retrieve: user message -> embedding -> LanceDB vector search -> return relevant memories

Storage: LanceDB (embedded, file-level, no standalone service)
Vectorization: Any OpenAI-compatible embedding API (1024 dimensions)
"""

import json
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
_llm_config = {}    # models config (used for calling LLM during compression)
_db = None          # LanceDB connection
_table = None       # LanceDB memories table
_enabled = False
_context_cache = {} # session_key -> str (pre-computed memory summary, for zero-latency hardware channels)

# ============================================================
#  Public API (4 functions)
# ============================================================


def init(config, llm_config, db_path):
    """Initialize LanceDB connection + embedding config. Called once at startup."""
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
        # Open or create table
        try:
            _table = _db.open_table("memories")
            count = _table.count_rows()
            log.info("[memory] opened table, %d memories" % count)
        except Exception:
            # Table doesn't exist, insert seed data to create schema
            import numpy as np
            dim = embedding_cfg.get("dimension", 1024)
            seed = [{
                "id": "seed",
                "fact": "System initialized",
                "keywords": "[]",
                "persons": "[]",
                "timestamp": "",
                "topic": "system",
                "session_key": "init",
                "created_at": time.time(),
                "vector": np.zeros(dim).tolist(),
            }]
            _table = _db.create_table("memories", seed)
            log.info("[memory] created new table")

        _enabled = True
        log.info("[memory] initialized, db_path=%s" % db_path)
    except Exception as e:
        log.error("[memory] init failed: %s" % e, exc_info=True)


def retrieve(user_msg, session_key, top_k=None):
    """Retrieve relevant memories, return formatted text block. Synchronous."""
    if not _enabled or not _table:
        return ""
    if top_k is None:
        top_k = _config.get("retrieve_top_k", 5)

    try:
        query_vec = _embed([user_msg])
        if not query_vec:
            return ""
        results = _table.search(query_vec[0]).limit(top_k).to_list()
        if not results:
            return ""

        # Filter out seed data and low-quality results
        filtered = [r for r in results if r.get("id") != "seed" and r.get("fact", "") != "System initialized"]
        if not filtered:
            return ""

        lines = ["[Relevant Memories]"]
        for r in filtered:
            line = "- " + r["fact"]
            ts = r.get("timestamp", "")
            if ts:
                line += " (%s)" % ts
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        log.error("[memory] retrieve error: %s" % e)
        return ""


def compress_async(evicted_messages, session_key):
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
        # Skip assistant messages that only have tool_calls (no content)
        if role == "assistant" and m.get("tool_calls"):
            continue
        msgs.append(m)

    if len(msgs) < 2:
        # Too few messages, not worth compressing
        return

    t = threading.Thread(target=_compress_worker, args=(msgs, session_key), daemon=True)
    t.start()
    log.info("[memory] compress started in background (%d messages)" % len(msgs))


def get_cached_context(session_key):
    """Zero-latency: return pre-computed memory summary. For hardware/voice channels."""
    return _context_cache.get(session_key, "")


# ============================================================
#  Embedding (OpenAI-compatible API)
# ============================================================


def _embed(texts):
    """Call embedding API, return list of vectors.

    Supports two formats:
    - OpenAI-compatible: {"model": ..., "input": [...], "dimensions": N}
    - MiniMax embo-01: {"model": "embo-01", "texts": [...], "type": "db"|"query"}
      (auto-detected when api_base contains 'minimax')
    """
    if not texts:
        return []

    cfg = _config.get("embedding_api", {})
    api_base = cfg.get("api_base", "https://api.example.com/v1")
    api_key = cfg.get("api_key", "")
    model = cfg.get("model", "text-embedding-3-small")
    dimension = cfg.get("dimension", 1024)

    is_minimax = "minimax" in api_base.lower()

    if is_minimax:
        # MiniMax native embedding API (embo-01, 1536 dimensions)
        embed_type = cfg.get("type", "db")
        body = json.dumps({
            "model": model,
            "texts": texts,
            "type": embed_type,
        }).encode("utf-8")
    else:
        # OpenAI-compatible embedding API
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

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    if is_minimax:
        # MiniMax returns {"vectors": [[...], [...]]}
        # On rate limit, vectors may be null
        return data.get("vectors") or []
    else:
        return [item["embedding"] for item in data["data"]]


# ============================================================
#  Compression (Three-Stage Core)
# ============================================================

COMPRESS_PROMPT = """You are a memory compressor. Extract structured memories from the following conversation.

Conversation:
{dialogue}

For each piece of valuable information, output a JSON array, each element:
{{
  "fact": "Complete factual statement (resolve pronouns, include timestamps)",
  "keywords": ["keyword1", "keyword2"],
  "persons": ["person names involved"],
  "timestamp": "YYYY-MM-DD HH:MM or null",
  "topic": "topic category"
}}

Rules:
- Only extract information with long-term value (preferences, plans, contacts, decisions, facts)
- Skip chitchat, greetings, repeated confirmations, pure tool call results
- Replace "he/she/I" with specific names (owner = user)
- Replace "tomorrow/next week" with specific dates (infer from conversation time)
- If the conversation has nothing worth remembering, return empty array []
- Output only the JSON array, no other text"""


def _format_messages(messages):
    """Format message list into conversation text"""
    lines = []
    for m in messages:
        role = "User" if m["role"] == "user" else "Assistant"
        content = m.get("content", "")
        if content:
            lines.append("%s: %s" % (role, content))
    return "\n".join(lines)


def _call_compress_llm(prompt):
    """Use LLM to extract structured memories. Prefer cheaper models for compression."""
    providers = _llm_config.get("providers", {})
    # Prefer a cheap model for compression (avoids compatibility issues with thinking models)
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
    # LLM may wrap in ```json, need to clean
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        # Try finding content between [ and ]
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
    """Compute cosine similarity between two vectors"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0
    return dot / (norm_a * norm_b)


def _compress_worker(messages, session_key):
    """Background thread: LLM extract -> embed -> deduplicate -> store in LanceDB"""
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

        # 3. Deduplicate: cosine similarity against existing memories
        threshold = _config.get("similarity_threshold", 0.92)
        new_records = []
        for i, (mem, vec) in enumerate(zip(memories, embeddings)):
            fact = mem.get("fact", "")
            if not fact:
                continue

            # Check if duplicate of existing memory
            try:
                existing = _table.search(vec).limit(1).to_list()
                if existing and existing[0].get("id") != "seed":
                    sim = 1 - existing[0].get("_distance", 1)
                    if sim > threshold:
                        log.info("[memory] skip duplicate (sim=%.3f): %s" % (sim, fact[:50]))
                        continue
            except Exception:
                pass  # Search failure shouldn't block storage

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
            _table.add(new_records)
            log.info("[memory] stored %d new memories (skipped %d duplicates)" % (
                len(new_records), len(facts) - len(new_records)))
        else:
            log.info("[memory] all %d memories were duplicates, nothing stored" % len(facts))

    except Exception as e:
        log.error("[memory] compress error: %s" % e, exc_info=True)
