from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from importlib.resources import files
import io
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit
import zipfile

from sidecar.diagnostics import record
from sidecar.gateway.profiles import T4ProfileStore
from sidecar.playback import PlaybackBridge


PROTOCOL = 1
JAR_VERSION = "1"


@dataclass
class ClientSession:
    id: str
    challenge: str
    base: str
    profile: str
    created: float
    state: str = "pending"
    native_sites: tuple[tuple[str, str, dict], ...] = ()
    plans: OrderedDict[str, tuple[float, str, str, dict]] = field(default_factory=OrderedDict)


class ClientAcceleration:
    """Ephemeral per-subscription capabilities; never infer them from an IP address."""

    def __init__(
        self, profiles: T4ProfileStore, *, capacity: int = 256,
        ttl: float = 86400, pending_ttl: float = 90, clock=time.monotonic,
    ) -> None:
        self.profiles = profiles
        self.capacity = capacity
        self.ttl = ttl
        self.pending_ttl = pending_ttl
        self.clock = clock
        self.sessions: OrderedDict[str, ClientSession] = OrderedDict()
        self.lock = threading.RLock()
        self.jar = files("sidecar.client_assets").joinpath("fm-accelerator.jar")

    def prune(self) -> int:
        with self.lock:
            expired = [
                key for key, item in self.sessions.items()
                if self.clock() - item.created >= (
                    self.ttl if item.state == "ready" else self.pending_ttl
                )
            ]
            for key in expired:
                self.sessions.pop(key)
            return len(expired)

    def offer(
        self, base: str, profile: str, *,
        native_sites: tuple[tuple[str, str, dict], ...] = (),
    ) -> ClientSession | None:
        if not self.jar.is_file() and not native_sites:
            record("client.acceleration_skipped", reason="artifact_unavailable")
            return None
        with self.lock:
            self.prune()
            session = ClientSession(
                secrets.token_hex(16), secrets.token_hex(16),
                base.rstrip("/"), profile, self.clock(), native_sites=native_sites,
            )
            self.sessions[session.id] = session
            while len(self.sessions) > self.capacity:
                self.sessions.popitem(last=False)
        record("client.probe_offered", profile=profile)
        return session

    def component(self, identity: str) -> dict | None:
        with self.lock:
            session = self.get(identity)
            if session is None or session.state == "disabled":
                return None
            prefix = session.base + f"/t4/acceleration/{identity}"
            policy = self.profiles.get(session.profile)
            if policy is None:
                return None
            sites = [
                dict(site) for source, plugin, site in session.native_sites
                if policy.allows(source, plugin)
            ]
            result = {
                "protocol": PROTOCOL, "optional": True, "fallback": "t4",
                "sites": sites, "disable": prefix + "/disable",
            }
            if self.jar.is_file():
                jar = prefix + "/client.jar"
                result.update(spider=jar, ready=prefix + "/ready")
                sites.append({
                    "key": "t4_acceleration_" + identity,
                    "name": "下载增强", "api": "csp_T4Relay", "type": 3,
                    "jar": jar, "hide": 1, "indexs": 0,
                    "searchable": 0, "quickSearch": 0, "filterable": 0, "changeable": 0,
                })
            return result

    def get(self, identity: str) -> ClientSession | None:
        with self.lock:
            self.prune()
            session = self.sessions.get(identity)
            if session is None:
                return None
            profile = self.profiles.get(session.profile)
            return session if profile is not None and profile.enabled else None

    def artifact(self, identity: str) -> bytes | None:
        with self.lock:
            session = self.get(identity)
            if session is None or session.state == "disabled" or not self.jar.is_file():
                return None
            settings = {
                "session": session.id, "challenge": session.challenge,
                "base": session.base, "version": JAR_VERSION,
                "siteKey": "t4_acceleration_" + session.id,
            }
        output = io.BytesIO()
        with zipfile.ZipFile(self.jar) as original, zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED,
        ) as generated:
            if not original.read("classes.dex").startswith(b"dex\n"):
                raise ValueError("invalid component DEX")
            for info in original.infolist():
                generated.writestr(info, original.read(info.filename))
            # Properties.load(InputStream) expects Latin-1 with Unicode escapes.
            content = "\n".join(
                key + "=" + value.replace("\\", "\\\\").encode("unicode_escape").decode("ascii")
                for key, value in settings.items()
            )
            generated.writestr("t4-accelerator.properties", content + "\n")
        return output.getvalue()

    def confirm(self, identity: str, proof: str, protocol: int, version: str) -> bool:
        with self.lock:
            session = self.get(identity)
            if (
                session is None or protocol != PROTOCOL or version != JAR_VERSION
                or session.state == "disabled"
                or not secrets.compare_digest(session.challenge, proof)
            ):
                return False
            session.state = "ready"
        record("client.probe_ready", profile=session.profile)
        return True

    def disable(self, identity: str) -> None:
        with self.lock:
            session = self.sessions.get(identity)
            if session is not None:
                session.state = "disabled"
                session.plans.clear()
        record("client.probe_disabled")

    def wrap(
        self, identity: str, payload: dict, base: str, bridge: PlaybackBridge,
        *, profile: str, source: str, plugin: str,
    ) -> dict:
        with self.lock:
            session = self.get(identity)
            if (
                session is None or session.state != "ready"
                or session.profile != profile or session.base != base.rstrip("/")
            ):
                return payload
            policy = self.profiles.get(profile)
            if policy is None or not policy.allows(source, plugin):
                return payload
            url = payload.get("url")
            headers = payload.get("header") or {}
            if (
                not isinstance(url, str) or not bridge._direct_target(url)
                or urlsplit(url).path.lower().rsplit(".", 1)[-1] not in {
                    "mp4", "mkv", "webm", "mov", "avi",
                }
                or payload.get("parse", 0) not in {None, 0, "0"}
                or payload.get("jx", 0) not in {None, 0, "0"}
                or payload.get("playUrl") or not isinstance(headers, dict)
                or any(key.lower() in {"cookie", "authorization"} for key in headers)
                or any(marker in str(payload.get("format", "")).lower()
                       for marker in ("mpegurl", "dash+xml"))
            ):
                return payload
            relay = bridge.client_relay(url, headers, base)
            plan_id = secrets.token_hex(16)
            plan = {"direct": url, "relay": relay, "headers": dict(headers), "protocol": PROTOCOL}
            session.plans[plan_id] = (self.clock(), source, plugin, plan)
            while len(session.plans) > 8:
                session.plans.popitem(last=False)
            query = urlencode({
                "do": "t4accel", "session": identity,
                "siteKey": "t4_acceleration_" + identity,
                "plan": base.rstrip("/") + f"/t4/acceleration/{identity}/plans/{plan_id}",
            })
        record("client.acceleration_selected", reason="direct_with_block_repair")
        return {**payload, "url": "proxy://" + query, "header": {}}

    def plan(self, identity: str, plan_id: str) -> dict | None:
        with self.lock:
            session = self.get(identity)
            if session is None or session.state != "ready":
                return None
            item = session.plans.get(plan_id)
            if item is None:
                return None
            created, source, plugin, value = item
            policy = self.profiles.get(session.profile)
            if self.clock() - created >= 7200 or policy is None or not policy.allows(source, plugin):
                session.plans.pop(plan_id)
                return None
            return dict(value)

    def close(self) -> None:
        with self.lock:
            self.sessions.clear()

    def summary(self) -> dict[str, Any]:
        with self.lock:
            self.prune()
            counts = {key: 0 for key in ("pending", "ready", "disabled")}
            for session in self.sessions.values():
                counts[session.state] += 1
            return {"available": self.jar.is_file(), "protocol": PROTOCOL,
                    "version": JAR_VERSION, **counts}
