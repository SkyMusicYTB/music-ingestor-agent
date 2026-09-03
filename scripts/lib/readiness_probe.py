"""Credential-free, local-only HTTP readiness polling for native activation."""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.db.engine import EXPECTED_SCHEMA_REVISION
from app.services.security import normalize_host_header


@dataclass(frozen=True)
class ProbeTarget:
    address: str
    port: int
    host_header: str
    legacy: bool = False


def legacy_readiness(release: Path) -> bool:
    manifest = release / "RELEASE.json"
    if not manifest.exists():
        return False
    if manifest.stat().st_size > 65536:
        raise ValueError("invalid release manifest")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid release manifest")
    revision = value.get("schema_revision")
    if revision != EXPECTED_SCHEMA_REVISION:
        raise ValueError("release manifest disagrees with its installed application")
    # Historical readiness routes demand writes to the web sandbox's read-only
    # download path. Target DB/schema validation precedes this compatibility
    # check. Never substitute liveness for a new release's readiness endpoint.
    return revision in {"0001", "0002", "0003"}


def probe_target(settings: Settings, *, legacy: bool = False) -> ProbeTarget:
    host = settings.bind_host.strip("[]")
    host = {
        "0.0.0.0": "127.0.0.1",  # noqa: S104 - map wildcard listeners to loopback, never bind them
        "::": "::1",
        "localhost": "127.0.0.1",
    }.get(host, host)
    hosts = settings.effective_trusted_hosts
    if not hosts:
        raise ValueError("readiness requires a configured trusted host")
    authority = hosts[0]
    if authority.startswith("*."):
        authority = "readiness" + authority[1:]
    authority = normalize_host_header(f"{authority}:{settings.bind_port}")
    try:
        addresses = socket.getaddrinfo(host, settings.bind_port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError("readiness bind address cannot be resolved") from error
    for family, _kind, _protocol, _canonical, address in addresses:
        candidate = ipaddress.ip_address(address[0])
        if candidate.is_unspecified or candidate.is_multicast:
            continue
        if not any(candidate in network for network in settings.allowed_networks):
            continue
        try:
            # An explicitly bound hostname must resolve to this machine, never an
            # arbitrary remote host. This local ephemeral bind transmits no data.
            with socket.socket(family, socket.SOCK_STREAM) as local:
                local.bind((str(candidate), 0))
        except OSError:
            continue
        return ProbeTarget(str(candidate), settings.bind_port, authority, legacy)
    raise ValueError(
        "readiness needs an allowed local bind address; wildcard listeners require loopback CIDRs"
    )


def probe_once(target: ProbeTarget, *, timeout: float) -> bool:
    connection = http.client.HTTPConnection(target.address, target.port, timeout=timeout)
    try:
        # HTTPConnection ignores proxy environment variables and never redirects.
        # No cookies, forwarding headers, API secrets or authentication are sent.
        connection.request(
            "GET",
            "/health/live" if target.legacy else "/health/ready",
            headers={
                "Host": target.host_header,
                "Connection": "close",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read(1025)
        return (
            response.status == 200
            and len(body) <= 1024
            and json.loads(body) == {"status": "live" if target.legacy else "ready"}
        )
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def wait_for_ready(target: ProbeTarget, *, timeout_seconds: float = 60) -> bool:
    deadline = time.monotonic() + timeout_seconds
    consecutive = 0
    while (remaining := deadline - time.monotonic()) > 0:
        consecutive = consecutive + 1 if probe_once(target, timeout=min(2.0, remaining)) else 0
        if consecutive >= 2:
            return True
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate local target without HTTP")
    parser.add_argument("--release", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        target = probe_target(
            Settings(service_role="worker"), legacy=legacy_readiness(arguments.release)
        )
    except (OSError, ValueError):
        print(
            "Readiness target is invalid; check bind address, "
            "allowed-client CIDRs and trusted hosts."
        )
        return 1
    if arguments.check or wait_for_ready(target):
        return 0
    print("Web readiness did not become healthy within 60 seconds.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
