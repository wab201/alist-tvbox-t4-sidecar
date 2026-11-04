"""Build the optional FM DEX JAR from source using an installed JDK and Android SDK."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def build(sdk: Path, java_home: Path | None, output: Path) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    java = str(java_home / "bin" / ("java" + suffix)) if java_home else shutil.which("java")
    javac = str(java_home / "bin" / ("javac" + suffix)) if java_home else shutil.which("javac")
    if not java or not javac:
        raise RuntimeError("JDK is required; set JAVA_HOME")
    platforms = sorted(
        (path for path in sdk.glob("platforms/android-*/android.jar")
         if path.parent.name.removeprefix("android-").isdigit()),
        key=lambda path: int(path.parent.name.removeprefix("android-")),
    )
    compilers = sorted(sdk.glob("build-tools/*/lib/d8.jar"))
    if not platforms or not compilers:
        raise RuntimeError("Android SDK platform and build-tools are required")
    with tempfile.TemporaryDirectory(prefix="t4-fm-build-") as temporary:
        work = Path(temporary)
        classes = work / "classes"
        classes.mkdir()
        stubs = work / "stubs"
        stubs.mkdir()
        subprocess.run([
            javac, "-encoding", "UTF-8", "--release", "8", "-g:none", "-Xlint:-options",
            "-d", str(stubs),
            *map(str, sorted((ROOT / "client/fm/compile-only").rglob("*.java"))),
        ], check=True)
        subprocess.run([
            javac, "-encoding", "UTF-8", "--release", "8", "-g:none", "-Xlint:-options",
            "-classpath", os.pathsep.join((str(platforms[-1]), str(stubs))),
            "-d", str(classes),
            *map(str, sorted((ROOT / "client/fm/src").rglob("*.java"))),
        ], check=True)
        intermediate = work / "classes.jar"
        with zipfile.ZipFile(intermediate, "w") as archive:
            for source in sorted(classes.rglob("*.class")):
                info = zipfile.ZipInfo(source.relative_to(classes).as_posix(), (2026, 1, 1, 0, 0, 0))
                archive.writestr(info, source.read_bytes())
        dex = work / "dex"
        dex.mkdir()
        subprocess.run([
            java, "-cp", str(compilers[-1]), "com.android.tools.r8.D8",
            "--release", "--min-api", "21", "--lib", str(platforms[-1]), "--lib", str(stubs),
            "--output", str(dex), str(intermediate),
        ], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("classes.dex", (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (dex / "classes.dex").read_bytes())
            info = zipfile.ZipInfo("t4-build.json", (2026, 1, 1, 0, 0, 0))
            manifest = {
                "protocol": 1, "version": "1", "min_api": 21,
                "sources": {
                    source.relative_to(ROOT).as_posix(): hashlib.sha256(source.read_bytes()).hexdigest()
                    for source in sorted((ROOT / "client/fm").rglob("*.java"))
                },
            }
            archive.writestr(info, json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=os.getenv("ANDROID_HOME") or os.getenv("ANDROID_SDK_ROOT"))
    parser.add_argument("--java-home", type=Path, default=os.getenv("JAVA_HOME"))
    parser.add_argument("--output", type=Path, default=ROOT / "sidecar/client_assets/fm-accelerator.jar")
    args = parser.parse_args()
    if args.sdk is None:
        parser.error("--sdk or ANDROID_HOME is required")
    print(build(args.sdk, args.java_home, args.output))


if __name__ == "__main__":
    main()
