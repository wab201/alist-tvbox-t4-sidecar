"""Read-only dependencies shared by Linux workers; never load a user plugin here."""
from __future__ import annotations

import gc
import importlib


gc.disable()
for name in (
    "sidecar.worker.atvp_runtime", "sidecar.worker.client", "httpx", "uvicorn", "fastapi",
    "Crypto.Cipher.AES", "Crypto.Hash.SHA256",
    "Crypto.Protocol.KDF", "Crypto.PublicKey.ECC", "Crypto.Signature.eddsa",
    "Cryptodome.Cipher.AES", "lxml.etree", "bs4", "pyquery", "m3u8",
):
    importlib.import_module(name)
# Keep the collector from dirtying inherited, otherwise read-only object pages.
gc.freeze()
