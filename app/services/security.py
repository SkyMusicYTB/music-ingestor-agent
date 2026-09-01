from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from app.config import Settings

SESSION_COOKIE = "music_agent_session"
CSRF_COOKIE = "music_agent_csrf"
PREAUTH_COOKIE = "music_agent_preauth"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hmac_keyed(secret: str, *parts: str) -> str:
    payload = "\x00".join(parts).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def client_ip(request: Request) -> str:
    if request.client is None:
        return "0.0.0.0"  # noqa: S104
    try:
        return str(ipaddress.ip_address(request.client.host))
    except ValueError:
        return "0.0.0.0"  # noqa: S104


def client_is_allowed(address: str, settings: Settings) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in settings.allowed_networks)


@dataclass(frozen=True)
class PreAuthToken:
    raw: str
    expires_at: int


def issue_preauth_token(settings: Settings, purpose: str, address: str) -> PreAuthToken:
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(32)
    body = f"{issued_at}.{nonce}"
    signature = hmac_keyed(settings.auth_hmac_key.get_secret_value(), purpose, address, body)
    return PreAuthToken(raw=f"{body}.{signature}", expires_at=issued_at + 1200)


def validate_preauth_token(
    settings: Settings, purpose: str, address: str, cookie_value: str, form_value: str
) -> bool:
    if not cookie_value or not hmac.compare_digest(cookie_value, form_value):
        return False
    pieces = form_value.split(".")
    if len(pieces) != 3:
        return False
    issued_raw, nonce, supplied = pieces
    try:
        issued_at = int(issued_raw)
    except ValueError:
        return False
    now = int(time.time())
    if issued_at > now + 30 or now - issued_at > 1200 or len(nonce) > 100:
        return False
    body = f"{issued_raw}.{nonce}"
    expected = hmac_keyed(settings.auth_hmac_key.get_secret_value(), purpose, address, body)
    return hmac.compare_digest(expected, supplied)


def validate_mutation_headers(request: Request, settings: Settings) -> None:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site not in (None, "same-origin", "none"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site mutation rejected")
    expected_host = request.headers.get("host", "").lower()
    if not expected_host:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing host")
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing request origin")
    parsed = urlsplit(source)
    if parsed.scheme not in ({"https"} if settings.https_enabled else {"http", "https"}):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid request origin")
    if (parsed.netloc or "").lower() != expected_host:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "request origin mismatch")


def safe_event_text(value: object, limit: int = 500) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:limit]
