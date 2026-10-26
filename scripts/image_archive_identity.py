from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path


def image_id(archive: str | Path, *, revision: str, tag: str) -> str:
    # Docker storage backends expose different inspect IDs; archive config is portable.
    with tarfile.open(archive, "r:gz") as bundle:
        manifest = json.load(bundle.extractfile("manifest.json"))
        matches = [entry for entry in manifest if tag in (entry.get("RepoTags") or [])]
        if len(matches) != 1:
            raise ValueError("archive must contain exactly one matching image")
        config = bundle.extractfile(matches[0]["Config"]).read()
    metadata = json.loads(config)
    labels = metadata.get("config", {}).get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != revision:
        raise ValueError("archive revision does not match the release")
    return "sha256:" + hashlib.sha256(config).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    print(json.dumps(image_id(args.archive, revision=args.revision, tag=args.tag)))


if __name__ == "__main__":
    main()
