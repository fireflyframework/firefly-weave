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

"""Kubernetes effects require pinned namespace identity and atomic server preconditions."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.deployments import ComponentSpec, DeploymentPlan, PlanStep
from firefly_weave.deployment_runner.kubernetes import KubernetesAdapter
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError


def fixture(kind="worker", action="update"):
    target = uuid4()
    policy = DestinationPolicy(
        target_id=target,
        adapter="kubernetes",
        external_identity="namespace-uid",
        boundary="owned",
        executable="/usr/local/bin/kubectl",
        context="owned-context",
        capabilities=["observe", "update", "scale_workers"],
        components=[{"name": "engine", "kind": kind, "configuration": "production", "container": "main"}],
        image_repositories=["registry.example/engine"],
    )
    component = ComponentSpec(
        name="engine",
        kind=kind,
        configuration="production",
        image="registry.example/engine@sha256:" + "b" * 64,
        replicas=1,
        worker_release_id=uuid4() if kind == "worker" else None,
    )
    now = datetime.now(UTC)
    plan = DeploymentPlan(
        id=uuid4(),
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        target_id=target,
        target_revision=1,
        deployment_id=uuid4(),
        deployment_revision=1,
        adapter="kubernetes",
        intent=action,
        observation_id=uuid4(),
        observation_digest="a" * 64,
        steps=[PlanStep(action=action, component=component, expected_version="8")],
        risks=["external_effects"],
        created_at=now,
        expires_at=now + timedelta(minutes=1),
        digest="a" * 64,
    )
    resource = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "engine",
            "namespace": "owned",
            "uid": "deployment-uid",
            "resourceVersion": "8",
            "generation": 1,
            "labels": {},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "engine"}},
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"annotations": {"firefly.weave/configuration": "production"}},
                "spec": {"containers": [{"name": "main", "image": component.image, "resources": {}}]},
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    return policy, plan, resource


def pod(name, *, template="7d9f8c6b5", owner=None, namespace="owned", labels=None, deleting=None, **status):
    """A pod as `kubectl get pods -o json` returns it, owned by the engine deployment's ReplicaSet."""
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "engine", "pod-template-hash": template} if labels is None else labels,
            "ownerReferences": [
                owner
                or {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": f"engine-{template}",
                    "uid": "replicaset-uid",
                    "controller": True,
                }
            ],
            **({"deletionTimestamp": deleting} if deleting else {}),
        },
        "spec": {"nodeName": status.pop("node", "node-1")},
        "status": {
            "phase": status.pop("phase", "Running"),
            "conditions": [{"type": "Ready", "status": status.pop("ready", "True")}],
            "containerStatuses": status.pop(
                "containers",
                [
                    {"name": "sidecar", "restartCount": 9, "state": {"running": {"startedAt": "2026-10-01T00:00:00Z"}}},
                    {"name": "main", "restartCount": 2, "state": {"running": {"startedAt": "2026-10-03T08:15:30Z"}}},
                ],
            ),
            **status,
        },
    }


# Exactly the pod fields the runner reads: one tab-separated line per pod, never whole pod objects.
POD_FIELDS = (
    "{range .items[*]}{.metadata.name}"
    '{"\\t"}{.metadata.namespace}'
    '{"\\t"}{.metadata.deletionTimestamp}'
    '{"\\t"}{.metadata.labels.pod-template-hash}'
    '{"\\t"}{.metadata.ownerReferences[?(@.controller==true)].apiVersion}'
    '{"\\t"}{.metadata.ownerReferences[?(@.controller==true)].kind}'
    '{"\\t"}{.metadata.ownerReferences[?(@.controller==true)].name}'
    '{"\\t"}{.status.phase}'
    '{"\\t"}{.status.reason}'
    '{"\\t"}{.status.conditions[?(@.type=="Ready")].status}'
    '{"\\t"}{.status.containerStatuses[?(@.name=="main")].restartCount}'
    '{"\\t"}{.status.containerStatuses[?(@.name=="main")].state.running.startedAt}'
    '{"\\t"}{.spec.nodeName}{"\\n"}{end}'
)


def render(pods, container="main"):
    """What kubectl prints for POD_FIELDS: missing values are empty and several matches join with spaces."""

    def text(value):
        return "" if value is None else str(value)

    lines = []
    for item in pods:
        metadata, status = item["metadata"], item.get("status", {})
        controllers = [ref for ref in metadata.get("ownerReferences", []) if ref.get("controller") is True]
        statuses = [entry for entry in status.get("containerStatuses", []) if entry.get("name") == container]
        ready = [entry for entry in status.get("conditions", []) if entry.get("type") == "Ready"]
        values = [
            metadata.get("name"),
            metadata.get("namespace"),
            metadata.get("deletionTimestamp"),
            metadata.get("labels", {}).get("pod-template-hash"),
            " ".join(text(ref.get("apiVersion")) for ref in controllers),
            " ".join(text(ref.get("kind")) for ref in controllers),
            " ".join(text(ref.get("name")) for ref in controllers),
            status.get("phase"),
            status.get("reason"),
            " ".join(text(entry.get("status")) for entry in ready),
            " ".join(text(entry.get("restartCount")) for entry in statuses),
            " ".join(text(entry.get("state", {}).get("running", {}).get("startedAt")) for entry in statuses),
            item.get("spec", {}).get("nodeName"),
        ]
        lines.append("\t".join(text(value) for value in values) + "\n")
    return "".join(lines).encode()


class Server:
    def __init__(self, policy, resource):
        self.policy, self.resource, self.calls, self.events = policy, resource, [], []
        self.namespace_uid = policy.external_identity
        self.fail_dry = False
        self.pods = [pod("engine-7d9f8c6b5-x2kq4")]

    async def command(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        assert argv[:6] == [
            self.policy.executable,
            "--context",
            self.policy.context,
            "--namespace",
            self.policy.boundary,
            "--request-timeout=20s",
        ]
        if "namespace" in argv:
            return json.dumps({"metadata": {"name": self.policy.boundary, "uid": self.namespace_uid}}).encode()
        if "horizontalpodautoscalers.autoscaling" in argv:
            return b'{"items":[]}'
        if "pods" in argv:
            assert argv[6:] == ["get", "pods", "--selector=app=engine", "-o=jsonpath=" + POD_FIELDS]
            return render(self.pods)
        assert "deployments.apps" in argv and "engine" in argv
        if "get" in argv:
            return json.dumps(self.resource).encode()
        assert "patch" in argv
        patch = json.loads(kwargs["stdin"])
        assert patch[:2] == [
            {"op": "test", "path": "/metadata/uid", "value": "deployment-uid"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "8"},
        ]
        dry = "--dry-run=server" in argv
        self.events.append("dry" if dry else "write")
        if dry and self.fail_dry:
            raise RunnerPolicyError()
        result = copy.deepcopy(self.resource)
        for edit in patch[2:]:
            current = result
            parts = edit["path"].strip("/").split("/")
            for raw in parts[:-1]:
                part = raw.replace("~1", "/").replace("~0", "~")
                current = current[int(part)] if isinstance(current, list) else current[part]
            key = parts[-1].replace("~1", "/").replace("~0", "~")
            if edit["op"] == "test":
                assert current[key] == edit["value"]
            else:
                current[key] = edit["value"]
        if not dry:
            result["metadata"]["resourceVersion"] = "9"
            self.resource = result
        return json.dumps(result).encode()

    async def before_write(self):
        self.events.append("renew")


async def test_observe_only_fixed_deployments_and_pinned_namespace():
    policy, _, resource = fixture()
    server = Server(policy, resource)
    result = await KubernetesAdapter(policy, command=server.command).observe()
    assert result.complete and result.settled and result.resources[0].ownership == "imported"
    assert result.resources[0].version == "8"
    # Namespace identity, the deployment, then its pods through the deployment's own selector.
    assert len(server.calls) == 3 and "pods" in server.calls[2][0]
    server.namespace_uid = "other"
    with pytest.raises(RunnerPolicyError):
        await KubernetesAdapter(policy, command=server.command).observe()
    assert len(server.calls) == 4


@pytest.mark.parametrize("action", ["update", "scale_workers"])
async def test_conditional_patch_dry_runs_before_renewal_and_effects(action):
    policy, plan, resource = fixture(action=action)
    server = Server(policy, resource)
    result = await KubernetesAdapter(policy, command=server.command).apply(plan, server.before_write)
    assert server.events == ["dry", "renew", "write"]
    assert result.complete and result.settled and result.resources[0].ownership == "managed"
    assert result.resources[0].image == plan.steps[0].component.image
    patches = [json.loads(kw["stdin"]) for argv, kw in server.calls if "patch" in argv]
    assert patches[0] == patches[1]
    paths = {item["path"] for item in patches[0] if item["op"] != "test"}
    if action == "scale_workers":
        assert paths == {"/metadata/labels/firefly.weave~1target", "/spec/replicas"}
    else:
        assert "/spec/template/spec/containers/0/resources" in paths


@pytest.mark.parametrize("mutation", ["version", "configuration", "container", "api_strategy", "scale_image"])
async def test_preconditions_fail_before_any_effect(mutation):
    policy, plan, resource = fixture(
        kind="api" if mutation == "api_strategy" else "worker",
        action="scale_workers" if mutation == "scale_image" else "update",
    )
    if mutation == "version":
        resource["metadata"]["resourceVersion"] = "9"
    if mutation == "configuration":
        resource["spec"]["template"]["metadata"]["annotations"] = {}
    if mutation == "container":
        resource["spec"]["template"]["spec"]["containers"][0]["name"] = "other"
    if mutation == "api_strategy":
        resource["spec"]["strategy"]["type"] = "RollingUpdate"
    if mutation == "scale_image":
        resource["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.example/engine@sha256:" + "c" * 64
    server = Server(policy, resource)
    with pytest.raises(RunnerPolicyError):
        await KubernetesAdapter(policy, command=server.command).apply(plan, server.before_write)
    assert server.events == []


async def test_dry_run_rejection_or_cancellation_never_starts_write():
    policy, plan, resource = fixture()
    server = Server(policy, resource)
    server.fail_dry = True
    with pytest.raises(RunnerPolicyError):
        await KubernetesAdapter(policy, command=server.command).apply(plan, server.before_write)
    assert server.events == ["dry"]
    server.fail_dry = False

    async def cancelled():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await KubernetesAdapter(policy, command=server.command).apply(plan, cancelled)
    assert "write" not in server.events


async def test_old_generation_is_not_reported_ready():
    policy, _, resource = fixture()
    resource["metadata"]["generation"] = 2
    server = Server(policy, resource)
    result = await KubernetesAdapter(policy, command=server.command).observe()
    assert not result.settled and result.resources[0].state == "progressing"


def test_shell_fragments_and_unbound_container_are_not_valid_policy():
    policy, _, _ = fixture()
    for changes in ({"boundary": "owned; id"}, {"context": "--other"}, {"executable": "kubectl"}):
        with pytest.raises(ValueError):
            DestinationPolicy.model_validate({**policy.model_dump(), **changes})


async def test_admission_cannot_change_requested_resources_or_image():
    policy, plan, resource = fixture()
    server = Server(policy, resource)

    async def command(argv, **kwargs):
        result = json.loads(await server.command(argv, **kwargs))
        if "--dry-run=server" in argv:
            result["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "16"
        return json.dumps(result).encode()

    with pytest.raises(RunnerPolicyError):
        await KubernetesAdapter(policy, command=command).apply(plan, server.before_write)
    assert server.events == ["dry"]


async def test_resources_preserve_unmanaged_keys_and_accept_canonical_quantities():
    policy, plan, resource = fixture()
    resource["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"ephemeral-storage": "2Gi"}}
    server = Server(policy, resource)

    async def command(argv, **kwargs):
        result = json.loads(await server.command(argv, **kwargs))
        if "patch" in argv:
            resources = result["spec"]["template"]["spec"]["containers"][0]["resources"]
            assert resources["limits"]["ephemeral-storage"] == "2Gi"
            for field in ("requests", "limits"):
                resources[field]["cpu"] = "0.5"
                resources[field]["memory"] = "1Gi"
        return json.dumps(result).encode()

    result = await KubernetesAdapter(policy, command=command).apply(plan, server.before_write)
    assert result.settled


async def test_actual_write_keeps_preconditions_after_dry_run():
    policy, plan, resource = fixture()
    server = Server(policy, resource)

    async def command(argv, **kwargs):
        if "patch" in argv and "--dry-run=server" not in argv:
            patch = json.loads(kwargs["stdin"])
            assert patch[1] == {"op": "test", "path": "/metadata/resourceVersion", "value": "8"}
            raise RunnerPolicyError()
        return await server.command(argv, **kwargs)

    with pytest.raises(RunnerPolicyError):
        await KubernetesAdapter(policy, command=command).apply(plan, server.before_write)
    assert server.resource["metadata"]["resourceVersion"] == "8"


@pytest.mark.asyncio
async def test_api_update_refuses_a_targeted_horizontal_autoscaler():
    policy, plan, resource = fixture(kind="api")
    server = Server(policy, resource)
    original = server.command

    async def command(argv, **kwargs):
        if "horizontalpodautoscalers.autoscaling" in argv:
            return json.dumps(
                {"items": [{"spec": {"scaleTargetRef": {"kind": "Deployment", "name": "engine"}}}]}
            ).encode()
        return await original(argv, **kwargs)

    async def before_write():
        raise AssertionError("An autoscaled API must not be patched")

    with pytest.raises(RunnerPolicyError):
        await KubernetesAdapter(policy, command=command).apply(plan, before_write)
    assert not any("patch" in argv for argv, _ in server.calls)


async def test_admission_cannot_turn_api_update_into_rolling_replacement():
    policy, plan, resource = fixture(kind="api")
    server = Server(policy, resource)

    async def command(argv, **kwargs):
        result = json.loads(await server.command(argv, **kwargs))
        if "--dry-run=server" in argv:
            result["spec"]["strategy"] = {"type": "RollingUpdate"}
        return json.dumps(result).encode()

    with pytest.raises(RunnerPolicyError):
        await KubernetesAdapter(policy, command=command).apply(plan, server.before_write)
    assert server.events == ["dry"]


def members_of(result):
    return [member.model_dump(mode="json") for member in result.resources[0].members]


def pods_command(server, *, pods=None, failure=None, output=None):
    """Wrap the fake server so the pods listing can answer differently."""
    calls = []

    async def command(argv, **kwargs):
        calls.append(argv)
        if "pods" in argv:
            if failure is not None:
                raise failure
            if output is not None:
                return output
            return render(server.pods if pods is None else pods)
        return await server.command(argv, **kwargs)

    return command, calls


async def test_worker_members_are_the_deployment_live_pods():
    policy, _, resource = fixture()
    server = Server(policy, resource)
    server.pods = [
        pod("engine-7d9f8c6b5-zz9", phase="Pending", ready="False", containers=[], node=None),
        pod("engine-7d9f8c6b5-x2kq4"),
        pod("engine-5c4b3a2-old1", template="5c4b3a2", deleting="2026-10-03T09:00:00Z"),
        pod("engine-7d9f8c6b5-done", phase="Succeeded", ready="False"),
        pod("engine-7d9f8c6b5-evicted", phase="Failed", reason="Evicted", ready="False", containers=[]),
        pod("engine-7d9f8c6b5-crashed", phase="Failed", ready="False"),
    ]
    result = await KubernetesAdapter(policy, command=server.command).observe()
    assert members_of(result) == [
        {
            "host": "engine-7d9f8c6b5-x2kq4",
            "ordinal": None,
            "state": "running",
            "ready": True,
            "started_at": "2026-10-03T08:15:30Z",
            "restarts": 2,
            "node": "node-1",
        },
        {
            "host": "engine-7d9f8c6b5-zz9",
            "ordinal": None,
            "state": "pending",
            "ready": False,
            "started_at": None,
            "restarts": 0,
            "node": None,
        },
    ]
    assert all(argv[6] == "get" for argv, _ in server.calls)


@pytest.mark.parametrize(
    "foreign",
    [
        pod(
            "other-abc12",
            template="abc12",
            owner={
                "apiVersion": "apps/v1",
                "kind": "ReplicaSet",
                "name": "other-abc12",
                "uid": "u",
                "controller": True,
            },
        ),
        pod(
            "engine-7d9f8c6b5-a",
            owner={"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "engine-7d9f8c6b5", "controller": False},
        ),
        pod(
            "engine-7d9f8c6b5-b",
            owner={"apiVersion": "batch/v1", "kind": "Job", "name": "engine-7d9f8c6b5", "controller": True},
        ),
        pod("engine-7d9f8c6b5-c", namespace="elsewhere"),
        pod("engine-7d9f8c6b5-d", labels={"app": "engine"}),
    ],
)
async def test_pods_the_deployment_does_not_own_are_not_members(foreign):
    policy, _, resource = fixture()
    server = Server(policy, resource)
    server.pods = [foreign, pod("engine-7d9f8c6b5-x2kq4")]
    result = await KubernetesAdapter(policy, command=server.command).observe()
    assert [member["host"] for member in members_of(result)] == ["engine-7d9f8c6b5-x2kq4"]


async def test_two_controllers_are_not_one_owner():
    policy, _, resource = fixture()
    server = Server(policy, resource)
    owned = pod("engine-7d9f8c6b5-twice")
    owned["metadata"]["ownerReferences"].append(dict(owned["metadata"]["ownerReferences"][0]))
    server.pods = [owned]
    assert members_of(await KubernetesAdapter(policy, command=server.command).observe()) == []


def surge(policy, plan, resource, *, live_old):
    """A worker at 100 replicas during a rolling update: 100 new pods and 25 old ones."""
    component = plan.steps[0].component.model_copy(update={"replicas": 100})
    plan = plan.model_copy(update={"steps": [plan.steps[0].model_copy(update={"component": component})]})
    resource["spec"]["replicas"] = 100
    resource["status"].update(replicas=100, readyReplicas=100, updatedReplicas=100, availableReplicas=100)
    server = Server(policy, resource)
    server.pods = [pod(f"engine-7d9f8c6b5-{index:05d}") for index in range(100)] + [
        pod(
            f"engine-5c4b3a2-{index:05d}", template="5c4b3a2", **({} if live_old else {"deleting": "2026-10-03T09:00Z"})
        )
        for index in range(25)
    ]
    return plan, server


async def test_update_at_the_replica_limit_completes_while_old_pods_drain():
    policy, plan, resource = fixture()
    plan, server = surge(policy, plan, resource, live_old=False)
    for item in server.pods[100:]:
        item["metadata"]["deletionTimestamp"] = "2026-10-03T09:00:00Z"
    result = await KubernetesAdapter(policy, command=server.command).apply(plan, server.before_write)
    assert result.settled and len(result.resources[0].members) == 100
    assert {member.host[:16] for member in result.resources[0].members} == {"engine-7d9f8c6b5"}


async def test_more_than_one_hundred_live_members_are_left_out_without_failing():
    policy, plan, resource = fixture()
    plan, server = surge(policy, plan, resource, live_old=True)
    result = await KubernetesAdapter(policy, command=server.command).apply(plan, server.before_write)
    assert result.settled and result.resources[0].members == []
    assert "members" not in result.model_dump(mode="json")["resources"][0]
    observed = await KubernetesAdapter(policy, command=server.command).observe()
    assert observed.complete and observed.resources[0].members == []
    server.pods = server.pods[:100]
    assert len((await KubernetesAdapter(policy, command=server.command).observe()).resources[0].members) == 100


@pytest.mark.parametrize(
    "selector,expected",
    [
        ({"matchLabels": {"tier": "workers", "app": "engine"}}, "app=engine,tier=workers"),
        (
            {
                "matchLabels": {"app": "engine"},
                "matchExpressions": [
                    {"key": "track", "operator": "In", "values": ["stable", "canary"]},
                    {"key": "app.kubernetes.io/part-of", "operator": "NotIn", "values": ["legacy"]},
                    {"key": "weave.example/worker", "operator": "Exists"},
                    {"key": "paused", "operator": "DoesNotExist", "values": []},
                ],
            },
            "app=engine,track in (canary,stable),app.kubernetes.io/part-of notin (legacy),weave.example/worker,!paused",
        ),
        ({"matchExpressions": [{"key": "app", "operator": "In", "values": ["engine"]}]}, "app in (engine)"),
    ],
)
async def test_selectors_become_kubectl_label_selectors(selector, expected):
    policy, _, resource = fixture()
    resource["spec"]["selector"] = selector
    server = Server(policy, resource)
    command, calls = pods_command(server)
    result = await KubernetesAdapter(policy, command=command).observe()
    [listing] = [argv for argv in calls if "pods" in argv]
    assert listing[6:] == ["get", "pods", "--selector=" + expected, "-o=jsonpath=" + POD_FIELDS]
    assert [member["host"] for member in members_of(result)] == ["engine-7d9f8c6b5-x2kq4"]


@pytest.mark.parametrize(
    "selector",
    [
        None,
        {},
        {"matchLabels": {}},
        {"matchLabels": {"app": "engine,tier=x"}},
        {"matchLabels": {"app in (a)": "engine"}},
        {"matchLabels": {"app": "engine", "bad key": "x"}},
        {"matchLabels": {"app": "x" * 64}},
        {"matchLabels": ["app=engine"]},
        {"matchLabels": {"app": 1}},
        {"matchLabels": {"app": "engine"}, "other": {}},
        {"matchExpressions": [{"key": "app", "operator": "In", "values": []}]},
        {"matchExpressions": [{"key": "app", "operator": "In", "values": [""]}]},
        {"matchExpressions": [{"key": "app", "operator": "In", "values": ["a,b"]}]},
        {"matchExpressions": [{"key": "app", "operator": "NotIn"}]},
        {"matchExpressions": [{"key": "app", "operator": "Exists", "values": ["x"]}]},
        {"matchExpressions": [{"key": "app", "operator": "DoesNotExist", "values": ["x"]}]},
        {"matchExpressions": [{"key": "app", "operator": "Gt", "values": ["1"]}]},
        {"matchExpressions": [{"key": "bad key", "operator": "Exists"}]},
        {"matchExpressions": [{"key": "app", "operator": "Exists", "extra": True}]},
        {"matchExpressions": {"key": "app", "operator": "Exists"}},
        {"matchExpressions": ["app"]},
    ],
)
async def test_a_selector_kubectl_cannot_express_lists_no_pods_and_leaves_members_out(selector):
    policy, _, resource = fixture()
    if selector is None:
        del resource["spec"]["selector"]
    else:
        resource["spec"]["selector"] = selector
    server = Server(policy, resource)
    result = await KubernetesAdapter(policy, command=server.command).observe()
    assert result.complete and result.resources[0].members == []
    assert not any("pods" in argv for argv, _ in server.calls)


@pytest.mark.parametrize(
    "output",
    [
        b"engine-7d9f8c6b5-x2kq4\towned\n",
        b"not\ttab\tseparated\tenough\n",
        b"\xff\xfe\n",
        render([pod("engine-7d9f8c6b5-x2kq4"), pod("engine-7d9f8c6b5-other")])[:-1],
    ],
)
async def test_unreadable_pod_output_leaves_members_out(output):
    policy, _, resource = fixture()
    command, _ = pods_command(Server(policy, resource), output=output)
    result = await KubernetesAdapter(policy, command=command).observe()
    assert result.complete and result.resources[0].members == []


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "Running Now"},
        {"node": "node/1"},
        {"containers": [{"name": "main", "restartCount": -1, "state": {}}]},
        {"containers": [{"name": "main", "restartCount": "1x", "state": {}}]},
        {"containers": [{"name": "main", "restartCount": 0, "state": {"running": {"startedAt": "today"}}}]},
        {"ready": "Maybe"},
    ],
)
async def test_one_malformed_live_pod_leaves_all_members_out(changes):
    policy, _, resource = fixture()
    server = Server(policy, resource)
    server.pods = [pod("engine-7d9f8c6b5-good"), pod("engine-7d9f8c6b5-x2kq4", **changes)]
    result = await KubernetesAdapter(policy, command=server.command).observe()
    assert result.complete and result.resources[0].members == []


async def test_a_failed_pod_listing_never_fails_the_observation():
    from firefly_weave.deployment_runner.command import CommandError

    policy, _, resource = fixture()
    command, _ = pods_command(Server(policy, resource), failure=CommandError())
    result = await KubernetesAdapter(policy, command=command).observe()
    assert result.complete and result.settled and result.resources[0].members == []


async def test_a_cancelled_pod_listing_still_cancels():
    policy, _, resource = fixture()
    command, _ = pods_command(Server(policy, resource), failure=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await KubernetesAdapter(policy, command=command).observe()


async def test_api_components_list_no_pods():
    policy, _, resource = fixture(kind="api")
    server = Server(policy, resource)
    result = await KubernetesAdapter(policy, command=server.command).observe()
    assert result.resources[0].members == [] and not any("pods" in argv for argv, _ in server.calls)
