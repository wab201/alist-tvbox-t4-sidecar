import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.rust.relay_support import Child, binary_path, python_app
from tests.rust.test_socket_lookup import tls_contexts
from tests.unit.test_media_disconnect import serving


@pytest.mark.skipif(not binary_path().is_file(), reason="release relay required")
@pytest.mark.parametrize("trusted", [False, True])
def test_tls_trust_and_range_delivery_match_python(tmp_path, monkeypatch, tls_contexts, trusted):
    if trusted and os.name == "nt":
        pytest.skip("Schannel uses OS certificate store; do not install test roots globally")
    server_tls, client_tls = tls_contexts

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["Range"] == "bytes=2-4"
            assert self.headers["Authorization"] == "Bearer fixture"
            self.send_response(206)
            self.send_header("Content-Length", "3")
            self.send_header("Content-Range", "bytes 2-4/8")
            self.end_headers()
            self.wfile.write(b"cde")

        def log_message(self, *_):
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin.socket = server_tls.wrap_socket(origin.socket, server_side=True)
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()
    manifest = tmp_path / "assets.json"
    manifest.write_text(json.dumps({"file": {
        "url": f"https://127.0.0.1:{origin.server_port}/file",
        "headers": {"Authorization": "Bearer fixture"},
    }}), encoding="utf-8")
    if trusted:
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "cert.pem"))

    def configure(bridge):
        bridge._local_media_client = httpx.AsyncClient(verify=client_tls if trusted else True,
                                                      trust_env=False, timeout=5)

    rust = None
    try:
        rust = Child([str(binary_path()), "--assets", str(manifest)], tmp_path, "tls-relay")
        with serving(python_app(manifest, configure_bridge=configure)) as python, \
                httpx.Client(trust_env=False, timeout=10) as client:
            for base in (python, rust.url):
                response = client.get(base + "/t4/media/file", headers={"Range": "bytes=2-4"})
                assert response.status_code == (206 if trusted else 502)
                if trusted:
                    assert response.content == b"cde"
                    assert response.headers["content-range"] == "bytes 2-4/8"
    finally:
        if rust is not None:
            rust.close()
        origin.shutdown()
        origin.server_close()
        thread.join(3)
