from __future__ import annotations

import json
import secrets

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.services.security import client_is_allowed


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
                if self.settings.https_enabled:
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
