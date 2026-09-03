from __future__ import annotations

import json
import os
import re
import runpy
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "scripts/lib/readiness_probe.py"
SQLITE = REPO / "scripts/sqlite-maintenance.py"


@pytest.fixture
def readiness_server():
    class Handler(BaseHTTPRequestHandler):
        status_code = 503
        payload = b'{"status":"ready"}'
        requests: ClassVar[list[tuple[str, dict[str, str]]]] = []

        def do_GET(self):
            self.requests.append((self.path, dict(self.headers)))
            self.send_response(self.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Location", "http://192.0.2.1/must-not-follow")
            self.end_headers()
            self.wfile.write(self.payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_http_probe_ignores_proxies_and_requires_healthy_status(
    settings, readiness_server, monkeypatch
):
    namespace = runpy.run_path(str(PROBE))
    server, handler = readiness_server
    target = namespace["probe_target"](
        settings.model_copy(update={"bind_port": server.server_port})
    )
    monkeypatch.setenv("http_proxy", "http://192.0.2.1:9")
    assert not namespace["probe_once"](target, timeout=1)
    handler.status_code = 302
    assert not namespace["probe_once"](target, timeout=1)
    handler.status_code = 200
    assert namespace["probe_once"](target, timeout=1)
    assert len(handler.requests) == 3
    for path, headers in handler.requests:
        assert path == "/health/ready"
        assert headers["Host"] == f"testserver:{server.server_port}"
        assert not any(
            name.casefold() in {"cookie", "authorization", "x-forwarded-for", "x-forwarded-host"}
            for name in headers
        )


def test_probe_requires_allowed_local_bind_target(settings, monkeypatch):
    namespace = runpy.run_path(str(PROBE))
    with pytest.raises(ValueError, match="allowed local bind address"):
        namespace["probe_target"](
            settings.model_copy(update={"allowed_client_cidrs": ["192.168.0.0/16"]})
        )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.1", 8787))],
    )
    with pytest.raises(ValueError, match="allowed local bind address"):
        namespace["probe_target"](
            settings.model_copy(
                update={"bind_host": "remote.example", "allowed_client_cidrs": ["0.0.0.0/0"]}
            )
        )


@pytest.mark.parametrize("schema,legacy", [("0002", True), ("0004", False)])
def test_legacy_route_requires_matching_target_manifest(
    tmp_path, monkeypatch, readiness_server, settings, schema, legacy
):
    namespace = runpy.run_path(str(PROBE))
    choose = namespace["legacy_readiness"]
    monkeypatch.setitem(choose.__globals__, "EXPECTED_SCHEMA_REVISION", schema)
    manifest = tmp_path / "RELEASE.json"
    manifest.write_text(json.dumps({"schema_revision": schema}))
    assert choose(tmp_path) is legacy
    server, handler = readiness_server
    handler.status_code = 200
    handler.payload = b'{"status":"live"}' if legacy else b'{"status":"ready"}'
    target = namespace["probe_target"](
        settings.model_copy(update={"bind_port": server.server_port}), legacy=choose(tmp_path)
    )
    assert namespace["probe_once"](target, timeout=1)
    assert handler.requests[0][0] == ("/health/live" if legacy else "/health/ready")
    manifest.write_text(json.dumps({"schema_revision": "0001"}))
    with pytest.raises(ValueError, match="disagrees"):
        choose(tmp_path)


def test_readiness_needs_two_consecutive_successes_and_has_deadline(monkeypatch, settings):
    namespace = runpy.run_path(str(PROBE))
    wait = namespace["wait_for_ready"]
    now = [0.0]
    answers = iter([True, False, True, True])
    attempts = []

    def probe(*args, **kwargs):
        attempts.append(kwargs["timeout"])
        return next(answers)

    monkeypatch.setitem(wait.__globals__, "probe_once", probe)
    monkeypatch.setattr(wait.__globals__["time"], "monotonic", lambda: now[0])
    monkeypatch.setattr(
        wait.__globals__["time"], "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    assert wait(object(), timeout_seconds=5)
    assert len(attempts) == 4
    monkeypatch.setitem(wait.__globals__, "probe_once", lambda *args, **kwargs: False)
    assert not wait(object(), timeout_seconds=3)
    assert now[0] == 6


def shell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}", source)
    assert match, name
    return match.group(0)


@pytest.mark.parametrize("failure", ["unready_http", "service_start"])
def test_activation_failure_restores_real_release_database_pair(
    tmp_path, readiness_server, failure
):
    """Exercise deploy's actual activation block/recovery with a real SQLite restore."""
    server, handler = readiness_server
    old = tmp_path / "old-release"
    release = tmp_path / "new-release"
    old.mkdir()
    (release / "scripts").mkdir(parents=True)
    shutil.copy2(SQLITE, release / "scripts/sqlite-maintenance.py")
    database = tmp_path / "music-agent.db"
    backup = tmp_path / "paired.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE alembic_version(version_num TEXT);"
            "INSERT INTO alembic_version VALUES ('0002');"
            "CREATE TABLE users(id TEXT,password_hash TEXT);"
            "INSERT INTO users VALUES ('existing-admin','preserved-hash');"
            "CREATE TABLE sessions(id TEXT,user_id TEXT);"
            "INSERT INTO sessions VALUES ('existing-session','existing-admin');"
        )
    subprocess.run(  # noqa: S603 - repository backup helper, synthetic SQLite data
        [
            sys.executable,
            str(SQLITE),
            "backup",
            "--source",
            str(database),
            "--destination",
            str(backup),
        ],
        check=True,
        capture_output=True,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE alembic_version SET version_num='0004'")
        connection.execute("UPDATE users SET password_hash='new-state'")
    current = tmp_path / "current"
    current.symlink_to(release, target_is_directory=True)
    deployments = tmp_path / "deployments"
    deployments.mkdir()
    log = tmp_path / "lifecycle.log"
    deploy = (REPO / "scripts/deploy.sh").read_text()
    common = (REPO / "scripts/lib/common.sh").read_text()
    activation = re.search(r'(?ms)^if \[\[ "\$start_services" -eq 1 \]\]; then\n.*?^fi$', deploy)
    assert activation
    probe_code = (
        "import runpy,sys; n=runpy.run_path(sys.argv[1]); "
        "t=n['ProbeTarget']('127.0.0.1',int(sys.argv[2]),'testserver'); "
        "sys.exit(0 if n['wait_for_ready'](t,timeout_seconds=0.05) else 1)"
    )
    link_code = (
        "import os,sys; temp=sys.argv[2]+'.replacement'; "
        "os.symlink(sys.argv[1],temp); os.replace(temp,sys.argv[2])"
    )
    variables = {
        "release": release,
        "previous_release": old,
        "release_id": "fixture",
        "MUSIC_AGENT_PYTHON": sys.executable,
        "MUSIC_AGENT_DB": database,
        "MUSIC_AGENT_CURRENT_LINK": current,
        "MUSIC_AGENT_DEPLOYMENT_DIR": deployments,
        "MUSIC_AGENT_SERVICE_USER": "music-agent",
        "MUSIC_AGENT_SERVICE_GROUP": "music-agent",
        "predeploy_backup": backup,
        "log": log,
    }
    script = "set -euo pipefail\n" + "\n".join(
        f"{name}={shlex.quote(str(value))}" for name, value in variables.items()
    )
    script += "\n" + "\n".join(
        [
            "database_existed=1; previous_web_active=1; previous_worker_active=1; start_services=1",
            'music_agent_warn() { printf "%s\\n" "$*" >> "$log"; }',
            'music_agent_die() { printf "%s\\n" "$*" >> "$log"; exit 1; }',
            'music_agent_stop_services() { printf "stopped\\n" >> "$log"; }',
            'restore_previous_units() { printf "restored-units\\n" >> "$log"; }',
            'music_agent_systemctl() { printf "%s\\n" "$*" >> "$log"; return 0; }',
            f"music_agent_start_services() {{ return {1 if failure == 'service_start' else 0}; }}",
            "chown() { :; }; chmod() { :; }",
            'write_manifest() { printf \'{"status":"%s"}\\n\' "$1" > "$release/RELEASE.json"; }',
            "music_agent_atomic_symlink() { "
            + shlex.join([sys.executable, "-c", link_code])
            + ' "$1" "$2"; }',
            "music_agent_check_readiness() { "
            + shlex.join([sys.executable, "-c", probe_code, str(PROBE), str(server.server_port)])
            + "; }",
            shell_function(common, "music_agent_wait_ready"),
            shell_function(deploy, "restore_previous_database"),
            shell_function(deploy, "recover_activation"),
            activation.group(0),
        ]
    )
    result = subprocess.run(  # noqa: S603 - actual recovery functions with bounded fixture shims
        ["/bin/bash", "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 1, result.stderr
    assert current.resolve() == old
    assert json.loads((release / "RELEASE.json").read_text())["status"] == "failed"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0002",)
        assert connection.execute("SELECT * FROM users").fetchone() == (
            "existing-admin",
            "preserved-hash",
        )
        assert connection.execute("SELECT * FROM sessions").fetchone() == (
            "existing-session",
            "existing-admin",
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    lifecycle = log.read_text()
    assert "restored-units" in lifecycle
    assert (
        "start music-agent-web.service" in lifecycle
        and "start music-agent-worker.service" in lifecycle
    )
    assert bool(handler.requests) is (failure == "unready_http")


def test_probe_has_outer_process_deadline_and_service_account_boundary():
    common = (REPO / "scripts/lib/common.sh").read_text()
    assert 'timeout --kill-after=5s 65s runuser -u "$MUSIC_AGENT_SERVICE_USER" -- env -i' in common
    assert 'local probe="$release/scripts/lib/readiness_probe.py"' in common
    assert (
        '"$release/venv/bin/python" -I -B - --release "$release" "${arguments[@]}" < "$probe"'
        in common
    )
    assert '"MUSIC_AGENT_SERVICE_ROLE=worker"' in common
    validator = (REPO / "scripts/validate.sh").read_text()
    assert 'music_agent_check_readiness "$release" check' in validator
    assert 'music_agent_wait_ready "$release"' in validator


def test_isolated_stdin_probe_ignores_private_checkout_and_pythonpath(tmp_path):
    poisoned_app = tmp_path / "app"
    poisoned_app.mkdir()
    (poisoned_app / "__init__.py").write_text(
        "raise RuntimeError('imported administrator checkout')"
    )
    (tmp_path / "RELEASE.json").write_text('{"schema_revision":"0004"}')
    result = subprocess.run(  # noqa: S603 - repository helper through isolated interpreter stdin
        [sys.executable, "-I", "-B", "-", "--release", str(tmp_path), "--check"],
        input=PROBE.read_text(),
        text=True,
        capture_output=True,
        timeout=10,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path), "MUSIC_AGENT_ENVIRONMENT": "test"},
    )
    assert result.returncode == 0, result.stderr
    assert not (poisoned_app / "__pycache__").exists()
