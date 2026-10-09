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

"""SQL terminal controls must preserve the kernel contract at the fetch boundary."""

import json
from uuid import UUID

import pytest
import test_parallel as parallel_tests
import test_waits as wait_tests
from sqlalchemy import text

pytestmark = pytest.mark.integration
author = wait_tests.author
waiting_run = wait_tests.waiting_run


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture):
    from types import SimpleNamespace

    fixture = parallel_tests.worker_runtime_fixture.__wrapped__(worker_runtime_fixture, SimpleNamespace(param="normal"))
    document = json.loads(fixture["source"])
    document["spec"]["timeoutSeconds"] = 600
    return {**fixture, "source": json.dumps(document)}


async def exhaust_pools(owner, project):
    await owner.execute(
        text(
            "UPDATE operation_policy SET limits=jsonb_set(jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT "
            "sum(value) FROM operation_usage WHERE metric='ordinary_bytes' AND project_id=:p)::bigint)),"
            "'{control_bytes}',to_jsonb((SELECT sum(value) FROM operation_usage WHERE metric IN "
            "('control_bytes','control_reserved') AND project_id=:p)::bigint))"
        ),
        {"p": project},
    )


async def assert_equivalent(owner, before, kind):
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent
    from firefly_weave.runtime.service import view_of

    event = (
        (
            await owner.execute(
                text("SELECT * FROM run_events WHERE run_id=:id AND type=:kind"), {"id": before["id"], "kind": kind}
            )
        )
        .mappings()
        .one()
    )
    accepted = RuntimeEvent(
        id=event["id"], type=kind, data=event["data"], timestamp=event["created_at"], sequence=event["sequence"]
    )
    expected = transition(
        RunState.model_validate_json(json.dumps(before["state"])), accepted, import_artifact(before["artifact"])
    )
    assert event["transition"] == expected.model_dump(mode="json")
    view = view_of(dict(before)).model_copy(
        update={"state": expected.state, "external_effects_may_continue": kind == "cancelled"}
    )
    assert event["response"] == view.model_dump(mode="json")
    assert (
        await owner.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": before["id"]})
        == event["transition"]["state"]
    )
    assert not await owner.scalar(
        text("SELECT count(*) FROM run_deadlines WHERE run_id=:id AND NOT consumed"), {"id": before["id"]}
    )
    assert not await owner.scalar(text("SELECT count(*) FROM task_leases WHERE status='active'"))
    assert not await owner.scalar(text("SELECT count(*) FROM task_intents WHERE status<>'cancelled'"))
    evidence = await owner.scalar(
        text("SELECT evidence FROM run_event_evidence WHERE run_id=:id AND id=:event"),
        {"id": before["id"], "event": event["id"]},
    )
    assert evidence["expected_transition_digest"] is None
    assert {"path": "/expected_transition_digest", "reason": "resource_limit"} in evidence["omissions"]
    assert (
        await owner.scalar(
            text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=:id"), {"id": before["id"]}
        )
        == 0
    )
    assert await owner.scalar(text("SELECT sum(value) FROM operation_usage WHERE metric='control_bytes'")) > 0

    fact = (
        (await owner.execute(text("SELECT * FROM run_facts WHERE run_id=:id"), {"id": before["id"]})).mappings().one()
    )
    assert fact["status"] == kind and fact["ended_at"] == event["created_at"]
    assert fact["updated_at"] == event["created_at"] and fact["last_event_sequence"] == event["sequence"]
    assert fact["failed_node_id"] is None and fact["failed_error_code"] is None and fact["active_incidents"] == 0
    assert not await owner.scalar(
        text("SELECT count(*) FROM task_facts WHERE run_id=:id AND (status<>'cancelled' OR ready_since IS NOT NULL)"),
        {"id": before["id"]},
    )
    assert not await owner.scalar(
        text("SELECT count(*) FROM step_facts WHERE run_id=:id AND status IN ('scheduled','running','waiting')"),
        {"id": before["id"]},
    )
    from operations_support import assert_usage

    await assert_usage(owner, {"tenant": before["tenant_id"], "project": before["project_id"]})


@pytest.mark.parametrize("kind", ["cancelled", "timed_out"])
async def test_sql_terminal_matches_sequential_kernel_at_full_pools(
    kind, waiting_run, access_db, author, client, headers, env_url, services, monkeypatch
):
    import firefly_weave.runtime.repository as repository
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    identifier = UUID(waiting_run)
    async with access_db[1].begin() as owner:
        before = (await owner.execute(text("SELECT * FROM runs WHERE id=:id"), {"id": identifier})).mappings().one()
        if kind == "timed_out":
            await owner.execute(text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second'"))
        await exhaust_pools(owner, author[2].project_id)
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)

    async def reject_payload_fetch(*args, **kwargs):
        raise AssertionError("Oversized terminal control fetched run payload")

    monkeypatch.setattr(repository.RuntimeRepository, "run", reject_payload_fetch)
    if kind == "cancelled":
        response = await client.post(f"{env_url}/runs/{waiting_run}/cancel", headers=headers, json={"reason": "stop"})
        assert response.status_code == 200, response.text
        assert response.json()["capacity_limited"] is True
        assert response.json()["omissions"] == [{"path": "/state", "reason": "resource_limit"}]
    else:
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 100) == 1
    async with access_db[1]() as owner:
        await assert_equivalent(owner, before, kind)


@pytest.mark.parametrize("kind", ["cancelled", "timed_out"])
async def test_sql_terminal_matches_parallel_kernel_and_fences_leases(
    kind, queued_task, task_service, transaction_factory, worker_ids, worker_setup, access_db, services, monkeypatch
):
    import firefly_weave.runtime.repository as repository
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.service import RuntimeService

    async with transaction_factory() as tx:
        leases = await task_service.claim(tx, worker_ids[0], 2)
    assert leases
    async with access_db[1].begin() as owner:
        before = (await owner.execute(text("SELECT * FROM runs WHERE id=:id"), {"id": queued_task.id})).mappings().one()
        assert before["state"]["branches"] and before["state"]["joins"]
        if kind == "timed_out":
            await owner.execute(text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second'"))
        await exhaust_pools(owner, worker_setup[4].project_id)
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)

    async def reject_payload_fetch(*args, **kwargs):
        raise AssertionError("Oversized terminal control fetched run payload")

    monkeypatch.setattr(repository.RuntimeRepository, "run", reject_payload_fetch)
    async with transaction_factory() as tx:
        if kind == "cancelled":
            result = (
                await services(access_db[0])
                .resolve(RuntimeService)
                .cancel(
                    tx, queued_task.id, "stop", actor=worker_setup[3], scope=worker_setup[4], context=AuditContext()
                )
            )
            assert result.capacity_limited
        else:
            assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 100) == 1
    async with access_db[1]() as owner:
        await assert_equivalent(owner, before, kind)
        statuses = list((await owner.execute(text("SELECT status FROM task_leases"))).scalars())
        assert set(statuses) == ({"control_revoked"} if kind == "cancelled" else {"revoked"})


async def test_sql_signal_timeout_preserves_earlier_accepted_signal(
    waiting_run, access_db, author, services, monkeypatch
):
    import firefly_weave.runtime.repository as repository
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService

    identifier = UUID(waiting_run)
    async with access_db[1].begin() as owner:
        await owner.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id='approval'")
        )
        await owner.execute(
            text(
                "INSERT INTO signal_receipts SELECT gen_random_uuid(),tenant_id,project_id,environment_id,run_id,"
                "'buffered','customer-approved',jsonb_build_object('approved',true),'owned',"
                "deadline-interval '1 second',false "
                "FROM run_deadlines WHERE run_id=:id AND node_id='approval'"
            ),
            {"id": identifier},
        )
        count = await owner.scalar(text("SELECT count(*) FROM run_events"))
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    async with UnitOfWork(access_db[0]).open(author[2]) as tx:
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 100) == 0
    async with access_db[1]() as owner:
        assert await owner.scalar(text("SELECT count(*) FROM run_events")) == count
        assert not await owner.scalar(text("SELECT count(*) FROM signal_receipts WHERE consumed"))
        assert not await owner.scalar(text("SELECT count(*) FROM wait_wakeups"))
        assert (
            await owner.scalar(text("SELECT state->>'status' FROM runs WHERE id=:id"), {"id": identifier}) == "waiting"
        )


@pytest.mark.parametrize(
    "waiting_run,mode",
    [
        (wait_tests.SOURCE, "timeout"),
        (wait_tests.SOURCE, "earlier"),
        (
            wait_tests.SOURCE.replace(
                "kind: signal\n      name: customer-approved\n      timeoutSeconds: 300\n"
                "      payloadSchema: {type: object, properties: {approved: {type: boolean}}, required: [approved]}",
                "kind: wait\n      durationSeconds: 300",
            ).replace("output: {ref: /steps/approval/output}", "output: {ref: /input}"),
            "duration",
        ),
    ],
    indirect=["waiting_run"],
)
@pytest.mark.parametrize("sql_path", [False, True])
async def test_capacity_rejection_still_allows_issued_signal_timeout(
    waiting_run, access_db, author, services, monkeypatch, sql_path, mode
):
    import firefly_weave.runtime.capacity as capacity
    import firefly_weave.runtime.repository as repository
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.deadlines import DeadlineService
    from firefly_weave.runtime.repository import RuntimeRepository

    identifier = UUID(waiting_run)
    uow = UnitOfWork(access_db[0])
    with monkeypatch.context() as lowered:
        lowered.setattr(capacity, "STATE_BYTES", 1)
        with pytest.raises(CatalogError) as rejected:
            async with uow.open(author[2]) as tx:
                runtime = RuntimeRepository(tx)
                row = await runtime.run(identifier)
                await runtime.require_work(identifier, row["state"])
        assert rejected.value.code == "WV-RUNTIME-LIMIT"
    async with access_db[1].begin() as owner:
        assert await owner.scalar(
            text("SELECT active FROM runtime_capacity_blocks WHERE run_id=:id"), {"id": identifier}
        )
        assert not await owner.scalar(
            text("SELECT count(*) FROM run_policy_blocks WHERE run_id=:id"), {"id": identifier}
        )
        await owner.execute(
            text("UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE node_id='approval'")
        )
        if mode == "earlier":
            await owner.execute(
                text(
                    "INSERT INTO signal_receipts SELECT gen_random_uuid(),tenant_id,project_id,environment_id,run_id,"
                    "'buffered','customer-approved',jsonb_build_object('approved',true),'owned',"
                    "deadline-interval '1 second',false FROM run_deadlines WHERE run_id=:id AND node_id='approval'"
                ),
                {"id": identifier},
            )
        count = await owner.scalar(text("SELECT count(*) FROM run_events"))
        assert await owner.scalar(
            text("SELECT deadline>clock_timestamp() FROM run_deadlines WHERE run_id=:id AND node_id='@run'"),
            {"id": identifier},
        )
    if sql_path:
        monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)
    async with uow.open(author[2]) as tx:
        assert await services(access_db[0]).resolve(DeadlineService).tick(tx, 100) == int(mode == "timeout")
    async with access_db[1]() as owner:
        assert await owner.scalar(text("SELECT state->>'status' FROM runs WHERE id=:id"), {"id": identifier}) == (
            "timed_out" if mode == "timeout" else "waiting"
        )
        assert await owner.scalar(text("SELECT count(*) FROM run_events")) == count + int(mode == "timeout")
        if mode != "timeout":
            assert not await owner.scalar(text("SELECT count(*) FROM wait_wakeups"))
            assert not await owner.scalar(text("SELECT count(*) FROM signal_receipts WHERE consumed"))


@pytest.mark.parametrize("point", ["before_projection", "after_projection"])
async def test_oversized_terminal_fact_failure_rolls_back_control_reserve(
    point, queued_task, task_service, transaction_factory, worker_ids, worker_setup, access_db, services, monkeypatch
):
    import firefly_weave.runtime.repository as repository
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.operations.facts import FactRepository
    from firefly_weave.operations.outbox import OutboxService
    from firefly_weave.runtime.service import RuntimeService

    async with transaction_factory() as tx:
        assert await task_service.claim(tx, worker_ids[0], 1)
    async with access_db[1].begin() as owner:
        await exhaust_pools(owner, worker_setup[4].project_id)
    tables = (
        "runs",
        "run_events",
        "task_intents",
        "task_leases",
        "run_facts",
        "step_facts",
        "task_facts",
        "incident_facts",
        "operation_usage",
        "operation_reservations",
        "operation_allocations",
        "event_deliveries",
        "integration_events",
        "operation_control_deliveries",
    )

    async def snapshot():
        async with access_db[1]() as owner:
            return {
                table: list(
                    (
                        await owner.execute(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)::text"))
                    ).scalars()
                )
                for table in tables
            }

    before = await snapshot()
    monkeypatch.setattr(repository, "RUN_FETCH_BYTES", 1)

    async def fail(*args, **kwargs):
        raise RuntimeError("terminal projection rollback")

    if point == "before_projection":
        monkeypatch.setattr(FactRepository, "project_terminal", fail)
    else:
        monkeypatch.setattr(OutboxService, "append", fail)
    with pytest.raises(RuntimeError, match="terminal projection rollback"):
        async with transaction_factory() as tx:
            await (
                services(access_db[0])
                .resolve(RuntimeService)
                .cancel(
                    tx, queued_task.id, "stop", actor=worker_setup[3], scope=worker_setup[4], context=AuditContext()
                )
            )
    assert await snapshot() == before


async def test_cancel_at_full_ordinary_quota_updates_facts_from_control_reserve(
    queued_task, task_service, transaction_factory, worker_ids, worker_setup, access_db, services
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.runtime.service import RuntimeService

    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with transaction_factory() as tx:
        await task_service.fail(tx, lease.proof, TaskError(completion_id=UUID(int=59), code="CONNECTION_LOST"))
    async with access_db[1].begin() as owner:
        before = (await owner.execute(text("SELECT * FROM runs WHERE id=:id"), {"id": queued_task.id})).mappings().one()
        opened = (await owner.execute(text("SELECT opened_at,updated_at FROM incident_facts"))).one()
        await exhaust_pools(owner, worker_setup[4].project_id)
    async with transaction_factory() as tx:
        result = (
            await services(access_db[0])
            .resolve(RuntimeService)
            .cancel(tx, queued_task.id, "stop", actor=worker_setup[3], scope=worker_setup[4], context=AuditContext())
        )
    assert result.state.status == "cancelled"
    async with access_db[1]() as owner:
        incident = (await owner.execute(text("SELECT opened_at,updated_at FROM incident_facts"))).one()
        assert incident.opened_at == opened.opened_at and incident.updated_at > opened.updated_at
        fact = (
            (
                await owner.execute(
                    text("SELECT status,active_incidents,ended_at,last_event_sequence FROM run_facts WHERE run_id=:id"),
                    {"id": queued_task.id},
                )
            )
            .mappings()
            .one()
        )
        assert fact["status"] == "cancelled" and fact["active_incidents"] == 0 and fact["ended_at"] is not None
        assert fact["last_event_sequence"] == result.state.accepted_sequence
        from operations_support import assert_usage

        await assert_usage(owner, {"tenant": before["tenant_id"], "project": before["project_id"]})
