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


"""Run statistics reads: points, coverage, rates, groupings, sources and authorization, without a database."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_statistics import StatisticsSeriesQuery
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations import statistics_reads
from firefly_weave.operations.statistics_histogram import histogram
from firefly_weave.operations.statistics_reads import (
    Outcomes,
    RunStatisticsService,
    _Cursor,
    points,
    rates,
    run_value,
    sources,
    top,
    utilization,
)

AT = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def outcomes(**counts) -> Outcomes:
    return Outcomes().add(counts)


def test_points_cover_whole_buckets_up_to_to():
    assert points(AT, AT + timedelta(minutes=12), "5m") == [AT, AT + timedelta(minutes=5), AT + timedelta(minutes=10)]
    assert points(AT, AT, "1h") == []
    assert len(points(AT, AT + timedelta(days=30), "1d")) == 30


def test_counts_are_zero_inside_covered_time_and_null_outside():
    assert run_value("runs_started", None, True) == 0
    assert run_value("runs_started", None, False) is None
    assert run_value("failure_rate", None, True) is None
    assert run_value("duration_p95", None, True) is None
    assert run_value("runs_started", outcomes(started=3), False) == 3


def test_rates_are_fractions_or_null_when_nothing_ended():
    assert rates(outcomes(started=4)) == (None, None)
    assert rates(outcomes(succeeded=3, failed=1)) == (0.25, 0.75)
    with pytest.raises(ValueError):
        run_value("tasks_ready", outcomes(), True)


def test_failed_counts_timed_out_runs_too():
    done = outcomes(succeeded=17, failed=1, timed_out=2, cancelled=0)
    assert run_value("runs_failed", done, True) == 3
    assert run_value("failure_rate", done, True) == pytest.approx(0.15)
    assert done.ended == 20


def test_top_workflows_fold_the_rest_into_other_with_merged_percentiles():
    assert top({"a": 5, "b": 9, "c": 9, "(other)": 50}, 2) == ["b", "c"]
    merged = Outcomes()
    for row in (
        {"succeeded": 1, "duration_hist": histogram([1_000])},
        {"succeeded": 1, "duration_hist": histogram([1_000_000])},
    ):
        merged.add(row)
    assert merged.ended == 2 and sum(merged.hist) == 2
    assert run_value("duration_p95", merged, True) > run_value("duration_p50", merged, True)


def test_utilization_is_null_without_capacity():
    assert utilization(3, 0) is None and utilization(None, 4) is None
    assert utilization(1, 4) == 0.25 and utilization(9, 4) == 1.0


def test_status_grouping_labels_each_outcome():
    query = StatisticsSeriesQuery.model_validate(
        {
            "from": "2026-10-26T12:00:00Z",
            "to": "2026-10-26T13:00:00Z",
            "signals": ["runs_succeeded", "runs_failed"],
            "group_by": "status",
        }
    )
    assert statistics_reads._STATUS_LABEL["runs_failed"] == "failed"
    assert query.series_count == 2


def test_hourly_and_daily_reads_join_closed_and_open_periods():
    cursor = _Cursor(hours_until=AT + timedelta(hours=3), days_until=AT - timedelta(hours=12))
    hi = AT + timedelta(hours=5)
    assert sources("5m", AT, hi, cursor) == [("5m", AT, hi)]
    assert sources("1h", AT, hi, cursor) == [("1h", AT, AT + timedelta(hours=3)), ("5m", AT + timedelta(hours=3), hi)]
    day = AT - timedelta(hours=12) - timedelta(days=2)
    assert sources("1d", day, hi, cursor) == [
        ("1d", day, AT - timedelta(hours=12)),
        ("1h", AT - timedelta(hours=12), AT + timedelta(hours=3)),
        ("5m", AT + timedelta(hours=3), hi),
    ]
    # Before any period closed, everything comes from five-minute rows.
    assert sources("1d", day, hi, _Cursor()) == [("5m", day, hi)]


class Authorization:
    def __init__(self, token: bool, stored: bool) -> None:
        self.allowed = {"token": token, "stored": stored}

    def require(self, actor, scope, capability, *, context):
        assert capability == "status.read"
        if not self.allowed[actor]:
            raise AccessDenied()


class Uow:
    def __init__(self) -> None:
        self.opened = 0

    @asynccontextmanager
    async def open(self, scope, *, mutation=True):
        self.opened += 1
        assert mutation is False
        yield SimpleNamespace(session=None)


async def test_every_read_needs_status_read_on_the_token_and_the_current_principal(monkeypatch):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=UUID(int=4))

    async def stored(session, actor_id):
        return "stored"

    monkeypatch.setattr(statistics_reads, "load_principal", stored)
    query = StatisticsSeriesQuery.model_validate(
        {"from": "2026-10-26T12:00:00Z", "to": "2026-10-26T13:00:00Z", "signals": ["runs_started"]}
    )
    actor = type("Actor", (), {"id": uuid4()})()
    # Missing on the token: refused before any transaction opens.
    uow = Uow()
    service = RunStatisticsService(uow, Authorization(token=False, stored=True))  # type: ignore[arg-type]
    monkeypatch.setattr(service.authorization, "require", _as(service.authorization.require, "token"))
    with pytest.raises(AccessDenied):
        await service.series(actor, scope, query, context=AuditContext())
    assert uow.opened == 0
    # Missing on the stored principal: refused inside the transaction.
    uow = Uow()
    service = RunStatisticsService(uow, Authorization(token=True, stored=False))  # type: ignore[arg-type]
    monkeypatch.setattr(service.authorization, "require", _as(service.authorization.require, "token"))
    with pytest.raises(AccessDenied):
        await service.workflows(actor, scope, query, context=AuditContext())  # type: ignore[arg-type]
    assert uow.opened == 1
    # An environment-less scope is refused.
    with pytest.raises(CatalogError):
        await service.steps(
            actor,
            Scope(tenant_id=scope.tenant_id, project_id=scope.project_id),
            query,  # type: ignore[arg-type]
            context=AuditContext(),
        )


def _as(require, token_name):
    """The token principal is the request's actor; the stored one is what load_principal returns."""

    def checked(actor, scope, capability, *, context):
        return require(actor if actor == "stored" else token_name, scope, capability, context=context)

    return checked


def _bucketed(at, entries):
    """Samples of one bucket from (metric, label, value, samples) rows, as the read gathers them."""
    found = statistics_reads._Samples()
    for metric, label, value, samples in entries:
        slot = found.by_bucket[at].setdefault((metric, label), statistics_reads._Sample())
        slot.weighted += value * samples
        slot.samples += samples
        slot.largest = max(slot.largest, value)
    return found


def test_ready_tasks_average_over_every_sampled_minute_not_only_those_with_tasks():
    # Five minutes sampled; type a had 10 ready tasks in one minute, type b 10 in another, none otherwise.
    found = _bucketed(
        AT,
        [("workers_online", "", 1, 5), ("tasks_ready", "a", 10, 1), ("tasks_ready", "b", 10, 1)],
    )
    query = StatisticsSeriesQuery.model_validate(
        {"from": "2026-10-26T12:00:00Z", "to": "2026-10-26T12:05:00Z", "signals": ["tasks_ready"]}
    )
    (ungrouped,) = statistics_reads._sample_series(query, ["tasks_ready"], [AT], found)
    assert ungrouped.points == [(AT, 4.0)]
    grouped = statistics_reads._sample_series(
        query.model_copy(update={"group_by": "task_type", "top": 4}), ["tasks_ready"], [AT], found
    )
    assert {item.labels["task_type"]: item.points[0][1] for item in grouped} == {"a": 2.0, "b": 2.0}


def test_the_oldest_ready_age_is_the_largest_over_task_types_not_their_sum():
    found = _bucketed(
        AT,
        [("workers_online", "", 1, 5), ("oldest_ready_seconds", "a", 30, 1), ("oldest_ready_seconds", "b", 90, 2)],
    )
    query = StatisticsSeriesQuery.model_validate(
        {"from": "2026-10-26T12:00:00Z", "to": "2026-10-26T12:05:00Z", "signals": ["oldest_ready_seconds"]}
    )
    (series,) = statistics_reads._sample_series(query, ["oldest_ready_seconds"], [AT], found)
    assert series.points == [(AT, 90.0)]


async def test_a_read_that_runs_out_of_time_answers_unavailable_not_an_internal_error(monkeypatch):
    from sqlalchemy.exc import DBAPIError

    class Cancelled(Exception):
        sqlstate = "57014"

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=UUID(int=4))

    async def stored(session, actor_id):
        return "stored"

    async def slow(tx):
        raise DBAPIError("SELECT 1", None, Cancelled("canceling statement due to statement timeout"))

    class Session:
        async def execute(self, statement, values=None):
            return None

    class Unit:
        @asynccontextmanager
        async def open(self, scope, *, mutation=True):
            yield SimpleNamespace(session=Session())

    monkeypatch.setattr(statistics_reads, "load_principal", stored)
    service = RunStatisticsService(Unit(), Authorization(token=True, stored=True))  # type: ignore[arg-type]
    monkeypatch.setattr(service.authorization, "require", _as(service.authorization.require, "token"))
    actor = type("Actor", (), {"id": uuid4()})()
    with pytest.raises(CatalogError) as caught:
        await service._read(actor, scope, AuditContext(), slow)
    assert (caught.value.status, caught.value.code) == (503, "WV-UNAVAILABLE")
    # Two seconds of database time per read.
    assert statistics_reads.READ_TIMEOUT == "2000ms"


def test_buckets_past_the_rollups_edge_are_unknown_not_zero():
    cursor = _Cursor(covered_from=AT - timedelta(hours=1), rolled_until=AT + timedelta(minutes=10))
    assert statistics_reads._covered(cursor, AT, "5m") is True
    assert statistics_reads._covered(cursor, AT + timedelta(minutes=10), "5m") is False
    assert statistics_reads._covered(cursor, AT - timedelta(hours=2), "5m") is False
    # An hour that only partly overlaps coverage is covered.
    assert statistics_reads._covered(cursor, AT - timedelta(minutes=90), "1h") is True


def test_task_types_rank_by_their_average_over_every_sampled_minute():
    # Five sampled minutes: a had 10 ready tasks in one minute (2 on average), b had 3 in every minute (3).
    found = _bucketed(
        AT,
        [("workers_online", "", 1, 5), ("tasks_ready", "a", 10, 1), ("tasks_ready", "b", 3, 5)],
    )
    query = StatisticsSeriesQuery.model_validate(
        {
            "from": "2026-10-26T12:00:00Z",
            "to": "2026-10-26T12:05:00Z",
            "signals": ["tasks_ready"],
            "group_by": "task_type",
            "top": 1,
        }
    )
    grouped = statistics_reads._sample_series(query, ["tasks_ready"], [AT], found)
    assert [item.labels["task_type"] for item in grouped] == ["b", "(other)"]


def test_buckets_the_rollup_missed_read_as_unknown():
    cursor = _Cursor(
        covered_from=AT - timedelta(days=30),
        rolled_until=AT + timedelta(minutes=10),
        gap_starts=[AT - timedelta(days=9)],
        gap_ends=[AT - timedelta(days=2)],
    )
    assert statistics_reads._covered(cursor, AT - timedelta(days=5), "1d") is False
    assert statistics_reads._covered(cursor, AT - timedelta(days=9, hours=1), "1d") is False
    assert statistics_reads._covered(cursor, AT - timedelta(days=12), "1d") is True
    assert statistics_reads._covered(cursor, AT - timedelta(days=1), "1h") is True
