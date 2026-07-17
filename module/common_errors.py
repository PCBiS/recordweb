import time

class NotLiveError(Exception):
    """방송이 OPEN/라이브 상태가 아님을 알리기 위한 예외 (FSM oneShot 전용)"""
    pass

__all__ = ["NotLiveError", "debugThrottle", "printOnce"]

# 같은 key 메시지는 min_secs 간격으로만 출력
_last_print_at = {}

# 프로세스 생애 동안 한 번만 출력
_printed_once_keys = set()

def debugThrottle(key: str, msg: str, min_secs: float = 30.0, print_fn=print):
    now = time.monotonic()
    prev = _last_print_at.get(key, 0.0)
    if now - prev >= min_secs:
        print_fn(msg)
        _last_print_at[key] = now


def printOnce(key: str, msg: str | None = None, print_fn=print):
    if key in _printed_once_keys:
        return
    _printed_once_keys.add(key)
    if msg is not None:
        print_fn(msg)
