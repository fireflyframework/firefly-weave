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
PORTS = "{{json .NetworkSettings.Ports}}"


def published(port):
    return json.dumps({"6443/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}) + "\n"


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
    commands.answer(["inspect", PORTS, "weave-test-control-plane"], published(43123))
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
        "No kind-weave-test context in your kubeconfig. Create the cluster with weave platform cluster create "
        "--provider kind --name weave-test, or pass --kubeconfig."
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
    directory, commands, _ = platform
    commands.answer(["inspect", PORTS, "weave-test-control-plane"], published(server.rsplit(":", 1)[1]))
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
        "weave platform cluster create, which gives each cluster its own network."
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


def test_refuses_a_context_whose_server_is_not_the_nodes_api_port(platform):
    """Same name, other cluster: kind-weave-test points at 127.0.0.1:43123, but the cluster on the platform's
    Docker context publishes its API on 127.0.0.1:50000 (the context was overwritten by a cluster on another VM)."""
    directory, commands, config = platform
    commands.answer(
        ["inspect", PORTS, "weave-test-control-plane"], '{"6443/tcp":[{"HostIp":"127.0.0.1","HostPort":"50000"}]}\n'
    )
    with pytest.raises(PlatformError):
        check(directory, config)
    assert not commands.argv("/opt/weave-tools/kubectl-v1.37.2"), "nothing reaches a cluster it didn't verify"


def test_reads_a_k3d_clusters_api_port_from_its_load_balancer(platform, tmp_path):
    directory, commands, _ = platform
    commands.answer(
        ["ps", "--filter", "label=k3d.cluster=weave-test"], "k3d-weave-test-server-0\nk3d-weave-test-serverlb\n"
    )
    commands.answer(
        ["inspect", "k3d-weave-test-server-0", "k3d-weave-test-serverlb"],
        '{"k3d-weave-test":{}}\n{"k3d-weave-test":{}}\n',
    )
    commands.answer(["ps", "--filter", "network=k3d-weave-test"], "k3d-weave-test-server-0\t\tweave-test\n")
    commands.answer(["inspect", PORTS, "k3d-weave-test-serverlb"], published(6550))
    config = kubeconfig(tmp_path / "k3d", context="k3d-weave-test", server="https://0.0.0.0:6550")
    assert check(directory, config, provider="k3d").network == "k3d-weave-test"


def test_refuses_a_cluster_reached_through_a_proxy(platform, tmp_path):
    directory, commands, _ = platform
    config = kubeconfig(tmp_path / "proxied")
    config.write_text(config.read_text().replace("    server:", "    proxy-url: http://127.0.0.1:3128\n    server:"))
    with pytest.raises(PlatformError, match="proxy-url"):
        check(directory, config)
    assert commands.calls == []


def test_follows_kubectls_merged_view_of_several_files(platform, tmp_path, monkeypatch):
    """The first file that defines a context owns it, as kubectl merges them: a later file never stands in for it,
    and a context whose cluster or user lives in another file is refused rather than passed as one file."""
    directory, commands, _ = platform
    partial = tmp_path / "partial"
    partial.write_text(
        "apiVersion: v1\nkind: Config\ncontexts:\n- name: kind-weave-test\n"
        "  context: {cluster: kind-weave-test, user: kind-weave-test}\n"
    )
    complete = kubeconfig(tmp_path / "complete")
    monkeypatch.setenv("KUBECONFIG", os.pathsep.join([str(partial), str(complete)]))
    with pytest.raises(PlatformError, match="split across several kubeconfig files"):
        check(directory, None)
    assert commands.calls == []


CONNECTED = {
    "target_id": "11111111-2222-3333-4444-555555555555",
    "target_name": "kind-weave-test",
    "pair": 1,
    "generation": "0123456789abcdef0123456789abcdef",
    "state": "connected",
    "runner_principal_id": "21111111-2222-3333-4444-555555555555",
    "worker_principal_id": "31111111-2222-3333-4444-555555555555",
    "runner_linked": True,
    "worker_linked": True,
    "deployment_id": "41111111-2222-3333-4444-555555555555",
    "runner_granted": "11111111-2222-3333-4444-555555555555",
    "worker_granted": "51111111-2222-3333-4444-555555555555",
    "connected_at": "2026-11-12T10:00:00Z",
}


@pytest.mark.parametrize(
    "change",
    [
        {"target_id": 7},
        {"target_id": "x" * 65},
        {"connected_at": ["2026-11-12"]},
        {"state": "lost"},
        {"target_name": "k" * 101},
        {"target_name": ""},
        {"generation": "G" * 32},
        {"generation": "0" * 31},
        {"runner_linked": "yes"},
        {"worker_linked": False},
        {"deployment_id": "deployment"},
        {"worker_granted": 3},
        {"generation": None},
        {"state": "pending", "connected_at": None},
        {"state": None},
        {"connected_at": None},
    ],
)
def test_the_receipt_refuses_target_values_of_the_wrong_type(platform, change):
    directory, _, _ = platform
    clusters.save(
        directory, {"format": clusters.FORMAT, "clusters": {"kind-weave-test": record({"weave-workers": CONNECTED})}}
    )
    target = {key: item for key, item in {**CONNECTED, **change}.items() if item is not None}
    if change == {"state": "pending", "connected_at": None}:
        # A pending target holds what the stages recorded so far; it must still say which pair and generation.
        del target["generation"]
    value = {"format": clusters.FORMAT, "clusters": {"kind-weave-test": record({"weave-workers": target})}}
    with pytest.raises(PlatformError, match="clusters.json"):
        clusters.save(directory, value)


def test_a_rerun_reuses_the_namespaces_pending_pair_and_its_generation(platform):
    """A connect that stopped after taking a pair (a failed probe) finds it again, so the rerun makes no second
    Keycloak client or principal: their idempotency keys carry the same generation."""
    directory, _, _ = platform
    value = {"format": clusters.FORMAT, "clusters": {"kind-weave-test": record({})}}
    first = clusters.reserve(value, "kind-weave-test", "weave-workers")
    assert first == {
        "target_name": "kind-weave-test",
        "pair": 1,
        "generation": first["generation"],
        "state": "pending",
    }
    clusters.save(directory, value)
    again = clusters.receipt(directory)
    assert clusters.reserve(again, "kind-weave-test", "weave-workers") == first
    other = clusters.reserve(again, "kind-weave-test", "team-b")
    assert (other["pair"], other["target_name"]) == (2, "kind-weave-test.team-b")
    assert other["generation"] != first["generation"]


def test_a_freed_pair_taken_again_gets_a_new_generation(platform):
    """A pair freed by disconnect and taken by the next connect gets new principals, never the revoked ones."""
    value = {"format": clusters.FORMAT, "clusters": {"kind-weave-test": record({})}}
    first = clusters.reserve(value, "kind-weave-test", "weave-workers")
    del value["clusters"]["kind-weave-test"]["targets"]["weave-workers"]
    second = clusters.reserve(value, "kind-weave-test", "team-b")
    assert second["pair"] == first["pair"] and second["generation"] != first["generation"]


def test_the_receipt_refuses_a_context_that_is_not_its_provider_and_cluster(platform):
    directory, _, _ = platform
    value = {"format": clusters.FORMAT, "clusters": {"kind-weave-test": record({}, cluster="other")}}
    with pytest.raises(PlatformError, match="clusters.json"):
        clusters.save(directory, value)
    clusters.save(directory, {"format": clusters.FORMAT, "clusters": {"kind-weave-test": record({})}})


def record(targets, *, cluster="weave-test"):
    return {
        "provider": "kind",
        "cluster": cluster,
        "context": "kind-weave-test",
        "docker_context": "colima-weave-tests",
        "network": "weave-kind-weave-test",
        "subnet": "172.30.0.0/16",
        "api_address": "172.30.0.5",
        "registry_address": "172.30.0.6",
        "targets": targets,
    }
