import hashlib
import io
import json
import tarfile

import pytest

from scripts.image_archive_identity import image_id


def archive(tmp_path, *, tags=None, revision="release", config_path="config.json"):
    config = json.dumps({
        "config": {"Labels": {"org.opencontainers.image.revision": revision}},
        "os": "linux",
        "architecture": "amd64",
    }).encode()
    manifest = json.dumps([{
        "Config": config_path,
        "RepoTags": ["image:release"] if tags is None else tags,
    }]).encode()
    path = tmp_path / "image.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        for name, content in (("manifest.json", manifest), (config_path, config)):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
    return path, config


@pytest.mark.parametrize("config_path", ["config.json", "blobs/sha256/config"])
def test_config_digest_is_independent_of_docker_storage_backend(tmp_path, config_path):
    path, config = archive(tmp_path, config_path=config_path)
    assert image_id(path, revision="release", tag="image:release") == (
        "sha256:" + hashlib.sha256(config).hexdigest()
    )


def test_archive_rejects_wrong_revision(tmp_path):
    path, _ = archive(tmp_path, revision="other")
    with pytest.raises(ValueError, match="revision"):
        image_id(path, revision="release", tag="image:release")


def test_archive_rejects_unexpected_image_tag(tmp_path):
    path, _ = archive(tmp_path, tags=["image:other"])
    with pytest.raises(ValueError, match="matching image"):
        image_id(path, revision="release", tag="image:release")
