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

"""Operations journey O-J4 and the restart journey on a real local kind or k3d cluster: nothing here is mocked.

Run them with ``pytest -m real_operations`` and WEAVE_ACC_RUN_DIR set to a kept run of the ``cluster`` or ``k3d``
acceptance profile (``scripts/acceptance.py --profile cluster --keep --stages prepare,up,run``): its prepare stage
made the run's cluster without connecting it, and J0 made the platform and the people. Without it, or without a
cluster in the run, every test is skipped, except where WEAVE_ACC_CLUSTER_REQUIRED is ``true``: the acceptance
workflow's cluster job sets it, so there every test fails instead. The run must live under this checkout's build/
directory and use the colima-weave-tests context (or ``default`` on GitHub Actions). Every kubectl and cluster
command uses the run's private kubeconfig, never the developer's.

The journeys connect the namespaces weave-workers-o-j4 and weave-workers-o4-b through ``weave platform cluster
connect`` and move them through change requests only. The pods they start themselves carry the label
weave.owner=operations-e2e and are deleted by that label; ``cluster disconnect`` removes each target's objects in a
``finally``; ``--stages down`` deletes the cluster.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import importlib.util
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID, uuid4

import pytest

from firefly_weave.contracts.deployments import (
    ChangeRequestApply,
    ChangeRequestApprove,
    ChangeRequestCreate,
    ChangeRequestDecision,
    ObserveRequest,
)
from firefly_weave.contracts.workers import WorkerControlRequest
from firefly_weave.sdk.errors import WeaveError
from firefly_weave.sdk.platform_clusters import NO_TARGET

pytestmark = pytest.mark.real_operations

ROOT = Path(__file__).resolve().parents[2]
OWNED_CONTEXT = "colima-weave-tests"
NAMESPACE = "weave-workers-o-j4"
SECOND_NAMESPACE = "weave-workers-o4-b"
SYSTEM = "weave-system"
COMPONENT = "weave-worker"
WORKER_SELECTOR = "app.kubernetes.io/name=weave-worker"
TASK = "example-sleep@1.0.0"
WORKER_SCRIPT = "/opt/weave/operations-worker/main.py"
SECRETS_FILE = "/run/weave/secrets/client-secret"
OWNER = ("weave.owner", "operations-e2e")
CHANGE_SECONDS = 900
CONTACT_SECONDS = 120
STALE_SECONDS = 180
# The worker's grace period is 240 s; a drained worker has no task, so it ends well before.
GONE_SECONDS = 300
PROBE_SECONDS = 180
ACTIVE = {"observing", "ready_to_plan", "awaiting_approval", "approved", "draining"}
FINISHED = {"succeeded", "failed", "cancelled", "timed_out", "suspended"}
CAPACITY = {"WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"}
SUFFIX = uuid4().hex[:6]
INPUT = {"ref": "/input"}
# The acceptance workflow's cluster job sets it to true, so that the journeys can't pass there having proven nothing.
REQUIRED = "WEAVE_ACC_CLUSTER_REQUIRED"


def _harness() -> Any:
    """tests/acceptance/conftest.py's Run: the prepared run and its people."""
    name = "operations_acceptance_harness"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "tests/acceptance/conftest.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _unavailable(reason: str) -> NoReturn:
    """Skip the journeys, or fail them where they must run."""
    if os.environ.get(REQUIRED) == "true":
        pytest.fail(f"{reason}. {REQUIRED} is true, so the cluster journeys must run here.", pytrace=False)
    pytest.skip(reason)


def prepared_run() -> Any:
    """The kept acceptance run the journeys use, with its cluster."""
    root = os.environ.get("WEAVE_ACC_RUN_DIR")
    if not root:
        _unavailable("Set WEAVE_ACC_RUN_DIR to a kept acceptance run to run the cluster journeys")
    path = Path(root).resolve()
    assert (ROOT / "build").resolve() in path.parents, "the run must live under this checkout's build/"
    found = _harness().Run(path)
    hosted = found.context == "default" and os.environ.get("GITHUB_ACTIONS") == "true"
    assert found.context == OWNED_CONTEXT or hosted, f"the journeys use only {OWNED_CONTEXT}"
    if "network" not in found.config.get("cluster", {}):
        _unavailable("The run has no cluster; prepare it with --profile cluster or --profile k3d")
    return found


@pytest.fixture(scope="module")
def run() -> Any:
    return prepared_run()


# The run's cluster, through its own kubeconfig and the pinned kubectl that prepare fetched.


def cluster(run: Any) -> dict[str, str]:
    return dict(run.config["cluster"])


def environment(run: Any) -> dict[str, str]:
    """The cluster commands' environment: the run's private kubeconfig, and none of the shell's Weave settings."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("WEAVE_", "PYFLY_"))}
    env["KUBECONFIG"] = str(run.private / _harness()._script("acceptance_evidence").KUBECONFIG)
    return env


def kubectl(run: Any, *args: str, input: str | None = None, timeout: float = 120) -> Any:
    versions = _harness().ACCEPTANCE.acceptance_journeys.load_versions(ROOT / "tests/acceptance/versions.toml")
    tool = run.private / "cluster" / "tools" / f"kubectl-{versions['cluster']['tools']['kubectl']}"
    env = environment(run)
    argv = [str(tool), "--kubeconfig", env["KUBECONFIG"], "--context", cluster(run)["context"], *args]
    return run.command(argv, input=input, timeout=timeout, env=env)


def kube_json(run: Any, *args: str) -> Any:
    return json.loads(run.require(kubectl(run, *args, "--output", "json")).stdout)


def weave_cluster(run: Any, *args: str, timeout: float = 900) -> Any:
    """``weave platform cluster <args> --output json`` on the run's platform, as the developer runs it."""
    argv = [sys.executable, "-m", "firefly_weave.cli.main", "platform", "--directory", str(run.platform_dir)]
    return run.command([*argv, "cluster", *args, "--output", "json"], timeout=timeout, env=environment(run))


def connect(run: Any, namespace: str) -> dict[str, Any]:
    provider, name = cluster(run)["provider"], cluster(run)["name"]
    argv = ["connect", "--provider", provider, "--name", name, "--namespace", namespace]
    return dict(json.loads(run.require(weave_cluster(run, *argv)).stdout))


def disconnect(run: Any, namespace: str, *, connect_failed: bool = False) -> dict[str, Any]:
    """``cluster disconnect`` of `namespace`, also after a connect that failed: that connect's pending target, with
    only its principals and identities, is freed as a connected one is. A connect that failed before it reserved the
    namespace's identities left no target, so disconnect refuses and changes nothing; that refusal returns nothing
    here, so the connect's own error is the one the test reports."""
    provider, name = cluster(run)["provider"], cluster(run)["name"]
    argv = ["disconnect", "--provider", provider, "--name", name, "--namespace", namespace]
    result = weave_cluster(run, *argv)
    nothing = NO_TARGET.format(key=cluster(run)["context"], namespace=namespace)
    if connect_failed and result.returncode != 0 and nothing in result.stdout:
        return {}
    return dict(json.loads(run.require(result).stdout))


def receipt(run: Any) -> dict[str, Any]:
    """This cluster's entry in the platform's clusters.json: its addresses and its targets' pairs."""
    value = json.loads((run.platform_dir / "clusters.json").read_text(encoding="utf-8"))
    return dict(value["clusters"][cluster(run)["context"]])


def pods(run: Any, namespace: str) -> dict[str, dict[str, Any]]:
    """The workers' pods that aren't terminating, by name."""
    listed = kube_json(run, "--namespace", namespace, "get", "pods", "--selector", WORKER_SELECTOR)["items"]
    return {item["metadata"]["name"]: item for item in listed if not item["metadata"].get("deletionTimestamp")}


def gone(run: Any, namespace: str, name: str) -> bool:
    found = run.require(kubectl(run, "--namespace", namespace, "get", "pod", name, "--ignore-not-found", "-o", "name"))
    return not found.stdout.strip()


def secret_mode(run: Any, namespace: str, workload: str, container: str) -> str:
    """The mode and owner of the client secret the main container reads, as ``stat`` prints them."""
    exec_ = ["--namespace", namespace, "exec", workload, "--container", container, "--"]
    return str(run.require(kubectl(run, *exec_, "stat", "-c", "%a %u", SECRETS_FILE)).stdout.strip())


def client_secret(run: Any, namespace: str, name: str) -> str:
    """One identity Secret's client secret, read into memory only and never printed."""
    path = "jsonpath={.data.client-secret}"
    read = run.require(kubectl(run, "--namespace", namespace, "get", "secret", name, "--output", path))
    return base64.b64decode(read.stdout.strip()).decode()


# The platform, as the run's people.


async def call(client: Any, operation: str, **kwargs: Any) -> Any:
    """One operation; a busy platform's 429 capacity refusal is sent again, as the platform asks clients to."""
    deadline = time.monotonic() + 120
    while True:
        try:
            return await client.invoke(operation, **kwargs)
        except WeaveError as error:
            if error.status != 429 or error.code not in CAPACITY or time.monotonic() > deadline:
                raise
        await asyncio.sleep(2)


def until(read: Any, done: Any, seconds: float, what: str) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if done(value):
            return value
        assert time.monotonic() < deadline, f"{what} after {seconds:.0f} s"
        time.sleep(2)


async def auntil(read: Any, done: Any, seconds: float, what: str) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = await read()
        if done(value):
            return value
        assert time.monotonic() < deadline, f"{what} after {seconds:.0f} s: {str(value)[:400]}"
        await asyncio.sleep(2)


async def target_of(client: Any, name: str) -> tuple[Any, Any]:
    """The enabled target of this name and its deployment."""
    targets = (await call(client, "deployment_targets.list", query={"limit": 100})).items
    (target,) = [item for item in targets if item.name == name and not item.disabled]
    deployments = (await call(client, "deployments.list", query={"limit": 100})).items
    (deployment,) = [item for item in deployments if item.target_id == target.id]
    return target, deployment


async def runner_online(client: Any, target_id: UUID) -> bool:
    runners = (await call(client, "deployment_runners.list", query={"target_id": str(target_id)})).items
    now = time.time()
    return any(not item.revoked and item.expires_at.timestamp() > now for item in runners)


async def observed(client: Any, target_id: UUID) -> Any:
    """A new, complete observation of the target's worker component."""
    job = await call(
        client, "deployment_observations.create", body=ObserveRequest(target_id=target_id), idempotency_key=uuid4().hex
    )

    async def read() -> Any:
        return await call(client, "deployment_jobs.read", identifier=job.id)

    done = await auntil(read, lambda item: item.state in FINISHED | {"reconciliation_required"}, 180, "observing")
    assert done.state == "succeeded" and done.observation_id is not None, done.state
    observation = await call(client, "deployment_observations.read", identifier=done.observation_id)
    assert observation.complete
    (resource,) = [item for item in observation.resources if item.name == COMPONENT]
    return resource


async def requested(client: Any, name: str, intent: str, replicas: int | None = None, **values: Any) -> Any:
    """A change request on the target's worker component, planned once the target was observed."""
    target, deployment = await target_of(client, name)
    body: dict[str, Any] = {
        "target_id": str(target.id),
        "deployment_id": str(deployment.id),
        "expected_deployment_revision": deployment.revision,
        "intent": intent,
        "components": [COMPONENT],
        "title": f"{intent} {COMPONENT} {SUFFIX}",
        **values,
    }
    if replicas is not None:
        body["desired"] = [{"name": COMPONENT, "replicas": replicas}]
    created = await call(
        client,
        "deployment_change_requests.create",
        body=ChangeRequestCreate.model_validate_json(json.dumps(body)),
        idempotency_key=uuid4().hex,
    )

    async def read() -> Any:
        return await call(client, "deployment_change_requests.read", identifier=created.id)

    ready = await auntil(read, lambda item: item.state != "observing", CHANGE_SECONDS, "still observing")
    assert ready.state == "ready_to_plan", ready.failure
    return await call(client, "deployment_change_requests.advance", identifier=created.id, revision=ready.revision)


async def finished(run: Any, client: Any, change: Any, *, approver: Any = None) -> Any:
    """Approve (as `approver`, else the same person), drain when the change drains, apply, and wait for the end."""
    approved = await call(
        approver or client,
        "deployment_change_requests.approve",
        identifier=change.id,
        body=ChangeRequestApprove(digest=change.plan_digest),
    )
    assert approved.state == "approved"
    if approved.drain_set:
        await call(client, "deployment_change_requests.drain", identifier=change.id, idempotency_key=uuid4().hex)

    async def apply() -> Any:
        try:
            return await call(
                client,
                "deployment_change_requests.apply",
                identifier=change.id,
                body=ChangeRequestApply(digest=change.plan_digest),
                idempotency_key=uuid4().hex,
            )
        except WeaveError as error:
            if error.code != "WV-DRAIN-PENDING":
                raise
            return None

    await auntil(apply, lambda value: value is not None, CHANGE_SECONDS, "drained workers still busy")

    async def read() -> Any:
        return await call(client, "deployment_change_requests.read", identifier=change.id)

    done = await auntil(read, lambda item: item.state not in {"applying", "verifying"}, CHANGE_SECONDS, "applying")
    assert done.state == "succeeded", (done.state, done.failure, runner_log(run, done.target_id))
    return done


def runner_log(run: Any, target_id: UUID) -> str:
    """The target's runner's last log lines, redacted: where it stopped a job it refused."""
    selector = f"app.kubernetes.io/name=weave-runner,firefly.weave/target={target_id}"
    found = kubectl(run, "--namespace", SYSTEM, "logs", "--selector", selector, "--tail", "40", "--all-containers")
    harness = _harness()
    return str(harness.redact(found.stdout + found.stderr, harness.known_secrets(run.root)))[-4000:]


async def scaled(run: Any, client: Any, name: str, replicas: int) -> Any:
    """Bring the worker component to `replicas` through a reviewed change, unless it is there already."""
    target, _ = await target_of(client, name)
    if (await observed(client, target.id)).replicas != replicas:
        change = await requested(client, name, "scale_workers", replicas, drain={"mode": "drain_first"})
        await finished(run, client, change)
    resource = await observed(client, target.id)
    assert (resource.replicas, resource.ready_replicas) == (replicas, replicas)
    return resource


async def workers_of(client: Any, hosts: set[str]) -> list[Any]:
    """Online fixture workers whose host is one of `hosts` (pod names for a cluster, container IDs for Compose)."""
    listed = (await call(client, "workers.list", query={"limit": 100})).items
    return [
        item
        for item in listed
        if TASK in getattr(item, "task_types", [])
        and not item.revoked
        and item.presence == "recent"
        and item.identity is not None
        and item.identity.host in hosts
    ]


async def other_workers(client: Any, hosts: set[str]) -> list[Any]:
    """Online fixture workers that don't run in this cluster: local-docker's."""
    listed = (await call(client, "workers.list", query={"limit": 100})).items
    return [
        item
        for item in listed
        if TASK in getattr(item, "task_types", [])
        and not item.revoked
        and item.presence == "recent"
        and (item.identity is None or item.identity.host not in hosts)
    ]


async def sleeps(client: Any, release: str, count: int, milliseconds: int) -> dict[UUID, str]:
    """Publish a one-step workflow that sleeps on the fixture worker, start it `count` times, and wait for the end."""
    from firefly_weave.contracts.catalog import ActivationRequest, PublicationRequest
    from firefly_weave.contracts.runtime import StartRunRequest

    manifest = json.loads((ROOT / "tests/fixtures/operations-worker/manifest.json").read_text())["capabilities"][0]
    step = f"o-j4-sleep-{SUFFIX}"
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": step, "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "example-sleep", "taskVersion": "1.0.0"},
            **{key: manifest[key] for key in ("inputSchema", "outputSchema", "sideEffect")},
            "timeoutSeconds": manifest["timeoutSeconds"],
            "retry": {"maxAttempts": 1, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
        },
    }
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": f"o-j4-lite-{SUFFIX}", "version": "1.0.0"},
        "spec": {
            "inputSchema": manifest["inputSchema"],
            "outputSchema": {"type": "object"},
            "timeoutSeconds": 600,
            "steps": [{"id": "sleep", "kind": "action", "uses": f"{step}@1.0.0", "with": INPUT}],
            "output": {"ref": "/steps/sleep/output"},
        },
    }
    for collection, document in (("actions", action), ("workflows", workflow)):
        version = await call(
            client,
            "definitions.publish",
            collection=collection,
            body=PublicationRequest(format="json", source=json.dumps(document)),
            idempotency_key=uuid4().hex,
        )
    activation = await call(
        client,
        "activations.create",
        body=ActivationRequest.model_validate_json(
            json.dumps(
                {
                    "scope": client.scope.model_dump(mode="json"),
                    "version_id": str(version.id),
                    "artifact_digest": version.digest,
                    "worker_release_ids": {"example-sleep": release},
                }
            )
        ),
        idempotency_key=uuid4().hex,
    )
    runs: list[UUID] = []
    for _ in range(count):
        key, deadline = uuid4().hex, time.monotonic() + 120
        body = StartRunRequest.model_validate_json(
            json.dumps({"activation_id": str(activation.id), "input": {"ms": milliseconds}})
        )
        while True:
            try:
                runs.append((await call(client, "runs.start", body=body, idempotency_key=key)).id)
                break
            except WeaveError as error:
                # A full start queue answers 429 WV-OPERATION-CAPACITY: send the same start again.
                if error.code != "WV-OPERATION-CAPACITY" or time.monotonic() > deadline:
                    raise
                await asyncio.sleep(2)

    async def states() -> dict[UUID, str]:
        return {item: (await call(client, "runs.read", identifier=item)).state.status for item in runs}

    return dict(await auntil(states, lambda value: set(value.values()) <= FINISHED, 600, "runs still running"))


def release_of(run: Any) -> str:
    return str(json.loads((run.platform_dir / "operations.json").read_text())["release_id"])


@contextlib.asynccontextmanager
async def drained(client: Any, workers: list[Any]) -> Any:
    """Drain `workers` for the block, and resume them after it, so tasks go only to the other workers."""
    for worker in workers:
        await call(
            client,
            "workers.drain",
            identifier=worker.id,
            body=WorkerControlRequest(expected_revision=worker.revision),
            idempotency_key=uuid4().hex,
        )
    try:
        yield
    finally:
        for worker in workers:
            current = await call(client, "workers.read", identifier=worker.id)
            await call(
                client,
                "workers.resume",
                identifier=worker.id,
                body=WorkerControlRequest(expected_revision=current.revision),
                idempotency_key=uuid4().hex,
            )


def keycloak_clients(run: Any, client_id: str) -> list[dict[str, Any]]:
    """The identity provider's clients with this client ID, read through its admin loopback."""
    from firefly_weave.sdk.platform import _env_file

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    secret = _env_file(run.platform_dir / "identity.env")["WEAVE_KC_ADMIN_SECRET"]
    base = f"http://localhost:{run.platform_state()['ports']['keycloak']}"
    form = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": "weave-bootstrap", "client_secret": secret}
    ).encode()
    with opener.open(urllib.request.Request(base + "/realms/master/protocol/openid-connect/token", data=form)) as found:
        token = json.loads(found.read())["access_token"]
    query = urllib.parse.urlencode({"clientId": client_id})
    request = urllib.request.Request(
        f"{base}/admin/realms/weave/clients?{query}", headers={"Authorization": "Bearer " + token}
    )
    with opener.open(request, timeout=30) as found:
        return list(json.loads(found.read()))


# Each target connected for the module, and disconnected after it, so its objects never outlive the journeys.


@pytest.fixture(scope="module")
def connected(run: Any) -> Iterator[dict[str, Any]]:
    started = time.monotonic()
    value: dict[str, Any] = {}
    try:
        value = connect(run, NAMESPACE)
        yield {**value, "seconds": time.monotonic() - started}
    finally:
        # Also after a connect that failed, which can leave a pending target behind.
        disconnect(run, NAMESPACE, connect_failed=not value)


@pytest.fixture(autouse=True)
def tidy(run: Any, connected: dict[str, Any]) -> Iterator[None]:
    """After each journey, cancel any change request it left active, so the next one finds the target free."""
    yield

    async def cancel() -> None:
        async with run.client("operator") as operator:
            for item in (await call(operator, "deployment_change_requests.list")).items:
                if str(item.target_id) == connected["target_id"] and item.state in ACTIVE:
                    with contextlib.suppress(WeaveError):
                        await call(
                            operator,
                            "deployment_change_requests.cancel",
                            identifier=item.id,
                            body=ChangeRequestDecision(reason="The journey ended"),
                        )

    asyncio.run(cancel())


@contextlib.contextmanager
def probes(run: Any, namespace: str, wanted: dict[str, tuple[bool, list[str]]]) -> Iterator[dict[str, tuple[int, str]]]:
    """Run short-lived pods from the worker's own pod template, labeled as a worker or carrying no role, and yield
    each one's exit code and log. Each pod carries weave.owner=operations-e2e and no workload label, so no
    ReplicaSet adopts it, and every pod of that label is deleted afterwards."""
    template = kube_json(run, "--namespace", namespace, "get", "deployment", COMPONENT)["spec"]["template"]
    main = template["spec"]["containers"][0]
    documents = []
    for name, (labeled, args) in wanted.items():
        command = ["python", "-I", WORKER_SCRIPT, *args]
        labels = {OWNER[0]: OWNER[1], "firefly.weave/probe": name}
        if labeled:
            spec = copy.deepcopy(template["spec"])
            spec["containers"][0]["command"] = command
            labels["firefly.weave/role"] = "worker"
        else:
            spec = {
                "securityContext": template["spec"]["securityContext"],
                "automountServiceAccountToken": False,
                "containers": [
                    {
                        "name": "probe",
                        "image": main["image"],
                        "imagePullPolicy": "IfNotPresent",
                        "command": command,
                        "resources": main["resources"],
                        "securityContext": main["securityContext"],
                        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                    }
                ],
                "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}],
            }
        spec |= {"restartPolicy": "Never", "activeDeadlineSeconds": 60, "terminationGracePeriodSeconds": 1}
        metadata = {"name": name, "namespace": namespace, "labels": labels}
        documents.append({"apiVersion": "v1", "kind": "Pod", "metadata": metadata, "spec": spec})
    owned = ["--namespace", namespace, "delete", "pods", "--selector", "=".join(OWNER), "--ignore-not-found"]
    run.require(kubectl(run, *owned))
    try:
        listed = json.dumps({"apiVersion": "v1", "kind": "List", "items": documents})
        run.require(kubectl(run, "--namespace", namespace, "create", "-f", "-", input=listed))

        def ended() -> dict[str, Any]:
            return {name: kube_json(run, "--namespace", namespace, "get", "pod", name)["status"] for name in wanted}

        statuses = until(
            ended,
            lambda value: all(item.get("phase") in {"Succeeded", "Failed"} for item in value.values()),
            PROBE_SECONDS,
            "probe pods still running",
        )
        outcomes = {}
        for name, status in statuses.items():
            (container,) = status["containerStatuses"]
            log = run.require(kubectl(run, "--namespace", namespace, "logs", f"pod/{name}")).stdout
            outcomes[name] = (int(container["state"]["terminated"]["exitCode"]), log)
        yield outcomes
    finally:
        run.require(kubectl(run, *owned, "--wait=false"))


async def test_o_j4_connect_scale_and_drain_on_kind(run: Any, connected: dict[str, Any]) -> None:
    """O-J4: connect gives a restricted namespace whose runner is online and whose secret files are 0400; reviewed
    changes scale the workers 0→2, they run ten sleeps, and 2→1 with drain_first removes exactly the drained pod."""
    provider, name = cluster(run)["provider"], cluster(run)["name"]
    expected = f"{provider}-{name}.{NAMESPACE}"
    assert (connected["target_name"], connected["namespace"], connected["runner"]) == (expected, NAMESPACE, "online")
    assert (connected["provider"], connected["cluster"], connected["context"]) == (provider, name, f"{provider}-{name}")
    path = "jsonpath={.metadata.labels.pod-security\\.kubernetes\\.io/enforce}"
    labels = run.require(kubectl(run, "get", "namespace", NAMESPACE, "--output", path)).stdout
    assert labels == "restricted"
    runner = f"deployment/weave-runner-{NAMESPACE}"
    assert secret_mode(run, SYSTEM, runner, "runner") == "400 65532"
    listed = run.require(kubectl(run, "get", "nodes", "--output", "name")).stdout.split()
    nodes = {item.removeprefix("node/") for item in listed}
    async with run.client("operator") as operator, run.client("approver") as approver, run.client("owner") as owner:
        target, _ = await target_of(operator, expected)
        assert str(target.id) == connected["target_id"] and target.adapter == "kubernetes"
        assert target.boundary == NAMESPACE
        assert await runner_online(operator, target.id)
        start = await observed(operator, target.id)
        assert (start.replicas, start.ready_replicas, start.members) == (0, 0, [])

        await finished(run, operator, await requested(operator, expected, "scale_workers", 2), approver=approver)
        two = await observed(operator, target.id)
        assert (two.replicas, two.ready_replicas) == (2, 2)
        live = pods(run, NAMESPACE)
        assert {member.host for member in two.members} == set(live)
        assert {member.node for member in two.members} <= nodes and all(member.ready for member in two.members)
        assert secret_mode(run, NAMESPACE, f"pod/{sorted(live)[0]}", "worker") == "400 65532"
        mine = await auntil(lambda: workers_of(operator, set(live)), lambda items: len(items) == 2, 180, "workers")
        assert {item.identity.host for item in mine} == set(live)

        # Only the cluster's workers take the sleeps: local-docker's drain for the time of the runs.
        async with drained(operator, await other_workers(operator, set(live))):
            states = await sleeps(owner, release_of(run), 10, 2000)
        assert list(states.values()) == ["succeeded"] * 10, states

        down = await requested(operator, expected, "scale_workers", 1, drain={"mode": "drain_first"})
        (victim,) = down.drain_set
        assert victim.host in live
        done = await finished(run, operator, down, approver=approver)
        assert done.warnings == [] and done.drained == [victim.worker_id]
        until(lambda: gone(run, NAMESPACE, victim.host), bool, GONE_SECONDS, "the drained pod still exists")
        assert set(pods(run, NAMESPACE)) == set(live) - {victim.host}
        one = await observed(operator, target.id)
        assert (one.replicas, one.ready_replicas) == (1, 1)


async def test_kind_restart_replaces_every_pod(run: Any, connected: dict[str, Any]) -> None:
    """At one worker, a restart with drain_first replaces its pod with the same image; the old worker goes offline
    and the new one registers."""
    async with run.client("operator") as operator:
        await scaled(run, operator, connected["target_name"], 1)
        ((before, pod),) = pods(run, NAMESPACE).items()
        image = pod["spec"]["containers"][0]["image"]
        (old,) = await auntil(lambda: workers_of(operator, {before}), lambda items: len(items) == 1, 180, "worker")
        restart = await requested(operator, connected["target_name"], "restart_workers", drain={"mode": "drain_first"})
        assert [member.worker_id for member in restart.drain_set] == [old.id]
        done = await finished(run, operator, restart)
        assert done.warnings == []
        ((after, fresh),) = until(lambda: pods(run, NAMESPACE).items(), lambda items: len(items) == 1, 120, "pods")
        assert after != before and fresh["spec"]["containers"][0]["image"] == image
        until(lambda: gone(run, NAMESPACE, before), bool, GONE_SECONDS, "the old pod still exists")
        (new,) = await auntil(lambda: workers_of(operator, {after}), lambda items: len(items) == 1, 180, "new worker")
        assert new.id != old.id

        async def presence() -> str:
            return str((await call(operator, "workers.read", identifier=old.id)).presence)

        await auntil(presence, lambda value: value == "stale", STALE_SECONDS, "the old worker is still online")
        resource = await observed(operator, UUID(connected["target_id"]))
        assert [member.host for member in resource.members] == [after] and resource.image == image


async def test_an_unlabeled_pod_cannot_reach_the_platform(run: Any, connected: dict[str, Any]) -> None:
    """Only pods labeled as a runner or worker reach the platform API and Keycloak, and no pod reaches the
    registry: an unlabeled pod finds all three closed, and a labeled one signs in but finds the registry closed."""
    found = receipt(run)
    api, registry = found["api_address"], found["registry_address"]
    issuer = f"http://localhost:{run.platform_state()['ports']['keycloak']}/realms/weave"
    wanted = {
        "o-j4-probe-unlabeled": (False, ["--probe-closed", f"{api}:8000", f"{api}:8080", f"{registry}:5000"]),
        "o-j4-probe-token": (True, ["--probe-token"]),
        "o-j4-probe-registry": (True, ["--probe-closed", f"{registry}:5000"]),
    }
    with probes(run, NAMESPACE, wanted) as outcomes:
        assert outcomes["o-j4-probe-unlabeled"][0] == 0, outcomes["o-j4-probe-unlabeled"][1]
        assert outcomes["o-j4-probe-registry"][0] == 0, outcomes["o-j4-probe-registry"][1]
        code, log = outcomes["o-j4-probe-token"]
        assert code == 0 and json.loads(log.strip().splitlines()[-1]) == {"iss": issuer}


def test_the_runner_account_can_do_only_what_its_commands_need(run: Any, connected: dict[str, Any]) -> None:
    """The runner's service account may read and patch its namespace's worker Deployment, list, read and patch its
    pods (kubectl patch reads a pod first), and read its own namespace; nothing else, anywhere."""
    account = f"system:serviceaccount:{SYSTEM}:weave-runner-{NAMESPACE}"
    allowed = [
        ("get", "deployments.apps", NAMESPACE),
        ("patch", "deployments.apps", NAMESPACE),
        ("list", "pods", NAMESPACE),
        ("get", "pods", NAMESPACE),
        ("patch", "pods", NAMESPACE),
        ("get", f"namespaces/{NAMESPACE}", None),
    ]
    refused = [
        ("get", "secrets", NAMESPACE),
        ("get", "configmaps", NAMESPACE),
        ("create", "pods", NAMESPACE),
        ("delete", "pods", NAMESPACE),
        ("watch", "pods", NAMESPACE),
        # can-i reads TYPE/NAME, so `pods/log` would ask about a pod named log: subresources go by --subresource.
        ("get", "pods --subresource=log", NAMESPACE),
        ("create", "pods --subresource=exec", NAMESPACE),
        ("delete", "deployments.apps", NAMESPACE),
        ("get", "horizontalpodautoscalers.autoscaling", NAMESPACE),
        ("get", "deployments.apps", SYSTEM),
        ("get", "secrets", SYSTEM),
        ("get", "configmaps", SYSTEM),
        ("*", "*", "default"),
        ("get", "namespaces/default", None),
        ("list", "namespaces", None),
    ]
    answers = {}
    for verb, resource, namespace in [*allowed, *refused]:
        where = ["--namespace", namespace] if namespace else []
        answer = kubectl(run, "auth", "can-i", verb, *resource.split(" "), *where, f"--as={account}")
        answers[(verb, resource, namespace)] = answer.stdout.strip()
    assert answers == {**{item: "yes" for item in allowed}, **{item: "no" for item in refused}}


async def test_two_namespaces_on_one_cluster_work_side_by_side(run: Any, connected: dict[str, Any]) -> None:
    """A second namespace gets its own target and runner and leaves the first one's runner as it was; disconnecting
    it keeps the shared objects, the first runner and the namespace itself, and deletes its identity clients;
    connecting it again enables the same target."""
    selector = f"firefly.weave/target={connected['target_id']}"
    listed = ["--namespace", SYSTEM, "get", "pods", "--selector", selector, "-o", "name"]
    first_runner = run.require(kubectl(run, *listed)).stdout.split()
    assert len(first_runner) == 1
    second: dict[str, Any] = {}
    try:
        second = connect(run, SECOND_NAMESPACE)
        provider, name = cluster(run)["provider"], cluster(run)["name"]
        assert (second["target_name"], second["runner"]) == (f"{provider}-{name}.{SECOND_NAMESPACE}", "online")
        pair = receipt(run)["targets"][SECOND_NAMESPACE]["pair"]
        assert pair != receipt(run)["targets"][NAMESPACE]["pair"]
        async with run.client("operator") as operator:
            assert await runner_online(operator, UUID(connected["target_id"]))
            assert await runner_online(operator, UUID(second["target_id"]))
        assert run.require(kubectl(run, *listed)).stdout.split() == first_runner
    finally:
        removed = disconnect(run, SECOND_NAMESPACE, connect_failed=not second)
    assert removed["target_id"] == second["target_id"] and removed["disabled"] is True
    assert f"Deployment/weave-runner-{SECOND_NAMESPACE}" in removed["removed"]
    from firefly_weave.sdk.platform_operations import cluster_clients

    for client_id in cluster_clients(pair):
        assert keycloak_clients(run, client_id) == [], client_id
    service = run.require(kubectl(run, "--namespace", SYSTEM, "get", "service", "weave-api", "-o", "name")).stdout
    assert service.strip() == "service/weave-api"
    kept = run.require(kubectl(run, "get", "namespace", SECOND_NAMESPACE, "-o", "name")).stdout
    assert kept.strip() == f"namespace/{SECOND_NAMESPACE}"
    assert SECOND_NAMESPACE not in receipt(run)["targets"]
    async with run.client("operator") as operator:
        assert await runner_online(operator, UUID(connected["target_id"]))
        found = await call(operator, "deployment_targets.read", identifier=UUID(second["target_id"]))
        assert found.disabled is True
    assert run.require(kubectl(run, *listed)).stdout.split() == first_runner
    # Connected again, the namespace gets its own target back, enabled for a new runner, and the result names it.
    again: dict[str, Any] = {}
    try:
        again = connect(run, SECOND_NAMESPACE)
        assert (again["target_id"], again["runner"]) == (second["target_id"], "online")
        assert again["enabled_again"] == {"target_id": second["target_id"], "target_name": second["target_name"]}
        async with run.client("operator") as operator:
            found = await call(operator, "deployment_targets.read", identifier=UUID(second["target_id"]))
            assert found.disabled is False and await runner_online(operator, found.id)
    finally:
        disconnect(run, SECOND_NAMESPACE, connect_failed=not again)


async def test_a_reconnect_after_scaling_keeps_the_workers(run: Any, connected: dict[str, Any]) -> None:
    """At two workers, a hand-edited NetworkPolicy shows in list --check; connect again reverts it, rolls the runner
    onto a new secret, and keeps both workers."""
    async with run.client("operator") as operator:
        await scaled(run, operator, connected["target_name"], 2)
        workers = set(pods(run, NAMESPACE))
        listed = json.loads(run.require(weave_cluster(run, "list", "--check")).stdout)
        (entry,) = [item for item in listed["clusters"] if item["context"] == cluster(run)["context"]]
        (mine,) = [item for item in entry["targets"] if item["namespace"] == NAMESPACE]
        assert (entry["state"], mine["runner"], mine["drift"]) == ("connected", "online", [])
        rotated = "jsonpath={.spec.template.metadata.annotations.firefly\\.weave/identity-rotated-at}"
        runner = ["--namespace", SYSTEM, "get", "deployment", f"weave-runner-{NAMESPACE}", "--output", rotated]
        stamp = run.require(kubectl(run, *runner)).stdout
        policy = kube_json(run, "--namespace", NAMESPACE, "get", "networkpolicy", "weave-platform-access")
        edited = copy.deepcopy(policy["spec"])
        edited["egress"][0]["ports"] = [{"protocol": "TCP", "port": 5000}]
        patch = json.dumps([{"op": "replace", "path": "/spec", "value": edited}])
        edit = ["--namespace", NAMESPACE, "patch", "networkpolicy", "weave-platform-access", "--type=json"]
        run.require(kubectl(run, *edit, "--patch", patch))
        listed = json.loads(run.require(weave_cluster(run, "list", "--check")).stdout)
        drift = [
            target["drift"]
            for item in listed["clusters"]
            for target in item["targets"]
            if target["namespace"] == NAMESPACE
        ]
        assert drift == [["NetworkPolicy/weave-platform-access"]]
        again = connect(run, NAMESPACE)
        assert (again["target_id"], again["runner"]) == (connected["target_id"], "online")
        restored = kube_json(run, "--namespace", NAMESPACE, "get", "networkpolicy", "weave-platform-access")
        assert restored["spec"] == policy["spec"]
        assert run.require(kubectl(run, *runner)).stdout != stamp
        replicas = ["--namespace", NAMESPACE, "get", "deployment", COMPONENT, "--output", "jsonpath={.spec.replicas}"]
        assert run.require(kubectl(run, *replicas)).stdout == "2"
        resource = await observed(operator, UUID(connected["target_id"]))
        assert (resource.replicas, resource.ready_replicas) == (2, 2)
        assert {member.host for member in resource.members} == workers == set(pods(run, NAMESPACE))
    listed = json.loads(run.require(weave_cluster(run, "list", "--check")).stdout)
    assert [t["drift"] for item in listed["clusters"] for t in item["targets"] if t["namespace"] == NAMESPACE] == [[]]


def test_no_client_secret_appears_outside_the_cluster(run: Any, connected: dict[str, Any]) -> None:
    """The runner's and the workers' client secrets live only in the cluster's Secrets: neither appears in the run
    directory (receipts, private logs and evidence) nor in the runner's or workers' container logs."""
    values = [
        client_secret(run, SYSTEM, f"weave-runner-identity-{NAMESPACE}"),
        client_secret(run, NAMESPACE, "weave-worker-identity"),
    ]
    try:
        assert all(len(value) >= 32 for value in values) and len(set(values)) == 2
        needles = [*values, *(base64.b64encode(value.encode()).decode() for value in values)]
        scanned = _harness()._script("acceptance_evidence").scan_tree([run.root], needles)
        assert scanned.files > 0 and scanned.hits == 0, f"{scanned.hits} file(s) of the run hold a client secret"
        logs = [
            kubectl(run, "--namespace", SYSTEM, "logs", f"deployment/weave-runner-{NAMESPACE}", "--all-containers"),
            kubectl(run, "--namespace", NAMESPACE, "logs", "--selector", WORKER_SELECTOR, "--all-containers"),
        ]
        for log in logs:
            run.require(log)
            assert not any(needle in log.stdout + log.stderr for needle in needles), "a container log holds a secret"
    finally:
        values.clear()
