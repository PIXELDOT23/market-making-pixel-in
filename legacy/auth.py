"""
auth.py
-------
Run this file FIRST, once per day (access tokens expire daily).
It walks you through the Fyers login handshake and caches the access token
to disk so bot.py can pick it up without you re-entering anything.

Usage:
    python auth.py
"""

import os
import webbrowser
from fyers_apiv3 import fyersModel
import config
import logger as log


def login_and_get_token() -> str:
    session = fyersModel.SessionModel(
        client_id=config.CLIENT_ID,
        secret_key=config.SECRET_KEY,
        redirect_uri=config.REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )

    auth_url = session.generate_authcode()
    log.info("Open this URL, log in with YOUR Fyers credentials, approve access:")
    print(auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    log.info(
        "After approving, your browser redirects to a URL containing "
        "'auth_code=XXXXXXXX' — copy just that value."
    )
    auth_code = input("Paste auth_code here: ").strip()

    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") != "ok":
        log.error(f"Token generation failed: {response}")
        raise RuntimeError(f"Token generation failed: {response}")

    access_token = response["access_token"]
    with open(config.TOKEN_FILE, "w") as f:
        f.write(access_token)

    log.success(f"Access token saved to {config.TOKEN_FILE}. You're connected.")
    return access_token


def load_cached_token() -> str | None:
    if os.path.exists(config.TOKEN_FILE):
        with open(config.TOKEN_FILE) as f:
            token = f.read().strip()
            if token:
                return token
    return None


def get_fyers_client():
    """Returns an authenticated fyersModel instance, prompting for login if needed."""
    token = load_cached_token()
    if token is None:
        token = login_and_get_token()

    fyers = fyersModel.FyersModel(
        client_id=config.CLIENT_ID, is_async=False, token=token, log_path=""
    )
    profile = fyers.get_profile()
    if profile.get("s") != "ok":
        # cached token is stale/expired -> force fresh login
        log.warn("Cached token invalid/expired, re-authenticating...")
        token = login_and_get_token()
        fyers = fyersModel.FyersModel(
            client_id=config.CLIENT_ID, is_async=False, token=token, log_path=""
        )
    return fyers


if __name__ == "__main__":
    login_and_get_token()