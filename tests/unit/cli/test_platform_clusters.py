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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

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


# Connect

SUBNET = "172.30.0.0/16"
SCOPE = {
    "tenant_id": "0b9f5f4e-0000-4000-8000-000000000001",
    "project_id": "0b9f5f4e-0000-4000-8000-000000000002",
    "environment_id": "0b9f5f4e-0000-4000-8000-000000000003",
}
RELEASE = "0b9f5f4e-0000-4000-8000-000000000004"
WORKER_IMAGE = "127.0.0.1:5001/example-sleep@sha256:" + "a" * 64
RUNNER_ID = "sha256:" + "2" * 64
RUNNER_REF = "127.0.0.1:5001/weave/runner@sha256:" + "b" * 64
KEYCLOAK, REGISTRY = "abcabcabcabc", "defdefdefdef"
UID = "9f0b6a1e-1111-4222-8333-444455556666"
ISSUER = "http://localhost:55202/realms/weave"
PROBES = ("weave-probe-token", "weave-probe-unlabeled", "weave-probe-registry")


class Cluster:
    """The cluster, Docker and the platform as connect sees them: answers, and what each was asked."""

    def __init__(self, commands):
        self.commands = commands
        self.joined = set()
        self.addresses = {KEYCLOAK: "172.30.0.5", REGISTRY: "172.30.0.6"}
        self.extra = []
        self.namespace = ""
        self.exits = dict.fromkeys(PROBES, 0)
        self.pull = None
        self.issuer = ISSUER
        self.worker_secret = False
        self.worker_deployment = False
        self.online = True
        self.api_calls = []
        self.keycloak = []
        self.origins = []
        self.registered_after = None

    def network(self, call):
        containers = {"node" + "0" * 60: {"Name": "weave-test-control-plane", "IPv4Address": "172.30.0.2/16"}}
        names = {KEYCLOAK: "weave-local-keycloak-1", REGISTRY: "registry-1"}
        for short, address in self.addresses.items():
            name = names[short]
            if short in self.joined:
                containers[short + "0" * 52] = {"Name": name, "IPv4Address": address + "/16"}
        for name in self.extra:
            containers[name * 8] = {"Name": name, "IPv4Address": "172.30.0.9/16"}
        return json.dumps(
            [{"IPAM": {"Config": [{"Subnet": SUBNET}, {"Subnet": "fc00:f853::/64"}]}, "Containers": containers}]
        )

    def create_secret(self, call):
        if "weave-worker-identity" in call.argv:
            self.worker_secret = True
        return "secret created"

    def create(self, call):
        if json.loads(call.stdin).get("kind") == "Deployment":
            self.worker_deployment = True
        return "created"

    def pod(self, call):
        name = next(item for item in call.argv if item in PROBES)
        if self.pull:
            waiting = {"state": {"waiting": {"reason": self.pull}}}
            return json.dumps({"status": {"phase": "Pending", "containerStatuses": [waiting]}})
        terminated = {"state": {"terminated": {"exitCode": self.exits[name]}}}
        phase = "Succeeded" if self.exits[name] == 0 else "Failed"
        return json.dumps({"status": {"phase": phase, "containerStatuses": [terminated]}})

    async def invoke(self, operation, **kwargs):
        self.api_calls.append((operation, kwargs))
        if operation == "deployment_targets.create":
            self.registered_after = [call.name for call in self.commands.calls]
        if operation == "deployment_runners.list":
            fresh = datetime.now(UTC) + timedelta(seconds=60)
            return SimpleNamespace(items=[SimpleNamespace(revoked=False, expires_at=fresh)] if self.online else [])
        if operation == "deployment_jobs.read":
            return SimpleNamespace(state="succeeded", observation_id=uuid4())
        if operation == "deployment_observations.read":
            return SimpleNamespace(complete=True, observed_at=datetime(2026, 11, 12, 10, 0, 5, tzinfo=UTC))
        return SimpleNamespace(id=uuid4())

    def count(self, operation):
        return sum(1 for name, _ in self.api_calls if name == operation)


@pytest.fixture
def world(platform, monkeypatch):
    directory, commands, config = platform
    cluster = Cluster(commands)
    clusters.local._state(directory)["ports"] = {"keycloak": 55202}
    operations = {
        "stage": "ready",
        "registry_port": 5001,
        "worker_image": WORKER_IMAGE,
        "runner_image": RUNNER_ID,
        "release_id": RELEASE,
        "scope": SCOPE,
    }
    monkeypatch.setattr(clusters.platform_operations, "receipt", lambda path: operations)

    async def api(state, value, work, transport):
        assert value["scope"] == SCOPE
        return await work(cluster)

    async def keycloak(state, named, transport):
        cluster.keycloak.append(dict(named))
        return {client: str(uuid4()) for client in named}

    def origins(state, name, subnet):
        cluster.origins.append((name, subnet, len(commands.calls)))
        return True

    async def no_wait(seconds):
        return None

    ticks = iter(range(0, 100000, 10))
    monkeypatch.setattr(clusters.platform_operations, "_api", api)
    monkeypatch.setattr(clusters.platform_operations, "_keycloak_clients", keycloak)
    monkeypatch.setattr(clusters.platform_origins, "set_cluster_entries", origins)
    monkeypatch.setattr(clusters, "sleep", lambda seconds: None)
    monkeypatch.setattr(clusters, "clock", lambda: next(ticks))
    monkeypatch.setattr(clusters, "wait_async", no_wait)
    for service, short in (("keycloak", KEYCLOAK), ("registry", REGISTRY)):
        commands.answer(["ps", "--quiet", f"label=com.docker.compose.service={service}"], short + "\n")
    commands.answer(["network", "inspect", "weave-kind-weave-test"], cluster.network)
    commands.answer(["network", "connect"], lambda call: cluster.joined.add(call.argv[-1]) or "")
    commands.answer(["exec", "mkdir"], "")
    commands.answer(["exec", "-i", "cp", "/dev/stdin"], "")
    commands.answer(["tag", RUNNER_ID], "")
    commands.answer(["push"], "")
    commands.answer(["image", "inspect", RUNNER_ID], json.dumps([{"RepoDigests": [RUNNER_REF]}]))
    commands.answer([KUBECTL, "apply", "--server-side"], "")
    commands.answer([KUBECTL, "get", "namespace", "weave-workers", "-o", "json"], lambda call: cluster.namespace)
    commands.answer([KUBECTL, "get", "namespace", "weave-workers", "-o", "jsonpath={.metadata.uid}"], UID)
    commands.answer(
        [KUBECTL, "get", "secret", "weave-worker-identity"],
        lambda call: "secret/weave-worker-identity" if cluster.worker_secret else "",
    )
    commands.answer([KUBECTL, "delete", "secret"], "")
    commands.answer([KUBECTL, "create", "secret", "generic"], cluster.create_secret)
    commands.answer([KUBECTL, "label", "secret"], "")
    commands.answer([KUBECTL, "delete", "pod"], "")
    commands.answer([KUBECTL, "create", "-f", "-"], cluster.create)
    for name in PROBES:
        commands.answer([KUBECTL, "get", "pod", name], cluster.pod)
    commands.answer([KUBECTL, "logs", "pod/weave-probe-token"], lambda call: json.dumps({"iss": cluster.issuer}) + "\n")
    commands.answer(
        [KUBECTL, "get", "deployment", "weave-worker"],
        lambda call: "deployment.apps/weave-worker" if cluster.worker_deployment else "",
    )
    return directory, commands, config, cluster


def connect(case, notice=None):
    directory, _, config, _ = case
    lines = [] if notice is None else notice
    return clusters.connect(directory, "kind", "weave-test", "weave-workers", kubeconfig=config, notice=lines.append)


def applied(commands, since=0):
    """Every object a server-side apply sent, in order."""
    return [item for call in commands.calls[since:] if "apply" in call.argv for item in json.loads(call.stdin)["items"]]


def test_connect_runs_its_stages_in_order_and_registers_the_target_only_after_the_probes_pass(world):
    directory, commands, _, cluster = world
    lines = []
    result = connect(world, lines)
    assert [line.split(":")[0] for line in lines] == list(clusters.CONNECT_STAGES)
    assert "cluster-probe-token" in cluster.registered_after and "cluster-probe-delete" in cluster.registered_after
    (request,) = [kwargs["body"] for name, kwargs in cluster.api_calls if name == "deployment_targets.create"]
    assert (request.name, request.adapter, request.external_identity, request.boundary) == (
        "kind-weave-test",
        "kubernetes",
        UID,
        "weave-workers",
    )
    assert request.capabilities == ["observe", "update", "scale_workers", "restart_workers"]
    assert request.approval_policy.distinct_approver is False
    (deployment,) = [kwargs["body"] for name, kwargs in cluster.api_calls if name == "deployments.create"]
    assert (deployment.name, deployment.components[0].name, deployment.components[0].replicas) == (
        "weave-workers",
        "weave-worker",
        0,
    )
    assert result == {
        "target_id": str(request_id(cluster)),
        "target_name": "kind-weave-test",
        "provider": "kind",
        "cluster": "weave-test",
        "namespace": "weave-workers",
        "context": "kind-weave-test",
        "runner": "online",
        "observed_at": "2026-11-12T10:00:05Z",
    }
    saved = clusters.receipt(directory)["clusters"]["kind-weave-test"]
    assert (saved["api_address"], saved["registry_address"], saved["subnet"]) == ("172.30.0.5", "172.30.0.6", SUBNET)
    assert saved["targets"]["weave-workers"]["state"] == "connected"
    assert saved["targets"]["weave-workers"]["pair"] == 1


def request_id(cluster):
    """The target ID connect recorded: the runner grant names it as its only resource."""
    grants = [kwargs["body"] for name, kwargs in cluster.api_calls if name == "members.grant"]
    return next(grant.resources[0] for grant in grants if grant.role == "deployment_runner")


def test_a_probe_that_reaches_the_platform_unlabeled_stops_connect_and_a_rerun_reuses_its_pair(world):
    directory, commands, _, cluster = world
    cluster.exits["weave-probe-unlabeled"] = 1
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == clusters.POLICY_OFF
    assert cluster.count("deployment_targets.create") == 0
    deleted = [call.argv for call in commands.calls if call.name == "cluster-probe-delete"]
    assert len(deleted) == 1 and set(PROBES) <= set(deleted[0])
    first = clusters.receipt(directory)["clusters"]["kind-weave-test"]["targets"]["weave-workers"]
    assert first["state"] == "pending" and "target_id" not in first
    cluster.exits["weave-probe-unlabeled"] = 0
    connect(world)
    again = clusters.receipt(directory)["clusters"]["kind-weave-test"]["targets"]["weave-workers"]
    assert (again["pair"], again["generation"]) == (first["pair"], first["generation"])
    # The worker's client was made once; the rerun only rotates the runner's secret.
    assert [sorted(named) for named in cluster.keycloak] == [
        ["weave-cluster-runner-1", "weave-cluster-worker-1"],
        ["weave-cluster-runner-1"],
    ]
    assert cluster.count("principals.create") == 2 and cluster.count("deployment_targets.create") == 1


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda cluster: setattr(cluster, "pull", "ImagePullBackOff"), clusters.PROBE_PULL),
        (lambda cluster: setattr(cluster, "issuer", "http://localhost:9999/realms/other"), clusters.OTHER_ISSUER),
        (lambda cluster: cluster.exits.update({"weave-probe-registry": 1}), clusters.POLICY_OFF),
        # A probe that never ran its check (here its init copy failed) proves nothing about the policies.
        (lambda cluster: cluster.exits.update({"weave-probe-registry": -1}), clusters.PROBE_FAILED),
    ],
)
def test_a_failed_probe_stops_connect_before_registering(world, setup, message):
    _, commands, _, cluster = world
    setup(cluster)
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == message
    assert cluster.count("deployment_targets.create") == 0
    assert [call.name for call in commands.calls].count("cluster-probe-delete") == 1


def test_secrets_reach_kubectl_only_on_stdin(world, monkeypatch):
    directory, commands, _, cluster = world
    made = iter(f"canary-client-secret-{index}" for index in range(10))
    monkeypatch.setattr(clusters.secrets, "token_urlsafe", lambda size: next(made))
    lines = []
    connect(world, lines)
    canaries = [f"canary-client-secret-{index}" for index in range(2)]
    created = [call for call in commands.calls if call.argv[-1:] == ["--from-file=client-secret=/dev/stdin"]]
    assert sorted(call.stdin.decode() for call in created) == canaries
    for call in commands.calls:
        assert not any(canary in " ".join([*call.argv, *(call.env or {}).values()]) for canary in canaries)
        if call not in created:
            assert not any(canary.encode() in (call.stdin or b"") for canary in canaries)
    for path in [*commands.logs(), directory / "clusters.json"]:
        assert not any(canary in path.read_text() for canary in canaries)
    assert not any(canary in line for line in lines for canary in canaries)
    # A reconnect rotates the runner's secret only.
    before = len(commands.calls)
    connect(world)
    again = [call for call in commands.calls[before:] if call.argv[-1:] == ["--from-file=client-secret=/dev/stdin"]]
    assert [call.stdin.decode() for call in again] == ["canary-client-secret-2"]
    assert "weave-runner-identity-weave-workers" in again[0].argv


def test_reconnecting_never_changes_the_worker_deployment_or_its_secret(world):
    _, commands, _, cluster = world
    connect(world)
    runner = [item for item in applied(commands) if item["metadata"]["name"] == "weave-runner-weave-workers"]
    before = len(commands.calls)
    connect(world)
    later = commands.calls[before:]
    creates = [call for call in later if call.argv[-3:] == ["create", "-f", "-"]]
    assert creates and not [call for call in creates if json.loads(call.stdin).get("kind") == "Deployment"]
    assert not [item for item in applied(commands, before) if item["metadata"]["name"] == "weave-worker"]
    assert not [call for call in later if "weave-worker-identity" in call.argv and "get" not in call.argv]
    again = [item for item in applied(commands, before) if item["metadata"]["name"] == "weave-runner-weave-workers"]
    stamp = "firefly.weave/identity-rotated-at"
    first, second = (item[-1]["spec"]["template"]["metadata"]["annotations"][stamp] for item in (runner, again))
    assert first != second


def test_the_clusters_private_origins_are_recorded_before_the_objects_that_carry_them(world):
    _, commands, _, cluster = world
    connect(world)
    ((name, subnet, at),) = cluster.origins
    assert (name, subnet) == ("kind-weave-test", SUBNET)
    assert "cluster-apply-shared" not in [call.name for call in commands.calls[:at]]


def test_only_the_clusters_nodes_and_the_platforms_containers_may_share_its_network(world):
    _, commands, _, cluster = world
    cluster.extra = ["someone-else"]
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == clusters.OTHER_CONTAINERS
    assert not [call for call in commands.calls if "apply" in call.argv]


def test_a_namespace_of_another_installation_is_refused_before_anything_is_applied_to_it(world):
    _, commands, _, cluster = world
    cluster.namespace = json.dumps({"metadata": {"labels": {"firefly.weave/installation": "b" * 24}}})
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert (
        str(refused.value) == "Namespace weave-workers belongs to another Weave installation. Choose another namespace."
    )
    assert "cluster-apply-namespace" not in [call.name for call in commands.calls]


def test_a_silent_runner_names_its_log_and_keeps_the_registered_target(world):
    directory, _, _, cluster = world
    cluster.online = False
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == (
        "The runner didn't make contact within 120 s. Read its log with kubectl --context kind-weave-test "
        "--namespace weave-system logs deployment/weave-runner-weave-workers. The target is registered; run connect "
        "again to retry."
    )
    target = clusters.receipt(directory)["clusters"]["kind-weave-test"]["targets"]["weave-workers"]
    assert target["state"] == "pending" and "target_id" in target


def test_up_repoints_a_connected_cluster_at_the_platforms_new_address(world, monkeypatch):
    directory, commands, config, cluster = world
    connect(world)
    monkeypatch.setenv("KUBECONFIG", str(config))
    lines = []
    before = len(commands.calls)
    assert clusters.refresh(directory, lines.append) == [{"name": "kind-weave-test", "state": "connected"}]
    assert applied(commands, before) == []
    # The platform restarted: keycloak left the cluster network and comes back with another address.
    cluster.joined.discard(KEYCLOAK)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    before = len(commands.calls)
    assert clusters.refresh(directory, lines.append) == [{"name": "kind-weave-test", "state": "connected"}]
    assert [call.argv[-1] for call in commands.calls[before:] if "connect" in call.argv] == [KEYCLOAK]
    items = applied(commands, before)
    (endpoints,) = [item for item in items if item["kind"] == "EndpointSlice"]
    assert endpoints["endpoints"][0]["addresses"] == ["172.30.0.7"]
    egress = [item for item in items if item["metadata"]["name"] == "weave-egress"]
    assert sorted(item["metadata"]["namespace"] for item in egress) == ["weave-system", "weave-workers"]
    for item in egress:
        assert "172.30.0.7/32" in item["spec"]["egress"][0]["to"][1]["ipBlock"]["except"]
    assert clusters.receipt(directory)["clusters"]["kind-weave-test"]["api_address"] == "172.30.0.7"


def test_a_deleted_cluster_reads_missing_and_changes_nothing(world):
    directory, commands, _, _ = world
    connect(world)
    commands.answer(["ps", "--filter", "label=io.x-k8s.kind.cluster=weave-test"], "")
    before = len(commands.calls)
    assert clusters.refresh(directory, lambda line: None) == [{"name": "kind-weave-test", "state": "missing"}]
    assert [call.name for call in commands.calls[before:]] == ["cluster-nodes"]


def test_a_cluster_that_cant_be_refreshed_reads_unreachable_and_says_why(world, monkeypatch):
    directory, _, _, cluster = world
    connect(world)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    monkeypatch.setenv("KUBECONFIG", str(directory / "absent"))
    lines = []
    assert clusters.refresh(directory, lines.append) == [{"name": "kind-weave-test", "state": "unreachable"}]
    assert lines[-1].startswith("kind-weave-test: not refreshed. No kind-weave-test context in your kubeconfig.")
