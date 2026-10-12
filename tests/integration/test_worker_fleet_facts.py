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

"""Real PostgreSQL worker fleet: one grouped query, authorized placement, bounded retirement and release usage."""

import asyncio
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.deployments import (
    ComponentSpec,
    Deployment,
    DeploymentObservation,
    DeploymentTarget,
    ObservedMember,
    ObservedResource,
)
from firefly_weave.contracts.worker_queries import (
    ReleaseUsageQuery,
    RetireStaleRequest,
    WorkerListQuery,
)
from firefly_weave.contracts.workers import WorkerControlRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.workers.repository import WorkerRepository

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "c" * 64


async def clock(case) -> datetime:
    async with case.owner() as session:
        value: datetime = await session.scalar(text("SELECT clock_timestamp()"))
        return value


def freeze(monkeypatch, at: datetime) -> None:
    async def fixed(self):
        return at

    monkeypatch.setattr(WorkerRepository, "now", fixed)


def identity(host: str, runtime: str = "process") -> dict:
    return {"host": host, "runtime": runtime, "platform_hint": "none"}


async def add_workers(
    case,
    count: int = 1,
    *,
    seen=None,
    registered=None,
    host=None,
    runtime="process",
    revoked=False,
    draining=False,
    task_types=("echo@1.2.0",),
    facts=True,
    release=None,
) -> list[UUID]:
    """Workers inserted as stored records; `seen` and `registered` may be one time or one per worker."""
    template = case.instances[0]
    release = release or template.release_id
    created = []
    async with case.owner.begin() as session:
        for index in range(count):
            identifier = uuid4()
            payload = template.model_copy(
                update={"id": identifier, "release_id": release, "task_types": list(task_types)}
            ).model_dump_json()
            last_seen = seen[index] if isinstance(seen, list) else seen
            at = registered[index] if isinstance(registered, list) else registered
            await session.execute(
                text(
                    "INSERT INTO worker_instances(id,tenant_id,project_id,environment_id,release_id,principal_id,"
                    "payload,revoked,last_seen_at,draining) VALUES(:id,:tenant,:project,:environment,:release,"
                    ":principal,cast(:payload AS jsonb),:revoked,:seen,:draining)"
                ),
                {
                    "id": identifier,
                    "tenant": case.scope.tenant_id,
                    "project": case.scope.project_id,
                    "environment": case.scope.environment_id,
                    "release": release,
                    "principal": template.principal_id,
                    "payload": payload,
                    "revoked": revoked,
                    "seen": last_seen,
                    "draining": draining,
                },
            )
            if facts:
                reported = {} if host is None else identity(host if count == 1 else f"{host}{index:03d}", runtime)
                await session.execute(
                    text(
                        "INSERT INTO worker_facts(tenant_id,project_id,environment_id,worker_id,registered_at,"
                        "identity) VALUES(:tenant,:project,:environment,:id,:registered,cast(:identity AS jsonb))"
                    ),
                    {
                        "tenant": case.scope.tenant_id,
                        "project": case.scope.project_id,
                        "environment": case.scope.environment_id,
                        "id": identifier,
                        "registered": at,
                        "identity": json.dumps(reported),
                    },
                )
            created.append(identifier)
    return created


async def listed(case, actor=None, position=None, **values) -> dict:
    return await case.workers.list(
        actor or case.actor, case.scope, query=WorkerListQuery(**values), position=position, context=AuditContext()
    )


async def walk(case, actor=None, **values) -> list[UUID]:
    from firefly_weave.api.transport import decode_cursor_v2
    from firefly_weave.operations.fact_positions import run_position

    query = WorkerListQuery(**values)
    position, found = None, []
    while True:
        page = await case.workers.list(
            actor or case.actor, case.scope, query=query, position=position, context=AuditContext()
        )
        assert len(page["items"]) <= query.limit
        found += [UUID(item["id"]) for item in page["items"]]
        if page["next_cursor"] is None:
            return found
        position = run_position(decode_cursor_v2(page["next_cursor"], case.scope, query.cursor_collection()))


def ids(page) -> list[UUID]:
    return [UUID(item["id"]) for item in page["items"]]


def capture(case):
    statements: list[str] = []
    engine = case.sessions.kw["bind"].sync_engine

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", record)
    return statements, lambda: event.remove(engine, "before_cursor_execute", record)


async def owner_insert(case, table: str, identifier: UUID, target: UUID, payload, *, revision: int = 1) -> None:
    async with case.owner.begin() as session:
        await session.execute(
            text(
                f"INSERT INTO {table}(id,tenant_id,project_id,environment_id,target_id,revision,payload) "
                "VALUES(:id,:tenant,:project,:environment,:target,:revision,cast(:payload AS jsonb))"
            ),
            {
                "id": identifier,
                "tenant": case.scope.tenant_id,
                "project": case.scope.project_id,
                "environment": case.scope.environment_id,
                "target": target,
                "revision": revision,
                "payload": payload.model_dump_json(),
            },
        )


async def add_target(case, name: str, *, revision: int = 1) -> UUID:
    identifier = uuid4()
    target = DeploymentTarget(
        name=name,
        adapter="docker-compose",
        external_identity="daemon-" + name,
        boundary="weave-" + name,
        runner_principal_id=uuid4(),
        capabilities=["observe"],
        id=identifier,
        scope=case.scope,
        revision=revision,
        created_at=await clock(case),
    )
    await owner_insert(case, "deployment_targets", identifier, identifier, target, revision=revision)
    return identifier


async def add_deployment(case, target: UUID, name: str = "weave", components=None) -> UUID:
    release = case.instances[0].release_id
    identifier = uuid4()
    specs = [
        ComponentSpec(
            name=component,
            kind=kind,
            image=f"registry.example/{component}@{digest}",
            configuration="current",
            replicas=2 if kind == "worker" else 1,
            worker_release_id=(component_release or release) if kind == "worker" else None,
        )
        for component, kind, digest, component_release in (components or [("engine", "worker", DIGEST, None)])
    ]
    deployment = Deployment(
        target_id=target,
        name=name,
        ownership="managed",
        components=specs,
        id=identifier,
        scope=case.scope,
        revision=1,
        created_at=await clock(case),
    )
    await owner_insert(case, "deployment_deployments", identifier, target, deployment)
    return identifier


async def add_observation(
    case, target: UUID, members: dict, *, complete=True, at=None, expires=None, revision=1, kind="worker"
) -> UUID:
    at = at or await clock(case)
    identifier = uuid4()
    observation = DeploymentObservation(
        resources=[
            ObservedResource(
                name=component,
                external_identity=f"compose/weave/{component}",
                kind=kind,
                replicas=len(hosts),
                ready_replicas=len(hosts),
                version="v1",
                ownership="managed",
                state="ready",
                members=[
                    ObservedMember(
                        host=host,
                        ordinal=index + 1,
                        state="running",
                        ready=True,
                        started_at=None,
                        restarts=0,
                        node=None,
                    )
                    for index, host in enumerate(hosts)
                ],
            )
            for component, hosts in members.items()
        ],
        complete=complete,
        settled=complete,
        id=identifier,
        target_id=target,
        target_revision=revision,
        observed_at=at,
        expires_at=expires or at + timedelta(minutes=15),
        digest="0" * 64,
    )
    await owner_insert(case, "deployment_observations", identifier, target, observation)
    return identifier


async def principal_with(case, *grants: Grant) -> Principal:
    identifier = await case.access.create_principal(case.admin, "application")
    for grant in grants:
        await case.access.grant(case.admin, identifier, grant)
    return await case.access.load_principal(identifier)


async def owner_rows(case, sql: str, **params) -> list[dict]:
    async with case.owner() as session:
        return [dict(row) for row in (await session.execute(text(sql), params)).mappings()]


def placement(target: UUID, name: str, deployment: UUID, component: str = "engine") -> dict:
    return {"target_id": str(target), "target_name": name, "deployment_id": str(deployment), "component": component}


def by_id(page) -> dict[UUID, dict]:
    return {UUID(item["id"]): item for item in page["items"]}


async def test_fleet_reads_no_per_worker_status(monkeypatch, operations_case):
    async def reject(*args, **kwargs):
        raise AssertionError("per-worker query")

    monkeypatch.setattr(WorkerRepository, "status", reject)
    page = await listed(operations_case, state="all")
    assert len(page["items"]) == 2


async def test_one_fleet_statement_whatever_the_page_size_filter_or_position(operations_case):
    case = operations_case
    now = await clock(case)
    await add_workers(case, 120, seen=[now - timedelta(seconds=index) for index in range(120)], host="bulk-")
    counts = {}
    for limit in (1, 50, 100):
        statements, stop = capture(case)
        try:
            page = await listed(case, limit=limit)
        finally:
            stop()
        assert len(page["items"]) == limit and page["next_cursor"] is not None
        fleet = [statement for statement in statements if "worker_instances" in statement]
        assert len(fleet) == 1 and "task_leases" in fleet[0] and "group by owner" in fleet[0]
        assert not any("from runs" in statement for statement in statements)
        counts[limit] = len(statements)
    assert counts[1] == counts[50] == counts[100]
    from firefly_weave.api.transport import decode_cursor_v2
    from firefly_weave.operations.fact_positions import run_position

    query = WorkerListQuery(host_prefix="bulk-", presence="recent", draining=False, order="registered_desc", limit=7)
    first = await case.workers.list(case.actor, case.scope, query=query, position=None, context=AuditContext())
    position = run_position(decode_cursor_v2(first["next_cursor"], case.scope, query.cursor_collection()))
    statements, stop = capture(case)
    try:
        await case.workers.list(case.actor, case.scope, query=query, position=position, context=AuditContext())
    finally:
        stop()
    assert sum("worker_instances" in statement for statement in statements) == 1
    assert len(statements) == counts[1]


async def test_presence_and_current_boundaries_are_exact(monkeypatch, operations_case):
    case = operations_case
    now = await clock(case)
    edge = timedelta(microseconds=1)
    recent = (await add_workers(case, seen=now - timedelta(seconds=60) + edge, registered=now, host="edge-recent"))[0]
    sixty = (await add_workers(case, seen=now - timedelta(seconds=60), registered=now, host="edge-sixty"))[0]
    inside = (await add_workers(case, seen=now - timedelta(hours=1) + edge, host="edge-inside"))[0]
    hour = (await add_workers(case, seen=now - timedelta(hours=1), host="edge-hour"))[0]
    new = (await add_workers(case, registered=now - timedelta(hours=1) + edge, host="edge-new"))[0]
    old = (await add_workers(case, registered=now - timedelta(hours=1), host="edge-old"))[0]
    revoked = (await add_workers(case, seen=now, registered=now, host="edge-revoked", revoked=True))[0]
    legacy = (await add_workers(case, facts=False))[0]
    freeze(monkeypatch, now)
    current = by_id(await listed(case, host_prefix="edge-"))
    assert set(current) == {recent, sixty, inside, new}
    assert current[recent]["presence"] == "recent" and current[recent]["available_capacity"] == 1
    assert current[sixty]["presence"] == "stale" and current[sixty]["available_capacity"] is None
    assert current[new]["presence"] == "unknown" and current[new]["registered_at"] is not None
    assert current[recent]["observed_at"] == now.isoformat().replace("+00:00", "Z")
    everything = by_id(await listed(case, state="all"))
    assert {hour, old, revoked, legacy} <= set(everything)
    assert legacy not in by_id(await listed(case))
    assert not {"identity", "registered_at", "placement", "drained_at"} & set(everything[legacy])
    assert everything[revoked]["revoked"] and everything[revoked]["available_capacity"] == 0
    for presence, expected in (("recent", {recent}), ("stale", {sixty, inside}), ("unknown", {new})):
        assert set(ids(await listed(case, host_prefix="edge-", presence=presence))) == expected


async def test_orders_walk_every_worker_once_with_ties_and_unknown_times(operations_case):
    case = operations_case
    now = await clock(case)
    times = [now - timedelta(minutes=index % 4) if index % 5 else None for index in range(23)]
    registered = [now - timedelta(minutes=index % 3) if index % 7 else None for index in range(23)]
    created = await add_workers(case, 23, seen=times, registered=registered, host="order-")
    rows = {identifier: (times[index], registered[index]) for index, identifier in enumerate(created)}

    def oracle(position: int) -> list[UUID]:
        known = sorted(
            (key for key, value in rows.items() if value[position]), key=lambda key: (rows[key][position], key)
        )
        unknown = sorted(key for key, value in rows.items() if not value[position])
        return [*reversed(known), *reversed(unknown)]

    for order, position in (("last_seen_desc", 0), ("registered_desc", 1)):
        expected = oracle(position)
        for limit in (1, 2, 7, 23, 100):
            assert await walk(case, host_prefix="order-", state="all", order=order, limit=limit) == expected


async def test_a_cursor_belongs_to_its_filters_and_order(operations_case):
    from firefly_weave.api.transport import decode_cursor_v2

    case = operations_case
    await add_workers(case, 3, seen=await clock(case), host="bound-")
    query = WorkerListQuery(host_prefix="bound-", limit=1)
    page = await case.workers.list(case.actor, case.scope, query=query, position=None, context=AuditContext())
    for other in (
        WorkerListQuery(host_prefix="bound-", limit=1, order="registered_desc"),
        WorkerListQuery(host_prefix="bound", limit=1),
        WorkerListQuery(host_prefix="bound-", limit=1, state="all"),
    ):
        with pytest.raises(ValueError):
            decode_cursor_v2(page["next_cursor"], case.scope, other.cursor_collection())


async def test_filters_select_on_the_server_and_prefixes_are_literal(operations_case):
    case = operations_case
    now = await clock(case)
    async with case.owner.begin() as session:
        other_release = uuid4()
        payload = (
            await session.execute(
                text("SELECT payload FROM worker_releases WHERE id=:id"), {"id": case.instances[0].release_id}
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO worker_releases VALUES(:id,:tenant,:project,:environment,:digest,cast(:payload AS jsonb))"
            ),
            {
                "id": other_release,
                "tenant": case.scope.tenant_id,
                "project": case.scope.project_id,
                "environment": case.scope.environment_id,
                "digest": OTHER_DIGEST,
                "payload": json.dumps({**payload, "id": str(other_release), "image_digest": OTHER_DIGEST}),
            },
        )
    a1 = (await add_workers(case, seen=now, host="fleet-a1"))[0]
    b1 = (await add_workers(case, seen=now, host="fleet-b1", draining=True))[0]
    other = (await add_workers(case, seen=now, host="fleet-c1", release=other_release, task_types=("other@2.0.0",)))[0]
    assert set(ids(await listed(case, host_prefix="fleet-"))) == {a1, b1, other}
    assert ids(await listed(case, host_prefix="fleet-a")) == [a1]
    for literal in ("fleet-%", "fleet-_1", "FLEET-", "fleet-a1x"):
        assert ids(await listed(case, host_prefix=literal)) == []
    assert ids(await listed(case, host_prefix="fleet-", draining=True)) == [b1]
    assert set(ids(await listed(case, host_prefix="fleet-", draining=False))) == {a1, other}
    assert ids(await listed(case, host_prefix="fleet-", release_id=other_release)) == [other]
    assert ids(await listed(case, task_type="other@2.0.0")) == [other]
    assert ids(await listed(case, task_type="other")) == []
    assert ids(await listed(case, host_prefix="fleet-", presence="unknown")) == []


async def test_placement_comes_from_the_latest_complete_unexpired_observation(operations_case):
    case = operations_case
    now = await clock(case)
    alpha, bravo, charlie, delta, echo = (
        await add_target(case, "alpha"),
        await add_target(case, "bravo"),
        await add_target(case, "charlie"),
        await add_target(case, "delta"),
        await add_target(case, "echo", revision=2),
    )
    deployments = {target: await add_deployment(case, target) for target in (alpha, bravo, charlie, delta, echo)}
    await add_observation(case, alpha, {"engine": ["alpha-1", "shared"]}, at=now - timedelta(minutes=5))
    await add_observation(case, bravo, {"engine": ["bravo-1", "shared"]})
    # An older complete observation still places members when a newer one is incomplete.
    await add_observation(case, charlie, {"engine": ["charlie-1"]}, at=now - timedelta(minutes=3))
    await add_observation(case, charlie, {"engine": []}, complete=False, at=now - timedelta(minutes=1))
    # The latest complete observation has expired: an older one is never presented as current.
    await add_observation(
        case, delta, {"engine": ["delta-1"]}, at=now - timedelta(minutes=30), expires=now + timedelta(minutes=10)
    )
    await add_observation(
        case, delta, {"engine": ["delta-1"]}, at=now - timedelta(minutes=16), expires=now - timedelta(minutes=1)
    )
    # Observed before the target changed.
    await add_observation(case, echo, {"engine": ["echo-1"]})
    # A member of a component that is not a worker places nothing.
    api = await add_target(case, "foxtrot")
    await add_deployment(case, api, components=[("engine", "api", DIGEST, None)])
    await add_observation(case, api, {"engine": ["api-1"]}, kind="api")
    hosts = {
        name: (await add_workers(case, seen=now, host=name))[0]
        for name in ("alpha-1", "bravo-1", "shared", "charlie-1", "delta-1", "echo-1", "api-1", "nowhere")
    }
    embedded = (await add_workers(case, seen=now, host="alpha-1", runtime="embedded"))[0]
    twin = (await add_workers(case, seen=now, host="alpha-1"))[0]
    reader = await principal_with(
        case, Grant(role="viewer", scope=case.scope), Grant(role="deployment_reader", scope=case.scope)
    )
    page = by_id(await listed(case, reader, state="all"))
    assert page[hosts["alpha-1"]]["placement"] == placement(alpha, "alpha", deployments[alpha])
    assert page[twin]["placement"] == placement(alpha, "alpha", deployments[alpha])
    assert page[hosts["bravo-1"]]["placement"] == placement(bravo, "bravo", deployments[bravo])
    assert page[hosts["charlie-1"]]["placement"] == placement(charlie, "charlie", deployments[charlie])
    for unplaced in ("shared", "delta-1", "echo-1", "api-1", "nowhere"):
        assert "placement" not in page[hosts[unplaced]], unplaced
    assert "placement" not in page[embedded] and page[embedded]["identity"]["runtime"] == "embedded"
    assert set(ids(await listed(case, reader, target_id=alpha))) == {hosts["alpha-1"], twin}
    assert ids(await listed(case, reader, target_id=delta)) == []
    # The fixture's actor reads workers but no deployment target, so it sees no placement.
    assert not any("placement" in item for item in (await listed(case, state="all"))["items"])
    read = await case.workers.read(reader, case.scope, hosts["bravo-1"], context=AuditContext())
    assert read.placement is not None and read.placement.model_dump(mode="json") == placement(
        bravo, "bravo", deployments[bravo]
    )


async def test_placement_names_only_targets_both_principals_may_read(operations_case):
    case = operations_case
    now = await clock(case)
    alpha, bravo = await add_target(case, "alpha"), await add_target(case, "bravo")
    first, second = await add_deployment(case, alpha), await add_deployment(case, bravo)
    await add_observation(case, alpha, {"engine": ["host-a1"]})
    await add_observation(case, bravo, {"engine": ["host-b1"]})
    on_alpha = (await add_workers(case, seen=now, host="host-a1"))[0]
    on_bravo = (await add_workers(case, seen=now, host="host-b1"))[0]
    viewer = Grant(role="viewer", scope=case.scope)
    reader = await principal_with(
        case, viewer, Grant(role="deployment_reader", scope=case.scope, resources=(str(alpha), str(bravo)))
    )
    unrestricted = await principal_with(case, viewer, Grant(role="deployment_reader", scope=case.scope))
    page = by_id(await listed(case, unrestricted, state="all"))
    assert page[on_alpha]["placement"] == placement(alpha, "alpha", first)
    assert page[on_bravo]["placement"] == placement(bravo, "bravo", second)
    # The stored principal loses bravo: the token alone no longer shows it.
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "UPDATE role_bindings SET resources=jsonb_build_array(cast(:target AS text)) "
                "WHERE principal_id=:id AND role='deployment_reader'"
            ),
            {"id": reader.id, "target": str(alpha)},
        )
    page = await listed(case, reader, state="all")
    assert by_id(page)[on_alpha]["placement"] == placement(alpha, "alpha", first)
    assert "placement" not in by_id(page)[on_bravo]
    assert "bravo" not in json.dumps(page) and str(second) not in json.dumps(page)
    assert ids(await listed(case, reader, target_id=alpha)) == [on_alpha]
    with pytest.raises(AccessDenied):
        await listed(case, reader, target_id=bravo)
    # A token without bravo is refused even when the stored principal could read it.
    token = reader.model_copy(
        update={"grants": (viewer, Grant(role="deployment_reader", scope=case.scope, resources=(str(alpha),)))}
    )
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE role_bindings SET resources='[]' WHERE principal_id=:id AND role='deployment_reader'"),
            {"id": reader.id},
        )
    assert "placement" not in by_id(await listed(case, token, state="all"))[on_bravo]
    with pytest.raises(AccessDenied):
        await listed(case, token, target_id=bravo)
    # Without any deployment grant the fleet is still listed, with no placement at all.
    plain = await principal_with(case, viewer)
    page = await listed(case, plain, state="all")
    assert not any("placement" in item for item in page["items"]) and "alpha" not in json.dumps(page)
    with pytest.raises(AccessDenied):
        await listed(case, plain, target_id=alpha)
    read = await case.workers.read(plain, case.scope, on_alpha, context=AuditContext())
    assert read.placement is None


async def test_token_and_stored_principal_both_need_status_read(monkeypatch, operations_case):
    from firefly_weave.definitions.service import DefinitionService

    case = operations_case
    opened = []
    original = DefinitionService.transaction

    def counting(self, *args, **kwargs):
        opened.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DefinitionService, "transaction", counting)
    outsider = Principal(id=case.actor.id, kind="application", grants=(Grant(role="developer", scope=case.scope),))
    with pytest.raises(AccessDenied):
        await listed(case, outsider)
    with pytest.raises(AccessDenied):
        await case.workers.read(outsider, case.scope, case.instances[0].id, context=AuditContext())
    assert opened == []
    async with case.owner.begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='viewer'"), {"id": case.actor.id}
        )
    with pytest.raises(AccessDenied):
        await listed(case)
    assert len(opened) == 1


async def test_a_page_above_the_byte_limit_is_refused_unread(monkeypatch, operations_case):
    import firefly_weave.workers.repository as repository

    case = operations_case
    created = await add_workers(case, 2, seen=await clock(case), host="bytes-")
    async with case.owner() as session:
        exact = await session.scalar(
            text(
                "SELECT sum(octet_length(w.payload::text)+octet_length(f.identity::text)) FROM worker_instances w "
                "JOIN worker_facts f ON f.worker_id=w.id WHERE w.id=ANY(:ids)"
            ),
            {"ids": created},
        )
    monkeypatch.setattr(repository, "PAGE_BYTES", exact)
    assert len((await listed(case, host_prefix="bytes-"))["items"]) == 2
    monkeypatch.setattr(repository, "PAGE_BYTES", exact - 1)
    with pytest.raises(CatalogError) as refused:
        await listed(case, host_prefix="bytes-")
    assert (refused.value.status, refused.value.code) == (429, "WV-PAGE-LIMIT")
    # The lookahead row is never measured.
    monkeypatch.setattr(repository, "PAGE_BYTES", exact // 2 + 1)
    assert len((await listed(case, host_prefix="bytes-", limit=1))["items"]) == 1


async def test_drain_time_is_recorded_once_and_cleared_on_resume(operations_case):
    case = operations_case
    worker = case.instances[0].id
    await case.access.grant(case.admin, case.actor.id, Grant(role="worker_operator", scope=case.scope))
    actor = await case.access.load_principal(case.actor.id)
    before = await clock(case)

    async def control(key, revision, draining):
        return await case.workers.control(
            actor,
            case.scope,
            worker,
            WorkerControlRequest(expected_revision=revision),
            key,
            draining=draining,
            context=AuditContext(),
        )

    drained = await control("drain", 1, True)
    assert drained.drained_at is not None and drained.drained_at >= before
    facts = await case.rows("SELECT drained_at FROM worker_facts WHERE worker_id=:id", id=worker)
    assert facts[0]["drained_at"] == drained.drained_at
    again = await control("drain-again", 2, True)
    assert again.drained_at == drained.drained_at and again.revision == 3
    assert (await control("drain", 1, True)) == drained
    listed_item = by_id(await listed(case, state="all"))[worker]
    assert listed_item["drained_at"] == drained.drained_at.isoformat().replace("+00:00", "Z")
    resumed = await control("resume", 3, False)
    assert resumed.drained_at is None and "drained_at" not in resumed.model_dump(mode="json")
    assert (await case.rows("SELECT drained_at FROM worker_facts WHERE worker_id=:id", id=worker))[0][
        "drained_at"
    ] is None
    # A worker drained before drain times were recorded keeps an unknown time.
    legacy = case.instances[1].id
    async with case.owner.begin() as session:
        await session.execute(text("UPDATE worker_instances SET draining=true WHERE id=:id"), {"id": legacy})
    repeated = await case.workers.control(
        actor,
        case.scope,
        legacy,
        WorkerControlRequest(expected_revision=1),
        "legacy",
        draining=True,
        context=AuditContext(),
    )
    assert repeated.draining and repeated.drained_at is None


async def test_dry_run_and_idempotent_retirement(operations_case):
    case = operations_case
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE worker_instances SET last_seen_at=clock_timestamp()-interval '25 hours' WHERE id=:id"),
            {"id": case.instances[0].id},
        )
    request = RetireStaleRequest(older_than_seconds=86400, dry_run=True)
    dry = await case.workers.retire_stale(case.actor, case.scope, request, "preview", context=AuditContext())
    assert (dry.matched, dry.revoked, dry.worker_ids) == (1, 0, [case.instances[0].id])
    assert not (await case.rows("SELECT revoked FROM worker_instances WHERE id=:id", id=case.instances[0].id))[0][
        "revoked"
    ]
    assert await case.workers.retire_stale(case.actor, case.scope, request, "preview", context=AuditContext()) == dry
    applied = await case.workers.retire_stale(
        case.actor,
        case.scope,
        RetireStaleRequest(older_than_seconds=86400, dry_run=False),
        "apply",
        context=AuditContext(),
    )
    assert (applied.matched, applied.revoked, applied.worker_ids) == (1, 1, [case.instances[0].id])
    rows = await case.rows(
        "SELECT revoked,control_revision FROM worker_instances WHERE id=:id", id=case.instances[0].id
    )
    assert rows[0]["revoked"] and rows[0]["control_revision"] == 2
    with pytest.raises(CatalogError) as conflict:
        await case.workers.retire_stale(case.actor, case.scope, request, "apply", context=AuditContext())
    assert conflict.value.code == "WV-IDEMPOTENCY-CONFLICT"
    audits = await owner_rows(case, "SELECT event FROM access_audit WHERE action='worker.retire_stale' ORDER BY id")
    assert [item["event"]["details"]["dry_run"] for item in audits] == [True, False]
    assert audits[1]["event"]["details"]["worker_ids"] == [str(case.instances[0].id)]
    assert audits[1]["event"]["capability"] == "release.retire"


async def test_retirement_threshold_and_last_contact_are_exact(monkeypatch, operations_case):
    case = operations_case
    now = await clock(case)
    hour, edge = timedelta(hours=1), timedelta(microseconds=1)
    exactly = (await add_workers(case, seen=now - hour))[0]
    older = (await add_workers(case, seen=now - hour - edge))[0]
    never_seen_new = (await add_workers(case, registered=now - hour - edge))[0]
    never_seen_recent = (await add_workers(case, registered=now - hour))[0]
    never_seen_legacy = (await add_workers(case, registered=None))[0]
    without_facts = (await add_workers(case, facts=False))[0]
    seen_long_ago_registered_now = (await add_workers(case, seen=now - 2 * hour, registered=now))[0]
    await add_workers(case, seen=now - 2 * hour, revoked=True)
    freeze(monkeypatch, now)
    request = RetireStaleRequest(older_than_seconds=3600, dry_run=True)
    result = await case.workers.retire_stale(case.actor, case.scope, request, "edge", context=AuditContext())
    # Oldest last contact first: the last contact wins over registration, and the unknown never qualifies.
    assert result.worker_ids == [seen_long_ago_registered_now, *sorted((older, never_seen_new))]
    assert result.matched == 3
    assert not {exactly, never_seen_recent, never_seen_legacy, without_facts} & set(result.worker_ids)


async def test_active_leases_keep_their_worker_and_rows(operations_case, task_service):
    case = operations_case
    await case.start("lease")
    worker = case.instances[0].id
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, worker, 1))[0]
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "UPDATE task_leases SET expires_at=clock_timestamp()+interval '1 hour',"
                "deadline=clock_timestamp()+interval '1 hour' WHERE task_id=:task"
            ),
            {"task": lease.proof.task_id},
        )
        await session.execute(
            text("UPDATE worker_instances SET last_seen_at=clock_timestamp()-interval '30 hours' WHERE id=:id"),
            {"id": worker},
        )
    retire = RetireStaleRequest(older_than_seconds=86400, dry_run=False)
    kept = await case.workers.retire_stale(case.actor, case.scope, retire, "kept", context=AuditContext())
    assert (kept.matched, kept.revoked) == (0, 0)
    listed_item = by_id(await listed(case, state="all"))[worker]
    assert listed_item["active_leases"] == 1 and listed_item["available_capacity"] is None
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE task_id=:task"),
            {"task": lease.proof.task_id},
        )
    expired = await case.workers.retire_stale(case.actor, case.scope, retire, "expired", context=AuditContext())
    assert expired.worker_ids == [worker]
    # Retirement never deletes or settles the attempt; recovery owns it.
    rows = await case.rows("SELECT status FROM task_leases WHERE task_id=:task", task=lease.proof.task_id)
    assert [row["status"] for row in rows] == ["active"]


async def test_one_hundred_workers_per_command_oldest_first(operations_case):
    case = operations_case
    now = await clock(case)
    stale = await add_workers(case, 105, seen=[now - timedelta(hours=30, minutes=index) for index in range(105)])
    oldest_first = list(reversed(stale))
    retire = RetireStaleRequest(older_than_seconds=86400, dry_run=False)
    first = await case.workers.retire_stale(case.actor, case.scope, retire, "one", context=AuditContext())
    assert (first.matched, first.revoked, first.worker_ids) == (105, 100, oldest_first[:100])
    assert await case.workers.retire_stale(case.actor, case.scope, retire, "one", context=AuditContext()) == first
    audits = await owner_rows(case, "SELECT count(*) AS count FROM access_audit WHERE action='worker.retire_stale'")
    assert audits[0]["count"] == 1
    second = await case.workers.retire_stale(case.actor, case.scope, retire, "two", context=AuditContext())
    assert (second.matched, second.revoked, second.worker_ids) == (5, 5, oldest_first[100:])
    revoked = await case.rows(
        "SELECT count(*) AS count FROM worker_instances WHERE id=ANY(:ids) AND revoked AND control_revision=2",
        ids=stale,
    )
    assert revoked[0]["count"] == 105


async def test_a_worker_locked_elsewhere_is_skipped_and_counted(operations_case):
    case = operations_case
    now = await clock(case)
    first, second = await add_workers(case, 2, seen=[now - timedelta(hours=40), now - timedelta(hours=30)])
    retire = RetireStaleRequest(older_than_seconds=86400, dry_run=False)
    async with case.owner.begin() as session:
        await session.execute(text("SELECT 1 FROM worker_instances WHERE id=:id FOR UPDATE"), {"id": first})
        skipped = await case.workers.retire_stale(case.actor, case.scope, retire, "busy", context=AuditContext())
    assert (skipped.matched, skipped.revoked, skipped.worker_ids) == (2, 1, [second])
    later = await case.workers.retire_stale(case.actor, case.scope, retire, "free", context=AuditContext())
    assert later.worker_ids == [first]


async def test_the_locked_update_rechecks_contact_leases_and_revocation(operations_case, task_service):
    case = operations_case
    now = await clock(case)
    await case.start("recheck")
    leased = case.instances[0].id
    async with case.tx() as tx:
        lease = (await task_service.claim(tx, leased, 1))[0]
    fresh = (await add_workers(case, seen=now))[0]
    gone = (await add_workers(case, seen=now - timedelta(hours=30), revoked=True))[0]
    eligible = (await add_workers(case, seen=now - timedelta(hours=30)))[0]
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE worker_instances SET last_seen_at=:seen WHERE id=:id"),
            {"id": leased, "seen": now - timedelta(hours=30)},
        )
        await session.execute(
            text("UPDATE task_leases SET expires_at=:until,deadline=:until WHERE task_id=:task"),
            {"task": lease.proof.task_id, "until": now + timedelta(hours=1)},
        )
    async with case.tx() as tx:
        revoked = await WorkerRepository(tx).retire([leased, fresh, gone, eligible], now - timedelta(hours=24), now)
    assert revoked == [eligible]


async def test_claims_and_retirement_never_leave_a_revoked_owner_with_a_live_lease(operations_case, task_service):
    case = operations_case
    await case.start("race")
    worker = case.instances[0].id
    async with case.owner.begin() as session:
        await session.execute(
            text("UPDATE worker_instances SET last_seen_at=clock_timestamp()-interval '30 hours' WHERE id=:id"),
            {"id": worker},
        )

    async def claim():
        async with case.tx() as tx:
            return await task_service.claim(tx, worker, 1)

    retire = case.workers.retire_stale(
        case.actor,
        case.scope,
        RetireStaleRequest(older_than_seconds=86400, dry_run=False),
        "race",
        context=AuditContext(),
    )
    outcomes = await asyncio.gather(claim(), retire, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, CatalogError):
            assert outcome.code in {"WV-OPERATION-CAPACITY", "WV-LEASE"}, outcome.code
        else:
            assert not isinstance(outcome, BaseException), outcome
    live = await case.rows(
        "SELECT count(*) AS count FROM task_leases l JOIN worker_instances w ON w.id=l.owner "
        "WHERE w.revoked AND l.status='active' AND l.expires_at>clock_timestamp()"
    )
    assert live[0]["count"] == 0


async def test_retirement_needs_release_retire_on_the_token_and_the_stored_principal(monkeypatch, operations_case):
    from firefly_weave.definitions.service import DefinitionService

    case = operations_case
    opened = []
    original = DefinitionService.transaction

    def counting(self, *args, **kwargs):
        opened.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DefinitionService, "transaction", counting)
    request = RetireStaleRequest(older_than_seconds=86400, dry_run=True)
    viewer = Principal(id=case.actor.id, kind="application", grants=(Grant(role="viewer", scope=case.scope),))
    with pytest.raises(AccessDenied):
        await case.workers.retire_stale(viewer, case.scope, request, "denied", context=AuditContext())
    assert opened == []
    async with case.owner.begin() as session:
        await session.execute(
            text("DELETE FROM role_bindings WHERE principal_id=:id AND role='deployer'"), {"id": case.actor.id}
        )
    with pytest.raises(AccessDenied):
        await case.workers.retire_stale(case.actor, case.scope, request, "removed", context=AuditContext())
    assert len(opened) == 1
    assert not await case.rows("SELECT 1 FROM mutation_idempotency WHERE idempotency_key IN ('denied','removed')")


async def test_release_usage_counts_workers_activations_and_permitted_deployments(operations_case):
    case = operations_case
    now = await clock(case)
    release = case.instances[0].release_id
    await add_workers(case, seen=now)
    await add_workers(case, seen=now, draining=True)
    await add_workers(case, seen=now - timedelta(hours=2))
    await add_workers(case, seen=now, revoked=True)
    alpha, bravo = await add_target(case, "alpha"), await add_target(case, "bravo")
    first = await add_deployment(
        case,
        alpha,
        components=[
            ("engine", "worker", DIGEST, None),
            ("batch", "worker", OTHER_DIGEST, None),
            ("api", "api", DIGEST, None),
        ],
    )
    other_release = uuid4()
    second = await add_deployment(
        case, bravo, components=[("engine", "worker", DIGEST, None), ("zeta", "worker", DIGEST, None)]
    )
    await add_deployment(case, bravo, name="elsewhere", components=[("engine", "worker", DIGEST, other_release)])

    async def usage(actor, **values):
        return await case.workers.release_usage(
            actor, case.scope, release, ReleaseUsageQuery(**values), context=AuditContext()
        )

    # The fixture's actor reads the catalog but no deployment target.
    catalog_only = await usage(case.actor)
    assert catalog_only.workers.model_dump() == {"online": 1, "offline": 3, "draining": 1, "revoked": 1}
    assert catalog_only.activations == 1 and catalog_only.deployments == [] and catalog_only.next_cursor is None
    viewer = Grant(role="viewer", scope=case.scope)
    everything = await principal_with(case, viewer, Grant(role="deployment_reader", scope=case.scope))
    rows = (await usage(everything)).deployments
    expected = sorted(
        [(alpha, "alpha", first, "engine"), (bravo, "bravo", second, "engine"), (bravo, "bravo", second, "zeta")],
        key=lambda row: (row[2], row[3]),
    )
    assert [(row.target_id, row.target_name, row.deployment_id, row.component) for row in rows] == expected
    assert all(row.replicas == 2 and row.deployment_name == "weave" for row in rows)
    restricted = await principal_with(
        case, viewer, Grant(role="deployment_reader", scope=case.scope, resources=(str(alpha),))
    )
    limited = await usage(restricted)
    assert [(row.target_id, row.component) for row in limited.deployments] == [(alpha, "engine")]
    assert "bravo" not in limited.model_dump_json()
    walked, cursor = [], None
    while True:
        page = await usage(everything, limit=1, **({"cursor": cursor} if cursor else {}))
        walked += [(row.deployment_id, row.component) for row in page.deployments]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert walked == [(row[2], row[3]) for row in expected]
    first_page = await usage(everything, limit=1)
    with pytest.raises(ValueError):
        await usage(restricted, limit=1, cursor=first_page.next_cursor)


async def test_release_usage_activations_skip_retired_ones(operations_case):
    case = operations_case
    release = case.instances[0].release_id
    async with case.owner.begin() as session:
        row = (
            (await session.execute(text("SELECT * FROM activation_revisions WHERE id=:id"), {"id": case.activation.id}))
            .mappings()
            .one()
        )
        retired = {**row["payload"], "id": str(uuid4()), "revision": row["revision"] + 1, "retired": True}
        await session.execute(
            text(
                "INSERT INTO activation_revisions(id,tenant_id,project_id,environment_id,name,revision,version_id,"
                "payload) VALUES(:id,:tenant,:project,:environment,:name,:revision,:version,cast(:payload AS jsonb))"
            ),
            {
                "id": UUID(retired["id"]),
                "tenant": row["tenant_id"],
                "project": row["project_id"],
                "environment": row["environment_id"],
                "name": row["name"],
                "revision": retired["revision"],
                "version": row["version_id"],
                "payload": json.dumps(retired),
            },
        )
    usage = await case.workers.release_usage(
        case.actor, case.scope, release, ReleaseUsageQuery(), context=AuditContext()
    )
    assert usage.activations == 0


async def test_release_usage_activations_skip_retired_workflow_versions(operations_case):
    case = operations_case
    release = case.instances[0].release_id

    async def activations():
        usage = await case.workers.release_usage(
            case.actor, case.scope, release, ReleaseUsageQuery(), context=AuditContext()
        )
        return usage.activations

    assert await activations() == 1
    # Runs cannot start from an activation whose workflow version is retired, so it no longer counts.
    project = case.scope.model_copy(update={"environment_id": None})
    await case.access.grant(case.admin, case.actor.id, Grant(role="deployer", scope=project))
    await case.workers.definitions.retire(
        await case.access.load_principal(case.actor.id),
        case.scope,
        case.activation.request.version_id,
        "Workflow",
        "retire-version",
        context=AuditContext(),
    )
    assert await activations() == 0


async def test_release_usage_is_scoped_and_authorized(monkeypatch, operations_case, provisioned):
    from firefly_weave.definitions.service import DefinitionService

    case = operations_case
    with pytest.raises(CatalogError) as missing:
        await case.workers.release_usage(case.actor, case.scope, uuid4(), ReleaseUsageQuery(), context=AuditContext())
    assert (missing.value.status, missing.value.code) == (404, "WV-NOT-FOUND")
    other = provisioned[1][1]
    owner = await principal_with(case, Grant(role="viewer", scope=other))
    with pytest.raises(CatalogError) as foreign:
        await case.workers.release_usage(
            owner, other, case.instances[0].release_id, ReleaseUsageQuery(), context=AuditContext()
        )
    assert foreign.value.status == 404
    opened = []
    original = DefinitionService.transaction

    def counting(self, *args, **kwargs):
        opened.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DefinitionService, "transaction", counting)
    outsider = Principal(id=case.actor.id, kind="application", grants=(Grant(role="worker", scope=case.scope),))
    with pytest.raises(AccessDenied):
        await case.workers.release_usage(
            outsider, case.scope, case.instances[0].release_id, ReleaseUsageQuery(), context=AuditContext()
        )
    assert opened == []


async def test_http_fleet_retirement_and_usage(
    client, authenticated_client, access_db, provisioned, headers, env_url, operations_case
):
    from firefly_weave.api.transport import encode_cursor

    case = operations_case
    async with access_db[0]() as session:
        principal = await session.scalar(text("SELECT principal_id FROM identity_links WHERE subject='0'"))
    canonical = "/api/v1" + env_url
    for base in (env_url, canonical):
        first = await client.get(base + "/workers?state=all&order=registered_desc&limit=1", headers=headers)
        assert first.status_code == 200, first.text
        assert len(first.json()["items"]) == 1 and first.json()["next_cursor"]
        second = await client.get(
            base + "/workers",
            params={"state": "all", "order": "registered_desc", "limit": 1, "cursor": first.json()["next_cursor"]},
            headers=headers,
        )
        assert second.status_code == 200 and second.json()["items"][0]["id"] != first.json()["items"][0]["id"]
        for params in ({"colour": "red"}, {"limit": "0"}, {"presence": "online"}, {"limit": "1", "cursor": "abc"}):
            refused = await client.get(base + "/workers", params=params, headers=headers)
            assert (refused.status_code, refused.json()["code"]) == (422, "WV-VALIDATION"), params
    earlier = encode_cursor(case.scope, "workers", case.instances[0].id)
    for base, cursor in ((canonical, earlier), (env_url, str(case.instances[0].id))):
        refused = await client.get(base + "/workers", params={"cursor": cursor}, headers=headers)
        assert refused.status_code == 422
    body = {"older_than_seconds": 86400, "dry_run": True}
    denied = await client.post(
        canonical + "/workers/retire-stale", headers={**headers, "Idempotency-Key": "k"}, json=body
    )
    assert (denied.status_code, denied.json()["code"]) == (403, "WV-FORBIDDEN")
    await case.access.grant(provisioned[0], principal, Grant(role="deployer", scope=case.scope))
    missing = await client.post(canonical + "/workers/retire-stale", headers=headers, json=body)
    assert missing.status_code == 422
    invalid = await client.post(
        canonical + "/workers/retire-stale",
        headers={**headers, "Idempotency-Key": "short"},
        json={"older_than_seconds": 60, "dry_run": True},
    )
    assert (invalid.status_code, invalid.json()["code"]) == (422, "WV-VALIDATION")
    preview = await client.post(
        canonical + "/workers/retire-stale", headers={**headers, "Idempotency-Key": "k"}, json=body
    )
    assert preview.status_code == 200, preview.text
    assert preview.json() == {"matched": 0, "revoked": 0, "worker_ids": []}
    assert preview.headers["cache-control"] == "no-store"
    release = case.instances[0].release_id
    usage = await client.get(f"{canonical}/worker-releases/{release}/usage", headers=headers)
    assert usage.status_code == 200, usage.text
    assert usage.json()["deployments"] == [] and usage.json()["activations"] == 1
    assert (await client.get(f"{canonical}/worker-releases/{uuid4()}/usage", headers=headers)).status_code == 404
    assert (
        await client.get(f"{canonical}/worker-releases/{release}/usage?limit=101", headers=headers)
    ).status_code == 422
