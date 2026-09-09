import json
import os
from datetime import datetime, timedelta

import lazop

from db import get_app_setting, set_app_setting


DARAZ_TOKEN_SETTING_KEY = "daraz_tokens"
DARAZ_API_URL = os.getenv("DARAZ_API_URL", "https://api.daraz.pk/rest").rstrip("/")


def _credentials():
    app_key = (os.getenv("DARAZ_APP_KEY") or "").strip()
    app_secret = (os.getenv("DARAZ_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        raise RuntimeError("Set DARAZ_APP_KEY and DARAZ_APP_SECRET before connecting Daraz.")
    return app_key, app_secret


def save_tokens(access_token, refresh_token, expires_in=604800):
    try:
        lifetime = max(int(expires_in or 604800), 60)
    except (TypeError, ValueError):
        lifetime = 604800
    data = {
        "access_token": str(access_token or "").strip(),
        "refresh_token": str(refresh_token or "").strip(),
        "expires_at": (datetime.now() + timedelta(seconds=lifetime)).isoformat(),
    }
    if not data["access_token"] or not data["refresh_token"]:
        raise RuntimeError("Daraz did not return both access and refresh tokens.")
    if not set_app_setting(DARAZ_TOKEN_SETTING_KEY, json.dumps(data)):
        raise RuntimeError("Could not persist Daraz tokens in the application database.")
    return data


def load_tokens():
    raw = (get_app_setting(DARAZ_TOKEN_SETTING_KEY, "") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if data.get("access_token") and data.get("refresh_token"):
                return data
        except (TypeError, ValueError):
            pass

    access_token = (os.getenv("DARAZ_ACCESS_TOKEN") or "").strip()
    refresh_token = (os.getenv("DARAZ_REFRESH_TOKEN") or "").strip()
    if not access_token:
        return None
    expires_at = (os.getenv("DARAZ_TOKEN_EXPIRES_AT") or "").strip()
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at or (datetime.now() + timedelta(days=7)).isoformat(),
    }


def is_expired(tokens):
    try:
        expires_at = datetime.fromisoformat(str(tokens.get("expires_at") or ""))
    except ValueError:
        return False
    return datetime.now() >= expires_at - timedelta(hours=1)


def refresh_access_token(refresh_token):
    if not refresh_token:
        raise RuntimeError("Daraz refresh token is missing. Re-authenticate Daraz.")
    app_key, app_secret = _credentials()
    client = lazop.LazopClient(DARAZ_API_URL, app_key, app_secret)
    api_request = lazop.LazopRequest("/auth/token/refresh")
    api_request.add_api_param("refresh_token", refresh_token)
    body = client.execute(api_request).body or {}
    if body.get("access_token"):
        save_tokens(
            body["access_token"],
            body.get("refresh_token") or refresh_token,
            body.get("expires_in") or 604800,
        )
        return body["access_token"]
    raise RuntimeError(f"Daraz token refresh failed: {body.get('message') or body.get('code') or 'unknown error'}")


def get_access_token():
    tokens = load_tokens()
    if not tokens:
        raise RuntimeError("Daraz is not authenticated. Use the Connect Daraz link in Sleek Space.")
    if is_expired(tokens):
        return refresh_access_token(tokens.get("refresh_token"))
    return tokens["access_token"]
