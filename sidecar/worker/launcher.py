from __future__ import annotations

import gc
import multiprocessing
import os
import signal
import subprocess
import sys
import threading


_start_lock = threading.Lock()
_context = None


def _detach(handle):
    return handle.detach()


class _PipeEnd:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __reduce__(self):
        from multiprocessing.reduction import DupFd
        return _detach, (DupFd(self.fd),)


def _run_worker(stdin: int, stdout: int, stderr: int, environment: dict[str, str]) -> None:
    # The forkserver is single-threaded and contains libraries only, not gateway state.
    os.environ.clear()
    os.environ.update(environment)
    os.setsid()
    for source, target in ((stdin, 0), (stdout, 1), (stderr, 2)):
        os.dup2(source, target)
        if source != target:
            os.close(source)
    sys.stdin = os.fdopen(0, "r", encoding="utf-8", errors="replace", closefd=False)
    sys.stdout = os.fdopen(1, "w", encoding="utf-8", buffering=1, errors="replace", closefd=False)
    sys.stderr = os.fdopen(2, "w", encoding="utf-8", buffering=1, errors="replace", closefd=False)
    gc.enable()
    from sidecar.worker.process import ATVP_RUNTIME, main
    try:
        main()
    finally:
        ATVP_RUNTIME.close()


class ForkWorker:
    """Popen-shaped stdio transport backed by the single-threaded forkserver."""

    def __init__(self) -> None:
        global _context
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        error_read, error_write = os.pipe()
        owned = {input_read, input_write, output_read, output_write, error_read, error_write}
        process = None
        try:
            with _start_lock:
                if _context is None:
                    _context = multiprocessing.get_context("forkserver")
                    _context.set_forkserver_preload(["sidecar.worker.preload"])
                process = _context.Process(
                    target=_run_worker,
                    args=(_PipeEnd(input_read), _PipeEnd(output_write),
                          _PipeEnd(error_write), dict(os.environ)),
                )
                process.start()
            self._process = process
            self._pid = process.pid
            for fd in (input_read, output_write, error_write):
                os.close(fd)
                owned.remove(fd)
            self.stdin = os.fdopen(input_write, "w", encoding="utf-8", buffering=1, errors="replace")
            owned.remove(input_write)
            self.stdout = os.fdopen(output_read, "r", encoding="utf-8", errors="replace")
            owned.remove(output_read)
            self.stderr = os.fdopen(error_read, "r", encoding="utf-8", errors="replace")
            owned.remove(error_read)
        except BaseException:
            if process is not None and process.pid is not None:
                process.kill()
                process.join()
                process.close()
            raise
        finally:
            for fd in owned:
                os.close(fd)

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        return self._process.exitcode

    def wait(self, timeout: float | None = None) -> int:
        self._process.join(timeout)
        code = self._process.exitcode
        if code is None:
            raise subprocess.TimeoutExpired("t4-worker", timeout)
        return code

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, signum: int) -> None:
        try:
            os.killpg(self.pid, signum)
        except ProcessLookupError:
            if self.poll() is None:
                os.kill(self.pid, signum)

    def close(self) -> None:
        # A plugin may have spawned children holding its stdio pipes open.
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._process.close()


def launch_worker(start_method: str = "auto"):
    if start_method not in {"auto", "subprocess", "forkserver"}:
        raise ValueError("unsupported worker start method")
    if start_method == "forkserver" or (start_method == "auto" and sys.platform == "linux"):
        return ForkWorker()
    return subprocess.Popen(
        [sys.executable, "-m", "sidecar.worker.process"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
