from __future__ import annotations

import ipaddress
import json
import secrets
from typing import ClassVar
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.services.security import client_is_allowed, normalize_host_header


async def _response(send: Send, status: int, message: str) -> None:
    body = json.dumps({"detail": message}, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AllowedClientMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            client = scope.get("client")
            address = str(client[0]) if client else "0.0.0.0"  # noqa: S104
            if not client_is_allowed(address, self.settings):
                if scope["type"] == "http":
                    await _response(send, 403, "client network is not allowed")
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


class TrustedProxyMiddleware:
    """Apply forwarding metadata only when the immediate TCP peer is trusted."""

    _FORWARDED_HEADERS: ClassVar[set[bytes]] = {
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
    }

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        try:
            immediate = ipaddress.ip_address(str(client[0])) if client else None
        except ValueError:
            immediate = None
        trusted = immediate is not None and any(
            immediate in network for network in self.settings.trusted_proxy_networks
        )
        headers = list(scope.get("headers", []))
        scope["headers"] = [
            (name, value) for name, value in headers if name.lower() not in self._FORWARDED_HEADERS
        ]
        if not trusted:
            await self.app(scope, receive, send)
            return

        assert immediate is not None
        try:
            forwarded = self._collect_forwarded(headers)
            self._apply(scope, forwarded, immediate)
        except ValueError as error:
            if scope["type"] == "http":
                await _response(send, 400, str(error))
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)

    @classmethod
    def _collect_forwarded(cls, headers: list[tuple[bytes, bytes]]) -> dict[bytes, str]:
        collected: dict[bytes, str] = {}
        for raw_name, raw_value in headers:
            name = raw_name.lower()
            if name not in cls._FORWARDED_HEADERS or name == b"forwarded":
                continue
            if name in collected:
                raise ValueError("multiple forwarded header fields are not allowed")
            try:
                value = raw_value.decode("ascii").strip()
            except UnicodeDecodeError as error:
                raise ValueError("forwarded headers must use ASCII") from error
            if not value:
                raise ValueError("forwarded header value is empty")
            if len(value) > 4_096:
                raise ValueError("forwarded header value is too long")
            collected[name] = value
        return collected

    def _apply(
        self,
        scope: Scope,
        forwarded: dict[bytes, str],
        immediate: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> None:
        forwarded_for = forwarded.get(b"x-forwarded-for")
        if forwarded_for:
            raw_addresses = forwarded_for.split(",")
            if len(raw_addresses) > 32:
                raise ValueError("X-Forwarded-For contains too many addresses")
            if not raw_addresses or any(not value.strip() for value in raw_addresses):
                raise ValueError("X-Forwarded-For contains an empty address")
            try:
                addresses = [ipaddress.ip_address(value.strip()) for value in raw_addresses]
            except ValueError as error:
                raise ValueError("X-Forwarded-For contains an invalid address") from error
            selected = addresses[0]
            for candidate in reversed([*addresses, immediate]):
                if any(candidate in network for network in self.settings.trusted_proxy_networks):
                    continue
                selected = candidate
                break
            scope["client"] = (str(selected), 0)

        public = urlsplit(self.settings.public_base_url or "")
        forwarded_proto = forwarded.get(b"x-forwarded-proto")
        if forwarded_proto is not None:
            if "," in forwarded_proto or forwarded_proto.lower() not in {"http", "https"}:
                raise ValueError("X-Forwarded-Proto must be one http or https value")
            scope["scheme"] = forwarded_proto.lower()
        elif public.scheme:
            scope["scheme"] = public.scheme

        forwarded_host = forwarded.get(b"x-forwarded-host")
        if forwarded_host is not None:
            if "," in forwarded_host:
                raise ValueError("X-Forwarded-Host must contain one host")
            host = normalize_host_header(forwarded_host)
        elif public.netloc:
            host = normalize_host_header(public.netloc)
        else:
            host = ""
        if host:
            scope["headers"] = [
                (name, value) for name, value in scope["headers"] if name.lower() != b"host"
            ]
            scope["headers"].append((b"host", host.encode("ascii")))


class TrustedHostMiddleware:
    """Validate one canonical Host value, including bracketed IPv6 addresses."""

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        self.app = app
        self.allowed_hosts = tuple(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        host_headers = [
            value for name, value in scope.get("headers", []) if name.lower() == b"host"
        ]
        try:
            if len(host_headers) != 1:
                raise ValueError("exactly one Host header is required")
            normalized = normalize_host_header(host_headers[0].decode("ascii"))
            hostname = _hostname_without_port(normalized)
        except (UnicodeDecodeError, ValueError):
            if scope["type"] == "http":
                await _response(send, 400, "invalid host header")
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        valid = any(
            hostname == pattern or (pattern.startswith("*.") and hostname.endswith(pattern[1:]))
            for pattern in self.allowed_hosts
        )
        if not valid:
            if scope["type"] == "http":
                await _response(send, 400, "invalid host header")
            else:
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


def _hostname_without_port(host: str) -> str:
    if host.startswith("["):
        closing = host.find("]")
        if closing < 0:
            raise ValueError("invalid bracketed host")
        return host[: closing + 1]
    return host.rsplit(":", 1)[0] if ":" in host else host


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = 1_048_576) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    await _response(send, 413, "request body is too large")
                    return
            except ValueError:
                await _response(send, 400, "invalid Content-Length")
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_id = secrets.token_hex(12)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                content_type = next(
                    (value.lower() for name, value in headers if name.lower() == b"content-type"),
                    b"",
                )
                headers.extend(
                    [
                        (
                            b"content-security-policy",
                            b"default-src 'self'; img-src 'self' data:; style-src 'self'; "
                            b"script-src 'self'; connect-src 'self'; object-src 'none'; "
                            b"base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
                        ),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"permissions-policy",
                            b"camera=(), microphone=(), geolocation=(), payment=()",
                        ),
                        (b"cross-origin-resource-policy", b"same-origin"),
                        (b"x-request-id", request_id.encode()),
                    ]
                )
                if content_type.startswith(b"text/html"):
                    headers = [
                        (name, value) for name, value in headers if name.lower() != b"cache-control"
                    ]
                    headers.append((b"cache-control", b"private, no-store"))
                if scope.get("scheme") == "https":
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
