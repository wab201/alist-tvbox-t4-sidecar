"""Build-time check for the supported server dependency set."""
from __future__ import annotations

import importlib
import json
import platform


REQUIRED_MODULES = (
    "fastapi", "uvicorn", "httpx", "requests", "Crypto.Cipher.AES",
    "Crypto.Signature.eddsa", "Cryptodome.Cipher.AES", "lxml.etree", "multipart",
    "m3u8", "bs4", "pyquery", "cryptography", "PIL.Image",
)


def main() -> int:
    missing = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    print(json.dumps({
        "python": platform.python_version(), "checked": len(REQUIRED_MODULES),
        "missing_modules": missing, "ready": not missing,
    }))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
