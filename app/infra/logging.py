"""
app/infra/logging.py
--------------------
Shared structured ANSI logger for every engine. Provides a unified, low-noise
detail level so event streams stay greppable while the monitor captures
structured heartbeats separately.
"""

from __future__ import annotations

import sys
import threading

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


def _write(prefix: str, msg: str):
    with _lock:
        sys.stdout.write(f"{prefix} {msg}\n")
        sys.stdout.flush()


def banner(msg: str):
    _write(f"{_C.BOLD}{_C.CYAN}=== {msg} ==={_C.RESET}", "")


def info(msg: str):
    _write(f"{_C.CYAN}[info]{_C.RESET}", msg)


def success(msg: str):
    _write(f"{_C.GREEN}[ok]{_C.RESET}", msg)


def warn(msg: str):
    _write(f"{_C.YELLOW}[warn]{_C.RESET}", msg)


def error(msg: str):
    _write(f"{_C.RED}{_C.BOLD}[ERROR]{_C.RESET}{_C.RED}", msg)


def trade(msg: str):
    _write(f"{_C.GREEN}[trade]{_C.RESET}", msg)


def risk(msg: str):
    _write(f"{_C.MAGENTA}[risk]{_C.RESET}", msg)


def status(msg: str):
    _write(f"{_C.GRAY}[status]{_C.RESET}", msg)


def halt(msg: str):
    _write("\n" + f"{_C.RED}{_C.BOLD}*** HALT ***{_C.RESET}", msg)