from __future__ import annotations

import base64
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests


class AtvpRuntimeError(RuntimeError):
    """Safe, non-secret runtime failure."""


class AtvpRuntime:
    def __init__(self, vendor_path: str | None = None) -> None:
        self._vendor_path = Path(vendor_path or Path(__file__).parent / "vendor" / "Atvp.py")
        self._module: types.ModuleType | None = None
        self._spider: Any = None
        self._js_process: subprocess.Popen[str] | None = None
        self._js_lock = threading.Lock()
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="t4-atvp-")

    def init(self, extend: str) -> dict[str, Any]:
        payload = self._decode_ext(extend)
        loader = str(payload.get("loader") or "").strip()
        module_path = self._prepare_loader(loader)
        if module_path.suffix.lower() == ".js":
            return self._init_javascript(module_path, extend)
        module = self._load_module(module_path)
        self._module = module
        spider_class = getattr(module, "Spider", None)
        if spider_class is None:
            raise AtvpRuntimeError("Atvp loader has no Spider class")
        if hasattr(spider_class, "_instance"):
            spider_class._instance = None
        self._spider = spider_class()

        with self._quiet_stdout():
            result = self._spider.init(extend)
            name = self._spider.getName()
        return {
            "result": self._json_value(result),
            "name": self._json_value(name),
        }

    def home(self, filter_value: bool = True) -> Any:
        return self._invoke("homeContent", filter_value)

    def category(
        self,
        tid: str,
        pg: str,
        filter_value: bool,
        extend: Any,
    ) -> Any:
        return self._invoke("categoryContent", tid, pg, filter_value, extend)

    def detail(self, ids: list[str]) -> Any:
        return self._invoke("detailContent", ids)

    def search(self, key: str, quick: bool, pg: str = "1") -> Any:
        return self._invoke("searchContent", key, quick, pg)

    def play(self, flag: str, play_id: str, vip_flags: Any) -> Any:
        return self._invoke("playerContent", flag, play_id, vip_flags)

    def action(self, action: str) -> Any:
        return self._invoke("action", action)

    def close(self) -> None:
        if self._spider is not None and hasattr(self._spider, "destroy"):
            try:
                with self._quiet_stdout():
                    self._spider.destroy()
            except Exception:
                pass
        self._spider = None
        self._module = None
        if self._js_process is not None:
            self._js_process.kill()
            self._js_process.wait(timeout=2)
            self._js_process = None
        self._temporary_directory.cleanup()

    def _invoke(self, method_name: str, *args: Any) -> Any:
        if self._js_process is not None:
            try:
                return self._json_value(
                    self._js_request(method_name, list(args))
                )
            except AtvpRuntimeError:
                raise
            except Exception as exc:
                raise AtvpRuntimeError(
                    f"JavaScript {method_name} failed: {type(exc).__name__}"
                ) from exc
        if self._spider is None:
            raise AtvpRuntimeError("Atvp runtime is not initialized")
        method = getattr(self._spider, method_name, None)
        if method is None:
            raise AtvpRuntimeError(f"Atvp method is unavailable: {method_name}")
        try:
            with self._quiet_stdout():
                return self._json_value(method(*args))
        except AtvpRuntimeError:
            raise
        except Exception as exc:
            raise AtvpRuntimeError(
                f"Atvp {method_name} failed: {type(exc).__name__}"
            ) from exc

    def _init_javascript(self, path: Path, extend: str) -> dict[str, Any]:
        node = shutil.which("node") or shutil.which("nodejs")
        if node is None:
            raise AtvpRuntimeError("JavaScript runtime is unavailable")
        bridge = Path(__file__).parent / "vendor" / "js_runtime.mjs"
        try:
            self._js_process = subprocess.Popen(
                [node, str(bridge), str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            result = self._js_request("init", [extend])
            name = self._js_request("getName", [])
        except Exception as exc:
            if self._js_process is not None:
                self._js_process.kill()
                self._js_process.wait(timeout=2)
                self._js_process = None
            raise AtvpRuntimeError(
                f"JavaScript plugin init failed: {type(exc).__name__}"
            ) from exc
        return {"result": self._json_value(result), "name": self._json_value(name)}

    def _js_request(self, method: str, args: list[Any]) -> Any:
        process = self._js_process
        if process is None or process.stdin is None or process.stdout is None:
            raise AtvpRuntimeError("JavaScript runtime is not initialized")
        request_id = uuid.uuid4().hex
        with self._js_lock:
            process.stdin.write(
                json.dumps(
                    {"id": request_id, "method": method, "args": args},
                    ensure_ascii=False,
                )
                + "\n"
            )
            process.stdin.flush()
            line = process.stdout.readline()
        if not line:
            raise AtvpRuntimeError("JavaScript runtime exited unexpectedly")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AtvpRuntimeError("JavaScript runtime returned invalid JSON") from exc
        if response.get("id") != request_id:
            raise AtvpRuntimeError("JavaScript runtime response id mismatch")
        if not response.get("ok"):
            error = response.get("error") or {}
            raise AtvpRuntimeError(
                str(error.get("message") or "JavaScript plugin rejected request")
            )
        return response.get("result")

    @staticmethod
    def _json_value(value: Any) -> Any:
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise AtvpRuntimeError("Atvp result is not JSON serializable") from exc
        return value

    @staticmethod
    def _decode_ext(extend: str) -> dict[str, Any]:
        raw = str(extend or "").strip()
        if not raw:
            return {}
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
            payload = json.loads(decoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AtvpRuntimeError("Atvp extend is not valid base64 JSON") from exc
        if not isinstance(payload, dict):
            raise AtvpRuntimeError("Atvp extend must be a JSON object")
        return payload

    def _prepare_loader(self, loader: str) -> Path:
        if not loader:
            return self._vendor_path
        if loader.startswith(("http://", "https://")):
            self._validate_remote_loader(loader)
            try:
                response = requests.get(loader, timeout=10, allow_redirects=False)
            except requests.RequestException as exc:
                raise AtvpRuntimeError("Atvp dynamic loader request failed") from exc
            if response.status_code != 200 or not response.text.strip():
                raise AtvpRuntimeError("Atvp dynamic loader returned no usable source")
            path = Path(self._temporary_directory.name) / f"loader-{uuid.uuid4().hex}.py"
            path.write_text(response.text, encoding="utf-8")
            return path

        path = Path(loader)
        if path.is_file():
            return path
        raise AtvpRuntimeError("Atvp loader file was not found")

    @staticmethod
    def _validate_remote_loader(loader: str) -> None:
        parsed = urlsplit(loader)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise AtvpRuntimeError("Atvp dynamic loader URL is invalid")
        if parsed.username or parsed.password:
            raise AtvpRuntimeError("Atvp dynamic loader URL contains credentials")
        normalized_path = parsed.path.lower()
        if normalized_path != "/atvp.py" and not (
            normalized_path.startswith("/atvp/") and normalized_path.endswith(".py")
        ):
            raise AtvpRuntimeError("Atvp dynamic loader path is invalid")

    def _load_module(self, path: Path) -> types.ModuleType:
        self._install_base_spider_module()
        base_spider = sys.modules["base.spider"]
        if (
            not hasattr(base_spider, "Spider")
            and path.resolve() != self._vendor_path.resolve()
        ):
            self._load_module(self._vendor_path)
        module_name = f"t4_atvp_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise AtvpRuntimeError("Atvp loader could not be imported")
        module = importlib.util.module_from_spec(spec)
        try:
            # Refresh must read current bytes, even for same-size edits in one second.
            with self._quiet_stdout():
                exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        except Exception as exc:
            raise AtvpRuntimeError(
                f"Atvp loader import failed: {type(exc).__name__}"
            ) from exc
        fallback = getattr(module, "_FallbackSpider", None)
        if fallback is not None:
            base_spider.Spider = fallback
        return module

    @staticmethod
    def _install_base_spider_module() -> None:
        base_module = types.ModuleType("base")
        base_spider = types.ModuleType("base.spider")
        base_module.__path__ = []
        sys.modules["base"] = base_module
        sys.modules["base.spider"] = base_spider

    @staticmethod
    @contextlib.contextmanager
    def _quiet_stdout():
        with contextlib.redirect_stdout(sys.stderr):
            yield
