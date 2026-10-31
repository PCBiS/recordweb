import atexit
import json
import os
import socket
import sys
import time
from datetime import datetime
from typing import Optional


class RuntimeAlreadyRunning(RuntimeError):
    pass


# 버전별 동시실행 방지
class RuntimeGuard:
    def __init__(self, mode: str, base_dir: Optional[str] = None):
        self.mode = mode
        self.base_dir = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.runtime_dir = os.path.join(self.base_dir, "json")
        self.lock_path = os.path.join(self.runtime_dir, "recordfsm_runtime.lock")
        self.info_path = os.path.join(self.runtime_dir, "recordfsm_runtime.json")
        self._file = None
        self._locked = False

    def acquire(self):
        os.makedirs(self.runtime_dir, exist_ok=True)
        self._file = open(self.lock_path, "a+", encoding="utf-8")

        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self._file.close()
            self._file = None
            raise RuntimeAlreadyRunning(self._build_running_message())

        self._locked = True
        self._write_info()
        atexit.register(self.release)
        return self

    def release(self):
        if not self._file:
            return

        try:
            if self._locked:
                if os.name == "nt":
                    import msvcrt
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

        try:
            self._file.close()
        except Exception:
            pass

        self._file = None
        self._locked = False

        try:
            if os.path.exists(self.info_path):
                os.remove(self.info_path)
        except Exception:
            pass

    def _write_info(self):
        info = {
            "mode": self.mode,
            "pid": os.getpid(),
            "python": sys.executable,
            "host": socket.gethostname(),
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "time": time.time(),
        }
        try:
            with open(self.info_path, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _build_running_message(self):
        info = None
        try:
            with open(self.info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception:
            info = None

        if info:
            return (
                "이미 recordFSM 계열 프로그램이 실행 중입니다.\n"
                f"실행 모드: {info.get('mode')}\n"
                f"PID: {info.get('pid')}\n"
                f"시작 시간: {info.get('started_at')}\n"
                "recordWEB / recordGUI / recordLITE는 동시에 실행하지 않는 것이 안전합니다."
            )

        return (
            "이미 recordFSM 계열 프로그램이 실행 중이거나, 이전 실행의 잠금이 남아 있습니다.\n"
            "실행 중인 recordWEB / recordGUI / recordLITE가 있는지 확인하세요."
        )
