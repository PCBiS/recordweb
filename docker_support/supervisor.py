"""Keep the original command; copy FD output without blocking recording on disk."""
from __future__ import annotations

import codecs
from collections import Counter, deque
import json
import os
from pathlib import Path
import queue
import re
import selectors
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docker_support.common import RotatingJSON, atomic_json, number, utcnow
from docker_support.diagnostics import EXCEPTION_TYPES, publish_packet, snapshot


def classify(text):
    """Only known level markers, never infer severity from stderr alone."""
    if text.startswith("Traceback (most recent call last):"):
        return "ERROR", "traceback"
    if re.match(r"^(?:" + "|".join(sorted(EXCEPTION_TYPES)) + r"):\s", text):
        return "ERROR", "application"
    if re.search(r"\[(?:ERROR|CRITICAL|FATAL)\]", text, re.I) or re.match(r"^(?:ERROR|CRITICAL):\s", text, re.I):
        level = "ERROR"
    elif re.search(r"\[(?:WARN|WARNING)\]", text, re.I) or re.match(r"^WARNING:\s", text, re.I):
        level = "WARNING"
    else:
        return None
    lowered = text.lower()
    if level == "ERROR" and re.search(r"명령 생성 실패|녹화 시작 실패|녹화 프로세스 오류|remux/move 실패|후처리.*실패|failed to start|could not start", lowered):
        code = "process"
    elif any(s in lowered for s in ("enospc", "no space left", "permission denied", "disk", "디스크", "용량 부족")):
        code = "storage"
    elif any(s in lowered for s in ("cookie", "authorization", "인증", "쿠키", "권한")):
        code = "authentication"
    elif any(s in lowered for s in ("timeout", "connection", "segment", "network", "tls", "ssl", "연결", "네트워크")):
        code = "network"
    elif any(s in lowered for s in ("ffmpeg", "streamlink", "process", "프로세스", "명령")):
        code = "process"
    else:
        code = "application"
    return level, code


class TriggerPolicy:
    def __init__(self, cooldown=120, warning_count=3, window=60):
        self.cooldown, self.warning_count, self.window = cooldown, warning_count, window
        self.last = {}
        self.global_last = -float("inf")
        self.warnings = {}

    def observe(self, level, code, now):
        if level == "WARNING":
            history = self.warnings.setdefault(code, deque(maxlen=128))
            history.append(now)
            while history and history[0] < now - self.window:
                history.popleft()
            if len(history) < self.warning_count:
                return False
        # Recoverable network errors must repeat; a retry can succeed normally.
        if level == "ERROR" and code == "network":
            history = self.warnings.setdefault("error:network", deque(maxlen=64))
            history.append(now)
            while history and history[0] < now - self.window:
                history.popleft()
            if len(history) < 2:
                return False
        if now - self.last.get(code, -float("inf")) < self.cooldown or now - self.global_last < min(30, self.cooldown):
            return False
        self.last[code] = now
        self.global_last = now
        return True


class Runtime:
    def __init__(self, command, log_dir, app_root="/app"):
        self.command, self.log_dir, self.app_root = command, Path(log_dir), app_root
        self.stats = Counter()
        self.console_queue = queue.Queue(maxsize=128)  # <= 8 MiB raw data
        self.file_queue = queue.Queue(maxsize=256)  # bounded records, each <= 8K chars
        self.capture_queue = queue.Queue(maxsize=2)
        self.events = deque(maxlen=2048)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.capture_stop = threading.Event()
        self.proc = None
        self.signal_deadline = None
        self.requested_signal = None
        self.policy = TriggerPolicy(number("RECORDWEB_DIAG_COOLDOWN", 120, 1, 86400), number("RECORDWEB_DIAG_WARNING_COUNT", 3, 2, 100), number("RECORDWEB_DIAG_WARNING_WINDOW", 60, 1, 3600))
        self.post_seconds = number("RECORDWEB_DIAG_POST_SECONDS", 30, 0, 120)
        self.auto = os.environ.get("RECORDWEB_AUTO_DIAGNOSTICS", "1") != "0"
        self.pre_seconds = number("RECORDWEB_DIAG_PRE_SECONDS", 300, 1, 3600)

    def event(self, level, code, text=""):
        event = {"time": utcnow(), "level": level, "code": code}
        for kind in EXCEPTION_TYPES:
            if re.search(r"\b" + kind + r"\b", text):
                event["exception_type"] = kind
                break
        for errno in ("ENOSPC", "EACCES", "ENOENT", "EROFS", "EIO"):
            if re.search(r"\b" + errno + r"\b", text):
                event["errno"] = errno
                break
        match = re.search(r"(?:returncode|return code|exit(?: code)?|종료 코드)\s*[=:]?\s*(-?\d{1,3})\b", text, re.I)
        if match and -128 <= int(match[1]) <= 255:
            event["exit_code"] = int(match[1])
        with self.lock:
            if len(self.events) == self.events.maxlen:
                self.stats["event_ring_dropped"] += 1
            self.events.append((time.monotonic(), event))
        return event

    def recent(self):
        cutoff = time.monotonic() - self.pre_seconds
        with self.lock:
            return [dict(e) for at, e in self.events if at >= cutoff]

    def offer(self, target, item, dropped):
        try:
            target.put_nowait(item)
        except queue.Full:
            self.stats[dropped] += 1

    def capture(self, reason, force=False):
        if self.auto or force:
            self.offer(self.capture_queue, (reason, utcnow(), self.recent(), dict(self.stats)), "capture_dropped")

    def console_worker(self):
        while not self.stop.is_set() or not self.console_queue.empty():
            try:
                fd, raw = self.console_queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                view = memoryview(raw)
                while view:
                    view = view[os.write(fd, view):]
            except (OSError, ValueError):
                self.stats["console_failures"] += 1
            finally:
                self.console_queue.task_done()

    def file_worker(self):
        writer = RotatingJSON(self.log_dir / "console.jsonl", number("RECORDWEB_LOG_MAX_BYTES", 10 * 1024 * 1024, 65536, 100 * 1024 * 1024), number("RECORDWEB_LOG_BACKUPS", 5, 1, 10))
        events = RotatingJSON(self.log_dir / "events.jsonl", 512 * 1024, 2)
        retry_at = 0
        while not self.stop.is_set() or not self.file_queue.empty():
            try:
                record, event = self.file_queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if time.monotonic() < retry_at:
                    self.stats["file_dropped"] += 1
                    continue
                writer.write(record)
                if event:
                    events.write(event)
            except Exception:
                self.stats["file_failures"] += 1
                self.stats["file_dropped"] += 1
                self.offer(self.console_queue, (2, b"[RECORDWEB-SUPPORT] file capture unavailable; recording continues; retrying in 5s\n"), "console_dropped")
                writer.close()
                events.close()
                retry_at = time.monotonic() + 5
            finally:
                self.file_queue.task_done()
        writer.close()
        events.close()

    def capture_worker(self):
        pending = []
        last_state = 0
        while not self.capture_stop.is_set() or not self.capture_queue.empty() or pending:
            now = time.monotonic()
            if self.proc and now - last_state >= 2:
                try:
                    atomic_json(self.log_dir / "collector-state.json", {"schema": 1, "app_pid": self.proc.pid, "observed_at": utcnow(), "stats": dict(self.stats), "running": self.proc.poll() is None})
                except Exception:
                    self.stats["state_failures"] += 1
                last_state = now
            try:
                reason, observed, before, stats = self.capture_queue.get(timeout=.1)
            except queue.Empty:
                pass
            else:
                try:
                    docs = snapshot(self.app_root, self.proc.pid, reason, before, stats)
                    docs["manifest.json"]["triggered_at"] = observed
                    docs["manifest.json"]["requested_pre_seconds"] = self.pre_seconds
                    docs["manifest.json"]["requested_post_seconds"] = self.post_seconds
                    pending.append((now + self.post_seconds, docs))
                    # Persist the immediate snapshot too, for collector/container death.
                    atomic_json(self.log_dir / "pending-diagnostic.json", docs)
                except Exception:
                    self.stats["capture_failures"] += 1
                finally:
                    self.capture_queue.task_done()
            for deadline, docs in list(pending):
                if time.monotonic() >= deadline or self.capture_stop.is_set():
                    try:
                        docs["manifest.json"]["finalized_at"] = utcnow()
                        docs["manifest.json"]["post_window_complete"] = time.monotonic() >= deadline
                        publish_packet(self.log_dir, docs, [e for e in self.recent() if e['time'] >= docs['manifest.json']['triggered_at']])
                        (self.log_dir / "pending-diagnostic.json").unlink(missing_ok=True)
                    except Exception:
                        self.stats["capture_failures"] += 1
                    pending.remove((deadline, docs))

    def line(self, stream, text, partial=False):
        self.stats["lines_seen"] += 1
        found = classify(text)
        event = self.event(*found, text) if found else None
        if not found:
            self.stats["unclassified_lines"] += 1
        self.offer(self.file_queue, ({"time": utcnow(), "stream": stream, "message": text, "partial": partial}, event), "file_dropped")
        if found and self.policy.observe(*found, time.monotonic()):
            self.capture(found[1])

    def signal(self, signum, frame):
        self.requested_signal = signum
        if self.signal_deadline is None:
            self.signal_deadline = time.monotonic() + number("RECORDWEB_STOP_TIMEOUT", 45, 1, 300)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signum)
            except ProcessLookupError:
                pass

    def run(self):
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self.signal)
        # Catch launch failures as diagnostic events without printing argv/secrets.
        try:
            # Container init forwards signals to our process group. Give the app
            # its own session so forwarding here cannot deliver TERM twice.
            self.proc = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, start_new_session=True)
        except OSError:
            print("[RECORDWEB-SUPPORT] application launch failed", file=sys.stderr)
            if self.auto:
                try:
                    publish_packet(self.log_dir, snapshot(self.app_root, None, "child_exit", [self.event("ERROR", "process")], dict(self.stats)))
                except Exception:
                    pass
            return 127
        threads = [threading.Thread(target=f, daemon=True) for f in (self.console_worker, self.file_worker, self.capture_worker)]
        for thread in threads:
            thread.start()
        decoder = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in ("stdout", "stderr")}
        buffers = {"stdout": "", "stderr": ""}
        selector = selectors.DefaultSelector()
        for stream, name, fd in ((self.proc.stdout, "stdout", 1), (self.proc.stderr, "stderr", 2)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, (name, fd))
        exited_at = None
        last_partial = time.monotonic()
        try:
            while selector.get_map() or self.proc.poll() is None:
                now = time.monotonic()
                if self.signal_deadline and now > self.signal_deadline and self.proc.poll() is None:
                    self.proc.kill()
                if self.proc.poll() is not None:
                    if exited_at is None:
                        exited_at = now
                    if now - exited_at > 2:
                        break  # inherited child FD must not keep the container alive forever
                for key, _ in selector.select(.1):
                    name, fd = key.data
                    try:
                        raw = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not raw:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        buffers[name] += decoder[name].decode(b"", final=True)
                        continue
                    self.offer(self.console_queue, (fd, raw), "console_dropped")
                    buffers[name] += decoder[name].decode(raw)
                    while "\n" in buffers[name] or len(buffers[name]) >= 8192:
                        end = buffers[name].find("\n")
                        if end < 0 or end > 8192:
                            end = 8192
                            partial = True
                        else:
                            partial = False
                        self.line(name, buffers[name][:end], partial)
                        buffers[name] = buffers[name][end + (0 if partial else 1):]
                if now - last_partial > 1:
                    for name, text in buffers.items():
                        if text:
                            self.line(name, text, True)
                            buffers[name] = ""
                    last_partial = now
            for name, text in buffers.items():
                if text:
                    self.line(name, text, True)
            rc = self.proc.wait()
            if rc and self.requested_signal is None:
                self.event("ERROR", "child_exit", "exit code="+str(rc))
                self.capture("child_exit")
            return rc if rc >= 0 else 128 - rc
        finally:
            selector.close()
            self.stop.set()
            self.capture_stop.set()
            # Slow or failed sinks cannot hold recording/container shutdown open.
            deadline = time.monotonic() + 3
            for thread in threads:
                thread.join(max(0, deadline - time.monotonic()))


def main():
    os.umask(0o077)
    command = sys.argv[1:]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("application_command_required")
    if os.environ.get("RECORDWEB_CAPTURE_LOGS", "1") == "0":
        os.execvp(command[0], command)
    raise SystemExit(Runtime(command, os.environ.get("RECORDWEB_LOG_DIR", "/var/log/recordweb"), os.environ.get("RECORDWEB_APP_ROOT", "/app")).run())


if __name__ == "__main__":
    main()
