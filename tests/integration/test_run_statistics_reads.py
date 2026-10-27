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


"""Run statistics reads on real PostgreSQL: exact figures from rolled-up facts, joined periods, isolation, routes."""

import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_operations_statistics import B0, NOW, ms, owner, place, rollup, started_runs, step

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_statistics import (
    StatisticsSeriesQuery,
    StatisticsStepsQuery,
    StatisticsWorkflowsQuery,
)
from firefly_weave.operations import statistics_reads
from firefly_weave.operations.statistics_histogram import histogram
from firefly_weave.persistence.uow import UnitOfWork

pytestmark = pytest.mark.integration

RANGE = {"from": "2031-03-04T11:00:00Z", "to": "2031-03-04T12:07:30Z"}


def series_query(**values) -> StatisticsSeriesQuery:
    return StatisticsSeriesQuery.model_validate({**RANGE, **values})


async def read(case, function, query):
    async with case.tx() as tx:
        return await function(tx, query)


def at(answer, signal: str, moment: datetime = B0, **labels):
    (found,) = [item for item in answer.series if item.signal == signal and item.labels == labels]
    return dict(found.points)[moment]


async def seeded(case, *, failed: int = 3, timed_out: int = 2, total: int = 25) -> list[float]:
    runs = await started_runs(case, total)
    durations = []
    for index, run in enumerate(runs):
        duration = 100 + 37 * index
        durations.append(float(duration))
        status = "failed" if index < failed else "timed_out" if index < failed + timed_out else "succeeded"
        await place(case, run, started=B0 + ms(5), ended=B0 + ms(5 + duration), status=status)
        await step(case, run, "send", ended=B0 + ms(duration), duration=ms(duration - 5))
    await rollup(case)
    return durations


async def test_the_series_of_seeded_runs_matches_their_facts(operations_case):
    durations = await seeded(operations_case)
    answer = await read(
        operations_case,
        statistics_reads.series,
        series_query(signals=["runs_started", "runs_succeeded", "runs_failed", "failure_rate", "duration_p95"]),
    )
    assert answer.granularity == "5m" and answer.complete_until == B0
    assert (at(answer, "runs_started"), at(answer, "runs_succeeded"), at(answer, "runs_failed")) == (25, 20, 5)
    assert at(answer, "failure_rate") == pytest.approx(0.2)
    truth = sorted(durations)[math.ceil(0.95 * 25) - 1]
    assert truth / math.sqrt(2) <= at(answer, "duration_p95") <= truth * math.sqrt(2)
    # Covered buckets without runs count zero; rates there are unknown.
    earlier = B0 - timedelta(minutes=5)
    assert at(answer, "runs_started", earlier) == 0 and at(answer, "failure_rate", earlier) is None
    # Buckets the rollup has not reached yet are unknown.
    assert at(answer, "runs_started", datetime(2031, 3, 4, 11, 0, tzinfo=UTC)) is None
    table = await read(operations_case, statistics_reads.workflows, StatisticsWorkflowsQuery.model_validate(RANGE))
    (row,) = table.workflows
    assert (row.workflow, row.versions, row.runs, row.failed, row.failure_rate) == ("orders", ["1.0.0"], 25, 5, 0.2)
    assert table.totals is not None and table.totals.runs == 25 and table.totals.duration_p95_ms == row.duration_p95_ms
    steps = await read(
        operations_case,
        statistics_reads.steps,
        StatisticsStepsQuery.model_validate({**RANGE, "workflow": "orders"}),
    )
    (send,) = steps.steps
    assert (send.node_id, send.kind, send.executions, send.failures) == ("send", "action", 25, 0)
    assert send.queue_p95_ms is not None


async def test_grouping_by_status_and_by_workflow_label_each_series(operations_case):
    await seeded(operations_case)
    by_status = await read(
        operations_case,
        statistics_reads.series,
        series_query(signals=["runs_succeeded", "runs_failed"], group_by="status"),
    )
    assert at(by_status, "runs_failed", status="failed") == 5
    assert at(by_status, "runs_succeeded", status="succeeded") == 20
    by_workflow = await read(
        operations_case,
        statistics_reads.series,
        series_query(signals=["runs_started"], group_by="workflow", top=1),
    )
    assert [item.labels for item in by_workflow.series] == [{"workflow": "orders"}]


async def test_the_last_day_equals_the_runs_of_the_same_window(operations_case):
    await seeded(operations_case)
    trial = (await started_runs(operations_case, 1))[0]
    await place(operations_case, trial, started=B0, ended=B0 + ms(10), status="failed", test=True)
    await rollup(operations_case)
    async with operations_case.tx() as tx:
        day = await statistics_reads.last_day(tx, NOW)
    counted = await owner(
        operations_case,
        "SELECT count(*) AS started FROM run_facts WHERE environment_id=:environment AND NOT test AND started_at>=:lo",
        lo=NOW - timedelta(hours=24),
    )
    assert day.started == counted[0]["started"] == 25
    assert (day.succeeded, day.failed, day.failure_rate) == (20, 5, 0.2)


async def test_hourly_and_daily_reads_join_closed_and_open_periods(operations_case):
    hour = datetime(2031, 3, 4, 10, 0, tzinfo=UTC)
    for granularity, start, started in (
        ("1d", datetime(2031, 3, 3, tzinfo=UTC), 100),
        ("1h", hour, 7),
        ("5m", hour + timedelta(hours=1, minutes=5), 2),
        # A five-minute row of a closed hour is already in its hourly row: it is never counted twice.
        ("5m", hour + timedelta(minutes=5), 7),
    ):
        await owner(
            operations_case,
            "INSERT INTO run_stat_buckets(tenant_id,project_id,environment_id,granularity,bucket_start,workflow_name,"
            "workflow_version,started,duration_hist) VALUES(:tenant,:project,:environment,:granularity,:start,"
            "'orders','1.0.0',:started,:hist)",
            granularity=granularity,
            start=start,
            started=started,
            hist=list(histogram([])),
        )
    await owner(
        operations_case,
        "INSERT INTO operations_statistics_cursors(tenant_id,project_id,environment_id,rolled_until,hours_until,"
        "days_until) VALUES(:tenant,:project,:environment,:rolled,:hours,:days)",
        rolled=NOW,
        hours=hour + timedelta(hours=1),
        days=datetime(2031, 3, 4, tzinfo=UTC),
    )
    hourly = await read(
        operations_case,
        statistics_reads.series,
        StatisticsSeriesQuery.model_validate(
            {
                "from": "2031-03-04T10:00:00Z",
                "to": "2031-03-04T12:00:00Z",
                "granularity": "1h",
                "signals": ["runs_started"],
            }
        ),
    )
    assert at(hourly, "runs_started", hour) == 7
    assert at(hourly, "runs_started", hour + timedelta(hours=1)) == 2
    daily = await read(
        operations_case,
        statistics_reads.series,
        StatisticsSeriesQuery.model_validate(
            {
                "from": "2031-03-03T00:00:00Z",
                "to": "2031-03-04T12:00:00Z",
                "granularity": "1d",
                "signals": ["runs_started"],
            }
        ),
    )
    assert at(daily, "runs_started", datetime(2031, 3, 3, tzinfo=UTC)) == 100
    assert at(daily, "runs_started", datetime(2031, 3, 4, tzinfo=UTC)) == 9


async def test_withheld_runs_count_only_under_other(operations_case):
    shown, withheld = await started_runs(operations_case, 2)
    await place(operations_case, shown, started=B0, ended=B0 + ms(100), status="succeeded")
    await place(
        operations_case,
        withheld,
        started=B0,
        ended=B0 + ms(100),
        status="failed",
        workflow="secret-flow",
        classification="unavailable",
    )
    await rollup(operations_case)
    table = await read(operations_case, statistics_reads.workflows, StatisticsWorkflowsQuery.model_validate(RANGE))
    assert [(item.workflow, item.versions, item.runs) for item in table.workflows] == [
        ("orders", ["1.0.0"], 1),
        ("(other)", [], 1),
    ]
    assert "secret-flow" not in table.model_dump_json()


async def test_reads_are_isolated_per_environment(operations_case):
    await seeded(operations_case, total=3, failed=0, timed_out=0)
    other = uuid4()
    await owner(operations_case, "INSERT INTO environments VALUES(:other,:tenant,:project,'reads-other')", other=other)
    elsewhere = Scope(
        tenant_id=operations_case.scope.tenant_id, project_id=operations_case.scope.project_id, environment_id=other
    )
    async with UnitOfWork(operations_case.sessions).open(elsewhere, mutation=False) as tx:
        answer = await statistics_reads.workflows(tx, StatisticsWorkflowsQuery.model_validate(RANGE))
        day = await statistics_reads.last_day(tx, NOW)
    assert answer.workflows == [] and answer.complete_until is None and day.started == 0


async def test_a_slow_read_answers_the_timeout_not_partial_data(operations_case, monkeypatch):
    await seeded(operations_case, total=2, failed=0, timed_out=0)
    cursor = statistics_reads.RunStatisticsReader.cursor

    async def slow(self):
        await self.tx.session.execute(text("SELECT pg_sleep(0.2)"))
        return await cursor(self)

    monkeypatch.setattr(statistics_reads, "READ_TIMEOUT", "50ms")
    monkeypatch.setattr(statistics_reads.RunStatisticsReader, "cursor", slow)
    service = statistics_reads.RunStatisticsService(UnitOfWork(operations_case.sessions), _Allow())  # type: ignore[arg-type]
    monkeypatch.setattr(statistics_reads, "load_principal", _stored)
    with pytest.raises(DBAPIError) as caught:
        await service.workflows(
            operations_case.actor,
            operations_case.scope,
            StatisticsWorkflowsQuery.model_validate(RANGE),
            context=AuditContext(),
        )
    assert caught.value.orig.sqlstate == "57014"


class _Allow:
    def require(self, actor, scope, capability, *, context):
        assert capability == "status.read"


async def _stored(session, actor_id):
    return actor_id


async def test_the_routes_serve_from_and_to_and_answer_the_query_errors(operations_case, authenticated_client):
    case = operations_case
    client, tokens, scopes = authenticated_client
    assert scopes[0] == case.scope
    environment = (
        f"/api/v1/tenants/{case.scope.tenant_id}/projects/{case.scope.project_id}"
        f"/environments/{case.scope.environment_id}/run-statistics"
    )
    headers = {"Authorization": "Bearer " + tokens[0]}
    served = await client.get(
        environment + "/series",
        params=[("from", "2031-03-04T11:00:00Z"), ("to", "2031-03-04T12:00:00Z"), ("signals", "runs_started")],
        headers=headers,
    )
    assert served.status_code == 200, served.text
    body = served.json()
    assert list(body)[:2] == ["from", "to"] and body["granularity"] == "5m"
    assert body["series"][0]["signal"] == "runs_started" and len(body["series"][0]["points"]) == 12
    for params, code in (
        ([("from", "2031-03-04T12:00:00Z"), ("to", "2031-03-04T11:00:00Z"), ("signals", "runs_started")], "WV-FILTER"),
        (
            [("from", "2031-01-01T00:00:00Z"), ("to", "2031-02-01T00:00:00Z"), ("granularity", "1h")]
            + [("signals", "runs_started")],
            "WV-STATS-RANGE",
        ),
        (
            [("from", "2031-03-04T11:00:00Z"), ("to", "2031-03-04T12:00:00Z"), ("group_by", "workflow"), ("top", "20")]
            + [("signals", "runs_started"), ("signals", "runs_failed")],
            "WV-STATS-SERIES",
        ),
        ([("from", "2031-03-04T11:00:00Z"), ("to", "2031-03-04T12:00:00Z"), ("unknown", "1")], "WV-VALIDATION"),
    ):
        refused = await client.get(environment + "/series", params=params, headers=headers)
        assert (refused.status_code, refused.json()["code"]) == (422, code), refused.text
    table = await client.get(
        environment + "/workflows",
        params={"from": "2031-03-04T11:00:00Z", "to": "2031-03-04T12:00:00Z"},
        headers=headers,
    )
    assert table.status_code == 200 and table.json()["workflows"] == []
    other = {"Authorization": "Bearer " + tokens[1]}
    assert (await client.get(environment + "/workflows", params=dict(RANGE), headers=other)).status_code == 403
