from __future__ import annotations

import threading

from app.workers.queue import (
    DownloadJobQueue,
    JobCancellationRequested,
    JobLease,
    LeaseLostError,
)


class LeaseMonitor:
    """Renew a lease and expose cancellation to a running subprocess."""

    def __init__(
        self,
        queue: DownloadJobQueue,
        lease: JobLease,
        *,
        interval_seconds: float | None = None,
    ) -> None:
        self.queue = queue
        self.lease = lease
        self.interval_seconds = interval_seconds or max(2.0, min(20.0, queue.lease_seconds / 3))
        self.cancel_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self) -> LeaseMonitor:
        if self._thread is not None:
            raise RuntimeError("lease monitor was already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-{self.lease.job_id[:12]}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_seconds + 1.0))

    def raise_if_unusable(self) -> None:
        if self._error is not None:
            raise self._error
        if self.cancel_event.is_set():
            raise JobCancellationRequested("job cancellation was requested")

    def __enter__(self) -> LeaseMonitor:
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.queue.heartbeat(self.lease)
            except JobCancellationRequested as exc:
                self._error = exc
                self.cancel_event.set()
                return
            except LeaseLostError as exc:
                self._error = exc
                self.cancel_event.set()
                return
            except BaseException as exc:  # keep the worker from running unfenced on DB failure
                self._error = exc
                self.cancel_event.set()
                return
