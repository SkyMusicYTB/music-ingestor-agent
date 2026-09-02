from __future__ import annotations

import base64
import ipaddress
import secrets
import select
import socket
import socketserver
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from types import TracebackType

from app.sources.policy import PublicNetworkPolicy, SourcePolicyViolation

Resolver = Callable[..., Sequence[tuple[object, ...]]]
Connector = Callable[[tuple[str, int], float], socket.socket]
_MAX_REQUEST_BYTES = 16 * 1024
_TUNNEL_CHUNK = 64 * 1024


def resolve_and_pin_public_host(
    hostname: str,
    port: int = 443,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> tuple[str, ...]:
    """Resolve once, reject mixed/private answers, and return the pinned public set."""

    normalized = hostname.rstrip(".").casefold()
    if not normalized or port != 443:
        raise SourcePolicyViolation("egress_target_invalid")
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SourcePolicyViolation("egress_host_non_ascii") from exc
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        raise SourcePolicyViolation("egress_literal_address_forbidden")
    host_check = PublicNetworkPolicy().validate_hostname(normalized)
    if not host_check.allowed:
        raise SourcePolicyViolation(host_check.reason_code)
    try:
        answers = resolver(normalized, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise SourcePolicyViolation("egress_resolution_failed") from exc
    addresses: set[str] = set()
    for answer in answers:
        if len(answer) < 5 or not answer[4]:
            continue
        sockaddr = answer[4]
        if not isinstance(sockaddr, tuple) or not sockaddr:
            continue
        addresses.add(str(sockaddr[0]).split("%", 1)[0])
    validation = PublicNetworkPolicy().validate_resolved_addresses(addresses)
    if not validation.allowed:
        raise SourcePolicyViolation(validation.reason_code)
    return tuple(sorted(addresses))


class _PinnedProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        auth_header: str,
        resolver: Resolver,
        connector: Connector,
    ) -> None:
        self.auth_header = auth_header
        self.resolver = resolver
        self.connector = connector
        self.stopping = threading.Event()
        self._active_lock = threading.Lock()
        self._active: set[socket.socket] = set()
        super().__init__(server_address, _ConnectHandler, bind_and_activate=True)

    def track(self, connection: socket.socket) -> None:
        with self._active_lock:
            self._active.add(connection)

    def untrack(self, connection: socket.socket) -> None:
        with self._active_lock:
            self._active.discard(connection)

    def close_active(self) -> None:
        self.stopping.set()
        with self._active_lock:
            active = tuple(self._active)
        for connection in active:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass


class _ConnectHandler(socketserver.BaseRequestHandler):
    server: _PinnedProxyServer

    def handle(self) -> None:
        upstream: socket.socket | None = None
        tunnel_established = False
        self.server.track(self.request)
        try:
            raw = _read_request(self.request)
            authority, headers = _parse_connect(raw)
            if headers.get("proxy-authorization") != self.server.auth_header:
                _respond(self.request, 407, "Proxy Authentication Required")
                return
            hostname, port = _parse_authority(authority)
            addresses = resolve_and_pin_public_host(
                hostname,
                port,
                resolver=self.server.resolver,
            )
            for address in addresses:
                try:
                    upstream = self.server.connector((address, port), 15.0)
                    break
                except OSError:
                    continue
            if upstream is None:
                _respond(self.request, 502, "Bad Gateway")
                return
            self.server.track(upstream)
            _respond(self.request, 200, "Connection Established")
            tunnel_established = True
            _tunnel(self.request, upstream, self.server.stopping)
        except (OSError, ValueError, SourcePolicyViolation):
            # Once CONNECT has succeeded, I/O errors are tunnel termination, not
            # a second HTTP response. This also keeps intentional proxy shutdown
            # from appending a spurious 400 response to an established stream.
            if not tunnel_established and not self.server.stopping.is_set():
                try:
                    _respond(self.request, 400, "Bad Request")
                except OSError:
                    pass
        finally:
            if upstream is not None:
                self.server.untrack(upstream)
                try:
                    upstream.close()
                except OSError:
                    pass
            self.server.untrack(self.request)


class PinnedEgressProxy(AbstractContextManager["PinnedEgressProxy"]):
    """Ephemeral authenticated CONNECT proxy that pins every public connection."""

    def __init__(
        self,
        *,
        resolver: Resolver = socket.getaddrinfo,
        connector: Connector = socket.create_connection,
    ) -> None:
        token = secrets.token_urlsafe(24)
        credentials = base64.b64encode(f"music-agent:{token}".encode()).decode("ascii")
        self._server = _PinnedProxyServer(
            ("127.0.0.1", 0),
            auth_header=f"Basic {credentials}",
            resolver=resolver,
            connector=connector,
        )
        self._token = token
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        port = int(self._server.server_address[1])
        return f"http://music-agent:{self._token}@127.0.0.1:{port}"

    def __enter__(self) -> PinnedEgressProxy:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="media-egress-guard",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.close_active()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _read_request(connection: socket.socket) -> bytes:
    payload = bytearray()
    while b"\r\n\r\n" not in payload:
        chunk = connection.recv(min(4096, _MAX_REQUEST_BYTES - len(payload)))
        if not chunk:
            raise ValueError("incomplete proxy request")
        payload.extend(chunk)
        if len(payload) >= _MAX_REQUEST_BYTES:
            raise ValueError("proxy request is too large")
    return bytes(payload)


def _parse_connect(payload: bytes) -> tuple[str, dict[str, str]]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("proxy request is not ASCII") from exc
    header_block = text.split("\r\n\r\n", 1)[0]
    lines = header_block.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        raise ValueError("only HTTP CONNECT is supported")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            raise ValueError("malformed proxy header")
        name, value = line.split(":", 1)
        normalized = name.strip().casefold()
        if normalized in headers:
            raise ValueError("duplicate proxy header")
        headers[normalized] = value.strip()
    return parts[1], headers


def _parse_authority(authority: str) -> tuple[str, int]:
    if "@" in authority or not authority or len(authority) > 300:
        raise ValueError("invalid CONNECT authority")
    if authority.startswith("["):
        raise ValueError("literal CONNECT addresses are forbidden")
    hostname, separator, raw_port = authority.rpartition(":")
    if not separator or not raw_port.isdigit() or int(raw_port) != 443:
        raise ValueError("CONNECT must target HTTPS port 443")
    if not hostname or hostname.endswith("."):
        raise ValueError("invalid CONNECT hostname")
    return hostname.casefold(), 443


def _respond(connection: socket.socket, status: int, reason: str) -> None:
    connection.sendall(f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\n\r\n".encode("ascii"))


def _tunnel(
    downstream: socket.socket,
    upstream: socket.socket,
    stopping: threading.Event,
) -> None:
    sockets = (downstream, upstream)
    while not stopping.is_set():
        readable, _writable, exceptional = select.select(sockets, (), sockets, 1.0)
        if exceptional:
            return
        for source in readable:
            chunk = source.recv(_TUNNEL_CHUNK)
            if not chunk:
                return
            destination = upstream if source is downstream else downstream
            destination.sendall(chunk)


__all__ = ["PinnedEgressProxy", "resolve_and_pin_public_host"]
