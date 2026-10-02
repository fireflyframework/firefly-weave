# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0

"""Installed Studio bundles must match hashes/version and never escape their cache."""

import hashlib
import json
import zipfile

import pytest

from firefly_weave import __version__
from firefly_weave.studio.assets import find_assets, install_bundle


def bundle(tmp_path, extra=None, version=__version__):
    files = {"index.html": b"<!doctype html><title>Studio</title>", "main.js": b"console.log('studio')"}
    if extra:
        files.update(extra)
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        z.writestr(
            "studio-manifest.json",
            json.dumps({"version": version, "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}),
        )
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_install_verified_bundle_without_node(tmp_path):
    archive, digest = bundle(tmp_path)
    cache = tmp_path / "cache"
    installed = install_bundle(archive, digest, cache=cache)
    assert (installed / "index.html").is_file()
    assert find_assets(cache=cache) == installed
    assert install_bundle(archive, digest, cache=cache) == installed


def test_reject_hash_version_and_traversal(tmp_path):
    archive, digest = bundle(tmp_path)
    with pytest.raises(ValueError):
        install_bundle(archive, "0" * 64, cache=tmp_path / "cache")
    archive, digest = bundle(tmp_path, version="999.0.0")
    with pytest.raises(ValueError):
        install_bundle(archive, digest, cache=tmp_path / "cache")
    archive, digest = bundle(tmp_path, extra={"../outside.js": b"bad"})
    with pytest.raises(ValueError):
        install_bundle(archive, digest, cache=tmp_path / "cache")
    assert not (tmp_path / "outside.js").exists()


def test_changed_installed_asset_is_rejected(tmp_path):
    archive, digest = bundle(tmp_path)
    installed = install_bundle(archive, digest, cache=tmp_path / "cache")
    (installed / "main.js").write_text("changed")
    with pytest.raises(ValueError):
        find_assets(cache=tmp_path / "cache")


def test_build_bundle_roundtrip(tmp_path):
    from firefly_weave.studio.assets import build_bundle

    source = tmp_path / "browser"
    source.mkdir()
    (source / "index.html").write_text("<!doctype html><title>Studio</title>")
    (source / "main.js").write_text("export const name='Weave';")
    (source / "main.js.map").write_text("private source map")
    archive, digest = build_bundle(source, tmp_path / "release")
    with zipfile.ZipFile(archive) as z:
        assert "main.js.map" not in z.namelist()
    assert install_bundle(archive, digest, cache=tmp_path / "cache").is_dir()


def test_reinstall_repairs_corruption_and_preserves_backup(tmp_path):
    archive, digest = bundle(tmp_path)
    cache = tmp_path / "cache"
    installed = install_bundle(archive, digest, cache=cache)
    (installed / "main.js").write_text("damaged")
    assert install_bundle(archive, digest, cache=cache) == installed
    assert find_assets(cache=cache) == installed
    backups = list(cache.glob(__version__ + ".backup-*"))
    assert len(backups) == 1 and (backups[0] / "main.js").read_text() == "damaged"


def test_failed_repair_restores_previous_installation(tmp_path, monkeypatch):
    from pathlib import Path

    archive, digest = bundle(tmp_path)
    cache = tmp_path / "cache"
    installed = install_bundle(archive, digest, cache=cache)
    (installed / "main.js").write_text("retained damaged bytes")
    original = Path.rename

    def rename(path, target):
        if path.name == "assets":
            raise OSError("simulated installation failure")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError):
        install_bundle(archive, digest, cache=cache)
    assert (installed / "main.js").read_text() == "retained damaged bytes"
