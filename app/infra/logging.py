"""
app/infra/logging.py
--------------------
Shared structured ANSI logger for every engine. Each engine gets a quiet,
event-stream style console output while a optional rotating (plain or JSON)
file sink keeps a full-fidelity record for post-mortems.

Volume controls (all env):
  LOG_LEVEL  = DEBUG | INFO | WARN | ERROR   (status lines are DEBUG)
  LOG_QUIET  = 1 suppresses [info]/[ok]/[status]; trades/risk/warn/error stay
  LOG_FILE   = path for a rotating plain-text log (50MB x 5)
  LOG_FILE_JSON = 1 writes newline-delimited JSON lines to LOG_FILE instead

Trade, risk and halt lines are money events and are never quiet-suppressed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

_IS_TTY = sys.stdout.isatty()
_lock = threading.Lock()


class _C:
    RESET = "\033[0m" if _IS_TTY else ""
    BOLD = "\033[1m" if _IS_TTY else ""
    CYAN = "\033[36m" if _IS_TTY else ""
    GREEN = "\033[32m" if _IS_TTY else ""
    YELLOW = "\033[33m" if _IS_TTY else ""
    RED = "\033[31m" if _IS_TTY else ""
    MAGENTA = "\033[35m" if _IS_TTY else ""
    GRAY = "\033[90m" if _IS_TTY else ""


# ---- level gating ----------------------------------------------------------
_LEVELS = {"DEBUG": 0, "INFO": 10, "WARN": 20, "ERROR": 30}
_MIN = _LEVELS.get(os.getenv("LOG_LEVEL", "INFO").upper() or "INFO", 10)
_QUIET = os.getenv("LOG_QUIET", "0").lower() in ("1", "true", "yes")
_ALWAYS = (  # ranks that stay visible even under LOG_QUIET (money events)
    "trade", "risk", "warn", "error", "halt", "banner",
)


def _visible(rank: str, quiet_suppressible: bool = False) -> bool:
    if _LEVELS.get(rank, 10) < _MIN:
        return False
    if _QUIET and quiet_suppressible and rank not in _ALWAYS:
        return False
    return True


def _write(prefix: str, msg: str):
    with _lock:
        sys.stdout.write(f"{prefix} {msg}\n")
        sys.stdout.flush()


# ---- optional rotating file sink ------------------------------------------
_LOG_FILE = os.getenv("LOG_FILE", "")
_LOG_FILE_JSON = os.getenv("LOG_FILE_JSON", "0").lower() in ("1", "true", "yes")
_MAX_BYTES = 50 * 1024 * 1024
_BACKUPS = 5
_fh = None
_fh_path = ""
_fh_seq = 0


def _file_emit(level: str, prefix: str, msg: str):
    global _fh, _fh_path, _fh_seq
    if not _LOG_FILE:
        return
    if _fh is None:
        try:
            _fh = open(_LOG_FILE, "a", encoding="utf-8")
            _fh_path = _LOG_FILE
        except OSError:
            return
    if _LOG_FILE_JSON:
        line = json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": level, "prefix": prefix.strip(), "msg": msg,
        }, ensure_ascii=False) + "\n"
    else:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {prefix} {msg}\n"
    try:
        with _lock:
            _fh.write(line)
            _fh.flush()
            if _fh.tell() >= _MAX_BYTES:
                _fh.close()
                _fh = None
                _fh_seq += 1
                if _fh_seq > _BACKUPS:
                    try:
                        os.remove(f"{_LOG_FILE}.{_fh_seq - _BACKUPS}")
                    except OSError:
                        pass
                try:
                    os.replace(_LOG_FILE, f"{_LOG_FILE}.{_fh_seq}")
                except OSError:
                    pass
    except Exception:
        pass


def _emit(level: str, prefix: str, msg: str, quiet_suppressible: bool):
    if level == "halt":
        prefix = "\n" + prefix
    if not _visible(level, quiet_suppressible):
        return
    _write(prefix, msg)
    _file_emit(level, prefix.lstrip("\n"), msg)


def banner(msg: str):
    _emit("banner", f"{_C.BOLD}{_C.CYAN}=== {msg} ==={_C.RESET}", "", False)


def debug(msg: str):
    _emit("debug", f"{_C.GRAY}[debug]{_C.RESET}", msg, False)


def info(msg: str):
    _emit("info", f"{_C.CYAN}[info]{_C.RESET}", msg, True)


def success(msg: str):
    _emit("info", f"{_C.GREEN}[ok]{_C.RESET}", msg, True)


def warn(msg: str):
    _emit("warn", f"{_C.YELLOW}[warn]{_C.RESET}", msg, False)


def error(msg: str):
    _emit("error", f"{_C.RED}{_C.BOLD}[ERROR]{_C.RESET}{_C.RED}", msg, False)


def trade(msg: str):
    _emit("trade", f"{_C.GREEN}[trade]{_C.RESET}", msg, False)


def risk(msg: str):
    _emit("risk", f"{_C.MAGENTA}[risk]{_C.RESET}", msg, False)


def status(msg: str):
    _emit("debug", f"{_C.GRAY}[status]{_C.RESET}", msg, True)


def halt(msg: str):
    _emit("halt", f"{_C.RED}{_C.BOLD}*** HALT ***{_C.RESET}", msg, False)