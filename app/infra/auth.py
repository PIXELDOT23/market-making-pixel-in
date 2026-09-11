"""
app/infra/auth.py
-----------------
FYERS access-token lifecycle for the whole project.

Design (as requested):
  * The access token is stored in Redis (`fyers:token:<client_id>`), not in a
    plaintext file.
  * A TTL on the Redis key mirrors the token's expiry window. Every engine /
    module reads the SAME token from Redis, checks TTL/expiry before use, and
    triggers a single re-auth when it expires.
  * Re-auth is processed once (Redis SETNX distributed lock) so all engines
    stay coordinated even when running in separate processes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from fyers_apiv3 import fyersModel

from app.config import settings
from app.infra.redis import RedisBus


@dataclass
class AccessToken:
    client_id: str
    token: str
    issued_ts: float = field(default_factory=time.time)
    expires_ts: Optional[float] = None      # optional explicit expiry
    expires_in_sec: int = 0
    refresh_token: str = ""                 # if the exchange returned one (15-day validity)

    @property
    def expired(self) -> bool:
        if self.expires_ts:
            return time.time() >= self.expires_ts
        # FYERS access tokens are valid for the trading day; default TTL.
        return False


class TokenStore:
    TOKEN_KEY_FMT = "fyers:token:{}"
    REAUTH_KEY_FMT = "fyers:reauth:{}"
    DEFAULT_TTL_SEC = 86400

    def __init__(self, bus: RedisBus, ttl_sec: int = 0):
        self.bus = bus
        self.ttl_sec = ttl_sec or self.DEFAULT_TTL_SEC

    @property
    def client_id(self) -> str:
        return settings.client_id

    @property
    def key(self) -> str:
        return self.TOKEN_KEY_FMT.format(settings.client_id)

    async def save(self, token: str) -> AccessToken:
        at = AccessToken(client_id=settings.client_id, token=token)
        await self.bus.set(self.key, at, ttl_sec=self.ttl_sec)
        return at

    async def load(self) -> Optional[AccessToken]:
        at = await self.bus.get(self.key, AccessToken)
        if at is not None and at.expired:
            await self.bus.delete(self.key)
            return None
        return at

    async def ttl_remaining(self) -> int:
        return await self.bus.ttl(self.key)

    async def live_status(self) -> dict:
        """UI-facing FYERS login state: cached token + live broker /profile check."""
        cached = await self.load()
        if cached is None or not cached.token:
            return {
                "logged_in": False,
                "reason": "no token cached",
                "client_id": settings.client_id,
                "expires_in_sec": 0,
                "has_refresh": False,
            }
        ttl = await self.bus.ttl(self.key)
        profile_ok = False
        try:
            loop = asyncio.get_running_loop()

            def _check():
                client = fyersModel.FyersModel(
                    client_id=settings.client_id, token=cached.token,
                    is_async=False, log_path="",
                )
                return client.get_profile().get("s") == "ok"

            profile_ok = await asyncio.wait_for(
                loop.run_in_executor(None, _check), timeout=5
            )
        except Exception:
            profile_ok = False
        return {
            "logged_in": bool(profile_ok),
            "token_cached": bool(ttl > 0),
            "expires_in_sec": int(max(ttl, 0)),
            "client_id": settings.client_id,
            "has_refresh": bool(cached.refresh_token),
            "reason": "profile OK" if profile_ok else "token cached but /profile failed",
        }

    async def clear(self):
        await self.bus.delete(self.key)

    async def reconcile_from_file(self, path: str = "fyers_access_token.txt") -> Optional[str]:
        """
        FYERS invalidates earlier access tokens whenever a NEW one is issued, so
        the Redis cache can outlive a token that a second login already replaced
        (e.g. when a legacy helper wrote only `fyers_access_token.txt`).

        At boot: gather the Redis + file candidates, validate each against
        /profile, and re-sync whichever is still live into BOTH stores. Returns
        the live token, or None when none of the candidates works.
        """
        import os

        # never reconcile under the stubbed smoke identity
        if str(settings.client_id).lower().startswith("smoke"):
            return None

        async def _live(token: str) -> bool:
            loop = asyncio.get_running_loop()

            def _check():
                client = fyersModel.FyersModel(
                    client_id=settings.client_id, token=token,
                    is_async=False, log_path="",
                )
                return client.get_profile().get("s") == "ok"

            try:
                return bool(await asyncio.wait_for(
                    loop.run_in_executor(None, _check), timeout=5
                ))
            except Exception:
                return False

        stored = await self.load()
        file_tok: Optional[str] = None
        if os.path.exists(path):
            with open(path) as f:
                file_tok = f.read().strip() or None

        candidates: list = []
        if stored is not None and stored.token:
            candidates.append(stored.token)
        if file_tok and file_tok not in candidates:
            candidates.append(file_tok)
        if not candidates:
            return None

        for tok in candidates:
            if not await _live(tok):
                continue
            # re-sync the live token into both stores
            if stored is None or stored.token != tok:
                await self.save(tok)
            if file_tok != tok:
                with open(path, "w") as f:
                    f.write(tok)
                os.chmod(path, 0o600)
            return tok
        return None

    # ------------------------------------------------------------------ coordinated re-auth
    @staticmethod
    def _require_credentials():
        """Fail fast with a clear message when broker credentials are missing.

        Only reached when the app actually needs to generate a fresh FYERS token —
        reads of a cached/valid token and boot never hit this, and the stub smoke
        identity (FYERS_CLIENT_ID=smoketest) is exempt so the smoke test can boot
        against a DB without a real broker secret."""
        if str(settings.client_id).lower().startswith("smoke"):
            return
        missing = []
        if not settings.client_id:
            missing.append("FYERS_CLIENT_ID")
        if not settings.secret_key:
            missing.append("FYERS_SECRET_KEY")
        if missing:
            raise RuntimeError(
                "Cannot authenticate to FYERS: missing environment variable(s) "
                + ", ".join(missing)
                + ". Set them before running (see .env.example), then seed the "
                "access token with: python scripts/fyers_login.py"
            )

    def _request_new_token_interactive(self) -> AccessToken:
        """
        Runs the FYERS OAuth2 handshake (browser login). Returns the fresh token.
        Falls back to the cached token file if present for backward compat.

        Engines never block here silently: interactive login requires a human.
        Use `python scripts/fyers_login.py` (auto browser flow) to seed the token,
        or set FYERS_AUTO_LOGIN=1 to allow an inline browser handshake.
        """
        import os

        self._require_credentials()

        if os.getenv("FYERS_AUTO_LOGIN", "0") != "1":
            raise RuntimeError(
                "no valid FYERS token in Redis — run: "
                "python scripts/fyers_login.py"
            )

        import webbrowser

        session = fyersModel.SessionModel(
            client_id=settings.client_id,
            secret_key=settings.secret_key,
            redirect_uri=settings.redirect_uri,
            response_type="code",
            grant_type="authorization_code",
        )
        auth_url = session.generate_authcode()
        print("\n" + "=" * 70)
        print("FYERS auth required — opening browser.")
        print(f"URL: {auth_url}")
        print("=" * 70)
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass
        auth_code = input("Paste the redirected URL or auth_code: ").strip()
        session.set_token(auth_code)
        response = session.generate_token()
        if response.get("s") != "ok":
            raise RuntimeError(f"FYERS token generation failed: {response}")
        token = response.get("access_token")
        return AccessToken(client_id=settings.client_id, token=token)

    async def get_token(self) -> AccessToken:
        """Return a valid token, triggering (once) a re-auth if expired/missing."""
        cached = await self.load()
        if cached is not None:
            if cached.token:
                return cached
            # validate token live on first use
            try:
                client = fyersModel.FyersModel(
                    client_id=settings.client_id, token=cached.token,
                    is_async=False, log_path="",
                )
                if client.get_profile().get("s") == "ok":
                    return cached
            except Exception:
                pass
            await self.bus.delete(self.key)

        # distributed lock so only one process performs the login
        lock_key = self.REAUTH_KEY_FMT.format(settings.client_id)
        lock_acquired = bool(await self.bus._client.set(lock_key, "1", nx=True, ex=300))
        try:
            if not lock_acquired:
                # another process is re-authenticating; wait for it
                for _ in range(60):
                    await asyncio.sleep(1)
                    cached = await self.load()
                    if cached is not None:
                        return cached
                raise RuntimeError("Timeout waiting for concurrent re-auth")
            at = await self._request_new_token_interactive()
            await self.save(at.token)
            return at
        finally:
            if lock_acquired:
                await self.bus.delete(lock_key)

    async def ensure_valid(self) -> str:
        """Convenience: returns the raw token string, re-authing if needed."""
        at = await self.get_token()
        return at.token


def get_fyers_client(token: Optional[str] = None, is_async: bool = False):
    """Return an authenticated fyersModel instance on a ready token."""
    return fyersModel.FyersModel(
        client_id=settings.client_id,
        token=token or settings.client_id,
        is_async=is_async,
        log_path="",
    )