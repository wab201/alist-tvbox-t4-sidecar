import json
from pathlib import Path
import zipfile

from scripts import build_fm_accelerator as builder


def test_build_ignores_extension_and_preview_sdk_platforms(tmp_path, monkeypatch):
    sdk = tmp_path / "sdk"
    for platform in ("android-9", "android-35", "android-36", "android-36-ext19",
                     "android-Preview"):
        jar = sdk / "platforms" / platform / "android.jar"
        jar.parent.mkdir(parents=True)
        jar.touch()
    compiler = sdk / "build-tools" / "36.0.0" / "lib" / "d8.jar"
    compiler.parent.mkdir(parents=True)
    compiler.touch()
    calls = []

    def run(command, **kwargs):
        assert kwargs["check"] is True
        calls.append(command)
        if "com.android.tools.r8.D8" in command:
            output = Path(command[command.index("--output") + 1])
            (output / "classes.dex").write_bytes(b"dex\n035\0fixture")

    monkeypatch.setattr(builder.subprocess, "run", run)
    output = tmp_path / "helper.jar"
    digest = builder.build(sdk, tmp_path / "jdk", output)
    assert len(digest) == 64
    d8 = calls[-1]
    assert d8[d8.index("--lib") + 1] == str(sdk / "platforms/android-36/android.jar")
    with zipfile.ZipFile(output) as archive:
        assert archive.read("classes.dex") == b"dex\n035\0fixture"
        assert json.loads(archive.read("t4-build.json"))["min_api"] == 21
