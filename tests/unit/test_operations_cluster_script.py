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

"""The scripted test cluster: one single-node cluster on its own network, from pinned tools, never filling the VM."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from firefly_weave.sdk import platform_cluster_tools as tools

ROOT = Path(__file__).resolve().parents[2]
GIB = 1024**3


@pytest.fixture
def script(monkeypatch):
    spec = importlib.util.spec_from_file_location("operations_cluster", ROOT / "scripts/operations-cluster.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "operations_cluster", module)
    spec.loader.exec_module(module)
    return module


class Recorder:
    """Stands in for run_command: records argv, the environment and any --config file's text at call time."""

    def __init__(self, free_gib=8.0, ready=True):
        self.calls, self.configs, self.free_gib, self.ready = [], {}, free_gib, ready

    def __call__(self, argv, *, env=None, timeout=30, **_):
        self.calls.append((list(argv), dict(env or {})))
        for flag in ("--config", "--registry-config"):
            if flag in argv:
                self.configs[flag] = Path(argv[argv.index(flag) + 1]).read_text()
        if "context" in argv and "inspect" in argv:
            return json.dumps(
                [{"Endpoints": {"docker": {"Host": "unix:///Users/me/.colima/weave-tests/docker.sock"}}}]
            ).encode()
        if "df" in argv:
            available = int(self.free_gib * GIB / 1024)
            heading = "Filesystem 1024-blocks Used Available Capacity Mounted on"
            return f"{heading}\noverlay 15728640 1 {available} 50% /\n".encode()
        if "nodes" in argv:
            return b"node-1\tTrue\n" if self.ready else b"node-1\tFalse\n"
        return b""

    def find(self, *tokens):
        return [argv for argv, _ in self.calls if all(token in argv for token in tokens)]


@pytest.fixture
def world(script, tmp_path, monkeypatch):
    recorder = Recorder()
    clock = {"now": 0.0}
    monkeypatch.setattr(script, "run", recorder)
    monkeypatch.setattr(script, "clock", lambda: clock["now"])
    monkeypatch.setattr(script, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    # The pinned tool's path, without downloading it.
    monkeypatch.setattr(script.tools, "tool", lambda directory, name: tools._path(directory, name))
    monkeypatch.setattr(script, "free_port", lambda: 43123)
    return recorder, tmp_path / "platform"


def run(script, capsys, *argv):
    code = script.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def up(script, capsys, directory, provider="kind", *extra):
    return run(
        script,
        capsys,
        "up",
        "--provider",
        provider,
        "--name",
        "weave-o4-abc123",
        "--no-connect",
        "--context",
        "colima-weave-tests",
        "--directory",
        str(directory),
        *extra,
    )


def test_up_refuses_when_the_docker_disk_has_less_than_6_gib_free(script, world, capsys):
    recorder, directory = world
    recorder.free_gib = 5.0
    code, out, err = up(script, capsys, directory)
    assert code == 1 and out == ""
    assert err.strip() == (
        "Docker has 5 GiB free; a cluster needs 6 GiB. Free space without pruning other projects' images, "
        "or use another context."
    )
    assert not recorder.find("create")
    (df,) = recorder.find("df")
    assert df[:4] == ["docker", "--context", "colima-weave-tests", "run"] and "--network" in df and "none" in df


def test_kind_clusters_get_their_own_network_and_the_registry_configuration(script, world, capsys):
    recorder, directory = world
    code, _, _ = up(script, capsys, directory)
    assert code == 0
    ((argv, env),) = [(argv, env) for argv, env in recorder.calls if "create" in argv]
    assert argv[1:5] == ["create", "cluster", "--name", "weave-o4-abc123"]
    assert argv[argv.index("--image") + 1] == tools.KIND_NODE_IMAGE
    assert env["KIND_EXPERIMENTAL_DOCKER_NETWORK"] == "weave-kind-weave-o4-abc123"
    assert env["DOCKER_CONTEXT"] == "colima-weave-tests"
    config = recorder.configs["--config"]
    assert '[plugins."io.containerd.grpc.v1.cri".registry]' in config
    assert 'config_path = "/etc/containerd/certs.d"' in config
    assert not list(directory.glob("**/*.yaml")), "the configuration file is removed after use"


def test_k3d_clusters_bind_their_api_to_loopback_and_disable_what_the_tests_do_not_need(script, world, capsys):
    recorder, directory = world
    code, out, _ = up(script, capsys, directory, "k3d")
    assert code == 0 and json.loads(out)["network"] == "k3d-weave-o4-abc123"
    ((argv, env),) = [(argv, env) for argv, env in recorder.calls if "create" in argv]
    assert argv[1:4] == ["cluster", "create", "weave-o4-abc123"]
    assert argv[argv.index("--image") + 1] == tools.K3S_IMAGE
    assert argv[argv.index("--api-port") + 1] == "127.0.0.1:43123"
    assert argv[argv.index("--servers") + 1] == "1" and argv[argv.index("--agents") + 1] == "0"
    disabled = [argv[index + 1] for index, item in enumerate(argv) if item == "--k3s-arg"]
    assert disabled == [
        f"--disable={name}@server:0" for name in ("traefik", "servicelb", "metrics-server", "local-storage")
    ]
    assert env["DOCKER_HOST"] == "unix:///Users/me/.colima/weave-tests/docker.sock"
    assert recorder.configs["--registry-config"].strip() == "mirrors: {}"


def test_no_connect_prints_the_cluster_context_and_network_only(script, world, capsys):
    _, directory = world
    code, out, _ = up(script, capsys, directory)
    assert code == 0
    assert out.count("\n") == 1
    assert json.loads(out) == {
        "cluster": "weave-o4-abc123",
        "context": "kind-weave-o4-abc123",
        "network": "weave-kind-weave-o4-abc123",
    }


def test_down_runs_after_a_failed_up(script, world, capsys):
    recorder, directory = world
    recorder.ready = False
    code, out, err = up(script, capsys, directory)
    assert code == 1 and out == ""
    assert "Ready within 180 s" in err
    assert recorder.find("delete", "cluster", "--name", "weave-o4-abc123")
    waits = recorder.find("get", "nodes")
    assert waits and all("--context" in argv and "kind-weave-o4-abc123" in argv for argv in waits)


def test_down_deletes_the_cluster_and_reports_it(script, world, capsys):
    recorder, directory = world
    code, out, _ = run(
        script,
        capsys,
        "down",
        "--provider",
        "k3d",
        "--name",
        "weave-o4-abc123",
        "--context",
        "colima-weave-tests",
        "--directory",
        str(directory),
    )
    assert code == 0
    assert json.loads(out) == {"cluster": "weave-o4-abc123", "disconnected": [], "deleted": True}
    assert recorder.find("cluster", "delete", "weave-o4-abc123")


def test_the_script_uses_the_pinned_tools(script, world, capsys):
    recorder, directory = world
    up(script, capsys, directory)
    tools_folder = directory / "tools"
    executables = {Path(argv[0]).parent for argv, _ in recorder.calls if argv[0] != "docker"}
    assert executables == {tools_folder}
    assert {Path(argv[0]).name for argv, _ in recorder.calls if argv[0] != "docker"} == {
        f"kind-{tools.TOOLS['kind'].version}",
        f"kubectl-{tools.TOOLS['kubectl'].version}",
    }


def test_a_bad_name_refuses_before_any_command(script, world, capsys):
    recorder, directory = world
    code, _, err = run(
        script,
        capsys,
        "up",
        "--provider",
        "kind",
        "--name",
        "Weave_Test",
        "--no-connect",
        "--context",
        "x",
        "--directory",
        str(directory),
    )
    assert code == 1 and "Use --provider kind or k3d" in err and recorder.calls == []
