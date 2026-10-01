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

"""Worker context validation and safe deployment command contracts."""

import json
import zipfile
from contextlib import suppress
from pathlib import Path

import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli


@pytest.fixture
def package_inputs(tmp_path):
    source = tmp_path / "worker source;$literal"
    source.mkdir()
    manifest = source / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "capabilities": [
                    {
                        "taskType": "echo",
                        "taskVersion": "1.0.0",
                        "inputSchema": {"type": "object"},
                        "outputSchema": {"type": "object"},
                        "sideEffect": "idempotency_key",
                        "timeoutSeconds": 30,
                    }
                ]
            }
        )
    )
    (source / "main.py").write_text("raise RuntimeError('must never execute while packaging')\n")
    wheel = source / "firefly_weave-0.1.0a1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("firefly_weave/__init__.py", "__version__ = '0.1.0a1'\n")
        archive.writestr(
            "firefly_weave-0.1.0a1.dist-info/METADATA", "Metadata-Version: 2.4\nName: firefly-weave\nVersion: 0.1.0a1\n"
        )
        archive.writestr("firefly_weave-0.1.0a1.dist-info/licenses/LICENSE", "Apache License\n")
        archive.writestr("firefly_weave-0.1.0a1.dist-info/licenses/NOTICE", "Firefly Software Foundation\n")
    requirements = source / "worker.txt"
    requirements.write_text("click==8.5.0 --hash=sha256:" + "1" * 64 + "\n")
    (source / ".env").write_text("SECRET=must-not-copy")
    return manifest, wheel, requirements


def invoke_package(inputs, output):
    manifest, wheel, requirements = inputs
    return CliRunner().invoke(
        cli,
        [
            "worker",
            "package",
            "--manifest",
            str(manifest),
            "--wheel",
            str(wheel),
            "--requirements",
            str(requirements),
            "--output",
            str(output),
            "--format",
            "json",
        ],
    )


def test_package_is_allowlisted_and_preserves_exact_wheel(package_inputs, tmp_path):
    output = tmp_path / "package"
    result = invoke_package(package_inputs, output)
    assert result.exit_code == 0, result.output
    metadata = json.loads((output / "build.json").read_text())
    assert metadata["complete"] is True
    assert set(p.name for p in output.iterdir()) == {
        "build.json",
        "manifest.json",
        "main.py",
        "worker-requirements.txt",
        "Dockerfile",
        "compose.json",
        "LICENSE",
        "NOTICE",
        package_inputs[1].name,
    }
    assert (output / package_inputs[1].name).read_bytes() == package_inputs[1].read_bytes()
    assert "must-not-copy" not in "".join(p.read_text(errors="replace") for p in output.iterdir())
    assert "must never execute" in (output / "main.py").read_text()
    assert "image_digest" not in json.loads((output / "manifest.json").read_text())


@pytest.mark.parametrize("bad", ["duplicate", "unknown", "schema", "image", "json-duplicate"])
def test_manifest_rejected_before_any_output(package_inputs, tmp_path, bad):
    manifest = package_inputs[0]
    value = json.loads(manifest.read_text())
    if bad == "duplicate":
        value["capabilities"] *= 2
    elif bad == "unknown":
        value["secret"] = "do-not-echo"
    elif bad == "schema":
        value["capabilities"][0]["inputSchema"] = {"type": "nonsense"}
    elif bad == "image":
        value["image_digest"] = "sha256:" + "0" * 64
    manifest.write_text('{"capabilities":[],"capabilities":[]}' if bad == "json-duplicate" else json.dumps(value))
    output = tmp_path / "invalid"
    result = invoke_package(package_inputs, output)
    assert result.exit_code == 1, result.output
    assert "WV-WORKER-INVALID" in result.output
    assert "do-not-echo" not in result.output
    assert not output.exists()


@pytest.mark.parametrize(
    "member",
    [
        "../escape",
        "%2e%2e/escape",
        "a/../../escape",
        "a/./escape",
        "a//escape",
        "/absolute",
        "a\\escape",
        "firefly_weave/__init__.py",
    ],
)
def test_unsafe_or_duplicate_wheel_members_rejected(package_inputs, tmp_path, member):
    with zipfile.ZipFile(package_inputs[1], "a") as archive:
        archive.writestr(member, "bad")
    result = invoke_package(package_inputs, tmp_path / "invalid")
    assert result.exit_code == 1
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize(
    "requirement",
    [
        "-e .",
        "click>=8",
        "click==8 --hash=md5:123",
        "--index-url https://secret.example",
        "pkg @ file:///private/file",
        "pkg @ git+https://example.org/pkg",
        "pkg==1 --hash=sha256:" + "1" * 64 + " --extra-index-url https://secret.example",
    ],
)
def test_unlocked_or_ambient_requirements_rejected(package_inputs, tmp_path, requirement):
    package_inputs[2].write_text(requirement)
    result = invoke_package(package_inputs, tmp_path / "invalid")
    assert result.exit_code == 1
    assert "secret.example" not in result.output
    assert not (tmp_path / "invalid").exists()


def test_symlink_input_and_existing_output_are_preserved(package_inputs, tmp_path):
    manifest, wheel, requirements = package_inputs
    link = tmp_path / "linked.json"
    link.symlink_to(manifest)
    result = invoke_package((link, wheel, requirements), tmp_path / "invalid")
    assert result.exit_code == 1
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("unchanged")
    result = invoke_package(package_inputs, occupied)
    assert result.exit_code == 1
    assert (occupied / "keep").read_text() == "unchanged"
    assert len(list(occupied.iterdir())) == 1


@pytest.mark.parametrize(
    "mode", ["success", "foreign-race", "image-mismatch", "existing", "remote-context", "open-env"]
)
def test_deploy_only_starts_verified_owned_container(package_inputs, tmp_path, monkeypatch, mode):
    from firefly_weave.sdk import deployment

    directory = tmp_path / "context;$literal"
    assert invoke_package(package_inputs, directory).exit_code == 0
    metadata = json.loads((directory / "build.json").read_text())
    env = tmp_path / "private env;$literal"
    env.write_text("SECRET=never-echo")
    env.chmod(0o644 if mode == "open-env" else 0o600)
    calls = []
    state = {}
    image = "sha256:" + "a" * 64
    container = "b" * 64

    def command(argv, **kwargs):
        calls.append(argv)
        assert argv[0:3] == ["docker", "--context", "owned-context"]
        args = argv[3:]
        if args[:2] == ["context", "inspect"]:
            return json.dumps(
                [
                    {
                        "Endpoints": {
                            "docker": {
                                "Host": "tcp://foreign:2375"
                                if mode == "remote-context"
                                else "unix:///private/docker.sock"
                            }
                        }
                    }
                ]
            ).encode()
        if args[:2] == ["image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": image,
                        "Config": {
                            "Labels": {
                                deployment.WHEEL_LABEL: "wrong"
                                if mode == "image-mismatch"
                                else metadata["wheel_sha256"],
                                deployment.MANIFEST_LABEL: metadata["manifest_sha256"],
                            }
                        },
                    }
                ]
            ).encode()
        if args[0] == "ps":
            if "created" in state:
                return (container + "\n").encode()
            return (container + "\n").encode() if mode == "existing" else b""
        if args[0] == "compose":
            compose = json.loads(Path(args[args.index("--file") + 1]).read_text())
            service = compose["services"]["worker"]
            assert service["env_file"] == [{"path": str(env).replace("$", "$$"), "format": "raw"}]
            assert service["image"] == image
            state["nonce"] = service["labels"][deployment.OWNERSHIP_LABEL]
            if "create" in args:
                assert "--no-recreate" in args and "--pull" in args
                state["created"] = True
            return b""
        if args[:2] == ["container", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": container,
                        "Image": image,
                        "State": {"Status": "created"},
                        "Config": {
                            "Image": image,
                            "Labels": {
                                "com.docker.compose.project": "owned-project",
                                "com.docker.compose.service": "worker",
                                deployment.OWNERSHIP_LABEL: "foreign" if mode == "foreign-race" else state["nonce"],
                            },
                        },
                    }
                ]
            ).encode()
        if args == ["start", container]:
            return (container + "\n").encode()
        raise AssertionError(args)

    monkeypatch.setattr(deployment, "run_command", command, raising=False)
    result = CliRunner().invoke(
        cli,
        [
            "worker",
            "deploy",
            "--target",
            "compose",
            "--image",
            image,
            "--project",
            "owned-project",
            "--context",
            "owned-context",
            "--directory",
            str(directory),
            "--env-file",
            str(env),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == (0 if mode == "success" else 1), result.output
    starts = [call for call in calls if call[3] == "start"]
    assert len(starts) == (1 if mode == "success" else 0)
    assert all("up" not in call and "stop" not in call and "down" not in call for call in calls)
    assert "never-echo" not in result.output


def test_bounded_real_subprocess_times_out_and_limits_output():
    import sys

    from firefly_weave.sdk.deployment import DeploymentError, run_command

    assert run_command([sys.executable, "-c", "print('owned')"], timeout=2, limit=100) == b"owned\n"
    with pytest.raises(DeploymentError, match="prerequisites"):
        run_command([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05, limit=100)
    with pytest.raises(DeploymentError, match="prerequisites"):
        run_command([sys.executable, "-c", "print('secret' * 10000)"], timeout=2, limit=100)
    with pytest.raises(DeploymentError, match="prerequisites"):
        run_command([sys.executable, "-c", "raise ValueError('secret')"], timeout=2, limit=10000)


def test_package_ancestor_swap_cannot_write_outside_requested_tree(package_inputs, tmp_path, monkeypatch):
    import os

    from firefly_weave.sdk import deployment

    parent = tmp_path / "destination-parent"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = tmp_path / "retained-parent"
    original_mkdir = os.mkdir
    swapped = False

    def swap_then_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and str(path).endswith("packaged"):
            swapped = True
            parent.rename(retained)
            parent.symlink_to(outside, target_is_directory=True)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(deployment.os, "mkdir", swap_then_mkdir)
    result = invoke_package(package_inputs, parent / "packaged")
    assert result.exit_code in (0, 1)
    assert swapped
    assert not list(outside.iterdir())
    if result.exit_code == 0:
        assert (retained / "packaged/build.json").is_file()


def test_hash_locked_platform_markers_remain_valid():
    from firefly_weave.sdk.deployment import requirements_bytes

    line = (
        "uvloop==0.22.1 ; platform_python_implementation != 'PyPy' and sys_platform != 'win32' --hash=sha256:"
        + "1" * 64
    )
    assert requirements_bytes(line.encode()) == (line + "\n").encode()


def test_failed_subprocess_retains_only_bounded_private_log(tmp_path):
    import sys

    from firefly_weave.sdk.deployment import DeploymentError, run_command

    log = tmp_path / "failure.log"
    with pytest.raises(DeploymentError):
        run_command([sys.executable, "-c", "print('private' * 10000)"], limit=128, log_path=log)
    assert log.stat().st_size <= 128
    assert log.stat().st_mode & 0o077 == 0


def test_timeout_stops_owned_descendant_after_leader_exits(tmp_path):
    import os
    import signal
    import sys
    import time

    from firefly_weave.sdk.deployment import DeploymentError, run_command

    marker, log = tmp_path / "descendant-finished", tmp_path / "child.log"
    child = "import time,pathlib; time.sleep(.5); pathlib.Path(" + repr(str(marker)) + ").touch()"
    parent = (
        "import subprocess,sys,os; p=subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "]); print(p.pid,flush=True); os._exit(0)"
    )
    try:
        with pytest.raises(DeploymentError):
            run_command([sys.executable, "-c", parent], timeout=0.1, log_path=log)
        time.sleep(0.6)
        assert not marker.exists(), "Owned descendant survived the bounded command timeout"
    finally:
        if log.exists() and log.read_text().strip().isdigit():
            with suppress(ProcessLookupError):
                os.kill(int(log.read_text().strip()), signal.SIGKILL)
