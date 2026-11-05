from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
REPORT_PATH = os.path.join(BASE_DIR, "json", "recent_errors.json")
MAX_ITEMS = 100

_last_print_at: dict[str, float] = {}
_printed_once_keys: set[str] = set()


class NotLiveError(Exception):
    pass


def getAppLogger(name: str | None = None) -> logging.Logger:
    if name:
        return logging.getLogger(f"recordFSM.{name}")

    return logging.getLogger("recordFSM")


def setupAppLogging(mode: str = "app") -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger("recordFSM")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = os.path.join(LOG_DIR, f"recordFSM_{mode}.log")

    existing_paths = {
        getattr(handler, "baseFilename", None)
        for handler in logger.handlers
    }

    if log_path not in existing_paths:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)

    def _excepthook(exc_type, exc, tb):
        logger.error("Unhandled exception", exc_info=(exc_type, exc, tb))

        try:
            recordException(
                f"unhandled:{mode}",
                exc,
                exc_info=(exc_type, exc, tb),
            )
        except Exception:
            pass

        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    return logger


def debugThrottle(
    key: str,
    msg: str,
    min_secs: float = 30.0,
    print_fn=print,
):

    now = time.monotonic()
    prev = _last_print_at.get(key, 0.0)

    if now - prev >= min_secs:
        print_fn(msg)
        _last_print_at[key] = now


def printOnce(
    key: str,
    msg: str | None = None,
    print_fn=print,
):

    if key in _printed_once_keys:
        return

    _printed_once_keys.add(key)

    if msg is not None:
        print_fn(msg)


def _load_items() -> list[dict]:
    try:
        with open(REPORT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return data

    except Exception:
        pass

    return []


def _save_items(items: list[dict]):
    try:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)

        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(
                items[-MAX_ITEMS:],
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception:
        pass


def recordMessage(
    source: str,
    level: str,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
):

    items = _load_items()

    items.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "level": level,
        "message": str(message),
        "extra": extra or {},
    })

    _save_items(items)


def recordException(
    source: str,
    exc: BaseException,
    extra: Optional[Dict[str, Any]] = None,
    exc_info=None,
):

    if exc_info:
        tb_text = "".join(traceback.format_exception(*exc_info))
    else:
        tb_text = traceback.format_exc()

    items = _load_items()

    items.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "level": "ERROR",
        "message": str(exc),
        "exception_type": exc.__class__.__name__,
        "traceback": tb_text,
        "extra": extra or {},
    })

    _save_items(items)


def loadRecentErrors() -> list[dict]:
    return _load_items()


__all__ = [
    "NotLiveError",
    "getAppLogger",
    "setupAppLogging",
    "debugThrottle",
    "printOnce",
    "recordMessage",
    "recordException",
    "loadRecentErrors",
]