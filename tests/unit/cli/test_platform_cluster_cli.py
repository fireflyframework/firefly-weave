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

"""weave platform cluster create and delete: one single-node test cluster on its own network, from pinned tools,
never filling the shared Docker disk and never deleting a cluster the command didn't create."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fake_commands import FakeCommands

from firefly_weave.cli.main import cli
from firefly_weave.sdk import platform_cluster_tools as tools
from firefly_weave.sdk import platform_clusters as clusters
from firefly_weave.sdk.platform import PlatformError

GIB = 1024**3
NAME = "weave-o4-abc123"
ENDPOINT = "unix:///Users/me/.colima/weave-tests/docker.sock"


def df(free_gib):
    available = int(free_gib * GIB / 1024)
    return f"Filesystem 1024-blocks Used Available Capacity Mounted on\noverlay 15728640 1 {available} 50% /\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """An existing platform folder, pinned tools that need no download, and a Docker that answers like Colima."""
    directory = tmp_path / "platform"
    directory.mkdir(mode=0o700)
    commands = FakeCommands(directory)
    configs = {}

    def created(call):
        for flag in ("--config", "--registry-config"):
            if flag in call.argv:
                configs[flag] = Path(call.argv[call.argv.index(flag) + 1]).read_text()
        return ""

    commands.answer(
        ["docker", "context", "inspect", "colima-weave-tests"],
        json.dumps([{"Endpoints": {"docker": {"Host": ENDPOINT}}}]),
    )
    commands.answer(["docker", "run", "df"], df(8))
    commands.answer(["docker", "ps", "--all"], "")
    commands.answer(["create"], created)
    commands.answer(["wait", "--for=condition=Ready"], "node/weave-o4-abc123-control-plane condition met\n")
    commands.answer(["delete"], "")
    commands.answer(["docker", "network", "ls"], "")
    clock = {"now": 0.0}
    monkeypatch.setattr(clusters.local, "_run", commands)
    monkeypatch.setattr(clusters, "clock", lambda: clock["now"])
    monkeypatch.setattr(clusters, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    monkeypatch.setattr(clusters, "free_port", lambda: 43123)
    monkeypatch.setattr(clusters.tools, "tool", lambda path, name: tools._path(path, name))
    return directory, commands, configs


def invoke(directory, action, provider="kind", *extra, output="json"):
    argv = ["platform", "--directory", str(directory), "cluster", action, "--provider", provider, "--name", NAME]
    argv += ["--context", "colima-weave-tests", "--output", output, *extra]
    return CliRunner().invoke(cli, argv)


def create(directory, provider="kind", *extra, output="json"):
    return invoke(directory, "create", provider, "--no-connect", *extra, output=output)


def calls(commands, *tokens):
    return [call for call in commands.calls if all(token in call.argv for token in tokens)]


def test_the_installed_weave_command_has_cluster_create_and_delete():
    from importlib.metadata import entry_points

    (weave,) = [item for item in entry_points(group="console_scripts") if item.name == "weave"]
    assert weave.load() is cli
    group = cli.commands["platform"].commands["cluster"]
    assert {"create", "delete"} <= set(group.commands)
    help_text = CliRunner().invoke(cli, ["platform", "cluster", "create", "--help"]).output
    for flag in ("--provider", "--name", "--no-connect", "--context", "--output"):
        assert flag in help_text


def test_create_refuses_when_the_docker_disk_has_less_than_6_gib_free(world):
    directory, commands, _ = world
    commands.answer(["docker", "run", "df"], df(5))
    result = create(directory)
    assert result.exit_code != 0
    assert (
        "Docker has 5 GiB free; a cluster needs 6 GiB. Free space without pruning other projects' images, "
        "or use another context."
    ) in result.output
    assert not calls(commands, "create")
    (check,) = calls(commands, "df")
    assert check.argv[:4] == ["docker", "--context", "colima-weave-tests", "run"] and "none" in check.argv


def test_kind_clusters_get_their_own_network_and_the_registry_configuration(world):
    directory, commands, configs = world
    assert create(directory).exit_code == 0
    (call,) = calls(commands, "create", "cluster")
    assert call.argv[1:5] == ["create", "cluster", "--name", NAME]
    assert call.argv[call.argv.index("--image") + 1] == tools.KIND_NODE_IMAGE
    assert call.env["KIND_EXPERIMENTAL_DOCKER_NETWORK"] == f"weave-kind-{NAME}"
    assert call.env["DOCKER_CONTEXT"] == "colima-weave-tests"
    assert '[plugins."io.containerd.grpc.v1.cri".registry]' in configs["--config"]
    assert 'config_path = "/etc/containerd/certs.d"' in configs["--config"]
    assert not list(directory.glob(".cluster-*")), "the configuration file is removed after use"


def test_k3d_clusters_bind_their_api_to_loopback_and_disable_what_the_tests_do_not_need(world):
    directory, commands, configs = world
    result = create(directory, "k3d")
    assert result.exit_code == 0 and json.loads(result.output)["network"] == f"k3d-{NAME}"
    (call,) = calls(commands, "cluster", "create", NAME)
    argv = call.argv
    assert argv[1:4] == ["cluster", "create", NAME]
    assert argv[argv.index("--image") + 1] == tools.K3S_IMAGE
    assert argv[argv.index("--api-port") + 1] == "127.0.0.1:43123"
    assert argv[argv.index("--servers") + 1] == "1" and argv[argv.index("--agents") + 1] == "0"
    disabled = [argv[index + 1] for index, item in enumerate(argv) if item == "--k3s-arg"]
    assert disabled == [
        f"--disable={name}@server:0" for name in ("traefik", "servicelb", "metrics-server", "local-storage")
    ]
    assert call.env["DOCKER_HOST"] == ENDPOINT
    assert configs["--registry-config"].strip() == "mirrors: {}"


def test_create_prints_the_cluster_context_and_network_as_one_json_line_or_a_sentence(world):
    directory, _, _ = world
    result = create(directory)
    assert result.exit_code == 0 and result.output.count("\n") == 1
    assert json.loads(result.output) == {"cluster": NAME, "context": f"kind-{NAME}", "network": f"weave-kind-{NAME}"}
    text = create(directory, output="text")
    assert text.output.strip() == (
        f"Created kind cluster {NAME}: kubeconfig context kind-{NAME}, Docker network weave-kind-{NAME}."
    )


def test_create_without_no_connect_refuses_until_connecting_exists(world):
    directory, commands, _ = world
    result = invoke(directory, "create")
    assert result.exit_code != 0 and "--no-connect" in result.output
    assert commands.calls == []


def test_a_failed_wait_deletes_the_cluster_this_run_created(world):
    directory, commands, _ = world
    commands.answer(["wait", "--for=condition=Ready"], PlatformError("not ready"))
    result = create(directory)
    assert result.exit_code != 0
    assert "The cluster's nodes weren't Ready within 180 s; the cluster was deleted." in result.output
    assert calls(commands, "delete", "cluster", "--name", NAME)
    for call in calls(commands, "wait"):
        assert call.argv[1:3] == ["--context", f"kind-{NAME}"]


def test_a_failed_create_never_deletes_a_cluster(world):
    """kind and k3d remove what a failed create made; the command deletes only a cluster it created."""
    directory, commands, _ = world
    commands.answer(["create"], PlatformError('node(s) already exist for a cluster with the name "weave-o4-abc123"'))
    assert create(directory).exit_code != 0
    assert calls(commands, "create") and not calls(commands, "delete")


@pytest.mark.parametrize(("provider", "label"), [("kind", "io.x-k8s.kind.cluster"), ("k3d", "k3d.cluster")])
def test_create_refuses_a_cluster_name_that_already_exists_on_the_context(world, provider, label):
    directory, commands, _ = world
    commands.answer(["docker", "ps", "--all", f"label={label}={NAME}"], f"{NAME}-control-plane\n")
    result = create(directory, provider)
    assert result.exit_code != 0
    assert f"A cluster named {NAME} already exists on context colima-weave-tests; nothing was changed." in result.output
    assert not calls(commands, "create") and not calls(commands, "delete")


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (PlatformError("not ready"), "The cluster's nodes weren't Ready within 180 s"),
        (KeyboardInterrupt(), "The readiness wait was interrupted"),
    ],
)
def test_a_failed_cleanup_says_to_run_delete(world, failure, reason):
    directory, commands, _ = world
    commands.answer(["wait", "--for=condition=Ready"], failure)
    commands.answer(["delete"], PlatformError("delete failed"))
    result = create(directory)
    assert result.exit_code != 0 and calls(commands, "delete", "cluster", "--name", NAME)
    assert "was deleted" not in result.output
    assert (
        f"{reason}, and deleting the cluster failed; run weave platform cluster delete with the same provider and "
        "name to remove it."
    ) in result.output


def test_the_tools_keep_the_docker_configuration_but_no_other_docker_setting(world, monkeypatch):
    directory, commands, _ = world
    monkeypatch.setenv("DOCKER_CONFIG", "/Users/me/.docker-weave")
    monkeypatch.setenv("DOCKER_HOST", "tcp://elsewhere:2375")
    create(directory)
    (call,) = calls(commands, "create", "cluster")
    assert call.env["DOCKER_CONFIG"] == "/Users/me/.docker-weave" and "DOCKER_HOST" not in call.env


def test_a_context_without_a_docker_endpoint_is_refused_in_one_sentence(world):
    directory, commands, _ = world
    commands.answer(["docker", "context", "inspect", "colima-weave-tests"], '[{"Endpoints": {}}]')
    result = create(directory, "k3d")
    assert result.exit_code != 0 and not calls(commands, "create")
    assert "Docker context colima-weave-tests has no Docker endpoint k3d can use; nothing was changed." in result.output


def test_delete_deletes_the_cluster_and_reports_it(world):
    directory, commands, _ = world
    result = invoke(directory, "delete", "k3d")
    assert result.exit_code == 0
    assert json.loads(result.output) == {"cluster": NAME, "disconnected": [], "deleted": True}
    (call,) = calls(commands, "cluster", "delete", NAME)
    assert call.env["DOCKER_HOST"] == ENDPOINT


def test_the_command_uses_the_pinned_tools(world):
    directory, commands, _ = world
    create(directory)
    used = {Path(call.argv[0]) for call in commands.calls if call.argv[0] != "docker"}
    assert used == {
        directory / "tools" / f"kind-{tools.TOOLS['kind'].version}",
        directory / "tools" / f"kubectl-{tools.TOOLS['kubectl'].version}",
    }


def test_a_bad_name_refuses_before_any_command(world):
    directory, commands, _ = world
    result = CliRunner().invoke(
        cli,
        [
            "platform",
            "--directory",
            str(directory),
            "cluster",
            "create",
            "--provider",
            "kind",
            "--name",
            "Weave_Test",
            "--no-connect",
            "--context",
            "colima-weave-tests",
        ],
    )
    assert result.exit_code != 0 and "Use --provider kind or k3d" in result.output and commands.calls == []


def test_the_docker_context_comes_from_the_installation_or_must_be_given(world, tmp_path):
    directory, commands, _ = world
    (directory / "platform.json").write_text(json.dumps({"context": "colima-weave-tests"}))
    (directory / "platform.json").chmod(0o600)
    result = CliRunner().invoke(
        cli,
        [
            "platform",
            "--directory",
            str(directory),
            "cluster",
            "create",
            "--provider",
            "kind",
            "--name",
            NAME,
            "--no-connect",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls(commands, "create")[0].env["DOCKER_CONTEXT"] == "colima-weave-tests"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = CliRunner().invoke(
        cli,
        [
            "platform",
            "--directory",
            str(elsewhere),
            "cluster",
            "create",
            "--provider",
            "kind",
            "--name",
            NAME,
            "--no-connect",
        ],
    )
    assert result.exit_code != 0 and "pass --context" in result.output


def test_create_never_makes_the_installation_folder(world, tmp_path):
    """weave platform setup refuses a folder that exists, so creating a cluster first must not make it."""
    _, commands, _ = world
    missing = tmp_path / "not-yet"
    result = create(missing)
    assert result.exit_code != 0 and "weave platform up" in result.output
    assert not missing.exists() and commands.calls == []


def test_deleting_a_kind_cluster_also_removes_its_own_network(world):
    """kind leaves the network it made for KIND_EXPERIMENTAL_DOCKER_NETWORK behind; k3d removes its own."""
    directory, commands, _ = world
    commands.answer(["docker", "network", "ls"], f"weave-kind-{NAME}\n")
    commands.answer(["docker", "network", "rm"], "")
    assert invoke(directory, "delete").exit_code == 0
    (removed,) = calls(commands, "network", "rm")
    assert removed.argv == ["docker", "--context", "colima-weave-tests", "network", "rm", f"weave-kind-{NAME}"]
    commands.calls.clear()
    commands.answer(["docker", "network", "ls"], "")
    assert invoke(directory, "delete").exit_code == 0
    assert not calls(commands, "network", "rm")
