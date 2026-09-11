"""
logger.py
---------
Plain ANSI color helpers for terminal output — no extra dependency needed on
Linux/macOS terminals. Color coding is by MEANING, not just prettiness:

  cyan    = routine status / heartbeat (safe to skim past)
  green   = success (connected, order placed, fill)
  yellow  = warning (cost-check widened spread, side skipped, etc.)
  red     = error / rejection / halt (needs your attention)
  magenta = risk/PnL summary line
  bold    = section headers / startup banner

If output is being redirected to a file (not a real terminal), colors are
automatically disabled so log files stay clean and greppable.
"""

import sys

_IS_TTY = sys.stdout.isatty()


class C:
    RESET = "\033[0m" if _IS_TTY else ""
    BOLD = "\033[1m" if _IS_TTY else ""
    CYAN = "\033[36m" if _IS_TTY else ""
    GREEN = "\033[32m" if _IS_TTY else ""
    YELLOW = "\033[33m" if _IS_TTY else ""
    RED = "\033[31m" if _IS_TTY else ""
    MAGENTA = "\033[35m" if _IS_TTY else ""
    GRAY = "\033[90m" if _IS_TTY else ""


def banner(msg: str):
    print(f"{C.BOLD}{C.CYAN}=== {msg} ==={C.RESET}")


def info(msg: str):
    print(f"{C.CYAN}[info]{C.RESET} {msg}")


def success(msg: str):
    print(f"{C.GREEN}[ok]{C.RESET}   {msg}")


def warn(msg: str):
    print(f"{C.YELLOW}[warn]{C.RESET} {msg}")


def error(msg: str):
    print(f"{C.RED}{C.BOLD}[ERROR]{C.RESET}{C.RED} {msg}{C.RESET}")


def trade(msg: str):
    print(f"{C.GREEN}[trade]{C.RESET} {msg}")


def risk(msg: str):
    print(f"{C.MAGENTA}[risk]{C.RESET} {msg}")


def status(msg: str):
    # dim/gray — the frequent heartbeat line, deliberately low-contrast
    print(f"{C.GRAY}{msg}{C.RESET}")


def halt(msg: str):
    print(f"\n{C.RED}{C.BOLD}*** HALT: {msg} ***{C.RESET}\n")
