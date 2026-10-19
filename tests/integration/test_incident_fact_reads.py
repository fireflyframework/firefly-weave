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

"""Real PostgreSQL incident reads: facts, filters, classification, authority and pagination without run payloads."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.incident_queries import IncidentQuery
from firefly_weave.contracts.operations import IncidentResolution, IncidentView
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.incidents import IncidentService
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.runtime.service import RuntimeService

pytestmark = pytest.mark.integration

UNAVAILABLE = [{"path": "", "reason": "classification_unavailable"}]


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture):
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import load_definition

    # With a retry policy, a lost connection opens an incident as soon as the worker reports it.
    action = worker_runtime_fixture["action"].model_dump(by_alias=True)
    action["spec"]["retry"] = {"maxAttempts": 3, "initialDelaySeconds": 1, "maxDelaySeconds": 2}
    action = load_definition(action)
    contract = worker_runtime_fixture["catalog"].resolve("TaskCapability", "echo@1.2.0").definition.value
    return {
        **worker_runtime_fixture,
        "action": action,
        "catalog": CatalogSnapshot.from_definitions([action], tasks=[contract]),
    }


def incidents(case) -> IncidentService:
    return IncidentService(case.runtime, case.access, case.tasks)


async def open_incident(case, task_service, worker_ids, key="incident"):
    from firefly_weave.contracts.workers import TaskError

    run = await case.start(key)
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    async with case.tx() as tx:
        await task_service.fail(tx, lease.proof, TaskError(completion_id=uuid4(), code="CONNECTION_LOST"))
    rows = await case.rows("SELECT * FROM incidents WHERE run_id=:run", run=run.id)
    assert len(rows) == 1 and rows[0]["status"] == "active"
    return run, rows[0]


async def seed(case, run_id, count, *, prefix="seed"):
    """Incidents with tied and unknown opening times: every fifth minute repeats and every seventh is unknown."""
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,incident_key,node_id,generation,"
                "origin_code,code,status,revision) SELECT md5(:prefix||r.id::text||g)::uuid,r.tenant_id,"
                "r.project_id,r.environment_id,r.id,:prefix||':'||g,'work',g,'WV-TASK-FAILED',"
                "CASE WHEN g%4=0 THEN 'WV-TASK-AMBIGUOUS' ELSE 'WV-TASK-FAILED' END,"
                "(ARRAY['active','closed','resolved'])[1+g%3],1 FROM runs r,generate_series(1,:count) g "
                "WHERE r.id=:run"
            ),
            {"prefix": prefix, "run": run_id, "count": count},
        )
        await session.execute(
            text(
                "INSERT INTO incident_facts(tenant_id,project_id,environment_id,incident_id,node_id,instance_key,"
                "opened_at,updated_at,workflow_name,workflow_version,test,opened_at_estimated) SELECT i.tenant_id,"
                "i.project_id,i.environment_id,i.id,'work','',CASE WHEN i.generation%7=0 THEN NULL ELSE "
                "timestamptz '2026-10-01T00:00:00Z'+(i.generation%5)*interval '1 minute' END,"
                "timestamptz '2026-10-02T00:00:00Z',f.workflow_name,f.workflow_version,false,i.generation%11=0 "
                "FROM incidents i JOIN run_facts f ON f.run_id=i.run_id WHERE i.run_id=:run "
                "AND i.incident_key LIKE :prefix||':%'"
            ),
            {"prefix": prefix, "run": run_id},
        )


def capture(case):
    statements: list[str] = []
    engine = case.sessions.kw["bind"].sync_engine

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", record)


def forbid_run_payloads(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("incident read loaded a run payload")

    monkeypatch.setattr(RuntimeRepository, "run", forbidden)
    monkeypatch.setattr(RuntimeService, "read", forbidden)


def position_of(scope, query, run_id, cursor):
    from firefly_weave.api.transport import decode_cursor, decode_cursor_v2
    from firefly_weave.operations.fact_positions import run_position

    collection = query.cursor_collection(run_id)
    if query.order == "id":
        return decode_cursor(cursor, scope, collection)
    return run_position(decode_cursor_v2(cursor, scope, collection))


async def walk(case, values, *, run_id=None):
    service, position, found = incidents(case), None, []
    query = IncidentQuery(**values)
    while True:
        page = await service.list_all(
            case.actor, case.scope, run_id=run_id, query=query, position=position, context=AuditContext()
        )
        assert len(page["items"]) <= query.limit
        found += [item["id"] for item in page["items"]]
        if page["next_cursor"] is None:
            return found
        position = position_of(case.scope, query, run_id, page["next_cursor"])


async def expected_order(case, order, *, statuses=None, run_id=None):
    direction = {"opened_desc": "DESC", "opened_asc": "ASC"}.get(order)
    sort = f"f.opened_at {direction} NULLS LAST,i.id {direction}" if direction else "i.id ASC"
    rows = await case.rows(
        "SELECT i.id FROM incidents i LEFT JOIN incident_facts f ON f.incident_id=i.id "
        "WHERE (cast(:statuses AS text[]) IS NULL OR i.status=ANY(cast(:statuses AS text[]))) "
        "AND (cast(:run AS uuid) IS NULL OR i.run_id=:run) ORDER BY " + sort,
        statuses=statuses,
        run=run_id,
    )
    return [str(row["id"]) for row in rows]


async def test_incident_list_never_loads_run_payload(monkeypatch, operations_case, task_service, worker_ids):
    case = operations_case
    service = incidents(case)
    forbid_run_payloads(monkeypatch)
    page = await service.list_all(
        case.actor, case.scope, query=IncidentQuery(status=["active"]), position=None, context=AuditContext()
    )
    assert page["items"] == []
    monkeypatch.undo()
    run, incident = await open_incident(case, task_service, worker_ids)
    forbid_run_payloads(monkeypatch)
    for run_id in (None, run.id):
        page = await service.list_all(
            case.actor,
            case.scope,
            run_id=run_id,
            query=IncidentQuery(status=["active"]),
            position=None,
            context=AuditContext(),
        )
        assert [item["id"] for item in page["items"]] == [str(incident["id"])]
    view = await service.read(case.actor, case.scope, incident["id"], context=AuditContext())
    assert isinstance(view, IncidentView) and view.id == incident["id"]


async def test_real_incident_reads_filters_and_resolution(operations_case, task_service, worker_ids, services):
    case = operations_case
    run, incident = await open_incident(case, task_service, worker_ids)
    service = incidents(case)
    view = await service.read(case.actor, case.scope, incident["id"], context=AuditContext())
    assert isinstance(view, IncidentView)
    assert (view.node_id, view.instance_key, view.status, view.revision, view.run_status) == (
        "work",
        "",
        "active",
        incident["revision"],
        "suspended",
    )
    assert view.workflow is not None and (view.workflow.name, view.workflow.version) == ("worker-flow", "1.0.0")
    assert view.opened_at is not None and view.opened_at == view.updated_at
    assert not view.opened_at_estimated and not view.test
    dumped = view.model_dump(mode="json")
    assert "instance_key" not in dumped and "test" not in dumped and "opened_at_estimated" not in dumped
    opened = view.opened_at

    def at(delta=timedelta()):
        return (opened + delta).isoformat()

    matching = [
        {"status": ["active"]},
        {"status": ["active", "closed"]},
        {"code": incident["code"]},
        {"workflow": "worker-flow"},
        {"node_id": "work"},
        {"opened_after": at()},
        {"opened_before": at(timedelta(microseconds=1))},
        {"opened_after": at(), "opened_before": at(timedelta(seconds=1))},
        {"include_test": True},
    ]
    missing = [
        {"status": ["resolved"]},
        {"status": ["closed", "resolved"]},
        {"code": "WV-OTHER"},
        {"workflow": "other-flow"},
        {"node_id": "other"},
        {"opened_after": at(timedelta(microseconds=1))},
        {"opened_before": at()},
    ]
    for values, expected in [(item, [str(incident["id"])]) for item in matching] + [(item, []) for item in missing]:
        for order in ("opened_desc", "opened_asc", "id"):
            assert await walk(case, {**values, "order": order}) == expected, (values, order)
            assert await walk(case, {**values, "order": order}, run_id=run.id) == expected, (values, order)

    resolver = services(case.sessions).resolve(IncidentService)
    request = IncidentResolution(kind="retry_safe", receipt_id=uuid4(), reason="reviewed")

    async def resolve(body, revision):
        async with case.tx() as tx:
            return await resolver.resolve(
                tx, incident["id"], body, revision, actor=case.actor, scope=case.scope, context=AuditContext()
            )

    receipt = await resolve(request, incident["revision"])
    assert await resolve(request, incident["revision"]) == receipt
    with pytest.raises(CatalogError) as conflict:
        await resolve(request.model_copy(update={"receipt_id": uuid4()}), incident["revision"])
    assert (conflict.value.status, conflict.value.code) == (409, "WV-INCIDENT-REVISION")
    with pytest.raises(CatalogError) as stale:
        await resolve(request.model_copy(update={"receipt_id": uuid4()}), receipt.revision)
    assert (stale.value.status, stale.value.code) == (409, "WV-RUNTIME-STATE")
    resolved = await service.read(case.actor, case.scope, incident["id"], context=AuditContext())
    assert isinstance(resolved, IncidentView)
    assert (resolved.status, resolved.revision, resolved.resolution) == ("resolved", receipt.revision, request)
    assert resolved.opened_at == opened and resolved.updated_at is not None and resolved.updated_at > opened
    assert resolved.updated_at == resolved.resolved_at
    assert await walk(case, {"status": ["resolved"]}) == [str(incident["id"])]
    assert await walk(case, {"status": ["active"]}) == []


async def test_statement_count_does_not_depend_on_page_size(operations_case, task_service, worker_ids):
    case = operations_case
    run, incident = await open_incident(case, task_service, worker_ids)
    await seed(case, run.id, 60)
    service = incidents(case)
    counts = {}
    for run_id in (None, run.id):
        for limit in (1, 50):
            statements, stop = capture(case)
            try:
                page = await service.list_all(
                    case.actor,
                    case.scope,
                    run_id=run_id,
                    query=IncidentQuery(limit=limit, include_test=True),
                    position=None,
                    context=AuditContext(),
                )
            finally:
                stop()
            assert len(page["items"]) == limit
            assert not any("from runs " in sql and ("state" in sql or "artifact" in sql) for sql in statements)
            assert not any("select runs.*" in sql or "r.state" in sql or "r.artifact" in sql for sql in statements)
            counts[run_id, limit] = len(statements)
    assert counts[None, 1] == counts[None, 50]
    assert counts[run.id, 1] == counts[run.id, 50]


async def test_token_and_current_principal_both_need_incident_read(
    monkeypatch, operations_case, task_service, worker_ids
):
    from firefly_weave.access.models import Grant, Principal
    from firefly_weave.definitions.service import DefinitionService

    case = operations_case
    run, incident = await open_incident(case, task_service, worker_ids)
    service = incidents(case)
    opened = []
    original = DefinitionService.transaction

    def counting(self, *args, **kwargs):
        opened.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DefinitionService, "transaction", counting)
    calls = (
        lambda actor: service.read(actor, case.scope, incident["id"], context=AuditContext()),
        lambda actor: service.list_all(actor, case.scope, query=IncidentQuery(), position=None, context=AuditContext()),
        lambda actor: service.list_all(
            actor, case.scope, run_id=run.id, query=IncidentQuery(), position=None, context=AuditContext()
        ),
    )
    viewer = Principal(id=case.actor.id, kind=case.actor.kind, grants=(Grant(role="viewer", scope=case.scope),))
    for call in calls:
        with pytest.raises(AccessDenied):
            await call(viewer)
    # A token without incident.read is refused before any read transaction opens.
    assert opened == []
    async with case.owner.begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='operator'"), {"id": case.actor.id}
        )
    for call in calls:
        with pytest.raises(AccessDenied):
            await call(case.actor)
    # A token whose grant was removed meanwhile is refused inside the read transaction, by the stored principal.
    assert len(opened) == len(calls)


async def test_other_scopes_never_find_the_incident_or_its_run(operations_case, provisioned, task_service, worker_ids):
    from firefly_weave.access.models import Grant

    case = operations_case
    run, incident = await open_incident(case, task_service, worker_ids)
    other = provisioned[1][1]
    await case.access.grant(case.admin, case.actor.id, Grant(role="operator", scope=other))
    actor = await case.access.load_principal(case.actor.id)
    service = incidents(case)
    with pytest.raises(CatalogError) as missing:
        await service.read(actor, other, incident["id"], context=AuditContext())
    assert (missing.value.status, missing.value.code) == (404, "WV-NOT-FOUND")
    with pytest.raises(CatalogError) as absent:
        await service.list_all(
            actor, other, run_id=run.id, query=IncidentQuery(), position=None, context=AuditContext()
        )
    assert absent.value.status == 404
    page = await service.list_all(actor, other, query=IncidentQuery(), position=None, context=AuditContext())
    assert page == {"items": [], "next_cursor": None}
    with pytest.raises(CatalogError) as unknown:
        await service.read(actor, case.scope, uuid4(), context=AuditContext())
    assert unknown.value.status == 404


async def test_test_runs_are_left_out_by_default_and_archived_runs_keep_their_incidents(
    operations_case, task_service, worker_ids
):
    case = operations_case
    run, incident = await open_incident(case, task_service, worker_ids)
    service = incidents(case)
    async with case.owner.begin() as session:
        await session.execute(text("UPDATE run_facts SET test=true WHERE run_id=:run"), {"run": run.id})
        await session.execute(text("UPDATE incident_facts SET test=true WHERE incident_id=:id"), {"id": incident["id"]})
    assert await walk(case, {}) == []
    assert await walk(case, {}, run_id=run.id) == []
    assert await walk(case, {"include_test": True}) == [str(incident["id"])]
    view = await service.read(case.actor, case.scope, incident["id"], context=AuditContext())
    assert isinstance(view, IncidentView) and view.test and view.model_dump(mode="json")["test"] is True
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE incident_facts SET test=false WHERE incident_id=:id"), {"id": incident["id"]}
        )
        await session.execute(
            text(
                "INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id,archived,revision,archived_at) "
                "SELECT tenant_id,project_id,environment_id,id,true,1,clock_timestamp() FROM runs WHERE id=:run"
            ),
            {"run": run.id},
        )
    # Archiving a run never hid its incidents and still does not.
    assert await walk(case, {}) == [str(incident["id"])]
    assert await walk(case, {"order": "id"}, run_id=run.id) == [str(incident["id"])]
    assert isinstance(await service.read(case.actor, case.scope, incident["id"], context=AuditContext()), IncidentView)


@pytest.mark.parametrize("order", ["opened_desc", "opened_asc", "id"])
async def test_pages_follow_sql_order_with_ties_and_unknown_times_last(operations_case, order):
    case = operations_case
    first, second = await case.start("first"), await case.start("second")
    await seed(case, first.id, 120, prefix="a")
    await seed(case, second.id, 37, prefix="b")
    everything = await expected_order(case, order)
    assert len(everything) == 157
    if order != "id":
        unknown = {
            str(row["id"])
            for row in await case.rows("SELECT incident_id AS id FROM incident_facts WHERE opened_at IS NULL")
        }
        assert set(everything[-len(unknown) :]) == unknown
    for limit in (1, 7, 50, 100):
        values = {"order": order, "limit": limit}
        assert await walk(case, values) == everything
        assert await walk(case, values, run_id=second.id) == await expected_order(case, order, run_id=second.id)
        statuses = ["active", "resolved"]
        assert await walk(case, {**values, "status": statuses}) == await expected_order(case, order, statuses=statuses)


async def test_newer_incidents_do_not_displace_a_continuation(operations_case):
    case = operations_case
    run = await case.start()
    await seed(case, run.id, 20, prefix="a")
    service = incidents(case)
    query = IncidentQuery(limit=5)
    first = await service.list_all(case.actor, case.scope, query=query, position=None, context=AuditContext())
    expected = await expected_order(case, "opened_desc")
    await seed(case, run.id, 9, prefix="later")
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "UPDATE incident_facts SET opened_at=timestamptz '2026-10-09T00:00:00Z' WHERE incident_id IN "
                "(SELECT id FROM incidents WHERE incident_key LIKE 'later:%')"
            )
        )
    position = position_of(case.scope, query, None, first["next_cursor"])
    rest = []
    while position is not None:
        page = await service.list_all(case.actor, case.scope, query=query, position=position, context=AuditContext())
        rest += [item["id"] for item in page["items"]]
        position = position_of(case.scope, query, None, page["next_cursor"]) if page["next_cursor"] else None
    assert [item["id"] for item in first["items"]] + rest == expected


async def test_classification_comes_from_run_facts_and_policy_blocks(
    monkeypatch, operations_case, task_service, worker_ids
):
    from firefly_weave.contracts import language_features

    case = operations_case
    run, incident = await open_incident(case, task_service, worker_ids)
    service = incidents(case)
    forbid_run_payloads(monkeypatch)

    async def answers():
        page = await service.list_all(
            case.actor, case.scope, query=IncidentQuery(), position=None, context=AuditContext()
        )
        direct = await service.read(case.actor, case.scope, incident["id"], context=AuditContext())
        return page["items"], direct

    withheld = ([{"id": str(incident["id"]), "unavailable": True, "omissions": UNAVAILABLE}], UnavailableResource)
    # The run pins a language feature, as a workflow that builds text with concat does.
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "UPDATE run_facts SET pinned_ir_version='weave/ir-v1alpha4',"
                "pinned_features='[\"text.concat\"]'::jsonb WHERE run_id=:run"
            ),
            {"run": run.id},
        )
    items, direct = await answers()
    assert isinstance(direct, IncidentView) and items[0]["id"] == str(incident["id"]) and "code" in items[0]
    # A server that no longer runs the feature withholds the incident at once, from metadata alone.
    monkeypatch.setattr(language_features, "ADVERTISED_FEATURES", ())
    items, direct = await answers()
    assert (items, type(direct)) == withheld
    monkeypatch.undo()
    forbid_run_payloads(monkeypatch)
    items, direct = await answers()
    assert isinstance(direct, IncidentView) and items[0]["id"] == str(incident["id"]) and "code" in items[0]
    for change in (
        "UPDATE run_facts SET classification_state='unsupported' WHERE run_id=:run",
        "UPDATE run_facts SET classification_state='available',classification_policy='retired' WHERE run_id=:run",
    ):
        async with case.owner.begin() as session:
            snapshot = (
                await session.execute(
                    text("SELECT classification_state,classification_policy FROM run_facts WHERE run_id=:run"),
                    {"run": run.id},
                )
            ).one()
            await session.execute(text(change), {"run": run.id})
        items, direct = await answers()
        assert (items, type(direct)) == withheld, change
        async with case.owner.begin() as session:
            await session.execute(
                text(
                    "UPDATE run_facts SET classification_state=:state,classification_policy=:policy WHERE run_id=:run"
                ),
                {"state": snapshot[0], "policy": snapshot[1], "run": run.id},
            )
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO run_policy_blocks(tenant_id,project_id,environment_id,run_id,reason_code) "
                "SELECT tenant_id,project_id,environment_id,id,'WV-LEGACY-UNAVAILABLE' FROM runs WHERE id=:run"
            ),
            {"run": run.id},
        )
    items, direct = await answers()
    assert (items, type(direct)) == withheld


@pytest.mark.parametrize(
    "change",
    [
        # A malformed historical identity cannot be normalized into a static step.
        "UPDATE incident_facts SET node_id=NULL WHERE incident_id=:id",
        # Facts that name another step or instance disagree with the incident's own step.
        "UPDATE incident_facts SET node_id='other' WHERE incident_id=:id",
        "UPDATE incident_facts SET instance_key='work[2]' WHERE incident_id=:id",
        # A missing projection does not mean the incident never existed.
        "DELETE FROM incident_facts WHERE incident_id=:id",
    ],
)
async def test_incidents_without_trustworthy_facts_stay_listed_as_unavailable(
    operations_case, task_service, worker_ids, change
):
    case = operations_case
    run, incident = await open_incident(case, task_service, worker_ids)
    async with case.owner.begin() as session:
        await session.execute(text(change), {"id": incident["id"]})
    service = incidents(case)
    for run_id in (None, run.id):
        page = await service.list_all(
            case.actor, case.scope, run_id=run_id, query=IncidentQuery(), position=None, context=AuditContext()
        )
        assert page["items"] == [{"id": str(incident["id"]), "unavailable": True, "omissions": UNAVAILABLE}]
    direct = await service.read(case.actor, case.scope, incident["id"], context=AuditContext())
    assert isinstance(direct, UnavailableResource) and direct.id == incident["id"]


async def test_instance_keys_and_run_level_incidents_keep_their_identity(operations_case):
    case = operations_case
    run = await case.start()
    async with case.owner.begin() as session:
        for key, source, node, instance in (
            ("loop", "work[2]", "work", "work[2]"),
            ("agent", "work[1]#3.review", "work", "work[1]#3.review"),
            ("@legacy", None, None, ""),
        ):
            await session.execute(
                text(
                    "WITH incident AS (INSERT INTO incidents(id,tenant_id,project_id,environment_id,run_id,"
                    "incident_key,node_id,generation,origin_code,code,status,revision) SELECT md5(:key)::uuid,"
                    "tenant_id,project_id,environment_id,id,:key,:source,NULL,'WV-TASK-FAILED','WV-TASK-FAILED',"
                    "'active',1 FROM runs WHERE id=:run RETURNING *) INSERT INTO incident_facts(tenant_id,"
                    "project_id,environment_id,incident_id,node_id,instance_key,opened_at,updated_at,workflow_name,"
                    "workflow_version,test,opened_at_estimated) SELECT i.tenant_id,i.project_id,i.environment_id,"
                    "i.id,:node,:instance,clock_timestamp(),clock_timestamp(),f.workflow_name,f.workflow_version,"
                    "false,true FROM incident i JOIN run_facts f ON f.run_id=i.run_id"
                ),
                {"key": key, "source": source, "node": node, "instance": instance, "run": run.id},
            )
    service = incidents(case)
    page = await service.list_all(case.actor, case.scope, query=IncidentQuery(), position=None, context=AuditContext())
    found = {item["incident_key"]: item for item in page["items"]}
    assert (found["loop"]["node_id"], found["loop"]["instance_key"]) == ("work", "work[2]")
    assert (found["agent"]["node_id"], found["agent"]["instance_key"]) == ("work", "work[1]#3.review")
    assert found["@legacy"]["node_id"] is None and "instance_key" not in found["@legacy"]
    assert all(item["opened_at_estimated"] is True for item in found.values())
    by_step = await service.list_all(
        case.actor, case.scope, query=IncidentQuery(node_id="work"), position=None, context=AuditContext()
    )
    assert {item["incident_key"] for item in by_step["items"]} == {"loop", "agent"}
    async with case.owner.begin() as session:
        await session.execute(text("UPDATE incident_facts SET opened_at=NULL WHERE incident_id=md5('loop')::uuid"))
    unknown = await service.read(case.actor, case.scope, UUID(found["loop"]["id"]), context=AuditContext())
    # An unknown opening time is never labeled estimated.
    assert isinstance(unknown, IncidentView) and unknown.opened_at is None and not unknown.opened_at_estimated


async def start_branch_failure(case):
    """A run whose parallel branch output reads a missing input field: the incident opens on a control step."""
    import json

    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest

    branch = {
        "steps": [{"id": "copy", "kind": "transform", "value": {"literal": 3}}],
        "output": {"ref": "/input/missing"},
    }
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "branch-flow", "version": "1.0.0"},
        "spec": {
            "inputSchema": {},
            "outputSchema": {},
            "steps": [{"id": "fork", "kind": "parallel", "concurrency": 1, "branches": {"one": branch}}],
            "output": {"literal": None},
        },
    }
    definitions = case.runtime.definitions
    published = await definitions.publish(
        case.actor, case.scope, "Workflow", json.dumps(document), "json", str(uuid4()), context=AuditContext()
    )
    activation = await definitions.activate(
        case.actor,
        case.scope,
        ActivationRequest(scope=case.scope, version_id=published.id, artifact_digest=published.digest),
        str(uuid4()),
        context=AuditContext(),
    )
    return await case.runtime.start(
        case.actor,
        case.scope,
        StartRunRequest(activation_id=activation.id, input={}),
        str(uuid4()),
        context=AuditContext(),
    )


async def test_control_step_incidents_are_listed_read_and_resolved(
    operations_case, authenticated_client, access_db, provisioned
):
    from firefly_weave.access.models import Grant

    case = operations_case
    run = await start_branch_failure(case)
    assert run.state.status == "suspended"
    [incident] = await case.rows("SELECT * FROM incidents WHERE run_id=:run", run=run.id)
    assert (incident["incident_key"], incident["node_id"], incident["code"]) == (
        "@branch:fork:0:0",
        "@branch:fork:0",
        "WV-EXPR-MISSING",
    )
    # The projection never attributes a control step to an author step.
    assert await case.rows(
        "SELECT node_id,instance_key FROM incident_facts WHERE incident_id=:id", id=incident["id"]
    ) == [{"node_id": None, "instance_key": ""}]
    service = incidents(case)
    for run_id in (None, run.id):
        page = await service.list_all(
            case.actor, case.scope, run_id=run_id, query=IncidentQuery(), position=None, context=AuditContext()
        )
        [item] = page["items"]
        assert "unavailable" not in item, item
        assert (item["id"], item["node_id"], item["code"], item["revision"], item["run_status"]) == (
            str(incident["id"]),
            "@branch:fork:0",
            "WV-EXPR-MISSING",
            incident["revision"],
            "suspended",
        )
        assert "instance_key" not in item and item["workflow"] == {"name": "branch-flow", "version": "1.0.0"}
    # The step filter takes static author step IDs, which a control-step incident does not have.
    assert await walk(case, {"node_id": "fork"}) == []

    client, tokens, scopes = authenticated_client
    async with access_db[1]() as session:
        subjects = {
            row.subject: row.principal_id
            for row in (await session.execute(text("SELECT subject,principal_id FROM identity_links"))).all()
        }
    await access_db[2].grant(provisioned[0], subjects["0"], Grant(role="operator", scope=scopes[0]))
    headers = {"Authorization": "Bearer " + tokens[0]}
    path = (
        f"/api/v1/tenants/{case.scope.tenant_id}/projects/{case.scope.project_id}"
        f"/environments/{case.scope.environment_id}/incidents/{incident['id']}"
    )
    read = await client.get(path, headers=headers)
    assert read.status_code == 200, read.text
    assert read.headers["ETag"] == f'"{incident["revision"]}"'
    assert (read.json()["node_id"], read.json()["code"]) == ("@branch:fork:0", "WV-EXPR-MISSING")
    assert "instance_key" not in read.json()

    resolution = IncidentResolution(kind="terminate", receipt_id=uuid4(), reason="branch output cannot be mapped")
    resolved = await client.post(
        path + "/resolve",
        content=resolution.model_dump_json(exclude_unset=True),
        headers={**headers, "If-Match": read.headers["ETag"], "Content-Type": "application/json"},
    )
    assert resolved.status_code == 200, resolved.text
    revision = resolved.json()["revision"]
    assert resolved.json()["status"] == "resolved" and revision > incident["revision"]
    after = await client.get(path, headers=headers)
    assert after.status_code == 200 and after.headers["ETag"] == f'"{revision}"'
    assert (after.json()["node_id"], after.json()["status"], after.json()["revision"]) == (
        "@branch:fork:0",
        "resolved",
        revision,
    )
    assert after.json()["resolution"]["kind"] == "terminate" and after.json()["external_effects_may_continue"]


@pytest.mark.parametrize("order", ["id", "opened_desc"])
async def test_incident_pages_keep_the_logical_byte_ceiling(operations_case, order):
    from firefly_weave.persistence.paging import PAGE_BYTES

    case = operations_case
    run = await case.start()
    await seed(case, run.id, 2)
    page = await expected_order(case, order)

    async def pad(characters):
        # Only the last item of the page grows, so a one-item page never charges it.
        async with case.owner.begin() as session:
            await session.execute(
                text(
                    "UPDATE incidents SET resolution=jsonb_build_object('receipt_id',cast(:receipt AS text),"
                    "'kind','accept_reconciled_result','reason','padding','output',repeat('x',:count),"
                    "'evidence_reference','padding') WHERE id=:id"
                ),
                {"receipt": str(uuid4()), "count": characters, "id": UUID(page[-1])},
            )
        measured = await case.rows(
            "SELECT octet_length(to_jsonb(i)::text) AS bytes FROM incidents i WHERE id=ANY(:ids)",
            ids=[UUID(identifier) for identifier in page],
        )
        return sum(row["bytes"] for row in measured)

    async def listed(limit):
        query = IncidentQuery(order=order, limit=limit)
        return await incidents(case).list_all(
            case.actor, case.scope, query=query, position=None, context=AuditContext()
        )

    baseline = await pad(0)
    assert await pad(PAGE_BYTES - baseline) == PAGE_BYTES
    full = await listed(2)
    assert [item["id"] for item in full["items"]] == page
    assert len(full["items"][-1]["resolution"]["output"]) == PAGE_BYTES - baseline
    assert await pad(PAGE_BYTES - baseline + 1) == PAGE_BYTES + 1
    with pytest.raises(CatalogError) as rejected:
        await listed(2)
    assert (rejected.value.status, rejected.value.code) == (429, "WV-PAGE-LIMIT")
    assert [item["id"] for item in (await listed(1))["items"]] == page[:1]


async def test_keyword_arguments_keep_id_order_and_raw_cursors(operations_case):
    case = operations_case
    run = await case.start()
    await seed(case, run.id, 3)
    service = incidents(case)
    ordered = await expected_order(case, "id")
    first = await service.list_all(case.actor, case.scope, limit=2, context=AuditContext())
    assert [item["id"] for item in first["items"]] == ordered[:2] and first["next_cursor"] == ordered[1]
    rest = await service.list_all(
        case.actor, case.scope, limit=2, cursor=UUID(first["next_cursor"]), context=AuditContext()
    )
    assert [item["id"] for item in rest["items"]] == ordered[2:] and rest["next_cursor"] is None
    by_run = await service.list_all(case.actor, case.scope, run_id=run.id, limit=3, context=AuditContext())
    assert [item["id"] for item in by_run["items"]] == ordered
    with pytest.raises(ValueError):
        await service.list_all(
            case.actor, case.scope, cursor=UUID(ordered[0]), query=IncidentQuery(order="id"), context=AuditContext()
        )
    with pytest.raises(ValueError):
        await service.list_all(
            case.actor, case.scope, query=IncidentQuery(), position=UUID(ordered[0]), context=AuditContext()
        )
    with pytest.raises(CatalogError) as unbounded:
        await service.list_all(case.actor, case.scope, limit=101, context=AuditContext())
    assert (unbounded.value.status, unbounded.value.code) == (422, "WV-PAGE")


async def test_routes_sdk_etags_and_scope_bound_cursors(
    operations_case, authenticated_client, access_db, provisioned, task_service, worker_ids
):
    from firefly_weave.access.models import Grant
    from firefly_weave.api.transport import encode_cursor, encode_cursor_v2
    from firefly_weave.sdk.client import WeaveClient

    case = operations_case
    run, incident = await open_incident(case, task_service, worker_ids)
    other_run = await case.start("other")
    await seed(case, other_run.id, 3)
    client, tokens, scopes = authenticated_client
    async with access_db[1]() as session:
        subjects = {
            row.subject: row.principal_id
            for row in (await session.execute(text("SELECT subject,principal_id FROM identity_links"))).all()
        }
    await access_db[2].grant(provisioned[0], subjects["0"], Grant(role="operator", scope=scopes[0]))
    await access_db[2].grant(provisioned[0], subjects["1"], Grant(role="operator", scope=scopes[1]))
    headers = {"Authorization": "Bearer " + tokens[0]}
    legacy = (
        f"/tenants/{case.scope.tenant_id}/projects/{case.scope.project_id}/environments/{case.scope.environment_id}"
    )
    prefix = "/api/v1" + legacy
    other = (
        f"/api/v1/tenants/{scopes[1].tenant_id}/projects/{scopes[1].project_id}/environments/{scopes[1].environment_id}"
    )
    identifier = str(incident["id"])

    listed = await client.get(prefix + "/incidents?status=active&status=closed&limit=1", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["id"] == identifier
    read = await client.get(prefix + "/incidents/" + identifier, headers=headers)
    assert read.status_code == 200 and read.headers["ETag"] == f'"{incident["revision"]}"'
    assert read.json()["workflow"] == {"name": "worker-flow", "version": "1.0.0"}
    assert read.json()["run_status"] == "suspended" and read.json()["node_id"] == "work"
    assert (await client.get(legacy + "/incidents/" + identifier, headers=headers)).headers["ETag"] == str(
        incident["revision"]
    )
    run_page = await client.get(prefix + f"/runs/{run.id}/incidents", headers=headers)
    assert [item["id"] for item in run_page.json()["items"]] == [identifier]
    # The earlier route keeps its plain array.
    array = await client.get(legacy + f"/runs/{run.id}/incidents", headers=headers)
    assert isinstance(array.json(), list) and array.json()[0]["id"] == identifier

    cursor = (await client.get(prefix + "/incidents?limit=1", headers=headers)).json()["next_cursor"]
    assert cursor is not None
    for path, params in (
        ("/incidents", {"cursor": cursor, "status": "active"}),
        ("/incidents", {"cursor": cursor, "order": "opened_asc"}),
        (f"/runs/{run.id}/incidents", {"cursor": cursor}),
        ("/incidents", {"colour": "red"}),
        ("/incidents", {"opened_after": "2026-10-07T12:00:00Z", "opened_before": "2026-10-07T12:00:00Z"}),
    ):
        response = await client.get(prefix + path, params=params, headers=headers)
        assert response.status_code == 422, (path, params, response.text)
    run_cursor = (await client.get(prefix + f"/runs/{other_run.id}/incidents?limit=1", headers=headers)).json()[
        "next_cursor"
    ]
    assert (
        await client.get(prefix + f"/runs/{run.id}/incidents", params={"cursor": run_cursor}, headers=headers)
    ).status_code == 422
    stolen = encode_cursor_v2(scopes[1], IncidentQuery().cursor_collection(), None, identifier)
    assert (await client.get(prefix + "/incidents", params={"cursor": stolen}, headers=headers)).status_code == 422

    ordered = await expected_order(case, "id")
    old = encode_cursor(case.scope, "incidents", UUID(ordered[0]))
    continued = await client.get(prefix + "/incidents", params={"order": "id", "cursor": old}, headers=headers)
    assert [item["id"] for item in continued.json()["items"]] == ordered[1:]
    raw = await client.get(legacy + "/incidents", params={"order": "id", "limit": 1}, headers=headers)
    assert raw.json()["next_cursor"] == ordered[0]

    other_headers = {"Authorization": "Bearer " + tokens[1]}
    assert (await client.get(other + "/incidents/" + identifier, headers=other_headers)).status_code == 404
    assert (await client.get(other + f"/runs/{run.id}/incidents", headers=other_headers)).status_code == 404

    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO run_policy_blocks(tenant_id,project_id,environment_id,run_id,reason_code) "
                "SELECT tenant_id,project_id,environment_id,id,'WV-LEGACY-UNAVAILABLE' FROM runs WHERE id=:run"
            ),
            {"run": run.id},
        )
    blocked = await client.get(prefix + "/incidents/" + identifier, headers=headers)
    assert blocked.status_code == 200 and "ETag" not in blocked.headers
    assert blocked.json() == {"id": identifier, "unavailable": True, "omissions": UNAVAILABLE}

    async with WeaveClient(
        "https://platform.example", lambda: tokens[0], case.scope, transport=client._transport
    ) as sdk:
        # Repeated statuses reach the server as repeated parameters, not only the last value.
        found = await sdk.list_incidents(query=IncidentQuery(status=["active", "closed"], include_test=True))
        assert {item.id for item in found.items} >= {UUID(identifier)}
        hidden = await sdk.list_incidents(query=IncidentQuery(status=["closed", "resolved"]))
        assert UUID(identifier) not in {item.id for item in hidden.items}
        direct = await sdk.read_incident(UUID(identifier))
        assert isinstance(direct, UnavailableResource)
        expected = await expected_order(case, "id", run_id=other_run.id)
        seeded = await sdk.run_incidents(other_run.id, query=IncidentQuery(order="id", limit=2))
        assert [str(item.id) for item in seeded.items] == expected[:2] and seeded.next_cursor is not None
        with pytest.raises(ValueError):
            await sdk.list_incidents(limit=10, query=IncidentQuery())
