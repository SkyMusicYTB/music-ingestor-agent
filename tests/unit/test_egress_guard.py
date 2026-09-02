from __future__ import annotations

import base64
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

import pytest

from app.sources.egress import PinnedEgressProxy, resolve_and_pin_public_host
from app.sources.policy import SourcePolicyViolation


def _dns_answers(*addresses: str) -> Sequence[tuple[object, ...]]:
    answers: list[tuple[object, ...]] = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr: tuple[object, ...]
        if family == socket.AF_INET6:
            sockaddr = (address, 443, 0, 0)
        else:
            sockaddr = (address, 443)
        answers.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
    return answers


def _resolver_for(
    *addresses: str,
) -> Callable[..., Sequence[tuple[object, ...]]]:
    answers = _dns_answers(*addresses)

    def resolver(
        _hostname: str,
        _port: int,
        *,
        type: socket.SocketKind,
    ) -> Sequence[tuple[object, ...]]:
        assert type == socket.SOCK_STREAM
        return answers

    return resolver


def _read_headers(connection: socket.socket) -> bytes:
    payload = bytearray()
    while b"\r\n\r\n" not in payload:
        chunk = connection.recv(4096)
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)


def _proxy_endpoint(proxy: PinnedEgressProxy) -> tuple[str, int, str]:
    parsed = urlsplit(proxy.url)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    assert parsed.username is not None and parsed.password is not None
    credentials = base64.b64encode(f"{parsed.username}:{parsed.password}".encode("ascii")).decode(
        "ascii"
    )
    return parsed.hostname, parsed.port, f"Basic {credentials}"


@pytest.mark.parametrize(
    "answers",
    [
        ("93.184.216.34", "127.0.0.1"),
        ("93.184.216.34", "10.0.0.8"),
        ("2606:4700:4700::1111", "fd7a:115c:a1e0::1"),
        ("169.254.169.254",),
        ("224.0.0.1",),
        ("ff02::1",),
        ("fec0::1",),
        ("feff::1",),
        ("192.0.2.1",),
        ("0.0.0.0",),  # noqa: S104 - inert egress-policy test vector
    ],
)
def test_resolution_rejects_private_or_mixed_dns_answers(
    answers: tuple[str, ...],
) -> None:
    with pytest.raises(SourcePolicyViolation) as error:
        resolve_and_pin_public_host(
            "media.example",
            resolver=_resolver_for(*answers),
        )
    assert error.value.reason_code == "network_resolution_not_public"


@pytest.mark.parametrize(
    "literal",
    ["93.184.216.34", "127.0.0.1", "2606:4700:4700::1111", "::1"],
)
def test_resolution_rejects_literal_targets_before_dns(literal: str) -> None:
    resolver_called = False

    def resolver(*_args: object, **_kwargs: object) -> Sequence[tuple[object, ...]]:
        nonlocal resolver_called
        resolver_called = True
        return ()

    with pytest.raises(SourcePolicyViolation) as error:
        resolve_and_pin_public_host(literal, resolver=resolver)
    assert error.value.reason_code == "egress_literal_address_forbidden"
    assert not resolver_called


def test_authenticated_connect_pins_resolved_ip_and_tunnels_both_directions() -> None:
    proxy_side, remote_side = socket.socketpair()
    connected_to: list[tuple[tuple[str, int], float]] = []

    def connector(address: tuple[str, int], timeout: float) -> socket.socket:
        connected_to.append((address, timeout))
        return proxy_side

    with PinnedEgressProxy(
        resolver=_resolver_for("93.184.216.34"),
        connector=connector,
    ) as proxy:
        hostname, port, auth_header = _proxy_endpoint(proxy)
        with socket.create_connection((hostname, port), timeout=1.0) as client:
            client.settimeout(1.0)
            client.sendall(
                b"CONNECT media.example:443 HTTP/1.1\r\n"
                b"Host: media.example:443\r\n"
                + f"Proxy-Authorization: {auth_header}\r\n\r\n".encode("ascii")
            )
            assert _read_headers(client).startswith(b"HTTP/1.1 200")
            client.sendall(b"request-through-pinned-tunnel")
            assert remote_side.recv(4096) == b"request-through-pinned-tunnel"
            remote_side.sendall(b"response-through-pinned-tunnel")
            assert client.recv(4096) == b"response-through-pinned-tunnel"

    remote_side.close()
    assert connected_to == [(("93.184.216.34", 443), 15.0)]


def test_connect_without_proxy_authentication_is_rejected_before_resolution() -> None:
    resolver_called = False
    connector_called = False

    def resolver(*_args: object, **_kwargs: object) -> Sequence[tuple[object, ...]]:
        nonlocal resolver_called
        resolver_called = True
        return _dns_answers("93.184.216.34")

    def connector(_address: tuple[str, int], _timeout: float) -> socket.socket:
        nonlocal connector_called
        connector_called = True
        raise AssertionError("unauthenticated request must not connect upstream")

    with PinnedEgressProxy(resolver=resolver, connector=connector) as proxy:
        hostname, port, _auth_header = _proxy_endpoint(proxy)
        with socket.create_connection((hostname, port), timeout=1.0) as client:
            client.settimeout(1.0)
            client.sendall(b"CONNECT media.example:443 HTTP/1.1\r\nHost: media.example:443\r\n\r\n")
            assert _read_headers(client).startswith(b"HTTP/1.1 407")

    assert not resolver_called
    assert not connector_called


def test_context_exit_closes_listener_and_active_tunnels() -> None:
    proxy_side, remote_side = socket.socketpair()

    def connector(_address: tuple[str, int], _timeout: float) -> socket.socket:
        return proxy_side

    proxy = PinnedEgressProxy(
        resolver=_resolver_for("93.184.216.34"),
        connector=connector,
    )
    with proxy:
        hostname, port, auth_header = _proxy_endpoint(proxy)
        client = socket.create_connection((hostname, port), timeout=1.0)
        client.settimeout(1.0)
        client.sendall(
            b"CONNECT media.example:443 HTTP/1.1\r\n"
            + f"Proxy-Authorization: {auth_header}\r\n\r\n".encode("ascii")
        )
        assert _read_headers(client).startswith(b"HTTP/1.1 200")

    assert client.recv(1) == b""
    client.close()
    remote_side.close()
    with pytest.raises(OSError):
        socket.create_connection((hostname, port), timeout=0.2)
