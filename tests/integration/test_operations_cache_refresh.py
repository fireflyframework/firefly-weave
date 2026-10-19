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

"""The operations loop revalidates cached run listing decisions in bounded, resumable quanta on real PostgreSQL.

Runs pin ``text.concat``. A server rolled back below it is simulated by advertising no language features;
the real retained artifact and runtime admission decide every classification.
"""

import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.scheduler import _SchedulerScope
from firefly_weave.contracts import language_features
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_views import RunStepQuery, RunSummaryQuery
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.facts import FactRepository, listing_metadata
from firefly_weave.operations.loop import ENVIRONMENT_SECONDS, OperationsLoop
from firefly_weave.operations.loop_health import LoopHealth
from firefly_weave.operations.run_views import RunViewService
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.runtime import admission
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.settings import Settings

pytestmark = pytest.mark.integration

GREETING = {"op": {"name": "concat", "args": [{"literal": "n="}, {"ref": "/input"}]}}
CLASSIFICATION = "classification_state,classification_policy,pinned_ir_version,pinned_features,node_kinds"
# Ordinary storage saturated: a write that grows a row fails, and one that shrinks it still succeeds.
SATURATE = text("UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}','0')")
RESTORE = text("UPDATE operation_policy SET limits=jsonb_set(limits,'{ordinary_bytes}','1099511627776')")


def ids(prefix: int, count: int, start: int = 1) -> list[UUID]:
    return [UUID(f"{prefix:08x}-0000-4000-8000-{index:012d}") for index in range(start, start + count)]


@pytest.fixture
def worker_runtime_fixture(worker_runtime_fixture):
    """The shared worker flow whose task input is built by ``text.concat``."""
    document = json.loads(worker_runtime_fixture["source"])
    document["metadata"]["name"] = "greeting-flow"
    document["spec"]["steps"][0]["with"] = GREETING
    return {**worker_runtime_fixture, "source": json.dumps(document)}


@pytest.fixture
async def refresh(operations_case, scheduler_url):
    """An operations loop on the execute-only scheduler login, cycled by the test instead of its poll task."""
    case = operations_case
    health = LoopHealth()
    health.register("operations_loop", 60, enabled=True)
    loop = OperationsLoop(
        Settings(scheduler_enabled=False, database_url=case.url.render_as_string(hide_password=False)),
        UnitOfWork(case.sessions),
        health,
    )
    loop.engine = create_async_engine(scheduler_url)
    # Quanta whose budget ran out: each can cut short at most the one refresh it was running.
    loop.cut_short = 0  # type: ignore[attr-defined]
    environment = loop._environment

    async def quantum(authority):
        started = loop.monotonic()
        try:
            await environment(authority)
        except Exception:
            if loop.monotonic() >= started + ENVIRONMENT_SECONDS - 0.01:
                loop.cut_short += 1  # type: ignore[attr-defined]
            raise

    loop._environment = quantum  # type: ignore[method-assign]
    try:
        yield loop
    finally:
        await loop.engine.dispose()


def extra_reads(reads: Counter, runs) -> int:
    """Reads beyond one per run: refreshes cut short at a budget edge and retried after the keyset wrapped."""
    return sum(reads[run] for run in runs) - len(runs)


class Reads(Counter):
    """Retained sources fetched by listing-metadata refreshes, by run, and any fetch of a run already refreshed."""

    after_success: list[UUID]


@pytest.fixture
def payload_reads(monkeypatch):
    """Every retained source a listing-metadata refresh fetches, by run.

    A refresh cut short by the end of its quantum's budget rolls back and is retried after the keyset wraps, so a
    run can be read again before its decision is stored. A read of a run whose stored decision is no longer due
    would repeat completed work; those are listed in ``after_success``.
    """
    reads = Reads()
    reads.after_success = []
    refreshing: ContextVar[bool] = ContextVar("refreshing", default=False)
    fetch, refresh = RuntimeRepository.run, FactRepository.refresh_listing_metadata

    async def run(self, identifier, *, lock=False):
        if refreshing.get():
            reads[identifier] += 1
            stored = (
                (
                    await self.tx.session.execute(
                        text(
                            "SELECT classification_state,classification_policy,pinned_ir_version,pinned_features "
                            "FROM run_facts WHERE run_id=:id"
                        ),
                        {"id": identifier},
                    )
                )
                .mappings()
                .one()
            )
            supported = admission.runnable_parameters()
            due = stored["classification_policy"] != admission.POLICY or (
                stored["classification_state"] == "unsupported"
                and stored["pinned_ir_version"] in supported["ir_versions"]
                and set(stored["pinned_features"]) <= set(json.loads(supported["ir_features"]))
            )
            if not due:
                reads.after_success.append(identifier)
        return await fetch(self, identifier, lock=lock)

    async def refreshed(self, run_id):
        token = refreshing.set(True)
        try:
            return await refresh(self, run_id)
        finally:
            refreshing.reset(token)

    monkeypatch.setattr(RuntimeRepository, "run", run)
    monkeypatch.setattr(FactRepository, "refresh_listing_metadata", refreshed)
    return reads


@asynccontextmanager
async def rolled_back():
    """A server whose capabilities list no language features."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(language_features, "ADVERTISED_FEATURES", ())
        yield


async def source(case, run: UUID) -> dict:
    async with case.owner() as session:
        return dict((await session.execute(text("SELECT * FROM runs WHERE id=:id"), {"id": run})).mappings().one())


async def cached(case, run: UUID) -> dict:
    async with case.owner() as session:
        return dict(
            (await session.execute(text(f"SELECT {CLASSIFICATION} FROM run_facts WHERE run_id=:id"), {"id": run}))
            .mappings()
            .one()
        )


async def states(case, runs) -> dict[UUID, str]:
    async with case.owner() as session:
        rows = await session.execute(
            text("SELECT run_id,classification_state FROM run_facts WHERE run_id=ANY(:ids)"), {"ids": list(runs)}
        )
        return dict(rows.all())


async def record_decision(case, run: UUID, metadata: dict) -> None:
    """Store a listing decision the way the upgrade backfill records one."""
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "UPDATE run_facts SET classification_state=:classification_state,"
                "classification_policy=:classification_policy,pinned_ir_version=:pinned_ir_version,"
                "pinned_features=cast(:pinned_features AS jsonb),node_kinds=cast(:node_kinds AS jsonb) "
                "WHERE run_id=:run"
            ),
            {
                **metadata,
                "pinned_features": json.dumps(metadata["pinned_features"]),
                "node_kinds": json.dumps(metadata["node_kinds"]),
                "run": run,
            },
        )


async def record_rolled_back_decision(case, run: UUID) -> dict:
    """What a server rolled back below ``text.concat`` records for the run: a transient unsupported decision."""
    row = await source(case, run)
    async with rolled_back():
        assert admission.admission(row) == "unsupported"
        metadata = listing_metadata(row)
    assert metadata["classification_state"] == "unsupported" and metadata["pinned_features"] == ["text.concat"]
    await record_decision(case, run, metadata)
    return metadata


async def cancelled_source(case) -> UUID:
    run = await case.start("source")
    async with case.tx() as tx:
        await case.runtime.cancel(tx, run.id, "fixture", actor=case.actor, scope=case.scope, context=AuditContext())
    return run.id


async def clone(
    case,
    run: UUID,
    identifiers,
    *,
    environment=None,
    activation=None,
    future=False,
    node_kinds=None,
    state="unsupported",
):
    """Retained copies of a real run, each cached (unsupported by default) like a backfill on a rolled-back server."""
    artifact = "jsonb_set(r.artifact,'{executable,irVersion}','\"weave/ir-future\"')" if future else "r.artifact"
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO runs SELECT (jsonb_populate_record(NULL::runs,to_jsonb(r) || jsonb_build_object("
                "'id',c.id,'environment_id',coalesce(cast(:environment AS uuid),r.environment_id),"
                "'activation_id',coalesce(cast(:activation AS uuid),r.activation_id),'artifact'," + artifact + "))).* "
                "FROM runs r CROSS JOIN unnest(cast(:ids AS uuid[])) AS c(id) WHERE r.id=:run"
            ),
            {"ids": list(identifiers), "run": run, "environment": environment, "activation": activation},
        )
        await session.execute(
            text(
                "INSERT INTO run_facts SELECT (jsonb_populate_record(NULL::run_facts,to_jsonb(f) || jsonb_build_object("
                "'run_id',c.id,'environment_id',coalesce(cast(:environment AS uuid),f.environment_id),"
                "'activation_id',coalesce(cast(:activation AS uuid),f.activation_id),"
                "'classification_state',cast(:state AS text),'pinned_features','[\"text.concat\"]'::jsonb,"
                "'pinned_ir_version',CASE WHEN :future THEN 'weave/ir-future' ELSE f.pinned_ir_version END,"
                "'node_kinds',coalesce(cast(:node_kinds AS jsonb),'{}'::jsonb)))).* "
                "FROM run_facts f CROSS JOIN unnest(cast(:ids AS uuid[])) AS c(id) WHERE f.run_id=:run"
            ),
            {
                "ids": list(identifiers),
                "run": run,
                "environment": environment,
                "activation": activation,
                "future": future,
                "state": state,
                "node_kinds": None if node_kinds is None else json.dumps(node_kinds),
            },
        )


async def second_environment(case, access_db, provisioned) -> tuple[Scope, UUID]:
    """Another environment of the same tenant and project, with a copy of the activation its runs pin."""
    admin = await access_db[2].load_principal(provisioned[0].id)
    environment = await access_db[2].create_environment(
        admin, Scope(tenant_id=case.scope.tenant_id, project_id=case.scope.project_id), "second"
    )
    activation = UUID("a0000000-0000-4000-8000-000000000001")
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO activation_revisions SELECT (jsonb_populate_record(NULL::activation_revisions,"
                "to_jsonb(a) || jsonb_build_object('id',cast(:new AS uuid),'environment_id',cast(:environment AS uuid)"
                "))).* FROM activation_revisions a WHERE a.id=:activation"
            ),
            {"new": activation, "environment": environment, "activation": case.activation.id},
        )
    return case.scope.model_copy(update={"environment_id": environment}), activation


async def cursors(case) -> dict:
    async with case.owner() as session:
        return {
            "facts": sorted(
                (await session.execute(text("SELECT environment_id,after_run_id FROM operations_fact_cursors"))).all()
            ),
            "environments": sorted(
                (
                    await session.execute(text("SELECT tenant_id,environment_id FROM operations_environment_cursors"))
                ).all()
            ),
            "tenants": (await session.execute(text("SELECT tenant_id FROM operations_tenant_cursor"))).all(),
        }


async def cache(case) -> list:
    async with case.owner() as session:
        return sorted(
            (await session.execute(text(f"SELECT run_id,{CLASSIFICATION} FROM run_facts ORDER BY run_id"))).all()
        )


def service(case) -> RunViewService:
    return RunViewService(UnitOfWork(case.sessions), AuthorizationService())


async def summary(case, run: UUID):
    page = await service(case).summaries(
        case.actor, case.scope, RunSummaryQuery(limit=100), None, context=AuditContext()
    )
    [item] = [item for item in page.items if item.id == run]
    return item


async def http_reads_leave_the_cache(case, authenticated_client, run: UUID) -> None:
    """Metadata reads over HTTP select no payload and change no cached decision or cursor."""
    client, tokens, _ = authenticated_client
    app = client._transport.app
    scope = case.scope
    prefix = f"/api/v1/tenants/{scope.tenant_id}/projects/{scope.project_id}/environments/{scope.environment_id}"
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    before = (await cache(case), await cursors(case))
    engine = app.state.resources.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        headers = {"Authorization": "Bearer " + tokens[0]}
        assert (await client.get(prefix + "/run-summaries?limit=100", headers=headers)).status_code == 200
        assert (await client.get(prefix + f"/runs/{run}/steps", headers=headers)).status_code in {200, 409, 422}
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert (await cache(case), await cursors(case)) == before
    assert any("from run_facts" in sql for sql in statements)
    assert not any("from runs " in sql or "runs.*" in sql or "weave_runs_bytes" in sql for sql in statements)
    assert not any(
        "run_facts set" in sql or "operations_fact_cursors" in sql or "operations_environment_cursors" in sql
        for sql in statements
    )


async def test_operations_traversal_is_execute_only_and_never_moves_recovery_cursors(
    operations_case, refresh, scheduler_url, monkeypatch
):
    case = operations_case
    await case.start()
    health = LoopHealth()
    health.register("operations_loop", 60, enabled=True)

    async def idle():
        return None

    refused = OperationsLoop(
        Settings(
            database_url=case.url.render_as_string(hide_password=False),
            scheduler_database_url=SecretStr(case.url.render_as_string(hide_password=False)),
        ),
        UnitOfWork(case.sessions),
        health,
    )
    with pytest.raises(RuntimeError, match="execute-only"):
        await refused.open()
    assert refused.engine is None and refused.task is None
    accepted = OperationsLoop(
        Settings(
            database_url=case.url.render_as_string(hide_password=False),
            scheduler_database_url=SecretStr(scheduler_url),
        ),
        UnitOfWork(case.sessions),
        health,
    )
    monkeypatch.setattr(accepted, "poll", idle)
    await accepted.open()
    await accepted.close()
    async with case.owner.begin() as session:
        recovery_before = (
            (await session.execute(text("SELECT tenant_id FROM scheduler_tenant_cursor"))).all(),
            sorted((await session.execute(text("SELECT * FROM scheduler_environment_cursors"))).all()),
        )
        await session.execute(text("SET LOCAL ROLE weave_scheduler"))
        for value in (None, 0, 17):
            with pytest.raises(Exception, match="Bounded catalog page"):
                async with session.begin_nested():
                    await session.execute(text("SELECT weave_operations_tenants(:size)"), {"size": value})
    async with case.owner() as session:
        assert not await session.scalar(
            text("SELECT has_function_privilege('weave_app','weave_operations_tenants(integer)','EXECUTE')")
        )
        assert not await session.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='operation_usage' AND tgrelid IN "
                "('operations_tenant_cursor'::regclass,'operations_environment_cursors'::regclass,"
                "'operations_fact_cursors'::regclass))"
            )
        )
        index = await session.scalar(text("SELECT indexdef FROM pg_indexes WHERE indexname='run_facts_revalidation'"))
    assert "(tenant_id, project_id, environment_id, run_id)" in index
    assert "classification_state = 'unsupported'" in index
    assert f"classification_policy <> '{admission.POLICY}'" in index
    assert await refresh.cycle() is True
    async with case.owner() as session:
        recovery_after = (
            (await session.execute(text("SELECT tenant_id FROM scheduler_tenant_cursor"))).all(),
            sorted((await session.execute(text("SELECT * FROM scheduler_environment_cursors"))).all()),
        )
    assert recovery_after == recovery_before
    moved = await cursors(case)
    # Both provisioned tenants were visited through the operations cursors only.
    assert len(moved["environments"]) == 2 and moved["tenants"][0][0] is not None


async def test_unsupported_fact_becomes_available_after_support_returns(operations_case, refresh, payload_reads):
    case = operations_case
    run = (await case.start()).id
    await record_rolled_back_decision(case, run)
    row = await source(case, run)
    async with rolled_back():
        assert (await summary(case, run)).unavailable
        # Still rolled back: the pins stay unsupported, so the run is not due and its source is never read.
        assert await refresh.cycle() is True
        assert payload_reads == Counter() and (await cached(case, run))["classification_state"] == "unsupported"
    # Support returns: runtime admission runs the run again, while the cached decision still withholds it.
    assert admission.admission(row) == "available"
    assert (await summary(case, run)).unavailable
    with pytest.raises(CatalogError) as withheld:
        await service(case).steps(case.actor, case.scope, run, RunStepQuery(), None, context=AuditContext())
    assert (withheld.value.status, withheld.value.code) == (422, "WV-IR-UNSUPPORTED")
    assert await refresh.cycle() is True
    assert payload_reads == Counter({run: 1})
    refreshed = await cached(case, run)
    assert refreshed["classification_state"] == "available" and refreshed["classification_policy"] == admission.POLICY
    assert refreshed["node_kinds"] and refreshed["pinned_features"] == ["text.concat"]
    assert not getattr(await summary(case, run), "unavailable", False)
    page = await service(case).steps(case.actor, case.scope, run, RunStepQuery(), None, context=AuditContext())
    assert page.items
    assert await refresh.cycle() is True
    assert payload_reads == Counter({run: 1})


async def test_policy_change_withholds_until_cache_is_revalidated(operations_case, refresh, payload_reads, monkeypatch):
    case = operations_case
    first = (await case.start("first")).id
    monkeypatch.setattr(admission, "POLICY", "classified-v2")
    assert (await summary(case, first)).unavailable
    with pytest.raises(CatalogError) as withheld:
        await service(case).steps(case.actor, case.scope, first, RunStepQuery(), None, context=AuditContext())
    assert (withheld.value.status, withheld.value.code) == (409, "WV-LEGACY-UNAVAILABLE")
    assert await refresh.cycle() is True
    assert payload_reads == Counter({first: 1})
    assert (await cached(case, first))["classification_policy"] == "classified-v2"
    assert not getattr(await summary(case, first), "unavailable", False)
    # A mismatch revalidated while rolled back becomes unsupported under the current policy and drops out.
    second = (await case.start("second")).id
    monkeypatch.setattr(admission, "POLICY", "classified-v3")
    async with rolled_back():
        # Rows before the keyset boundary wait for its wrap, so a few cycles visit both runs once.
        for _ in range(4):
            assert await refresh.cycle() is True
        assert payload_reads[second] == 1 and payload_reads[first] == 2
        assert (await cached(case, second))["classification_state"] == "unsupported"
        for _ in range(2):
            assert await refresh.cycle() is True
        assert payload_reads[second] == 1 and payload_reads[first] == 2
    # Its pins are supported again, so it is due once more; a policy block still withholds it afterwards.
    assert await refresh.cycle() is True
    assert payload_reads[second] == 2 and (await cached(case, second))["classification_state"] == "available"
    async with case.owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO run_policy_blocks(tenant_id,project_id,environment_id,run_id,reason_code) "
                "SELECT tenant_id,project_id,environment_id,id,'WV-LEGACY-UNAVAILABLE' FROM runs WHERE id=:run"
            ),
            {"run": second},
        )
    assert (await summary(case, second)).unavailable


async def test_cache_refresh_never_calls_runtime_start_or_persist(operations_case, refresh, monkeypatch):
    from firefly_weave.runtime import kernel
    from firefly_weave.runtime.service import RuntimeService

    case = operations_case
    run = (await case.start()).id
    await record_rolled_back_decision(case, run)

    async def forbidden(*args, **kwargs):
        raise AssertionError("A cache refresh must not start, reduce or persist a run")

    for owner, name in (
        (RuntimeService, "start"),
        (RuntimeRepository, "persist"),
        (RuntimeRepository, "insert"),
        (FactRepository, "project_run"),
        (FactRepository, "project_terminal"),
        (kernel, "transition"),
    ):
        monkeypatch.setattr(owner, name, forbidden)

    async def evidence():
        async with case.owner() as session:
            return [
                await session.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": run}),
                await session.scalar(text("SELECT count(*) FROM run_events WHERE run_id=:id"), {"id": run}),
                await session.scalar(text("SELECT count(*) FROM task_intents WHERE run_id=:id"), {"id": run}),
                await session.scalar(text("SELECT count(*) FROM runtime_capacity_blocks")),
                await session.scalar(text("SELECT count(*) FROM run_policy_blocks")),
                await session.scalar(text("SELECT count(*) FROM incidents")),
            ]

    before = await evidence()
    assert await refresh.cycle() is True
    assert (await cached(case, run))["classification_state"] == "available"
    assert await evidence() == before


async def test_a_refresh_refused_for_capacity_never_blocks_the_run(operations_case, refresh, monkeypatch):
    from firefly_weave.operations import facts
    from firefly_weave.runtime.capacity import RuntimeCapacityError

    case = operations_case
    run = (await case.start()).id
    await record_rolled_back_decision(case, run)
    classify = facts.listing_metadata

    def refused(row):
        # Classification meets a capacity limit; the unit of work then refuses the whole refresh.
        RuntimeCapacityError()
        return classify(row)

    async def blocks():
        async with case.owner() as session:
            return (await session.execute(text("SELECT * FROM runtime_capacity_blocks ORDER BY run_id"))).all()

    before = await blocks()
    monkeypatch.setattr(facts, "listing_metadata", refused)
    assert await refresh.cycle() is False
    assert (await cached(case, run))["classification_state"] == "unsupported"
    # No capacity block is raised against the run, so its work goes on.
    assert await blocks() == before
    async with case.owner() as session:
        assert not await session.scalar(text("SELECT count(*) FROM runtime_capacity_blocks WHERE active"))
        assert await session.scalar(
            text("SELECT count(*) FROM task_intents WHERE run_id=:id AND status='ready'"), {"id": run}
        )


async def test_cache_refresh_rolls_back_failed_row_and_keeps_prior_progress(operations_case, refresh, payload_reads):
    case = operations_case
    origin = await cancelled_source(case)
    final = (await cached(case, origin))["node_kinds"]
    first, failing, last = ids(0x20000000, 3)
    # Only the middle row grows when refreshed; the others shrink and never need storage.
    await clone(case, origin, [first, last], node_kinds=final)
    await clone(case, origin, [failing])
    before = await cached(case, failing)
    async with case.owner.begin() as session:
        await session.execute(SATURATE)
    assert await refresh.cycle() is False
    assert await states(case, [first, failing, last]) == {first: "available", failing: "unsupported", last: "available"}
    assert await cached(case, failing) == before
    assert payload_reads == Counter({first: 1, failing: 1, last: 1})
    assert (await cursors(case))["facts"] == [(case.scope.environment_id, last)]
    async with case.owner.begin() as session:
        await session.execute(RESTORE)
    # Nothing is due after the cursor: the environment wraps, and the next cycle retries the failed row.
    assert await refresh.cycle() is True
    assert (await cursors(case))["facts"] == [(case.scope.environment_id, None)]
    assert await refresh.cycle() is True
    assert (await states(case, [failing]))[failing] == "available"
    assert payload_reads == Counter({first: 1, failing: 2, last: 1})


async def test_an_oversized_source_is_recorded_unavailable_without_being_read(operations_case, refresh, payload_reads):
    from firefly_weave.runtime import repository

    case = operations_case
    run = (await case.start()).id
    metadata = await record_rolled_back_decision(case, run)
    async with case.owner() as session:
        size = await session.scalar(text("SELECT public.weave_runs_bytes(r) FROM runs r WHERE id=:id"), {"id": run})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(repository, "RUN_FETCH_BYTES", size - 1)
        assert await refresh.cycle() is True
    assert payload_reads == Counter()
    assert await cached(case, run) == {
        **metadata,
        "classification_state": "unavailable",
        "classification_policy": admission.POLICY,
    }
    with pytest.raises(CatalogError) as withheld:
        await service(case).steps(case.actor, case.scope, run, RunStepQuery(), None, context=AuditContext())
    assert withheld.value.code == "WV-LEGACY-UNAVAILABLE"
    # A permanent decision is not due again, even once the source fits the fetch budget.
    assert await refresh.cycle() is True
    assert payload_reads == Counter() and (await cached(case, run))["classification_state"] == "unavailable"


async def test_cache_refresh_does_not_change_operational_updated_at(operations_case, refresh):
    case = operations_case
    run = (await case.start()).id
    await record_rolled_back_decision(case, run)
    columns = (
        "status,paused,test,origin,started_at,updated_at,ended_at,failed_node_id,failed_instance_key,"
        "failed_error_code,active_incidents,handled_errors,last_event_sequence,facts_started_at,business_key,"
        "correlation_key,workflow_name,workflow_version,activation_id,definition_version_id"
    )

    async def operational():
        async with case.owner() as session:
            return (
                dict(
                    (await session.execute(text(f"SELECT {columns} FROM run_facts WHERE run_id=:id"), {"id": run}))
                    .mappings()
                    .one()
                ),
                await session.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": run}),
            )

    before = await operational()
    assert await refresh.cycle() is True
    assert (await cached(case, run))["classification_state"] == "available"
    assert await operational() == before


async def due_progress(refresh, case, due, *, cycles, done) -> int:
    for turn in range(1, cycles + 1):
        await refresh.cycle()
        if await done():
            return turn
    remaining = Counter((await states(case, due)).values())
    raise AssertionError(f"Revalidation did not finish in {cycles} cycles: {remaining}")


async def test_cache_refresh_advances_across_250_due_rows_and_two_environments(
    operations_case, refresh, payload_reads, access_db, provisioned, authenticated_client
):
    case = operations_case
    origin = await cancelled_source(case)
    other, activation = await second_environment(case, access_db, provisioned)
    still_unsupported = ids(0x10000000, 120)
    due = ids(0x20000000, 250)
    elsewhere = ids(0x30000000, 40)
    # A leading batch whose pins stay unsupported precedes every due row in UUID order.
    await clone(case, origin, still_unsupported, future=True)
    await clone(case, origin, due)
    await clone(case, origin, elsewhere, environment=other.environment_id, activation=activation)
    await http_reads_leave_the_cache(case, authenticated_client, due[0])

    async def finished() -> bool:
        await http_reads_leave_the_cache(case, authenticated_client, due[0])
        return set((await states(case, due + elsewhere)).values()) == {"available"}

    turns = await due_progress(refresh, case, due + elsewhere, cycles=80, done=finished)
    # Every due row was refreshed and none was read once its decision was stored; the still-unsupported rows never.
    assert set(payload_reads) == set(due + elsewhere) and payload_reads.after_success == []
    # A row is read again only after a quantum ran out of budget while refreshing it.
    assert extra_reads(payload_reads, due + elsewhere) <= refresh.cut_short
    assert set((await states(case, still_unsupported)).values()) == {"unsupported"}
    environments = {environment for environment, _ in (await cursors(case))["facts"]}
    assert environments == {case.scope.environment_id, other.environment_id}
    settled = Counter(payload_reads)
    for _ in range(4):
        assert await refresh.cycle() is True
    # Completed rows are never read again.
    assert payload_reads == settled and payload_reads.after_success == []
    assert turns >= 4


@pytest.mark.parametrize("failure", ["timeout", "quota"])
async def test_failed_first_cache_candidate_does_not_starve_later_rows(
    operations_case, refresh, payload_reads, access_db, provisioned, authenticated_client, monkeypatch, failure
):
    case = operations_case
    origin = await cancelled_source(case)
    final = (await cached(case, origin))["node_kinds"]
    other, activation = await second_environment(case, access_db, provisioned)
    [failing] = ids(0x18000000, 1)
    later = ids(0x20000000, 250)
    elsewhere = ids(0x30000000, 20)
    await clone(case, origin, ids(0x10000000, 10), future=True)
    await clone(case, origin, [failing])
    await clone(case, origin, later, node_kinds=final)
    await clone(case, origin, elsewhere, environment=other.environment_id, activation=activation, node_kinds=final)
    attempts: list[UUID] = []
    original = FactRepository.refresh_listing_metadata

    async def attempt(self, run):
        attempts.append(run)
        return await original(self, run)

    monkeypatch.setattr(FactRepository, "refresh_listing_metadata", attempt)
    wraps = []
    selection = FactRepository.revalidation_candidates

    async def candidates(self, after):
        found = await selection(self, after)
        if not found and after is not None:
            wraps.append(self.tx.scope.environment_id)
        return found

    monkeypatch.setattr(FactRepository, "revalidation_candidates", candidates)
    lock = await case.owner.kw["bind"].connect()
    transaction = await lock.begin()
    try:
        if failure == "timeout":
            await lock.execute(text("SELECT 1 FROM runs WHERE id=:id FOR UPDATE"), {"id": failing})
        else:
            async with case.owner.begin() as session:
                await session.execute(SATURATE)

        async def finished() -> bool:
            await http_reads_leave_the_cache(case, authenticated_client, later[0])
            return (
                set((await states(case, later + elsewhere)).values()) == {"available"}
                and case.scope.environment_id in wraps
                and attempts.count(failing) >= 2
            )

        await due_progress(refresh, case, later + elsewhere, cycles=120, done=finished)
        # The failed row stays withheld and was retried after the wrap; every later row advanced once.
        assert (await states(case, [failing]))[failing] == "unsupported"
        with pytest.raises(CatalogError) as withheld:
            await service(case).steps(case.actor, case.scope, failing, RunStepQuery(), None, context=AuditContext())
        assert withheld.value.code == "WV-IR-UNSUPPORTED"
        assert all(payload_reads[run] >= 1 for run in later + elsewhere) and payload_reads.after_success == []
        assert extra_reads(payload_reads, later + elsewhere) <= refresh.cut_short
        assert attempts.index(failing) < attempts.index(later[0])
    finally:
        await transaction.rollback()
        await lock.close()
    if failure == "quota":
        async with case.owner.begin() as session:
            await session.execute(RESTORE)
    retried = attempts.count(failing)
    for _ in range(10):
        await refresh.cycle()
        if (await states(case, [failing]))[failing] == "available":
            break
    assert (await states(case, [failing]))[failing] == "available" and attempts.count(failing) > retried


async def test_environment_overrun_keeps_its_cursor_and_the_next_environment_progresses(
    operations_case, refresh, payload_reads, access_db, provisioned, monkeypatch
):
    case = operations_case
    origin = await cancelled_source(case)
    other, activation = await second_environment(case, access_db, provisioned)
    due = ids(0x20000000, 5)
    elsewhere = ids(0x30000000, 5)
    await clone(case, origin, due)
    await clone(case, origin, elsewhere, environment=other.environment_id, activation=activation)
    boundary = UUID("15000000-0000-4000-8000-000000000000")
    async with case.owner.begin() as session:
        await session.execute(
            text("INSERT INTO operations_fact_cursors VALUES(:tenant,:project,:environment,:after)"),
            {
                "tenant": case.scope.tenant_id,
                "project": case.scope.project_id,
                "environment": case.scope.environment_id,
                "after": boundary,
            },
        )
    offset = {"seconds": 0.0, "overrun": False}
    refresh.monotonic = lambda: time.monotonic() + offset["seconds"]
    selection = FactRepository.revalidation_candidates

    async def candidates(self, after):
        found = await selection(self, after)
        if self.tx.scope.environment_id == case.scope.environment_id and not offset["overrun"]:
            # The first environment's database work takes longer than its whole budget.
            offset["overrun"] = True
            offset["seconds"] += 1.5
        return found

    monkeypatch.setattr(FactRepository, "revalidation_candidates", candidates)
    results = []
    while not offset["overrun"]:
        results.append(await refresh.cycle())
        assert len(results) <= 2
    # The overrun quantum fails its cycle, reads nothing and keeps its committed cursor.
    assert results[-1] is False
    assert not any(payload_reads[run] for run in due)
    assert (case.scope.environment_id, boundary) in (await cursors(case))["facts"]
    environment_turn = [
        environment for tenant, environment in (await cursors(case))["environments"] if tenant == case.scope.tenant_id
    ]
    assert environment_turn == [case.scope.environment_id]
    # The traversal moved past the overrun environment, so the other one is next and progresses.
    results.append(await refresh.cycle())
    assert all(payload_reads[run] >= 1 for run in elsewhere)
    for _ in range(3):
        results.append(await refresh.cycle())
    assert set((await states(case, due + elsewhere)).values()) == {"available"}
    assert set(payload_reads) == set(due + elsewhere) and payload_reads.after_success == []
    assert extra_reads(payload_reads, due + elsewhere) <= refresh.cut_short


async def test_each_quantum_lowers_its_database_timeouts_to_the_remaining_budget(operations_case, refresh, monkeypatch):
    case = operations_case
    run = (await case.start()).id
    await record_rolled_back_decision(case, run)
    clock = {"now": 1000.0}
    verify = _SchedulerScope.verify

    async def slow_verify(self, tx):
        # Each authority check takes a quarter of the budget on the fake clock, which moves only here.
        await verify(self, tx)
        clock["now"] += 0.25

    monkeypatch.setattr(_SchedulerScope, "verify", slow_verify)
    refresh.monotonic = lambda: clock["now"]
    observed = []
    for name in ("revalidation_candidates", "advance_revalidation_cursor", "refresh_listing_metadata"):
        original = getattr(FactRepository, name)

        def observing(original, name):
            async def method(self, *args):
                values = [
                    await self.tx.session.scalar(text("SELECT current_setting(:name)"), {"name": setting})
                    for setting in ("statement_timeout", "lock_timeout")
                ]
                if self.tx.scope.environment_id == case.scope.environment_id:
                    observed.append((name, *values))
                return await original(self, *args)

            return method

        monkeypatch.setattr(FactRepository, name, observing(original, name))
    assert await refresh.cycle() is True
    # The quantum starts with one second; each transaction gets what is left after it verified its authority, so
    # a timeout taken before the check would read 1000, 750 and 500 ms.
    assert observed == [
        ("revalidation_candidates", "750ms", "750ms"),
        ("advance_revalidation_cursor", "500ms", "500ms"),
        ("refresh_listing_metadata", "250ms", "250ms"),
    ]
    assert (await cached(case, run))["classification_state"] == "available"


async def test_a_decision_changed_after_selection_keeps_its_newer_value(
    operations_case, refresh, payload_reads, monkeypatch
):
    case = operations_case
    run = (await case.start()).id
    await record_rolled_back_decision(case, run)
    selection = FactRepository.revalidation_candidates

    async def candidates(self, after):
        found = await selection(self, after)
        if run in found:
            # An authoritative writer withholds the run between the selection and its refresh.
            async with case.owner.begin() as session:
                await session.execute(
                    text("UPDATE run_facts SET classification_state='unavailable' WHERE run_id=:run"), {"run": run}
                )
        return found

    monkeypatch.setattr(FactRepository, "revalidation_candidates", candidates)
    assert await refresh.cycle() is True
    # The recheck under the lock sees the newer decision: nothing is read and nothing is overwritten.
    assert payload_reads == Counter()
    assert (await cached(case, run))["classification_state"] == "unavailable"


async def test_the_due_scan_uses_the_revalidation_index(operations_case, refresh, monkeypatch):
    case = operations_case
    origin = await cancelled_source(case)
    final = (await cached(case, origin))["node_kinds"]
    await clone(case, origin, ids(0x20000000, 3000), node_kinds=final, state="available")
    due = ids(0x30000000, 20)
    await clone(case, origin, due)
    async with case.owner.begin() as session:
        await session.execute(text("ANALYZE run_facts"))
    plans = []
    rows = FactRepository.rows

    async def explained(self, sql, **values):
        if "ORDER BY f.run_id LIMIT" in sql:
            plan = await self.tx.session.scalar(text("EXPLAIN (FORMAT JSON) " + sql), {**values, **self.params})
            plans.append(json.dumps(plan))
        return await rows(self, sql, **values)

    monkeypatch.setattr(FactRepository, "rows", explained)
    assert await refresh.cycle() is True
    assert plans and all('"Index Name": "run_facts_revalidation"' in plan for plan in plans)
    assert set((await states(case, due)).values()) == {"available"}


async def test_a_policy_change_never_upgrades_a_decision_the_upgrade_withheld(
    empty_settings, payload_reads, monkeypatch
):
    """Runs whose history the upgrade could not trust stay unavailable, whatever later policy re-checks the rest."""
    import secrets
    from uuid import uuid4

    from conftest import retain_resource
    from operations_support import seed_prior_head
    from sqlalchemy import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from test_incident_migration import upgrade

    owner_url = make_url(empty_settings.database_url.get_secret_value())
    owner = create_async_engine(owner_url, hide_parameters=True)
    logins = {}
    async with owner.begin() as connection:
        await upgrade(connection, "0031_operations_key_collation")
        identifiers = await seed_prior_head(connection, count=20)
        # The loop below runs the current application, which reads every table of the current schema.
        await upgrade(connection, "head")
        for role in ("weave_app", "weave_scheduler"):
            name, password = f"{role}_{uuid4().hex}", secrets.token_urlsafe(32)
            await connection.execute(text(f"CREATE ROLE {name} LOGIN PASSWORD '{password}' IN ROLE {role}"))
            logins[role] = owner_url.set(username=name, password=password)
            retain_resource("role", name)
    application = create_async_engine(logins["weave_app"], hide_parameters=True)
    withheld = [identifiers[name] for name in ("incoherent", "no_events")]

    async def facts(runs):
        async with owner.connect() as connection:
            rows = await connection.execute(
                text(f"SELECT run_id,{CLASSIFICATION} FROM run_facts WHERE run_id=ANY(:ids) ORDER BY run_id"),
                {"ids": list(runs)},
            )
            return [dict(row) for row in rows.mappings()]

    try:
        before = await facts(withheld)
        # The upgrade withheld them for their history, although each run row alone would classify as available.
        assert {row["classification_state"] for row in before} == {"unavailable"}
        async with owner.connect() as connection:
            row = dict(
                (await connection.execute(text("SELECT * FROM runs WHERE id=:id"), {"id": identifiers["incoherent"]}))
                .mappings()
                .one()
            )
        assert listing_metadata(row)["classification_state"] == "available"
        monkeypatch.setattr(admission, "POLICY", "classified-v2")
        health = LoopHealth()
        health.register("operations_loop", 60, enabled=True)
        loop = OperationsLoop(
            Settings(scheduler_enabled=False, database_url=logins["weave_app"].render_as_string(hide_password=False)),
            UnitOfWork(async_sessionmaker(application, expire_on_commit=False)),
            health,
        )
        loop.engine = create_async_engine(logins["weave_scheduler"], hide_parameters=True)
        try:
            # The seeded tenant has two environments and each cycle visits one of them.
            for _ in range(4):
                assert await loop.cycle() is True
        finally:
            await loop.engine.dispose()
        assert not any(payload_reads[run] for run in withheld) and await facts(withheld) == before
        # Decisions that are available under the earlier policy are re-checked under the new one.
        [first] = await facts([identifiers["first_run"]])
        assert payload_reads[identifiers["first_run"]] == 1
        assert (first["classification_state"], first["classification_policy"]) == ("available", "classified-v2")
    finally:
        await application.dispose()
        await owner.dispose()
