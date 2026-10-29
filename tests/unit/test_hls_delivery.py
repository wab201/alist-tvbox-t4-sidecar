import re
import struct
import zlib

from fastapi.testclient import TestClient
import httpx
import m3u8
import pytest

from sidecar.compatibility.probes import ResourceProbes
from sidecar.hls import HlsError, png_ts_offset
from sidecar.playback import PlaybackBridge
from tests.unit.test_playback_diagnostics import FakeWorker, application


def packet(pid):
    return bytes((0x47, (pid >> 8) & 31, pid & 255, 0x10)) + bytes(184)


TS = b"".join(packet(pid) for pid in (17, 0, 4096, *([256] * 77)))


def png_chunk(kind, content):
    body = kind + content
    return len(content).to_bytes(4, "big") + body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")


PREFIX = (
    b"\x89PNG\r\n\x1a\n"
    + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0))
    + png_chunk(b"tEXt", b"x" * 25) + png_chunk(b"PLTE", bytes(6))
    + png_chunk(b"tRNS", b"x") + struct.pack(">I", 646) + b"IDAT" + bytes(17)
)
WRAPPED = PREFIX + TS


class HlsWorker(FakeWorker):
    def call(self, runtime_key, plugin, method, params):
        if method == "play":
            return {"url": "https://cdn.example/master.m3u8?signature=private",
                    "header": {"Cookie": "private-cookie", "Referer": "https://allowed.example"},
                    "parse": 0}
        return super().call(runtime_key, plugin, method, params)


def upstream(seen, *, wrapped=True, encrypted=False, byterange=False):
    def respond(request):
        seen.append(request)
        assert request.headers["cookie"] == "private-cookie"
        assert request.headers["referer"] == "https://allowed.example"
        if request.url.path == "/master.m3u8":
            return httpx.Response(200, content=b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nmedia.m3u8\n")
        if request.url.path == "/media.m3u8":
            key = '#EXT-X-KEY:METHOD=AES-128,URI="key"\n' if encrypted else ""
            ranges = "#EXT-X-BYTERANGE:1880@0\n" if byterange else ""
            return httpx.Response(200, content=(
                "#EXTM3U\n#EXT-X-TARGETDURATION:5\n" + key
                + "#EXTINF:5,\n" + ranges + "one.png\n#EXTINF:5,\n"
                + ("#EXT-X-BYTERANGE:1880\n" if byterange else "")
                + "one.png\n#EXT-X-ENDLIST\n"
            ).encode())
        data = bytes(range(16)) if request.url.path == "/key" else WRAPPED if wrapped else TS
        range_value = request.headers.get("range", "")
        if range_value:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_value)
            if match[1]:
                start, end = int(match[1]), int(match[2]) if match[2] else len(data) - 1
            else:
                start, end = len(data) - int(match[2]), len(data) - 1
            end = min(end, len(data) - 1)
            headers = {"Content-Range": f"bytes {start}-{end}/{len(data)}"}
            data, code = data[start:end + 1], 206
        else:
            headers, code = {}, 200
        headers.update({"Content-Type": "image/png", "Content-Length": str(len(data)), "Accept-Ranges": "bytes"})
        return httpx.Response(code, headers=headers, stream=httpx.ByteStream(
            b"" if request.method == "HEAD" else data,
        ))
    return respond


def setup_client(tmp_path, seen, **options):
    app = application(tmp_path, HlsWorker())
    app.state.playback.client.close()
    app.state.playback.client = httpx.Client(transport=httpx.MockTransport(upstream(seen, **options)))
    app.state.playback.media_client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream(seen, **options))
    )
    return app


def media_url(client):
    payload = client.get("/t4/fm/source-a/demo/", params={"play": "raw"}).json()
    assert payload["header"] == {} and payload["parse"] == payload["jx"] == 0
    master = client.get(payload["url"])
    assert master.status_code == 200
    assert "cdn.example" not in master.text and "private" not in master.text
    variant = m3u8.loads(master.text).playlists[0].uri
    listing = client.get(variant)
    assert listing.status_code == 200
    return m3u8.loads(listing.text).segments[0].uri, payload, listing


def test_hls_http_delivery_unwraps_real_response_bytes_not_only_probe(tmp_path):
    seen = []
    app = setup_client(tmp_path, seen)
    with TestClient(app, base_url="https://public.example:9443") as client:
        target, payload, listing = media_url(client)
        assert all(segment.uri == target for segment in m3u8.loads(listing.text).segments)
        response = client.get(target)
        assert response.status_code == 200 and response.content == TS
        assert response.headers["content-type"] == "video/mp2t"
        assert int(response.headers["content-length"]) == len(TS)
        assert client.head(target).headers["content-length"] == str(len(TS))
        assert client.head(target).content == b""
        exported = client.get("/t4/api/diagnostics/export").text
        assert "segment_unwrapped" in exported
        assert all(secret not in exported for secret in ("private-cookie", "private", "cdn.example"))
    assert len([r for r in seen if r.url.path == "/one.png" and r.method == "GET"]) == 1


@pytest.mark.parametrize("range_value,expected", [
    ("bytes=0-99", TS[:100]), ("bytes=188-375", TS[188:376]),
    ("bytes=188-", TS[188:]), ("bytes=-100", TS[-100:]),
], ids=["start", "middle", "open", "suffix"])
def test_wrapped_segment_range_offsets_and_head_are_consistent(tmp_path, range_value, expected):
    seen = []
    app = setup_client(tmp_path, seen)
    with TestClient(app) as client:
        target, _, _ = media_url(client)
        response = client.get(target, headers={"Range": range_value})
        assert response.status_code == 206 and response.content == expected
        assert response.headers["content-range"].endswith("/" + str(len(TS)))
        assert int(response.headers["content-length"]) == len(expected)
        head = client.head(target, headers={"Range": range_value})
        assert head.status_code == 206 and head.headers["content-range"] == response.headers["content-range"]
        assert head.headers["content-length"] == response.headers["content-length"]
        assert head.content == b""
        assert client.get(target, headers={"Range": "bytes=0-99,200-299"}).status_code == 502
    assert len([r for r in seen if r.headers.get("range") == "bytes=0-16383"]) == 1


def test_wrapped_segment_oversized_suffix_and_unsatisfiable_ranges(tmp_path):
    seen = []
    with TestClient(setup_client(tmp_path, seen)) as client:
        target, _, _ = media_url(client)
        response = client.get(target, headers={"Range": "bytes=-999999"})
        assert response.status_code == 206 and response.content == TS
        assert response.headers["content-range"] == f"bytes 0-{len(TS)-1}/{len(TS)}"
        for value in (f"bytes={len(TS)}-", "bytes=-0", "bytes=10-1"):
            response = client.get(target, headers={"Range": value})
            assert response.status_code == 416
            assert response.headers["content-range"] == f"bytes */{len(TS)}"
            assert response.content == b""


def test_hls_gate_consumes_same_delivery_transform_and_keeps_device_boundary(tmp_path):
    seen = []
    app = setup_client(tmp_path, seen)
    with TestClient(app) as client:
        _, payload, _ = media_url(client)
        probes = ResourceProbes(app.state.playback)
        try:
            result = probes.media(payload)
            assert result["status"] == "pass", result
            assert all(item["format"] == "mpegts" for item in result["segments"])
            assert result["real_device_verified"] is result["continuous_play_verified"] is False
        finally:
            probes.close()


@pytest.mark.parametrize("encrypted,byterange", [(True, False), (False, True)])
def test_encrypted_or_byte_range_segments_are_not_stripped(tmp_path, encrypted, byterange):
    seen = []
    app = setup_client(tmp_path, seen, encrypted=encrypted, byterange=byterange)
    with TestClient(app) as client:
        target, _, listing = media_url(client)
        assert client.get(target).content == WRAPPED
        playlist = m3u8.loads(listing.text)
        if encrypted:
            assert "cdn.example" not in listing.text
            assert client.get(playlist.keys[0].uri).content == bytes(range(16))
        else:
            assert [s.byterange for s in playlist.segments] == ["1880@0", "1880"]


@pytest.mark.parametrize("bad", [
    PREFIX + bytes(2000), b"<html>denied</html>" + TS,
    b"x" + PREFIX + TS, PREFIX[:-1] + TS[:188 * 3],
    b"\x89PNG\r\n\x1a\n" + png_chunk(b"IEND", b"") + TS,
], ids=["bad-ts", "html", "arbitrary", "truncated", "iend"])
def test_only_structural_png_wrappers_are_recognized(bad):
    if bad.startswith(b"\x89PNG\r\n\x1a\n"):
        with pytest.raises(HlsError):
            png_ts_offset(bad)
    else:
        assert png_ts_offset(bad) == 0
    assert png_ts_offset(WRAPPED) == len(PREFIX) == 126


def test_hls_many_segments_keep_root_and_early_segments_alive():
    seen = []
    manifest = "#EXTM3U\n" + "".join(f"#EXTINF:5,\n{i}.ts\n" for i in range(900)) + "#EXT-X-ENDLIST\n"
    bridge = PlaybackBridge(capacity=1000, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=manifest.encode())
    ))
    try:
        payload = bridge.prepare({"url": "https://cdn.example/all.m3u8"}, "https://public.example")
        key = payload["url"].rsplit("/", 1)[1]
        asset = bridge.get(key)
        data = bridge.render_hls(key, asset, "https://public.example")
        playlist = m3u8.loads(data.decode())
        assert len(playlist.segments) == 900
        first = playlist.segments[0].uri.rsplit("/", 1)[-1]
        assert bridge.get(first) is not None and bridge.get(key) is asset
        assert bridge.render_hls(key, asset, "https://public.example") == data
        assert len(asset.children) == 900 and bridge._size == 901
    finally:
        bridge.close()


def test_hls_live_reload_is_not_stale_and_new_uris_remain_private():
    generation = [0]
    def respond(request):
        generation[0] += 1
        return httpx.Response(200, content=(
            f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:{generation[0]}\n"
            f"#EXTINF:2,\n{generation[0]}.ts?sign=secret\n"
        ).encode())
    bridge = PlaybackBridge(transport=httpx.MockTransport(respond))
    try:
        payload = bridge.prepare({"url": "https://cdn.example/live.m3u8"}, "https://public.example")
        key = payload["url"].rsplit("/", 1)[1]
        first = bridge.render_hls(key, bridge.get(key), "https://public.example")
        second = bridge.render_hls(key, bridge.get(key), "https://public.example")
        assert first != second and b"secret" not in first + second
    finally:
        bridge.close()


def test_hls_shared_init_map_external_audio_and_keys_are_rewritten():
    from sidecar.hls import rewrite_manifest
    mapping = {}
    def register(url, kind):
        mapping.setdefault((url, kind), f"https://public.example/{len(mapping)}")
        return mapping[url, kind]
    media = rewrite_manifest((
        '#EXTM3U\n#EXT-X-MAP:URI="init.mp4",BYTERANGE="100@0"\n'
        '#EXT-X-KEY:METHOD=AES-128,URI="key"\n'
        '#EXTINF:5,\n1.m4s\n#EXTINF:5,\n2.m4s\n#EXT-X-ENDLIST\n'
    ).encode(), "https://cdn.example/media.m3u8", register)
    listing = m3u8.loads(media.decode())
    assert all(s.init_section.uri.startswith("https://public.example/") for s in listing.segments)
    assert all(s.key.uri.startswith("https://public.example/") for s in listing.segments)
    assert ("https://cdn.example/init.mp4", "binary") in mapping
    assert ("https://cdn.example/1.m4s", "binary") in mapping
    assert b"cdn.example" not in media
    master = rewrite_manifest((
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="main",DEFAULT=YES,URI="audio.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="a"\nvideo.m3u8\n'
    ).encode(), "https://cdn.example/master.m3u8", register)
    assert b"cdn.example" not in master
    assert ("https://cdn.example/audio.m3u8", "hls") in mapping


def test_hls_resource_budget_evicts_an_entire_session_not_partial_segments():
    bridge = PlaybackBridge(capacity=4)
    try:
        first = bridge.prepare({"url": "https://cdn.example/one.m3u8"}, "https://public.example")
        parent = first["url"].rsplit("/", 1)[-1]
        child = bridge._hls_child(parent, "https://cdn.example/one.ts", "segment", "https://public.example")
        bridge.prepare({"url": "https://cdn.example/two.m3u8"}, "https://public.example")
        bridge.prepare({"url": "https://cdn.example/three.m3u8"}, "https://public.example")
        bridge.prepare({"url": "https://cdn.example/four.m3u8"}, "https://public.example")
        assert bridge.get(parent) is None and bridge.get(child.rsplit("/", 1)[-1]) is None
        assert bridge._size == 3
    finally:
        bridge.close()


@pytest.mark.parametrize("suffix", ["aac", "m4s", "vtt"])
def test_hls_preserves_audio_fmp4_and_subtitle_file_type_hints(suffix):
    bridge = PlaybackBridge()
    try:
        result = bridge.prepare({"url": "https://cdn.example/master.m3u8"}, "https://public.example")
        parent = result["url"].rsplit("/", 1)[-1]
        target = bridge._hls_child(parent, f"https://cdn.example/one.{suffix}", "segment",
                                   "https://public.example")
        assert target.endswith("." + suffix)
    finally:
        bridge.close()
