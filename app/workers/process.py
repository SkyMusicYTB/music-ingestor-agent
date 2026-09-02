from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO, Protocol

_CHUNK_BYTES = 32 * 1024
_QUEUE_CHUNKS = 16


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class BoundedProcessError(RuntimeError):
    pass


class ProcessOutputLimitExceeded(BoundedProcessError):
    def __init__(self, stream: str) -> None:
        super().__init__(f"subprocess {stream} exceeded its configured byte limit")
        self.stream = stream


class ProcessFrameLimitExceeded(BoundedProcessError):
    def __init__(self, stream: str) -> None:
        super().__init__(f"subprocess {stream} emitted an oversized frame")
        self.stream = stream


class ProcessTimedOut(BoundedProcessError):
    pass


class ProcessCancelled(BoundedProcessError):
    pass


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr_tail: bytes


FrameCallback = Callable[[str, bytes], None]


def terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - production target is Ubuntu
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - production target is Ubuntu
            process.kill()
    except ProcessLookupError:
        return
    process.wait(timeout=max(1.0, grace_seconds))


def run_bounded_process(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: float,
    cancel_signal: CancellationSignal | None = None,
    stdout_limit: int,
    stderr_limit: int,
    frame_limit: int = 256 * 1024,
    stderr_tail_limit: int = 16 * 1024,
    on_frame: FrameCallback | None = None,
    capture_stdout: bool = True,
) -> BoundedProcessResult:
    """Run one fixed-argv child with pre-allocation output bounds.

    Reader threads perform fixed-size ``os.read`` calls into a bounded queue. The
    consumer counts raw bytes before storing or decoding them. A giant newline-free
    frame, total output overflow, timeout, or cancellation terminates the entire
    process group without calling ``communicate()``.
    """

    if not argv:
        raise ValueError("argv must not be empty")
    if min(stdout_limit, stderr_limit, frame_limit, stderr_tail_limit) <= 0:
        raise ValueError("process output limits must be positive")
    if timeout_seconds <= 0:
        raise ValueError("process timeout must be positive")

    process = subprocess.Popen(  # noqa: S603 - caller supplies fixed argv, never a shell
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        shell=False,
        env=dict(environment),
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    chunks: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=_QUEUE_CHUNKS)
    stop_readers = threading.Event()

    def reader(name: str, stream: BinaryIO) -> None:
        try:
            while not stop_readers.is_set():
                chunk = os.read(stream.fileno(), _CHUNK_BYTES)
                if not chunk:
                    break
                while not stop_readers.is_set():
                    try:
                        chunks.put((name, chunk), timeout=0.1)
                        break
                    except queue.Full:
                        continue
        finally:
            while not stop_readers.is_set():
                try:
                    chunks.put((name, None), timeout=0.1)
                    break
                except queue.Full:
                    continue

    readers = [
        threading.Thread(
            target=reader,
            args=("stdout", process.stdout),
            name="bounded-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=reader,
            args=("stderr", process.stderr),
            name="bounded-stderr",
            daemon=True,
        ),
    ]
    for thread in readers:
        thread.start()

    started = time.monotonic()
    totals = {"stdout": 0, "stderr": 0}
    limits = {"stdout": stdout_limit, "stderr": stderr_limit}
    frames = {"stdout": bytearray(), "stderr": bytearray()}
    stdout = bytearray()
    stderr_tail = bytearray()
    closed: set[str] = set()
    failure: BaseException | None = None
    try:
        while len(closed) < 2:
            if cancel_signal is not None and cancel_signal.is_set():
                failure = ProcessCancelled("subprocess was cancelled")
                terminate_process_group(process)
                break
            if time.monotonic() - started > timeout_seconds:
                failure = ProcessTimedOut("subprocess exceeded its time limit")
                terminate_process_group(process)
                break
            try:
                stream_name, chunk = chunks.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None and not any(thread.is_alive() for thread in readers):
                    break
                continue
            if chunk is None:
                closed.add(stream_name)
                continue
            totals[stream_name] += len(chunk)
            if totals[stream_name] > limits[stream_name]:
                failure = ProcessOutputLimitExceeded(stream_name)
                terminate_process_group(process)
                break
            if stream_name == "stdout" and capture_stdout:
                stdout.extend(chunk)
            if stream_name == "stderr":
                stderr_tail.extend(chunk)
                if len(stderr_tail) > stderr_tail_limit:
                    del stderr_tail[: len(stderr_tail) - stderr_tail_limit]
            if on_frame is not None:
                frame_buffer = frames[stream_name]
                frame_buffer.extend(chunk)
                _emit_frames(stream_name, frame_buffer, frame_limit, on_frame)

        if failure is None and on_frame is not None:
            for stream_name, frame_buffer in frames.items():
                if frame_buffer:
                    if len(frame_buffer) > frame_limit:
                        failure = ProcessFrameLimitExceeded(stream_name)
                        terminate_process_group(process)
                        break
                    on_frame(stream_name, bytes(frame_buffer))
                    frame_buffer.clear()
        if failure is None:
            remaining = max(0.1, timeout_seconds - (time.monotonic() - started))
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                failure = ProcessTimedOut("subprocess exceeded its time limit")
                terminate_process_group(process)
    except BaseException as exc:
        failure = exc
        terminate_process_group(process)
    finally:
        stop_readers.set()
        if process.poll() is None:
            terminate_process_group(process)
        for thread in readers:
            thread.join(timeout=1.0)
        process.stdout.close()
        process.stderr.close()

    if failure is not None:
        raise failure
    return BoundedProcessResult(
        returncode=int(process.returncode or 0),
        stdout=bytes(stdout),
        stderr_tail=bytes(stderr_tail),
    )


def _emit_frames(
    stream_name: str,
    buffer: bytearray,
    frame_limit: int,
    callback: FrameCallback,
) -> None:
    while True:
        newline = buffer.find(b"\n")
        carriage = buffer.find(b"\r")
        boundaries = [value for value in (newline, carriage) if value >= 0]
        if not boundaries:
            if len(buffer) > frame_limit:
                raise ProcessFrameLimitExceeded(stream_name)
            return
        boundary = min(boundaries)
        if boundary > frame_limit:
            raise ProcessFrameLimitExceeded(stream_name)
        frame = bytes(buffer[:boundary])
        del buffer[: boundary + 1]
        callback(stream_name, frame)
