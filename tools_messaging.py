"""
Messaging / file / scheduling / media tools
"""

import json
import os
import subprocess
import time

from tools_base import tool, log, _resolve_path, _strip_markdown, _split_message
import messaging

# POI coordinate cache: search_nearby results auto-cached, send_location looks up by name/address
_poi_cache = {}  # key: poi_name or address -> {"lat": float, "lng": float, "name": str, "address": str}

def _cache_poi(name, address, location_str):
    """Cache POI coordinates, location_str format: lng,lat"""
    coords = location_str.split(",")
    if len(coords) == 2:
        entry = {"lat": float(coords[1]), "lng": float(coords[0]), "name": name, "address": address}
        if name:
            _poi_cache[name] = entry
        if address:
            _poi_cache[address] = entry

def _lookup_poi(label, address):
    """Look up POI coordinates from cache, returns (lat, lng) or None"""
    for key in [label, address]:
        if key and key in _poi_cache:
            e = _poi_cache[key]
            return e["lat"], e["lng"]
    # Fuzzy match: cached name is contained in label, or vice versa
    for key, e in _poi_cache.items():
        if label and (label in key or key in label):
            return e["lat"], e["lng"]
    return None
import scheduler

# --- Basic tools ---

_DANGEROUS_CMDS = ['rm -rf /', 'rm -rf /*', 'dd if=', 'mkfs', ':(){', 'fork bomb',
                   'chmod -R 777 /', 'chown -R', '> /dev/sda', 'shutdown', 'reboot',
                   'init 0', 'init 6', 'kill -9 1', 'killall']

@tool("exec", "Execute a shell command on the server. Can be used for curl, python3, system administration, etc. "
      "Default timeout is 60 seconds. For slow operations like installing packages or downloading files, set timeout up to 300.",
      {"command": {"type": "string", "description": "Shell command to execute"},
       "timeout": {"type": "integer", "description": "Timeout in seconds, default 60, max 300. Recommended 180-300 for install/download operations"}},
      ["command"])
def tool_exec(args, ctx):
    cmd = args["command"]
    # Dangerous command blacklist check
    cmd_lower = cmd.lower().strip()
    for dangerous in _DANGEROUS_CMDS:
        if dangerous in cmd_lower:
            log.warning("[exec] BLOCKED dangerous command: %s", cmd[:200])
            return "[error] Command blocked (security policy): contains dangerous operation '%s'" % dangerous
    log.info("[exec] running: %s", cmd[:200])
    timeout = min(args.get("timeout", 60), 300)
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=ctx["workspace"]
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n[stderr] " + result.stderr) if output else result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        # Prevent large output from blowing up LLM context
        if len(output) > 8000:
            trunc_msg = "\n... [truncated, total %d chars]" % len(output)
            output = output[:8000] + trunc_msg
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "[error] command timed out (%ds)" % timeout


@tool("message", "Send a text message to the current user via messaging platform. Used for proactive messages triggered by scheduled tasks. Not needed for normal conversation replies.",
      {"content": {"type": "string", "description": "Message content"}},
      ["content"])
def tool_message(args, ctx):
    target_id = ctx.get("group_id") if ctx.get("is_group") else ctx["owner_id"]
    owner_id = target_id  # backward compat for send logic below
    chunks = _split_message(_strip_markdown(args["content"]), 1800)
    failed = []
    for i, chunk in enumerate(chunks):
        result = messaging.send_text(owner_id, chunk)
        if result.get("code") != 0:
            failed.append(f"message {i+1}: {result.get('msg', 'unknown error')}")
        if i < len(chunks) - 1:
            time.sleep(0.5)
    if failed:
        return f"[error] Send failed ({len(failed)}/{len(chunks)} messages): " + "; ".join(failed)
    return f"Sent to user ({len(chunks)} messages)"


# --- File tools ---

@tool("read_file", "Read file content. Path is relative to workspace directory.",
      {"path": {"type": "string", "description": "File path (relative to workspace or absolute)"}},
      ["path"])
def tool_read_file(args, ctx):
    fpath = _resolve_path(args["path"], ctx["workspace"])
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        if len(content) > 10000:
            content = content[:10000] + f"\n... (truncated, total {len(content)} chars)"
        return content or "(empty file)"
    except FileNotFoundError:
        return f"[error] file not found: {fpath}"
    except UnicodeDecodeError:
        # Binary file — check if it is an image
        try:
            with open(fpath, "rb") as f:
                header = f.read(8)
            is_image = (
                header[:2] == b'\xff\xd8' or        # JPEG
                header[:4] == b'\x89PNG' or           # PNG
                header[:4] == b'RIFF' or               # WebP
                header[:3] == b'GIF'                    # GIF
            )
            if is_image:
                return "[This is an image file. You already viewed its content when it was received. Please refer to your earlier analysis. If you need to re-examine it, ask the user to resend the image.]"
        except Exception:
            pass
        return "[error] This is a binary file and cannot be read as text"
    except Exception as e:
        return f"[error] {e}"


@tool("write_file", "Write to a file (overwrite). Path is relative to workspace directory.",
      {"path": {"type": "string", "description": "File path"},
       "content": {"type": "string", "description": "File content"}},
      ["path", "content"])
def tool_write_file(args, ctx):
    fpath = _resolve_path(args["path"], ctx["workspace"])
    try:
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(args["content"])
        return f"Written to {fpath} ({len(args['content'])} chars)"
    except Exception as e:
        return f"[error] {e}"


@tool("edit_file", "Edit a file: replace old text with new text. To append content, use a string at the end of the file as old, and that string plus the new content as new.",
      {"path": {"type": "string", "description": "File path"},
       "old": {"type": "string", "description": "Original text to replace"},
       "new": {"type": "string", "description": "Replacement text"}},
      ["path", "old", "new"])
def tool_edit_file(args, ctx):
    fpath = _resolve_path(args["path"], ctx["workspace"])
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        if args["old"] not in content:
            return f"[error] old string not found in {fpath}"
        content = content.replace(args["old"], args["new"], 1)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)
        # When MEMORY.md is modified, clean up stale memories in LanceDB that contradict old text
        if fpath.endswith("MEMORY.md"):
            try:
                import memory as mem_mod
                removed = mem_mod.invalidate_stale_facts(args["old"], user_id=ctx.get("owner_id"))
                if removed:
                    log.info("[edit_file] invalidated %d stale LanceDB facts after MEMORY.md edit", removed)
            except Exception as e:
                log.error("[edit_file] memory invalidation failed: %s", e)
        return f"Edited {fpath}"
    except FileNotFoundError:
        return f"[error] file not found: {fpath}"
    except Exception as e:
        return f"[error] {e}"


@tool("list_files", "List received and saved files. Can filter by type (image/video/file/voice/GIF), or list all. Returns the most recent files.",
      {"type": {"type": "string", "description": "File type filter (image/video/file/voice/GIF), leave empty for all"},
       "limit": {"type": "integer", "description": "Number of results to return (default 20)"}})
def tool_list_files(args, ctx):
    index_path = os.path.join(ctx["workspace"], "files", "index.json")
    if not os.path.exists(index_path):
        return "No files received yet."
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
    except Exception:
        return "Failed to read file index."

    file_type = args.get("type", "")
    if file_type:
        index = [e for e in index if e.get("type") == file_type]

    limit = args.get("limit", 20)
    recent = index[-limit:]
    recent.reverse()

    if not recent:
        return f"No files found of type '{file_type}'." if file_type else "No files received yet."

    lines = [f"Total {len(index)} files" + (f" (type: {file_type})" if file_type else "") + f", showing most recent {len(recent)}:"]
    for e in recent:
        size_kb = e.get("size", 0) / 1024
        size_str = f"{size_kb/1024:.1f}MB" if size_kb > 1024 else f"{size_kb:.0f}KB"
        lines.append(f"  - [{e.get('type', '?')}] {e.get('filename', '?')} ({size_str}) {e.get('time', '?')}")
        lines.append(f"    Path: {e.get('path', '?')}")
    return "\n".join(lines)


# --- Scheduler tools ---

@tool("schedule", "Create a scheduled task. For one-time tasks use target_time, for recurring tasks use cron_expr. When triggered, the message will be sent to the LLM as a user message for processing.",
      {"name": {"type": "string", "description": "Task name (unique identifier)"},
       "message": {"type": "string", "description": "Message sent to LLM when triggered (should include instructions like 'use message tool to send to user')"},
       "target_time": {"type": "string", "description": "Target trigger time, format 'YYYY-MM-DD HH:MM' (local time), recommended for one-time tasks"},
       "delay_seconds": {"type": "integer", "description": "Delay in seconds (one-time task, prefer target_time instead)"},
       "cron_expr": {"type": "string", "description": "Cron expression (recurring task, e.g. '0 9 * * *')"},
       "once": {"type": "boolean", "description": "Execute only once (default true, only applies to cron_expr)"}},
      ["name", "message"])
def tool_schedule(args, ctx):
    args["owner_id"] = ctx.get("owner_id", "")
    if ctx.get("is_group"):
        args["group_id"] = ctx["group_id"]
    return scheduler.add(args)


@tool("list_schedules", "List all scheduled tasks", {})
def tool_list_schedules(args, ctx):
    return scheduler.list_all(owner_id=ctx.get("owner_id", ""))


@tool("remove_schedule", "Remove a scheduled task",
      {"name": {"type": "string", "description": "Task name"}},
      ["name"])
def tool_remove_schedule(args, ctx):
    return scheduler.remove(args["name"], owner_id=ctx.get("owner_id", ""))


# --- Media sending tools ---

@tool("send_image", "Send an image to the current user. Supports HTTP URL or server local file path. The image will be displayed as an image in the chat.",
      {"path": {"type": "string", "description": "Image URL (http/https) or server local file path"},
       "caption": {"type": "string", "description": "Optional text caption for the image"}},
      ["path"])
def tool_send_image(args, ctx):
    to_id = ctx.get("group_id") if ctx.get("is_group") else ctx["owner_id"]
    result = messaging.upload_and_send(to_id, args["path"], args.get("caption", ""), ctx["workspace"])
    return "Image sent to user" if result.get("code") == 0 else f"[error] Send failed: {result.get('msg', '?')}"


@tool("send_file", "Send a file to the current user (PDF, Excel, Word, ZIP, etc.). Supports HTTP URL or server local file path.",
      {"path": {"type": "string", "description": "File URL (http/https) or server local file path"},
       "caption": {"type": "string", "description": "Optional text caption for the file"}},
      ["path"])
def tool_send_file(args, ctx):
    to_id = ctx.get("group_id") if ctx.get("is_group") else ctx["owner_id"]
    result = messaging.upload_and_send(to_id, args["path"], args.get("caption", ""), ctx["workspace"])
    return "File sent to user" if result.get("code") == 0 else f"[error] Send failed: {result.get('msg', '?')}"


@tool("send_video", "Send a video to the current user. Supports HTTP URL or server local MP4 file path.",
      {"path": {"type": "string", "description": "Video URL (http/https) or server local file path"},
       "caption": {"type": "string", "description": "Optional text caption for the video"}},
      ["path"])
def tool_send_video(args, ctx):
    to_id = ctx.get("group_id") if ctx.get("is_group") else ctx["owner_id"]
    result = messaging.upload_and_send(to_id, args["path"], args.get("caption", ""), ctx["workspace"])
    return "Video sent to user" if result.get("code") == 0 else f"[error] Send failed: {result.get('msg', '?')}"


@tool("send_link", "Send a rich link card to the current user. Displayed as a card with title, description, and icon in the chat; opens the URL on click.",
      {"title": {"type": "string", "description": "Card title"},
       "desc": {"type": "string", "description": "Card description"},
       "link_url": {"type": "string", "description": "URL to open on click"},
       "icon_url": {"type": "string", "description": "Card icon URL (optional)"}},
      ["title", "desc", "link_url"])
def tool_send_link(args, ctx):
    to_id = ctx.get("group_id") if ctx.get("is_group") else ctx["owner_id"]
    result = messaging.send_link(to_id, args["title"], args["desc"], args["link_url"], args.get("icon_url", ""))
    return f"Link card sent to user: {args['title']}" if result.get("code") == 0 else f"[error] Send failed: {result.get('msg', '?')}"



# --- Location sending tool ---

def _gaode_geocode(address):
    """Gaode geocoding: address -> coordinates. Returns (lat, lng, formatted_address) or None."""
    import urllib.request, urllib.parse
    config_path = os.environ.get("AGENT_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        key = config.get("gaode_key", "")
        if not key:
            return None
        url = "https://restapi.amap.com/v3/geocode/geo?" + urllib.parse.urlencode({
            "address": address, "key": key, "output": "JSON"
        })
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "1" and data.get("geocodes"):
            geo = data["geocodes"][0]
            loc = geo["location"].split(",")
            return (float(loc[1]), float(loc[0]), geo.get("formatted_address", address))
    except Exception as e:
        log.error(f"[gaode] geocode error: {e}")
    return None



def _gaode_nearby(location, keywords="", types="", radius=3000, count=5):
    """Gaode nearby search: coordinates + keywords -> POI list.
    location: "lng,lat" format (Gaode standard: longitude first)
    Returns list[dict] or None. Each dict contains name/address/location/distance/tel/rating/cost/type.
    """
    import urllib.request, urllib.parse
    config_path = os.environ.get("AGENT_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        key = config.get("gaode_key", "")
        if not key:
            return None
        params = {"location": location, "key": key, "output": "JSON",
                  "radius": str(radius), "offset": str(count), "page": "1",
                  "extensions": "all", "sortrule": "weight"}
        if keywords:
            params["keywords"] = keywords
        if types:
            params["types"] = types
        url = "https://restapi.amap.com/v3/place/around?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("status") != "1":
            log.error(f"[gaode] nearby error: {data.get('info')}")
            return None
        results = []
        for poi in data.get("pois", []):
            biz = poi.get("biz_ext", {}) or {}
            results.append({
                "name": poi.get("name", ""),
                "address": poi.get("address", ""),
                "location": poi.get("location", ""),  # "lng,lat"
                "distance": poi.get("distance", ""),   # meters
                "tel": poi.get("tel", ""),
                "type": poi.get("type", ""),
                "rating": biz.get("rating", ""),
                "cost": biz.get("cost", ""),
                "business_area": poi.get("business_area", ""),
                "tag": poi.get("tag", ""),
            })
        return results
    except Exception as e:
        log.error(f"[gaode] nearby error: {e}")
    return None


def _gaode_text_search(keywords, city="", types="", count=5):
    """Gaode keyword search: keywords + city -> POI list. No coordinates needed."""
    import urllib.request, urllib.parse
    config_path = os.environ.get("AGENT_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        key = config.get("gaode_key", "")
        if not key:
            return None
        params = {"keywords": keywords, "key": key, "output": "JSON",
                  "offset": str(count), "page": "1", "extensions": "all"}
        if city:
            params["city"] = city
            params["citylimit"] = "true"
        if types:
            params["types"] = types
        url = "https://restapi.amap.com/v3/place/text?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("status") != "1":
            log.error(f"[gaode] text_search error: {data.get('info')}")
            return None
        results = []
        for poi in data.get("pois", []):
            biz = poi.get("biz_ext", {}) or {}
            results.append({
                "name": poi.get("name", ""),
                "address": poi.get("address", ""),
                "location": poi.get("location", ""),
                "tel": poi.get("tel", ""),
                "type": poi.get("type", ""),
                "rating": biz.get("rating", ""),
                "cost": biz.get("cost", ""),
                "business_area": poi.get("business_area", ""),
                "tag": poi.get("tag", ""),
            })
        return results
    except Exception as e:
        log.error(f"[gaode] text_search error: {e}")
    return None


@tool("search_nearby", "Search nearby places (restaurants/attractions/hotels/supermarkets/hospitals, etc.). "
      "Based on Gaode Maps data, returns name, address, coordinates, distance, rating, average cost, phone, etc. "
      "Found places can be sent to user via send_location. Two modes: "
      "1. Nearby search: provide location (coordinates or address) + keywords to search around a specific location. "
      "2. City search: provide keywords + city to search the entire city.",
      {"keywords": {"type": "string", "description": "Search keywords (e.g. 'hotpot', 'cafe', 'gas station', 'hospital')"},
       "location": {"type": "string", "description": "Center location, can be coordinates '116.397,39.908' or an address (auto-geocoded). Required for nearby search"},
       "city": {"type": "string", "description": "City name, used for city-wide search"},
       "radius": {"type": "integer", "description": "Search radius in meters, default 3000, max 50000"},
       "count": {"type": "integer", "description": "Number of results, default 5, max 25"}},
      ["keywords"])
def tool_search_nearby(args, ctx):
    keywords = args["keywords"]
    location = args.get("location", "")
    city = args.get("city", "")
    radius = min(int(args.get("radius", 3000)), 50000)
    count = min(int(args.get("count", 5)), 25)

    results = None

    if location:
        # Nearby search mode
        # If location is not in coordinate format, geocode first
        if not all(c in "0123456789.," for c in location.strip()):
            geo = _gaode_geocode(location)
            if not geo:
                return f"[error] Cannot resolve location: {location}"
            lat, lng, _ = geo
            loc_str = f"{lng},{lat}"
        else:
            loc_str = location.strip()
        results = _gaode_nearby(loc_str, keywords=keywords, radius=radius, count=count)
    elif city:
        # City search mode
        results = _gaode_text_search(keywords, city=city, count=count)
    else:
        return "[error] Please provide location (nearby search) or city (city search)"

    if not results:
        return f"No places found for '{keywords}'"
    if len(results) == 0:
        return f"No places found for '{keywords}'"

    # Format output
    lines = [f"Found {len(results)} results:"]
    for i, poi in enumerate(results, 1):
        parts = [f"**{i}. {poi['name']}**"]
        if poi.get("rating") and poi["rating"] != "[]":
            parts.append(f"Rating: {poi['rating']}")
        if poi.get("cost") and poi["cost"] != "[]":
            parts.append(f"Avg cost: {poi['cost']}")
        if poi.get("distance"):
            parts.append(f"Distance: {poi['distance']}m")
        if poi.get("address"):
            parts.append(f"Address: {poi['address']}")
        if poi.get("tel") and poi["tel"] != "[]":
            parts.append(f"Phone: {poi['tel']}")
        if poi.get("tag") and poi["tag"] != "[]":
            parts.append(f"Tags: {poi['tag']}")
        if poi.get("business_area") and poi["business_area"] != "[]":
            parts.append(f"Area: {poi['business_area']}")
        # Cache coordinates and output
        if poi.get("location"):
            _cache_poi(poi["name"], poi.get("address", ""), poi["location"])
            coords = poi["location"].split(",")
            if len(coords) == 2:
                parts.append(f"Coords: lng={coords[0]},lat={coords[1]}")
        lines.append(" | ".join(parts))
    return "".join(lines)



@tool("send_location", "Send a location card to the current user. Coordinates from search_nearby can be passed directly to latitude/longitude to avoid precision loss from re-geocoding.",
      {"address": {"type": "string", "description": "Detailed address (shown below card title)"},
       "label": {"type": "string", "description": "Location name (shown as card title)"},
       "latitude": {"type": "number", "description": "Latitude (from search_nearby results, optional)"},
       "longitude": {"type": "number", "description": "Longitude (from search_nearby results, optional)"}},
      ["address"])
def tool_send_location(args, ctx):
    address = args["address"]
    label = args.get("label", "")
    lat = args.get("latitude")
    lng = args.get("longitude")
    # Priority: 1. Explicitly provided coordinates 2. POI cache 3. Geocode
    if lat and lng:
        lat, lng = float(lat), float(lng)
    else:
        cached = _lookup_poi(label, address)
        if cached:
            lat, lng = cached
            log.info(f"[send_location] using cached coords for {label or address}: {lat},{lng}")
        else:
            result = _gaode_geocode(address)
            if not result:
                return f"[error] Cannot resolve address: {address} (geocoding API returned no results)"
            lat, lng, formatted = result
            if not label:
                label = formatted
    to_id = ctx.get("group_id") if ctx.get("is_group") else ctx["owner_id"]
    send_result = messaging.send_location(to_id, lat, lng, label or address, address)
    if send_result.get("code") == 0:
        return f"Location sent: {label or address} ({lat:.6f}, {lng:.6f})"
    return f"[error] Failed to send location: {send_result.get('msg', '?')}"


# --- Contact card sending tool ---

@tool("send_namecard", "Send a contact card to the current user. Security restriction: only owner can use this, and by default only the AI's own card can be sent. "
      "Use when user says 'send me your contact info' or 'recommend you to someone'.",
      {"contact_id": {"type": "string", "description": "Contact ID to share (leave empty to send AI's own card)"}},
      [])
def tool_send_namecard(args, ctx):
    # Security restriction: only owner can call this
    config_path = os.environ.get("AGENT_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return "[error] Failed to read config file"

    owner_ids = config.get("owner_ids", [])
    users_cfg = config.get("users", {})
    is_owner = (str(ctx["owner_id"]) in [str(x) for x in owner_ids] or
                users_cfg.get(str(ctx["owner_id"]), {}).get("role") == "owner")

    contact_id = args.get("contact_id", "").strip()
    ai_wechat_id = config.get("messaging", {}).get("wechat_id", "")

    if not contact_id:
        # Default: send AI's own card
        contact_id = ai_wechat_id
    elif not is_owner:
        # Non-owner can only send AI's card
        return "Sorry, for privacy protection, I can only send my own contact card to you."

    if not contact_id:
        return "[error] AI card ID not configured (messaging.wechat_id)"

    to_id = ctx.get("group_id") if ctx.get("is_group") else ctx["owner_id"]
    result = messaging.send_namecard(to_id, contact_id)
    if result.get("code") == 0:
        who = "my contact card" if contact_id == ai_wechat_id else f"contact card for {contact_id}"
        return f"Sent {who} to user"
    return f"[error] Failed to send contact card: {result.get('msg', '?')}"
