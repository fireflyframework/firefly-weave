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

"""Connecting a cluster starts with names that every surface agrees on and a preflight that reaches only local
clusters: the developer's kubeconfig is read, never written, copied or stored."""

import json
import os
from pathlib import Path

import pytest
from fake_commands import FakeCommands

from firefly_weave.sdk import platform_clusters as clusters
from firefly_weave.sdk.platform import PlatformError

ROOT = Path(__file__).resolve().parents[3]
NAMES = json.loads((ROOT / "tests/fixtures/kubernetes/target-names.json").read_text())
KUBECTL = "/opt/weave-tools/kubectl-v1.37.2"
CANARY = "canary-kubeconfig-secret-4f1b"


def kubeconfig(path, *, context="kind-weave-test", server="https://127.0.0.1:43123"):
    path.write_text(
        f"""apiVersion: v1
kind: Config
clusters:
- name: {context}
  cluster:
    server: {server}
    certificate-authority-data: Q0FOQVJZ
users:
- name: {context}
  user:
    token: {CANARY}
contexts:
- name: {context}
  context:
    cluster: {context}
    user: {context}
current-context: {context}
"""
    )
    return path


@pytest.fixture
def platform(tmp_path, monkeypatch):
    """A platform directory with operations on, the pinned kubectl, and a kind cluster on its own network."""
    directory = tmp_path / "platform"
    directory.mkdir(mode=0o700)
    state = {"directory": str(directory), "source": str(tmp_path), "context": "colima-weave-tests", "id": "a" * 24}
    monkeypatch.setattr(clusters.local, "_state", lambda path: state)
    monkeypatch.setattr(clusters.platform_operations, "receipt", lambda path: {"stage": "ready"})
    monkeypatch.setattr(clusters.tools, "tool", lambda path, name: Path(KUBECTL))
    commands = FakeCommands(directory)
    monkeypatch.setattr(clusters.local, "_run", commands)
    commands.answer([KUBECTL, "get", "--raw", "/readyz"], "ok")
    commands.answer(["ps", "--filter", "label=io.x-k8s.kind.cluster=weave-test"], "weave-test-control-plane\n")
    commands.answer(["inspect", "weave-test-control-plane"], '{"weave-kind-weave-test":{"IPAddress":"172.30.0.2"}}\n')
    commands.answer(["ps", "--filter", "network=weave-kind-weave-test"], "weave-test-control-plane\tweave-test\t\n")
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return directory, commands, kubeconfig(tmp_path / "kubeconfig")


def check(directory, config, provider="kind", cluster="weave-test", namespace="weave-workers"):
    return clusters.preflight(directory, provider, cluster, namespace, kubeconfig=config)


@pytest.mark.parametrize("row", NAMES, ids=[f"{row['cluster']}/{row['namespace']}" for row in NAMES])
def test_names_follow_the_shared_table(row):
    assert clusters.cluster_name_problem(row["cluster"]) == row["cluster_problem"]
    assert clusters.namespace_problem(row["namespace"]) == row["namespace_problem"]
    if row["target"] is not None:
        assert clusters.target_name(row["provider"], row["cluster"], row["namespace"]) == row["target"]


def test_bad_names_refuse_before_any_command(platform):
    directory, commands, config = platform
    for provider, cluster, namespace, message in [
        ("docker", "weave-test", "weave-workers", "Use --provider kind or k3d"),
        ("kind", "Weave", "weave-workers", "Use --provider kind or k3d"),
        ("kind", "weave-test", "kube-system", "kube-system is used by Kubernetes"),
    ]:
        with pytest.raises(PlatformError, match=message):
            check(directory, config, provider, cluster, namespace)
    assert commands.calls == []


def test_refuses_a_context_that_is_not_kind_name_or_k3d_name(platform, tmp_path):
    directory, commands, _ = platform
    config = kubeconfig(tmp_path / "other", context="docker-desktop")
    with pytest.raises(PlatformError) as refused:
        check(directory, config)
    assert str(refused.value) == (
        "No kind-weave-test context in your kubeconfig. Create the cluster with python scripts/operations-cluster.py "
        "up --provider kind --name weave-test --no-connect, or pass --kubeconfig."
    )
    assert commands.calls == []


@pytest.mark.parametrize(
    "server", ["https://10.0.0.5:6443", "https://example.com:6443", "https://127.0.0.1.example.com"]
)
def test_refuses_a_server_that_is_not_on_this_computer(platform, tmp_path, server):
    directory, commands, _ = platform
    with pytest.raises(PlatformError) as refused:
        check(directory, kubeconfig(tmp_path / "remote", server=server))
    assert str(refused.value) == (
        "kind-weave-test points at a server that isn't this computer. Weave connects only local kind and k3d clusters."
    )
    assert commands.calls == []


@pytest.mark.parametrize(
    "server", ["https://127.0.0.1:43123", "https://[::1]:6443", "https://localhost:6443", "https://0.0.0.0:6550"]
)
def test_accepts_a_server_on_this_computer(platform, tmp_path, server):
    directory, _, _ = platform
    found = check(directory, kubeconfig(tmp_path / "local", server=server))
    assert (found.context, found.network, found.nodes, found.docker_context) == (
        "kind-weave-test",
        "weave-kind-weave-test",
        ["weave-test-control-plane"],
        "colima-weave-tests",
    )


def test_passes_the_kubeconfig_and_context_on_every_kubectl_call_and_never_writes_kubeconfig_settings(platform):
    directory, commands, config = platform
    check(directory, config)
    calls = commands.argv(KUBECTL)
    assert calls, "preflight checks the cluster answers"
    for argv in calls:
        assert argv[1:5] == ["--kubeconfig", str(config), "--context", "kind-weave-test"], argv
        assert "config" not in argv
    assert config.read_text().count(CANARY) == 1


def test_resolves_the_kubeconfig_as_kubectl_does(platform, tmp_path, monkeypatch):
    directory, _, _ = platform
    empty, holder = tmp_path / "empty", kubeconfig(tmp_path / "holder")
    empty.write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.setenv("KUBECONFIG", os.pathsep.join([str(tmp_path / "missing"), str(empty), str(holder)]))
    assert check(directory, None).kubeconfig == holder
    explicit = kubeconfig(tmp_path / "explicit")
    assert check(directory, explicit).kubeconfig == explicit
    monkeypatch.delenv("KUBECONFIG")
    home = tmp_path / "home" / ".kube"
    home.mkdir(parents=True)
    default = kubeconfig(home / "config")
    assert check(directory, None).kubeconfig == default


def test_refuses_nodes_on_another_docker_context(platform):
    directory, commands, config = platform
    commands.answer(["ps", "--filter", "label=io.x-k8s.kind.cluster=weave-test"], "")
    with pytest.raises(PlatformError) as refused:
        check(directory, config)
    assert str(refused.value) == (
        "The cluster's nodes don't run on Docker context colima-weave-tests, which this platform uses. "
        "Create the cluster on that context."
    )


def test_refuses_a_cluster_network_shared_with_another_cluster(platform):
    directory, commands, config = platform
    commands.answer(
        ["ps", "--filter", "network=weave-kind-weave-test"],
        "weave-test-control-plane\tweave-test\t\nother-control-plane\tother\t\n",
    )
    with pytest.raises(PlatformError) as refused:
        check(directory, config)
    assert str(refused.value) == (
        "The cluster's Docker network also holds another cluster's nodes. Create the cluster with "
        "scripts/operations-cluster.py, which gives each cluster its own network."
    )


def test_refuses_when_operations_are_off(platform, monkeypatch):
    directory, commands, config = platform
    monkeypatch.setattr(clusters.platform_operations, "receipt", lambda path: None)
    with pytest.raises(PlatformError) as refused:
        check(directory, config)
    assert str(refused.value) == (
        "Operations are off in this installation. Run weave platform up without --no-operations, then connect again."
    )
    assert commands.calls == []


def test_the_receipt_holds_no_kubeconfig_path_or_content(platform):
    directory, _, config = platform
    found = check(directory, config)
    value = clusters.receipt(directory)
    value["clusters"]["kind-weave-test"] = clusters.cluster_record(
        found, subnet="172.30.0.0/16", api_address="172.30.0.5", registry_address="172.30.0.6"
    )
    clusters.save(directory, value)
    path = directory / "clusters.json"
    text = path.read_text()
    assert "kubeconfig" not in text and str(config) not in text and CANARY not in text and "Q0FOQVJZ" not in text
    assert clusters.receipt(directory)["clusters"]["kind-weave-test"]["context"] == "kind-weave-test"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    value["clusters"]["kind-weave-test"]["kubeconfig"] = str(config)
    with pytest.raises(PlatformError, match="clusters.json"):
        clusters.save(directory, value)


def test_a_ninth_target_is_refused_and_names_disconnect():
    def receipt(pairs):
        return {
            "format": clusters.FORMAT,
            "clusters": {
                "kind-weave-test": {
                    "targets": {f"weave-workers-{pair}": {"pair": pair} for pair in pairs},
                }
            },
        }

    assert clusters.free_pair(receipt([1, 2, 4])) == 3
    with pytest.raises(PlatformError) as refused:
        clusters.free_pair(receipt(range(1, 9)))
    assert str(refused.value) == (
        "This installation already connects 8 cluster targets. Disconnect one with weave platform cluster "
        "disconnect, then connect again."
    )
