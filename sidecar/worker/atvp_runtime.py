from __future__ import annotations

import base64
import ast
import contextlib
from functools import wraps
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
from sidecar.worker.host import PluginHost


JSON_RESULT_METHODS = (
    "homeContent", "homeVideoContent", "categoryContent",
    "detailContent", "searchContent", "playerContent",
)


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
        self._modules: dict[str, types.ModuleType] = {}
        self._module_prefix = f"t4_{uuid.uuid4().hex}_"
        self._import_paths: list[str] = []
        self._category_filter_mode = "dict"
        self._base_modules: dict[str, types.ModuleType | None] | None = None
        self._host = PluginHost(self.local_proxy, "")

    def init(self, extend: str, *, site_key: str = "") -> dict[str, Any]:
        self._host.site_key = site_key
        payload = self._decode_ext(extend)
        source = str(payload.get("source") or "")
        common = Path(os.getenv("T4_PLUGIN_DIR", "/data/plugins")) / "common"
        if common.is_dir():
            self._add_import_path(common.resolve())
        if source and not source.startswith(("http://", "https://")) and Path(source).is_file():
            self._add_import_path(Path(source).resolve().parent)
        loader = str(payload.get("loader") or "").strip()
        module_path = self._prepare_loader(loader)
        if module_path.suffix.lower() == ".js":
            return self._init_javascript(module_path, extend)
        if module_path.resolve() != self._vendor_path.resolve():
            self._add_import_path(module_path.resolve().parent)
        module = self._load_module(module_path)
        self._module = module
        spider_class = getattr(module, "Spider", None)
        if spider_class is None:
            raise AtvpRuntimeError("Atvp loader has no Spider class")
        if hasattr(spider_class, "_instance"):
            spider_class._instance = None
        self._spider = spider_class()
        self._spider.siteKey = site_key

        with self._quiet_stdout():
            result = self._spider.init(extend)
            if payload.get("_t4_server_proxy") is True:
                self._configure_server_proxy()
            name = self._spider.getName()
        return {
            "result": self._json_value(result),
            "name": self._json_value(name),
            **({"proxy_url": self._host.endpoint} if site_key else {}),
        }

    def _configure_server_proxy(self) -> None:
        spider = self._spider
        if not hasattr(spider, "_load_inner_spider_class"):
            return
        endpoint = spider._build_backend_endpoint("play")
        fetch = spider.fetch

        def server_fetch(url, *args, **kwargs):
            if url == endpoint:
                kwargs["params"] = {**(kwargs.get("params") or {}), "type": ""}
            return fetch(url, *args, **kwargs)

        # Keep driver settings available to the inner spider and its filters.
        # Suppress only the ATVP device-proxy handoff, not plugin capabilities.
        spider.fetch = server_fetch
        if hasattr(spider, "_resolve_local_proxy_base"):
            spider._resolve_local_proxy_base = lambda: None

    def local_proxy(self, params: dict[str, Any]) -> Any:
        if self._spider is None:
            raise AtvpRuntimeError("Atvp runtime is not initialized")
        with self._quiet_stdout():
            return self._spider.localProxy(params)

    def home(self, filter_value: bool = True) -> Any:
        return self._invoke("homeContent", filter_value)

    def category(
        self,
        tid: str,
        pg: str,
        filter_value: bool,
        extend: Any,
    ) -> Any:
        if isinstance(extend, str):
            try:
                extend = json.loads(extend) if extend.strip() else {}
            except ValueError as exc:
                raise AtvpRuntimeError("Category filters must be a JSON object") from exc
        if extend is None:
            extend = {}
        if not isinstance(extend, dict):
            raise AtvpRuntimeError("Category filters must be an object")
        if self._js_process is None and self._category_filter_mode == "json":
            extend = json.dumps(extend, ensure_ascii=False)
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
        self._host.close()
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
        for name, module in self._modules.items():
            if sys.modules.get(name) is module:
                del sys.modules[name]
        self._modules.clear()
        for path in self._import_paths:
            if path in sys.path:
                sys.path.remove(path)
        self._import_paths.clear()
        if self._base_modules is not None:
            for name, previous in self._base_modules.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous
            self._base_modules = None

    def _invoke(self, method_name: str, *args: Any) -> Any:
        normalize = (
            self._plugin_result if method_name in JSON_RESULT_METHODS else self._json_value
        )
        if self._js_process is not None:
            try:
                return normalize(
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
                return normalize(method(*args))
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
    def _plugin_result(value: Any) -> Any:
        # TVBox spiders may return a JSON string instead of a Python object.
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        return AtvpRuntime._json_value(value)

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
        self._register_module(module)
        code = path.read_bytes()
        self._category_filter_mode = self._filter_mode(code)
        try:
            # Refresh must read current bytes, even for same-size edits in one second.
            with self._quiet_stdout():
                exec(compile(code, str(path), "exec"), module.__dict__)
        except Exception as exc:
            if sys.modules.get(module_name) is module:
                del sys.modules[module_name]
            self._modules.pop(module_name, None)
            raise AtvpRuntimeError(
                f"Atvp loader import failed: {type(exc).__name__}"
            ) from exc
        fallback = getattr(module, "_FallbackSpider", None)
        if fallback is not None:
            self._host.bind_base(fallback)
            base_spider.Spider = fallback
        # ATVP creates inner/filter modules through its own types global.
        # Register those modules without modifying the versioned vendor source.
        spider_class = getattr(module, "Spider", None)
        is_atvp_loader = spider_class is not None and hasattr(
            spider_class, "_load_inner_spider_class"
        )
        module_types = getattr(module, "types", None)
        if is_atvp_loader and module_types is types:
            host_types = types.ModuleType("t4_host_types")
            host_types.__dict__.update(vars(types))
            runtime = self

            class RegisteredModule(types.ModuleType):
                def __init__(self, name, doc=None):
                    super().__init__(runtime._module_prefix + name, doc)
                    runtime._register_module(self)

            host_types.ModuleType = RegisteredModule
            module.types = host_types
        if is_atvp_loader:
            original = spider_class._load_inner_spider_class

            def load_inner(instance, source_text):
                self._category_filter_mode = self._filter_mode(source_text)
                inner_class = original(instance, source_text)
                inner_class.siteKey = self._host.site_key
                # Decode before Atvp applies category wrapping, filters and play caches.
                def normalize_result(method):
                    @wraps(method)
                    def invoke(*args, **kwargs):
                        return self._plugin_result(method(*args, **kwargs))
                    return invoke

                for name in JSON_RESULT_METHODS:
                    method = getattr(inner_class, name, None)
                    if callable(method):
                        setattr(inner_class, name, normalize_result(method))
                return inner_class

            spider_class._load_inner_spider_class = load_inner
        return module

    def _install_base_spider_module(self) -> None:
        if self._base_modules is not None:
            return
        self._base_modules = {name: sys.modules.get(name) for name in ("base", "base.spider")}
        base_module = types.ModuleType("base")
        base_spider = types.ModuleType("base.spider")
        base_module.__path__ = []
        sys.modules["base"] = base_module
        sys.modules["base.spider"] = base_spider

    def _register_module(self, module: types.ModuleType) -> None:
        sys.modules[module.__name__] = module
        self._modules[module.__name__] = module

    def _add_import_path(self, path: Path) -> None:
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
            self._import_paths.append(value)

    @staticmethod
    def _filter_mode(source: str | bytes) -> str:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "categoryContent":
                continue
            if not node.args.args:
                continue
            parameter = node.args.args[-1].arg
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not call.args:
                    continue
                name = call.func.attr if isinstance(call.func, ast.Attribute) else (
                    call.func.id if isinstance(call.func, ast.Name) else ""
                )
                argument = call.args[0]
                direct_parameter = isinstance(argument, ast.Name) and argument.id == parameter
                defaulted_parameter = (
                    isinstance(argument, ast.BoolOp) and isinstance(argument.op, ast.Or)
                    and isinstance(argument.values[0], ast.Name)
                    and argument.values[0].id == parameter
                )
                if name == "loads" and (direct_parameter or defaulted_parameter):
                    return "json"
        return "dict"

    @staticmethod
    @contextlib.contextmanager
    def _quiet_stdout():
        with contextlib.redirect_stdout(sys.stderr):
            yield
