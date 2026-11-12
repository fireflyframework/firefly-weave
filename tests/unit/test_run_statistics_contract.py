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


"""Run statistics queries: time ranges, automatic granularity, point and series limits, signal grouping rules."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from starlette.datastructures import QueryParams

from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.run_statistics import (
    MAX_POINTS,
    MAX_SERIES,
    SIGNAL_UNITS,
    RunsDaySection,
    RunTotals,
    StatisticsSeries,
    StatisticsSeriesAnswer,
    StatisticsSeriesQuery,
    StatisticsStepsQuery,
    StatisticsWorkflowsQuery,
    WorkflowStatisticsAnswer,
    automatic_granularity,
)
from firefly_weave.definitions.models import CatalogError

TO = datetime(2026, 10, 26, 14, 32, 10, tzinfo=UTC)


def query(model=StatisticsSeriesQuery, **values):
    params = []
    for name, value in values.items():
        for item in value if isinstance(value, list) else [value]:
            params.append((name, item.isoformat() if isinstance(item, datetime) else str(item)))
    return parse_query(QueryParams(params), model)


def series(span, **values):
    return query(**{"from": TO - span, "to": TO, "signals": ["runs_started"], **values})


@pytest.mark.parametrize(
    ("span", "granularity"),
    [
        (timedelta(hours=1), "5m"),
        (timedelta(hours=6), "5m"),
        (timedelta(hours=6, seconds=1), "1h"),
        (timedelta(days=1), "1h"),
        (timedelta(days=7), "1h"),
        (timedelta(days=7, seconds=1), "1d"),
        (timedelta(days=30), "1d"),
    ],
)
def test_granularity_is_chosen_from_the_range_as_the_metrics_page_shows_it(span, granularity):
    assert automatic_granularity(span) == granularity
    assert series(span).resolved_granularity == granularity


def test_points_start_on_whole_buckets_and_cover_the_range():
    day = series(timedelta(days=1))
    assert day.first_bucket == datetime(2026, 10, 25, 14, tzinfo=UTC)
    assert day.points == 25
    month = series(timedelta(days=30))
    assert month.first_bucket == datetime(2026, 9, 26, tzinfo=UTC) and month.points == 31


def test_too_many_points_for_the_chosen_granularity_is_a_range_error():
    assert series(timedelta(days=20), granularity="1h").points == 481
    with pytest.raises(CatalogError) as raised:
        series(timedelta(days=30), granularity="1h")
    assert (raised.value.status, raised.value.code) == (422, "WV-STATS-RANGE")
    assert MAX_POINTS == 500


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"from": TO, "to": TO}, "from must be before to"),
        ({"from": TO - timedelta(days=401), "to": TO}, "range"),
        ({"from": TO - timedelta(hours=1), "to": TO, "version": "1.0.0"}, "version requires workflow"),
        ({"from": TO - timedelta(hours=1), "to": TO, "group_by": "task_type"}, "task_type"),
        ({"from": TO - timedelta(hours=1), "to": TO, "group_by": "status", "signals": ["failure_rate"]}, "status"),
        (
            {"from": TO - timedelta(hours=1), "to": TO, "group_by": "workflow", "workflow": "orders"},
            "group_by workflow",
        ),
        (
            {"from": TO - timedelta(hours=1), "to": TO, "workflow": "orders", "signals": ["tasks_ready"]},
            "run signals",
        ),
        ({"from": TO - timedelta(hours=1), "to": TO, "top": 3}, "top requires group_by"),
    ],
)
def test_contradictory_filters_answer_the_filter_error_naming_the_rule(values, message):
    values.setdefault("signals", ["runs_started"])
    with pytest.raises(CatalogError) as raised:
        query(**values)
    assert (raised.value.status, raised.value.code) == (422, "WV-FILTER")
    assert message in raised.value.message


def test_series_beyond_the_limit_answer_the_series_error():
    grouped = query(
        **{"from": TO - timedelta(hours=1), "to": TO, "signals": ["runs_failed"], "group_by": "workflow", "top": 20}
    )
    assert grouped.series_count == MAX_SERIES == 21
    with pytest.raises(CatalogError) as raised:
        query(
            **{
                "from": TO - timedelta(hours=1),
                "to": TO,
                "signals": ["runs_failed", "runs_started"],
                "group_by": "workflow",
                "top": 10,
            }
        )
    assert (raised.value.status, raised.value.code) == (422, "WV-STATS-SERIES")
    every = query(**{"from": TO - timedelta(hours=1), "to": TO, "signals": sorted(SIGNAL_UNITS)})
    assert every.series_count == len(SIGNAL_UNITS) <= MAX_SERIES


def test_signals_are_known_unique_and_kept_in_a_canonical_order():
    answer = query(**{"from": TO - timedelta(hours=1), "to": TO, "signals": ["runs_failed", "runs_started"]})
    assert answer.signals == ["runs_failed", "runs_started"]
    duplicate = query(**{"from": TO - timedelta(hours=1), "to": TO, "signals": ["runs_started", "runs_started"]})
    assert duplicate.signals == ["runs_started"]
    for invalid in ([], ["runs"], ["RUNS_STARTED"]):
        with pytest.raises((ValidationError, CatalogError, ValueError)):
            query(**{"from": TO - timedelta(hours=1), "to": TO, "signals": invalid} if invalid else {"to": TO})


def test_units_are_counts_ratios_milliseconds_or_seconds():
    assert SIGNAL_UNITS["failure_rate"] == SIGNAL_UNITS["worker_utilization"] == "ratio"
    assert SIGNAL_UNITS["duration_p95"] == "ms" and SIGNAL_UNITS["oldest_ready_seconds"] == "seconds"
    assert {SIGNAL_UNITS[name] for name in ("runs_started", "tasks_ready", "outbox_pending")} == {"count"}
    assert {"runs_active", "waits_active", "provider_pending"} <= set(SIGNAL_UNITS)


def test_workflow_and_step_tables_share_the_range_rules():
    table = query(StatisticsWorkflowsQuery, **{"from": TO - timedelta(days=7), "to": TO})
    assert table.resolved_granularity == "1h"
    steps = query(StatisticsStepsQuery, **{"from": TO - timedelta(hours=1), "to": TO, "workflow": "orders"})
    assert steps.workflow == "orders" and steps.resolved_granularity == "5m"
    with pytest.raises((ValidationError, CatalogError, ValueError)):
        query(StatisticsStepsQuery, **{"from": TO - timedelta(hours=1), "to": TO})
    with pytest.raises(CatalogError):
        query(StatisticsWorkflowsQuery, **{"from": TO, "to": TO - timedelta(hours=1)})


def test_answers_use_from_on_the_wire_and_bound_points_and_series():
    point = (TO, 0.25)
    one = StatisticsSeries(signal="failure_rate", labels={}, unit="ratio", points=[point, (TO, None)])
    answer = StatisticsSeriesAnswer(
        **{"from": TO - timedelta(hours=1), "to": TO, "granularity": "5m", "complete_until": None, "series": [one]}
    )
    body = answer.model_dump(mode="json")
    assert list(body)[:2] == ["from", "to"] and body["series"][0]["points"][1] == ["2026-10-26T14:32:10Z", None]
    with pytest.raises(ValidationError):
        StatisticsSeries(signal="failure_rate", labels={}, unit="ratio", points=[point] * (MAX_POINTS + 1))
    with pytest.raises(ValidationError):
        StatisticsSeriesAnswer(
            **{"from": TO, "to": TO, "granularity": "5m", "complete_until": None, "series": [one] * (MAX_SERIES + 1)}
        )


def test_the_last_day_reports_rates_as_fractions_or_nothing():
    assert RunsDaySection(started=240, succeeded=231, failed=6, failure_rate=0.025).failure_rate == 0.025
    assert RunsDaySection(started=0, succeeded=0, failed=0, failure_rate=None).model_dump()["failure_rate"] is None
    with pytest.raises(ValidationError):
        RunsDaySection(started=1, succeeded=0, failed=1, failure_rate=1.5)


def test_the_workflow_table_may_carry_the_range_totals_and_answers_without_them_still_parse():
    window = {"from": TO - timedelta(hours=24), "to": TO, "granularity": "1h", "complete_until": TO}
    totals = RunTotals(
        runs=25, succeeded=20, failed=5, cancelled=0, failure_rate=0.2, duration_p50_ms=120.0, duration_p95_ms=900.5
    )
    body = WorkflowStatisticsAnswer(**window, workflows=[], totals=totals).model_dump(mode="json")
    assert body["totals"]["failure_rate"] == 0.2 and body["totals"]["duration_p95_ms"] == 900.5
    old = WorkflowStatisticsAnswer.model_validate({**window, "workflows": []})
    assert old.totals is None and "totals" not in old.model_dump(mode="json", exclude_none=True)
    with pytest.raises(ValidationError):
        RunTotals(
            runs=1, succeeded=0, failed=1, cancelled=0, failure_rate=2, duration_p50_ms=None, duration_p95_ms=None
        )
