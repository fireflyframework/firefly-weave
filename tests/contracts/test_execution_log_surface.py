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

"""Writing log entries and reading them across runs are published, documented and bounded like every operation."""

import json
from datetime import UTC, datetime, timedelta
from functools import cache

import pytest
from starlette.datastructures import QueryParams

from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.run_views import ExecutionLogPage, ExecutionLogQuery
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.contracts.workers import TaskLogAccepted, TaskLogEntry, TaskLogRequest
from firefly_weave.definitions.models import CatalogError

ENVIRONMENT = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
spec = cache(export_openapi)


def native(identifier: str) -> dict:
    operation = OPERATIONS[identifier]
    return spec()["paths"][operation.canonical_path][operation.method.lower()]


def test_workers_write_entries_with_task_log_and_get_202():
    operation = OPERATIONS["tasks.log"]
    assert (operation.method, operation.canonical_path) == ("POST", ENVIRONMENT + "/tasks/logs")
    assert (operation.capability, operation.request, operation.response) == (
        "task.log",
        TaskLogRequest,
        TaskLogAccepted,
    )
    assert operation.statuses == (202,) and operation.served
    assert "202" in native("tasks.log")["responses"]


def test_the_log_across_runs_is_a_run_read_with_its_own_query():
    operation = OPERATIONS["execution_logs.list"]
    assert (operation.method, operation.canonical_path) == ("GET", ENVIRONMENT + "/execution-logs")
    assert (operation.capability, operation.query, operation.response) == (
        "run.read",
        ExecutionLogQuery,
        ExecutionLogPage,
    )
    names = [
        parameter["name"] for parameter in native("execution_logs.list")["parameters"] if parameter["in"] == "query"
    ]
    assert names == [
        "from",
        "to",
        "workflow",
        "level",
        "code",
        "node_id",
        "contains",
        "include_test",
        "limit",
        "cursor",
    ]


def test_a_batch_holds_one_to_fifty_entries_and_no_engine_entry():
    entry = {"at": NOW.isoformat(), "source": "worker", "level": "info", "message": "Checked"}
    lease = {
        "task_id": "6f2c1a8e-3b4d-4e5f-9a7b-1c2d3e4f5a6b",
        "generation": 1,
        "owner": "6f2c1a8e-3b4d-4e5f-9a7b-1c2d3e4f5a6c",
        "token": "t",
    }
    # As the wire sends it: strict models take UUIDs and times as strings only in JSON.
    TaskLogRequest.model_validate_json(json.dumps({"lease": lease, "entries": [entry] * 50}))
    for entries in ([], [entry] * 51, [{**entry, "source": "engine"}], [{**entry, "message": "m" * 513}]):
        with pytest.raises(ValueError):
            TaskLogRequest.model_validate_json(json.dumps({"lease": lease, "entries": entries}))


def test_a_usage_entry_comes_from_an_ai_source_with_both_counts():
    usage = {"at": NOW, "level": "info", "code": "AI.USAGE", "message": "AI usage"}
    TaskLogEntry.model_validate({**usage, "source": "ai", "fields": {"input_tokens": 1, "output_tokens": 2}})
    for source, fields in (("worker", {"input_tokens": 1, "output_tokens": 2}), ("ai", {"input_tokens": 1})):
        with pytest.raises(ValueError):
            TaskLogEntry.model_validate({**usage, "source": source, "fields": fields})


def query(text: str) -> ExecutionLogQuery:
    return parse_query(QueryParams(text), ExecutionLogQuery)


def test_without_a_range_the_last_hour_is_read():
    assert query("").window(NOW) == (NOW - timedelta(hours=1), NOW)
    assert query("to=2026-11-17T10:00:00Z").window(NOW) == (NOW - timedelta(hours=3), NOW - timedelta(hours=2))


@pytest.mark.parametrize(
    "text,message",
    [
        ("from=2026-11-17T12:00:00Z&to=2026-11-17T11:00:00Z", "from must be earlier than to"),
        ("from=2026-11-09T11:00:00Z&to=2026-11-17T12:00:00Z", "The time range is longer than 7 days"),
        ("from=2026-11-17T05:00:00Z&to=2026-11-17T12:00:00Z&contains=refund", "6 hours or less"),
    ],
)
def test_a_range_too_long_or_backwards_is_a_filter_error(text, message):
    with pytest.raises(CatalogError) as refused:
        query(text)
    assert (refused.value.status, refused.value.code) == (422, "WV-FILTER")
    assert message in refused.value.message


def test_a_search_over_six_hours_from_now_is_refused_when_read():
    from pydantic_core import PydanticCustomError

    searching = query("from=2026-11-17T05:00:00Z&contains=refund")
    with pytest.raises(PydanticCustomError):
        searching.window(NOW)
    assert query("from=2026-11-17T06:00:00Z&contains=refund").window(NOW)[0] == NOW - timedelta(hours=6)


def test_the_cursor_is_bound_to_every_filter_including_the_search():
    assert query("contains=refund").cursor_collection() != query("contains=Refund").cursor_collection()
    assert query("level=warning").cursor_collection() != query("").cursor_collection()
    assert query("limit=20").cursor_collection() == query("").cursor_collection()
