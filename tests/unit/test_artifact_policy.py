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

"""Publication policy excludes internal agent documents without banning domain plan APIs."""

import importlib.util
import io
import tarfile
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[2]


def verifier():
    spec = importlib.util.spec_from_file_location("verify_artifacts", ROOT / "scripts/verify_artifacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name",
    [
        ".codex/instructions.md",
        ".agents/notes.md",
        "AGENTS.md",
        "CLAUDE.md",
        "docs/superpowers/plans/implementation.md",
        "docs/implementation-status.md",
        "internal-review.md",
        "src/firefly_weave/.codex/task.md",
    ],
)
def test_internal_sdist_members_fail_before_extraction(tmp_path, name):
    archive = tmp_path / "distribution.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("firefly_weave-0.1.0a1/" + name)
        member.size = 8
        stream.addfile(member, io.BytesIO(b"internal"))
    destination = tmp_path / "unpack"
    with pytest.raises(ValueError):
        verifier().extract_sdist(archive, destination)
    assert not destination.exists()


def test_real_domain_retention_plans_remain_publishable():
    assert verifier().public_source_path(PurePosixPath("tests/fixtures/retention/plans.json"))
    assert verifier().public_source_path(PurePosixPath("src/firefly_weave/operations/retention_plans.py"))
    assert verifier().public_source_path(PurePosixPath("docs/operations/backup-restore.md"))


def prepared_release(tmp_path):
    import hashlib
    import json
    import zipfile

    release = tmp_path / "release"
    artifacts, images = release / "artifacts", release / "images"
    artifacts.mkdir(parents=True)
    images.mkdir()
    wheel = artifacts / "firefly_weave-0.1.0a1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("firefly_weave/__init__.py", "")
        archive.writestr("firefly_weave-0.1.0a1.dist-info/METADATA", "Name: firefly-weave\nVersion: 0.1.0a1\n")
        for name in ("LICENSE", "NOTICE"):
            archive.writestr("firefly_weave-0.1.0a1.dist-info/licenses/" + name, name)
    sdist = artifacts / "firefly_weave-0.1.0a1.tar.gz"
    sdist.write_bytes(b"not read before intercepted command")
    inputs = {wheel.name: wheel.read_bytes()}
    inputs.update(
        {
            name: b"prepared input"
            for name in (
                "Dockerfile",
                "worker.Dockerfile",
                "image_identity.py",
                "main.py",
                "manifest.json",
                "LICENSE",
                "NOTICE",
            )
        }
    )
    for name in ("base", "worker", "server", "teams", "kafka"):
        inputs[name + "-requirements.txt"] = b"click==8.5.0 --hash=sha256:" + b"1" * 64 + b"\n"
    for name, data in inputs.items():
        (images / name).write_bytes(data)
    (images / ".dockerignore").write_text("**\n")
    metadata = {
        "complete": True,
        "wheel": wheel.name,
        "sdist": sdist.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "sdist_sha256": hashlib.sha256(sdist.read_bytes()).hexdigest(),
        "inputs": {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()},
    }
    for path in (release / "release.json", images / "release.json"):
        path.write_text(json.dumps(metadata))
    return release, metadata


@pytest.mark.parametrize("closure", ["base", "worker", "server", "teams", "kafka"])
@pytest.mark.parametrize("mutation", ["changed", "missing", "unrecorded"])
def test_changed_or_missing_exports_fail_before_any_external_command(tmp_path, monkeypatch, closure, mutation):
    import json

    module = verifier()
    release, metadata = prepared_release(tmp_path)
    name = closure + "-requirements.txt"
    if mutation == "changed":
        (release / "images" / name).write_bytes(b"changed export")
    elif mutation == "missing":
        (release / "images" / name).unlink()
    else:
        metadata["inputs"].pop(name)
        for path in (release / "release.json", release / "images/release.json"):
            path.write_text(json.dumps(metadata))
    calls = []

    def forbidden(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("Unverified input reached an external command")

    monkeypatch.setattr(module, "run_command", forbidden)
    with pytest.raises(ValueError):
        module.verify(release, tmp_path / "verification")
    assert calls == []


def test_unchanged_exports_are_snapshotted_and_recorded_before_installer(tmp_path, monkeypatch):
    import json

    module = verifier()
    release, metadata = prepared_release(tmp_path)
    output = tmp_path / "verification"
    calls = []

    class ReachedInstaller(Exception):
        pass

    def command(argv, **kwargs):
        calls.append(argv)
        if "venv" in argv:
            for name in ("base", "worker", "server", "teams", "kafka"):
                (release / "images" / (name + "-requirements.txt")).write_bytes(b"changed after validation")
            return b""
        assert "--require-hashes" in argv
        actual = Path(argv[argv.index("-r") + 1])
        assert actual.is_relative_to(output) and actual.read_bytes().startswith(b"click==8.5.0 ")
        recorded = json.loads((output / "validated-inputs.json").read_text())
        assert recorded["closure_sha256"] == {
            name.removesuffix("-requirements.txt"): digest
            for name, digest in metadata["inputs"].items()
            if name.endswith("-requirements.txt")
        }
        raise ReachedInstaller

    monkeypatch.setattr(module, "run_command", command)
    with pytest.raises(ReachedInstaller):
        module.verify(release, output)
    assert len(calls) == 2


def test_cli_installer_checksum_is_verified_before_execution(tmp_path, monkeypatch):
    import hashlib

    module = verifier()
    release = tmp_path / "release"
    artifacts = release / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "install.sh").write_bytes(b"changed installer")
    (artifacts / "SHA256SUMS").write_text(hashlib.sha256(b"original installer").hexdigest() + "  install.sh\n")
    calls = []

    def forbidden(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("Unverified installer reached an external command")

    monkeypatch.setattr(module, "run_command", forbidden)
    with pytest.raises(ValueError, match="CLI installer checksum"):
        module.verify_cli_installer(release, tmp_path, {"wheel_sha256": "unused"})
    assert calls == []
