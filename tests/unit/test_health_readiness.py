from __future__ import annotations

import os
from types import SimpleNamespace

from sqlalchemy import text

from app.api import health


def test_readiness_does_not_require_worker_writes_or_initial_scan(client, monkeypatch):
    app = client.app
    app.state.settings.initial_scan_required = True
    original = os.access

    def web_access(path, mode):
        if path == app.state.settings.downloads_path and mode & os.W_OK:
            return False
        return original(path, mode)

    monkeypatch.setattr(health.os, "access", web_access)
    request = SimpleNamespace(app=app)
    ready, details = health.health_snapshot(request)
    assert ready
    assert details["initial_scan"] == {"ok": False, "required": True, "completed": False}
    response = client.get("/health/ready")
    assert response.status_code == 200 and response.json() == {"status": "ready"}


def test_readiness_requires_web_state_and_artwork_access(client, monkeypatch):
    original = os.access
    monkeypatch.setattr(
        health.os,
        "access",
        lambda path, mode: False
        if path == client.app.state.settings.artwork_path
        else original(path, mode),
    )
    response = client.get("/health/ready")
    assert response.status_code == 503 and response.json() == {"status": "not_ready"}


def test_readiness_rejects_wrong_schema_without_exposing_details(client):
    with client.app.state.engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='0002'"))
    response = client.get("/health/ready")
    assert response.status_code == 503 and response.json() == {"status": "not_ready"}


def test_readiness_requires_allowed_client_and_trusted_host(client):
    assert client.get("/health/ready", headers={"host": "unknown.example"}).status_code == 400
    client.app.state.settings.allowed_client_cidrs = ["192.168.0.0/16"]
    assert client.get("/health/ready").status_code == 403
