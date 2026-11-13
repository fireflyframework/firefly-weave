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
from uuid import UUID, uuid4

import pytest
from fake_commands import FakeCommands

from firefly_weave.contracts.public import Problem
from firefly_weave.sdk import platform_clusters as clusters
from firefly_weave.sdk.errors import WeaveError
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
    # Neither weave-system nor the workers namespace exists yet.
    commands.answer([KUBECTL, "get", "namespace", "-o", "json"], "")
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
        # The node containers on the cluster network: full ID to name.
        self.nodes = {"node" + "0" * 60: "weave-test-control-plane"}
        self.addresses = {KEYCLOAK: "172.30.0.5", REGISTRY: "172.30.0.6"}
        self.extra = []
        self.namespace = ""
        self.exits = dict.fromkeys(PROBES, 0)
        self.pull = None
        self.issuer = ISSUER
        self.worker_secret = False
        # The worker Deployment as the cluster holds it, once connect created it.
        self.worker = None
        self.online = True
        self.job_state = "succeeded"
        self.api_calls = []
        self.keycloak = []
        # How many commands had run when each Keycloak call came.
        self.keycloak_at = []
        self.origins = []
        self.registered_after = None
        self.runner_id = uuid4()
        # Keycloak client IDs each disconnect deleted, and the clusters whose private origins it removed.
        self.deleted_clients = []
        self.removed_origins = []
        # The targets and deployments the API lists. As the API does, create refuses a second target for a namespace
        # (adapter, external identity, boundary) it already holds, disabled or not.
        self.targets = []
        self.deployments = []

    def network(self, call):
        containers = {key: {"Name": name, "IPv4Address": "172.30.0.2/16"} for key, name in self.nodes.items()}
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
        document = json.loads(call.stdin)
        if document.get("kind") == "Deployment":
            self.worker = document
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
            body = kwargs["body"]
            held = {(item.adapter, item.external_identity, item.boundary) for item in self.targets}
            if (body.adapter, body.external_identity, body.boundary) in held:
                message = "This target boundary is already registered"
                raise WeaveError(Problem(status=409, code="WV-TARGET-EXISTS", message=message))
        if operation == "deployment_targets.list":
            return SimpleNamespace(items=list(self.targets), next_cursor=None)
        if operation == "deployments.list":
            wanted = kwargs["query"].get("target_id")
            items = [item for item in self.deployments if wanted is None or str(item.target_id) == wanted]
            return SimpleNamespace(items=items, next_cursor=None)
        if operation == "deployment_runners.list":
            fresh = datetime.now(UTC) + timedelta(seconds=60)
            runner = SimpleNamespace(id=self.runner_id, revoked=False, expires_at=fresh)
            return SimpleNamespace(items=[runner] if self.online else [])
        if operation == "deployment_targets.read":
            capabilities = ["observe", "update", "scale_workers", "restart_workers"]
            runner_principal = uuid4()
            return SimpleNamespace(
                id=kwargs["identifier"],
                revision=4,
                disabled=False,
                runner_principal_id=runner_principal,
                capabilities=capabilities,
            )
        if operation == "deployment_jobs.read":
            observation = uuid4() if self.job_state == "succeeded" else None
            return SimpleNamespace(state=self.job_state, observation_id=observation)
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
        cluster.keycloak_at.append(len(commands.calls))
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

    async def delete_clients(state, client_ids, transport):
        cluster.deleted_clients.append(list(client_ids))

    def remove_origins(state, name):
        cluster.removed_origins.append(name)
        return True

    monkeypatch.setattr(clusters.platform_operations, "_delete_keycloak_clients", delete_clients, raising=False)
    monkeypatch.setattr(clusters.platform_origins, "remove_cluster_entries", remove_origins)
    monkeypatch.setattr(clusters, "sleep", lambda seconds: None)
    monkeypatch.setattr(clusters, "clock", lambda: next(ticks))
    monkeypatch.setattr(clusters, "wait_async", no_wait)
    for service, short in (("keycloak", KEYCLOAK), ("registry", REGISTRY)):
        commands.answer(["ps", "--quiet", f"label=com.docker.compose.service={service}"], short + "\n")
    commands.answer(["network", "inspect", "weave-kind-weave-test"], cluster.network)
    commands.answer(["network", "connect"], lambda call: cluster.joined.add(call.argv[-1]) or "")
    commands.answer(["network", "disconnect"], lambda call: cluster.joined.discard(call.argv[-1]) or "")
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
    commands.answer([KUBECTL, "delete", "--ignore-not-found", "-f", "-"], "")
    commands.answer([KUBECTL, "create", "-f", "-"], cluster.create)
    for name in PROBES:
        commands.answer([KUBECTL, "get", "pod", name], cluster.pod)
    commands.answer([KUBECTL, "logs", "pod/weave-probe-token"], lambda call: json.dumps({"iss": cluster.issuer}) + "\n")
    commands.answer(
        [KUBECTL, "get", "deployment", "weave-worker"],
        lambda call: "deployment.apps/weave-worker" if cluster.worker is not None else "",
    )
    commands.answer(
        [KUBECTL, "get", "deployment", "weave-worker", "-o", "json"],
        lambda call: json.dumps(cluster.worker) if cluster.worker is not None else "",
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


def test_connecting_a_disconnected_namespace_again_enables_the_target_it_kept(world):
    """Disconnect can only disable a target and keeps the namespace, and the API never registers one namespace twice:
    connect enables the namespace's own target again for the new runner, with its deployment, and keeps its approval
    policy. A disabled target of another namespace with the same name stays as it is."""
    directory, _, _, cluster = world
    kept, deployment = uuid4(), uuid4()

    def target(identifier, uid, revision):
        return SimpleNamespace(
            id=identifier,
            adapter="kubernetes",
            external_identity=uid,
            boundary="weave-workers",
            disabled=True,
            revision=revision,
            runner_principal_id=uuid4(),
        )

    cluster.targets = [target(uuid4(), "another-cluster-namespace-uid", 2), target(kept, UID, 3)]
    cluster.deployments = [
        SimpleNamespace(id=uuid4(), target_id=cluster.targets[0].id, name="weave-workers"),
        SimpleNamespace(id=deployment, target_id=kept, name="weave-workers"),
    ]
    result = connect(world)
    assert result["target_id"] == str(kept) and request_id(cluster) == str(kept)
    assert cluster.count("deployment_targets.create") == 0 and cluster.count("deployments.create") == 0
    (update,) = [kwargs for name, kwargs in cluster.api_calls if name == "deployment_targets.update"]
    entry = clusters.receipt(directory)["clusters"]["kind-weave-test"]["targets"]["weave-workers"]
    assert (update["identifier"], update["revision"], update["body"].disabled) == (kept, 3, False)
    assert str(update["body"].runner_principal_id) == entry["runner_principal_id"]
    assert update["body"].capabilities == ["observe", "update", "scale_workers", "restart_workers"]
    assert update["body"].approval_policy is None
    assert (entry["target_id"], entry["deployment_id"], entry["state"]) == (str(kept), str(deployment), "connected")


def test_a_connect_that_stopped_after_enabling_the_kept_target_adopts_it_on_the_rerun(world, monkeypatch):
    """The API enabled the kept target for this pair's runner, then connect stopped before recording it: the rerun
    takes that target as it is instead of registering the namespace a second time."""
    directory, _, _, cluster = world
    kept = SimpleNamespace(
        id=uuid4(),
        adapter="kubernetes",
        external_identity=UID,
        boundary="weave-workers",
        disabled=True,
        revision=3,
        runner_principal_id=uuid4(),
    )
    cluster.targets = [kept]
    invoke = cluster.invoke

    async def stopping(operation, **kwargs):
        if operation == "deployments.list":
            raise RuntimeError("connect stopped here")
        return await invoke(operation, **kwargs)

    monkeypatch.setattr(cluster, "invoke", stopping)
    with pytest.raises(RuntimeError):
        connect(world)
    entry = clusters.receipt(directory)["clusters"]["kind-weave-test"]["targets"]["weave-workers"]
    assert "target_id" not in entry and cluster.count("deployment_targets.update") == 1
    # What the update did on the API's side.
    kept.disabled, kept.runner_principal_id = False, UUID(entry["runner_principal_id"])
    monkeypatch.setattr(cluster, "invoke", invoke)
    result = connect(world)
    assert result["target_id"] == str(kept.id) and request_id(cluster) == str(kept.id)
    assert cluster.count("deployment_targets.update") == 1 and cluster.count("deployment_targets.create") == 0


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


def test_a_reconnect_rotates_the_runners_secret_only_once_the_probes_and_registration_pass(world):
    """The probes sign in as the worker, not the runner: a reconnect whose probe fails leaves the running runner and
    its secret (in Keycloak and in weave-system) untouched; one that passes rotates both in the runner stage, together
    with the pod template's stamp."""
    _, commands, _, cluster = world
    connect(world)
    cluster.exits["weave-probe-unlabeled"] = 1
    before = len(commands.calls)
    with pytest.raises(PlatformError):
        connect(world)
    assert len(cluster.keycloak) == 1
    assert not [call for call in commands.calls[before:] if call.name == "cluster-secret"]
    assert not [item for item in applied(commands, before) if item["metadata"]["name"] == "weave-runner-weave-workers"]
    cluster.exits["weave-probe-unlabeled"] = 0
    before = len(commands.calls)
    connect(world)
    assert [sorted(named) for named in cluster.keycloak] == [
        ["weave-cluster-runner-1", "weave-cluster-worker-1"],
        ["weave-cluster-runner-1"],
    ]
    names = [call.name for call in commands.calls[before:]]
    rotated, secret = cluster.keycloak_at[-1] - before, names.index("cluster-secret")
    assert names.index("cluster-probe-delete") < rotated <= secret < names.index("cluster-apply-target")
    assert cluster.registered_after is not None and "cluster-probe-delete" in cluster.registered_after


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


def network_changes(commands, since=0):
    """Each docker network connect or disconnect: (verb, container)."""
    return [
        (call.argv[4], call.argv[-1])
        for call in commands.calls[since:]
        if call.argv[3:5] in (["network", "connect"], ["network", "disconnect"])
    ]


def test_only_the_clusters_nodes_and_the_platforms_containers_may_share_its_network(world):
    """The rule holds as a state, not only as a refusal: the platform's keycloak (whose network namespace the API
    shares) and registry never join a network that holds another container."""
    _, commands, _, cluster = world
    cluster.extra = ["someone-else"]
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == clusters.OTHER_CONTAINERS
    assert network_changes(commands) == []
    assert not [call for call in commands.calls if "apply" in call.argv]


def test_a_container_that_appears_during_the_join_makes_every_platform_container_leave(world):
    """The check after the join takes off what this call joined and what an earlier connect joined alike."""
    _, commands, _, cluster = world
    # keycloak is on the network from an earlier connect; only the registry joins now.
    cluster.joined.add(KEYCLOAK)

    def join(call):
        cluster.joined.add(call.argv[-1])
        cluster.extra = ["someone-else"]
        return ""

    commands.answer(["network", "connect"], join)
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == clusters.OTHER_CONTAINERS
    assert network_changes(commands) == [("connect", REGISTRY), ("disconnect", KEYCLOAK), ("disconnect", REGISTRY)]
    assert cluster.joined == set()
    assert not [call for call in commands.calls if "apply" in call.argv]


def test_a_container_that_cant_leave_the_network_is_named_with_the_command_that_removes_it(world):
    _, commands, _, cluster = world

    def join(call):
        cluster.joined.add(call.argv[-1])
        cluster.extra = ["someone-else"]
        return ""

    commands.answer(["network", "connect"], join)
    commands.answer(["network", "disconnect", KEYCLOAK], PlatformError("Docker refused."))
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == (
        f"{clusters.OTHER_CONTAINERS} weave-kind-weave-test still holds the platform's container {KEYCLOAK}; remove "
        f"it with docker --context colima-weave-tests network disconnect weave-kind-weave-test {KEYCLOAK}."
    )
    assert cluster.joined == {KEYCLOAK}


def test_a_refresh_that_finds_another_container_takes_the_platform_off_the_network(world):
    """A container that joined the cluster network after connect makes refresh refuse, and the platform's keycloak
    (whose network namespace the API shares) and registry, which that earlier connect joined, leave the network."""
    directory, commands, _, cluster = world
    connect(world)
    assert {KEYCLOAK, REGISTRY} <= cluster.joined
    cluster.extra = ["someone-else"]
    lines = []
    before = len(commands.calls)
    assert clusters.refresh(directory, lines.append) == [{"name": "kind-weave-test", "state": "unreachable"}]
    assert lines[-1] == f"kind-weave-test: not refreshed. {clusters.OTHER_CONTAINERS}"
    assert network_changes(commands, before) == [("disconnect", KEYCLOAK), ("disconnect", REGISTRY)]
    assert cluster.joined == set()
    assert applied(commands, before) == []


def test_a_reconnect_that_finds_another_container_takes_the_platform_off_the_network(world):
    _, commands, _, cluster = world
    connect(world)
    cluster.extra = ["someone-else"]
    before = len(commands.calls)
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == clusters.OTHER_CONTAINERS
    assert network_changes(commands, before) == [("disconnect", KEYCLOAK), ("disconnect", REGISTRY)]
    assert cluster.joined == set()
    assert applied(commands, before) == []


FOREIGN = json.dumps({"metadata": {"labels": {"firefly.weave/installation": "b" * 24}}})
# A namespace as connect leaves it: labeled with this installation's ID.
OWN = json.dumps({"metadata": {"labels": {"firefly.weave/installation": "a" * 24}}})
DISCONNECT_ADVICE = (
    "Disconnect it with weave platform cluster disconnect --provider kind --name weave-test --namespace weave-workers, "
    "then connect again."
)
TAKEN_TARGET = (
    "Namespace weave-workers, which this installation connected, now belongs to another Weave installation. "
    + DISCONNECT_ADVICE
)


def test_reconnecting_a_namespace_another_installation_took_says_to_disconnect_it(world):
    _, commands, _, cluster = world
    connect(world)
    cluster.namespace = FOREIGN
    before = len(commands.calls)
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == TAKEN_TARGET
    assert applied(commands, before) == []


def test_a_namespace_of_another_installation_is_refused_before_anything_changes(world):
    _, commands, _, cluster = world
    cluster.namespace = FOREIGN
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert (
        str(refused.value) == "Namespace weave-workers belongs to another Weave installation. Choose another namespace."
    )
    assert not [call for call in commands.calls if "apply" in call.argv]
    assert network_changes(commands) == []


def test_another_installations_weave_system_is_refused_before_anything_changes(world):
    """weave-system carries its installation's label like the workers namespace: the shared apply (with
    --force-conflicts) must never overwrite another installation's EndpointSlice and policies."""
    _, commands, _, cluster = world
    commands.answer([KUBECTL, "get", "namespace", "weave-system", "-o", "json"], FOREIGN)
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == (
        "Namespace weave-system belongs to another Weave installation, which connects this cluster. Create another "
        "cluster with weave platform cluster create."
    )
    assert not [call for call in commands.calls if "apply" in call.argv]
    assert network_changes(commands) == []
    assert cluster.count("deployment_targets.create") == 0


def test_a_ninth_target_is_refused_before_the_cluster_changes(world):
    directory, commands, _, _ = world
    targets = {
        f"ns-{pair}": {**CONNECTED, "pair": pair, "target_name": f"kind-weave-test.ns-{pair}"} for pair in range(1, 9)
    }
    clusters.save(directory, {"format": clusters.FORMAT, "clusters": {"kind-weave-test": record(targets)}})
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value).startswith("This installation already connects 8 cluster targets.")
    assert network_changes(commands) == []
    changes = ("cluster-apply", "cluster-mirror", "cluster-runner", "cluster-secret", "cluster-probe")
    assert [call.name for call in commands.calls if call.name.startswith(changes)] == []
    # A namespace that already holds one of the eight pairs connects again.
    commands.answer([KUBECTL, "get", "namespace", "ns-3", "-o", "jsonpath={.metadata.uid}"], UID)
    clusters.connect(directory, "kind", "weave-test", "ns-3", kubeconfig=world[2], notice=lambda line: None)


K3D_NODES = ("k3d-weave-test-server-0", "k3d-weave-test-serverlb")
K3D_REGISTRIES = "/etc/rancher/k3s/registries.yaml"


@pytest.fixture
def k3d(world, tmp_path):
    """The same platform with a k3d cluster: a server node and its load balancer, which publishes the API."""
    directory, commands, _, cluster = world
    cluster.nodes = {"1" * 64: K3D_NODES[0], "2" * 64: K3D_NODES[1]}
    cluster.registries = "mirrors: {}\n"
    commands.answer(["ps", "--filter", "label=k3d.cluster=weave-test"], "\n".join(K3D_NODES) + "\n")
    commands.answer(["inspect", *K3D_NODES], '{"k3d-weave-test":{}}\n' * 2)
    commands.answer(
        ["ps", "--filter", "network=k3d-weave-test"], "".join(f"{node}\t\tweave-test\n" for node in K3D_NODES)
    )
    commands.answer(["inspect", PORTS, K3D_NODES[1]], published(6550))
    commands.answer(["network", "inspect", "k3d-weave-test"], cluster.network)
    commands.answer(["exec", K3D_NODES[0], "cat", K3D_REGISTRIES], lambda call: cluster.registries)

    def write(call):
        cluster.registries = call.stdin.decode()
        return ""

    commands.answer(["exec", "-i", K3D_NODES[0], "cp", "/dev/stdin", K3D_REGISTRIES], write)
    commands.answer(["restart", K3D_NODES[0]], "")
    commands.answer([KUBECTL, "wait", "--for=condition=Ready", "node", "--all"], "")
    config = kubeconfig(tmp_path / "k3d-kubeconfig", context="k3d-weave-test", server="https://0.0.0.0:6550")
    return directory, commands, config, cluster


def test_connecting_a_k3d_cluster_restarts_its_node_only_when_the_registry_mirror_changes(k3d):
    directory, commands, config, cluster = k3d

    def mirror_calls():
        before = len(commands.calls)
        clusters.connect(directory, "k3d", "weave-test", "weave-workers", kubeconfig=config, notice=lambda line: None)
        return [call for call in commands.calls[before:] if call.name.startswith("cluster-mirror")]

    first = mirror_calls()
    assert [call.name for call in first] == [
        "cluster-mirror-read",
        "cluster-mirror",
        "cluster-mirror-restart",
        "cluster-mirror-ready",
    ]
    assert first[1].stdin == b'mirrors:\n  "127.0.0.1:5001":\n    endpoint:\n      - "http://weave-registry:5000"\n'
    assert first[2].argv[-2:] == ["restart", K3D_NODES[0]]
    assert cluster.count("deployment_targets.create") == 1
    # The node already reads the platform registry's mirror: nothing is written, restarted or waited for.
    assert [call.name for call in mirror_calls()] == ["cluster-mirror-read"]
    assert clusters.receipt(directory)["clusters"]["k3d-weave-test"]["targets"]["weave-workers"]["state"] == (
        "connected"
    )


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


@pytest.mark.parametrize("state", ["failed", "cancelled", "reconciliation_required"])
def test_an_observation_job_that_ends_unfinished_is_named_by_its_state(world, state):
    directory, _, _, cluster = world
    cluster.job_state = state
    with pytest.raises(PlatformError) as refused:
        connect(world)
    assert str(refused.value) == (
        f"The runner's first observation job ended in state {state}. Read the runner's log with kubectl --context "
        "kind-weave-test --namespace weave-system logs deployment/weave-runner-weave-workers. The target is "
        "registered; run connect again to retry."
    )
    assert cluster.count("deployment_jobs.read") == 1
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
    # The cluster's nodes exist, so creating it, as connect's refusal says, isn't the answer.
    assert lines[-1] == (
        "kind-weave-test: not refreshed. No kind-weave-test context in your default kubeconfig. Run weave platform "
        "cluster connect again with the --kubeconfig you connected it with."
    )


def endpoint_addresses(commands, since=0):
    return [item["endpoints"][0]["addresses"] for item in applied(commands, since) if item["kind"] == "EndpointSlice"]


def test_a_refresh_after_an_unreachable_one_still_repoints_the_cluster(world, monkeypatch):
    """The receipt records the platform's new addresses only once the EndpointSlice and the policies carry them, so
    a refresh that couldn't apply leaves the next one something to repair."""
    directory, commands, config, cluster = world
    connect(world)
    cluster.joined.discard(KEYCLOAK)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    monkeypatch.setenv("KUBECONFIG", str(directory / "absent"))
    assert clusters.refresh(directory, lambda line: None) == [{"name": "kind-weave-test", "state": "unreachable"}]
    assert clusters.receipt(directory)["clusters"]["kind-weave-test"]["api_address"] == "172.30.0.5"
    monkeypatch.setenv("KUBECONFIG", str(config))
    before = len(commands.calls)
    assert clusters.refresh(directory, lambda line: None) == [{"name": "kind-weave-test", "state": "connected"}]
    assert endpoint_addresses(commands, before) == [["172.30.0.7"]]
    assert clusters.receipt(directory)["clusters"]["kind-weave-test"]["api_address"] == "172.30.0.7"


def test_a_connect_whose_shared_apply_fails_keeps_the_addresses_the_cluster_still_carries(world, monkeypatch):
    directory, commands, config, cluster = world
    connect(world)
    cluster.joined.discard(KEYCLOAK)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    commands.answer([KUBECTL, "apply", "--server-side"], PlatformError("The API server refused the apply."))
    with pytest.raises(PlatformError):
        connect(world)
    assert clusters.receipt(directory)["clusters"]["kind-weave-test"]["api_address"] == "172.30.0.5"
    commands.answer([KUBECTL, "apply", "--server-side"], "")
    monkeypatch.setenv("KUBECONFIG", str(config))
    before = len(commands.calls)
    assert clusters.refresh(directory, lambda line: None) == [{"name": "kind-weave-test", "state": "connected"}]
    assert endpoint_addresses(commands, before) == [["172.30.0.7"]]


def test_connecting_another_namespace_after_the_platform_moved_repoints_the_clusters_other_targets(world):
    directory, commands, config, cluster = world
    connect(world)
    cluster.joined.discard(KEYCLOAK)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    commands.answer([KUBECTL, "get", "namespace", "team-b", "-o", "jsonpath={.metadata.uid}"], UID)
    before = len(commands.calls)
    clusters.connect(directory, "kind", "weave-test", "team-b", kubeconfig=config, notice=lambda line: None)
    egress = [item for item in applied(commands, before) if item["metadata"]["name"] == "weave-egress"]
    assert {item["metadata"]["namespace"] for item in egress} == {"weave-system", "weave-workers", "team-b"}
    for item in egress:
        assert "172.30.0.7/32" in item["spec"]["egress"][0]["to"][1]["ipBlock"]["except"]
    saved = clusters.receipt(directory)["clusters"]["kind-weave-test"]
    assert saved["api_address"] == "172.30.0.7" and set(saved["targets"]) == {"weave-workers", "team-b"}


def test_connect_never_repoints_another_target_whose_namespace_another_installation_owns(world):
    """Like refresh, connect checks each of the cluster's other targets' installation label before it applies the
    platform's new addresses to them with --force-conflicts, and refuses before anything is applied."""
    directory, commands, config, cluster = world
    connect(world)
    cluster.joined.discard(KEYCLOAK)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    cluster.namespace = FOREIGN
    commands.answer([KUBECTL, "get", "namespace", "team-b", "-o", "jsonpath={.metadata.uid}"], UID)
    before = len(commands.calls)
    with pytest.raises(PlatformError) as refused:
        clusters.connect(directory, "kind", "weave-test", "team-b", kubeconfig=config, notice=lambda line: None)
    assert str(refused.value) == TAKEN_TARGET
    assert applied(commands, before) == []
    saved = clusters.receipt(directory)["clusters"]["kind-weave-test"]
    assert saved["api_address"] == "172.30.0.5" and set(saved["targets"]) == {"weave-workers"}


def test_refresh_applies_only_to_the_cluster_that_publishes_the_contexts_port(world, monkeypatch, tmp_path):
    """Refresh reaches the cluster through the default kubeconfig, so it ties the context's server port to the port
    the cluster's API publishes, as preflight does."""
    directory, commands, _, cluster = world
    connect(world)
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig(tmp_path / "default", server="https://127.0.0.1:9999")))
    cluster.joined.discard(KEYCLOAK)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    lines = []
    before = len(commands.calls)
    assert clusters.refresh(directory, lines.append) == [{"name": "kind-weave-test", "state": "unreachable"}]
    assert applied(commands, before) == []
    assert lines[-1] == (
        "kind-weave-test: not refreshed. kind-weave-test points at port 9999, but the kind cluster weave-test on "
        "Docker context colima-weave-tests publishes its API on 43123. Point the context at that cluster, or use "
        "another cluster name."
    )
    assert clusters.receipt(directory)["clusters"]["kind-weave-test"]["api_address"] == "172.30.0.5"


def test_a_busy_cluster_lock_reads_unreachable_and_never_fails_up(world):
    directory, commands, _, _ = world
    connect(world)
    lines = []
    before = len(commands.calls)
    with clusters.local._exclusive(directory, clusters.LOCK, clusters.BUSY):
        result = clusters.refresh(directory, lines.append)
    assert result == [{"name": "kind-weave-test", "state": "unreachable"}]
    assert lines == [f"clusters: not refreshed. {clusters.BUSY}"]
    assert commands.calls[before:] == []


def test_a_malformed_cluster_receipt_never_fails_up(world):
    directory, commands, _, _ = world
    (directory / "clusters.json").write_text("{not json")
    os.chmod(directory / "clusters.json", 0o600)
    lines = []
    assert clusters.refresh(directory, lines.append) == []
    assert lines == [f"clusters: not refreshed. {clusters.MALFORMED}"]
    assert commands.calls == []


def test_refresh_never_applies_over_another_installations_weave_system(world, monkeypatch):
    directory, commands, config, cluster = world
    connect(world)
    monkeypatch.setenv("KUBECONFIG", str(config))
    commands.answer([KUBECTL, "get", "namespace", "weave-system", "-o", "json"], FOREIGN)
    cluster.joined.discard(KEYCLOAK)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    lines = []
    before = len(commands.calls)
    assert clusters.refresh(directory, lines.append) == [{"name": "kind-weave-test", "state": "unreachable"}]
    assert applied(commands, before) == []
    assert lines[-1].startswith("kind-weave-test: not refreshed. Namespace weave-system belongs to another Weave")


def test_refresh_that_finds_a_target_namespace_taken_says_to_disconnect_it(world, monkeypatch):
    directory, commands, config, cluster = world
    connect(world)
    monkeypatch.setenv("KUBECONFIG", str(config))
    cluster.namespace = FOREIGN
    cluster.joined.discard(KEYCLOAK)
    cluster.addresses[KEYCLOAK] = "172.30.0.7"
    lines = []
    before = len(commands.calls)
    assert clusters.refresh(directory, lines.append) == [{"name": "kind-weave-test", "state": "unreachable"}]
    assert applied(commands, before) == []
    assert lines[-1] == (
        "kind-weave-test: not refreshed. Namespace weave-workers belongs to another Weave installation. "
        + DISCONNECT_ADVICE
    )


# Disconnect


def deleted(commands, since=0):
    """Every object a kubectl delete named: (Kind, namespace or None, name)."""
    return [
        (item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"])
        for call in commands.calls[since:]
        if call.name == "cluster-disconnect"
        for item in json.loads(call.stdin)["items"]
    ]


def disconnect(case, namespace="weave-workers", notice=None):
    directory, _, config, _ = case
    lines = [] if notice is None else notice
    return clusters.disconnect(directory, "kind", "weave-test", namespace, kubeconfig=config, notice=lines.append)


def connect_team_b(case):
    directory, commands, config, _ = case
    commands.answer([KUBECTL, "get", "namespace", "team-b", "-o", "jsonpath={.metadata.uid}"], UID)
    return clusters.connect(directory, "kind", "weave-test", "team-b", kubeconfig=config, notice=lambda line: None)


def test_disconnecting_one_of_two_targets_removes_only_its_own_objects_and_identities(world):
    directory, commands, _, cluster = world
    connect(world)
    before = len(commands.calls)
    connected = connect_team_b(world)
    # What the cluster holds for team-b: every object connect applied or created for it (its probe pods are gone).
    made = {
        (item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"])
        for call in commands.calls[before:]
        if "apply" in call.argv or call.argv[-3:] == ["create", "-f", "-"]
        for item in json.loads(call.stdin).get("items", [json.loads(call.stdin)])
        if item["kind"] != "Pod"
        if "team-b" in (item["metadata"].get("namespace"), item["metadata"]["name"])
        or item["metadata"]["name"].endswith("-team-b")
    }
    calls = len(cluster.api_calls)
    before = len(commands.calls)
    result = disconnect(world, "team-b")
    assert result == {
        "target_id": connected["target_id"],
        "disabled": True,
        "removed": sorted(f"{kind}/{name}" for kind, _, name in deleted(commands, before)),
    }
    names = [name for name, _ in cluster.api_calls[calls:]]
    assert names[:4] == [
        "deployment_runners.list",
        "deployment_runners.revoke",
        "deployment_targets.read",
        "deployment_targets.update",
    ]
    (update,) = [kwargs for name, kwargs in cluster.api_calls[calls:] if name == "deployment_targets.update"]
    assert update["body"].disabled is True and update["revision"] == 4
    statuses = [kwargs["body"].active for name, kwargs in cluster.api_calls[calls:] if name == "principals.status"]
    assert statuses == [False, False]
    assert cluster.deleted_clients == [["weave-cluster-runner-2", "weave-cluster-worker-2"]]
    removed = set(deleted(commands, before))
    # Every object of team-b goes, but never its namespace or the default account Kubernetes keeps in it.
    assert removed | {("Namespace", None, "team-b"), ("ServiceAccount", "team-b", "default")} >= made
    assert ("Namespace", None, "team-b") not in removed
    assert ("Secret", "weave-system", "weave-runner-identity-team-b") in removed
    assert ("Secret", "team-b", "weave-worker-identity") in removed
    assert not [item for item in removed if "weave-workers" in (item[1], item[2]) or item[2].endswith("-weave-workers")]
    assert ("Service", "weave-system", "weave-api") not in removed
    assert network_changes(commands, before) == [] and cluster.removed_origins == []
    saved = clusters.receipt(directory)["clusters"]["kind-weave-test"]
    assert set(saved["targets"]) == {"weave-workers"}


def test_disconnecting_the_last_target_removes_the_shared_parts_but_never_the_workers_namespace(world):
    directory, commands, _, cluster = world
    connect(world)
    commands.answer([KUBECTL, "get", "namespace", "weave-system", "-o", "json"], OWN)
    before = len(commands.calls)
    disconnect(world)
    removed = set(deleted(commands, before))
    assert {("Namespace", None, "weave-system"), ("Service", "weave-system", "weave-api")} <= removed
    assert ("EndpointSlice", "weave-system", "weave-api-platform") in removed
    assert ("Namespace", None, "weave-workers") not in removed
    assert not [call for call in commands.calls[before:] if "delete" in call.argv and "weave-workers" in call.argv]
    assert network_changes(commands, before) == [("disconnect", KEYCLOAK), ("disconnect", REGISTRY)]
    assert cluster.removed_origins == ["kind-weave-test"]
    assert clusters.receipt(directory)["clusters"] == {}


@pytest.mark.parametrize("labels", [{}, {"app.kubernetes.io/name": "someone-else"}])
def test_the_last_disconnect_keeps_a_weave_system_without_this_installations_label(world, labels):
    """An unlabeled weave-system may hold another tool's objects: this installation's shared objects go by name, and
    the namespace, which would take everything in it, stays."""
    directory, commands, _, _ = world
    connect(world)
    unlabeled = json.dumps({"metadata": {"name": "weave-system", "labels": labels}})
    commands.answer([KUBECTL, "get", "namespace", "weave-system", "-o", "json"], unlabeled)
    before = len(commands.calls)
    result = disconnect(world)
    removed = deleted(commands, before)
    assert ("Namespace", None, "weave-system") not in removed
    assert {("Service", "weave-system", "weave-api"), ("EndpointSlice", "weave-system", "weave-api-platform")} <= set(
        removed
    )
    assert "Namespace/weave-system" not in result["removed"]
    assert clusters.receipt(directory)["clusters"] == {}


def test_a_container_that_cant_leave_at_the_last_disconnect_is_named_with_the_command_that_removes_it(world):
    directory, commands, _, cluster = world
    connect(world)
    commands.answer(["network", "disconnect", KEYCLOAK], PlatformError("Docker refused."))
    lines = []
    disconnect(world, notice=lines)
    assert lines[-1] == (
        f"network: weave-kind-weave-test still holds the platform's container {KEYCLOAK}; remove it with "
        f"docker --context colima-weave-tests network disconnect weave-kind-weave-test {KEYCLOAK}."
    )
    assert cluster.joined == {KEYCLOAK}
    assert clusters.receipt(directory)["clusters"] == {}


def test_disconnect_frees_a_pending_target_with_its_keycloak_clients_and_principals(world):
    """A connect that stopped after the identities stage left a pending target: no platform target to disable, but
    clients and principals that a later connect of another namespace must not inherit."""
    directory, commands, _, cluster = world
    cluster.exits["weave-probe-unlabeled"] = 1
    with pytest.raises(PlatformError):
        connect(world)
    calls = len(cluster.api_calls)
    result = disconnect(world)
    assert result["target_id"] is None and result["disabled"] is False
    names = [name for name, _ in cluster.api_calls[calls:]]
    assert names == ["principals.status", "principals.status"]
    assert cluster.deleted_clients == [["weave-cluster-runner-1", "weave-cluster-worker-1"]]
    assert clusters.receipt(directory)["clusters"] == {}


def test_disconnect_deletes_nothing_in_a_namespace_another_installation_took(world):
    """The refusal for a taken namespace says to disconnect it: that frees the target here and leaves the other
    installation's objects in the namespace alone."""
    directory, commands, _, cluster = world
    connect(world)
    cluster.namespace = FOREIGN
    before = len(commands.calls)
    disconnect(world)
    removed = deleted(commands, before)
    assert ("Deployment", "weave-system", "weave-runner-weave-workers") in removed
    assert not [item for item in removed if "weave-workers" in (item[1], item[2])]
    assert clusters.receipt(directory)["clusters"] == {}


def test_disconnect_keeps_every_object_of_a_weave_system_another_installation_owns(world):
    """Another installation's weave-system keeps its objects and the cluster-wide ones; this target's workers
    namespace, which is still this installation's, loses only this target's objects."""
    directory, commands, _, cluster = world
    connect(world)
    commands.answer([KUBECTL, "get", "namespace", "weave-system", "-o", "json"], FOREIGN)
    before = len(commands.calls)
    result = disconnect(world)
    removed = deleted(commands, before)
    assert not [item for item in removed if item[1] in {"weave-system", None}]
    assert ("Deployment", "weave-workers", "weave-worker") in removed
    assert result["disabled"] is True and cluster.count("deployment_runners.revoke") == 1
    assert clusters.receipt(directory)["clusters"] == {}


def test_disconnecting_a_cluster_whose_nodes_are_gone_frees_the_target_without_reaching_it(world):
    """A deleted cluster has nothing left to delete: the platform still revokes, disables, deletes the clients, leaves
    the network kind left behind, and frees the pair."""
    directory, commands, _, cluster = world
    connected = connect(world)
    commands.answer(["ps", "--filter", "label=io.x-k8s.kind.cluster=weave-test"], "")
    before, calls = len(commands.calls), len(cluster.api_calls)
    result = disconnect(world)
    assert result == {"target_id": connected["target_id"], "disabled": True, "removed": []}
    assert not [call for call in commands.calls[before:] if call.argv[0] == KUBECTL]
    names = [name for name, _ in cluster.api_calls[calls:]]
    assert {"deployment_runners.revoke", "deployment_targets.update"} <= set(names)
    assert cluster.deleted_clients == [["weave-cluster-runner-1", "weave-cluster-worker-1"]]
    assert network_changes(commands, before) == [("disconnect", KEYCLOAK), ("disconnect", REGISTRY)]
    assert cluster.removed_origins == ["kind-weave-test"]
    assert clusters.receipt(directory)["clusters"] == {}


def test_disconnect_revokes_the_runner_and_disables_the_target_before_it_deletes_anything_in_the_cluster(world):
    """The platform stops trusting the runner before its objects go, so it never acts on a half-deleted target."""
    _, commands, _, cluster = world
    connect(world)
    original, asked = cluster.invoke, {}

    async def invoke(operation, **kwargs):
        # How many commands had run when the platform was asked.
        asked[operation] = len(commands.calls)
        return await original(operation, **kwargs)

    cluster.invoke = invoke
    before = len(commands.calls)
    disconnect(world)
    (delete,) = [index for index, call in enumerate(commands.calls) if call.name == "cluster-disconnect"]
    assert before <= asked["deployment_runners.revoke"] <= delete
    assert asked["deployment_targets.update"] <= delete


@pytest.mark.parametrize("silent", ["no published port", "no context", "no API"])
def test_deleting_a_cluster_that_doesnt_answer_still_frees_its_targets_and_deletes_it(world, monkeypatch, silent):
    """The cluster goes with every object in it, so delete never reaches it: stopped nodes publish no ports, the
    default kubeconfig may lack the context, and the API may not answer. The platform's side is still undone."""
    directory, commands, config, cluster = world
    connect(world)
    monkeypatch.setenv("KUBECONFIG", str(config))
    if silent == "no published port":
        commands.answer(["inspect", PORTS, "weave-test-control-plane"], "{}\n")
    elif silent == "no context":
        monkeypatch.setenv("KUBECONFIG", str(config.parent / "another-kubeconfig"))
    else:
        commands.answer([KUBECTL, "get", "namespace", "-o", "json"], PlatformError("Stage cluster-namespace failed."))
    kind = "/opt/weave-tools/kind-v0.33.0"
    monkeypatch.setattr(clusters.tools, "tool", lambda path, name: Path(KUBECTL if name == "kubectl" else kind))
    socket = json.dumps([{"Endpoints": {"docker": {"Host": "unix:///Users/me/.colima/weave-tests/docker.sock"}}}])
    commands.answer(["docker", "context", "inspect", "colima-weave-tests"], socket)
    commands.answer([kind, "delete", "cluster", "--name", "weave-test"], "")
    commands.answer(["network", "ls"], "")
    before = len(commands.calls)
    result = clusters.delete_cluster(directory, "kind", "weave-test", context="colima-weave-tests")
    assert result == {"cluster": "weave-test", "disconnected": ["kind-weave-test"], "deleted": True}
    assert not [call for call in commands.calls[before:] if call.argv[0] == KUBECTL]
    assert (cluster.count("deployment_runners.revoke"), cluster.count("deployment_targets.update")) == (1, 1)
    assert cluster.deleted_clients == [["weave-cluster-runner-1", "weave-cluster-worker-1"]]
    assert network_changes(commands, before) == [("disconnect", KEYCLOAK), ("disconnect", REGISTRY)]
    assert cluster.removed_origins == ["kind-weave-test"]
    assert clusters.receipt(directory)["clusters"] == {}
    (deleted_at,) = [index for index, call in enumerate(commands.calls) if call.argv[:3] == [kind, "delete", "cluster"]]
    left = [index for index, call in enumerate(commands.calls) if call.argv[3:5] == ["network", "disconnect"]]
    assert deleted_at > max(left)


@pytest.mark.parametrize("command", ["disconnect", "--check"])
def test_a_missing_context_says_to_pass_the_kubeconfig_that_reaches_the_cluster(world, tmp_path, command):
    """The cluster's nodes exist, so creating it, as connect's refusal says, isn't the answer."""
    directory, commands, _, cluster = world
    connect(world)
    elsewhere = kubeconfig(tmp_path / "elsewhere", context="kind-another")
    before, calls = len(commands.calls), len(cluster.api_calls)
    with pytest.raises(PlatformError) as refused:
        if command == "disconnect":
            clusters.disconnect(
                directory, "kind", "weave-test", "weave-workers", kubeconfig=elsewhere, notice=lambda line: None
            )
        else:
            clusters.clusters(directory, check=True, kubeconfig=elsewhere)
    assert str(refused.value) == (
        f"No kind-weave-test context in your kubeconfig, so {command} can't reach the cluster. Pass --kubeconfig with "
        "the file kind wrote. Nothing was changed."
    )
    assert not [call for call in commands.calls[before:] if call.argv[0] == KUBECTL]
    assert not [name for name, _ in cluster.api_calls[calls:] if name != "deployment_runners.list"]
    assert set(clusters.receipt(directory)["clusters"]["kind-weave-test"]["targets"]) == {"weave-workers"}


def test_disconnecting_a_namespace_without_a_target_changes_nothing(world):
    directory, commands, _, cluster = world
    connect(world)
    before, calls = len(commands.calls), len(cluster.api_calls)
    with pytest.raises(PlatformError) as refused:
        disconnect(world, "team-b")
    assert str(refused.value) == "kind-weave-test has no target in namespace team-b; nothing was changed."
    assert commands.calls[before:] == [] and cluster.api_calls[calls:] == []


# List


def test_list_reports_each_clusters_state_and_each_targets_runner_without_reaching_the_cluster(world):
    directory, commands, config, cluster = world
    result = connect(world)
    before = len(commands.calls)
    listed = clusters.clusters(directory, check=False, kubeconfig=config)
    assert listed == {
        "clusters": [
            {
                "provider": "kind",
                "cluster": "weave-test",
                "context": "kind-weave-test",
                "network": "weave-kind-weave-test",
                "state": "connected",
                "targets": [
                    {
                        "target_id": result["target_id"],
                        "target_name": "kind-weave-test",
                        "namespace": "weave-workers",
                        "runner": "online",
                        "drift": [],
                    }
                ],
            }
        ]
    }
    assert not [call for call in commands.calls[before:] if call.argv[0] == KUBECTL]
    cluster.online = False
    commands.answer(["ps", "--filter", "label=io.x-k8s.kind.cluster=weave-test"], "")
    (gone,) = clusters.clusters(directory, check=False, kubeconfig=config)["clusters"]
    assert (gone["state"], gone["targets"][0]["runner"]) == ("missing", "offline")


def test_list_check_names_a_drifted_object_without_printing_its_values(world):
    directory, commands, config, _ = world
    connect(world)
    canary = "canary-drift-value-7c1d"
    rotated = "2026-11-12T10:00:00.000000Z"
    commands.answer([KUBECTL, "get", "deployment", "weave-runner-weave-workers", "-o"], rotated)
    live = "/tmp/LIVE-1/networking.k8s.io.v1.NetworkPolicy.weave-system.weave-egress"
    merged = "/tmp/MERGED-2/networking.k8s.io.v1.NetworkPolicy.weave-system.weave-egress"
    commands.answer([KUBECTL, "diff"], f"Files {live} and {merged} differ\n-  cidr: {canary}\n")
    before = len(commands.calls)
    listed = clusters.clusters(directory, check=True, kubeconfig=config)
    assert listed["clusters"][0]["targets"][0]["drift"] == ["NetworkPolicy/weave-egress"]
    assert canary not in json.dumps(listed)
    (diff,) = [call for call in commands.calls[before:] if "diff" in call.argv]
    # kubectl hands the two folders to `diff -q`, which prints file names only, so no value reaches the log either.
    assert diff.env["KUBECTL_EXTERNAL_DIFF"] == "diff -q"
    sent = {(item["kind"], item["metadata"]["name"]) for item in json.loads(diff.stdin)["items"]}
    assert {("Service", "weave-api"), ("Deployment", "weave-runner-weave-workers")} <= sent
    assert applied(commands, before) == []


@pytest.mark.parametrize(
    ("change", "drift"),
    [
        ("writable root filesystem", ["Deployment/weave-worker"]),
        ("another Secret as environment", ["Deployment/weave-worker"]),
        ("the identity Secret mounted", ["Deployment/weave-worker"]),
        ("deleted", ["Deployment/weave-worker"]),
        ("scaled to a new image and restarted", []),
    ],
)
def test_list_check_compares_only_the_worker_deployments_hardening(world, change, drift):
    """Connect creates the worker once; its replicas, image and restart time then belong to reviewed change requests,
    so only a weakened or missing worker counts as drift."""
    directory, commands, config, cluster = world
    connect(world)
    commands.answer([KUBECTL, "get", "deployment", "weave-runner-weave-workers", "-o"], "2026-11-12T10:00:00.000000Z")
    commands.answer([KUBECTL, "diff"], "")
    template = cluster.worker["spec"]["template"]
    container = template["spec"]["containers"][0]
    canary = "canary-secret-5e2b"
    if change == "writable root filesystem":
        container["securityContext"]["readOnlyRootFilesystem"] = False
    elif change == "another Secret as environment":
        container["envFrom"] = [{"secretRef": {"name": canary}}]
    elif change == "the identity Secret mounted":
        container["volumeMounts"].append({"name": "identity", "mountPath": f"/run/{canary}", "readOnly": True})
    elif change == "deleted":
        cluster.worker = None
    else:
        cluster.worker["spec"]["replicas"] = 3
        container["image"] = "127.0.0.1:5001/example-sleep@sha256:" + "c" * 64
        template["metadata"]["annotations"]["firefly.weave/restarted-at"] = "2026-11-12T11:00:00Z"
        cluster.worker["status"] = {"replicas": 3, "readyReplicas": 3}
    before = len(commands.calls)
    listed = clusters.clusters(directory, check=True, kubeconfig=config)
    assert listed["clusters"][0]["targets"][0]["drift"] == drift
    # Kind and name only: neither the edited field nor what it names reaches the output.
    shown = json.dumps(listed)
    assert not [word for word in ("readOnlyRootFilesystem", "envFrom", "volumeMounts", canary) if word in shown]
    assert applied(commands, before) == [] and not [call for call in commands.calls[before:] if "create" in call.argv]


REPAIR = (
    "kubectl --context kind-weave-test --namespace weave-workers delete deployment/weave-worker "
    "secret/weave-worker-identity"
)


def test_the_guides_worker_repair_brings_the_worker_back_with_a_new_secret(world):
    """Connect creates the worker and its secret only when they're missing, so the repair deletes both: a drifted
    worker may have sent its secret elsewhere. The worker comes back as rendered, at 0 replicas, and its client gets a
    new secret, which voids the old one."""
    directory, commands, config, cluster = world

    def drift():
        return clusters.clusters(directory, check=True, kubeconfig=config)["clusters"][0]["targets"][0]["drift"]

    connect(world)
    commands.answer([KUBECTL, "get", "deployment", "weave-runner-weave-workers", "-o"], "2026-11-12T10:00:00.000000Z")
    commands.answer([KUBECTL, "diff"], "")
    cluster.worker["spec"]["replicas"] = 3
    cluster.worker["spec"]["template"]["spec"]["hostAliases"] = [{"ip": "203.0.113.7", "hostnames": ["weave-api"]}]
    connect(world)
    assert drift() == ["Deployment/weave-worker"]
    cluster.worker, cluster.worker_secret = None, False
    asked, before = len(cluster.keycloak), len(commands.calls)
    connect(world)
    assert drift() == []
    assert cluster.worker["spec"]["replicas"] == 0
    assert "hostAliases" not in cluster.worker["spec"]["template"]["spec"]
    # The worker's client gets a new secret before the runner's routine rotation, and the Secret holds that one.
    assert [sorted(named) for named in cluster.keycloak[asked:]] == [
        ["weave-cluster-worker-1"],
        ["weave-cluster-runner-1"],
    ]
    issued = cluster.keycloak[asked]["weave-cluster-worker-1"]
    (written,) = [call for call in commands.calls[before:] if {"generic", "weave-worker-identity"} <= set(call.argv)]
    assert written.stdin == issued != cluster.keycloak[0]["weave-cluster-worker-1"]
    # And the guide's repair deletes exactly these two.
    assert REPAIR in (ROOT / "docs/operations/cluster-management.md").read_text()


def test_list_check_names_a_deleted_workers_namespace_as_drift(world):
    """Everything in the namespace went with it, and connect makes it again: --check names the namespace and still
    compares the cluster's shared objects."""
    directory, commands, config, _ = world
    connect(world)
    commands.answer([KUBECTL, "get", "deployment", "weave-runner-weave-workers", "-o"], "2026-11-12T10:00:00.000000Z")

    def gone(call):
        # kubectl fails on a namespace that doesn't exist unless told to ignore it.
        if "--ignore-not-found" not in call.argv:
            raise PlatformError("Stage cluster-namespace-uid failed.")
        return ""

    commands.answer([KUBECTL, "get", "namespace", "weave-workers", "-o", "jsonpath={.metadata.uid}"], gone)
    commands.answer([KUBECTL, "diff"], "")
    before = len(commands.calls)
    listed = clusters.clusters(directory, check=True, kubeconfig=config)
    assert listed["clusters"][0]["targets"][0]["drift"] == ["Namespace/weave-workers"]
    (diff,) = [call for call in commands.calls[before:] if "diff" in call.argv]
    sent = [
        (item["kind"], item["metadata"].get("namespace"), item["metadata"]["name"])
        for item in json.loads(diff.stdin)["items"]
    ]
    assert ("Service", "weave-system", "weave-api") in sent
    assert not [item for item in sent if "weave-workers" in item[1:] or item[2].endswith("-weave-workers")]


def test_list_check_says_in_its_own_words_when_the_runner_image_is_gone(world):
    directory, commands, config, _ = world
    connect(world)
    commands.answer(["image", "inspect", RUNNER_ID], PlatformError("Stage cluster-runner-inspect failed."))
    before = len(commands.calls)
    with pytest.raises(PlatformError) as refused:
        clusters.clusters(directory, check=True, kubeconfig=config)
    assert str(refused.value) == (
        "Docker on context colima-weave-tests no longer holds the runner image this platform built and pushed, so "
        "--check can't tell which runner connect applies. List without --check still works; nothing was changed."
    )
    assert not [call for call in commands.calls[before:] if "diff" in call.argv]
    assert clusters.clusters(directory, check=False, kubeconfig=config)["clusters"][0]["state"] == "connected"


def test_a_second_namespace_leaves_the_first_targets_objects_and_runner_unchanged(world):
    _, commands, _, _ = world
    connect(world)
    shared = [item for item in applied(commands) if item["metadata"].get("namespace") == "weave-system"]
    before = len(commands.calls)
    connect_team_b(world)
    again = applied(commands, before)
    assert not [call for call in commands.calls[before:] if "weave-runner-weave-workers" in " ".join(call.argv)]
    assert not [
        item for item in again if "weave-workers" in (item["metadata"].get("namespace"), item["metadata"]["name"])
    ]
    first = {
        item["metadata"]["name"]: item for item in shared if not item["metadata"]["name"].endswith("weave-workers")
    }
    for item in again:
        if item["metadata"]["name"] in first and item["metadata"].get("namespace") == "weave-system":
            assert item == first[item["metadata"]["name"]]
