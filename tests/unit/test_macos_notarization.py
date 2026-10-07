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

"""Release signing must fail closed before secrets or notarization are used."""

import base64
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "desktop/scripts/notarize_macos.py"


def load():
    assert SCRIPT.is_file(), "The trusted macOS signing helper is not implemented"
    return runpy.run_path(str(SCRIPT))


def environment():
    return {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "fireflyframework/firefly-weave",
        "GITHUB_REF": "refs/tags/v0.1.0a13",
        "GITHUB_SHA": "a" * 40,
        "APPLE_CERTIFICATE": base64.b64encode(b"test certificate").decode(),
        "APPLE_CERTIFICATE_PASSWORD": "private-password",
        "APPLE_SIGNING_IDENTITY": "Developer ID Application: Example (ABCDEFGHIJ)",
        "APPLE_TEAM_ID": "ABCDEFGHIJ",
        "APPLE_API_ISSUER": "00000000-0000-0000-0000-000000000001",
        "APPLE_API_KEY": "0123456789",
        "APPLE_API_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----",
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_EVENT_NAME", "pull_request_target"),
        ("GITHUB_REPOSITORY", "untrusted/fork"),
        ("GITHUB_REF", "refs/heads/main"),
    ],
)
def test_untrusted_context_rejected_before_git_or_credentials(key, value):
    module = load()
    env = environment()
    env[key] = value
    with pytest.raises(RuntimeError, match="trusted release tag"):
        module["verify_source"](ROOT, env, "0.1.0a13")


def test_tag_and_checked_out_commit_must_match(monkeypatch):
    module = load()
    responses = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: next(responses))
    with pytest.raises(RuntimeError, match="commit"):
        module["verify_source"](ROOT, environment(), "0.1.0a13")


def test_partial_credentials_list_names_without_values():
    env = environment()
    del env["APPLE_API_PRIVATE_KEY"]
    with pytest.raises(RuntimeError) as exc:
        load()["validate_credentials"](env)
    assert "APPLE_API_PRIVATE_KEY" in str(exc.value)
    assert "private-password" not in str(exc.value)


@pytest.mark.parametrize(
    "identity", ["-", "Apple Development: Example (ABCDEFGHIJ)", "Developer ID Application: Example (WRONGTEAM1)"]
)
def test_wrong_certificate_kind_or_team_rejected(identity):
    env = environment()
    env["APPLE_SIGNING_IDENTITY"] = identity
    with pytest.raises(RuntimeError, match="Developer ID Application"):
        load()["validate_credentials"](env)


def test_complete_credentials_accepted():
    load()["validate_credentials"](environment())


@pytest.mark.parametrize("change", ["authority", "team", "runtime", "timestamp"])
def test_signed_metadata_requires_exact_identity_runtime_and_timestamp(change):
    env = environment()
    text = (
        f"Authority={env['APPLE_SIGNING_IDENTITY']}\nTeamIdentifier=ABCDEFGHIJ\n"
        "CodeDirectory flags=0x10000(runtime)\nTimestamp=Oct 7, 2026\n"
    )
    text = {
        "authority": text.replace("Developer ID Application", "Apple Development"),
        "team": text.replace("TeamIdentifier=ABCDEFGHIJ", "TeamIdentifier=WRONGTEAM1"),
        "runtime": text.replace("(runtime)", "(adhoc)"),
        "timestamp": text.replace("Timestamp=Oct 7, 2026", "Signed Time=Oct 7, 2026"),
    }[change]
    with pytest.raises(RuntimeError, match="signature"):
        load()["verify_signature_details"](text, env["APPLE_SIGNING_IDENTITY"], env["APPLE_TEAM_ID"])


def test_unsigned_or_pending_notary_response_never_accepted():
    module = load()
    for status in ("Invalid", "In Progress", None):
        with pytest.raises(RuntimeError, match="Accepted"):
            module["accepted_submission"]({"id": "submission", "status": status})
    assert module["accepted_submission"]({"id": "submission", "status": "Accepted"}) == "submission"


def test_frozen_host_signing_includes_entitlements(monkeypatch, tmp_path):
    module = runpy.run_path(str(ROOT / "desktop/scripts/build_sidecar.py"))
    assert "signing_arguments" in module, "Frozen host signing must explicitly include entitlements"
    args = module["signing_arguments"]("Developer ID Application: Example (ABCDEFGHIJ)", tmp_path)
    assert args == [
        "--codesign-identity",
        "Developer ID Application: Example (ABCDEFGHIJ)",
        "--osx-entitlements-file",
        str(tmp_path / "desktop/src-tauri/entitlements.plist"),
    ]
    assert module["signing_arguments"](None, tmp_path) == []


def test_cleanup_runs_after_signing_failure(tmp_path, monkeypatch):
    module = load()
    env = environment() | {"RUNNER_TEMP": str(tmp_path)}
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["list-keychains", "-d"] and "-s" not in command:
            return subprocess.CompletedProcess(
                command, 0, stdout='"/Users/runner/Library/Keychains/login.keychain-db"\n', stderr=""
            )
        if command[1] == "import":
            raise subprocess.CalledProcessError(1, command, stderr="private-password")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError) as exc, module["signing_session"](env):
        pytest.fail("Import failure must prevent signing")
    assert "private-password" not in str(exc.value)
    assert any(command[1] == "delete-keychain" for command in commands)
    assert not list(tmp_path.glob("weave-signing-*"))
    assert not (tmp_path / "weave-macos-signing-state.json").exists()


def test_workflow_never_exposes_credentials_to_pull_requests_or_ordinary_builds():
    import yaml

    workflow = ROOT / ".github/workflows/desktop-notarized.yml"
    assert workflow.exists(), "A separately gated signing workflow is required"
    data = yaml.safe_load(workflow.read_text())
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["notarize"]["default"] is False
    job = data["jobs"]["macos"]
    assert "github.repository == 'fireflyframework/firefly-weave'" in job["if"]
    assert "startsWith(github.ref, 'refs/tags/v')" in job["if"]
    assert "inputs.notarize == true" in job["if"]
    assert job["environment"] == "macos-release-signing"
    secret_steps = [step for step in job["steps"] if "secrets." in str(step)]
    assert len(secret_steps) == 1
    assert secret_steps[0]["run"].endswith("notarize_macos.py --target ${{ matrix.target }}")
    assert "secrets." not in (ROOT / ".github/workflows/desktop.yml").read_text()
    cleanup = next(step for step in job["steps"] if "--cleanup" in step.get("run", ""))
    assert cleanup["if"] == "always()"


def test_notary_ticket_failure_blocks_gatekeeper_and_installer_verification(tmp_path, monkeypatch):
    module = load()
    app = tmp_path / "Studio.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/host").write_bytes(b"test")
    (app / "Contents/_CodeSignature").mkdir()
    (app / "Contents/_CodeSignature/CodeResources").write_bytes(b"seal")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["xcrun", "stapler", "validate"]:
            raise subprocess.CalledProcessError(1, command)
        output = ""
        if command[:3] == ["codesign", "--display", "--verbose=4"]:
            output = (
                "Authority=Developer ID Application: Example (ABCDEFGHIJ)\nTeamIdentifier=ABCDEFGHIJ\n"
                "CodeDirectory flags=0x10000(runtime)\nTimestamp=Oct 7, 2026\n"
            )
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError, match="signing was not completed"):
        module["verify_tickets"](app, tmp_path / "Studio.dmg", environment()["APPLE_SIGNING_IDENTITY"], "ABCDEFGHIJ")
    assert not any(command[0] in {"spctl", "hdiutil"} for command in commands)


@pytest.mark.parametrize("interrupted", [False, True])
def test_session_restores_search_list_and_removes_private_files(tmp_path, monkeypatch, interrupted):
    module = load()
    env = environment() | {"RUNNER_TEMP": str(tmp_path)}
    previous = ["/Users/runner/Library/Keychains/login.keychain-db", "/Library/Keychains/System.keychain"]
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        output = ""
        if command[1] == "list-keychains" and "-s" not in command:
            output = "\n".join(f'"{path}"' for path in previous)
        if command[1] == "find-identity":
            output = f'1) {"A" * 40} "{env["APPLE_SIGNING_IDENTITY"]}"\n 1 valid identities found'
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    def invoke():
        with module["signing_session"](env) as (child, directory):
            assert "APPLE_CERTIFICATE" not in child
            assert "APPLE_CERTIFICATE_PASSWORD" not in child
            assert "APPLE_API_PRIVATE_KEY" not in child
            assert (directory / "AuthKey.p8").stat().st_mode & 0o777 == 0o600
            assert not (directory / "certificate.p12").exists()
            if interrupted:
                raise SystemExit("interrupted")

    if interrupted:
        with pytest.raises(SystemExit):
            invoke()
    else:
        invoke()
    assert ["security", "list-keychains", "-d", "user", "-s", *previous] in commands
    assert not any(command[1] == "default-keychain" for command in commands)
    assert not list(tmp_path.iterdir())


def test_cleanup_refuses_unowned_directory(tmp_path):
    import json

    (tmp_path / "weave-macos-signing-state.json").write_text(
        json.dumps({"directory": str(tmp_path.parent), "search_list": [], "keychain_created": True})
    )
    with pytest.raises(RuntimeError, match="outside the owned signing directory"):
        load()["cleanup"](tmp_path)


@pytest.mark.parametrize("ticket_valid", [False, True])
def test_artifacts_only_collected_after_ticket_gate_and_cleanup(tmp_path, monkeypatch, ticket_valid):
    import json
    import sys
    from contextlib import contextmanager

    module = load()
    function = module["build"]
    scope = function.__globals__
    target = "aarch64-apple-darwin"
    (tmp_path / "desktop/src-tauri").mkdir(parents=True)
    (tmp_path / "desktop/src-tauri/tauri.conf.json").write_text(json.dumps({"version": "0.1.0-alpha.13"}))
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.1.0a13"\n')
    bundle = tmp_path / "desktop/src-tauri/target" / target / "release/bundle"
    (bundle / "macos/Firefly Weave Studio.app/Contents/MacOS").mkdir(parents=True)
    (bundle / "macos/Firefly Weave Studio.app/Contents/MacOS/weave-studio-host").write_bytes(b"signed host")
    (bundle / "dmg").mkdir()
    (bundle / "dmg/Studio.dmg").write_bytes(b"signed and stapled installer")
    (tmp_path / "desktop/work").mkdir()
    (tmp_path / "desktop/work/sidecar-manifest.json").write_text(json.dumps({"sha256": "previous", "filename": "host"}))
    output = tmp_path / "desktop/work/notarized-assets"
    state = {"cleaned": False}

    @contextmanager
    def session(env):
        try:
            yield env | {"APPLE_API_KEY_PATH": "private-key-file"}, tmp_path
        finally:
            assert not output.exists()
            state["cleaned"] = True

    def tickets(*args):
        if not ticket_valid:
            raise RuntimeError("Ticket rejected")

    def run(command, **kwargs):
        if "hash_installers.py" in str(command):
            assert state["cleaned"]
            (output / "SHA256SUMS").write_text("hashes")
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"status": "Accepted", "id": "test-submission"}), stderr=""
        )

    monkeypatch.setitem(scope, "verify_source", lambda *args: None)
    monkeypatch.setitem(scope, "signing_session", session)
    monkeypatch.setitem(scope, "verify_tickets", tickets)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "platform", "darwin")
    if ticket_valid:
        function(tmp_path, target, environment())
        record = json.loads((output / f"weave-studio-{target}-build.json").read_text())
        assert record["notarized"] is True and record["unsigned"] is False
        assert record["dmg_submission_id"] == "test-submission"
        assert record["dmg_sha256"] == module["digest"](bundle / "dmg/Studio.dmg")
    else:
        with pytest.raises(RuntimeError, match="Ticket rejected"):
            function(tmp_path, target, environment())
        assert not output.exists()
    assert state["cleaned"]


def test_always_cleanup_can_retry_search_list_restoration_after_failure(tmp_path, monkeypatch):
    import json

    module = load()
    directory = tmp_path / "weave-signing-owned"
    directory.mkdir()
    (directory / "AuthKey.p8").write_text("private")
    state = tmp_path / "weave-macos-signing-state.json"
    state.write_text(
        json.dumps({"directory": str(directory), "search_list": ["original.keychain-db"], "keychain_created": True})
    )
    attempts = []

    def run(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            raise subprocess.CalledProcessError(1, command)
        assert command[1] != "delete-keychain", "Private directory has already been removed"
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(RuntimeError):
        module["cleanup"](tmp_path)
    assert not directory.exists()
    assert state.exists()
    module["cleanup"](tmp_path)
    assert not state.exists()
