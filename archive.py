#!/usr/bin/env python3
"""
Daily Black Box Archive Script

Runs twice daily (14:00 + 02:00), saves all objective events.
Archive directory: workspace/archive/YYYY-MM-DD/

Archive contents:
1. Conversation session snapshots (full text, no truncation)
2. Scheduled task session snapshots
3. System behavior logs (journald)
4. File index snapshot (image/voice/file references)
5. Scheduled task config snapshot (jobs.json)

Pure standard library, no external dependencies.
"""

import json
import os
import shutil
import subprocess
import time

BASE_DIR = os.environ.get("AGENT_DATA", os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.join(BASE_DIR, "workspace")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
ARCHIVE_BASE = os.path.join(WORKSPACE, "archive")


def _today():
    return time.strftime("%Y-%m-%d")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _archive_dir(date_str=None):
    d = os.path.join(ARCHIVE_BASE, date_str or _today())
    os.makedirs(d, exist_ok=True)
    return d


def archive_sessions(archive_dir, tag):
    """Snapshot all session files to archive directory"""
    if not os.path.isdir(SESSIONS_DIR):
        return {"status": "skip", "msg": "sessions/ does not exist"}

    saved = []
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        src = os.path.join(SESSIONS_DIR, fname)
        # Skip sessions not modified today (avoid archiving stale zombie files)
        if os.path.getmtime(src) < time.mktime(time.strptime(_today(), "%Y-%m-%d")):
            continue
        base = fname.replace(".json", "")
        dest_name = "%s_%s.jsonl" % (base, tag)
        dest = os.path.join(archive_dir, dest_name)

        try:
            with open(src, "r", encoding="utf-8") as f:
                msgs = json.load(f)

            # If archive file exists, merge and deduplicate (by content hash)
            existing = []
            if os.path.exists(dest):
                with open(dest, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            existing.append(line)

            existing_set = set(existing)
            new_count = 0
            with open(dest, "a", encoding="utf-8") as f:
                for msg in msgs:
                    line = json.dumps(msg, ensure_ascii=False)
                    if line not in existing_set:
                        f.write(line + "\n")
                        existing_set.add(line)
                        new_count += 1

            saved.append({"file": fname, "total": len(msgs), "new": new_count})
        except Exception as e:
            saved.append({"file": fname, "error": str(e)})

    return {"status": "ok", "sessions": saved}


def archive_journald(archive_dir):
    """Export today's journald logs"""
    # Container env has no journald, logs accessible via docker logs
    if os.path.exists("/.dockerenv"):
        return {"status": "skip", "msg": "container env, use docker logs"}
    today = _today()
    dest = os.path.join(archive_dir, "system.log")

    try:
        result = subprocess.run(
            ["journalctl", "-u", "agent", "--no-pager",
             "--since", today, "--output", "short-iso"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(result.stdout)
            lines = len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
            return {"status": "ok", "lines": lines}
        else:
            return {"status": "error", "msg": result.stderr[:200]}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def archive_files_index(archive_dir):
    """Snapshot file index"""
    index_path = os.path.join(WORKSPACE, "files", "index.json")
    if not os.path.exists(index_path):
        return {"status": "skip", "msg": "files/index.json does not exist"}

    dest = os.path.join(archive_dir, "files_index.json")
    try:
        shutil.copy2(index_path, dest)
        with open(index_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        today = _today()
        today_files = [e for e in entries if e.get("time", "").startswith(today)]
        return {"status": "ok", "total": len(entries), "today": len(today_files)}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def archive_jobs(archive_dir):
    """Snapshot scheduled task config"""
    jobs_path = os.path.join(BASE_DIR, "jobs.json")
    if not os.path.exists(jobs_path):
        return {"status": "skip", "msg": "jobs.json does not exist"}

    dest = os.path.join(archive_dir, "jobs.json")
    try:
        shutil.copy2(jobs_path, dest)
        with open(jobs_path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        return {"status": "ok", "count": len(jobs)}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def archive_daily_memory(archive_dir):
    """Snapshot today's memory file (if exists)"""
    today = _today()
    src = os.path.join(WORKSPACE, "memory", "%s.md" % today)
    if not os.path.exists(src):
        return {"status": "skip", "msg": "no daily log file today"}

    dest = os.path.join(archive_dir, "memory_%s.md" % today)
    try:
        shutil.copy2(src, dest)
        size = os.path.getsize(src)
        return {"status": "ok", "size_bytes": size}
    except Exception as e:
        return {"status": "error", "msg": str(e)}


def run_archive():
    """Execute full archive"""
    t0 = time.time()
    today = _today()
    tag = time.strftime("%H%M")  # e.g. "1400" or "0200"
    archive_dir = _archive_dir(today)

    results = {
        "timestamp": _now(),
        "tag": tag,
        "archive_dir": archive_dir,
        "sessions": archive_sessions(archive_dir, tag),
        "journald": archive_journald(archive_dir),
        "files_index": archive_files_index(archive_dir),
        "jobs": archive_jobs(archive_dir),
        "daily_memory": archive_daily_memory(archive_dir),
    }
    results["duration_ms"] = int((time.time() - t0) * 1000)

    # Save archive metadata
    meta_path = os.path.join(archive_dir, "meta_%s.json" % tag)
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return results


if __name__ == "__main__":
    report = run_archive()
    print(json.dumps(report, ensure_ascii=False, indent=2))
