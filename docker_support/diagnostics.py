"""Allowlisted diagnostic packets. Never dump environment, cookies or raw logs."""
from __future__ import annotations

import argparse
from collections import Counter
import io
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import subprocess
import tempfile
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docker_support.common import atomic_json, private_dir, read_json, utcnow

MAX_PACKET = 4 * 1024 * 1024
BOOL_KEYS = ("autoRecordingMode", "splitRecordingMode", "autoPostProcessing", "splitPostProcessing", "stream_copy", "moveAfterProcessingEnabled", "deleteAfterPostProcessing")
INT_KEYS = ("recheckInterval", "autoStopInterval", "splitOverlapSec", "timemachine_time_shift", "video_quality")
ENUM_KEYS = {"plugin_type": ("basic", "timemachine_plus"), "video_codec": ("libx264", "libx265", "h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv", "h264_vaapi"), "preset": ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"), "audio_codec": ("aac", "copy", "libopus")}
EVENT_CODES = {"application", "network", "authentication", "storage", "process", "traceback", "child_exit", "manual", "collector"}
EXCEPTION_TYPES = {"FileNotFoundError", "PermissionError", "ValueError", "KeyError", "TimeoutError", "SSLCertVerificationError", "ConnectionError", "RuntimeError", "OSError", "UnicodeDecodeError", "JSONDecodeError"}


def safe_settings(root):
    result = {"status": "unavailable"}
    try:
        cfg = read_json(Path(root) / "json/config.json")
        selected = {k: cfg[k] for k in BOOL_KEYS if type(cfg.get(k)) is bool}
        selected.update({k: cfg[k] for k in INT_KEYS if type(cfg.get(k)) is int and -1 <= cfg[k] <= 1000000})
        selected.update({k: cfg[k] for k, choices in ENUM_KEYS.items() if cfg.get(k) in choices})
        result = {"status": "ok", "values": selected}
    except Exception:
        pass
    return result


def resource_snapshot(pid):
    result = {"observed_at": utcnow(), "status": "unavailable"}
    if type(pid) is not int or pid <= 0:
        return result
    try:
        status = Path("/proc") / str(pid) / "status"
        entries = {}
        for line in status.read_text().splitlines():
            key, _, value = line.partition(":")
            if key in ("VmRSS", "VmSize", "Threads"):
                m = re.match(r"\s*(\d+)", value)
                if m:
                    entries[key] = int(m[1])
        entries["fd_count"] = len(list((status.parent / "fd").iterdir()))
        result.update(status="ok", process=entries)
    except Exception:
        pass
    return result


def tool_versions():
    result = {}
    for package in ("streamlink", "yt-dlp", "fastapi", "uvicorn", "httpx", "aiohttp"):
        try:
            value = importlib.metadata.version(package)
            if re.fullmatch(r"[A-Za-z0-9.+_-]{1,80}", value):
                result[package] = value
        except Exception:
            result[package] = "unavailable"
    try:
        p = subprocess.run(["/usr/bin/ffmpeg", "-version"], capture_output=True, timeout=1)
        match = re.match(rb"ffmpeg version ([A-Za-z0-9.+_~-]{1,80})", p.stdout[:256])
        result["ffmpeg"] = match[1].decode() if match else "unavailable"
    except Exception:
        result["ffmpeg"] = "unavailable"
    return result


def safe_events(events):
    out = []
    for event in events[-2048:]:
        if event.get("code") not in EVENT_CODES or event.get("level") not in ("ERROR", "WARNING", "INFO"):
            continue
        row = {"code": event["code"], "level": event["level"]}
        if event.get("exception_type") in EXCEPTION_TYPES:
            row["exception_type"] = event["exception_type"]
        if event.get("errno") in ("ENOSPC", "EACCES", "ENOENT", "EROFS", "EIO"):
            row["errno"] = event["errno"]
        if type(event.get("exit_code")) is int and -128 <= event["exit_code"] <= 255:
            row["exit_code"] = event["exit_code"]
        try:
            from datetime import datetime
            row["time"] = datetime.fromisoformat(event["time"]).isoformat()
        except Exception:
            continue
        out.append(row)
    return out


def snapshot(app_root, pid, trigger, events, stats):
    manifest = {"schema": 1, "support_version": "20261031.1", "collected_at": utcnow(), "trigger": trigger,
                "scope": "selected observations; not a replay or memory dump", "raw_logs_included": False}
    selected_events = safe_events(events)
    manifest["event_range"] = {"count": len(selected_events), "first": selected_events[0]["time"] if selected_events else None, "last": selected_events[-1]["time"] if selected_events else None}
    try:
        build = read_json(Path(app_root) / "docker-build-manifest.json")
        manifest["build"] = {k: build[k] for k in ("upstream_version", "upstream_archive_sha256", "public_patch") if k in build}
        manifest["source_files"] = {k: v for k, v in build.get("files", {}).items() if isinstance(k, str) and re.fullmatch(r"[0-9a-f]{64}", str(v))}
    except Exception:
        manifest["build_status"] = "unavailable"
    selected_stats = {k: max(0, int(stats.get(k, 0))) for k in ("file_dropped", "console_dropped", "file_failures", "console_failures", "capture_dropped", "capture_failures", "state_failures", "unclassified_lines", "lines_seen", "event_ring_dropped") if type(stats.get(k, 0)) is int}
    resources = resource_snapshot(pid)
    resources["storage"] = {}
    for role, path in (("recordings", "/recordings"), ("application", str(app_root))):
        try:
            fs = os.statvfs(path)
            resources["storage"][role] = {"free_bytes": fs.f_bavail * fs.f_frsize, "free_inodes": fs.f_favail, "read_only": bool(fs.f_flag & os.ST_RDONLY), "mount_point": os.path.ismount(path)}
        except Exception:
            resources["storage"][role] = {"status": "unavailable"}
    return {"manifest.json": manifest,
            "environment.json": {"python": platform.python_version(), "os": sys.platform, "architecture": platform.machine(), "tools": tool_versions()},
            "settings.json": safe_settings(app_root), "resources.json": resources,
            "events.json": selected_events, "collection.json": {"stats": selected_stats, "observed_at": utcnow(), "omitted": ["cookies", "accounts", "channel_ids", "filenames", "raw_logs", "command_lines", "environment", "memory", "browser_storage"], "unavailable": ["async_tasks", "browser_state", "runtime_oom_reason"]}}


def publish_packet(log_dir, documents, post_events=(), maximum=MAX_PACKET, retain=3):
    directory = private_dir(Path(log_dir) / "diagnostics")
    documents = dict(documents)
    documents["post-events.json"] = safe_events(list(post_events))
    encoded = {name: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() for name, value in documents.items()}
    if sum(map(len, encoded.values())) > maximum:
        raise ValueError("packet_input_budget_exceeded")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in encoded.items():
            entry = zipfile.ZipInfo(name)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o600 << 16
            archive.writestr(entry, raw)
    if len(content.getvalue()) > maximum:
        raise ValueError("packet_output_budget_exceeded")
    name = "diagnostic-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex + ".zip"
    fd, temporary = tempfile.mkstemp(prefix=".packet-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.getvalue())
        os.replace(temporary, directory / name)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    files = sorted((p for p in directory.glob("diagnostic-*.zip") if p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime)
    for old in files[:-retain]:
        old.unlink()
    return directory / name


def load_event_tail(log_dir):
    events = []
    for name in ("events.jsonl.2", "events.jsonl.1", "events.jsonl"):
        try:
            path = Path(log_dir) / name
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not __import__("stat").S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                for raw in stream.read(1024 * 1024).splitlines():
                    try:
                        events.append(json.loads(raw))
                    except Exception:
                        pass
        except Exception:
            pass
    return safe_events(events)


def cli():
    parser = argparse.ArgumentParser(description="Create a local, allowlisted support packet; no upload.")
    parser.add_argument("--log-dir", default=os.environ.get("RECORDWEB_LOG_DIR", "/var/log/recordweb"))
    parser.add_argument("--app-root", default="/app")
    parser.add_argument("--symptom", choices=("start", "recording", "postprocessing", "ui", "storage", "network", "other"), default="other")
    args = parser.parse_args()
    state = {}
    try:
        state = read_json(Path(args.log_dir) / "collector-state.json", 65536)
    except Exception:
        pass
    docs = snapshot(args.app_root, state.get("app_pid"), "manual", load_event_tail(args.log_dir), state.get("stats", {}))
    docs["manifest.json"]["reported_symptom"] = args.symptom
    path = publish_packet(args.log_dir, docs)
    print(json.dumps({"packet": str(path), "bytes": path.stat().st_size, "raw_logs_included": False}))


if __name__ == "__main__":
    cli()
