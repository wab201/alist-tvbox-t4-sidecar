import gzip

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from fastapi.testclient import TestClient
import httpx
import m3u8
import pytest

from sidecar.compatibility.probes import ResourceProbes
from sidecar.playback import PlaybackBridge
from tests.unit.test_playback_diagnostics import FakeWorker, application


TS = (b"\x47" + bytes(187)) * 9


@pytest.mark.parametrize("explicit_iv", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
def test_real_http_delivery_preserves_aes_key_iv_and_encrypted_bytes(tmp_path, explicit_iv, compressed):
    key, sequence = bytes(range(16)), 987
    iv = (0x123 if explicit_iv else sequence).to_bytes(16, "big")
    encrypted = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(TS, 16))
    manifest = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:987\n"
        "#EXT-X-TARGETDURATION:5\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="key"'
        + (",IV=0x123" if explicit_iv else "")
        + "\n#EXTINF:5,\none.ts\n#EXT-X-ENDLIST\n"
    ).encode()

    class Worker(FakeWorker):
        def call(self, runtime_key, plugin, method, params):
            if method == "play":
                return {
                    "parse": 0, "jx": 0, "url": "https://cdn.example/index.m3u8",
                    "format": "application/x-mpegURL",
                    "header": {"User-Agent": "fixture-agent", "Referer": "https://allowed.example"},
                }
            return super().call(runtime_key, plugin, method, params)

    def respond(request):
        assert request.headers["user-agent"] == "fixture-agent"
        assert request.headers["referer"] == "https://allowed.example"
        if request.url.path == "/index.m3u8":
            return httpx.Response(
                200, content=gzip.compress(manifest) if compressed else manifest,
                headers={"Content-Encoding": "gzip"} if compressed else {},
            )
        data = key if request.url.path == "/key" else encrypted
        return httpx.Response(200, stream=httpx.ByteStream(data))

    app = application(tmp_path, Worker())
    app.state.playback.client.close()
    app.state.playback.client = httpx.Client(transport=httpx.MockTransport(respond))
    app.state.playback.media_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with TestClient(app) as client:
        player = client.get("/t4/fm/source-a/demo/", params={"play": "raw"}).json()
        assert player["header"] == {}
        assert player["format"] == "application/x-mpegURL"
        response = client.get(player["url"])
        assert response.status_code == 200
        assert response.headers["content-type"].split(";")[0] == "application/vnd.apple.mpegurl"
        assert "content-encoding" not in response.headers
        assert int(response.headers["content-length"]) == len(response.content)
        playlist = m3u8.loads(response.text)
        assert playlist.media_sequence == sequence
        segment = playlist.segments[0]
        delivered_key = client.get(segment.key.uri).content
        delivered_segment = client.get(segment.uri).content
        delivered_iv = (
            int(segment.key.iv, 16) if segment.key.iv else playlist.media_sequence
        ).to_bytes(16, "big")
        assert delivered_key == key
        assert delivered_iv == iv
        assert delivered_segment == encrypted
        assert unpad(AES.new(delivered_key, AES.MODE_CBC, delivered_iv).decrypt(delivered_segment), 16) == TS


@pytest.mark.parametrize("explicit_iv", [False, True])
@pytest.mark.parametrize("rotate", [False, True])
def test_aes128_hls_decrypts_two_real_segments_and_caches_keys(explicit_iv, rotate):
    keys = {"/key1": bytes(range(16)), "/key2": bytes(reversed(range(16)))}
    seen = []

    def respond(request):
        seen.append(request.url.path)
        assert request.headers["referer"] == "https://allowed.example"
        if request.url.path == "/master":
            return httpx.Response(200, content=b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nmedia.m3u8\n")
        if request.url.path == "/media.m3u8":
            iv = ",IV=0x123" if explicit_iv else ""
            key1 = '#EXT-X-KEY:METHOD=AES-128,URI="key1"' + iv + "\n"
            key2 = '#EXT-X-KEY:METHOD=AES-128,URI="key2"' + iv + "\n" if rotate else ""
            return httpx.Response(200, content=(
                "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:987\n#EXT-X-TARGETDURATION:5\n" +
                key1 + "#EXTINF:5,\none.ts\n" + key2 + "#EXTINF:5,\ntwo.ts\n#EXT-X-ENDLIST\n"
            ).encode())
        if request.url.path in keys:
            return httpx.Response(200, content=keys[request.url.path])
        sequence = 987 if request.url.path == "/one.ts" else 988
        iv = (0x123 if explicit_iv else sequence).to_bytes(16, "big")
        key = keys["/key2" if rotate and request.url.path == "/two.ts" else "/key1"]
        return httpx.Response(200, content=AES.new(key, AES.MODE_CBC, iv).encrypt(pad(TS, 16)))

    bridge = PlaybackBridge()
    probes = ResourceProbes(bridge, httpx.MockTransport(respond))
    try:
        result = probes.media({"url": "https://video.example/master",
                               "header": {"Referer": "https://allowed.example"}})
        assert result["status"] == "pass", result
        assert len(result["segments"]) == 2
        assert all(s["encryption"] == "AES-128" and s["decrypted_prefix"] for s in result["segments"])
        assert seen.count("/key1") == 1
        assert seen.count("/key2") == int(rotate)
        assert result["continuous_play_verified"] is result["real_device_verified"] is False
    finally:
        probes.close()
        bridge.close()


@pytest.mark.parametrize("change,reason,status", [
    ("short_key", "invalid_hls_key_length", "fail"),
    ("wrong_key", "invalid_segment_bytes", "fail"),
    ("bad_iv", "invalid_hls_iv", "fail"),
    ("sample_aes", "protected_hls_not_probed", "blocked"),
    ("keyformat", "protected_hls_not_probed", "blocked"),
    ("key403", "http_status", "fail"),
])
def test_encryption_failures_cannot_manufacture_pass(change, reason, status):
    def respond(request):
        if request.url.path == "/media":
            method = "SAMPLE-AES" if change == "sample_aes" else "AES-128"
            extra = ',IV=0xz' if change == "bad_iv" else (
                ',KEYFORMAT="com.apple.streamingkeydelivery"' if change == "keyformat" else ""
            )
            return httpx.Response(200, content=(
                '#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-KEY:METHOD=' + method +
                ',URI="key"' + extra + '\n#EXTINF:5,\nsegment\n#EXT-X-ENDLIST\n'
            ).encode())
        if request.url.path == "/key":
            return httpx.Response(403 if change == "key403" else 200, content=(
                b"x" if change == "short_key" else b"x" * 16 if change == "wrong_key" else bytes(16)
            ))
        return httpx.Response(200, content=AES.new(bytes(16), AES.MODE_CBC, bytes(16)).encrypt(pad(TS, 16)))

    bridge = PlaybackBridge()
    probes = ResourceProbes(bridge, httpx.MockTransport(respond))
    try:
        result = probes.media({"url": "https://video.example/media"})
        assert result["status"] == status and result["reason"] == reason
        assert result["real_device_verified"] is False
    finally:
        probes.close()
        bridge.close()


@pytest.mark.parametrize("bad_audio", [False, True])
def test_hls_external_audio_is_probed_and_requires_audio_bytes(bad_audio):
    ts = (b"\x47" + bytes(187)) * 4
    aac = (b"\xff\xf1\x50\x80\x01\x1f\xfc" + bytes(1)) * 4

    def respond(request):
        if request.url.path == "/master":
            return httpx.Response(200, content=(
                '#EXTM3U\n'
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",'
                'DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=1,AUDIO="audio"\n'
                'video.m3u8\n'
            ).encode())
        if request.url.path == "/video.m3u8":
            return httpx.Response(200, content=b"#EXTM3U\n#EXTINF:5,\none.ts\n#EXTINF:5,\ntwo.ts\n")
        if request.url.path == "/audio.m3u8":
            return httpx.Response(200, content=b"#EXTM3U\n#EXTINF:5,\none.aac\n#EXTINF:5,\ntwo.aac\n")
        if request.url.path.endswith(".aac"):
            return httpx.Response(200, content=b"<html>denied</html>" if bad_audio else aac)
        return httpx.Response(200, content=ts)

    bridge = PlaybackBridge()
    probes = ResourceProbes(bridge, httpx.MockTransport(respond))
    try:
        result = probes.media({"url": "https://video.example/master"})
        assert result["status"] == ("fail" if bad_audio else "pass"), result
        if bad_audio:
            assert result["reason"] == "invalid_audio_segment_bytes"
        else:
            assert result["audio"]["selection"] == "default"
            assert all(item["format"] == "aac" for item in result["audio"]["segments"])
    finally:
        probes.close()
        bridge.close()


def test_hls_id3_timed_metadata_prefix_is_structurally_validated():
    metadata = b"ID3\x04\x00\x00\x00\x00\x00\x05hello"
    ts = (b"\x47" + bytes(187)) * 4

    def respond(request):
        if request.url.path == "/media":
            return httpx.Response(200, content=b"#EXTM3U\n#EXTINF:5,\nsegment.ts\n")
        return httpx.Response(200, content=metadata + ts)

    bridge = PlaybackBridge()
    probes = ResourceProbes(bridge, httpx.MockTransport(respond))
    try:
        result = probes.media({"url": "https://video.example/media"})
        assert result["status"] == "pass", result
        assert result["segments"][0]["format"] == "mpegts"
    finally:
        probes.close()
        bridge.close()


def test_hls_invalid_id3_prefix_is_not_stripped_into_a_false_pass():
    def respond(request):
        if request.url.path == "/media":
            return httpx.Response(200, content=b"#EXTM3U\n#EXTINF:5,\nsegment.ts\n")
        return httpx.Response(200, content=b"ID3\x04\x00\x00\x00\x00\x00\x05hello" + b"x" * 600)

    bridge = PlaybackBridge()
    probes = ResourceProbes(bridge, httpx.MockTransport(respond))
    try:
        result = probes.media({"url": "https://video.example/media"})
        assert result["status"] == "fail"
        assert result["reason"] == "invalid_segment_bytes"
    finally:
        probes.close()
        bridge.close()
