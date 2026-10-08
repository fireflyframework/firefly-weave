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

"""Recurring starts and duration waits on real, scoped PostgreSQL."""

import asyncio
from uuid import UUID

import pytest
import test_definitions as catalog_tests
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.operations.outbox import OutboxService
from firefly_weave.persistence.uow import UnitOfWork

pytestmark = pytest.mark.integration
author = catalog_tests.author
publication_request = catalog_tests.publication_request


@pytest.fixture
async def schedule_activation(author, access_db, provisioned, headers, project_url, env_url, publication_request):
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="operator", scope=author[2]))
    published = await catalog_tests.publish(author, headers, project_url, publication_request)
    assert published.status_code == 201, published.text
    response = await author[0].post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "schedule-activation"},
        json={
            "version_id": published.json()["id"],
            "artifact_digest": published.json()["digest"],
            "scope": author[2].model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def test_two_schedulers_create_one_run_for_same_occurrence(
    services, access_db, author, schedule_activation, replica_apps
):
    from firefly_weave.triggers.scheduler import Scheduler
    from firefly_weave.triggers.schedules import ScheduleRequest, ScheduleService

    actor = await access_db[2].load_principal(author[1].id)
    container = services(access_db[0])
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        schedule = await container.resolve(ScheduleService).save(
            tx,
            ScheduleRequest(cron="* * * * *", activation_id=schedule_activation, input={}),
            None,
            actor=actor,
            context=AuditContext(),
        )
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE schedules SET next_due_at=date_trunc('minute',clock_timestamp())"))
    schedulers = [app.state.pyfly.context.get_bean(Scheduler) for app in replica_apps]
    await asyncio.gather(*(s.tick(author[2], 20, actor=actor, context=AuditContext()) for s in schedulers))
    async with access_db[1]() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM schedule_occurrences WHERE schedule_id=:id"), {"id": schedule.id}
            )
            == 1
        )
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 1


@pytest.fixture
async def due_schedule(services, access_db, author, schedule_activation):
    from firefly_weave.triggers.schedules import ScheduleRequest, ScheduleService

    actor = await access_db[2].load_principal(author[1].id)
    service = services(access_db[0]).resolve(ScheduleService)
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        result = await service.save(
            tx,
            ScheduleRequest(cron="* * * * *", activation_id=schedule_activation, input={}),
            None,
            actor=actor,
            context=AuditContext(),
        )
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE schedules SET next_due_at=date_trunc('minute',clock_timestamp())"))
    return result


async def test_years_of_downtime_records_one_range_and_one_current_start(services, access_db, author, due_schedule):
    from firefly_weave.triggers.scheduler import Scheduler

    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE schedules SET next_due_at='2001-01-01T00:00:00Z'"))
    actor = await access_db[2].load_principal(author[1].id)
    assert await services(access_db[0]).resolve(Scheduler).tick(author[2], 20, actor=actor, context=AuditContext()) == 1
    async with access_db[1]() as session:
        rows = (
            await session.execute(
                text("SELECT kind,instant,through,observed_at FROM schedule_occurrences ORDER BY sequence")
            )
        ).all()
        assert len(rows) == 2
        assert rows[0].kind == "skipped" and rows[0].instant.year == 2001
        assert rows[1].kind == "started" and rows[1].instant == rows[1].observed_at.replace(second=0, microsecond=0)
        assert rows[0].through < rows[1].instant


@pytest.mark.parametrize("status", ["disabled", "deleted", "enabled"])
async def test_mutation_at_due_boundary_and_tombstones(client, headers, env_url, access_db, due_schedule, status):
    action = {"disabled": "disable", "deleted": "delete", "enabled": "enable"}[status]
    response = await client.post(
        f"{env_url}/schedules/{due_schedule.id}/{action}", headers={**headers, "If-Match": "1"}
    )
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["status"] == status
    assert row["revision"] == (2 if status == "enabled" else 1)
    detail = await client.get(f"{env_url}/schedules/{due_schedule.id}", headers=headers)
    assert detail.status_code == 200 and detail.json() == row
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM schedule_revisions")) == row["revision"]
        if status == "enabled":
            assert await session.scalar(text("SELECT next_due_at>clock_timestamp() FROM schedules"))


@pytest.mark.parametrize("disable_owner", [False, True])
async def test_revoked_owner_blocks_without_consuming(
    services, access_db, author, due_schedule, disable_owner, scheduler_url
):
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.access.scheduler import next_scope, tenant_page
    from firefly_weave.triggers.scheduler import Scheduler

    async with access_db[1].begin() as session:
        if disable_owner:
            await session.execute(text("UPDATE principals SET active=false WHERE id=:id"), {"id": author[1].id})
        else:
            await session.execute(
                text("DELETE FROM role_bindings WHERE principal_id=:id AND role='operator'"), {"id": author[1].id}
            )
    engine = create_async_engine(scheduler_url)
    try:
        tenants = await tenant_page(engine)
        for tenant in tenants:
            authority = await next_scope(UnitOfWork(access_db[0]), tenant)
            if authority.scope == author[2]:
                await services(access_db[0]).resolve(Scheduler)._scan_scheduled(authority, 10)
    finally:
        await engine.dispose()
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT status FROM schedules")) == "blocked"
        assert await session.scalar(text("SELECT count(*) FROM schedule_occurrences")) == 0
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0


async def test_occurrence_and_start_rollback_atomically(services, access_db, author, due_schedule):
    from firefly_weave.triggers.scheduler import Scheduler

    with pytest.raises(RuntimeError, match="rollback"):
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            assert await services(access_db[0]).resolve(Scheduler)._tick(tx, 10) == 1
            raise RuntimeError("rollback")
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM schedule_occurrences")) == 0
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
        assert await session.scalar(text("SELECT next_due_at<=clock_timestamp() FROM schedules"))


async def test_revision_edit_wins_before_firing(services, access_db, author, due_schedule):
    from firefly_weave.triggers.scheduler import Scheduler
    from firefly_weave.triggers.schedules import ScheduleRequest, ScheduleService

    actor = await access_db[2].load_principal(author[1].id)
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        result = (
            await services(access_db[0])
            .resolve(ScheduleService)
            .save(
                tx,
                ScheduleRequest(id=due_schedule.id, activation_id=due_schedule.activation_id, cron="* * * * *"),
                1,
                actor=actor,
                context=AuditContext(),
            )
        )
        assert result.revision == 2
    assert await services(access_db[0]).resolve(Scheduler).tick(author[2], 20, actor=actor, context=AuditContext()) == 0


async def test_future_cursor_never_regresses_and_bare_authority_rejected(services, access_db, author, due_schedule):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.scheduler import _SchedulerScope
    from firefly_weave.triggers.scheduler import Scheduler

    scheduler = services(access_db[0]).resolve(Scheduler)
    with pytest.raises(AccessDenied):
        await scheduler._scan_scheduled(_SchedulerScope(author[2], object()), 10)
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE schedules SET next_due_at=clock_timestamp()+interval '10 years'"))
        due = await session.scalar(text("SELECT next_due_at FROM schedules"))
    actor = await access_db[2].load_principal(author[1].id)
    assert await scheduler.tick(author[2], 20, actor=actor, context=AuditContext()) == 0
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT next_due_at FROM schedules")) == due


@pytest.fixture
async def duration_run(author, access_db, provisioned, headers, project_url, env_url, request):
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="operator", scope=author[2]))
    source = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: duration, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {}
  steps:
    - {id: pause, kind: wait, durationSeconds: 1}
  output: {literal: done}
"""
    mode = getattr(request, "param", None)
    if mode in {"overflow", "overflow_overall"}:
        source = source.replace("durationSeconds: 1", "durationSeconds: 9000000000000000")
        if mode == "overflow_overall":
            source = source.replace("  steps:", "  timeoutSeconds: 1\n  steps:")
    publication = {"format": "yaml", "source": source}
    published = await catalog_tests.publish(author, headers, project_url, publication)
    assert published.status_code == 201, published.text
    response = await author[0].post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "duration-activation"},
        json={
            "version_id": published.json()["id"],
            "artifact_digest": published.json()["digest"],
            "scope": author[2].model_dump(mode="json"),
        },
    )
    assert response.status_code == 201, response.text
    result = await author[0].post(
        env_url + "/runs",
        headers={**headers, "Idempotency-Key": "duration-run"},
        json={"activation_id": response.json()["id"], "input": None},
    )
    assert result.status_code == 201, result.text
    return result.json()


async def test_two_timer_replicas_wake_once_after_database_time_and_replay(services, access_db, author, duration_run):
    import json

    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    async def tick():
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            return await services(access_db[0]).resolve(DeadlineService).tick(tx, 10)

    assert await tick() == 0
    await asyncio.sleep(1.05)
    assert sum(await asyncio.gather(tick(), tick())) == 1
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 1
        assert await session.scalar(text("SELECT wakeup_kind FROM wait_wakeups")) == "elapsed"
        run = (await session.execute(text("SELECT artifact,state FROM runs"))).one()
        state = RunState()
        for row in (await session.execute(text("SELECT * FROM run_events ORDER BY sequence"))).mappings():
            event = RuntimeEvent(
                id=row["id"], type=row["type"], timestamp=row["created_at"], sequence=row["sequence"], data=row["data"]
            )
            state = transition(state, event, import_artifact(run.artifact)).state
        assert state == RunState.model_validate_json(json.dumps(run.state))
        assert state.status == "succeeded"


async def test_duration_restart_and_cancel(client, headers, env_url, duration_run, restart_app, access_db):
    fresh = await restart_app()
    async with asyncio.timeout(5):
        while True:
            view = (await fresh.get(f"{env_url}/runs/{duration_run['id']}", headers=headers)).json()
            if view["state"]["status"] == "succeeded":
                break
            await asyncio.sleep(0.05)
    assert view["state"]["output"] == "done"


async def test_duration_cancel_prevents_elapsed(services, client, headers, env_url, access_db, author, duration_run):
    from firefly_weave.runtime.deadlines import DeadlineService

    response = await client.post(
        f"{env_url}/runs/{duration_run['id']}/cancel", headers=headers, json={"reason": "stop"}
    )
    assert response.status_code == 200, response.text
    await asyncio.sleep(1.05)
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 10) == 0
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == 0
        assert await session.scalar(text("SELECT state->>'status' FROM runs")) == "cancelled"


async def test_duration_suspended_wakeup_stays_deferred(services, access_db, author, duration_run):
    from uuid import uuid4

    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RuntimeEvent
    from firefly_weave.runtime.repository import RuntimeRepository
    from firefly_weave.runtime.service import event_hash, view_of

    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        repo = RuntimeRepository(tx, services(access_db[0]).resolve(OutboxService))
        row = await repo.run(UUID(duration_run["id"]), lock=True)
        view = view_of(row)
        event = RuntimeEvent(
            id=uuid4(),
            type="incident_opened",
            sequence=2,
            timestamp=await repo.now(),
            data={"node_id": "pause", "code": "TEST"},
        )
        result = transition(view.state, event, import_artifact(row["artifact"]))
        await repo.persist(view.model_copy(update={"state": result.state}), event, result, event_hash(event))
    await asyncio.sleep(1.05)
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 10) == 1
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 10) == 0
        view = view_of(await RuntimeRepository(tx).run(UUID(duration_run["id"])))
        assert view.state.status == "suspended"
        assert len(view.state.deferred_results) == 1
        assert view.state.deferred_results[0].type == "wait_elapsed"
        assert view.state.active == ["pause"]


async def test_overall_timeout_wins_even_if_duration_due_first(services, access_db, author, duration_run):
    from firefly_weave.runtime.deadlines import DeadlineService

    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "INSERT INTO "
                "run_deadlines(tenant_id,project_id,environment_id,run_id,node_id,deadline,consumed) "
                "SELECT tenant_id,project_id,environment_id,id,'@run',clock_timestamp()+interval '.5 "
                "second',false FROM runs"
            )
        )
    await asyncio.sleep(1.05)
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        report = await services(access_db[0]).resolve(DeadlineService).scan(tx, 10)
        assert report.timed_out == 1 and report.elapsed == 0
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT state->>'status' FROM runs")) == "timed_out"
        assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='wait_elapsed'")) == 0


async def test_post_admission_database_clock_rejects_stale_start(services, access_db, author, schedule_activation):
    from datetime import timedelta

    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.repository import RuntimeRepository
    from firefly_weave.runtime.service import RuntimeService

    actor = await access_db[2].load_principal(author[1].id)
    with pytest.raises(CatalogError, match="Occurrence grace"):
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            now = await RuntimeRepository(tx).now()
            await (
                services(access_db[0])
                .resolve(RuntimeService)
                .start(
                    actor,
                    author[2],
                    StartRunRequest(activation_id=schedule_activation, input={}),
                    "stale",
                    context=AuditContext(),
                    tx=tx,
                    not_after=now - timedelta(seconds=1),
                )
            )
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0


async def test_noisy_multi_environment_tenant_cannot_monopolize_or_block_recovery(
    services, access_db, provisioned, scheduler_url, monkeypatch
):
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.access.scheduler import tenant_page
    from firefly_weave.contracts.access import Scope
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.scheduler import RecoveryLoop
    from firefly_weave.settings import Settings
    from firefly_weave.triggers.scheduler import Scheduler

    admin = await access_db[2].load_principal(provisioned[0].id)
    noisy, quiet = sorted(provisioned[1], key=lambda s: s.tenant_id)
    for index in range(5):
        await access_db[2].create_environment(
            admin, Scope(tenant_id=noisy.tenant_id, project_id=noisy.project_id), f"noisy-{index}"
        )
    engine = create_async_engine(scheduler_url)
    container = services(access_db[0])
    recovery = container.resolve(RecoveryService)
    schedules = container.resolve(Scheduler)
    calls = []
    original_recovery = recovery._scan_scheduled
    original_schedules = schedules._scan_scheduled

    async def recover(authority, limit):
        calls.append(("recovery", authority.scope, limit))
        return await original_recovery(authority, limit)

    async def fire(authority, limit):
        calls.append(("schedule", authority.scope, limit))
        if authority.scope.tenant_id == noisy.tenant_id:
            raise RuntimeError("Injected early-tenant failure")
        return await original_schedules(authority, limit)

    monkeypatch.setattr(recovery, "_scan_scheduled", recover)
    monkeypatch.setattr(schedules, "_scan_scheduled", fire)
    try:
        visited = set()
        for _ in range(6):
            # Recreate the loop each turn: traversal must be durable, not instance memory.
            loop = RecoveryLoop(
                Settings(scheduler_enabled=False, database_url=access_db[3].render_as_string(hide_password=False)),
                UnitOfWork(access_db[0]),
                recovery,
                schedules,
            )
            loop.engine = engine
            before = len(calls)
            await loop.cycle()
            turn = calls[before:]
            assert len(turn) == 4
            assert all(c[2] == 10 for c in turn)
            assert [c[0] for c in turn if c[1].tenant_id == quiet.tenant_id] == ["recovery", "schedule"]
            visited.update(c[1].environment_id for c in turn if c[1].tenant_id == noisy.tenant_id)
        assert len(visited) == 6
        # A bounded catalog page also rotates durably between independent calls.
        first = await tenant_page(engine, 1)
        second = await tenant_page(engine, 1)
        assert first != second
        assert {first[0], second[0]} == {noisy.tenant_id, quiet.tenant_id}
        async with access_db[1]() as session:
            assert await session.scalar(text("SELECT count(*) FROM scheduler_environment_cursors")) == 2
    finally:
        await engine.dispose()


async def test_backlogged_tenant_environments_leave_other_tenant_deadline_progress(
    services, access_db, provisioned, author, headers, publication_request, scheduler_url
):
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.contracts.access import Scope
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.scheduler import RecoveryLoop
    from firefly_weave.settings import Settings
    from firefly_weave.triggers.scheduler import Scheduler
    from firefly_weave.triggers.schedules import ScheduleRequest, ScheduleService

    noisy, quiet = provisioned[1]
    admin = await access_db[2].load_principal(provisioned[0].id)
    actor_id = author[1].id
    all_scopes = [noisy, quiet]
    for index in range(3):
        identifier = await access_db[2].create_environment(
            admin, Scope(tenant_id=noisy.tenant_id, project_id=noisy.project_id), f"backlog-{index}"
        )
        all_scopes.append(Scope(tenant_id=noisy.tenant_id, project_id=noisy.project_id, environment_id=identifier))
    graph = services(access_db[0])
    for scope in all_scopes:
        for role in ("developer", "deployer", "operator"):
            grant_scope = (
                Scope(tenant_id=scope.tenant_id, project_id=scope.project_id) if role == "developer" else scope
            )
            await access_db[2].grant(provisioned[0], actor_id, Grant(role=role, scope=grant_scope))
        actor = await access_db[2].load_principal(actor_id)
        project = f"/tenants/{scope.tenant_id}/projects/{scope.project_id}"
        environment = project + f"/environments/{scope.environment_id}"
        publication = publication_request
        if scope == quiet:
            publication = {
                **publication_request,
                "source": publication_request["source"]
                .replace(
                    "    - {id: value, kind: transform, value: {literal: 7}}",
                    "    - {id: value, kind: wait, durationSeconds: 300}",
                )
                .replace("  steps:", "  timeoutSeconds: 1\n  steps:"),
            }
        result = await author[0].post(
            project + "/workflows", headers={**headers, "Idempotency-Key": str(uuid4())}, json=publication
        )
        assert result.status_code == 201, result.text
        response = await author[0].post(
            environment + "/activations",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={
                "version_id": result.json()["id"],
                "artifact_digest": result.json()["digest"],
                "scope": scope.model_dump(mode="json"),
            },
        )
        assert response.status_code == 201, response.text
        activation = UUID(response.json()["id"])
        if scope == quiet:
            response = await author[0].post(
                environment + "/runs",
                headers={**headers, "Idempotency-Key": str(uuid4())},
                json={"activation_id": str(activation), "input": {}},
            )
            assert response.status_code == 201, response.text
            quiet_run = UUID(response.json()["id"])
        else:
            async with UnitOfWork(access_db[0]).open(scope) as tx:
                for _ in range(15):
                    await graph.resolve(ScheduleService).save(
                        tx,
                        ScheduleRequest(cron="* * * * *", activation_id=activation, input={}),
                        None,
                        actor=actor,
                        context=AuditContext(),
                    )
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE schedules SET next_due_at=date_trunc('minute',clock_timestamp())"))
        await session.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id='@run'")
        )
    engine = create_async_engine(scheduler_url)
    try:
        loop = RecoveryLoop(
            Settings(scheduler_enabled=False, database_url=access_db[3].render_as_string(hide_password=False)),
            UnitOfWork(access_db[0]),
            graph.resolve(RecoveryService),
            graph.resolve(Scheduler),
        )
        loop.engine = engine
        await loop.cycle()
        async with access_db[1]() as session:
            assert (
                await session.scalar(text("SELECT state->>'status' FROM runs WHERE id=:id"), {"id": quiet_run})
                == "timed_out"
            )
            assert await session.scalar(text("SELECT count(*) FROM schedule_occurrences WHERE kind='started'")) == 10
            assert await session.scalar(text("SELECT count(DISTINCT environment_id) FROM schedule_occurrences")) == 1
        await loop.cycle()
        async with access_db[1]() as session:
            assert await session.scalar(text("SELECT count(*) FROM schedule_occurrences WHERE kind='started'")) == 20
            assert await session.scalar(text("SELECT count(DISTINCT environment_id) FROM schedule_occurrences")) == 2
            assert (
                await session.scalar(text("SELECT count(*) FROM schedules WHERE next_due_at<=clock_timestamp()")) >= 40
            )
    finally:
        await engine.dispose()


async def test_queue_deadline_remains_effective_before_schedule_backlog(
    services, access_db, author, due_schedule, queued_task, worker_setup, scheduler_url
):
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.scheduler import RecoveryLoop
    from firefly_weave.settings import Settings
    from firefly_weave.triggers.scheduler import Scheduler

    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE task_intents SET payload=jsonb_set(payload,'{deadline}',"
                "to_jsonb((clock_timestamp()-interval '1 second')::text)) WHERE run_id=:run"
            ),
            {"run": queued_task.id},
        )
    graph = services(access_db[0])
    engine = create_async_engine(scheduler_url)
    try:
        loop = RecoveryLoop(
            Settings(scheduler_enabled=False, database_url=access_db[3].render_as_string(hide_password=False)),
            UnitOfWork(access_db[0]),
            graph.resolve(RecoveryService),
            graph.resolve(Scheduler),
        )
        loop.engine = engine
        await loop.cycle()
        async with access_db[1]() as session:
            assert (
                await session.scalar(text("SELECT state->>'incident' FROM runs WHERE id=:run"), {"run": queued_task.id})
                == "WV-TASK-DEADLINE"
            )
            assert await session.scalar(text("SELECT count(*) FROM schedule_occurrences WHERE kind='started'")) == 1
    finally:
        await engine.dispose()


async def test_schedule_read_current_authority_and_wrong_scope(services, access_db, author, provisioned, due_schedule):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.triggers.schedules import ScheduleService

    actor = await access_db[2].load_principal(author[1].id)
    service = services(access_db[0]).resolve(ScheduleService)
    with pytest.raises(AccessDenied):
        async with UnitOfWork(access_db[0]).open(provisioned[1][1]) as tx:
            await service.read(tx, actor=actor, context=AuditContext())
    async with access_db[1].begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role IN ('viewer','operator')"), {"id": actor.id}
        )
    with pytest.raises(AccessDenied):
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            await service.read(tx, actor=actor, context=AuditContext())


async def test_disable_prevents_due_start_and_enable_rebinds_modifier(
    services, access_db, author, due_schedule, client, headers, env_url
):
    from firefly_weave.triggers.scheduler import Scheduler

    actor = await access_db[2].load_principal(author[1].id)
    response = await client.post(f"{env_url}/schedules/{due_schedule.id}/disable", headers={**headers, "If-Match": "1"})
    assert response.status_code == 200
    assert await services(access_db[0]).resolve(Scheduler).tick(author[2], 10, actor=actor, context=AuditContext()) == 0
    response = await client.post(f"{env_url}/schedules/{due_schedule.id}/enable", headers={**headers, "If-Match": "1"})
    assert response.status_code == 200
    assert response.json()["principal_id"] == str(actor.id)
    assert response.json()["revision"] == 2
    assert await services(access_db[0]).resolve(Scheduler).tick(author[2], 10, actor=actor, context=AuditContext()) == 0


@pytest.mark.parametrize("duration_run", ["overflow_overall", "overflow"], indirect=True)
async def test_accepted_duration_overflow_keeps_overall_or_terminate_only(
    services, access_db, author, duration_run, request
):
    from uuid import uuid4

    from firefly_weave.contracts.operations import IncidentResolution
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.operations.incidents import IncidentService
    from firefly_weave.runtime.recovery import RecoveryService

    overall = request.node.callspec.params["duration_run"] == "overflow_overall"
    assert duration_run["state"]["status"] == "suspended"
    assert duration_run["state"]["incident"] == "WV-RUNTIME-DEADLINE-RANGE"
    graph = services(access_db[0])
    actor = await access_db[2].load_principal(author[1].id)
    async with access_db[1]() as session:
        deadlines = (await session.execute(text("SELECT node_id,deadline FROM run_deadlines"))).all()
        assert [row.node_id for row in deadlines] == (["@run"] if overall else [])
        if overall:
            assert await session.scalar(
                text(
                    "SELECT d.deadline=e.created_at+interval '1 second' FROM run_deadlines d "
                    "JOIN run_events e ON e.run_id=d.run_id WHERE e.type='started'"
                )
            )
        assert await session.scalar(text("SELECT count(*) FROM task_intents")) == 0
        assert await session.scalar(text("SELECT count(*) FROM step_instances")) == 0
        incident = (await session.execute(text("SELECT id,revision FROM incidents WHERE status='active'"))).one()
    await asyncio.sleep(1.05)
    reports = await asyncio.gather(
        *(graph.resolve(RecoveryService).scan(author[2], 10, actor=actor, context=AuditContext()) for _ in range(2))
    )
    assert sum(report.timed_out for report in reports) == int(overall)
    assert sum(report.elapsed + report.resumed for report in reports) == 0
    again = await graph.resolve(RecoveryService).scan(author[2], 10, actor=actor, context=AuditContext())
    assert again.timed_out == again.elapsed == again.resumed == 0
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM wait_wakeups")) == int(overall)
        assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='wait_elapsed'")) == 0
        assert await session.scalar(text("SELECT count(*) FROM run_events WHERE type='timed_out'")) == int(overall)
        assert await session.scalar(text("SELECT state->>'status' FROM runs")) == (
            "timed_out" if overall else "suspended"
        )
        assert await session.scalar(text("SELECT count(*) FROM task_intents")) == 0
    if not overall:
        for kind in ("retry_safe", "accept_reconciled_result"):
            resolution = IncidentResolution(receipt_id=uuid4(), kind=kind, reason="cannot retry wait")
            with pytest.raises(CatalogError, match="can only terminate"):
                async with UnitOfWork(access_db[0]).open(author[2]) as tx:
                    await graph.resolve(IncidentService).resolve(
                        tx,
                        incident.id,
                        resolution,
                        incident.revision,
                        actor=actor,
                        scope=author[2],
                        context=AuditContext(),
                    )
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            await graph.resolve(IncidentService).resolve(
                tx,
                incident.id,
                IncidentResolution(receipt_id=uuid4(), kind="terminate", reason="stop overflowing wait"),
                incident.revision,
                actor=actor,
                scope=author[2],
                context=AuditContext(),
            )
        async with access_db[1]() as session:
            assert await session.scalar(text("SELECT state->>'status' FROM runs")) == "cancelled"
            assert await session.scalar(text("SELECT count(*) FROM run_deadlines")) == 0
