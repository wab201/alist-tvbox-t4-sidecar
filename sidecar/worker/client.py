from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from sidecar.worker.protocol import WorkerRequest, WorkerResponse


class WorkerError(RuntimeError):
    """Base error for isolated worker failures."""


class WorkerTimeoutError(WorkerError):
    """Worker did not answer before the request deadline."""


class WorkerProtocolError(WorkerError):
    """Worker emitted an invalid or mismatched response."""


class WorkerRemoteError(WorkerError):
    """Worker rejected a request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    request_timeout: float = 5.0


class WorkerSession:
    def __init__(self, config: WorkerConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[WorkerResponse | Exception] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self.start()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def start(self) -> None:
        if self._process is not None:
            return
        process = subprocess.Popen(
            [sys.executable, "-m", "sidecar.worker.process"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._process = process
        self._reader = threading.Thread(
            target=self._read_responses,
            name=f"t4-worker-reader-{process.pid}",
            daemon=True,
        )
        self._reader.start()

    def _read_responses(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        raise ValueError("response must be an object")
                    response = WorkerResponse.from_dict(payload)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    self._responses.put(WorkerProtocolError(str(exc)))
                    continue
                self._responses.put(response)
        except Exception as exc:
            self._responses.put(WorkerProtocolError(str(exc)))
        finally:
            self._responses.put(WorkerError("worker response stream closed"))

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise WorkerError("worker is not running")
            if process.stdin is None:
                raise WorkerError("worker stdin is unavailable")

            request = WorkerRequest(
                request_id=uuid.uuid4().hex,
                method=method,
                params=params or {},
            )
            process.stdin.write(json.dumps(request.to_dict()) + "\n")
            process.stdin.flush()

            deadline = self._config.request_timeout if timeout is None else timeout
            try:
                response_or_error = self._responses.get(timeout=deadline)
            except queue.Empty as exc:
                raise WorkerTimeoutError(
                    f"worker request timed out after {deadline:.3f}s"
                ) from exc

            if isinstance(response_or_error, Exception):
                raise response_or_error
            if response_or_error.request_id != request.request_id:
                raise WorkerProtocolError("worker response id mismatch")
            if not response_or_error.ok:
                error = response_or_error.error or {}
                raise WorkerRemoteError(
                    str(error.get("code", "worker_error")),
                    str(error.get("message", "worker rejected request")),
                )
            return response_or_error.result

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        if self._reader is not None:
            self._reader.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()

    def __enter__(self) -> "WorkerSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class WorkerPool:
    """Keeps one isolated worker session per source for context affinity."""

    def __init__(self, config: WorkerConfig | None = None) -> None:
        self._config = config or WorkerConfig()
        self._sessions: dict[str, WorkerSession] = {}
        self._lock = threading.Lock()

    def _session_for(self, source_id: str) -> WorkerSession:
        with self._lock:
            session = self._sessions.get(source_id)
            if session is None:
                session = WorkerSession(self._config)
                self._sessions[source_id] = session
            return session

    def request(
        self,
        source_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        session = self._session_for(source_id)
        try:
            return session.request(method, params, timeout=timeout)
        except WorkerTimeoutError:
            self.restart(source_id)
            raise
        except WorkerError:
            self.restart(source_id)
            raise

    def restart(self, source_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(source_id, None)
        if session is not None:
            session.close()

    def close(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def __enter__(self) -> "WorkerPool":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
