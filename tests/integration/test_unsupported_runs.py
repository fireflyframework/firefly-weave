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

"""A platform rolled back below a run's language features skips the run until an upgrade, never blocks it."""

import json

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts import language_features

pytestmark = pytest.mark.integration

GREETING = {"op": {"name": "concat", "args": [{"literal": "n="}, {"ref": "/input"}]}}


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture):
    """The shared worker flow with an overall timeout, and a copy whose task input is built by ``text.concat``."""
    plain = json.loads(worker_runtime_fixture["source"])
    plain["spec"]["timeoutSeconds"] = 3600
    greeting = json.loads(json.dumps(plain))
    greeting["metadata"]["name"] = "greeting-flow"
    greeting["spec"]["steps"][0]["with"] = GREETING
    return {**worker_runtime_fixture, "source": json.dumps(greeting), "plain_source": json.dumps(plain)}


@pytest.fixture
async def plain_run(worker_setup, worker_runtime_fixture, services, access_db):
    """A run in the same environment whose workflow uses no language feature."""
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.service import DefinitionService

    _, runtime, _, actor, scope, activation, _ = worker_setup
    definitions = services(access_db[0]).resolve(DefinitionService)
    published = await definitions.publish(
        actor, scope, "Workflow", worker_runtime_fixture["plain_source"], "json", "plain", context=AuditContext()
    )
    plain = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            scope=scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids=activation.request.worker_release_ids,
        ),
        "plain-activation",
        context=AuditContext(),
    )
    return await runtime.start(
        actor, scope, StartRunRequest(activation_id=plain.id, input=4), "plain-run", context=AuditContext()
    )


UNSUPPORTED = {"reason": "ir_unsupported", "missing_features": ["text.concat"]}


def roll_back(monkeypatch):
    """Simulate a server rolled back to a release that lists no language features; ``undo`` upgrades it again."""
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", ())


async def policy_blocks(access_db):
    async with access_db[1]() as session:
        return await session.scalar(text("SELECT count(*) FROM run_policy_blocks"))


async def status(access_db, run):
    async with access_db[1]() as session:
        return await session.scalar(text("SELECT state->>'status' FROM runs WHERE id=:id"), {"id": run.id})


async def test_claims_skip_unsupported_runs_and_offer_them_after_an_upgrade(
    queued_task, plain_run, task_service, transaction_factory, worker_ids, worker_setup, access_db, monkeypatch
):
    from firefly_weave.runtime.repository import RuntimeRepository

    roll_back(monkeypatch)
    release = worker_setup[5].request.worker_release_ids["echo"]
    async with transaction_factory() as tx:
        offered = await RuntimeRepository(tx).candidates(release, ["echo@1.2.0"])
    # The run the platform does not run is never offered, so it cannot hold back the runs behind it.
    async with access_db[1]() as session:
        plain_task = await session.scalar(text("SELECT id FROM task_intents WHERE run_id=:id"), {"id": plain_run.id})
    assert offered == [plain_task]
    async with transaction_factory() as tx:
        leases = await task_service.claim(tx, worker_ids[0], 1)
    assert [lease.input for lease in leases] == [4]
    assert await policy_blocks(access_db) == 0
    monkeypatch.undo()
    async with transaction_factory() as tx:
        leases = await task_service.claim(tx, worker_ids[1], 1)
    assert [lease.input for lease in leases] == ["n=3"]
    assert await status(access_db, queued_task) == "waiting"


async def test_deadlines_of_unsupported_runs_wait_for_an_upgrade(
    queued_task, plain_run, transaction_factory, services, access_db, monkeypatch
):
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.repository import RuntimeRepository

    roll_back(monkeypatch)
    async with access_db[1].begin() as session:
        await session.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id='@run'")
        )
    async with transaction_factory() as tx:
        due = [row["id"] for row in await RuntimeRepository(tx).scanner_runs(10)]
    assert due == [plain_run.id]
    service = services(access_db[0]).resolve(DeadlineService)
    async with transaction_factory() as tx:
        assert await service.tick(tx, 1) == 1
    async with transaction_factory() as tx:
        assert await service.tick(tx, 1) == 0
    assert (await status(access_db, plain_run), await status(access_db, queued_task)) == ("timed_out", "waiting")
    assert await policy_blocks(access_db) == 0
    monkeypatch.undo()
    async with transaction_factory() as tx:
        assert await service.tick(tx, 1) == 1
    assert await status(access_db, queued_task) == "timed_out"
    async with access_db[1]() as session:
        event = await session.scalar(
            text("SELECT data FROM run_events WHERE run_id=:id AND type='timed_out'"), {"id": queued_task.id}
        )
    assert event["node_id"] == "@run" and "code" not in event
    assert await policy_blocks(access_db) == 0


async def test_recovery_leaves_expired_attempts_of_unsupported_runs_for_an_upgrade(
    queued_task, task_service, transaction_factory, worker_ids, worker_setup, services, access_db, monkeypatch
):
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.workers.leases import TaskService

    async with transaction_factory() as tx:
        assert len(await task_service.claim(tx, worker_ids[0], 1)) == 1
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    roll_back(monkeypatch)
    async with transaction_factory() as tx:
        assert await services(access_db[0]).resolve(TaskService).recovery_candidates(tx, 10) == []
    recovery = services(access_db[0]).resolve(RecoveryService)
    report = await recovery.scan(worker_setup[4], 10, actor=worker_setup[3], context=AuditContext())
    assert not any(report.model_dump().values())
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT status FROM task_leases")) == "active"
        assert await session.scalar(text("SELECT status FROM task_intents")) == "leased"
    assert await policy_blocks(access_db) == 0
    monkeypatch.undo()
    report = await recovery.scan(worker_setup[4], 10, actor=worker_setup[3], context=AuditContext())
    assert report.resumed + report.incidents == 1
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT status FROM task_leases")) != "active"


async def test_expired_ready_tasks_of_unsupported_runs_wait_for_an_upgrade(
    queued_task, transaction_factory, worker_setup, services, access_db, monkeypatch
):
    from firefly_weave.operations.incidents import IncidentService
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.repository import RuntimeRepository

    roll_back(monkeypatch)
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',"
                "to_jsonb(cast(clock_timestamp()-interval '1 second' AS text)))"
            )
        )
    async with transaction_factory() as tx:
        assert await RuntimeRepository(tx).expired_ready(10) == []
    recovery = services(access_db[0]).resolve(RecoveryService)
    report = await recovery.scan(worker_setup[4], 10, actor=worker_setup[3], context=AuditContext())
    assert report.incidents == 0
    assert await status(access_db, queued_task) == "waiting"
    assert await policy_blocks(access_db) == 0
    monkeypatch.undo()
    report = await recovery.scan(worker_setup[4], 10, actor=worker_setup[3], context=AuditContext())
    assert report.incidents == 1
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT status FROM task_intents")) == "incident"
    # Rolled back again, incident lists show the waiting run's incident as unavailable instead of failing.
    roll_back(monkeypatch)
    incidents = services(access_db[0]).resolve(IncidentService)
    listed = await incidents.list_all(worker_setup[3], worker_setup[4], context=AuditContext())
    assert [(item.get("unavailable"), item.get("omissions")) for item in listed["items"]] == [
        (True, [{"path": "", "reason": "classification_unavailable"}])
    ]


async def test_workers_and_reads_of_an_unsupported_run_wait_for_an_upgrade(
    queued_task, task_service, transaction_factory, worker_ids, worker_setup, services, access_db, monkeypatch
):
    from uuid import uuid4

    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.signals import SignalService

    _, runtime, _, actor, scope, _, _ = worker_setup
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    roll_back(monkeypatch)

    def ir_unsupported(refused):
        error = refused.value
        return (error.status, error.code, error.result) == (422, "WV-IR-UNSUPPORTED", UNSUPPORTED)

    # Reads, signals and workers answer ir_unsupported, not the permanent legacy answer.
    with pytest.raises(CatalogError) as refused:
        await runtime.read(actor, scope, queued_task.id, context=AuditContext())
    assert ir_unsupported(refused)
    signals = services(access_db[0]).resolve(SignalService)
    with pytest.raises(CatalogError) as refused:
        async with transaction_factory() as tx:
            await signals.deliver(
                tx, queued_task.id, "rolled-back", "approve", {}, actor=actor, scope=scope, context=AuditContext()
            )
    assert ir_unsupported(refused)
    with pytest.raises(CatalogError) as refused:
        async with transaction_factory() as tx:
            await task_service.heartbeat(tx, lease.proof)
    assert ir_unsupported(refused)
    with pytest.raises(CatalogError) as refused:
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), 7)
    assert ir_unsupported(refused)
    # Run lists still show the run as an unavailable item instead of failing.
    listed = await runtime.list(actor, scope, context=AuditContext())
    assert [item for item in listed["items"] if item["id"] == str(queued_task.id)] == [
        {
            "id": str(queued_task.id),
            "unavailable": True,
            "omissions": [{"path": "", "reason": "classification_unavailable"}],
        }
    ]
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT status FROM task_leases")) == "active"
        assert await session.scalar(text("SELECT count(*) FROM completion_receipts")) == 0
        assert await session.scalar(text("SELECT count(*) FROM signal_receipts")) == 0
    assert await policy_blocks(access_db) == 0
    monkeypatch.undo()
    async with transaction_factory() as tx:
        await task_service.complete(tx, lease.proof, uuid4(), 7)
    view = await runtime.read(actor, scope, queued_task.id, context=AuditContext())
    assert (view.state.status, view.state.output) == ("succeeded", 7)


async def test_cancelling_on_the_older_server_records_only_the_cancellation(
    queued_task, worker_setup, transaction_factory, access_db, monkeypatch
):
    from firefly_weave.contracts.runtime import UnavailableRunAcknowledgment
    from firefly_weave.definitions.models import CatalogError

    _, runtime, _, actor, scope, _, _ = worker_setup
    roll_back(monkeypatch)
    async with transaction_factory() as tx:
        acknowledgment = await runtime.cancel(
            tx, queued_task.id, "rolled back", actor=actor, scope=scope, context=AuditContext()
        )
    assert isinstance(acknowledgment, UnavailableRunAcknowledgment)
    monkeypatch.undo()
    assert await status(access_db, queued_task) == "cancelled"
    with pytest.raises(CatalogError, match="Legacy execution evidence is unavailable"):
        await runtime.read(actor, scope, queued_task.id, context=AuditContext())
    assert await policy_blocks(access_db) == 0


@pytest.mark.parametrize("advertised", [(), ("text.concat",), ("text.concat", "text.join")])
async def test_the_scanner_condition_agrees_with_artifact_import(access_db, monkeypatch, advertised):
    from firefly_weave.compiler.ir import UnsupportedIR, require_supported_ir
    from firefly_weave.runtime.admission import runnable, runnable_parameters

    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", advertised)
    current = {"irVersion": "weave/ir-v1alpha4", "features": ["text.concat"]}
    artifacts = [
        {"executable": current},
        {"executable": {**current, "features": ["text.join"]}},
        {"executable": {**current, "features": ["text.concat", "flow.forEach"]}},
        {"executable": {**current, "features": []}},
        {"executable": {"irVersion": "weave/ir-v1alpha3", "features": []}},
        {"executable": {"irVersion": "weave/ir-v1alpha3"}},
        {"executable": {"irVersion": "weave/ir-v9", "features": []}},
        {"executable": {"features": []}},
        {"executable": {**current, "irVersion": 4}},
        {"executable": {**current, "features": "text.concat"}},
        {"executable": {**current, "features": [1]}},
        {"executable": {**current, "features": ["text.concat", 1]}},
        {"executable": []},
        {},
        # Duplicate or non-string features, features that are not a list, odd IR versions and executables.
        {"executable": {**current, "features": ["text.concat", "text.concat"]}},
        {"executable": {**current, "features": [None]}},
        {"executable": {**current, "features": [["text.concat"]]}},
        {"executable": {**current, "features": {"text.concat": True}}},
        {"executable": {**current, "features": None}},
        {"executable": {**current, "irVersion": None}},
        {"executable": {**current, "irVersion": {"version": "weave/ir-v1alpha4"}}},
        {"executable": {"irVersion": "weave/ir-v1alpha1", "features": ["text.join"]}},
        {"executable": {"irVersion": "weave/ir-v1alpha2", "features": "flow.forEach"}},
        {"executable": "weave/ir-v1alpha4"},
        {"executable": None},
        {"executable": {}},
    ]
    async with access_db[1]() as session:
        for artifact in artifacts:
            try:
                require_supported_ir(artifact.get("executable"))
                expected = True
            except UnsupportedIR:
                expected = False
            actual = await session.scalar(
                text(f"SELECT {runnable('runs')} FROM (SELECT cast(:artifact AS jsonb) AS artifact) runs"),
                {"artifact": json.dumps(artifact), **runnable_parameters()},
            )
            assert actual is expected, artifact


@pytest.mark.parametrize(
    "query",
    [
        "SELECT {runnable} FROM runs",
        # Task offering, expired ready tasks and lease recovery evaluate it per task.
        "SELECT id FROM task_intents WHERE status='ready' AND (SELECT {runnable} FROM runs r WHERE r.id=run_id)",
        # The deadline scanner evaluates it only for a due run.
        "SELECT id FROM runs WHERE CASE WHEN state->>'status'='waiting' THEN {runnable} ELSE false END",
    ],
    ids=["run", "per-task", "due-run"],
)
async def test_the_scanner_condition_reads_the_artifact_once_per_row(access_db, query):
    from firefly_weave.runtime.admission import runnable, runnable_parameters

    run = "r" if "runs r" in query else "runs"
    async with access_db[1]() as session:
        plan = "\n".join(
            await session.scalars(
                text("EXPLAIN (VERBOSE, COSTS OFF) " + query.format(runnable=runnable(run))), runnable_parameters()
            )
        )
    # Each reference to the column fetches and decompresses the whole stored artifact again; the fenced subquery
    # extracts the executable once and every check reads that copy.
    assert plan.count(f"{run}.artifact") == 1, plan
