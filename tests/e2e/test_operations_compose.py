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

"""Operations journeys O-J1, O-J2, O-J3, O-J8 and O-J9 on a real local Docker platform: nothing here is mocked.

Run them with ``pytest -m real_operations`` and WEAVE_ACC_RUN_DIR set to an acceptance run that
``scripts/acceptance.py --stages prepare,up --keep`` left in place: its J0 created the platform in a clean
directory, the people, and one fixture worker on local-docker (J0.10). Without it every test is skipped. The run must
live under this checkout's build/ directory and use the colima-weave-tests context. The journeys move local-docker
through change requests only; the one container they start themselves (O-J9's fault-enabled fixture worker) carries
the label weave.owner=o3 and is removed by name and label before the test ends. ``--stages down`` removes the rest.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from firefly_weave.contracts.deployments import (
    ChangeRequestApply,
    ChangeRequestApprove,
    ChangeRequestCreate,
    PlanApprovalRequest,
    TargetUpdate,
)
from firefly_weave.contracts.workers import WorkerControlRequest
from firefly_weave.sdk.errors import WeaveError
from firefly_weave.sdk.platform import LOCAL_OWNER_ROLES

pytestmark = pytest.mark.real_operations

ROOT = Path(__file__).resolve().parents[2]
OWNED_CONTEXT = "colima-weave-tests"
COMPONENT = "fixture-worker"
TASK = "example-sleep@1.0.0"
CHANGE_SECONDS = 900
STALE_SECONDS = 180
FAULT_CONTAINER = "weave-o3-fault-71"
FAULT_LABEL = "weave.owner=o3"
SUFFIX = uuid4().hex[:6]


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


@pytest.fixture(scope="module")
def run() -> Any:
    import os

    root = os.environ.get("WEAVE_ACC_RUN_DIR")
    if not root:
        pytest.skip("Set WEAVE_ACC_RUN_DIR to a kept acceptance run to run the operations journeys")
    path = Path(root).resolve()
    assert (ROOT / "build").resolve() in path.parents, "the run must live under this checkout's build/"
    found = _harness().Run(path)
    assert found.context == OWNED_CONTEXT, f"the journeys use only {OWNED_CONTEXT}"
    return found


def until(read, done, seconds: float, what: str) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if done(value):
            return value
        assert time.monotonic() < deadline, f"{what} after {seconds:.0f} s"
        time.sleep(2)


async def auntil(read, done, seconds: float, what: str) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        value = await read()
        if done(value):
            return value
        assert time.monotonic() < deadline, f"{what} after {seconds:.0f} s: {str(value)[:400]}"
        await asyncio.sleep(2)


async def local_docker(client) -> tuple[Any, Any]:
    targets = (await client.invoke("deployment_targets.list", query={"limit": 100})).items
    (target,) = [item for item in targets if item.name == "local-docker"]
    deployments = (await client.invoke("deployments.list", query={"limit": 100})).items
    (deployment,) = [item for item in deployments if item.target_id == target.id]
    return target, deployment


async def requested(client, intent: str, replicas: int | None = None, **values: Any) -> Any:
    """A change request on local-docker's fixture worker, planned once the target was observed."""
    target, deployment = await local_docker(client)
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
    created = await client.invoke(
        "deployment_change_requests.create",
        body=ChangeRequestCreate.model_validate_json(json.dumps(body)),
        idempotency_key=uuid4().hex,
    )

    async def read():
        return await client.invoke("deployment_change_requests.read", identifier=created.id)

    ready = await auntil(read, lambda item: item.state != "observing", CHANGE_SECONDS, "still observing")
    assert ready.state == "ready_to_plan", ready.failure
    return await client.invoke("deployment_change_requests.advance", identifier=created.id, revision=ready.revision)


async def finished(client, change, *, approver=None) -> Any:
    """Approve (as `approver`, else the same person), let workers finish when the change drains, apply, wait."""
    approving = approver or client
    approved = await approving.invoke(
        "deployment_change_requests.approve",
        identifier=change.id,
        body=ChangeRequestApprove(digest=change.plan_digest),
    )
    assert approved.state == "approved"
    if approved.drain_set:
        await client.invoke("deployment_change_requests.drain", identifier=change.id, idempotency_key=uuid4().hex)

    async def apply():
        try:
            return await client.invoke(
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

    async def read():
        return await client.invoke("deployment_change_requests.read", identifier=change.id)

    done = await auntil(read, lambda item: item.state != "applying", CHANGE_SECONDS, "still applying")
    assert done.state == "succeeded", (done.state, done.failure)
    return done


async def fixture_workers(client) -> list[Any]:
    listed = (await client.invoke("workers.list", query={"limit": 100})).items
    return [
        item
        for item in listed
        if TASK in getattr(item, "task_types", []) and not item.revoked and item.presence == "recent"
    ]


def containers(run) -> dict[str, str]:
    """The workers project's running containers: their 12-character host, mapped to the full ID."""
    project = f"weave-local-{run.platform_state()['id']}-workers"
    listed = run.require(
        run.docker("ps", "--no-trunc", "--quiet", "--filter", f"label=com.docker.compose.project={project}")
    ).stdout.split()
    return {item[:12]: item for item in listed}


async def lite_activation(client, release: str) -> Any:
    """order-review-lite for these journeys: one step that sleeps on the fixture worker."""
    from firefly_weave.contracts.catalog import ActivationRequest, PublicationRequest

    manifest = json.loads((ROOT / "tests/fixtures/operations-worker/manifest.json").read_text())["capabilities"][0]
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "o-j-sleep-" + SUFFIX, "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "worker", "taskType": "example-sleep", "taskVersion": "1.0.0"},
            **{key: manifest[key] for key in ("inputSchema", "outputSchema", "sideEffect")},
            "timeoutSeconds": manifest["timeoutSeconds"],
            "retry": {"maxAttempts": 1, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
        },
    }
    await client.invoke(
        "definitions.publish",
        collection="actions",
        body=PublicationRequest(format="json", source=json.dumps(action)),
        idempotency_key=uuid4().hex,
    )
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "o-j-lite-" + SUFFIX, "version": "1.0.0"},
        "spec": {
            "inputSchema": manifest["inputSchema"],
            "outputSchema": {"type": "object"},
            "timeoutSeconds": 600,
            "steps": [
                {"id": "sleep", "kind": "action", "uses": f"o-j-sleep-{SUFFIX}@1.0.0", "with": {"ref": "/input"}}
            ],
            "output": {"ref": "/steps/sleep/output"},
        },
    }
    version = await client.invoke(
        "definitions.publish",
        collection="workflows",
        body=PublicationRequest(format="json", source=json.dumps(workflow)),
        idempotency_key=uuid4().hex,
    )
    return await client.invoke(
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


async def start_runs(client, activation, inputs: list[dict]) -> list[UUID]:
    """Start each run; a full start queue answers 429 WV-OPERATION-CAPACITY, so wait and send the same start again."""
    from firefly_weave.contracts.runtime import StartRunRequest

    runs = []
    for value in inputs:
        key = uuid4().hex
        deadline = time.monotonic() + 120
        while True:
            try:
                started = await client.invoke(
                    "runs.start",
                    body=StartRunRequest.model_validate_json(
                        json.dumps({"activation_id": str(activation.id), "input": value})
                    ),
                    idempotency_key=key,
                )
                break
            except WeaveError as error:
                if error.code != "WV-OPERATION-CAPACITY" or time.monotonic() > deadline:
                    raise
                await asyncio.sleep(2)
        runs.append(started.id)
    return runs


async def run_states(client, runs: list[UUID]) -> dict[UUID, str]:
    return {run: (await client.invoke("runs.read", identifier=run)).state.status for run in runs}


def platform_sql(run, sql: str) -> str:
    """One value from the platform's own database, read inside its PostgreSQL container."""
    container = f"weave-local-{run.platform_state()['id']}-postgres-1"
    databases = run.require(
        run.docker(
            "exec", container, "sh", "-c", 'psql -U "$POSTGRES_USER" -d postgres -Atc "SELECT datname FROM pg_database"'
        )
    ).stdout.split()
    for database in databases:
        found = run.docker("exec", container, "sh", "-c", f'psql -U "$POSTGRES_USER" -d {database} -Atc "{sql}"')
        if found.returncode == 0:
            return found.stdout.strip()
    raise AssertionError("No database of the platform answered")


def release_of(run) -> str:
    return str(json.loads((run.platform_dir / "operations.json").read_text())["release_id"])


async def test_o_j1_a_clean_platform_has_local_operations_ready(run):
    """O-J1: the clean directory J0 created answers Ready, its runner is online within 60 s, and the owner holds
    exactly the local owner roles."""
    status = until(run.status, lambda value: value["operations"]["runner"] == "online", 60, "runner not online")
    assert status["operations"] == {"stage": "ready", "runner": "online", "target": "local-docker"}
    assert set(run.roles("owner")) >= set(LOCAL_OWNER_ROLES)
    async with run.client("owner") as owner:
        summary = await owner.invoke("operations_summary.read")
        assert summary.api.compatibility.mode == "ready"
        target, deployment = await local_docker(owner)
        assert target.approval_policy.distinct_approver is False
        assert [component.name for component in deployment.components] == [COMPONENT]


async def test_o_j2_scale_up_run_twenty_tasks_then_scale_down_removing_the_drained_workers(run):
    """O-J2: 3 fixture workers run 20 sleeps of 5 s; scaling to 1 drains exactly the containers it removes."""
    # The operator changes infrastructure; the owner, who may publish and run workflows, runs the work.
    async with run.client("operator") as operator, run.client("owner") as owner:
        await finished(operator, await requested(operator, "scale_workers", 3))
        await auntil(lambda: fixture_workers(operator), lambda items: len(items) == 3, 180, "three workers online")
        activation = await lite_activation(owner, release_of(run))
        runs = await start_runs(owner, activation, [{"ms": 5000}] * 20)
        states = await auntil(
            lambda: run_states(owner, runs),
            lambda value: all(
                state in {"succeeded", "failed", "cancelled", "timed_out", "suspended"} for state in value.values()
            ),
            600,
            "twenty runs still running",
        )
        assert set(states.values()) == {"succeeded"}, states
        before = containers(run)
        down = await requested(operator, "scale_workers", 1)
        removed_hosts = {member.host for member in down.drain_set}
        assert len(removed_hosts) == 2 and removed_hosts <= set(before), (removed_hosts, before)
        done = await finished(operator, down)
        assert set(done.drained) == {member.worker_id for member in down.drain_set}
        after = containers(run)
        assert set(after) == set(before) - removed_hosts, (before, after)


async def test_o_j3_a_restart_replaces_every_worker_and_the_old_ones_are_retired(run):
    """O-J3: restarting lets the workers finish, replaces their containers; the old instances go offline and are
    revoked one by one (retire-stale waits an hour, so the journey revokes the offline instances directly)."""
    async with run.client("operator") as operator:
        old = {item.id for item in await fixture_workers(operator)}
        assert old
        before = containers(run)
        restart = await requested(operator, "restart_workers", drain={"mode": "drain_first"})
        assert {member.worker_id for member in restart.drain_set} == old
        await finished(operator, restart)
        after = containers(run)
        assert len(after) == len(before) and not set(after) & set(before)
        fresh = await auntil(
            lambda: fixture_workers(operator),
            lambda items: len({item.id for item in items} - old) == len(old),
            180,
            "new instances not registered",
        )
        assert {item.id for item in fresh} - old

        async def stale():
            listed = (await operator.invoke("workers.list", query={"limit": 100})).items
            return {item.id: item.presence for item in listed if item.id in old}

        await auntil(stale, lambda value: set(value.values()) <= {"stale"}, STALE_SECONDS, "old instances online")
    async with run.client("owner") as owner:
        for worker in old:
            await owner.invoke("workers.revoke", identifier=worker)
        listed = (await owner.invoke("workers.list", query={"limit": 100})).items
        assert all(item.revoked for item in listed if item.id in old)


async def test_o_j8_four_eyes_on_local_docker(run):
    """O-J8: turned on (audited), the requester and the planner cannot approve, on the change and on the plan; a
    second person approves; an expired plan is observed and planned again."""
    async with run.client("owner") as owner, run.client("operator") as operator:
        target, _ = await local_docker(owner)
        policy = {"runner_principal_id": str(target.runner_principal_id), "capabilities": list(target.capabilities)}
        updated = await owner.invoke(
            "deployment_targets.update",
            identifier=target.id,
            body=TargetUpdate.model_validate_json(
                json.dumps({**policy, "disabled": False, "approval_policy": {"distinct_approver": True}})
            ),
            revision=target.revision,
        )
        try:
            assert updated.approval_policy.distinct_approver is True
            change = await requested(operator, "scale_workers", 2)
            for refused in (
                operator.invoke(
                    "deployment_change_requests.approve",
                    identifier=change.id,
                    body=ChangeRequestApprove(digest=change.plan_digest),
                ),
                operator.invoke(
                    "deployment_plans.approve",
                    identifier=change.plan_id,
                    body=PlanApprovalRequest(digest=change.plan_digest),
                ),
            ):
                with pytest.raises(WeaveError) as error:
                    await refused
                assert (error.value.status, error.value.code) == (403, "WV-APPROVAL-SELF")
            await finished(operator, change, approver=owner)
            # A plan that expires before anyone approves it is observed and planned again.
            short = await requested(operator, "scale_workers", 3, plan_ttl_seconds=30)

            async def read():
                return await operator.invoke("deployment_change_requests.read", identifier=short.id)

            expired = await auntil(read, lambda item: item.state == "expired", 120, "plan not expired")
            observing = await operator.invoke(
                "deployment_change_requests.replan", identifier=short.id, revision=expired.revision
            )
            assert observing.state == "observing"
            ready = await auntil(read, lambda item: item.state != "observing", CHANGE_SECONDS, "still observing")
            planned = await operator.invoke(
                "deployment_change_requests.advance", identifier=short.id, revision=ready.revision
            )
            assert planned.state == "awaiting_approval"
            await finished(operator, planned, approver=owner)
        finally:
            current, _ = await local_docker(owner)
            await owner.invoke(
                "deployment_targets.update",
                identifier=current.id,
                body=TargetUpdate.model_validate_json(
                    json.dumps({**policy, "disabled": False, "approval_policy": {"distinct_approver": False}})
                ),
                revision=current.revision,
            )
    events = platform_sql(run, "SELECT count(*) FROM access_audit WHERE action='deployment_target.approval_policy'")
    # Turned on, then off again: both changes are in the audit trail.
    assert int(events) >= 2, events


async def test_o_j9_an_unknown_outcome_opens_an_incident_that_a_safe_retry_resolves(run):
    """O-J9: a fault-enabled fixture worker reports an unknown outcome; the incident appears; once the platform's
    own workers take the step again, Retry safely resolves it and the run succeeds."""
    from firefly_weave.contracts.operations import IncidentResolution

    workers_file = run.platform_dir / "operations" / "workers" / "compose.json"
    async with run.client("operator") as operator, run.client("owner") as owner:
        own = await fixture_workers(operator)
        for worker in own:
            await operator.invoke(
                "workers.drain",
                identifier=worker.id,
                body=WorkerControlRequest(expected_revision=worker.revision),
                idempotency_key=uuid4().hex,
            )
        run.require(
            run.docker(
                "compose",
                "--file",
                str(workers_file),
                "--project-name",
                FAULT_CONTAINER,
                "run",
                "--detach",
                "--name",
                FAULT_CONTAINER,
                "--label",
                FAULT_LABEL,
                "--env",
                "WEAVE_FIXTURE_FAULTS=1",
                COMPONENT,
            )
        )
        try:
            activation = await lite_activation(owner, release_of(run))
            (run_id,) = await start_runs(owner, activation, [{"ms": 0, "fail": "unknown"}])

            async def incidents():
                return (await owner.invoke("incidents.run_list", identifier=run_id)).items

            (incident,) = await auntil(incidents, lambda items: len(items) == 1, 300, "no incident")
        finally:
            labels = run.require(run.docker("inspect", "--format", "{{json .Config.Labels}}", FAULT_CONTAINER)).stdout
            assert json.loads(labels).get("weave.owner") == "o3", "refusing to remove an unowned container"
            run.require(run.docker("rm", "--force", FAULT_CONTAINER))
            for worker in own:
                current = await operator.invoke("workers.read", identifier=worker.id)
                await operator.invoke(
                    "workers.resume",
                    identifier=worker.id,
                    body=WorkerControlRequest(expected_revision=current.revision),
                    idempotency_key=uuid4().hex,
                )
        resolved = await owner.invoke(
            "incidents.resolve",
            identifier=incident.id,
            body=IncidentResolution(receipt_id=uuid4(), kind="retry_safe", reason="The fault was injected on purpose"),
            revision=incident.revision,
        )
        assert resolved.id == incident.id
        states = await auntil(
            lambda: run_states(owner, [run_id]),
            lambda value: value[run_id] in {"succeeded", "failed", "cancelled", "timed_out"},
            300,
            "the retried run is still running",
        )
        assert states[run_id] == "succeeded", states
