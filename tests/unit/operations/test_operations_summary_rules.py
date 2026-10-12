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

"""Service-free summary rules: section isolation, attention order, health components and safe telemetry answers."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.compatibility import CompatibilityFinding, CompatibilityReport
from firefly_weave.contracts.operations_summary import UnavailableSection
from firefly_weave.contracts.telemetry import TelemetryOptions
from firefly_weave.observability import TelemetryDrops
from firefly_weave.operations import summary
from firefly_weave.operations.loop_health import LoopSnapshot
from firefly_weave.operations.summary import (
    Candidate,
    OperationsSummaryService,
    attention_order,
    health_components,
    native_state,
    read_section,
    telemetry_host,
    telemetry_state,
)
from firefly_weave.settings import Settings

DATABASE = "postgresql+asyncpg://weave_app:unused@127.0.0.1:65432/weave"
AT = datetime(2026, 10, 12, 12, tzinfo=UTC)
SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
PROJECT = Scope(tenant_id=SCOPE.tenant_id, project_id=SCOPE.project_id)
CANARY = "canary-credential-7f3a"


class Failure(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def failure(sqlstate: str, *, invalidated: bool = False) -> DBAPIError:
    return DBAPIError("SELECT", {}, Failure(sqlstate), connection_invalidated=invalidated)


class Session:
    """Records statements and savepoints; the section timeout in force is what the last set_config chose.

    Each other statement takes `cost` seconds of the fake monotonic clock and records the timeout it ran under.
    """

    def __init__(self, prior: str = "0", prior_ms: int = 0, cost: float = 0.0) -> None:
        self.prior, self.prior_ms, self.cost = prior, prior_ms, cost
        self.timeout = prior
        self.savepoints: list[str] = []
        self.elapsed = 100.0
        self.ran_under: list[str] = []

    def monotonic(self) -> float:
        return self.elapsed

    async def execute(self, statement, values=None):
        sql = str(statement)
        if "pg_settings" in sql:
            return SimpleNamespace(one=lambda: SimpleNamespace(prior=self.prior, prior_ms=self.prior_ms))
        if "set_config" in sql:
            self.timeout = values["value"]
            return None
        self.ran_under.append(self.timeout)
        self.elapsed += self.cost
        return None

    @asynccontextmanager
    async def begin_nested(self):
        saved = self.timeout
        try:
            yield
        except BaseException:
            # Rolling back to the savepoint restores the setting that was in force before it.
            self.timeout = saved
            self.savepoints.append("rolled back")
            raise
        self.savepoints.append("released")


def transaction(session: Session) -> SimpleNamespace:
    return SimpleNamespace(session=session, scope=SCOPE)


def test_only_the_collector_host_is_kept():
    assert telemetry_host(None) is None
    assert telemetry_host("https://collector.example:4318/v1/traces") == "collector.example"
    assert (
        telemetry_host(f"https://user:{CANARY}@Collector.Example:4318/{CANARY}?token={CANARY}") == "collector.example"
    )
    assert telemetry_host("http://[::1]:4318/v1/metrics") == "::1"


async def section(session: Session, read):
    return await read_section(transaction(session), read, monotonic=session.monotonic)


async def test_a_section_runs_under_two_seconds_and_restores_the_timeout_in_force():
    session = Session(prior="30s", prior_ms=30000)
    seen = []

    async def read(budget):
        seen.append(session.timeout)
        await budget.execute(text("SELECT 1"))
        return 7

    assert await section(session, read) == 7
    assert (seen, session.ran_under, session.timeout, session.savepoints) == (
        ["2000ms"],
        ["2000ms"],
        "30s",
        ["released"],
    )
    # A stricter timeout already in force is kept, and no timeout at all becomes the section budget.
    for prior, prior_ms, expected in (("500ms", 500, "500ms"), ("0", 0, "2000ms")):
        session = Session(prior=prior, prior_ms=prior_ms)
        seen.clear()
        assert await section(session, read) == 7
        assert seen == [expected] == session.ran_under and session.timeout == prior


async def test_the_statements_of_a_section_share_one_budget():
    # Each statement takes 0.75 s: the next one gets what is left, and the fourth finds nothing left.
    session = Session(prior="30s", prior_ms=30000, cost=0.75)

    async def read(budget):
        for _ in range(4):
            await budget.execute(text("SELECT 1"))
        return 7

    assert await section(session, read) == UnavailableSection(state="unavailable")
    assert session.ran_under == ["2000ms", "1250ms", "500ms"]
    assert (session.timeout, session.savepoints) == ("30s", ["rolled back"])
    # Three statements fit: the section is served and the earlier timeout comes back.
    session = Session(prior="30s", prior_ms=30000, cost=0.5)

    async def three(budget):
        for _ in range(3):
            await budget.execute(text("SELECT 1"))
        return 3

    assert await section(session, three) == 3
    assert session.ran_under == ["2000ms", "1500ms", "1000ms"]
    assert (session.timeout, session.savepoints) == ("30s", ["released"])
    # A stricter timeout in force is the whole section's budget.
    session = Session(prior="500ms", prior_ms=500, cost=0.25)
    assert await section(session, three) == UnavailableSection(state="unavailable")
    assert session.ran_under == ["500ms", "250ms"]


@pytest.mark.parametrize("sqlstate", ["57014", "55P03", "40001", "40P01", "53200"])
async def test_a_timed_out_or_transient_section_is_unavailable_and_rolled_back(sqlstate):
    session = Session(prior="30s", prior_ms=30000)

    async def read(budget):
        raise failure(sqlstate)

    assert await section(session, read) == UnavailableSection(state="unavailable")
    assert session.savepoints == ["rolled back"] and session.timeout == "30s"


@pytest.mark.parametrize(
    "error",
    [
        failure("57014", invalidated=True),
        failure("08006"),
        failure("42601"),
        failure("42501"),
        AccessDenied(),
        asyncio.CancelledError(),
        RuntimeError("programmer error"),
    ],
)
async def test_a_lost_connection_cancellation_denial_or_programming_error_is_never_unavailable(error):
    session = Session()

    async def read(budget):
        raise error

    with pytest.raises(type(error)):
        await section(session, read)
    assert session.savepoints == ["rolled back"]


def item(kind, identifier, at=None, **values) -> Candidate:
    return Candidate(kind, identifier, f"{kind} {identifier}", at, **values)


def test_attention_is_ranked_by_priority_then_time_then_id_and_capped_at_twenty():
    first, second, third = UUID(int=1), UUID(int=2), UUID(int=3)
    candidates = [
        item("run", first, AT),
        item("compatibility", "compatibility"),
        item("runner", second, AT),
        item("incident", third, None),
        item("incident", second, AT + timedelta(minutes=1)),
        item("incident", first, AT + timedelta(minutes=1)),
        item("worker", first, AT),
        item("approval", second, AT + timedelta(minutes=2), expires_at=AT + timedelta(minutes=2)),
        item("approval", third, AT + timedelta(minutes=1), expires_at=AT + timedelta(minutes=1)),
        item("job", third, AT),
    ]
    ranked = [(entry.kind, entry.id) for entry in attention_order(reversed(candidates))]
    assert ranked == [
        ("job", third),
        # Approvals that expire first come first.
        ("approval", third),
        ("approval", second),
        ("worker", first),
        # Equal times are ordered by ID; unknown times come last.
        ("incident", first),
        ("incident", second),
        ("incident", third),
        ("runner", second),
        ("compatibility", "compatibility"),
        ("run", first),
    ]
    assert attention_order(candidates)[1].expires_at == AT + timedelta(minutes=1)
    many = [item("run", UUID(int=index), AT) for index in range(30, 0, -1)] + [item("job", UUID(int=99), AT)]
    ranked_many = attention_order(many)
    assert len(ranked_many) == 20 and ranked_many[0].kind == "job"
    assert [entry.id for entry in ranked_many[1:]] == [UUID(int=index) for index in range(1, 20)]


def test_long_titles_are_cut_to_the_contract_bound():
    [entry] = attention_order([Candidate("run", UUID(int=1), "A run of " + "w" * 300 + " failed", AT)])
    assert len(entry.title) == 200 and entry.title.endswith("…")


def snapshot(name, state="ok", **values) -> LoopSnapshot:
    return LoopSnapshot(
        component=name,
        state=state,
        last_started_at=values.get("started"),
        last_ok_at=values.get("ok"),
        consecutive_failures=values.get("failures", 0),
        complete_until=None,
        recent_failures=values.get("recent", 0),
    )


def test_health_lists_the_database_each_loop_native_executors_and_telemetry_without_messages():
    components = health_components(
        4.25,
        (
            snapshot("outbox_loop", "degraded", started=AT, ok=AT, failures=1, recent=3),
            snapshot("alert_evaluator", "disabled"),
        ),
        native="down",
        telemetry="disabled",
    )
    assert [component.model_dump(mode="json") for component in components] == [
        {"component": "database", "state": "ok", "latency_ms": 4.25},
        {
            "component": "outbox_loop",
            "state": "degraded",
            "last_started_at": "2026-10-12T12:00:00Z",
            "last_ok_at": "2026-10-12T12:00:00Z",
            "consecutive_failures": 1,
            "recent_failures": 3,
        },
        {"component": "alert_evaluator", "state": "disabled", "consecutive_failures": 0, "recent_failures": 0},
        {"component": "native_executors", "state": "down"},
        {"component": "telemetry", "state": "disabled"},
    ]
    # A database probe that timed out is degraded, never a zero latency.
    [database, *_] = health_components(UnavailableSection(state="unavailable"), (), native="ok", telemetry="ok")
    assert database.model_dump(mode="json") == {"component": "database", "state": "degraded"}


def test_telemetry_health_follows_recent_drops_never_the_configuration_alone():
    elapsed = [1000.0]
    drops = TelemetryDrops(monotonic=lambda: elapsed[0])
    exporting = TelemetryOptions(enabled=True, metrics_endpoint="https://collector.example/v1/metrics")
    assert telemetry_state(TelemetryOptions(), drops) == "disabled"
    assert telemetry_state(exporting, drops) == "ok"
    # A failed export makes it degraded for three metric intervals (90 s by default), then it is ok again.
    drops.add(2)
    assert telemetry_state(exporting, drops) == "degraded"
    elapsed[0] += 90
    assert telemetry_state(exporting, drops) == "degraded"
    elapsed[0] += 0.5
    assert telemetry_state(exporting, drops) == "ok"
    slower = exporting.model_copy(update={"metrics_interval_seconds": 60.0})
    drops.add(1)
    elapsed[0] += 179
    assert telemetry_state(slower, drops) == "degraded"
    elapsed[0] += 2
    assert telemetry_state(slower, drops) == "ok"
    # Drops while export is off do not make it a health claim.
    drops.add(1)
    assert telemetry_state(TelemetryOptions(), drops) == "disabled"


def test_native_executors_follow_their_existing_readiness():
    configured = SimpleNamespace(native_executors=(object(),))
    assert native_state(SimpleNamespace(native_executors=()), None) == "disabled"
    assert native_state(configured, None) == "down"
    assert native_state(configured, SimpleNamespace(healthy=lambda: True)) == "ok"
    assert native_state(configured, SimpleNamespace(healthy=lambda: False)) == "degraded"


class Units:
    """A unit of work that only counts how often it opened; the stored principal comes from `stored`."""

    def __init__(self, stored: Principal) -> None:
        self.stored, self.opened = stored, 0

    @asynccontextmanager
    async def open(self, scope, *, mutation=True):
        assert mutation is False
        self.opened += 1
        yield SimpleNamespace(session=self, scope=scope)


def principal(*grants: Grant, identifier: UUID | None = None) -> Principal:
    return Principal(id=identifier or uuid4(), kind="human", grants=grants)


def service(monkeypatch, units: Units, *, telemetry: TelemetryOptions | None = None) -> OperationsSummaryService:
    async def load_principal(session, identifier):
        assert session is units
        return units.stored

    monkeypatch.setattr(summary, "load_principal", load_principal)
    settings = Settings(database_url=DATABASE).model_copy(update={"telemetry": telemetry or TelemetryOptions()})
    drops = TelemetryDrops()
    drops.add(5)
    return OperationsSummaryService(
        units,  # type: ignore[arg-type]
        AuthorizationService(),
        settings,
        SimpleNamespace(snapshots=lambda: ()),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        drops=drops,
        instance=uuid4(),
        started_monotonic=0.0,
    )


async def test_telemetry_never_answers_credentials_headers_ports_paths_or_queries(monkeypatch):
    reader = principal(Grant(role="viewer", scope=PROJECT))
    options = TelemetryOptions.model_construct(
        enabled=True,
        # A value planted around validation still yields only the host.
        traces_endpoint=f"https://exporter:{CANARY}@traces.example:4318/v1/{CANARY}?key={CANARY}",
        metrics_endpoint="https://metrics.example/v1/metrics",
        authorization=SecretStr(f"Bearer {CANARY}"),
        timeout_seconds=5.0,
        metrics_interval_seconds=15.0,
    )
    units = Units(reader)
    answer = await service(monkeypatch, units, telemetry=options).telemetry(reader, PROJECT, context=AuditContext())
    body = json.dumps(answer.model_dump(mode="json"))
    assert json.loads(body) == {
        "enabled": True,
        "traces": {"configured": True, "host": "traces.example"},
        "metrics": {"configured": True, "host": "metrics.example", "interval_seconds": 15.0},
        "dropped": 5,
    }
    assert CANARY not in body and "4318" not in body and "/v1" not in body and units.opened == 1
    off = await service(monkeypatch, Units(reader)).telemetry(reader, PROJECT, context=AuditContext())
    assert off.model_dump(mode="json") == {
        "enabled": False,
        "traces": {"configured": False, "host": None},
        "metrics": {"configured": False, "host": None, "interval_seconds": None},
        "dropped": 5,
    }


async def test_both_reads_check_the_token_before_opening_and_the_stored_principal_inside(monkeypatch):
    viewer = Grant(role="viewer", scope=PROJECT)
    outsider = principal(Grant(role="developer", scope=PROJECT))
    for read, scope in ((OperationsSummaryService.telemetry, PROJECT), (OperationsSummaryService.read, SCOPE)):
        units = Units(principal(viewer))
        with pytest.raises(AccessDenied):
            await read(service(monkeypatch, units), outsider, scope, context=AuditContext())
        assert units.opened == 0
        token = principal(viewer)
        units = Units(principal(Grant(role="developer", scope=PROJECT), identifier=token.id))
        with pytest.raises(AccessDenied):
            await read(service(monkeypatch, units), token, scope, context=AuditContext())
        assert units.opened == 1
    # The summary is per environment and the telemetry settings per project.
    reader = principal(viewer)
    with pytest.raises(AccessDenied):
        await service(monkeypatch, Units(reader)).read(reader, PROJECT, context=AuditContext())
    with pytest.raises(AccessDenied):
        await service(monkeypatch, Units(reader)).telemetry(
            reader, Scope(tenant_id=PROJECT.tenant_id), context=AuditContext()
        )


def test_compatibility_findings_are_counted_for_this_project_only(monkeypatch):
    other_project = Scope(tenant_id=SCOPE.tenant_id, project_id=uuid4())
    other_tenant = Scope(tenant_id=uuid4(), project_id=uuid4())
    sibling = Scope(tenant_id=SCOPE.tenant_id, project_id=SCOPE.project_id, environment_id=uuid4())
    report = CompatibilityReport(
        mode="restricted",
        findings=[
            CompatibilityFinding(kind="inventory", code="inventory_incomplete"),
            CompatibilityFinding(kind="run", code="ir_unsupported", scope=SCOPE),
            CompatibilityFinding(kind="activation", code="policy_mismatch", scope=sibling),
            CompatibilityFinding(kind="run", code="ir_unsupported", scope=other_project),
            CompatibilityFinding(kind="worker", code="worker_protocol_unsupported", scope=other_tenant),
        ],
    )
    reader = service(monkeypatch, Units(principal()))
    reader.compatibility = SimpleNamespace(report=report, ready=False)  # type: ignore[assignment]
    # The platform-wide finding and this project's own two, never another project's or tenant's.
    assert reader._api(SCOPE).compatibility.model_dump() == {"mode": "restricted", "findings": 3}


def test_section_probes_never_write_deny_records(monkeypatch, caplog):
    viewer = principal(Grant(role="viewer", scope=PROJECT))
    reader = service(monkeypatch, Units(viewer))
    caplog.set_level(logging.INFO, logger="weave.authorization")
    reach = reader._reach(viewer, viewer, SCOPE, AuditContext())
    assert (reach.incidents, reach.runs, reach.targets, reach.approvals) == (False, True, (), ())
    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "weave.authorization"]
    # Reading runs is recorded as allowed for both principals; the capabilities the viewer lacks are not attempts.
    assert [(record["capability"], record["outcome"]) for record in records] == [
        ("run.read", "allow"),
        ("run.read", "allow"),
    ]
