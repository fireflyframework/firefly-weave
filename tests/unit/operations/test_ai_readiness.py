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

"""Readiness uses bounded stored facts and never turns withheld evidence into health."""

import importlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, action_definition, task_capability
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.workers import WorkerInstance, WorkerRelease
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.persistence.uow import Transaction
from firefly_weave.workers.models import observed_worker

NOW = datetime(2026, 10, 9, tzinfo=UTC)
REFERENCE = "weave-agentic.generate@1.0.0"
IDS = [
    "worker_installed",
    "actions_published",
    "worker_online",
    "connection",
    "key_available",
    "connection_authorized",
    "model_responds",
    "weave_ai",
]


def modules():
    assert importlib.util.find_spec("firefly_weave.operations.ai_readiness"), "Readiness service is unavailable"
    return (
        importlib.import_module("firefly_weave.operations.ai_readiness"),
        importlib.import_module("firefly_weave.operations.ai_facts"),
    )


def revision(**changes):
    values = dict(
        id=uuid4(),
        revision=1,
        name="provider",
        connector_version_id=uuid4(),
        connector="weave-agentic-provider@1.0.0",
        connector_digest=AGENTIC_DESCRIPTOR.manifest.digest,
        adapter="weave-agentic-provider",
        config={"provider": "openai-chat", "endpoint": "https://models.example/v1"},
        secretRef={"apiKey": "private-handle"},
        allowed_destinations=("https://models.example",),
    )
    values.update(changes)
    return ConnectionRevision(**values)


def release(**changes):
    values = dict(
        id=uuid4(),
        image_digest="sha256:" + "a" * 64,
        capabilities=[task_capability()],
        credential_capabilities=[REFERENCE],
    )
    values.update(changes)
    return WorkerRelease(**values)


def good_test(**changes):
    return {"kind": "ai", "ok": True, "tested_at": (NOW - timedelta(hours=23)).isoformat(), **changes}


@pytest.mark.parametrize(
    "results,status,detail",
    [
        ([], "action", "test_required"),
        ([None], "action", "test_required"),
        ([good_test()], "done", "test_passed"),
        ([good_test(tested_at=(NOW - timedelta(hours=24)).isoformat())], "action", "test_stale"),
        ([good_test(tested_at=(NOW + timedelta(seconds=1)).isoformat())], "unknown", "record_unavailable"),
        ([good_test(), good_test(ok=False)], "action", "test_failed"),
        ([{"ok": True}], "unknown", "record_unavailable"),
        ([good_test(tested_at="bad")], "unknown", "record_unavailable"),
        ([good_test(tested_at="2026-10-08T12:00:00")], "unknown", "record_unavailable"),
        ([good_test(ok="true")], "unknown", "record_unavailable"),
    ],
)
def test_model_response_uses_dedicated_aware_recent_results(results, status, detail):
    item = modules()[0].model_response_item(results, NOW)
    assert (item.id, item.status, item.detail) == ("model_responds", status, detail)


def test_builtin_publication_uses_normalized_definition_digests():
    _, facts = modules()
    builtins = facts.builtin_ai_definitions()
    assert [(row.kind, row.reference) for row in builtins] == [
        ("Connector", "weave-agentic-provider@1.0.0"),
        ("Action", "weave-agentic-generate@1.0.0"),
    ]
    expected = [load_definition(AGENTIC_DESCRIPTOR.manifest.value), load_definition(action_definition())]
    assert [row.definition_digest for row in builtins] == [
        FrozenDocument.from_value(row.model_dump(by_alias=True)).digest for row in expected
    ]
    assert facts.required_ai_capabilities() == frozenset({REFERENCE})


@pytest.fixture
def ready(monkeypatch):
    module, facts_module = modules()
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    project = Scope(tenant_id=scope.tenant_id, project_id=scope.project_id)
    actor = Principal(
        id=uuid4(),
        kind="human",
        grants=(
            Grant(role="viewer", scope=project),
            Grant(role="developer", scope=project),
            Grant(role="tenant_admin", scope=scope),
            Grant(role="lumi_manager", scope=scope),
        ),
    )
    conn, rel = revision(), release()
    worker = WorkerInstance(id=uuid4(), release_id=rel.id, principal_id=uuid4(), task_types=[REFERENCE], capacity=1)
    state = SimpleNamespace(
        scope=scope,
        project=project,
        actor=actor,
        current=actor,
        context=AuditContext(),
        connections=[conn],
        releases=[rel],
        tests={conn.id: good_test()},
        grants={(rel.id, conn.id, REFERENCE)},
        workers=[worker.id],
        worker=worker,
        seen=NOW,
        revoked=False,
        lumi=SimpleNamespace(enabled=True),
        opened=[],
        reads=[],
        statuses=[],
        failures={},
        active=False,
        configured=True,
    )
    state.published = {row.reference: (row.definition_digest, False) for row in facts_module.builtin_ai_definitions()}
    state.session = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("Unexpected SQL")))

    @asynccontextmanager
    async def opened(given, mutation=True):
        assert given == scope and not mutation
        state.opened.append(mutation)
        state.active = True
        try:
            yield Transaction(state.session, scope)
        finally:
            state.active = False

    async def principal(session, identifier):
        assert state.active and session is state.session and identifier == actor.id
        return state.current

    monkeypatch.setattr(module, "load_principal", principal)

    class Facts:
        def __init__(self, tx):
            assert state.active and tx.session is state.session

        async def read(self, name):
            assert state.active
            state.reads.append(name)
            if name in state.failures:
                raise state.failures[name]
            return getattr(state, name)

        async def now(self):
            assert state.active
            return NOW

        async def releases(self):
            return await self.read("releases")

        async def latest_connections(self):
            return await self.read("connections")

        async def published(self):
            return await self.read("published")

        async def latest_tests(self, identifiers):
            state.test_ids = identifiers
            return await self.read("tests")

        async def granted_pairs(self, identifiers):
            state.grant_ids = identifiers
            return await self.read("grants")

        async def worker_ids(self):
            return await self.read("workers")

        async def lumi(self):
            return await self.read("lumi")

    monkeypatch.setattr(module, "AIFacts", Facts)

    async def status(repo, identifier, observed_at=None):
        assert state.active and observed_at == NOW
        state.statuses.append(identifier)
        return observed_worker(
            state.worker.model_copy(update={"revoked": state.revoked}),
            last_seen_at=state.seen,
            draining=False,
            revision=1,
            active_leases=0,
            observed_at=observed_at,
        )

    monkeypatch.setattr(module.WorkerRepository, "status", status)
    provider = SimpleNamespace(resolve=lambda handle: pytest.fail("Readiness resolved a secret"))
    secrets = ScopedSecrets({"test": provider}, (SecretGrant(scope, "private-handle", "test", "value"),))
    definitions = object.__new__(DefinitionService)
    definitions.authorization = AuthorizationService()
    definitions.uow = SimpleNamespace(open=opened)
    connections = ConnectionService(definitions.uow, definitions, ConnectorRegistry(), secrets)
    forbidden = AsyncMock(side_effect=AssertionError("Readiness is read-only"))
    gateway = SimpleNamespace(configured=True, models=forbidden, test=forbidden, ask=forbidden)
    state.service = module.AIReadinessService(connections, gateway)
    return state


async def read(state):
    return await state.service.read(state.actor, state.scope, context=state.context)


def by_id(result):
    return {row.id: row for row in result.items}


async def test_ready_result_contains_only_eight_safe_items_in_one_readonly_snapshot(ready):
    result = await read(ready)
    assert [row.id for row in result.items] == IDS
    assert [row.status for row in result.items] == ["done"] * 8
    assert ready.opened == [False] and not ready.active
    encoded = result.model_dump_json()
    for hidden in ("private-handle", str(ready.connections[0].id), str(ready.worker.id), "models.example", "23:00"):
        assert hidden not in encoded
    for row in result.items:
        if row.fix and row.fix.kind == "operation":
            from firefly_weave.contracts.surface import OPERATIONS

            assert row.fix.operation in OPERATIONS


@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_catalog_denial_fails_before_fact_payloads(ready, snapshot):
    setattr(ready, snapshot, ready.actor.model_copy(update={"grants": ()}))
    with pytest.raises(AccessDenied):
        await read(ready)
    assert ready.reads == []


@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_hidden_facts_are_unknown_and_never_read(ready, snapshot):
    setattr(ready, snapshot, ready.actor.model_copy(update={"grants": (Grant(role="developer", scope=ready.scope),)}))
    items = by_id(await read(ready))
    for identifier in [
        "actions_published",
        "worker_online",
        "connection",
        "key_available",
        "connection_authorized",
        "model_responds",
        "weave_ai",
    ]:
        assert items[identifier].status == "unknown"
        assert items[identifier].detail.startswith("requires_")
        assert items[identifier].fix.kind == "ask"
    assert not {"published", "lumi", "tests", "grants"} & set(ready.reads)
    assert ready.statuses == []


@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_revision_binding_never_becomes_scope_management_or_healthy_filtered_aggregate(ready, snapshot):
    grants = (
        Grant(role="viewer", scope=ready.project),
        Grant(role="deployer", scope=ready.scope, resources=(str(ready.connections[0].id),)),
    )
    setattr(ready, snapshot, ready.actor.model_copy(update={"grants": grants}))
    # Both principals may read this first connection, while only one sees the second.
    ready.actor = (
        ready.actor.model_copy(update={"grants": (*ready.actor.grants, Grant(role="deployer", scope=ready.scope))})
        if snapshot == "current"
        else ready.actor
    )
    ready.current = (
        ready.current.model_copy(update={"grants": (*ready.current.grants, Grant(role="deployer", scope=ready.scope))})
        if snapshot == "actor"
        else ready.current
    )
    assert by_id(await read(ready))["connection"].status == "done"
    ready.connections.append(revision(name="other"))
    assert by_id(await read(ready))["connection"].status == "unknown"
    assert by_id(await read(ready))["key_available"].status == "unknown"


@pytest.mark.parametrize(
    "seen,revoked,status",
    [
        (NOW, False, "done"),
        (NOW - timedelta(seconds=60), False, "action"),
        (None, False, "unknown"),
        (NOW, True, "action"),
    ],
)
async def test_worker_uses_actual_presence_and_revocation(ready, seen, revoked, status):
    ready.seen, ready.revoked = seen, revoked
    assert by_id(await read(ready))["worker_online"].status == status


@pytest.mark.parametrize(
    "seen,other,status,detail",
    [
        (NOW, "unknown", "done", "worker_online"),
        (NOW, "stale", "done", "worker_online"),
        (NOW, "malformed", "done", "worker_online"),
        (NOW - timedelta(minutes=5), "unknown", "unknown", "record_unavailable"),
        (NOW - timedelta(minutes=5), "stale", "action", "worker_offline"),
        (NOW - timedelta(minutes=5), "malformed", "unknown", "record_unavailable"),
    ],
)
async def test_recent_worker_establishes_online_despite_other_unavailable_presence(
    ready, monkeypatch, seen, other, status, detail
):
    second = uuid4()
    ready.workers.append(second)

    async def observed(repo, identifier, observed_at=None):
        assert ready.active and observed_at == NOW
        ready.statuses.append(identifier)
        last_seen = (
            seen if identifier == ready.worker.id else None if other == "unknown" else NOW - timedelta(minutes=5)
        )
        return observed_worker(
            ready.worker.model_copy(update={"id": identifier}),
            last_seen_at=last_seen,
            draining=False,
            revision=0 if identifier == second and other == "malformed" else 1,
            active_leases=0,
            observed_at=observed_at,
        )

    monkeypatch.setattr(modules()[0].WorkerRepository, "status", observed)
    item = by_id(await read(ready))["worker_online"]
    assert (item.status, item.detail) == (status, detail)
    assert ready.statuses == [ready.worker.id, second]
    assert str(second) not in item.model_dump_json() and str(ready.worker.id) not in item.model_dump_json()


@pytest.mark.parametrize("snapshot", ["actor", "current"])
@pytest.mark.parametrize(
    "seen,revoked,status,detail",
    [
        (NOW, False, "done", "worker_online"),
        (NOW - timedelta(minutes=5), False, "unknown", "requires_viewer"),
        (NOW, True, "unknown", "requires_viewer"),
    ],
)
async def test_recent_authorized_worker_establishes_online_without_reading_hidden_worker(
    ready, snapshot, seen, revoked, status, detail
):
    second = uuid4()
    ready.workers.append(second)
    ready.seen, ready.revoked = seen, revoked
    original = getattr(ready, snapshot)
    setattr(
        ready,
        snapshot,
        original.model_copy(
            update={
                "grants": (
                    Grant(role="developer", scope=ready.project),
                    Grant(role="viewer", scope=ready.scope, resources=(str(ready.worker.id),)),
                )
            }
        ),
    )
    item = by_id(await read(ready))["worker_online"]
    assert (item.status, item.detail) == (status, detail)
    assert ready.statuses == [ready.worker.id]
    assert str(second) not in item.model_dump_json() and str(ready.worker.id) not in item.model_dump_json()


@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_worker_resource_denial_precedes_status_payload_read(ready, snapshot):
    original = getattr(ready, snapshot)
    setattr(
        ready,
        snapshot,
        original.model_copy(
            update={
                "grants": (
                    Grant(role="developer", scope=ready.project),
                    Grant(role="viewer", scope=ready.scope, resources=(str(uuid4()),)),
                )
            }
        ),
    )
    assert by_id(await read(ready))["worker_online"].status == "unknown"
    assert ready.statuses == []


@pytest.mark.parametrize("wrong,retired,detail", [(True, False, "catalog_mismatch"), (False, True, "actions_missing")])
async def test_published_requires_definition_digest_and_nonretirement(ready, wrong, retired, detail):
    reference = "weave-agentic-generate@1.0.0"
    digest = "b" * 64 if wrong else ready.published[reference][0]
    ready.published[reference] = (digest, retired)
    item = by_id(await read(ready))["actions_published"]
    assert (item.status, item.detail) == ("action", detail)
    assert item.fix is None or item.fix.kind != "operation"


@pytest.mark.parametrize("field", ["connections", "releases"])
async def test_empty_collections_do_not_report_vacuous_health(ready, field):
    setattr(ready, field, [])
    items = by_id(await read(ready))
    assert items["connection_authorized"].status == "action"
    if field == "connections":
        for key in ["connection", "key_available", "model_responds"]:
            assert items[key].status == "action"
    else:
        assert items["connection_authorized"].detail == "worker_missing"


async def test_all_release_capability_connection_grants_are_required(ready):
    ready.releases.append(release(image_digest="sha256:" + "b" * 64))
    assert by_id(await read(ready))["connection_authorized"].detail == "authorization_missing"
    ready.grants.add((ready.releases[1].id, ready.connections[0].id, REFERENCE))
    assert by_id(await read(ready))["connection_authorized"].status == "done"
    ready.releases = [release(credential_capabilities=[])]
    assert by_id(await read(ready))["connection_authorized"].status == "action"


async def test_key_metadata_does_not_resolve_provider_value_and_missing_grant_is_action(ready):
    assert by_id(await read(ready))["key_available"].status == "done"
    ready.service.connections.secrets = ScopedSecrets()
    assert by_id(await read(ready))["key_available"].detail == "key_missing"


@pytest.mark.parametrize(
    "lumi,configured,detail",
    [
        (None, True, "weave_ai_missing"),
        (SimpleNamespace(enabled=False), True, "weave_ai_disabled"),
        (SimpleNamespace(enabled=True), False, "gateway_missing"),
    ],
)
async def test_weave_ai_keeps_distinct_stored_and_gateway_facts(ready, lumi, configured, detail):
    ready.lumi = lumi
    ready.service.gateway.configured = configured
    item = by_id(await read(ready))["weave_ai"]
    assert (item.status, item.detail) == ("action", detail)


@pytest.mark.parametrize(
    "field,identifiers",
    [
        ("releases", ["worker_installed", "worker_online", "connection_authorized"]),
        ("connections", ["connection", "key_available", "connection_authorized", "model_responds"]),
        ("tests", ["model_responds"]),
        ("workers", ["worker_online"]),
        ("lumi", ["weave_ai"]),
    ],
)
async def test_fact_limits_propagate_to_every_dependent_item(ready, field, identifiers):
    ready.failures[field] = modules()[1].FactLimit()
    items = by_id(await read(ready))
    for key in identifiers:
        assert (items[key].status, items[key].detail) == ("unknown", "limit_exceeded")
    if field in {"releases", "workers"}:
        assert ready.statuses == []


class QuerySession:
    def __init__(self, rows=(), *, count=None, size=0):
        self.rows, self.count, self.size = list(rows), count, size
        self.calls = []

    async def execute(self, statement, params):
        sql = str(statement)
        assert "INSERT" not in sql and "UPDATE" not in sql
        self.calls.append((statement, params))
        if "count(*)" in sql:
            return SimpleNamespace(one=lambda: (len(self.rows) if self.count is None else self.count, self.size))
        return SimpleNamespace(mappings=lambda: iter(self.rows))


def facts(session):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    return modules()[1].AIFacts(Transaction(session, scope))


@pytest.mark.parametrize("count,size", [(1001, 1), (1, 8 * 1024 * 1024 + 1)])
@pytest.mark.parametrize("method", ["releases", "latest_connections", "latest_tests", "granted_pairs"])
async def test_fact_bounds_precede_payload_fetch(method, count, size):
    session = QuerySession(count=count, size=size)
    reader = facts(session)
    with pytest.raises(modules()[1].FactLimit):
        await getattr(reader, method)(*((uuid4(),),) if method in {"latest_tests", "granted_pairs"} else ())
    assert len(session.calls) == 1


async def test_cumulative_payload_budget_does_not_reset_between_fact_groups():
    session = QuerySession(size=4 * 1024 * 1024 + 1)
    reader = facts(session)
    await reader.latest_connections()
    with pytest.raises(modules()[1].FactLimit):
        await reader.latest_tests((uuid4(),))
    assert len(session.calls) == 3


async def test_latest_revision_is_selected_before_filtering_ai(ready):
    old = ready.connections[0]
    current = revision(id=uuid4(), revision=2, connector="weave-http@1.0.0", name=old.name)

    class LatestSession(QuerySession):
        async def execute(self, statement, params):
            sql = str(statement)
            assert "DISTINCT ON (name)" in sql
            inner, outer = sql.split(") latest", 1)
            assert "payload->>'connector'" not in inner and "payload->>'connector'=:connector" in outer
            assert "ORDER BY name, revision DESC, id DESC" in inner
            candidates = sorted([old, current], key=lambda row: (row.revision, row.id), reverse=True)[:1]
            self.rows = [
                {"id": row.id, "payload": row.model_dump(mode="json", by_alias=True)}
                for row in candidates
                if row.connector == params["connector"]
            ]
            return await super().execute(statement, params)

    reader = facts(LatestSession())
    ready.connections = await reader.latest_connections()
    assert ready.connections == []
    items = by_id(await read(ready))
    assert items["connection"].status == items["model_responds"].status == "action"
    assert not set(getattr(ready, "test_ids", ())) | set(getattr(ready, "grant_ids", ()))


async def test_tests_use_persisted_scoped_join_uuid_arrays_and_latest_failure():
    identifier = uuid4()
    rows = [
        {"revision_id": identifier, "job_id": UUID(int=1), "payload": good_test()},
        {"revision_id": identifier, "job_id": UUID(int=2), "payload": good_test(ok=False, tested_at=NOW.isoformat())},
    ]
    session = QuerySession(rows)
    reader = facts(session)
    result = await reader.latest_tests((identifier,))
    assert result[identifier]["ok"] is False
    for statement, params in session.calls:
        sql = str(statement)
        for clause in [
            "connection_test_jobs j",
            "connection_test_results r",
            "r.job_id=j.id",
            "r.tenant_id=j.tenant_id",
            "r.project_id=j.project_id",
            "r.environment_id=j.environment_id",
            "j.tenant_id=:tenant",
            "j.project_id=:project",
            "j.environment_id=:environment",
            "j.revision_id=ANY(:revision_ids)",
            "r.payload->>'kind'='ai'",
        ]:
            assert clause in sql
        assert params["environment"] == reader.tx.scope.environment_id
        assert params["revision_ids"] == [identifier]
        assert "UUID[]" in str(statement.compile(dialect=postgresql.asyncpg.dialect()))


@pytest.mark.parametrize("timestamp", [None, "bad", "2026-10-09T00:00:00"])
async def test_malformed_history_cannot_hide_behind_older_success(timestamp):
    identifier = uuid4()
    session = QuerySession(
        [
            {"revision_id": identifier, "job_id": uuid4(), "payload": good_test()},
            {"revision_id": identifier, "job_id": uuid4(), "payload": good_test(tested_at=timestamp)},
        ]
    )
    result = await facts(session).latest_tests((identifier,))
    assert modules()[0].model_response_item([result[identifier]], NOW).status == "unknown"


async def test_equal_test_times_use_job_uuid_for_deterministic_latest():
    identifier = uuid4()
    rows = [
        {"revision_id": identifier, "job_id": UUID(int=2), "payload": good_test(ok=False)},
        {"revision_id": identifier, "job_id": UUID(int=1), "payload": good_test()},
    ]
    assert (await facts(QuerySession(rows)).latest_tests((identifier,)))[identifier]["ok"] is False


@pytest.mark.parametrize("method", ["latest_tests", "granted_pairs"])
async def test_empty_revision_sets_do_not_query_history_or_grants(method):
    session = QuerySession()
    assert not await getattr(facts(session), method)(())
    assert session.calls == []


@pytest.mark.parametrize("mutation", ["version", "digest"])
async def test_releases_must_match_the_current_exact_capability(mutation):
    capability = task_capability().model_dump(by_alias=True)
    capability["taskVersion" if mutation == "version" else "timeoutSeconds"] = "2.0.0" if mutation == "version" else 599
    row = release(capabilities=[capability], credential_capabilities=[])
    session = QuerySession([{"id": row.id, "payload": row.model_dump(mode="json", by_alias=True)}])
    assert await facts(session).releases() == []
    assert all("environment_id=:environment" in str(statement) for statement, _ in session.calls)


async def test_invalid_current_connector_digest_is_unavailable():
    row = revision(connector_digest="b" * 64)
    session = QuerySession([{"id": row.id, "payload": row.model_dump(mode="json", by_alias=True)}])
    with pytest.raises(ValueError):
        await facts(session).latest_connections()


def test_readiness_contract_rejects_wrong_order_duplicate_or_missing_items():
    module, _ = modules()
    result = importlib.import_module("firefly_weave.contracts.ai").AIReadinessResult
    rows = [dict(id=name, status="unknown", detail="record_unavailable") for name in IDS]
    assert len(result(items=rows).items) == 8
    for bad in [rows[:-1], list(reversed(rows)), [rows[0]] * 8]:
        with pytest.raises(ValidationError):
            result(items=bad)


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("release_error", [None, "unavailable", "limit"])
async def test_keyless_connections_skip_secret_checks_and_worker_grants(ready, mixed, release_error):
    from firefly_weave import private_origins

    local = revision(
        name="local",
        config={"provider": "openai-chat", "endpoint": "http://ollama:11434/v1"},
        secretRef={"apiKey": "no-credential"},
    )
    ready.connections = [*ready.connections, local] if mixed else [local]
    origins = private_origins.PrivateOrigins(
        platform=private_origins.PLATFORM,
        entries=(
            private_origins.PrivateOrigin(
                origin="http://ollama:11434", purpose="model", credentials="none", networks=("10.246.27.0/24",)
            ),
        ),
    )
    if release_error:
        ready.failures["releases"] = modules()[1].FactLimit() if release_error == "limit" else ValueError("Unavailable")
    with private_origins.installed(origins):
        items = by_id(await read(ready))
    assert items["key_available"].status == ("done" if mixed else "not_needed")
    expected = "not_needed" if not mixed else "unknown" if release_error else "done"
    assert items["connection_authorized"].status == expected
    if release_error:
        assert items["worker_installed"].status == items["worker_online"].status == "unknown"
    assert local.id not in getattr(ready, "grant_ids", ())


@pytest.mark.parametrize(
    "field,identifier",
    [
        ("connections", "connection"),
        ("lumi", "weave_ai"),
        ("published", "actions_published"),
        ("releases", "worker_installed"),
        ("workers", "worker_online"),
    ],
)
async def test_damaged_stored_facts_are_safe_unknown(ready, field, identifier):
    ready.failures[field] = ValueError("private-handle and provider configuration")
    result = await read(ready)
    assert (by_id(result)[identifier].status, by_id(result)[identifier].detail) == ("unknown", "record_unavailable")
    assert "private-handle" not in result.model_dump_json()


async def test_read_cancellation_closes_the_owned_snapshot(ready):
    import asyncio

    ready.failures["connections"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await read(ready)
    assert not ready.active and ready.opened == [False]
    assert "tests" not in ready.reads


@pytest.mark.parametrize("snapshot", ["actor", "current"])
async def test_test_fix_is_registered_and_requires_both_manage_snapshots(ready, snapshot):
    from firefly_weave.contracts.surface import OPERATIONS

    ready.tests = {}
    item = by_id(await read(ready))["model_responds"]
    assert item.fix.operation == "ai_connections.test" and item.fix.operation in OPERATIONS
    principal = getattr(ready, snapshot)
    setattr(
        ready,
        snapshot,
        principal.model_copy(
            update={"grants": tuple(grant for grant in principal.grants if grant.role != "tenant_admin")}
        ),
    )
    result = await read(ready)
    assert by_id(result)["model_responds"].status == "unknown"
    assert not any(row.fix and row.fix.kind == "operation" for row in result.items)


async def test_use_only_lumi_reader_does_not_receive_a_configuration_fix(ready):
    ready.lumi = None
    grants = tuple(grant for grant in ready.actor.grants if grant.role != "lumi_manager")
    ready.actor = ready.current = ready.actor.model_copy(
        update={"grants": (*grants, Grant(role="lumi_user", scope=ready.scope))}
    )
    item = by_id(await read(ready))["weave_ai"]
    assert item.status == "action" and item.fix is None


@pytest.mark.parametrize("count,size", [(1001, 1), (1, 8 * 1024 * 1024)])
async def test_worker_identity_query_bounds_full_status_rows_before_payload_access(count, size):
    rel = release()
    worker = uuid4()

    class Session(QuerySession):
        async def execute(self, statement, params):
            if "worker_releases" in str(statement):
                self.rows = [{"id": rel.id, "payload": rel.model_dump(mode="json", by_alias=True)}]
                self.size = 1
            else:
                assert "worker_instances w" in str(statement)
                assert "NOT revoked" in str(statement) and "release_id=ANY(:release_ids)" in str(statement)
                assert params["release_ids"] == [rel.id]
                assert "UUID[]" in str(statement.compile(dialect=postgresql.asyncpg.dialect()))
                assert "octet_length(to_jsonb(w)::text)" in str(statement)
                self.rows, self.count, self.size = [{"id": worker}], count, size
            return await super().execute(statement, params)

    session = Session()
    with pytest.raises(modules()[1].FactLimit):
        await facts(session).worker_ids()
    assert len(session.calls) == 3
    assert "count(*)" in str(session.calls[-1][0])


async def test_grants_are_scoped_unrevoked_typed_and_bounded():
    revision_id, release_id = uuid4(), uuid4()
    session = QuerySession([{"release_id": release_id, "connection_id": revision_id, "capability": REFERENCE}])
    result = await facts(session).granted_pairs((revision_id,))
    assert result == {(release_id, revision_id, REFERENCE)}
    for statement, params in session.calls:
        sql = str(statement)
        for clause in [
            "tenant_id=:tenant",
            "project_id=:project",
            "environment_id=:environment",
            "NOT revoked",
            "connection_id=ANY(:revision_ids)",
            "capability=ANY(:capabilities)",
        ]:
            assert clause in sql
        assert params["capabilities"] == [REFERENCE]
        assert "UUID[]" in str(statement.compile(dialect=postgresql.asyncpg.dialect()))


async def test_published_reads_only_builtin_versions_and_the_definition_digest():
    session = QuerySession(
        [
            {
                "kind": "Action",
                "name": "weave-agentic-generate",
                "version": "1.0.0",
                "definition_digest": "a" * 64,
                "retired": True,
            }
        ]
    )
    result = await facts(session).published()
    assert result == {"weave-agentic-generate@1.0.0": ("a" * 64, True)}
    for statement, params in session.calls:
        sql = str(statement)
        assert "v.tenant_id=:tenant" in sql and "v.project_id=:project" in sql and "environment_id" not in sql
        assert "v.definition_digest" in sql and "definition_retirements" in sql
        assert {params["name0"], params["name1"]} == {"weave-agentic-generate", "weave-agentic-provider"}
        assert params["version0"] == params["version1"] == "1.0.0"


async def test_latest_current_ai_success_does_not_use_superseded_failure(ready):
    old = ready.connections[0]
    latest = revision(name=old.name, revision=2)
    ready.connections = [latest]
    ready.tests = {old.id: good_test(ok=False), latest.id: good_test()}
    items = by_id(await read(ready))
    assert items["model_responds"].status == "done"
    assert ready.test_ids == (latest.id,)


async def test_equal_connection_revision_numbers_use_descending_uuid_tie_order():
    low, high = revision(id=UUID(int=1)), revision(id=UUID(int=2))

    class Session(QuerySession):
        async def execute(self, statement, params):
            assert "ORDER BY name, revision DESC, id DESC" in str(statement)
            selected = max((low, high), key=lambda row: (row.revision, row.id))
            self.rows = [{"id": selected.id, "payload": selected.model_dump(mode="json", by_alias=True)}]
            return await super().execute(statement, params)

    assert [row.id for row in await facts(Session()).latest_connections()] == [UUID(int=2)]


async def test_admitted_capability_digest_matches_catalog_normalization():
    from firefly_weave.compiler.catalog import CatalogSnapshot

    capability = task_capability()
    resolved = CatalogSnapshot.from_definitions([], tasks=[capability]).resolve("TaskCapability", REFERENCE)
    assert resolved.digest == FrozenDocument.from_value(capability.model_dump(by_alias=True)).digest
    rel = release()
    assert await facts(
        QuerySession([{"id": rel.id, "payload": rel.model_dump(mode="json", by_alias=True)}])
    ).releases() == [rel]


async def test_exact_count_and_byte_boundary_is_allowed():
    row = revision()
    session = QuerySession(
        [{"id": row.id, "payload": row.model_dump(mode="json", by_alias=True)}], count=1000, size=8 * 1024 * 1024
    )
    reader = facts(session)
    assert await reader.latest_connections() == [row]
    session.rows, session.count, session.size = [], 0, 0
    assert await reader.latest_tests((row.id,)) == {}


@pytest.mark.parametrize("boundary", ["empty", "unavailable", "permission"])
async def test_keyless_exception_requires_nonempty_fully_observed_managed_connections(ready, boundary):
    ready.failures["releases"] = modules()[1].FactLimit()
    if boundary == "empty":
        ready.connections = []
    elif boundary == "unavailable":
        ready.failures["connections"] = ValueError("Unavailable")
    else:
        ready.current = ready.current.model_copy(
            update={"grants": tuple(grant for grant in ready.current.grants if grant.role != "tenant_admin")}
        )
    item = by_id(await read(ready))["connection_authorized"]
    assert item.status in {"unknown", "action"}


@pytest.mark.parametrize("count,size", [(1001, 1), (1, 8 * 1024 * 1024 + 1)])
async def test_rejected_fact_group_does_not_consume_unfetched_payload_budget(count, size):
    session = QuerySession(count=count, size=size)
    reader = facts(session)
    with pytest.raises(modules()[1].FactLimit):
        await reader.releases()
    row = revision()
    session.rows = [{"id": row.id, "payload": row.model_dump(mode="json", by_alias=True)}]
    session.count, session.size = 1, 1
    assert await reader.latest_connections() == [row]
    assert len(session.calls) == 3


async def test_rejected_fact_group_does_not_reset_already_consumed_bytes():
    session = QuerySession(count=0, size=4 * 1024 * 1024)
    reader = facts(session)
    assert await reader.latest_connections() == []
    session.count, session.size = 1001, 1
    with pytest.raises(modules()[1].FactLimit):
        await reader.releases()
    session.count, session.size = 1, 4 * 1024 * 1024 + 1
    with pytest.raises(modules()[1].FactLimit):
        await reader.latest_tests((uuid4(),))
    assert len(session.calls) == 4
    session.count, session.size = 0, 4 * 1024 * 1024
    assert await reader.latest_tests((uuid4(),)) == {}
    assert len(session.calls) == 6


@pytest.mark.parametrize("payload_type", ["array", "string", "number", "boolean", "null", "object"])
async def test_worker_metadata_rejects_nonobject_payloads_before_status_expansion(payload_type):
    rel, worker = release(), uuid4()

    class Session(QuerySession):
        async def execute(self, statement, params):
            if "worker_releases" in str(statement):
                self.rows = [{"id": rel.id, "payload": rel.model_dump(mode="json", by_alias=True)}]
            else:
                self.rows = [{"id": worker, "logical_bytes": 100, "payload_type": payload_type}]
            return await super().execute(statement, params)

    session = Session()
    reader = facts(session)
    if payload_type == "object":
        assert await reader.worker_ids() == [worker]
        assert "jsonb_typeof(payload)" in str(session.calls[-1][0])
    else:
        with pytest.raises(ValueError):
            await reader.worker_ids()
