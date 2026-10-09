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

"""Interrupted temporary writes never expose incomplete final journals or mounted secret generations."""

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest
from ai_platform_support import owned_fixture  # noqa: F401
from test_platform_ai import MODEL, enable, harness_fixture  # noqa: F401
from test_platform_ai_origins import ENTRIES

from firefly_weave.sdk import platform, platform_ai, platform_origins
from firefly_weave.sdk import platform_ai_files as files


def creation_fault(patch, destination, boundary):
    """Inject at real stream/syscall boundaries for the selected publication, including direct-write controls."""
    selected = set()
    fdopen, temporary, chmod, fsync, link, replace, unlink = (
        os.fdopen,
        tempfile.NamedTemporaryFile,
        os.fchmod,
        os.fsync,
        os.link,
        Path.replace,
        Path.unlink,
    )

    def inode(fd):
        info = os.fstat(fd)
        return info.st_dev, info.st_ino

    def target():
        return destination() if callable(destination) else destination

    class Stream:
        def __init__(self, stream):
            self.stream = stream
            selected.add(inode(stream.fileno()))

        def __getattr__(self, key):
            return getattr(self.stream, key)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            if boundary == "write":
                self.stream.write(data[:2])
                self.stream.flush()
                raise OSError("interrupted partial write")
            return self.stream.write(data)

        def flush(self):
            self.stream.flush()
            if boundary == "flush":
                raise OSError("interrupted flush")

    def opened(fd, *args, **kwargs):
        path = target()
        if path.exists() and inode(fd) == (path.stat().st_dev, path.stat().st_ino):
            return Stream(fdopen(fd, *args, **kwargs))
        return fdopen(fd, *args, **kwargs)

    def created(*args, **kwargs):
        stream = temporary(*args, **kwargs)
        return Stream(stream) if target().name in kwargs.get("prefix", "") else stream

    def changed_mode(fd, mode):
        chmod(fd, mode)
        if boundary == "chmod" and inode(fd) in selected:
            raise OSError("interrupted chmod")

    def synced(fd):
        fsync(fd)
        path = target()
        matches = inode(fd) in selected or (path.exists() and inode(fd) == (path.stat().st_dev, path.stat().st_ino))
        if boundary == "fsync" and matches:
            raise OSError("interrupted fsync")

    def published(source, final, *args, **kwargs):
        if boundary == "publish" and Path(final) == target():
            raise OSError("interrupted publish")
        result = link(source, final, *args, **kwargs)
        if boundary == "after-publish" and Path(final) == target():
            raise OSError("interrupted after publish")
        return result

    def replaced(source, final):
        if boundary == "publish" and Path(final) == target():
            raise OSError("interrupted publish")
        result = replace(source, final)
        if boundary == "after-publish" and Path(final) == target():
            raise OSError("interrupted after publish")
        return result

    def retained(path, *args, **kwargs):
        if boundary == "after-publish" and path.name.startswith("." + target().name + "-"):
            raise OSError("interrupted temporary cleanup")
        return unlink(path, *args, **kwargs)

    patch.setattr(os, "fdopen", opened)
    patch.setattr(tempfile, "NamedTemporaryFile", created)
    patch.setattr(os, "fchmod", changed_mode)
    patch.setattr(os, "fsync", synced)
    patch.setattr(os, "link", published)
    patch.setattr(Path, "replace", replaced)
    patch.setattr(Path, "unlink", retained)


@pytest.mark.parametrize("boundary", ["write", "flush", "chmod", "fsync", "publish"])
def test_incomplete_journal_creation_preserves_resumable_previous_pair(owned, monkeypatch, boundary):
    directory, _, _ = owned
    platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    state = platform._load(directory)
    before = (directory / "platform.json").read_bytes(), (directory / platform_origins.FILE).read_bytes()
    changed = [
        e.model_copy(update={"networks": ("10.246.23.0/24",)}) if e.origin == "http://ollama:11434" else e
        for e in ENTRIES
    ]
    with monkeypatch.context() as patch:
        creation_fault(patch, directory / platform_origins.PENDING, boundary)
        with pytest.raises(OSError, match="interrupted"):
            platform_origins.set_ai_entries(state, changed)
    assert not (directory / platform_origins.PENDING).exists()
    assert before == ((directory / "platform.json").read_bytes(), (directory / platform_origins.FILE).read_bytes())
    recovered = platform._load(directory)
    assert platform_origins.set_ai_entries(recovered, changed)
    platform._load(directory)


@pytest.mark.parametrize("secret", [files.GATEWAY_TOKEN, files.WORKER_SECRET])
@pytest.mark.parametrize("boundary", ["write", "flush", "chmod", "fsync", "publish", "after-publish"])
def test_incomplete_generation_creation_keeps_intent_and_resumes(harness, monkeypatch, secret, boundary):
    h = harness
    enable(h)
    legacy = h.directory / files.SECRETS_DIRECTORY / secret
    legacy.chmod(0o600)
    previous = platform_ai.receipt(h.directory)["secret_generations"][secret]

    def destination():
        value = json.loads((h.directory / "ai.json").read_text())
        return h.directory / files.SECRETS_DIRECTORY / (secret + "-" + value["secret_generations"][secret])

    with monkeypatch.context() as patch:
        creation_fault(patch, destination, boundary)
        with pytest.raises(platform.PlatformError):
            enable(h)
    value = platform_ai.receipt(h.directory)
    generation = value["secret_generations"][secret]
    assert generation != previous
    assert destination().exists() is (boundary == "after-publish")
    assert enable(h)["stage"] == "ready"
    assert platform_ai.receipt(h.directory)["secret_generations"][secret] == generation
    assert stat.S_IMODE(destination().stat().st_mode) == 0o444
    assert enable(h)["changed"] == []


def test_metadata_temporary_is_complete_and_fsynced_before_replacement(owned, monkeypatch):
    directory, _, _ = owned
    synced = set()
    fsync, replace = os.fsync, Path.replace
    observed = []

    def sync(fd):
        info = os.fstat(fd)
        synced.add((info.st_dev, info.st_ino))
        fsync(fd)

    def publish(source, destination):
        if Path(destination) == directory / "platform.json":
            info = source.stat()
            assert (info.st_dev, info.st_ino) in synced
            assert json.loads(source.read_text())["private_origins"]["ai"]["entries"]
            observed.append(True)
        return replace(source, destination)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "replace", publish)
    platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    assert observed


@pytest.mark.parametrize("argument", [Path("platform"), Path("./platform"), Path("platform/../platform")])
def test_sdk_relative_and_lexical_aliases_share_validated_lock_identity(harness, monkeypatch, argument):
    h = harness
    monkeypatch.chdir(h.directory.parent)
    result = platform_ai.enable(argument, ollama_mode="container", models=[MODEL], confirm=lambda _: True)
    assert result["stage"] == "ready"
    assert enable(h)["changed"] == []


def test_lexical_alias_cannot_hide_a_disallowed_symlink(harness, monkeypatch):
    h = harness
    monkeypatch.chdir(h.directory.parent)
    Path("alias").symlink_to(h.directory, target_is_directory=True)
    with pytest.raises((platform.PlatformError, platform.DeploymentError)):
        platform_ai.enable(Path("alias/../platform"), ollama_mode="container", models=[MODEL], confirm=lambda _: True)
    assert not (h.directory / "ai.json").exists()


def test_published_journal_is_complete_and_recovers_with_orphan_temporary(owned, monkeypatch):
    directory, _, _ = owned
    unrecognized = directory / ("." + platform_origins.PENDING + "-unrecognized")
    unrecognized.write_bytes(b"foreign temporary")
    with monkeypatch.context() as patch:
        creation_fault(patch, directory / platform_origins.PENDING, "after-publish")
        with pytest.raises(OSError, match="after publish"):
            platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    pending = (directory / platform_origins.PENDING).read_bytes()
    record = json.loads(pending)
    assert record["before"] is None and record["after"]["ai"]["entries"]
    orphans = [p for p in directory.glob("." + platform_origins.PENDING + "-*") if p != unrecognized]
    assert orphans and all(p.read_bytes() == pending for p in orphans)
    state = platform._load(directory)
    assert platform_origins.set_ai_entries(state, ENTRIES) is False
    assert not (directory / platform_origins.PENDING).exists()
    assert unrecognized.read_bytes() == b"foreign temporary" and all(p.exists() for p in orphans)


@pytest.mark.parametrize("boundary", ["write", "flush", "chmod", "fsync", "publish", "after-publish"])
def test_metadata_creation_interruption_recovers_from_complete_journal(owned, monkeypatch, boundary):
    directory, _, _ = owned
    before = (directory / "platform.json").read_bytes()
    with monkeypatch.context() as patch:
        creation_fault(patch, directory / "platform.json", boundary)
        with pytest.raises(OSError, match="interrupted"):
            platform_origins.set_ai_entries(platform._load(directory), ENTRIES)
    pending = json.loads((directory / platform_origins.PENDING).read_bytes())
    assert pending["after"]["ai"]["entries"]
    if boundary == "after-publish":
        assert json.loads((directory / "platform.json").read_text())["private_origins"] == pending["after"]
    else:
        assert (directory / "platform.json").read_bytes() == before
    state = platform._load(directory)
    assert state["private_origins"] == pending["after"]
    assert not (directory / platform_origins.PENDING).exists()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_non_overwriting_publication_preserves_foreign_final_entry(owned, kind):
    directory, _, _ = owned
    target = directory / "foreign-target"
    target.write_bytes(b"foreign value")
    path = directory / "final"
    if kind == "symlink":
        path.symlink_to(target)
    else:
        path.write_bytes(b"foreign value")
    with pytest.raises(FileExistsError):
        platform._atomic(path, b"owned replacement")
    assert path.read_bytes() == target.read_bytes() == b"foreign value"
    assert path.is_symlink() is (kind == "symlink")


@pytest.mark.parametrize("publication", ["journal", "generation"])
def test_foreign_final_created_during_publication_is_refused_without_overwrite(harness, monkeypatch, publication):
    h = harness
    enable(h)
    if publication == "generation":
        (h.directory / files.SECRETS_DIRECTORY / files.GATEWAY_TOKEN).chmod(0o600)
    link = os.link
    observed = []

    def racing(source, destination, *args, **kwargs):
        path = Path(destination)
        selected = (
            path.name == platform_origins.PENDING
            if publication == "journal"
            else path.name.startswith(files.GATEWAY_TOKEN + "-")
        )
        if selected:
            path.write_bytes(b"foreign final")
            path.chmod(0o600 if publication == "journal" else 0o444)
            observed.append(path)
        return link(source, destination, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", racing)
        with pytest.raises((OSError, platform.PlatformError)):
            if publication == "journal":
                entries = [
                    e.model_copy(update={"networks": ("10.246.23.0/24",)}) if e.origin == "http://ollama:11434" else e
                    for e in ENTRIES
                ]
                platform_origins.set_ai_entries(platform._load(h.directory), entries)
            else:
                enable(h)
    assert len(observed) == 1 and observed[0].read_bytes() == b"foreign final"
    with pytest.raises(platform.PlatformError):
        if publication == "journal":
            platform._load(h.directory)
        else:
            enable(h)
    assert observed[0].read_bytes() == b"foreign final"


@pytest.mark.parametrize("secret", [files.GATEWAY_TOKEN, files.WORKER_SECRET])
def test_content_changed_generation_is_refused_without_new_intent(harness, secret):
    h = harness
    enable(h)
    value = platform_ai.receipt(h.directory)
    path = h.directory / files.SECRETS_DIRECTORY / (secret + "-" + value["secret_generations"][secret])
    path.chmod(0o600)
    foreign = b"x" * 43 + b"\n" if secret == files.GATEWAY_TOKEN else b"foreign worker value"
    path.write_bytes(foreign)
    path.chmod(0o444)
    saved = (h.directory / "ai.json").read_bytes()
    with pytest.raises(platform.PlatformError, match="generation changed"):
        enable(h)
    assert path.read_bytes() == foreign
    assert platform_ai.receipt(h.directory)["secret_generations"] == value["secret_generations"]
    # Enable may record its validation stage, but it must not silently invent a replacement generation.
    assert (
        json.loads(saved)["secret_generations"]
        == json.loads((h.directory / "ai.json").read_bytes())["secret_generations"]
    )


def test_authoritative_worker_rotation_accepts_matching_old_compatibility_and_generation(harness):
    h = harness
    enable(h)
    value = platform_ai.receipt(h.directory)
    previous = dict(value["secret_generations"])
    identity = h.directory / "identity.env"
    values = platform._env_file(identity)
    original = values["WEAVE_WORKER_SECRET"]
    replacement = "new-authoritative-worker-secret-canary"
    identity.write_text(identity.read_text().replace(original, replacement))
    assert files.write_settings(platform._load(h.directory), value)
    assert value["secret_generations"][files.WORKER_SECRET] != previous[files.WORKER_SECRET]
    assert value["secret_generations"][files.GATEWAY_TOKEN] == previous[files.GATEWAY_TOKEN]
    path = (
        h.directory
        / files.SECRETS_DIRECTORY
        / (files.WORKER_SECRET + "-" + value["secret_generations"][files.WORKER_SECRET])
    )
    assert path.read_bytes() == replacement.encode()
    assert files.write_settings(platform._load(h.directory), value) is False


def test_worker_rotation_with_unavailable_old_copy_and_conflicting_generation_refuses_ambiguity(harness):
    h = harness
    enable(h)
    value = platform_ai.receipt(h.directory)
    legacy = h.directory / files.SECRETS_DIRECTORY / files.WORKER_SECRET
    legacy.unlink()
    identity = h.directory / "identity.env"
    original = platform._env_file(identity)["WEAVE_WORKER_SECRET"]
    identity.write_text(identity.read_text().replace(original, "new-authoritative-worker-secret-canary"))
    previous = dict(value["secret_generations"])
    with pytest.raises(platform.PlatformError, match="generation changed"):
        files.write_settings(platform._load(h.directory), value)
    assert platform_ai.receipt(h.directory)["secret_generations"] == previous
