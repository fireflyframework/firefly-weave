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

"""Real PostgreSQL operational reads, classification, pagination and legacy coverage."""

import pytest
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.contracts.run_views import RunStepQuery, RunSummaryQuery
from firefly_weave.operations.run_views import RunViewService
from firefly_weave.persistence.uow import UnitOfWork


@pytest.mark.integration
async def test_real_summary_and_steps_use_metadata_only(operations_case, monkeypatch):
    from firefly_weave.runtime.repository import RuntimeRepository

    case = operations_case
    run = await case.start()
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())

    async def forbidden(*args, **kwargs):
        pytest.fail("Metadata read fetched a full run")

    monkeypatch.setattr(RuntimeRepository, "run", forbidden)
    page = await service.summaries(case.actor, case.scope, RunSummaryQuery(), None, context=AuditContext())
    assert [item.id for item in page.items] == [run.id]
    assert page.items[0].workflow.name == "worker-flow"
    steps = await service.steps(case.actor, case.scope, run.id, RunStepQuery(), None, context=AuditContext())
    assert steps.complete and len(steps.items) == 1
    assert "output" not in steps.items[0].model_dump(mode="json")


@pytest.mark.integration
async def test_summary_filters_classification_and_archive_restore(operations_case):
    from firefly_weave.operations.fact_reads import FactReadRepository

    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "UPDATE run_facts SET business_key='invoice',correlation_key='thread',"
                "handled_errors=2,active_incidents=1 WHERE run_id=:run"
            ),
            {"run": run.id},
        )
    positive = [
        {"workflow": "worker-flow"},
        {"workflow": "worker-flow", "version": "1.0.0"},
        {"status": ["waiting", "running"]},
        {"origin": "manual"},
        {"started_after": "2000-01-01T00:00:00Z"},
        {"started_before": "2100-01-01T00:00:00Z"},
        {"include_test": True},
        {"top_level_only": True},
        {"business_key": "invoice"},
        {"correlation_key": "thread"},
        {"has_active_incident": True},
        {"has_handled_errors": True},
        {"include_archived": True},
        {"activation_id": str(case.activation.id)},
    ]
    negative = [
        {"workflow": "other"},
        {"workflow": "worker-flow", "version": "9.0.0"},
        {"status": ["failed"]},
        {"origin": "webhook"},
        {"started_after": "2100-01-01T00:00:00Z"},
        {"started_before": "2000-01-01T00:00:00Z"},
        {"business_key": "other"},
        {"correlation_key": "other"},
        {"has_active_incident": False},
        {"has_handled_errors": False},
        {"caller_run_id": str(run.id)},
        {"retried_from_run_id": str(run.id)},
        {"activation_id": str(run.id)},
    ]
    async with case.tx() as tx:
        repository = FactReadRepository(tx)
        for values in positive:
            query = RunSummaryQuery.model_validate_json(__import__("json").dumps(values))
            assert [row["run_id"] for row in await repository.summary_rows(query, None)] == [run.id], values
        for values in negative:
            query = RunSummaryQuery.model_validate_json(__import__("json").dumps(values))
            assert await repository.summary_rows(query, None) == [], values

    cases = [
        ("test=true", {"include_test": True}, {}),
        (
            "test=false,origin='call',caller_run_id=run_id,caller_node_id='work',caller_instance_key=''",
            {"caller_run_id": str(run.id)},
            {"top_level_only": True},
        ),
        (
            "origin='retry',caller_run_id=NULL,caller_node_id=NULL,caller_instance_key=NULL,retried_from_run_id=run_id",
            {"retried_from_run_id": str(run.id)},
            {"retried_from_run_id": str(case.activation.id)},
        ),
        (
            "active_incidents=0,handled_errors=0",
            {"has_active_incident": False, "has_handled_errors": False},
            {"has_active_incident": True, "has_handled_errors": True},
        ),
    ]
    for update, matches, excludes in cases:
        async with case.owner.begin() as session:
            await session.execute(text("UPDATE run_facts SET " + update + " WHERE run_id=:run"), {"run": run.id})
        async with case.tx() as tx:
            repository = FactReadRepository(tx)
            assert (
                len(
                    await repository.summary_rows(
                        RunSummaryQuery.model_validate_json(__import__("json").dumps(matches)), None
                    )
                )
                == 1
            )
            assert (
                await repository.summary_rows(
                    RunSummaryQuery.model_validate_json(__import__("json").dumps(excludes)), None
                )
                == []
            )


@pytest.mark.integration
async def test_legacy_steps_use_null_times_and_run_wide_coverage(operations_case):
    from firefly_weave.operations.fact_positions import StepPosition
    from firefly_weave.operations.fact_reads import FactReadRepository

    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO step_instances SELECT tenant_id,project_id,environment_id,"
                "id,key,'completed','null'::jsonb FROM runs CROSS JOIN "
                "unnest(ARRAY['echo[2]','echo[10]','@join:hidden']) AS key WHERE id=:run"
            ),
            {"run": run.id},
        )
        await session.execute(
            text('UPDATE run_facts SET node_kinds=node_kinds || \'{"echo":"action"}\'::jsonb WHERE run_id=:run'),
            {"run": run.id},
        )
    async with case.tx() as tx:
        repository = FactReadRepository(tx)
        rows, complete = await repository.step_rows(run.id, RunStepQuery(step="echo", limit=1), None)
        assert not complete and len(rows) == 2
        assert [row["instance_key"] for row in rows] == ["echo[10]", "echo[2]"]
        assert all(row["scheduled_at"] is None and row["started_at"] is None for row in rows)
        tail, complete = await repository.step_rows(run.id, RunStepQuery(step="unknown"), None)
        assert tail == [] and not complete
        rows, complete = await repository.step_rows(
            run.id, RunStepQuery(step="echo"), StepPosition(None, "echo", "echo[10]")
        )
        assert [row["instance_key"] for row in rows] == ["echo[2]"] and not complete


@pytest.mark.integration
async def test_explicit_step_output_is_paged_and_classified_once(operations_case, monkeypatch):
    from firefly_weave.runtime.repository import RuntimeRepository

    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO step_instances SELECT tenant_id,project_id,environment_id,"
                "id,'work','completed','null'::jsonb FROM runs WHERE id=:run "
                "ON CONFLICT(run_id,node_id) "
                "DO UPDATE SET output='null'::jsonb,status='completed'"
            ),
            {"run": run.id},
        )
    reads = []
    original = RuntimeRepository.run

    async def counted(repository, identifier, **kwargs):
        reads.append(identifier)
        return await original(repository, identifier, **kwargs)

    monkeypatch.setattr(RuntimeRepository, "run", counted)
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    page = await service.steps(
        case.actor, case.scope, run.id, RunStepQuery(include="output", limit=1), None, context=AuditContext()
    )
    assert reads == [run.id]
    assert "output" in page.items[0].model_dump() and page.items[0].output is None
    assert not page.items[0].omissions


@pytest.mark.integration
async def test_archive_restore_visibility_for_summary_and_runs_orders(operations_case):
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.run_lifecycle import RunLifecycleRequest
    from firefly_weave.contracts.run_views import RunListQuery
    from firefly_weave.runtime.lifecycle import RunLifecycleService

    case = operations_case
    run = await case.start()
    await case.access.grant(case.admin, case.actor.id, Grant(role="execution_manager", scope=case.scope))
    actor = await case.access.load_principal(case.actor.id)
    async with case.tx() as tx:
        await case.runtime.cancel(tx, run.id, "archive fixture", actor=actor, scope=case.scope, context=AuditContext())
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    lifecycle = RunLifecycleService(case.runtime)
    for action, revision in (("archive", 0), ("restore", 1)):
        await lifecycle.command(
            actor,
            case.scope,
            run.id,
            RunLifecycleRequest(expected_revision=revision, reason="fixture"),
            action,
            action=action,
            context=AuditContext(),
        )
        for order in ("started_desc", "started_asc", "updated_desc"):
            for include in (False, True):
                page = await service.summaries(
                    actor,
                    case.scope,
                    RunSummaryQuery(order=order, include_archived=include),
                    None,
                    context=AuditContext(),
                )
                assert bool(page.items) is (include or action == "restore")
                if page.items:
                    assert page.items[0].archived is (action == "archive")
        for order in ("started_desc", "started_asc", "updated_desc", "id"):
            for include in (False, True):
                page = await case.runtime.list(
                    actor, case.scope, query=RunListQuery(order=order, include_archived=include), context=AuditContext()
                )
                assert bool(page["items"]) is (include or action == "restore")
    assert (await case.rows("SELECT archived FROM run_archives WHERE run_id=:run", run=run.id)) == [{"archived": False}]


@pytest.mark.integration
async def test_summaries_withhold_cached_or_current_unsupported_and_policy_blocks(operations_case, monkeypatch):
    from firefly_weave.contracts import language_features
    from firefly_weave.operations.fact_reads import summary_of
    from firefly_weave.runtime import admission

    case = operations_case
    run = await case.start()
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())

    async def read():
        return (await service.summaries(case.actor, case.scope, RunSummaryQuery(), None, context=AuditContext())).items[
            0
        ]

    original = await read()
    assert not getattr(original, "unavailable", False)
    monkeypatch.setattr(admission, "POLICY", "new-classification")
    assert (await read()).unavailable
    monkeypatch.undo()
    features = language_features.ADVERTISED_FEATURES
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE run_facts SET pinned_features=cast(:features AS jsonb) WHERE run_id=:run"),
            {"features": __import__("json").dumps([features[0]]), "run": run.id},
        )
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", ())
    assert (await read()).unavailable
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", features)
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE run_facts SET classification_state='unsupported' WHERE run_id=:run"), {"run": run.id}
        )
    assert (await read()).unavailable
    assert (await case.rows("SELECT classification_state FROM run_facts WHERE run_id=:run", run=run.id))[0][
        "classification_state"
    ] == "unsupported"
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE run_facts SET classification_state='available' WHERE run_id=:run"), {"run": run.id}
        )
        await session.execute(
            text(
                "INSERT INTO run_policy_blocks(tenant_id,project_id,environment_id,run_id,reason_code) "
                "SELECT tenant_id,project_id,environment_id,id,'WV-LEGACY-UNAVAILABLE' FROM runs WHERE id=:run"
            ),
            {"run": run.id},
        )
    listed = await case.runtime.list(case.actor, case.scope, context=AuditContext())
    assert listed["items"][0].get("unavailable") is True
    assert (await read()).model_dump(mode="json") == {
        "id": str(run.id),
        "unavailable": True,
        "omissions": [{"path": "", "reason": "classification_unavailable"}],
    }
    row = (
        await case.rows(
            "SELECT *,false AS policy_blocked,false AS archived FROM run_facts WHERE run_id=:run", run=run.id
        )
    )[0]
    for changes in (
        {"started_at": None},
        {"updated_at": "bad"},
        {"status": None},
        {"workflow_name": "bad/name"},
        {"pinned_features": [None]},
        {"classification_policy": "old"},
        {"pinned_ir_version": "unknown"},
    ):
        assert summary_of({**row, **changes}).unavailable


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture, request):
    import json

    mode = getattr(request, "param", None)
    if mode not in {"parallel", "switch", "secret"}:
        return worker_runtime_fixture
    document = json.loads(worker_runtime_fixture["source"])
    branch = {
        "steps": [{"id": "copy", "kind": "transform", "value": {"literal": 3}}],
        "output": {"ref": "/steps/copy/output"},
    }
    if mode == "parallel":
        step = {"id": "fork", "kind": "parallel", "concurrency": 1, "branches": {"one": branch}}
    elif mode == "switch":
        step = {
            "id": "fork",
            "kind": "switch",
            "cases": [{"when": {"literal": True}, **branch}],
            "default": {"steps": [], "output": {"literal": None}},
        }
    else:
        step = {
            "id": "secret",
            "kind": "signal",
            "name": "secret",
            "timeoutSeconds": 300,
            "payloadSchema": {"type": "string", "x-secret": True},
        }
    document["spec"]["steps"] = [step, *document["spec"]["steps"]]
    document["spec"]["output"] = {"literal": None}
    document["spec"]["outputSchema"] = {}
    return {**worker_runtime_fixture, "source": json.dumps(document)}


@pytest.mark.integration
@pytest.mark.parametrize("worker_runtime_fixture", ["parallel", "switch"], indirect=True)
async def test_current_parallel_and_switch_steps_exclude_synthetic_and_are_complete(operations_case, monkeypatch):
    from firefly_weave.operations.fact_reads import FactReadRepository

    case = operations_case
    run = await case.start()
    assert await case.rows("SELECT node_id FROM step_instances WHERE run_id=:run AND left(node_id,1)='@'", run=run.id)

    async def forbidden(*args, **kwargs):
        pytest.fail("Current author facts must not invoke legacy fallback")

    monkeypatch.setattr(FactReadRepository, "historical_steps", forbidden)
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    page = await service.steps(case.actor, case.scope, run.id, RunStepQuery(), None, context=AuditContext())
    assert page.complete and {item.node_id for item in page.items} == {"fork", "copy", "work"}
    assert all(not item.instance_key.startswith("@") for item in page.items)


@pytest.mark.integration
@pytest.mark.parametrize("worker_runtime_fixture", ["parallel", "switch"], indirect=True)
async def test_legacy_parallel_and_switch_fallback_counts_only_author_instances(operations_case):
    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(text("DELETE FROM step_facts WHERE run_id=:run AND node_id='copy'"), {"run": run.id})
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    page = await service.steps(
        case.actor, case.scope, run.id, RunStepQuery(step="fork", include="output"), None, context=AuditContext()
    )
    assert not page.complete and [item.node_id for item in page.items] == ["fork"]
    assert all(not item.instance_key.startswith("@") for item in page.items)


@pytest.mark.integration
@pytest.mark.parametrize("worker_runtime_fixture", ["secret"], indirect=True)
async def test_step_output_redaction_hides_secret_canary(operations_case):
    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "UPDATE step_instances SET output='\"secret-read-canary\"'::jsonb WHERE run_id=:run "
                "AND node_id='secret'"
            ),
            {"run": run.id},
        )
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    page = await service.steps(
        case.actor, case.scope, run.id, RunStepQuery(include="output"), None, context=AuditContext()
    )
    assert "secret-read-canary" not in page.model_dump_json()
    assert "output" not in page.items[0].model_dump()
    assert page.items[0].omissions[0].path == "/output" and page.items[0].omissions[0].reason == "classified_secret"


@pytest.mark.integration
async def test_ten_thousand_facts_walk_exact_sql_order(operations_case):
    from firefly_weave.operations.fact_positions import RunPosition
    from firefly_weave.operations.fact_reads import FactReadRepository

    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        await case.runtime.cancel(
            tx, run.id, "pagination fixture", actor=case.actor, scope=case.scope, context=AuditContext()
        )
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r) || "
                "jsonb_build_object('id',md5(i::text)::uuid))).* FROM runs r CROSS JOIN "
                "generate_series(1,9999) i WHERE r.id=:run"
            ),
            {"run": run.id},
        )
        await session.execute(
            text(
                "INSERT INTO run_facts SELECT (jsonb_populate_record(NULL::run_facts,to_jsonb(f) || "
                "jsonb_build_object('run_id',md5(i::text)::uuid,'classification_state',"
                "CASE WHEN i%17=0 THEN 'unavailable' WHEN i%19=0 THEN 'unsupported' ELSE 'available' END,"
                "'started_at',CASE WHEN i%991=0 THEN NULL ELSE timestamptz '2026-01-01Z' "
                "+ (i/3)*interval '1 second' END,"
                "'updated_at',CASE WHEN i%991=0 THEN NULL ELSE timestamptz '2026-01-01Z' "
                "+ (i/5)*interval '2 seconds' END,"
                "'test',i%23=0,'handled_errors',i%4,'active_incidents',i%3,"
                "'origin',CASE WHEN i%7=0 THEN 'call' WHEN i%11=0 THEN 'retry' ELSE 'manual' END,"
                "'caller_run_id',CASE WHEN i%7=0 THEN f.run_id ELSE NULL END,"
                "'caller_node_id',CASE WHEN i%7=0 THEN 'work' ELSE NULL END,"
                "'caller_instance_key',CASE WHEN i%7=0 THEN '' ELSE NULL END,"
                "'retried_from_run_id',CASE WHEN i%7<>0 AND i%11=0 THEN f.run_id ELSE NULL END,"
                "'ended_at',timestamptz '2026-01-02Z'))).* FROM run_facts f CROSS JOIN "
                "generate_series(1,9999) i WHERE f.run_id=:run"
            ),
            {"run": run.id},
        )
        await session.execute(
            text(
                "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived) "
                "SELECT tenant_id,project_id,environment_id,run_id,true FROM run_facts WHERE run_id<>:run "
                "AND get_byte(uuid_send(run_id),0)<8"
            ),
            {"run": run.id},
        )
        await session.execute(
            text(
                "INSERT INTO run_policy_blocks(tenant_id,project_id,environment_id,run_id,reason_code) "
                "SELECT tenant_id,project_id,environment_id,run_id,'WV-LEGACY-UNAVAILABLE' FROM run_facts "
                "WHERE run_id<>:run AND get_byte(uuid_send(run_id),0)=8"
            ),
            {"run": run.id},
        )
    async with case.tx() as tx:
        repository = FactReadRepository(tx)
        for order, column, direction in (
            ("started_desc", "started_at", "DESC"),
            ("started_asc", "started_at", "ASC"),
            ("updated_desc", "updated_at", "DESC"),
        ):
            expected = [
                row["run_id"]
                for row in await case.rows(
                    f"SELECT run_id FROM run_facts ORDER BY {column} {direction} NULLS LAST,run_id {direction}"
                )
            ]
            assert len(expected) == 10000
            for limit in (1, 50, 100):
                query = RunSummaryQuery(order=order, limit=limit, include_test=True, include_archived=True)
                found, position = [], None
                while True:
                    rows = await repository.summary_rows(query, position)
                    assert len(rows) <= limit + 1
                    visible = rows[:limit]
                    found.extend(row["run_id"] for row in visible)
                    if len(rows) <= limit:
                        break
                    last = visible[-1]
                    position = RunPosition(last[column], last["run_id"])
                assert found == expected
        tail = await repository.summary_rows(
            RunSummaryQuery(order="started_asc"), RunPosition(None, __import__("uuid").UUID(int=2**128 - 1))
        )
        assert tail == []


@pytest.mark.integration
async def test_native_routes_sdk_and_scope_bound_cursors(operations_case, authenticated_client):
    from firefly_weave.api.transport import encode_cursor, encode_cursor_v2
    from firefly_weave.contracts.run_views import RunListQuery
    from firefly_weave.contracts.runtime import RunListFilters
    from firefly_weave.sdk.client import WeaveClient

    case = operations_case
    first, second = await case.start("first"), await case.start("second")
    client, tokens, scopes = authenticated_client
    prefix = (
        f"/api/v1/tenants/{case.scope.tenant_id}/projects/{case.scope.project_id}"
        f"/environments/{case.scope.environment_id}"
    )
    headers = {"Authorization": "Bearer " + tokens[0]}
    response = await client.get(prefix + "/run-summaries?limit=1", headers=headers)
    assert response.status_code == 200 and response.json()["items"][0]["id"] == str(second.id)
    cursor = response.json()["next_cursor"]
    assert (await client.get(prefix + "/run-summaries", params={"limit": 1, "cursor": cursor}, headers=headers)).json()[
        "items"
    ][0]["id"] == str(first.id)
    for path, status in ((f"/runs/{first.id}/steps", 200), (f"/runs/{first.id}/logs", 501)):
        assert (await client.get(prefix + path, headers=headers)).status_code == status
    bad = encode_cursor_v2(scopes[1], RunSummaryQuery().cursor_collection(), None, str(first.id))
    assert (await client.get(prefix + "/run-summaries", params={"cursor": bad}, headers=headers)).status_code == 422
    assert (
        await client.get(prefix + "/run-summaries", params={"cursor": cursor, "include_test": "true"}, headers=headers)
    ).status_code == 422
    async with WeaveClient(
        "https://platform.example", lambda: tokens[0], case.scope, transport=client._transport
    ) as sdk:
        page = await sdk.list_runs(query=RunListQuery(limit=1, status=["running", "waiting"]))
        assert page.items[0].id == second.id
        page2 = await sdk.list_runs(query=RunListQuery(limit=1, status=["waiting", "running"], cursor=page.next_cursor))
        assert page2.items[0].id == first.id and page2.next_cursor is None
        ordered = sorted([first.id, second.id])
        legacy = await case.runtime.list(
            case.actor, case.scope, limit=1, filters=RunListFilters(), context=AuditContext()
        )
        assert legacy["items"][0]["id"] == str(ordered[0]) and legacy["next_cursor"] == str(ordered[0])
        legacy_next = await case.runtime.list(
            case.actor, case.scope, limit=1, filters=RunListFilters(), cursor=ordered[0], context=AuditContext()
        )
        assert legacy_next["items"][0]["id"] == str(ordered[1])
        old = encode_cursor(case.scope, RunListFilters().cursor_collection(), ordered[0])
        page = await sdk.list_runs(order="id", cursor=old)
        assert [item.id for item in page.items] == [ordered[1]]


@pytest.mark.integration
@pytest.mark.parametrize("count", [499, 500, 501])
async def test_real_legacy_batches_and_c_collation(operations_case, count):
    from firefly_weave.operations.fact_positions import StepPosition
    from firefly_weave.operations.fact_reads import FactReadRepository

    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO step_instances SELECT tenant_id,project_id,environment_id,id,"
                "'work['||i||']','completed','null'::jsonb FROM runs CROSS JOIN generate_series(0,:count-1) i "
                "WHERE id=:run"
            ),
            {"run": run.id, "count": count},
        )
    async with case.tx() as tx:
        repository = FactReadRepository(tx)
        rows, complete = await repository.step_rows(run.id, RunStepQuery(limit=1), None)
        assert not complete and rows[0]["instance_key"] == "" and rows[1]["instance_key"] == "work[0]"
        rows, complete = await repository.step_rows(
            run.id, RunStepQuery(limit=2), StepPosition(None, "work", "work[1]")
        )
        assert [row["instance_key"] for row in rows] == ["work[200]", "work[201]", "work[202]"]


@pytest.mark.integration
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_legacy_cursor_closes_on_timeout_and_cancellation(operations_case, monkeypatch, cancel):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    from firefly_weave.operations.fact_reads import FactReadRepository

    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO step_instances SELECT tenant_id,project_id,environment_id,id,"
                "'work[1]','completed','null'::jsonb FROM runs WHERE id=:run"
            ),
            {"run": run.id},
        )
    opened, closed = asyncio.Event(), []
    original = AsyncSession.stream

    async def delayed(session, statement, *args, **kwargs):
        stream = await original(session, statement, *args, **kwargs)

        class Wrapped:
            def mappings(self):
                return self

            async def partitions(self, size):
                async for batch in stream.mappings().partitions(size):
                    opened.set()
                    await asyncio.Event().wait()
                    yield batch

            async def close(self):
                await stream.close()
                closed.append(stream.closed)

        return Wrapped()

    monkeypatch.setattr(AsyncSession, "stream", delayed)
    async with case.tx() as tx:
        task = asyncio.create_task(FactReadRepository(tx).historical_steps(run.id, None, None, 1))
        await opened.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            from firefly_weave.definitions.models import CatalogError

            with pytest.raises(CatalogError) as failure:
                await task
            assert (failure.value.status, failure.value.code) == (429, "WV-RUNTIME-LIMIT")
        assert closed == [True]
        assert await tx.session.scalar(text("SELECT 1")) == 1


@pytest.mark.integration
@pytest.mark.parametrize("count", [100000, 100001])
async def test_real_legacy_key_cap_independently_of_deadline(operations_case, monkeypatch, count):
    import json
    import os
    import time
    from pathlib import Path

    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.operations import fact_reads

    case = operations_case
    run = await case.start()
    async with case.tx() as tx:
        await case.runtime.cancel(
            tx, run.id, "legacy cap fixture", actor=case.actor, scope=case.scope, context=AuditContext()
        )
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO step_instances SELECT tenant_id,project_id,environment_id,id,"
                "'work['||i||']','completed','null'::jsonb FROM runs CROSS JOIN generate_series(0,:count-1) i "
                "WHERE id=:run"
            ),
            {"run": run.id, "count": count},
        )
        await session.execute(
            text(
                "INSERT INTO step_instances SELECT tenant_id,project_id,environment_id,id,"
                "'@control:'||i,'completed','null'::jsonb FROM runs CROSS JOIN generate_series(1,501) i WHERE id=:run"
            ),
            {"run": run.id},
        )
    # Isolate the key cap from the separately tested production two-second deadline.
    monkeypatch.setattr(fact_reads, "LEGACY_SECONDS", 60)
    started = time.monotonic()
    async with case.tx() as tx:
        repository = fact_reads.FactReadRepository(tx)
        if count == 100000:
            rows, complete = await repository.historical_steps(run.id, None, None, 1)
            assert not complete and [row["instance_key"] for row in rows] == ["work[0]", "work[10000]"]
            monkeypatch.setattr(fact_reads, "LEGACY_SECONDS", 2)
            deadline_rows, deadline_complete = await repository.historical_steps(run.id, None, None, 1)
            assert deadline_rows == rows and deadline_complete is False
        else:
            with pytest.raises(CatalogError) as failure:
                await repository.historical_steps(run.id, None, None, 1)
            assert (failure.value.status, failure.value.code) == (429, "WV-RUNTIME-LIMIT")
        assert await tx.session.scalar(text("SELECT 1")) == 1
    if target := os.environ.get("WEAVE_FACT_READ_EVIDENCE"):
        with Path(target).open("a") as evidence:
            evidence.write(
                json.dumps(
                    {
                        "keys": count,
                        "scan_seconds": time.monotonic() - started,
                        "test_deadline_seconds": 60,
                        "production_deadline_verified_separately": True,
                    }
                )
                + "\n"
            )


@pytest.mark.integration
async def test_current_and_legacy_step_order_matches_c_collation_and_null_tail(operations_case):
    from datetime import UTC, datetime

    from firefly_weave.operations.fact_positions import StepPosition
    from firefly_weave.operations.fact_reads import FactReadRepository

    case = operations_case
    run = await case.start()
    keys = ["send[2]", "send[10]", "send[2]#agent.tool~1", "A", "a"]
    at = datetime(2026, 1, 1, tzinfo=UTC)
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "UPDATE run_facts SET node_kinds=node_kinds || "
                '\'{"send":"action","A":"transform","a":"transform"}\'::jsonb WHERE run_id=:run'
            ),
            {"run": run.id},
        )
        for key in keys:
            await session.execute(
                text(
                    "INSERT INTO step_instances SELECT tenant_id,project_id,environment_id,id,"
                    ":key,'completed','null'::jsonb FROM runs WHERE id=:run"
                ),
                {"run": run.id, "key": key},
            )
        for key in ["A", "a"]:
            await session.execute(
                text(
                    "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,kind,status,"
                    "scheduled_at) "
                    "SELECT tenant_id,project_id,environment_id,id,:key,'transform','succeeded',:at "
                    "FROM runs WHERE id=:run"
                ),
                {"run": run.id, "key": key, "at": at},
            )
    async with case.tx() as tx:
        repo = FactReadRepository(tx)
        found, position = [], None
        while True:
            rows, complete = await repo.step_rows(run.id, RunStepQuery(limit=1), position)
            assert not complete
            if not rows:
                break
            row = rows[0]
            found.append(row["instance_key"] or row["node_id"])
            position = StepPosition(row["scheduled_at"], row["node_id"], row["instance_key"])
        assert found == ["A", "a", "work", "send[10]", "send[2]", "send[2]#agent.tool~1"]
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO step_instances SELECT tenant_id,project_id,environment_id,id,"
                "'broken[','completed','null'::jsonb FROM runs WHERE id=:run"
            ),
            {"run": run.id},
        )
    from firefly_weave.definitions.models import CatalogError

    async with case.tx() as tx:
        with pytest.raises(CatalogError) as failure:
            await FactReadRepository(tx).step_rows(run.id, RunStepQuery(step="absent"), None)
        assert failure.value.code == "WV-LEGACY-UNAVAILABLE"


@pytest.mark.integration
async def test_newer_runs_do_not_displace_started_desc_continuation(operations_case):
    from firefly_weave.api.transport import decode_cursor_v2

    case = operations_case
    first, second = await case.start("first"), await case.start("second")
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    query = RunSummaryQuery(limit=1)
    page = await service.summaries(case.actor, case.scope, query, None, context=AuditContext())
    assert page.items[0].id == second.id
    third = await case.start("third")
    next_page = await service.summaries(
        case.actor,
        case.scope,
        query,
        decode_cursor_v2(page.next_cursor, case.scope, query.cursor_collection()),
        context=AuditContext(),
    )
    assert next_page.items[0].id == first.id and next_page.next_cursor is None
    refreshed = await service.summaries(case.actor, case.scope, query, None, context=AuditContext())
    assert refreshed.items[0].id == third.id


@pytest.mark.integration
async def test_metadata_statements_never_select_run_payloads(operations_case):
    from sqlalchemy import event

    case = operations_case
    run = await case.start()
    statements = []
    engine = case.sessions.kw["bind"].sync_engine

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
        await service.summaries(case.actor, case.scope, RunSummaryQuery(), None, context=AuditContext())
        await service.steps(case.actor, case.scope, run.id, RunStepQuery(), None, context=AuditContext())
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert any("from run_facts" in sql for sql in statements)
    assert not any("from runs " in sql or "select runs.*" in sql or "s.output" in sql for sql in statements)
    assert not any("insert " in sql or "update run_facts" in sql for sql in statements)


@pytest.mark.integration
async def test_malformed_current_author_key_is_unavailable(operations_case):
    from firefly_weave.definitions.models import CatalogError

    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(text("UPDATE step_facts SET instance_key='work[' WHERE run_id=:run"), {"run": run.id})
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    with pytest.raises(CatalogError) as failure:
        await service.steps(case.actor, case.scope, run.id, RunStepQuery(), None, context=AuditContext())
    assert (failure.value.status, failure.value.code) == (409, "WV-LEGACY-UNAVAILABLE")


@pytest.mark.integration
async def test_unavailable_timeline_does_not_fetch_requested_output(operations_case, monkeypatch):
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.runtime.repository import RuntimeRepository

    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE run_facts SET classification_state='unsupported' WHERE run_id=:run"), {"run": run.id}
        )

    async def forbidden(*args, **kwargs):
        pytest.fail("Unavailable metadata fetched source payloads")

    monkeypatch.setattr(RuntimeRepository, "run", forbidden)
    service = RunViewService(UnitOfWork(case.sessions), AuthorizationService())
    with pytest.raises(CatalogError) as failure:
        await service.steps(
            case.actor, case.scope, run.id, RunStepQuery(include="output"), None, context=AuditContext()
        )
    assert failure.value.code == "WV-LEGACY-UNAVAILABLE"
