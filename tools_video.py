"""
Video processing tools — trim, bgm, generate, auto_cut (silence removal)
"""

import json
import os
import subprocess
import time
import urllib.request

import messaging

from tools_base import tool, log

# --- Helper functions ---

def _ensure_local(path, workspace, label="file"):
    """If path is a URL, download to /tmp/ and return local path; otherwise return as-is"""
    if path.startswith("http://") or path.startswith("https://"):
        ext = os.path.splitext(urllib.parse.urlparse(path).path)[1] or ".mp4"
        local = "/tmp/agent_%s_%d%s" % (label, int(time.time()), ext)
        log.info("[video] downloading %s -> %s" % (path[:80], local))
        urllib.request.urlretrieve(path, local)
        return local
    return path


def _video_output_path(workspace, suffix=""):
    """Generate output path under workspace/files/YYYY-MM/"""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    out_dir = os.path.join(workspace, "files", now.strftime("%Y-%m"))
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, "video_%s%s.mp4" % (now.strftime("%Y%m%d_%H%M%S"), suffix))


def _compress_if_needed(path, max_mb=3):
    """If video exceeds max_mb, compress and return new path"""
    size_mb = os.path.getsize(path) / 1024 / 1024
    if size_mb <= max_mb:
        return path
    compressed = path.replace(".mp4", "_compressed.mp4")
    cmd = ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-crf", "28",
           "-preset", "fast", "-c:a", "aac", "-b:a", "128k", compressed]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode == 0 and os.path.exists(compressed):
        log.info("[video] compressed %.1fMB -> %.1fMB" % (size_mb, os.path.getsize(compressed) / 1024 / 1024))
        return compressed
    return path


def _detect_nonsilent_segments(audio_path, silence_thresh_db=-40, min_silence_ms=500, padding_ms=100):
    """Use ffmpeg silencedetect to find silent segments, return non-silent time ranges [(start, end), ...]"""
    # Use ffmpeg silencedetect filter (pure ffmpeg, no pydub dependency)
    cmd = [
        "ffmpeg", "-i", audio_path, "-af",
        "silencedetect=noise=%ddB:d=%.3f" % (silence_thresh_db, min_silence_ms / 1000.0),
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    stderr = result.stderr

    # Get total duration
    duration = None
    for line in stderr.split("\n"):
        if "Duration:" in line:
            # Duration: 00:01:23.45, ...
            import re
            m = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.\d+)", line)
            if m:
                h, mins, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
                duration = h * 3600 + mins * 60 + s
                break

    if duration is None:
        log.warning("[video] could not detect duration, skipping silence removal")
        return None

    # Parse silencedetect output
    import re
    silence_starts = []
    silence_ends = []
    for line in stderr.split("\n"):
        # silence_start: 1.234
        m = re.search(r"silence_start:\s+([\d.]+)", line)
        if m:
            silence_starts.append(float(m.group(1)))
        # silence_end: 2.345 | silence_duration: 1.111
        m = re.search(r"silence_end:\s+([\d.]+)", line)
        if m:
            silence_ends.append(float(m.group(1)))

    if not silence_starts:
        log.info("[video] no silence detected")
        return [(0, duration)]

    # Build non-silent segments (add padding to avoid cutting too tight)
    pad = padding_ms / 1000.0
    segments = []

    # If audio does not start with silence
    if silence_starts[0] > 0.1:
        segments.append((0, silence_starts[0] + pad))

    # Non-silent segments between silence gaps
    for i, end in enumerate(silence_ends):
        seg_start = max(0, end - pad)
        if i + 1 < len(silence_starts):
            seg_end = min(duration, silence_starts[i + 1] + pad)
        else:
            seg_end = duration
        if seg_end - seg_start > 0.1:  # Ignore very short fragments
            segments.append((seg_start, seg_end))

    # Merge overlapping segments
    if not segments:
        return [(0, duration)]

    merged = [segments[0]]
    for start, end in segments[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    log.info("[video] detected %d non-silent segments from %.1fs total (removed %d silence gaps)" %
             (len(merged), duration, len(silence_starts)))
    return merged


@tool("trim_video", "Trim video: cut a specific time range, or automatically remove pauses/silence (auto_cut=true).",
      {"input_path": {"type": "string", "description": "Video file path (local path or URL)"},
       "start": {"type": "string", "description": "Start time, format HH:MM:SS or seconds (optional in auto_cut mode)"},
       "end": {"type": "string", "description": "End time, format HH:MM:SS or seconds (optional, defaults to end of video)"},
       "auto_cut": {"type": "boolean", "description": "Automatically remove pauses/silence (ideal for removing stutters in talking-head videos), default false"},
       "silence_thresh": {"type": "number", "description": "Silence detection threshold (dB), default -40. Higher values (e.g. -30) are more aggressive, removing more low-volume segments"},
       "min_silence": {"type": "number", "description": "Minimum silence duration (ms), default 500. Pauses shorter than this are kept"},
       "send_to": {"type": "string", "description": "Send the result to this recipient (optional, if omitted just saves)"}},
      ["input_path"])
def tool_trim_video(args, ctx):
    input_path = _ensure_local(args["input_path"], ctx["workspace"], "trim_in")

    auto_cut = args.get("auto_cut", False)

    if auto_cut:
        # Smart pause removal mode: detect silence -> concatenate non-silent segments
        silence_thresh = args.get("silence_thresh", -40)
        min_silence = args.get("min_silence", 500)

        segments = _detect_nonsilent_segments(input_path, silence_thresh, min_silence)
        if segments is None:
            return "[error] Unable to analyze video audio, please verify the video file is intact"

        if len(segments) <= 1:
            return "No significant pauses detected in the video, no trimming needed."

        # Use ffmpeg concat demuxer to join non-silent segments
        output_path = _video_output_path(ctx["workspace"], "_autocut")
        concat_file = "/tmp/agent_concat_%d.txt" % int(time.time())

        # Cut each segment first
        seg_files = []
        for i, (start, end) in enumerate(segments):
            seg_path = "/tmp/agent_seg_%d_%d.mp4" % (int(time.time()), i)
            cmd = ["ffmpeg", "-y", "-ss", "%.3f" % start, "-to", "%.3f" % end,
                   "-i", input_path, "-c:v", "libx264", "-crf", "23",
                   "-preset", "fast", "-c:a", "aac", "-b:a", "128k",
                   "-avoid_negative_ts", "make_zero", seg_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and os.path.exists(seg_path):
                seg_files.append(seg_path)

        if not seg_files:
            return "[error] Segment cutting failed"

        # Write concat file
        with open(concat_file, "w") as f:
            for sf in seg_files:
                f.write("file '%s'\n" % sf)

        # Concatenate
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
               "-c:v", "libx264", "-crf", "23", "-preset", "fast",
               "-c:a", "aac", "-b:a", "128k", output_path]
        log.info("[video] auto_cut concat: %d segments" % len(seg_files))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        # Clean up temp files
        for sf in seg_files:
            try:
                os.remove(sf)
            except OSError:
                pass
        try:
            os.remove(concat_file)
        except OSError:
            pass

        if result.returncode != 0:
            return "[error] ffmpeg concat failed: %s" % result.stderr[-500:]

        # Calculate how much was removed
        orig_probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", input_path],
            capture_output=True, text=True, timeout=30)
        new_probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", output_path],
            capture_output=True, text=True, timeout=30)
        try:
            orig_dur = float(orig_probe.stdout.strip())
            new_dur = float(new_probe.stdout.strip())
            removed = orig_dur - new_dur
            msg = "Smart trim complete: %s\nOriginal %.1fs -> Trimmed %.1fs, removed %.1fs of pauses (%d silence gaps)" % (
                output_path, orig_dur, new_dur, removed, len(segments) - 1)
        except (ValueError, AttributeError):
            size_mb = os.path.getsize(output_path) / 1024 / 1024
            msg = "Smart trim complete: %s (%.1fMB), removed %d pause segments" % (output_path, size_mb, len(segments) - 1)

    else:
        # Normal trim mode
        if not args.get("start"):
            return "[error] start parameter is required in non-auto_cut mode"

        output_path = _video_output_path(ctx["workspace"])
        cmd = ["ffmpeg", "-y", "-ss", str(args["start"])]
        if args.get("end"):
            cmd += ["-to", str(args["end"])]
        cmd += ["-i", input_path, "-c:v", "copy", "-c:a", "copy",
                "-avoid_negative_ts", "make_zero", output_path]

        log.info("[video] trim: %s" % " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return "[error] ffmpeg trim failed: %s" % result.stderr[-500:]

        size_mb = os.path.getsize(output_path) / 1024 / 1024
        msg = "Trim complete: %s (%.1fMB)" % (output_path, size_mb)

    # Send (shared by both modes)
    if args.get("send_to"):
        send_path = _compress_if_needed(output_path)
        send_result = messaging.upload_and_send(args["send_to"], send_path, "", ctx["workspace"])
        if send_result.get("code") == 0:
            msg += ", sent successfully"
        else:
            msg += ", send failed: %s" % send_result.get("msg", "?")

    log.info("[video] %s" % msg)
    return msg


@tool("add_bgm", "Add background music to a video. Video stream is not re-encoded (-c:v copy), only audio tracks are mixed.",
      {"video_path": {"type": "string", "description": "Video file path (local or URL)"},
       "audio_path": {"type": "string", "description": "Audio file path (mp3/wav/aac, local or URL)"},
       "volume": {"type": "number", "description": "Background music volume ratio, default 0.3 (30%), to avoid overpowering the original audio"},
       "send_to": {"type": "string", "description": "Send the result to this recipient (optional)"}},
      ["video_path", "audio_path"])
def tool_add_bgm(args, ctx):
    video = _ensure_local(args["video_path"], ctx["workspace"], "bgm_video")
    audio = _ensure_local(args["audio_path"], ctx["workspace"], "bgm_audio")
    output_path = _video_output_path(ctx["workspace"])
    vol = args.get("volume", 0.3)

    # Copy video stream, only mix audio: original + background music (with volume adjustment)
    filter_complex = "[1:a]volume=%.2f[bgm];[0:a][bgm]amix=inputs=2:duration=first[a]" % vol
    cmd = ["ffmpeg", "-y", "-i", video, "-i", audio,
           "-filter_complex", filter_complex,
           "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
           output_path]

    log.info("[video] add_bgm: %s" % " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        return "[error] ffmpeg bgm mixing failed: %s" % result.stderr[-500:]

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    msg = "BGM added: %s (%.1fMB)" % (output_path, size_mb)

    if args.get("send_to"):
        send_result = messaging.upload_and_send(args["send_to"], output_path, "", ctx["workspace"])
        if send_result.get("code") == 0:
            msg += ", sent successfully"
        else:
            msg += ", send failed: %s" % send_result.get("msg", "?")

    log.info("[video] %s" % msg)
    return msg


@tool("generate_video", "Generate a video from text description (ZhipuAI CogVideoX). Async task, typically takes 2-5 minutes.",
      {"prompt": {"type": "string", "description": "Video content description (the more detailed, the better)"},
       "size": {"type": "string", "description": "Video resolution, default 1280x720. Options: 768x1344, 864x1152, etc."},
       "send_to": {"type": "string", "description": "Send the result to this recipient (optional)"}},
      ["prompt"])
def tool_generate_video(args, ctx):
    # Read video_api config
    video_cfg = _video_api_config
    api_key = video_cfg.get("api_key", "")
    if not api_key:
        return "[error] video_api.api_key not configured, please add video_api section in config.json"
    api_base = video_cfg.get("api_base", "https://open.bigmodel.cn/api/paas/v4")
    model = video_cfg.get("model", "cogvideox-flash")

    # 1. Submit video generation task
    body = json.dumps({
        "model": model,
        "prompt": args["prompt"],
        "size": args.get("size", "1280x720"),
    }).encode("utf-8")
    req = urllib.request.Request(
        "%s/videos/generations" % api_base, data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            task = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
            err_msg = err_body.get("error", {}).get("message", str(err_body)[:200])
        except Exception:
            err_msg = "HTTP %d" % e.code
        return "[error] Failed to submit video generation task: %s" % err_msg
    except Exception as e:
        return "[error] Failed to submit video generation task: %s" % e

    task_id = task.get("id", "")
    if not task_id:
        return "[error] No task_id returned: %s" % json.dumps(task, ensure_ascii=False)[:300]
    log.info("[video] generate task submitted: %s (model=%s)" % (task_id, model))

    # 2. Poll for result (interval 3s, max 300s)
    poll_url = "%s/async-result/%s" % (api_base, task_id)
    for i in range(100):  # 100 * 3s = 300s (video generation typically takes 2-5 minutes)
        time.sleep(3)
        try:
            poll_req = urllib.request.Request(poll_url, headers={
                "Authorization": "Bearer %s" % api_key,
            })
            with urllib.request.urlopen(poll_req, timeout=15) as resp:
                status = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # API returns HTTP 400 but body is still JSON (contains FAIL status and error info)
            try:
                status = json.loads(e.read())
            except Exception:
                log.warning("[video] poll HTTP %d (retry)" % e.code)
                continue
        except Exception as e:
            log.warning("[video] poll error (retry): %s" % e)
            continue

        task_status = status.get("task_status", "")
        if task_status == "SUCCESS":
            # 3. Download the generated video
            video_results = status.get("video_result", [])
            if not video_results:
                return "[error] Task succeeded but no video result: %s" % json.dumps(status, ensure_ascii=False)[:300]

            video_url = video_results[0].get("url", "")
            if not video_url:
                return "[error] Video result missing url"

            output_path = _video_output_path(ctx["workspace"])
            urllib.request.urlretrieve(video_url, output_path)
            size_mb = os.path.getsize(output_path) / 1024 / 1024
            msg = "Video generation complete: %s (%.1fMB, model=%s)" % (output_path, size_mb, model)

            if args.get("send_to"):
                send_result = messaging.upload_and_send(args["send_to"], output_path, "", ctx["workspace"])
                if send_result.get("code") == 0:
                    msg += ", sent successfully"
                else:
                    msg += ", send failed: %s" % send_result.get("msg", "?")

            log.info("[video] %s" % msg)
            return msg

        elif task_status == "FAIL":
            err = status.get("error", {})
            err_msg = err.get("message", json.dumps(status, ensure_ascii=False)[:300])
            return "[error] Video generation failed: %s" % err_msg

        # PROCESSING — continue waiting

    return "[error] Video generation timed out (300s), task_id=%s, you can query later using exec tool" % task_id


# Video API config injected by init_extra
_video_api_config = {}

def set_video_config(config):
    global _video_api_config
    _video_api_config = config
