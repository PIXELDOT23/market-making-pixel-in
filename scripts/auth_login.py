"""
scripts/auth_login.py
---------------------
Interactive FYERS login that stores the access token in Redis (TokenStore),
so every engine and the whole project share one token via Redis with its
expiry window tracked.

Usage:
    python scripts/auth_login.py

Backward compatible: also refreshes the legacy fyers_access_token.txt.
"""

from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.infra.logging as log
from app.infra.redis import RedisBus
from app.infra.auth import TokenStore


async def main():
    bus = RedisBus()
    await bus.connect()
    store = TokenStore(bus)

    cached = await store.load()
    if cached is not None and cached.token:
        try:
            from fyers_apiv3 import fyersModel
            from app.config import settings
            probe = fyersModel.FyersModel(client_id=settings.client_id, token=cached.token, is_async=False, log_path="")
            if probe.get_profile().get("s") == "ok":
                ttl = await store.ttl_remaining()
                log.success(f"Valid token already in Redis for {ttl}s. Nothing to do.")
                await bus.close()
                return
        except Exception:
            pass

    at = await store._request_new_token_interactive()
    await store.save(at.token)

    # backward-compatible file mirror
    with open("fyers_access_token.txt", "w") as f:
        f.write(at.token)
    log.success("Access token stored in Redis + mirrored to fyers_access_token.txt")
    await bus.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.warn("Aborted.")