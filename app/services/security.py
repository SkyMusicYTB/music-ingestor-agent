from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

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


def normalize_host_header(value: str) -> str:
    """Return a canonical HTTP Host value or reject ambiguous syntax."""

    raw = value.strip()
    if (
        not raw
        or len(raw) > 300
        or any(character.isspace() or ord(character) < 32 for character in raw)
        or any(character in raw for character in "/?#@,")
    ):
        raise ValueError("invalid host header")
    parsed = urlsplit(f"//{raw}")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid host header")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid host port") from error
    if port is None and parsed.netloc.endswith(":"):
        raise ValueError("invalid empty host port")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError as address_error:
        try:
            normalized_host = host.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as unicode_error:
            raise ValueError("invalid internationalized host") from unicode_error
        labels = normalized_host.split(".")
        if (
            len(normalized_host) > 253
            or not normalized_host
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or not all(
                    character.isascii() and (character.isalnum() or character == "-")
                    for character in label
                )
                for label in labels
            )
        ):
            raise ValueError("invalid host header") from address_error
    else:
        normalized_host = f"[{address}]" if address.version == 6 else str(address)
    return normalized_host if port is None else f"{normalized_host}:{port}"


def normalize_origin(value: str, *, allow_path: bool) -> str:
    """Normalize an Origin or Referer to a comparable scheme/host origin."""

    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid request origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid request origin")
    if not allow_path and (parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("invalid request origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid request origin port") from error
    host = normalize_host_header(
        f"[{parsed.hostname}]" if ":" in parsed.hostname and port is None else parsed.netloc
    )
    if port == (443 if scheme == "https" else 80):
        host = host.rsplit(":", 1)[0]
        if ":" in parsed.hostname:
            host = f"[{ipaddress.IPv6Address(parsed.hostname)}]"
    return urlunsplit((scheme, host, "", "", ""))


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
    fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site == "cross-site":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "cross-site mutation rejected")
    if settings.origin_policy == "private_network":
        return

    expected_host = request.headers.get("host", "")
    try:
        effective_origin = normalize_origin(
            f"{request.url.scheme}://{normalize_host_header(expected_host)}", allow_path=False
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid request host") from error
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source.strip().lower() == "null":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing request origin")
    try:
        supplied_origin = normalize_origin(source, allow_path=request.headers.get("origin") is None)
    except ValueError as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid request origin") from error
    allowed = {effective_origin, *settings.allowed_origin_values}
    if supplied_origin not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "request origin mismatch")


def safe_event_text(value: object, limit: int = 500) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")[:limit]
