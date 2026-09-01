from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ServiceHeartbeat

logger = logging.getLogger(__name__)


class WorkerHeartbeat:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        service_version: str,
        interval_seconds: float = 10.0,
    ) -> None:
        self.factory = factory
        self.service_version = service_version
        self.interval_seconds = interval_seconds
        self._active = 0
        self._active_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._write()
        self._thread = threading.Thread(target=self._run, name="worker-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1)
        self.set_active(0)
        self._write()

    def set_active(self, count: int) -> None:
        with self._active_lock:
            self._active = max(0, count)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._write()
            except Exception:
                logger.exception("worker heartbeat update failed")

    def _write(self) -> None:
        with self._active_lock:
            active = self._active
        with self.factory.begin() as session:
            heartbeat = session.get(ServiceHeartbeat, "worker")
            if heartbeat is None:
                heartbeat = ServiceHeartbeat(service="worker", service_version=self.service_version)
                session.add(heartbeat)
            heartbeat.service_version = self.service_version
            heartbeat.last_heartbeat_at = datetime.now(UTC)
            heartbeat.active_work_count = active
            heartbeat.details_json = json.dumps(
                {"pid": os.getpid()}, ensure_ascii=True, separators=(",", ":")
            )
