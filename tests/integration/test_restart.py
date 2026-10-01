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

"""Fresh-process recovery, persisted waits, deadlines, and uncertain effects."""

import pytest
from test_waits import EVENT
from test_waits import author as author
from test_waits import waiting_run as waiting_run

pytestmark = pytest.mark.integration


async def test_wait_survives_fresh_application(client, headers, env_url, waiting_run, restart_app):
    fresh = await restart_app()
    response = await fresh.post(f"{env_url}/runs/{waiting_run}/signals", headers=headers, json=EVENT)
    assert response.status_code == 202, response.text
    run = await fresh.get(f"{env_url}/runs/{waiting_run}", headers=headers)
    assert run.json()["state"]["status"] == "succeeded"


@pytest.mark.parametrize("effect", ["read_only", "non_idempotent"])
async def test_two_scanners_recover_lost_lease(
    services, access_db, worker_setup, queued_task, transaction_factory, task_service, worker_ids, effect
):
    import asyncio

    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime.recovery import RecoveryService

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    recovery = services(access_db[0]).resolve(RecoveryService)
    actor, scope = worker_setup[3:5]
    reports = await asyncio.gather(*(recovery.scan(scope, 100, actor=actor, context=AuditContext()) for _ in range(2)))
    assert sum(r.resumed for r in reports) == (1 if effect == "read_only" else 0)
    assert sum(r.incidents for r in reports) == (1 if effect == "non_idempotent" else 0)
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker_ids[1], 1) == []
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE task_intents SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE "
                "next_attempt_at IS NOT NULL"
            )
        )
    async with transaction_factory() as tx:
        claimed = await task_service.claim(tx, worker_ids[1], 1)
    assert len(claimed) == (1 if effect == "read_only" else 0)
    if claimed:
        assert claimed[0].operation_key == lease.operation_key
        assert claimed[0].proof.generation == 2


async def test_background_timeout_and_shutdown(client, headers, env_url, waiting_run, restart_app, access_db):
    import asyncio

    from sqlalchemy import text

    fresh = await restart_app()
    loop = fresh._transport.app.state.recovery_loop
    assert loop.task is not None and not loop.task.done()
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second'"))
    async with asyncio.timeout(5):
        while True:
            view = (await fresh.get(f"{env_url}/runs/{waiting_run}", headers=headers)).json()
            if view["state"]["status"] == "timed_out":
                break
            await asyncio.sleep(0.05)
    task = loop.task
    await loop.close()
    await loop.close()
    assert task.done() and loop.engine is None


async def test_scheduler_is_execute_only_and_catalog_owner_has_no_bypass(access_db, scheduler_url, provisioned):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(scheduler_url, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            ids = set((await connection.execute(text("SELECT weave_tenant_ids()"))).scalars())
            assert ids == {s.tenant_id for s in provisioned[1]}
            for table in ("tenants", "runs", "task_leases", "environments"):
                with pytest.raises(DBAPIError):
                    await connection.execute(text(f"SELECT * FROM {table}"))
                await connection.rollback()
        async with access_db[1]() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.rolsuper,r.rolbypassrls,r.rolcanlogin FROM pg_proc p JOIN pg_roles r ON "
                        "r.oid=p.proowner WHERE p.proname='weave_tenant_ids'"
                    )
                )
            ).one()
            assert tuple(row) == (False, False, False)
    finally:
        await engine.dispose()


async def test_missing_scheduler_authority_keeps_startup_restricted(access_db):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from firefly_weave.app import make_app
    from firefly_weave.settings import Settings

    app = make_app(
        Settings(database_url=access_db[3].render_as_string(hide_password=False), scheduler_database_url=None)
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://weave.test") as client,
    ):
        assert not app.state.compatibility.ready
        assert app.state.compatibility.report.mode == "restricted"
        assert not app.state.compatibility.report.complete
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        for name in ("recovery_loop", "outbox_loop", "dispatcher", "kafka_loop", "provider_loop"):
            assert getattr(app.state, name, None) is None
        async with access_db[1]() as observer:
            assert await observer.scalar(text("SELECT count(*) FROM runs")) == 0
            assert await observer.scalar(text("SELECT count(*) FROM task_intents")) == 0
    assert app.state.resources.closed


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    effect = (
        request.node.callspec.params.get("effect", "read_only") if hasattr(request.node, "callspec") else "read_only"
    )
    document = worker_runtime_fixture["action"].model_dump(by_alias=True)
    document["spec"]["sideEffect"] = effect
    delay = request.node.callspec.params.get("retry_delay", 1) if hasattr(request.node, "callspec") else 1
    document["spec"]["retry"] = {"maxAttempts": 3, "initialDelaySeconds": delay, "maxDelaySeconds": max(2, delay)}
    action = load_definition(document)
    contract = {
        "taskType": "echo",
        "taskVersion": "1.2.0",
        "inputSchema": {},
        "outputSchema": {"type": "integer"},
        "sideEffect": effect,
        "timeoutSeconds": 30,
    }
    catalog = CatalogSnapshot.from_definitions([action], tasks=[contract])
    compiled = compile_source(worker_runtime_fixture["source"], format="json", catalog=catalog)
    assert compiled.ok
    return {**worker_runtime_fixture, "action": action, "catalog": catalog, "artifact": compiled.artifact}


@pytest.mark.parametrize("external_effect", [False, True])
@pytest.mark.parametrize("effect", ["non_idempotent"])
async def test_lost_worker_before_or_after_effect_never_reexecutes(
    services,
    access_db,
    worker_setup,
    queued_task,
    transaction_factory,
    task_service,
    worker_ids,
    effect,
    external_effect,
):
    from uuid import uuid4

    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.recovery import RecoveryService

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with access_db[1].begin() as external:
        await external.execute(text("CREATE TABLE external_effects(id integer PRIMARY KEY)"))
        if external_effect:
            await external.execute(text("INSERT INTO external_effects VALUES(1)"))
        await external.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    recovery = services(access_db[0]).resolve(RecoveryService)
    result = await recovery.scan(worker_setup[4], 100, actor=worker_setup[3], context=AuditContext())
    assert result.incidents == 1
    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker_ids[1], 1) == []
    with pytest.raises(CatalogError):
        async with transaction_factory() as tx:
            await task_service.complete(tx, lease.proof, uuid4(), 7)
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM external_effects")) == int(external_effect)
        assert await session.scalar(text("SELECT state->>'incident' FROM runs")) == "WV-TASK-AMBIGUOUS"


@pytest.mark.parametrize("state", ["queued", "suspended"])
async def test_run_deadline_terminates_nonwaiting_states(services, access_db, author, waiting_run, state):
    from sqlalchemy import text

    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    async with access_db[1].begin() as session:
        await session.execute(
            text("UPDATE runs SET state=jsonb_set(state,'{status}',to_jsonb(cast(:state AS text)))"), {"state": state}
        )
        await session.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id='@run'")
        )
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 100) == 1
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT state->>'status' FROM runs")) == "timed_out"


async def test_expired_unclaimed_action_opens_incident(services, access_db, worker_setup, queued_task):
    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime.recovery import RecoveryService

    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',"
                "to_jsonb((clock_timestamp()-interval '1 second')::text))"
            )
        )
    report = (
        await services(access_db[0])
        .resolve(RecoveryService)
        .scan(worker_setup[4], 100, actor=worker_setup[3], context=AuditContext())
    )
    assert report.incidents == 1
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT state->>'incident' FROM runs")) == "WV-TASK-DEADLINE"


@pytest.mark.parametrize("code,resumed", [("TRANSIENT", 1), ("PERMANENT", 0)])
async def test_only_classified_failure_retries_and_receipt_survives(
    services, access_db, worker_setup, queued_task, transaction_factory, task_service, worker_ids, code, resumed
):
    from uuid import uuid4

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.runtime.recovery import RecoveryService

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        error = TaskError(completion_id=uuid4(), code=code, outcome="failed")
        receipt = await task_service.fail(tx, lease.proof, error)
    result = (
        await services(access_db[0])
        .resolve(RecoveryService)
        .scan(worker_setup[4], 100, actor=worker_setup[3], context=AuditContext())
    )
    assert result.resumed == resumed
    assert result.incidents == 1 - resumed
    async with transaction_factory() as tx:
        assert await task_service.fail(tx, lease.proof, error) == receipt


async def test_scheduler_context_rejects_forgery_and_transaction_scope(services, access_db, provisioned, scheduler_url):
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.scheduler import _SchedulerScope, scopes
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.recovery import RecoveryService

    engine = create_async_engine(scheduler_url, hide_parameters=True)
    uow = UnitOfWork(access_db[0])
    recovery = services(access_db[0]).resolve(RecoveryService)
    try:
        verified = await scopes(engine, uow)
        assert len(verified) == 2
        wrong = next(s for s in provisioned[1] if s != verified[0].scope)
        with pytest.raises(AccessDenied):
            async with uow.open(wrong) as tx:
                await verified[0].verify(tx)
        with pytest.raises(AccessDenied):
            await recovery._scan_scheduled(_SchedulerScope(verified[0].scope, object()), 100)
        with pytest.raises(AccessDenied):
            await recovery.scan(verified[0].scope, 100)
    finally:
        await engine.dispose()


async def test_persisted_timeout_replays_without_clock(services, access_db, author, waiting_run):
    import json

    from sqlalchemy import text

    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second'"))
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        await services(access_db[0]).resolve(DeadlineService).tick(tx, 100)
    async with access_db[1]() as session:
        run = (await session.execute(text("SELECT * FROM runs"))).mappings().one()
        events = (await session.execute(text("SELECT * FROM run_events ORDER BY sequence"))).mappings().all()
    state = RunState.model_validate_json(json.dumps({"input": run["request"]["input"]}))
    artifact = import_artifact(run["artifact"])
    for row in events:
        event = RuntimeEvent.model_validate_json(
            json.dumps(
                {
                    "id": str(row["id"]),
                    "type": row["type"],
                    "data": row["data"],
                    "sequence": row["sequence"],
                    "timestamp": row["created_at"].isoformat(),
                }
            )
        )
        state = transition(state, event, artifact).state
    assert state.model_dump(mode="json") == run["state"]


async def test_suspended_ready_tasks_cannot_starve_expired_work(services, access_db, worker_setup):
    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.recovery import RecoveryService

    _, runtime, _, actor, scope, activation, _ = worker_setup
    for index in range(101):
        await runtime.start(
            actor,
            scope,
            StartRunRequest(activation_id=activation.id, input=3),
            f"starvation-{index}",
            context=AuditContext(),
        )
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',"
                "to_jsonb((clock_timestamp()-interval '1 second')::text))"
            )
        )
        await session.execute(
            text(
                "UPDATE runs SET state=jsonb_set(state,'{status}','\"suspended\"') "
                "WHERE id IN (SELECT run_id FROM task_intents ORDER BY id LIMIT 100)"
            )
        )
    report = await services(access_db[0]).resolve(RecoveryService).scan(scope, 100, actor=actor, context=AuditContext())
    assert report.incidents == 1


@pytest.mark.parametrize("retry_delay", [3])
async def test_project_fence_serializes_claim_and_preserves_recovered_backoff(
    services,
    access_db,
    worker_setup,
    queued_task,
    transaction_factory,
    task_service,
    worker_ids,
    monkeypatch,
    retry_delay,
):
    import asyncio
    from contextlib import suppress
    from uuid import uuid4

    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.service import RuntimeService

    original = RuntimeService.task_candidates
    returned, resume = asyncio.Event(), asyncio.Event()
    intercepted = False

    async def paused_candidates(self, tx, release_id, references):
        nonlocal intercepted
        identifiers = await original(self, tx, release_id, references)
        if not intercepted:
            intercepted = True
            assert len(identifiers) == 1
            returned.set()
            await resume.wait()
        return identifiers

    monkeypatch.setattr(RuntimeService, "task_candidates", paused_candidates)

    async def stale_claim():
        async with transaction_factory() as tx:
            return await task_service.claim(tx, worker_ids[0], 1)

    pending = asyncio.create_task(stale_claim())
    try:
        async with asyncio.timeout(5):
            await returned.wait()
        # Candidate selection and claim share the project fence: no competing writer
        # may recover/change this candidate while the first transaction is paused.
        async with asyncio.timeout(2):
            with pytest.raises(CatalogError) as rejected:
                async with transaction_factory() as tx:
                    await task_service.claim(tx, worker_ids[1], 1)
        assert rejected.value.status == 429 and rejected.value.code == "WV-OPERATION-CAPACITY"
        resume.set()
        async with asyncio.timeout(5):
            initial = await pending
        assert len(initial) == 1 and initial[0].proof.generation == 1
        first = initial[0]
        async with transaction_factory() as tx:
            await task_service.fail(
                tx, first.proof, TaskError(completion_id=uuid4(), code="TRANSIENT", outcome="failed")
            )
        recovery = services(access_db[0]).resolve(RecoveryService)
        result = await recovery.scan(worker_setup[4], 100, actor=worker_setup[3], context=AuditContext())
        assert result.resumed == 1
        async with transaction_factory() as tx:
            assert await tx.session.scalar(text("SELECT next_attempt_at>clock_timestamp() FROM task_intents"))
        async with transaction_factory() as tx:
            assert await task_service.claim(tx, worker_ids[1], 1) == [], "A fresh claim must honor persisted backoff"
            assert await tx.session.scalar(text("SELECT max(generation) FROM task_leases")) == 1

        # Observe the real database deadline passing; never rewrite next_attempt_at or mock the clock.
        async with asyncio.timeout(retry_delay + 5):
            while True:
                async with transaction_factory() as tx:
                    available = await tx.session.scalar(
                        text("SELECT next_attempt_at<=clock_timestamp() FROM task_intents")
                    )
                if available:
                    break
                await asyncio.sleep(0.05)
        async with transaction_factory() as tx:
            claimed = await task_service.claim(tx, worker_ids[0], 1)
        assert len(claimed) == 1
        assert claimed[0].proof.generation == 2
        assert claimed[0].operation_key == first.operation_key
    finally:
        resume.set()
        if not pending.done():
            pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
