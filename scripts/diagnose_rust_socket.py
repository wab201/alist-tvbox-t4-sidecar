"""Opt-in gateway socket lookup experiment; no dependency or production patches."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import inspect

import httpcore
from anyio._backends._asyncio import SocketStream
from httpcore._backends.anyio import AnyIOBackend, AnyIOStream
from httpcore._utils import is_socket_readable


class SocketLookupStream(AnyIOStream):
    def __init__(self, stream, counters):
        super().__init__(stream)
        self._lookup_counters = counters

    def get_extra_info(self, info):
        if info in {"socket", "is_readable"} and type(self._stream) is SocketStream:
            # Read the current socket each time, never a cached/recyclable fd.
            sock = self._stream._raw_socket
            self._lookup_counters["socket_fastpath_calls"] += 1
            return sock if info == "socket" else is_socket_readable(sock)
        return super().get_extra_info(info)


class SocketLookupBackend(AnyIOBackend):
    def __init__(self, counters):
        self.counters = counters

    async def connect_tcp(self, *args, **kwargs):
        stream = await super().connect_tcp(*args, **kwargs)
        return SocketLookupStream(stream._stream, self.counters)


def install_socket_lookup(pools, counters):
    """Only fresh, owned direct HTTP/1 pools. TLS upgrade stays upstream-owned."""
    pools = list(pools)
    if httpcore.__version__ != "1.0.9" or version("anyio") not in {"4.14.1", "4.16.0"}:
        raise RuntimeError("socket experiment requires verified dependency versions")
    if not pools or len({id(pool) for pool in pools}) != len(pools):
        raise ValueError("unique explicit pools required")
    for pool in pools:
        if (type(pool) is not httpcore.AsyncConnectionPool or pool.connections or pool._requests
                or pool._http2 or not pool._http1 or pool._proxy is not None):
            raise RuntimeError("socket experiment requires fresh direct HTTP/1 pools")
    identity = {"httpcore": httpcore.__version__, "anyio": version("anyio"),
        "scope": "plain-asyncio-socket-only; TLS, unknown streams and other attributes unchanged",
        "sources": {fn.__qualname__: hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
                    for fn in (AnyIOStream.get_extra_info, SocketStream._raw_socket.fget,
                               SocketLookupStream.get_extra_info)}}
    counters["socket_fastpath_calls"] = 0
    for pool in pools:
        pool._network_backend = SocketLookupBackend(counters)
    return identity
