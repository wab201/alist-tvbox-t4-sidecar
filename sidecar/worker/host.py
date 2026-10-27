from __future__ import annotations

from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import sys
import threading
from typing import Any
from urllib.parse import parse_qsl, quote, urlsplit
from sidecar.plugin_proxy import is_proxy_text


HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


class PluginHost:
    def __init__(self, invoke, site_key: str) -> None:
        self.invoke = invoke
        self.site_key = site_key
        self.cache: OrderedDict[str, Any] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._server_lock = threading.Lock()

    @property
    def endpoint(self) -> str:
        with self._server_lock:
            if self._server is None:
                host = self

                class Handler(BaseHTTPRequestHandler):
                    protocol_version = "HTTP/1.1"

                    def do_GET(self):
                        host._serve(self)

                    def do_HEAD(self):
                        host._serve(self)

                    def log_message(self, *_):
                        pass

                self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                self._server.daemon_threads = True
                self._thread = threading.Thread(
                    target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
                )
                self._thread.start()
            return f"http://127.0.0.1:{self._server.server_port}/proxy"

    def bind_base(self, spider_class) -> None:
        host = self
        spider_class.siteKey = self.site_key
        spider_class.getProxyUrl = lambda instance, local=True: (
            host.endpoint + "?do=py&siteKey=" + quote(host.site_key, safe="")
        )
        spider_class.getCache = lambda instance, key: host.get_cache(key)
        spider_class.setCache = lambda instance, key, value: host.set_cache(key, value)
        spider_class.delCache = lambda instance, key: host.del_cache(key)

    def get_cache(self, key):
        with self._cache_lock:
            if key not in self.cache:
                return None
            self.cache.move_to_end(key)
            return self.cache[key]

    def set_cache(self, key, value):
        with self._cache_lock:
            self.cache[key] = value
            self.cache.move_to_end(key)
            while len(self.cache) > 512:
                self.cache.popitem(last=False)

    def del_cache(self, key):
        with self._cache_lock:
            self.cache.pop(key, None)

    def close(self):
        with self._server_lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
            self._thread.join(timeout=2)
        with self._cache_lock:
            self.cache.clear()

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        body = None
        sent = False
        try:
            parsed = urlsplit(handler.path)
            if parsed.path.rstrip("/") != "/proxy":
                raise ValueError("invalid proxy path")
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            request_headers = dict(handler.headers)
            params["headers"] = request_headers
            params["header"] = json.dumps(request_headers)
            for name in ("Range", "If-Range", "User-Agent", "Referer", "Origin", "Accept"):
                if handler.headers.get(name):
                    params[name] = params[name.lower()] = handler.headers[name]
            result = self.invoke(params)
            if not isinstance(result, (list, tuple)) or len(result) not in {3, 4}:
                raise ValueError("invalid localProxy response")
            status, mime, body = result[:3]
            if not isinstance(status, int) or status < 200 or status > 599:
                raise ValueError("invalid localProxy status")
            headers = dict(result[3]) if len(result) == 4 else {}
            headers = {str(key): str(value) for key, value in headers.items()
                       if str(key).lower() not in HOP_HEADERS}
            if any("\r" in key + value or "\n" in key + value for key, value in headers.items()):
                raise ValueError("invalid localProxy headers")
            if not isinstance(mime, str) or "\r" in mime or "\n" in mime:
                raise ValueError("invalid localProxy MIME")
            if isinstance(body, str):
                body = body.encode("utf-8")
            if isinstance(body, (bytes, bytearray)):
                body = bytes(body)
                headers = {key: value for key, value in headers.items() if key.lower() != "content-length"}
                if status == 200 and handler.headers.get("Range") and not is_proxy_text(mime):
                    start, end = self._range(handler.headers["Range"], len(body))
                    if start is None:
                        status = 416
                        headers["Content-Range"] = f"bytes */{len(body)}"
                        body = b""
                    else:
                        headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
                        headers["Accept-Ranges"] = "bytes"
                        body = body[start:end + 1]
                        status = 206
                headers["Content-Length"] = str(len(body))
            if not any(key.lower() == "content-type" for key in headers):
                headers["Content-Type"] = mime
            if body is None:
                raise ValueError("missing localProxy body")
            chunked = not isinstance(body, bytes) and not any(
                key.lower() == "content-length" for key in headers
            )
            handler.send_response(status)
            for name, value in headers.items():
                handler.send_header(name, value)
            handler.send_header("Connection", "close")
            if chunked:
                handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            sent = True
            handler.close_connection = True
            if handler.command == "HEAD":
                return
            if isinstance(body, bytes):
                handler.wfile.write(body)
            else:
                chunks = iter(lambda: body.read(65536), b"") if hasattr(body, "read") else iter(body)
                for chunk in chunks:
                    if not chunk:
                        continue
                    if chunked:
                        handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    else:
                        handler.wfile.write(chunk)
                if chunked:
                    handler.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            # Never echo plugin exceptions: they may include credentials or signed URLs.
            print(f"PluginProxyError: {type(exc).__name__}", file=sys.stderr)
            if sent:
                handler.close_connection = True
            else:
                handler.send_error(502, "Plugin proxy failed")
        finally:
            if hasattr(body, "close"):
                body.close()

    @staticmethod
    def _range(value: str, size: int):
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
        if not match or not size or not any(match.groups()):
            return None, None
        first, last = match.groups()
        if not first:
            length = int(last)
            return (max(0, size - length), size - 1) if length else (None, None)
        start, end = int(first), min(int(last), size - 1) if last else size - 1
        return (start, end) if start <= end < size else (None, None)
