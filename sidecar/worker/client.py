from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
import hashlib
import time
import re
from dataclasses import dataclass
from typing import Any

from sidecar.worker.protocol import WorkerRequest, WorkerResponse
from sidecar.worker.errors import safe_error_fields
from sidecar.worker.launcher import ForkWorker, launch_worker
from sidecar.diagnostics import current_trace, record


class WorkerError(RuntimeError):
    """Base error for isolated worker failures."""


class WorkerTimeoutError(WorkerError):
    """Worker did not answer before the request deadline."""


class WorkerProtocolError(WorkerError):
    """Worker emitted an invalid or mismatched response."""


class WorkerRemoteError(WorkerError):
    """Worker rejected a request."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = safe_error_fields(details or {})

    def public_error(self) -> dict[str, Any]:
        missing = self.details.get("missing_module")
        message = (
            f"插件缺少依赖模块：{missing}" if missing
            else f"插件执行失败：{self.details.get('root_error', 'RuntimeError')}"
        )
        return {"code": self.code, "message": message, **self.details}


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    request_timeout: float = 5.0
    start_method: str = "auto"


class WorkerSession:
    def __init__(self, config: WorkerConfig) -> None:
        self._config = config
        self._process: subprocess.Popen[str] | ForkWorker | None = None
        self._responses: queue.Queue[WorkerResponse | Exception] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._trace_context = current_trace()
        self.proxy_url: str | None = None
        self._lock = threading.Lock()
        self._last_activity = time.monotonic()
        self._next_reclaim = 0.0
        self._maintenance_request: str | None = None
        self.start()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def start(self) -> None:
        if self._process is not None:
            return
        process = launch_worker(self._config.start_method)
        self._process = process
        self._reader = threading.Thread(
            target=self._read_responses,
            name=f"t4-worker-reader-{process.pid}",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, args=(process,),
            name=f"t4-worker-stderr-{process.pid}", daemon=True,
        )
        self._stderr_reader.start()

    def _read_stderr(self, process: subprocess.Popen[str] | ForkWorker) -> None:
        if process.stderr is None:
            return
        window, count, suppressed = time.monotonic(), 0, 0
        while chunk := process.stderr.readline(4096):
            context = dict(self._trace_context)
            store = context.pop("store", None)
            if store is not None:
                if time.monotonic() - window >= 1:
                    if suppressed:
                        store.emit("worker.stderr_suppressed", **context,
                                   session=process.pid, count=suppressed)
                    window, count, suppressed = time.monotonic(), 0, 0
                count += 1
                if count > 20:
                    suppressed += 1
                    continue
                # Plugin stderr may contain cookies, ext or full URLs. Keep metadata only.
                exception = re.match(r"^([A-Za-z_]\w*(?:Error|Exception)):", chunk)
                store.emit(
                    "worker.stderr", **context, bytes=len(chunk.encode("utf-8")),
                    session=process.pid,
                    level="error" if "Error" in chunk or "Traceback" in chunk else "info",
                    fingerprint=hashlib.sha256(chunk.encode()).hexdigest()[:16],
                    **({"error": exception.group(1)} if exception else {}),
                )

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
                if response.request_id == self._maintenance_request:
                    self._maintenance_request = None
                    result = response.result or {}
                    if response.ok and result.get("reclaimed"):
                        context = dict(self._trace_context)
                        store = context.pop("store", None)
                        if store is not None:
                            store.emit(
                                "worker.memory_reclaimed", **context,
                                session=process.pid, count=result.get("collected", 0),
                                reason="trimmed" if result.get("trimmed") else "collected",
                            )
                else:
                    self._responses.put(response)
                del line, payload, response
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
            try:
                return self._request(method, params, timeout=timeout)
            finally:
                self._last_activity = time.monotonic()

    def _request(
        self, method: str, params: dict[str, Any] | None, *, timeout: float | None,
    ) -> Any:
        self._trace_context = current_trace()
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
        if timeout is None and method == "detail" and any(
            str(value).startswith(("atvp_detail:", "atvp_group:"))
            for value in request.params.get("ids", [])
        ):
            # One T4 detail can expand several AT resource redirects/shares.
            deadline = max(deadline, 120.0)
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
        self.proxy_url = response_or_error.proxy_url
        if not response_or_error.ok:
            error = response_or_error.error or {}
            raise WorkerRemoteError(
                str(error.get("code", "worker_error")),
                str(error.get("message", "worker rejected request")),
                error,
            )
        return response_or_error.result

    def reclaim_idle(self, *, idle_seconds: float = 30, interval: float = 300) -> bool:
        if not self._lock.acquire(blocking=False):
            return False
        try:
            now = time.monotonic()
            process = self._process
            if (
                process is None or process.poll() is not None or process.stdin is None
                or now - self._last_activity < idle_seconds or now < self._next_reclaim
                or self._maintenance_request is not None
            ):
                return False
            request = WorkerRequest(
                request_id=uuid.uuid4().hex, method="reclaim",
                params={"idle_seconds": idle_seconds},
            )
            self._maintenance_request = request.request_id
            try:
                process.stdin.write(json.dumps(request.to_dict()) + "\n")
                process.stdin.flush()
            except (OSError, ValueError):
                self._maintenance_request = None
                return False
            self._next_reclaim = now + interval
            return True
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        if hasattr(process, "close"):
            process.close()
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self._stderr_reader is not None:
            self._stderr_reader.join(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        self.proxy_url = None
        self._trace_context = {}
        self._maintenance_request = None
        while not self._responses.empty():
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break

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
        start = time.monotonic()
        record("worker.start", method=method, session=session.pid)
        try:
            result = session.request(method, params, timeout=timeout)
            record("worker.complete", method=method, session=session.pid,
                   duration_ms=round((time.monotonic() - start) * 1000))
            return result
        except WorkerTimeoutError:
            record("worker.timeout", method=method, session=session.pid)
            self.restart(source_id)
            raise
        except WorkerError as exc:
            record(
                "worker.failed", method=method, session=session.pid, error=type(exc).__name__,
                **({
                    "reason": exc.code,
                    "fingerprint": hashlib.sha256(exc.message.encode()).hexdigest()[:16],
                    **exc.details,
                } if isinstance(exc, WorkerRemoteError) else {}),
            )
            self.restart(source_id)
            raise

    def restart(self, source_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(source_id, None)
        if session is not None:
            session.close()

    def proxy_endpoint(self, source_id: str) -> str | None:
        with self._lock:
            session = self._sessions.get(source_id)
            return session.proxy_url if session is not None else None

    def reclaim_idle(
        self, *, idle_seconds: float = 30, interval: float = 300, batch_size: int = 8,
    ) -> int:
        with self._lock:
            sessions = tuple(self._sessions.values())
        scheduled = 0
        for session in sessions:
            scheduled += session.reclaim_idle(idle_seconds=idle_seconds, interval=interval)
            if scheduled >= batch_size:
                break
        return scheduled

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
