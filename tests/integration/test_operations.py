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

"""Real PostgreSQL operational limits; every allocated fixture is retained."""

from datetime import timedelta
from uuid import uuid4

import pytest
import test_outbox as outbox_tests
import test_waits as wait_tests
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration


async def test_scheduler_null_page_is_rejected_under_execute_only_role(migration_db):
    async with migration_db() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE weave_scheduler"))
        for value in (None, 0, 17):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(text("SELECT weave_scheduler_tenants(:size)"), {"size": value})


async def test_project_admission_fence_precedes_business_rows(access_db, provisioned):
    from firefly_weave.persistence.uow import UnitOfWork

    scope = provisioned[1][0]
    async with UnitOfWork(access_db[0]).open(scope) as tx:
        fenced = await tx.session.scalar(
            text("SELECT weave_operation_fenced(:tenant,:project)"),
            {"tenant": scope.tenant_id, "project": scope.project_id},
        )
        assert fenced is True


async def test_debug_limit_rejects_fifth_live_session_across_instances(access_db, provisioned):
    from firefly_weave.contracts.access import Scope
    from firefly_weave.persistence.uow import UnitOfWork

    scope = provisioned[1][0]
    project = Scope(tenant_id=scope.tenant_id, project_id=scope.project_id)
    actor = provisioned[0]
    uow = UnitOfWork(access_db[0])
    async with uow.open(project) as tx:
        for _ in range(4):
            await tx.session.execute(
                text(
                    "INSERT INTO debug_sessions(tenant_id,project_id,id,creator_id,state) VALUES(:t,:p,:id,:actor,'{}')"
                ),
                {"t": project.tenant_id, "p": project.project_id, "id": uuid4(), "actor": actor.id},
            )
    async with UnitOfWork(access_db[0]).open(project) as tx:
        with pytest.raises(DBAPIError):
            await tx.session.execute(
                text(
                    "INSERT INTO debug_sessions(tenant_id,project_id,id,creator_id,state) VALUES(:t,:p,:id,:actor,'{}')"
                ),
                {"t": project.tenant_id, "p": project.project_id, "id": uuid4(), "actor": actor.id},
            )
        await tx.session.rollback()


async def test_metadata_read_does_not_wait_for_project_mutation_fence(access_db, provisioned):
    import asyncio

    from firefly_weave.persistence.uow import UnitOfWork

    scope = provisioned[1][0]
    async with UnitOfWork(access_db[0]).open(scope):
        async with asyncio.timeout(0.2):
            async with UnitOfWork(access_db[0]).open(scope, mutation=False) as read:
                assert await read.session.scalar(text("SELECT count(*) FROM environments")) == 1
                assert not await read.session.scalar(
                    text("SELECT weave_operation_fenced(:tenant,:project)"),
                    {"tenant": scope.tenant_id, "project": scope.project_id},
                )


async def test_retention_is_server_planned_scoped_and_delete_trials_rollback(access_db, provisioned):
    import json

    from firefly_weave.persistence.uow import UnitOfWork

    scope = provisioned[1][0].model_copy(update={"environment_id": None})
    actor = provisioned[0]
    expired, held, live = uuid4(), uuid4(), uuid4()
    async with access_db[1].begin() as owner:
        await owner.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('weave.operations:'||:t||':'||:p,0))"),
            {"t": str(scope.tenant_id), "p": str(scope.project_id)},
        )
        for identifier in (expired, held, live):
            await owner.execute(
                text(
                    "INSERT INTO debug_sessions(tenant_id,project_id,id,creator_id,state,expires_at) "
                    "VALUES(:t,:p,:id,:actor,'{}',clock_timestamp()+cast(:age AS interval))"
                ),
                {
                    "t": scope.tenant_id,
                    "p": scope.project_id,
                    "id": identifier,
                    "actor": actor.id,
                    "age": timedelta(hours=1) if identifier == live else timedelta(days=-2),
                },
            )
    uow = UnitOfWork(access_db[0])
    # The only delete is inside this deliberately rolled-back transaction.
    with pytest.raises(RuntimeError, match="owned deletion trial rollback"):
        async with uow.open(scope) as tx:
            await tx.session.execute(
                text(
                    "INSERT INTO retention_holds(tenant_id,project_id,debug_id,reference_id) "
                    "VALUES(:t,:p,:id,:reference)"
                ),
                {"t": scope.tenant_id, "p": scope.project_id, "id": held, "reference": uuid4()},
            )
            plan = await tx.session.scalar(
                text("SELECT weave_retention_plan(:actor,:limit)"), {"actor": actor.id, "limit": 100}
            )
            if isinstance(plan, str):
                plan = json.loads(plan)
            assert plan["scope"]["project_id"] == str(scope.project_id)
            assert plan["principal_id"] == str(actor.id)
            assert plan["complete"] is True and plan["next_cursor"] is None
            assert plan["cutoff"] < plan["created_at"] < plan["expires_at"]
            assert {v["id"] for v in plan["candidates"]} == {str(expired), str(held)}
            assert next(v for v in plan["candidates"] if v["id"] == str(held))["reason"] == "referenced"
            result = await tx.session.scalar(
                text("SELECT weave_retention_apply(:id,:actor)"), {"id": plan["id"], "actor": actor.id}
            )
            if isinstance(result, str):
                result = json.loads(result)
            assert result["deleted"] == [str(expired)]
            assert (
                await tx.session.scalar(text("SELECT count(*) FROM debug_sessions WHERE id=:id"), {"id": expired}) == 0
            )
            assert await tx.session.scalar(text("SELECT count(*) FROM debug_sessions WHERE id=:id"), {"id": held}) == 1
            raise RuntimeError("owned deletion trial rollback")
    async with uow.open(scope, mutation=False) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM debug_sessions")) == 3
        assert not await tx.session.scalar(text("SELECT has_table_privilege(current_user,'debug_sessions','DELETE')"))


async def test_storage_counter_is_atomic_and_shared_by_application_instances(access_db, provisioned):
    from firefly_weave.persistence.uow import UnitOfWork

    scope = provisioned[1][0].model_copy(update={"environment_id": None})
    with pytest.raises(RuntimeError, match="rollback counter trial"):
        async with UnitOfWork(access_db[0]).open(scope) as tx:
            before = await tx.session.scalar(
                text("SELECT coalesce(sum(value),0) FROM operation_usage WHERE metric='ordinary_bytes'")
            )
            await tx.session.execute(
                text(
                    "INSERT INTO debug_sessions(tenant_id,project_id,id,creator_id,state) "
                    "VALUES(:t,:p,:id,:actor,cast(:state AS jsonb))"
                ),
                {
                    "t": scope.tenant_id,
                    "p": scope.project_id,
                    "id": uuid4(),
                    "actor": provisioned[0].id,
                    "state": '{"owned":"trial"}',
                },
            )
            after = await tx.session.scalar(
                text("SELECT coalesce(sum(value),0) FROM operation_usage WHERE metric='ordinary_bytes'")
            )
            assert after - before >= 64 * 1024 * 1024
            assert not await tx.session.scalar(
                text("SELECT has_table_privilege(current_user,'operation_usage','UPDATE')")
            )
            raise RuntimeError("rollback counter trial")
    async with UnitOfWork(access_db[0]).open(scope, mutation=False) as tx:
        assert await tx.session.scalar(text("SELECT count(*) FROM debug_sessions")) == 0


async def test_after_commit_callbacks_are_bounded_and_discarded_on_rollback(access_db, provisioned):
    from firefly_weave.persistence.uow import UnitOfWork

    seen = []
    scope = provisioned[1][0]
    uow = UnitOfWork(access_db[0])
    async with uow.open(scope) as tx:
        assert all(tx.on_commit(lambda: seen.append("committed")) for _ in range(64))
        assert not tx.on_commit(lambda: seen.append("overflow"))
        assert not seen
    assert seen == ["committed"] * 64
    with pytest.raises(RuntimeError, match="rollback callback"):
        async with uow.open(scope) as tx:
            assert tx.on_commit(lambda: seen.append("rolled_back"))
            raise RuntimeError("rollback callback")
    assert seen == ["committed"] * 64
    async with uow.open(scope) as tx:
        assert tx.on_commit(lambda: (_ for _ in ()).throw(RuntimeError("exporter failed")))
        assert tx.on_commit(lambda: seen.append("after_failure"))
    assert seen[-1] == "after_failure"


author = wait_tests.author
waiting_run = wait_tests.waiting_run
setup = outbox_tests.setup


async def test_cancel_uses_reserved_control_when_ordinary_pool_is_full(
    client, headers, env_url, waiting_run, access_db, author, services
):
    async with access_db[1].begin() as owner:
        reserved = await owner.scalar(
            text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=cast(:id AS uuid)"),
            {"id": waiting_run},
        )
        assert reserved > 16 * 1024 * 1024
        await owner.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT "
                "sum(value) FROM operation_usage WHERE metric='ordinary_bytes' AND project_id=:p)::bigint))"
            ),
            {"p": author[2].project_id},
        )
    response = await client.post(
        f"{env_url}/runs/{waiting_run}/cancel", headers=headers, json={"reason": "owned capacity trial"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"]["status"] == "cancelled"
    async with access_db[1]() as owner:
        assert (
            await owner.scalar(
                text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=cast(:id AS uuid)"),
                {"id": waiting_run},
            )
            == 0
        )
        actual = await owner.scalar(
            text("SELECT sum(value) FROM operation_usage WHERE metric='control_bytes' AND project_id=:p"),
            {"p": author[2].project_id},
        )
        assert 0 < actual < reserved
        assert (
            await owner.scalar(
                text("SELECT count(*) FROM run_events WHERE run_id=cast(:id AS uuid)"), {"id": waiting_run}
            )
            == 2
        )


async def test_compatibility_catalog_is_bounded_readonly_and_execute_only(migration_db):
    async with migration_db() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE weave_scheduler"))
        assert not await session.scalar(text("SELECT has_table_privilege(current_user,'runs','SELECT')"))
        assert list((await session.execute(text("SELECT weave_compatibility_tenants(NULL,16)"))).scalars()) == []
        for size in (None, 0, 17):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(text("SELECT weave_compatibility_tenants(NULL,:size)"), {"size": size})
        for size in (None, 0, 26):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("SELECT * FROM weave_compatibility_page(:tenant,NULL,NULL,:size)"),
                        {"tenant": uuid4(), "size": size},
                    )
        rows = (
            await session.execute(
                text("SELECT * FROM weave_compatibility_page(:tenant,NULL,NULL,25)"), {"tenant": uuid4()}
            )
        ).all()
        assert rows == []


async def test_runtime_capacity_block_uses_preallocated_metadata_at_full_ordinary_pool(waiting_run, access_db, author):
    from uuid import UUID

    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.runtime.capacity import RuntimeCapacityError

    async with access_db[1].begin() as owner:
        await owner.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT "
                "sum(value) FROM operation_usage WHERE metric='ordinary_bytes' AND project_id=:p)::bigint))"
            ),
            {"p": author[2].project_id},
        )
        before = await owner.scalar(text("SELECT state FROM runs WHERE id=cast(:id AS uuid)"), {"id": waiting_run})
        reserved = await owner.scalar(
            text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=cast(:id AS uuid)"),
            {"id": waiting_run},
        )
    with pytest.raises(CatalogError) as error:
        async with UnitOfWork(access_db[0]).open(author[2]) as tx:
            tx.session.info["weave_runtime_candidate"] = UUID(waiting_run)
            raise RuntimeCapacityError()
    assert error.value.code == "WV-RUNTIME-LIMIT"
    async with access_db[1]() as owner:
        assert (
            await owner.scalar(
                text("SELECT active FROM runtime_capacity_blocks WHERE run_id=cast(:id AS uuid)"), {"id": waiting_run}
            )
            is True
        )
        assert (
            await owner.scalar(text("SELECT state FROM runs WHERE id=cast(:id AS uuid)"), {"id": waiting_run}) == before
        )
        assert (
            await owner.scalar(
                text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=cast(:id AS uuid)"),
                {"id": waiting_run},
            )
            == reserved
        )


async def test_native_inventory_restricts_effects_but_keeps_terminal_control(
    waiting_run, client, headers, env_url, access_db
):
    app = client._transport.app
    assert app.state.compatibility.ready
    async with access_db[1].begin() as owner:
        await owner.execute(
            text(
                "UPDATE runs SET "
                "artifact=jsonb_set(artifact,'{executable,irVersion}','\"weave/unknown\"'::jsonb) WHERE "
                "id=cast(:id AS uuid)"
            ),
            {"id": waiting_run},
        )
    report = await app.state.compatibility.scan()
    assert not report.complete and report.mode == "restricted"
    assert any(f.code == "inventory_incomplete" for f in report.findings)
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 503
    assert (await client.get(env_url + "/runs", headers=headers)).status_code == 200
    assert (await client.post(env_url + "/runs", headers=headers, json={})).status_code == 503
    cancelled = await client.post(
        f"{env_url}/runs/{waiting_run}/cancel", headers=headers, json={"reason": "restricted owned trial"}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["unavailable"]


async def test_native_committed_transition_and_request_emit_safe_telemetry(
    waiting_run, client, headers, env_url, monkeypatch
):
    telemetry = client._transport.app.state.telemetry_service
    records = []
    monkeypatch.setattr(telemetry, "record", lambda kind, **values: records.append((kind, values)))
    result = await client.post(
        f"{env_url}/runs/{waiting_run}/cancel", headers=headers, json={"reason": "private-canary-do-not-record"}
    )
    assert result.status_code == 200, result.text
    assert sum(kind == "transition" for kind, _ in records) == 1
    request = [values for kind, values in records if kind == "request"]
    assert len(request) == 1 and request[0]["operation"] == "runs.cancel"
    assert "private-canary" not in repr(records) and waiting_run not in repr(records)


async def test_recovery_failure_cannot_rollback_another_runs_terminal_control(
    waiting_run, queued_task, worker_setup, access_db, services, monkeypatch
):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.capacity import RuntimeCapacityError
    from firefly_weave.runtime.recovery import RecoveryService

    async with access_db[1].begin() as owner:
        await owner.execute(
            text(
                "UPDATE run_deadlines SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=cast(:id AS uuid)"
            ),
            {"id": waiting_run},
        )
        await owner.execute(
            text(
                "UPDATE task_intents SET "
                "payload=jsonb_set(payload,'{deadline}',to_jsonb((clock_timestamp()-interval '1 "
                "second')::text)) WHERE run_id=:id"
            ),
            {"id": queued_task.id},
        )

    async def reject(*args, **kwargs):
        raise RuntimeCapacityError()

    monkeypatch.setattr("firefly_weave.runtime.recovery.transition", reject)
    recovery = services(access_db[0]).resolve(RecoveryService)
    try:
        await recovery.scan(worker_setup[4], 10, actor=worker_setup[3], context=AuditContext())
    except CatalogError as error:
        assert error.code == "WV-RUNTIME-LIMIT"
    async with access_db[1]() as owner:
        assert (
            await owner.scalar(
                text("SELECT state->>'status' FROM runs WHERE id=cast(:id AS uuid)"), {"id": waiting_run}
            )
            == "timed_out"
        )
        assert (
            await owner.scalar(
                text("SELECT active FROM runtime_capacity_blocks WHERE run_id=:id"), {"id": queued_task.id}
            )
            is True
        )


async def test_maximal_state_terminal_serialization_fits_reserved_pool(
    waiting_run, access_db, author, client, headers, env_url
):
    import json

    from firefly_weave.runtime.capacity import STATE_BYTES, logical_size
    from firefly_weave.runtime.models import BranchState, JoinState, RunState

    async with access_db[1].begin() as owner:
        state = await owner.scalar(text("SELECT state FROM runs WHERE id=cast(:id AS uuid)"), {"id": waiting_run})
        # A typed maximal retained-state fixture: no accepted history row is changed.
        model = RunState.model_validate_json(json.dumps(state))
        model.steps.update({f"node{i}": "x" * 940000 for i in range(34)})
        model.branches.update(
            {f"b{i}": BranchState(owner="root", name=f"b{i}", target="approval") for i in range(1000)}
        )
        model.joins.update(
            {
                f"j{i}": JoinState(
                    owner="root", node_id="approval", mode="all", branches=[f"b{i}"], concurrency=1, required_count=1
                )
                for i in range(1000)
            }
        )
        size = logical_size(model, STATE_BYTES)
        assert 30 * 1024 * 1024 < size < STATE_BYTES
        await owner.execute(
            text("UPDATE runs SET state=cast(:state AS jsonb) WHERE id=cast(:id AS uuid)"),
            {"state": model.model_dump_json(), "id": waiting_run},
        )
        reserved = await owner.scalar(
            text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=cast(:id AS uuid)"),
            {"id": waiting_run},
        )
        await owner.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT "
                "sum(value) FROM operation_usage WHERE metric='ordinary_bytes' AND "
                "project_id=:p)::bigint)),'{control_bytes}',to_jsonb((SELECT sum(value) FROM operation_usage WHERE "
                "metric IN ('control_bytes','control_reserved') AND project_id=:p)::bigint))"
            ),
            {"p": author[2].project_id},
        )
    response = await client.post(
        f"{env_url}/runs/{waiting_run}/cancel", headers=headers, json={"reason": "bounded terminal fixture"}
    )
    assert response.status_code == 200, response.text[:1000]
    async with access_db[1]() as owner:
        event = (
            (
                await owner.execute(
                    text(
                        "SELECT transition,response,octet_length(transition::text)+octet_length(response::text) AS "
                        "bytes FROM run_events WHERE run_id=cast(:id AS uuid) AND type='cancelled'"
                    ),
                    {"id": waiting_run},
                )
            )
            .mappings()
            .one()
        )
        assert event["transition"]["state"]["steps"] == model.steps
        assert event["response"]["state"]["steps"] == model.steps
        assert event["bytes"] > 60 * 1024 * 1024
        actual = await owner.scalar(
            text("SELECT sum(value) FROM operation_usage WHERE metric='control_bytes' AND project_id=:p"),
            {"p": author[2].project_id},
        )
        assert event["bytes"] <= actual < reserved
        assert (
            await owner.scalar(
                text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=cast(:id AS uuid)"),
                {"id": waiting_run},
            )
            == 0
        )


async def test_reserved_terminal_fanout_and_source_revocation_at_full_pools(
    setup, waiting_run, access_db, client, headers, env_url
):
    from firefly_weave.access.audit import AuditContext

    for index in range(63):
        await setup.subscriptions.save(
            setup.actor, setup.scope, setup.request.model_copy(update={"name": f"sink{index}"}), context=AuditContext()
        )
    async with access_db[1].begin() as owner:
        await owner.execute(
            text(
                "UPDATE operation_policy SET limits=jsonb_set(jsonb_set(limits,'{ordinary_bytes}',to_jsonb((SELECT "
                "sum(value) FROM operation_usage WHERE metric='ordinary_bytes' AND "
                "project_id=:p)::bigint)),'{control_bytes}',to_jsonb((SELECT sum(value) FROM operation_usage WHERE "
                "metric IN ('control_bytes','control_reserved') AND project_id=:p)::bigint))"
            ),
            {"p": setup.scope.project_id},
        )
        reserved = await owner.scalar(
            text("SELECT amount FROM operation_reservations WHERE kind='run' AND resource_id=cast(:id AS uuid)"),
            {"id": waiting_run},
        )
    result = await client.post(
        f"{env_url}/runs/{waiting_run}/cancel", headers=headers, json={"reason": "full fanout owned trial"}
    )
    assert result.status_code == 200, result.text
    async with access_db[1]() as owner:
        assert await owner.scalar(text("SELECT count(*) FROM operation_control_deliveries")) == 64
        assert (
            await owner.scalar(text("SELECT sum(value) FROM operation_usage WHERE metric='control_outbox_pending'"))
            == 64
        )
        assert (
            await owner.scalar(
                text("SELECT sum(value) FROM operation_usage WHERE metric='control_bytes' AND project_id=:p"),
                {"p": setup.scope.project_id},
            )
            < reserved
        )
    await setup.subscriptions.bindings.revoke(setup.actor, setup.scope, setup.sub.binding_id, context=AuditContext())
    await setup.subscriptions.disable(setup.actor, setup.scope, setup.sub.id, context=AuditContext())
    async with access_db[1]() as owner:
        assert (
            await owner.scalar(
                text("SELECT revoked FROM connection_source_bindings WHERE id=:id"), {"id": setup.sub.binding_id}
            )
            is True
        )
        assert (
            await owner.scalar(text("SELECT active FROM event_subscriptions WHERE id=:id"), {"id": setup.sub.id})
            is False
        )
        assert (
            await owner.scalar(
                text("SELECT sum(amount) FROM operation_reservations WHERE resource_id IN (:binding,:subscription)"),
                {"binding": setup.sub.binding_id, "subscription": setup.sub.id},
            )
            == 0
        )


async def test_factual_inventory_detects_binding_fingerprint_and_version_mismatch(setup, waiting_run, access_db):
    from firefly_weave.operations.compatibility import classify_inventory_row

    async with access_db[1]() as owner:
        rows = (
            (
                await owner.execute(
                    text("SELECT * FROM weave_compatibility_page(:tenant,NULL,NULL,25)"),
                    {"tenant": setup.scope.tenant_id},
                )
            )
            .mappings()
            .all()
        )
        assert {r["kind"] for r in rows} >= {"run", "activation", "connection", "subscription"}
        actual = [
            (r["kind"], classify_inventory_row(r["kind"], r["payload"], r["facts_valid"], setup.registry)) for r in rows
        ]
        assert all(code is None for _, code in actual), actual
    with pytest.raises(RuntimeError, match="rollback fact corruption"):
        async with access_db[1].begin() as owner:
            await owner.execute(
                text("UPDATE connection_source_bindings SET source_fingerprint='mismatched' WHERE id=:id"),
                {"id": setup.sub.binding_id},
            )
            await owner.execute(
                text(
                    "UPDATE runs SET activation=jsonb_set(activation,'{request,version_id}',to_jsonb(cast(:fake AS "
                    "text))) WHERE id=cast(:id AS uuid)"
                ),
                {"fake": str(uuid4()), "id": waiting_run},
            )
            rows = (
                (
                    await owner.execute(
                        text("SELECT * FROM weave_compatibility_page(:tenant,NULL,NULL,25)"),
                        {"tenant": setup.scope.tenant_id},
                    )
                )
                .mappings()
                .all()
            )
            findings = {
                r["kind"]: classify_inventory_row(r["kind"], r["payload"], r["facts_valid"], setup.registry)
                for r in rows
            }
            assert findings["subscription"] == findings["run"] == "inventory_incomplete"
            assert findings["activation"] is None
            raise RuntimeError("rollback fact corruption")


async def test_inventory_checks_its_own_role_and_endpoint_with_scheduler_disabled(access_db, scheduler_url):
    from types import SimpleNamespace

    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.operations.compatibility import CompatibilityService
    from firefly_weave.settings import Settings

    telemetry = SimpleNamespace(record=lambda *a, **k: None, inventory=lambda *a, **k: None)
    runtime_url = access_db[3].render_as_string(hide_password=False)
    owner_url = access_db[1].kw["bind"].url.render_as_string(hide_password=False)
    for catalog_url, expected in ((scheduler_url, True), (owner_url, False), (runtime_url, False)):
        service = CompatibilityService(
            Settings(database_url=runtime_url, scheduler_database_url=catalog_url, scheduler_enabled=False),
            ConnectorRegistry(),
            None,
            None,
            telemetry,
        )
        try:
            assert (await service.scan()).mode == ("ready" if expected else "restricted")
        finally:
            await service.close()
    from sqlalchemy import make_url

    other_database = make_url(scheduler_url).set(database="unrelated_database").render_as_string(hide_password=False)
    service = CompatibilityService(
        Settings(database_url=runtime_url, scheduler_database_url=other_database, scheduler_enabled=False),
        ConnectorRegistry(),
        None,
        None,
        telemetry,
    )
    assert (await service.scan()).findings[0].code == "authority_missing"


async def test_inventory_rejects_column_only_privilege_on_new_owned_login(access_db, scheduler_url):
    import secrets
    from types import SimpleNamespace

    from conftest import retain_resource
    from sqlalchemy import make_url

    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.operations.compatibility import CompatibilityService
    from firefly_weave.settings import Settings

    username, password = "weave_d4_column_" + uuid4().hex, secrets.token_urlsafe(32)
    async with access_db[1].begin() as owner:
        await owner.execute(text(f"CREATE ROLE {username} LOGIN PASSWORD '{password}' IN ROLE weave_scheduler"))
        retain_resource("role", username)
        await owner.execute(text(f"GRANT SELECT(state) ON runs TO {username}"))
        assert not await owner.scalar(text("SELECT has_table_privilege(:role,'runs','SELECT')"), {"role": username})
        assert await owner.scalar(text("SELECT has_any_column_privilege(:role,'runs','SELECT')"), {"role": username})
    catalog = make_url(scheduler_url).set(username=username, password=password).render_as_string(hide_password=False)
    service = CompatibilityService(
        Settings(
            database_url=access_db[3].render_as_string(hide_password=False),
            scheduler_database_url=catalog,
            scheduler_enabled=False,
        ),
        ConnectorRegistry(),
        None,
        None,
        SimpleNamespace(record=lambda *a, **k: None, inventory=lambda *a, **k: None),
    )
    try:
        report = await service.scan()
        assert report.mode == "restricted"
        assert report.findings[0].code == "authority_missing"
    finally:
        await service.close()
