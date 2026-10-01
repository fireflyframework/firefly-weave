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

"""Protect installation integrity and preserve working commands on upgrade failure."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/install_cli.py"


@pytest.fixture
def installer():
    spec = importlib.util.spec_from_file_location("weave_installer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release(directory, *, wheel="firefly_weave-0.1.0a1-py3-none-any.whl"):
    directory.mkdir()
    artifacts = {wheel: b"wheel bytes", "cli-requirements.txt": b""}
    manifest = {
        "schema_version": 1,
        "version": "0.1.0a1",
        "wheel": wheel,
        "wheel_sha256": hashlib.sha256(artifacts[wheel]).hexdigest(),
        "requirements": "cli-requirements.txt",
        "requirements_sha256": hashlib.sha256(artifacts["cli-requirements.txt"]).hexdigest(),
    }
    artifacts["cli-install.json"] = json.dumps(manifest).encode()
    for name, content in artifacts.items():
        (directory / name).write_bytes(content)
    (directory / "SHA256SUMS").write_text(
        "".join(f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in artifacts.items())
    )
    return directory


def test_corrupt_release_rejected_before_install(installer, tmp_path):
    source = release(tmp_path / "release")
    (source / "cli-requirements.txt").write_text("tampered")
    with pytest.raises(installer.InstallError, match="checksum"):
        installer.load_release(source)


def test_manifest_path_traversal_rejected(installer, tmp_path):
    source = release(tmp_path / "release")
    manifest = json.loads((source / "cli-install.json").read_text())
    manifest["wheel"] = "../evil.whl"
    data = json.dumps(manifest).encode()
    (source / "cli-install.json").write_bytes(data)
    sums = (source / "SHA256SUMS").read_text().splitlines()
    sums[-1] = f"{hashlib.sha256(data).hexdigest()}  cli-install.json"
    (source / "SHA256SUMS").write_text("\n".join(sums))
    with pytest.raises(installer.InstallError, match="filename"):
        installer.load_release(source)


def test_existing_command_not_overwritten(installer, tmp_path):
    source = release(tmp_path / "release")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    command = bindir / "weave"
    command.write_text("another tool")
    with pytest.raises(installer.InstallError, match="unmanaged"):
        installer.install(source, tmp_path / "installation", bindir)
    assert command.read_text() == "another tool"


def test_failed_upgrade_preserves_previous_command(installer, tmp_path, monkeypatch):
    source = release(tmp_path / "release")
    root, bindir = tmp_path / "installation", tmp_path / "bin"

    def build(_source, _manifest, destination):
        (destination / "bin").mkdir(parents=True)
        (destination / "bin/weave").write_text("working command")

    monkeypatch.setattr(installer, "build_environment", build)
    installer.install(source, root, bindir)
    command = bindir / "weave"
    previous = command.readlink()

    def fail(*args):
        raise installer.InstallError("smoke failed")

    monkeypatch.setattr(installer, "build_environment", fail)
    with pytest.raises(installer.InstallError, match="smoke failed"):
        installer.install(source, root, bindir)
    assert command.readlink() == previous
    assert command.read_text() == "working command"


def test_uninstall_only_removes_owned_entrypoint(installer, tmp_path, monkeypatch):
    source = release(tmp_path / "release")
    root, bindir = tmp_path / "installation", tmp_path / "bin"

    def build(_source, _manifest, destination):
        (destination / "bin").mkdir(parents=True)
        (destination / "bin/weave").write_text("working command")

    monkeypatch.setattr(installer, "build_environment", build)
    installer.install(source, root, bindir)
    installer.uninstall(root, bindir)
    assert not (bindir / "weave").is_symlink()
    assert list((root / "versions").iterdir())


def test_symlinked_install_directory_refused(installer, tmp_path):
    source = release(tmp_path / "release")
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "installation"
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(installer.InstallError, match="symlink"):
        installer.install(source, root, tmp_path / "bin")
    assert list(target.iterdir()) == []


def test_shell_payload_matches_source_and_truncation_cannot_run(tmp_path):
    shell = SCRIPT.parents[1] / "install.sh"
    contents = shell.read_text()
    marker = "WEAVE_INSTALL_PYTHON"
    payload = contents.split("<<'" + marker + "'\n", 1)[1].split("\n" + marker + "\n", 1)[0]
    assert payload == SCRIPT.read_text().rstrip()
    truncated = contents[: contents.index("\n" + marker + "\n")]
    result = subprocess.run(["sh", "-s", "--", "--help"], input=truncated, text=True, capture_output=True)
    assert result.returncode != 0
    assert "usage:" not in result.stdout


def test_help_needs_no_network_or_installation():
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], text=True, capture_output=True)
    assert result.returncode == 0
    assert "--prerelease" in result.stdout
    assert "--uninstall" in result.stdout


def test_safe_exact_version_checked_before_network(installer, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid release selectors must not reach the network")

    monkeypatch.setattr(installer, "download", forbidden)
    with pytest.raises(installer.InstallError, match="safe release tag"):
        installer.fetch_release(tmp_path, "../../other/repository", False)


def test_release_asset_cannot_redirect_to_an_untrusted_repository(installer, tmp_path, monkeypatch):
    metadata = {
        "tag_name": "v0.1.0a1",
        "draft": False,
        "assets": [{"name": "SHA256SUMS", "browser_download_url": "https://example.com/SHA256SUMS"}],
    }
    monkeypatch.setattr(installer, "download", lambda *args, **kwargs: json.dumps(metadata).encode())
    with pytest.raises(installer.InstallError, match="trusted CLI artifact"):
        installer.fetch_release(tmp_path, "v0.1.0a1", False)


def test_dependency_process_ignores_ambient_python_and_pip_options(installer, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/untrusted")
    monkeypatch.setenv("PIP_INDEX_URL", "https://secret@example.com")
    monkeypatch.setenv("PIP_CONFIG_FILE", "/untrusted/pip.conf")
    monkeypatch.setenv("VIRTUAL_ENV", "/unrelated")
    environment = installer.clean_environment()
    assert "PYTHONPATH" not in environment
    assert "PIP_INDEX_URL" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert environment["PIP_CONFIG_FILE"] == "/dev/null"


def test_shell_help_uses_explicit_python_without_network():
    import os

    environment = dict(os.environ, WEAVE_INSTALL_PYTHON=sys.executable)
    result = subprocess.run(
        ["sh", str(SCRIPT.parents[1] / "install.sh"), "--help"],
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert "--from-release" in result.stdout


def test_dependency_timeout_preserves_clear_error(installer, monkeypatch):
    def timeout(arguments, **kwargs):
        assert kwargs["timeout"] == 900
        raise subprocess.TimeoutExpired(arguments, 900)

    monkeypatch.setattr(installer.subprocess, "run", timeout)
    with pytest.raises(installer.InstallError, match="timed out"):
        installer.run(["python", "-m", "pip"], "Dependency installation")


def test_redirect_rejects_plain_http_before_request(installer):
    import urllib.request

    handler = installer.GitHubRedirectHandler()
    request = urllib.request.Request("https://github.com/fireflyframework/firefly-weave/releases/download/v1/file")
    with pytest.raises(installer.InstallError, match="GitHub HTTPS"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://github.com/file")
    with pytest.raises(installer.InstallError, match="GitHub HTTPS"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://example.com/file")


def test_remote_release_manifest_must_match_selected_tag(installer, tmp_path, monkeypatch):
    source = release(tmp_path / "source")
    destination = tmp_path / "download"
    destination.mkdir()
    tag = "v0.1.0a2"
    base = f"https://github.com/{installer.REPOSITORY}/releases/download/{tag}/"
    metadata = {
        "tag_name": tag,
        "draft": False,
        "assets": [{"name": path.name, "browser_download_url": base + path.name} for path in source.iterdir()],
    }

    def download(url, **kwargs):
        if url.startswith(installer.API):
            return json.dumps(metadata).encode()
        return (source / url.rsplit("/", 1)[-1]).read_bytes()

    monkeypatch.setattr(installer, "download", download)
    with pytest.raises(installer.InstallError, match="manifest version.*release tag"):
        installer.fetch_release(destination, tag, False)
    assert not list(destination.glob("*.whl"))


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_concurrent_mutation_rejected_before_changing_command(installer, tmp_path, monkeypatch, action):
    source = release(tmp_path / "release")
    root, bindir = tmp_path / "installation", tmp_path / "bin"

    def build(_source, _manifest, destination):
        (destination / "bin").mkdir(parents=True)
        (destination / "bin/weave").write_text("working command")

    monkeypatch.setattr(installer, "build_environment", build)
    installer.install(source, root, bindir)
    previous = (bindir / "weave").readlink()
    with installer.installation_lock(root), pytest.raises(installer.InstallError, match="Another installer"):
        if action == "install":
            installer.install(source, root, bindir)
        else:
            installer.uninstall(root, bindir)
    assert (bindir / "weave").readlink() == previous
    assert len(json.loads((root / "installation.json").read_text())["environments"]) == 1


def test_lock_symlink_cannot_be_followed(installer, tmp_path):
    root = tmp_path / "installation"
    root.mkdir()
    unrelated = tmp_path / "other-file"
    unrelated.write_text("untouched")
    (root / ".install.lock").symlink_to(unrelated)
    with pytest.raises(installer.InstallError, match="lock"), installer.installation_lock(root):
        pytest.fail("symlinked lock must be rejected")
    assert unrelated.read_text() == "untouched"
