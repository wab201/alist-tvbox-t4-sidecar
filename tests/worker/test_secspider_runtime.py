import base64
import hashlib
import json
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC
from Crypto.Random import get_random_bytes
from Crypto.Signature import eddsa

from sidecar.worker.atvp_runtime import AtvpRuntime


VENDOR_ATVP = Path(__file__).parents[2] / "sidecar" / "worker" / "vendor" / "Atvp.py"


def xor_chunks(text: str, xor_key: int, chunk_size: int = 16) -> list[str]:
    raw = text.encode("utf-8")
    chunks = []
    for index in range(0, len(raw), chunk_size):
        chunk = bytes(value ^ xor_key for value in raw[index:index + chunk_size])
        chunks.append(base64.b64encode(chunk).decode("ascii"))
    return list(reversed(chunks))


def signing_bytes(headers: dict[str, str], payload_b64: str) -> bytes:
    lines = [
        f"//@name:{headers['name']}",
        f"//@version:{headers['version']}",
        f"//@remark:{headers['remark']}",
        f"//@format:{headers['format']}",
        f"//@alg:{headers['alg']}",
        f"//@wrap:{headers['wrap']}",
        f"//@sign:{headers['sign']}",
        f"//@kid:{headers['kid']}",
        f"//@nonce:{headers['nonce']}",
        f"//@ek:{headers['ek']}",
        f"//@hash:{headers['hash']}",
        f"payload.base64:{payload_b64}",
    ]
    return "\n".join(lines).encode("utf-8")


def build_secspider_fixture(
    tmp_path: Path,
    source: str | None = None,
) -> tuple[str, str, list[str], list[str]]:
    source = source if source is not None else """
from base.spider import Spider

class Spider(Spider):
    def init(self, extend=""):
        self.extend = extend
        return "encrypted-init-ok"

    def getName(self):
        return "Encrypted Demo"

    def playerContent(self, flag, id, vipFlags):
        return {"parse": 0, "url": "https://video.invalid/encrypted.m3u8"}
"""
    source_bytes = source.encode("utf-8")
    name = "Encrypted Demo"
    version = "7"
    remark = "test"
    kid = "self-kid"
    master_secret = "self-master-secret"

    key_pair = ECC.generate(curve="Ed25519")
    public_key_text = key_pair.public_key().export_key(format="PEM")
    if isinstance(public_key_text, bytes):
        public_key_text = public_key_text.decode("utf-8")
    content_key = get_random_bytes(32)
    payload_nonce = get_random_bytes(12)
    payload_cipher = AES.new(content_key, AES.MODE_GCM, nonce=payload_nonce)
    payload_data, payload_tag = payload_cipher.encrypt_and_digest(source_bytes)
    payload_b64 = base64.b64encode(payload_data + payload_tag).decode("ascii")

    wrap_key = HKDF(
        master_secret.encode("utf-8"),
        32,
        salt=kid.encode("utf-8"),
        hashmod=SHA256,
        context=f"secspider:{name}:{version}:wrap-key".encode("utf-8"),
    )
    wrap_nonce = HKDF(
        master_secret.encode("utf-8"),
        12,
        salt=kid.encode("utf-8"),
        hashmod=SHA256,
        context=f"secspider:{name}:{version}:wrap-nonce".encode("utf-8"),
    )
    wrap_cipher = AES.new(wrap_key, AES.MODE_GCM, nonce=wrap_nonce)
    wrapped_key, wrapped_tag = wrap_cipher.encrypt_and_digest(content_key)

    headers = {
        "name": name,
        "version": version,
        "remark": remark,
        "format": "secspider/1",
        "alg": "aes-256-gcm",
        "wrap": "hkdf-aes-keywrap",
        "sign": "ed25519",
        "kid": kid,
        "nonce": "base64:" + base64.b64encode(payload_nonce).decode("ascii"),
        "ek": "base64:" + base64.b64encode(wrapped_key + wrapped_tag).decode("ascii"),
        "hash": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
    }
    signature = eddsa.new(key_pair, "rfc8032").sign(signing_bytes(headers, payload_b64))
    package = "\n".join(
        [
            *[f"//@{key}:{value}" for key, value in headers.items()],
            "//@sig:base64:" + base64.b64encode(signature).decode("ascii"),
            "",
            "payload.base64:" + payload_b64,
            "",
        ]
    )
    package_path = tmp_path / "encrypted-demo.txt"
    package_path.write_text(package, encoding="utf-8")

    vendor_text = VENDOR_ATVP.read_text(encoding="utf-8")
    vendor_text = vendor_text.replace(
        "    _self_public_key_chunks = []",
        "    _self_public_key_chunks = "
        + repr(xor_chunks(public_key_text, 23)),
    )
    vendor_text = vendor_text.replace(
        "    _self_master_secret_chunks = []",
        "    _self_master_secret_chunks = "
        + repr(xor_chunks(master_secret, 41)),
    )
    loader_path = tmp_path / "Atvp-custom.py"
    loader_path.write_text(vendor_text, encoding="utf-8")
    return (
        str(package_path),
        str(loader_path),
        xor_chunks(public_key_text, 23),
        xor_chunks(master_secret, 41),
    )


def make_ext(package_path: str, loader_path: str) -> str:
    payload = {
        "source": package_path,
        "loader": loader_path,
        "raw": False,
        "secspider_loader": "self",
        "token": "-",
    }
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def test_custom_secspider_dynamic_loader_decrypts_and_executes(tmp_path) -> None:
    package_path, loader_path, _, _ = build_secspider_fixture(tmp_path)
    runtime = AtvpRuntime(str(VENDOR_ATVP))
    try:
        assert runtime.init(make_ext(package_path, loader_path)) == {
            "result": "encrypted-init-ok",
            "name": "Encrypted Demo",
        }
        assert runtime.play("Demo", "id", []) == {
            "parse": 0,
            "url": "https://video.invalid/encrypted.m3u8",
        }
    finally:
        runtime.close()


def test_custom_secspider_uses_read_only_keyring_fallback(tmp_path, monkeypatch) -> None:
    package_path, _, public_chunks, secret_chunks = build_secspider_fixture(tmp_path)
    keyring_path = tmp_path / "atvp-keyring.json"
    keyring_path.write_text(
        json.dumps(
            {
                "publicKeyChunks": public_chunks,
                "masterSecretChunks": secret_chunks,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ATV_SECSPIDER_KEYRING", str(keyring_path))

    runtime = AtvpRuntime(str(VENDOR_ATVP))
    try:
        assert runtime.init(make_ext(package_path, loader_path=""))["name"] == "Encrypted Demo"
    finally:
        runtime.close()


def test_custom_secspider_uses_http_dynamic_loader(tmp_path) -> None:
    package_path, loader_path, _, _ = build_secspider_fixture(tmp_path)
    package_file = Path(package_path)
    loader_file = Path(loader_path)
    root = tmp_path / "http-root"
    (root / "Atvp" / "token").mkdir(parents=True)
    (root / "plugins").mkdir(parents=True)
    (root / "Atvp" / "token" / "v1.py").write_text(
        loader_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "Atvp.py").write_text(
        loader_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "plugins" / "demo.txt").write_text(
        package_file.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

    handler = lambda *args, **kwargs: QuietHandler(
        *args,
        directory=str(root),
        **kwargs,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        for loader_url in (
            f"{base_url}/Atvp/token/v1.py",
            f"{base_url}/Atvp.py",
        ):
            payload = {
                "api": base_url,
                "source": f"{base_url}/plugins/demo.txt",
                "loader": loader_url,
                "raw": False,
                "secspider_loader": "self",
                "token": "-",
            }
            extend = base64.b64encode(
                json.dumps(payload, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")
            runtime = AtvpRuntime(str(VENDOR_ATVP))
            try:
                assert runtime.init(extend)["name"] == "Encrypted Demo"
            finally:
                runtime.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
