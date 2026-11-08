"""Isolated HTTP/1.1 reservation experiment; never imported by the application."""
from __future__ import annotations

import httpcore


class ReservedHTTP1Pool(httpcore.AsyncConnectionPool):
    reserve_http1 = False

    def _assign_requests_to_connections(self):
        if not self.reserve_http1:
            return super()._assign_requests_to_connections()
        assert self._http1 and not self._http2 and self._proxy is None
        # Assignment precedes HTTP/1.1's asynchronous state lock. Reserve across
        # invocations, not just this pass, until the existing pool request closes.
        reserved = {r.connection for r in self._requests if not r.is_queued()}
        closing = []
        for connection in list(self._connections):
            if connection in reserved:
                continue
            if connection.is_closed() or connection.has_expired():
                self._connections.remove(connection)
                closing.append(connection)
        idle = [c for c in self._connections if c not in reserved and c.is_idle()]
        for connection in idle[:max(0, len(idle) - self._max_keepalive_connections)]:
            self._connections.remove(connection)
            closing.append(connection)
        for request in self._requests:
            if not request.is_queued():
                continue
            origin = request.request.url.origin
            connection = next((c for c in self._connections if c not in reserved
                               and c.can_handle_request(origin) and c.is_available()), None)
            if connection is None and len(self._connections) >= self._max_connections:
                victim = next((c for c in self._connections
                               if c not in reserved and c.is_idle()), None)
                if victim is not None:
                    self._connections.remove(victim)
                    closing.append(victim)
            if connection is None and len(self._connections) < self._max_connections:
                connection = self.create_connection(origin)
                self._connections.append(connection)
            if connection is not None:
                reserved.add(connection)
                request.assign_to_connection(connection)
        return closing


def install_reservation(app):
    """Replace only fresh, direct HTTP/1 media pools, retaining their settings."""
    assert httpcore.__version__ == "1.0.9", "revalidate experiment for this dependency"
    replacements = []
    for client in (app.state.playback.media_client, app.state.playback._local_media_client):
        assert not client._mounts, "proxy/mount routing is outside this experiment"
        transport = client._transport
        old = transport._pool
        assert type(old) is httpcore.AsyncConnectionPool
        assert not old.connections and not old._requests and old._proxy is None
        assert old._http1 and not old._http2
        options = {key: getattr(old, "_" + key) for key in (
            "ssl_context", "max_connections", "max_keepalive_connections", "keepalive_expiry",
            "http1", "http2", "retries", "local_address", "uds", "network_backend", "socket_options",
        )}
        replacements.append((transport, ReservedHTTP1Pool(**options)))
    for transport, pool in replacements:
        transport._pool = pool
    return [pool for _, pool in replacements]


def set_reservation(pools, enabled):
    assert all(not pool._requests for pool in pools), "switch only after all streams close"
    for pool in pools:
        pool.reserve_http1 = enabled
