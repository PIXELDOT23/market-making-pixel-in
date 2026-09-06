"""
scripts/fyers_login.py
----------------------
FYERS v3 daily OAuth login helper (per https://myapi.fyers.in/docsv3).

Two-step flow:
  Step 1  GET  /api/v3/generate-authcode   -> browser login URL
  Step 2  POST /api/v3/validate-authcode   -> daily access_token

The default (no-arg) flow is fully automatic:
  1. loads + /profile-checks the cached token
  2. if token is missing/expired: starts a tiny listener on the redirect URI,
     opens the FYERS login page in your browser, and catches the auth_code from
     the redirect — no copy/paste needed
  3. exchanges the auth_code for the token, validates via /profile, and caches

The token is cached BOTH to `fyers_access_token.txt` (legacy compat) and to
Redis at `fyers:token:<app_id>` so `app/` engines read the same token from the
Redis TokenStore with a TTL mirroring the trading-day expiry.

Usage:
  python scripts/fyers_login.py                     # auto flow (browser + capture)
  python scripts/fyers_login.py --url               # print login URL only
  python scripts/fyers_login.py --check             # is the cached token live?
  python scripts/fyers_login.py --auth-code <code>  # log in with pasted code
  python scripts/fyers_login.py --print-token       # print cached raw token

Environment (all optional, defaults in app/config.py):
  FYERS_CLIENT_ID / FYERS_SECRET_KEY / FYERS_REDIRECT_URI / REDIS_URL
"""

from __future__ import annotations

import argparse
import asyncio
import os
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import fyers_apiv3 as _fyers_apiv3  # noqa: F401  (import order stable)
from fyers_apiv3 import fyersModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.config import settings
from app.infra.auth import AccessToken
from app.infra.redis import RedisBus

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(PROJECT_ROOT, "fyers_access_token.txt")
STATE = "pxl"


# ------------------------------------------------------------------ FYERS flows
def build_session():
    return fyersModel.SessionModel(
        client_id=settings.client_id,
        secret_key=settings.secret_key,
        redirect_uri=settings.redirect_uri,
        response_type="code",
        state=STATE,
        grant_type="authorization_code",
    )


def auth_url() -> str:
    return build_session().generate_authcode()


def extract_auth_code(pasted: str) -> str:
    pasted = pasted.strip()
    if "auth_code=" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        if "auth_code" in qs:
            return qs["auth_code"][0]
    return pasted


def exchange(code: str) -> dict:
    session = build_session()
    session.set_token(code)
    resp = session.generate_token()
    if resp.get("s") != "ok" or "access_token" not in resp:
        raise RuntimeError(f"token exchange failed: {resp}")
    return resp


def validate(token: str, client_id: str) -> bool:
    client = fyersModel.FyersModel(
        client_id=client_id, token=token, is_async=False, log_path=""
    )
    try:
        return client.get_profile().get("s") == "ok"
    except Exception:
        return False


# ------------------------------------------------------------------ caching
def save_token_file(token: str):
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
    os.chmod(TOKEN_FILE, 0o600)


async def save_token_redis(token: str, refresh_token: str | None):
    from app.infra.auth import TokenStore

    bus = RedisBus(url=settings.redis_url)
    await bus.connect()
    at = AccessToken(client_id=settings.client_id, token=token)
    if refresh_token:
        at.refresh_token = refresh_token
    await bus.set(TokenStore.TOKEN_KEY_FMT.format(settings.client_id), at, ttl_sec=24 * 3600)
    await bus.close()
    print(f"[ok] cached token to redis {settings.redis_url}")


def cached_token() -> str | None:
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE).read().strip() or None
    return None


# ------------------------------------------------------------------ browser capture
def capture_auth_code(host: str, port: int, timeout: float = 300.0) -> str:
    """
    Run a throwaway HTTP server on host:port and wait for the FYERS redirect
    (localhost:2000/callback?auth_code=...&state=...). Returns the auth_code.
    """
    found: queue.Queue = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                qs = parse_qs(urlparse(self.path).query)
                found.put((qs.get("auth_code") or [""])[0])
                body = "<h2 style='font-family:sans-serif'>Login complete</h2><p>You can close this tab and return to the terminal.</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode())
            except Exception:
                self.send_response(500)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[info] listening on http://{host}:{port}/callback for the FYERS redirect...")
    try:
        return found.get(timeout=timeout)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def run_auto_login() -> int:
    url = auth_url()
    print("\nFYERS v3 Login")
    print("  - no code to copy/paste: the login completes in the browser and")
    print(f"    this script picks up the auth_code automatically.\n")
    print(f"URL: {url}\n")
    try:
        webbrowser.open(url, new=1)
    except Exception:
        pass

    ru = urlparse(settings.redirect_uri)
    host, port = ru.hostname or "localhost", ru.port or (443 if ru.scheme == "https" else 80)
    if host not in ("localhost", "127.0.0.1", "0.0.0.0"):
        print(f"[warn] redirect host {host!r} is not loopback; browser capture needs a reachable listener")
    code = capture_auth_code(host, port)

    if not code:
        print("\n[warn] no redirect received — paste the auth_code from the URL manually.")
        code = extract_auth_code(input("auth_code> ").strip())
    if not code:
        print("[error] no auth_code obtained")
        return 1

    print("[info] exchanging auth_code for access_token...")
    resp = exchange(code)
    token = resp["access_token"]
    refresh = resp.get("refresh_token")

    if not validate(token, settings.client_id):
        print("[error] exchanged token failed /profile validation")
        return 1

    save_token_file(token)
    asyncio.run(save_token_redis(token, refresh))
    print(f"[ok] login complete — token cached (refresh_token={'yes' if refresh else 'no'})")
    return 0


# ------------------------------------------------------------------ entry
def main() -> int:
    ap = argparse.ArgumentParser(description="FYERS v3 daily OAuth login")
    ap.add_argument("--url", action="store_true", help="print login URL and exit")
    ap.add_argument("--check", action="store_true", help="validate cached token")
    ap.add_argument("--print-token", action="store_true", help="print cached token")
    ap.add_argument("--auth-code", help="auth_code (raw or full redirect URL) — manual flow")
    args = ap.parse_args()

    if args.check:
        cached = cached_token()
        if cached and validate(cached, settings.client_id):
            print("[ok] cached token is LIVE")
            return 0
        print("[warn] cached token invalid/expired — run login")
        return 1

    if args.print_token:
        print(cached_token() or "")
        return 0

    if args.url:
        print(auth_url())
        return 0

    if args.auth_code:
        resp = exchange(extract_auth_code(args.auth_code))
        token = resp["access_token"]
        refresh = resp.get("refresh_token")
        if not validate(token, settings.client_id):
            print("[error] exchanged token failed /profile validation")
            return 1
        save_token_file(token)
        asyncio.run(save_token_redis(token, refresh))
        print(f"[ok] login complete — token cached (refresh_token={'yes' if refresh else 'no'})")
        return 0

    # default: auto flow
    cached = cached_token()
    if cached and validate(cached, settings.client_id):
        print("[ok] cached token is LIVE — nothing to do")
        return 0
    print("[warn] cached token missing/expired — running the browser login flow...")
    return run_auto_login()


if __name__ == "__main__":
    raise SystemExit(main())