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

"""Time-ordered cursors bound to their filters, and strict query decoding for run views."""

import base64
import json
from uuid import uuid4

import pytest
from starlette.datastructures import QueryParams

from firefly_weave.api.transport import decode_cursor_v2, encode_cursor, encode_cursor_v2, parse_query
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_views import RunLogQuery, RunStepQuery, RunSummaryQuery
from firefly_weave.definitions.models import CatalogError

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


def test_cursor_v2_round_trips_sort_values_of_every_run_view():
    collection = RunSummaryQuery(include_test=True).cursor_collection()
    for sort_value, identifier in (
        ("2026-10-07T12:00:00Z", str(uuid4())),
        (["2026-10-07T12:00:00Z", "send"], "send[3]"),
        ([None, "notify"], ""),
        ([42, "2026-10-07T12:00:00.250000Z"], str(uuid4())),
    ):
        cursor = encode_cursor_v2(SCOPE, collection, sort_value, identifier)
        assert len(cursor) <= 2048
        assert decode_cursor_v2(cursor, SCOPE, collection) == (sort_value, identifier)
    assert decode_cursor_v2(None, SCOPE, collection) is None


def test_cursor_v2_rejects_other_filters_orders_scopes_versions_and_tampering():
    query = RunSummaryQuery(workflow="invoice-approval", include_test=True)
    cursor = encode_cursor_v2(SCOPE, query.cursor_collection(), "2026-10-07T12:00:00Z", str(uuid4()))
    other_scope = Scope(tenant_id=SCOPE.tenant_id, project_id=SCOPE.project_id, environment_id=uuid4())
    raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    padded = base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
    for value, scope, collection in (
        (cursor, SCOPE, query.model_copy(update={"include_test": False}).cursor_collection()),
        (cursor, SCOPE, query.model_copy(update={"order": "started_asc"}).cursor_collection()),
        (cursor, other_scope, query.cursor_collection()),
        (encode_cursor(SCOPE, query.cursor_collection(), uuid4()), SCOPE, query.cursor_collection()),
        (padded, SCOPE, query.cursor_collection()),
        (cursor[:-2], SCOPE, query.cursor_collection()),
        (cursor + "==", SCOPE, query.cursor_collection()),
        ("A" * 2049, SCOPE, query.cursor_collection()),
    ):
        with pytest.raises(ValueError, match="Invalid scope-bound cursor"):
            decode_cursor_v2(value, scope, collection)


@pytest.mark.parametrize(
    ("sort_value", "identifier"),
    [
        (float("nan"), "x"),
        (float("inf"), "x"),
        (float("-inf"), "x"),
        (9007199254740993, "x"),
        ("\ud800", "x"),
        ("2026-10-07T12:00:00Z", "\ud800"),
    ],
)
def test_cursor_v2_rejects_values_outside_the_json_domain(sort_value, identifier):
    collection = RunSummaryQuery(include_test=True).cursor_collection()
    raw = json.dumps([2, SCOPE.model_dump(mode="json"), collection, sort_value, identifier], separators=(",", ":"))
    cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    with pytest.raises(ValueError, match="Invalid scope-bound cursor"):
        decode_cursor_v2(cursor, SCOPE, collection)
    # The same shape with the largest safe integer decodes, so only the bad value makes each probe invalid.
    boundary = encode_cursor_v2(SCOPE, collection, 9007199254740991, "x")
    assert decode_cursor_v2(boundary, SCOPE, collection) == (9007199254740991, "x")


def test_queries_decode_typed_repeated_and_encoded_values():
    query = parse_query(
        QueryParams(
            "workflow=invoice-approval&include_test=true&order=started_desc&limit=50"
            "&status=running&status=failed&started_after=2026-10-07T14%3A00%3A00%2B02%3A00"
        ),
        RunSummaryQuery,
    )
    assert (query.workflow, query.include_test, query.limit) == ("invoice-approval", True, 50)
    assert query.status == ["failed", "running"]
    assert query.started_after is not None and query.started_after.isoformat() == "2026-10-07T12:00:00+00:00"
    assert parse_query(QueryParams("step=send&include=output&limit=500"), RunStepQuery).limit == 500
    assert parse_query(QueryParams("level=warning&node_id=send"), RunLogQuery).level == "warning"
    assert parse_query(QueryParams(""), RunSummaryQuery) == RunSummaryQuery()


@pytest.mark.parametrize(
    "raw",
    [
        "version=1.3.0",
        "top_level_only=true&origin=call",
        "origin=test",
        "started_after=2026-10-07T12:00:00Z&started_before=2026-10-07T11:00:00Z",
        "started_after=2025-01-01T00:00:00Z&started_before=2026-10-07T00:00:00Z",
    ],
)
def test_contradictory_filters_answer_wv_filter(raw):
    with pytest.raises(CatalogError) as failure:
        parse_query(QueryParams(raw), RunSummaryQuery)
    assert (failure.value.status, failure.value.code) == (422, "WV-FILTER")


@pytest.mark.parametrize(
    "raw",
    [
        "unknown=1",
        "workflow=a&workflow=b",
        "include_test=yes",
        "include_test=1",
        "limit=5.0",
        "limit=101",
        "status=bogus",
        "started_after=2026-10-07T12:00:00",
        # An unencoded "+" decodes to a space, so the offset is lost: clients must encode or send UTC.
        "started_after=2026-10-07T12:00:00+02:00",
        # An offset that moves the instant past year 9999 or before year 1 is a bad value, not a server error.
        "started_after=9999-12-31T23:59:59-23:59",
        "started_before=9999-12-31T23:59:59-01:00",
        "started_after=0001-01-01T00:00:00%2B23:59",
        "started_before=0001-01-01T00:00:00%2B00:01",
        "&".join(["status=queued"] * 9),
    ],
)
def test_invalid_values_answer_wv_validation(raw):
    # ValidationError is a ValueError: the error advice answers both with 422 WV-VALIDATION.
    with pytest.raises(ValueError):
        parse_query(QueryParams(raw), RunSummaryQuery)


@pytest.mark.parametrize(
    "raw",
    [
        "started_after=0001-01-01T00:00:00Z",
        "started_after=0001-01-01T01:00:00%2B01:00",
        "started_before=9999-12-31T23:59:59.999999Z",
        "started_before=9999-12-31T22:59:59-01:00",
    ],
)
def test_time_filters_accept_the_first_and_last_representable_instants(raw):
    query = parse_query(QueryParams(raw), RunSummaryQuery)
    assert (query.started_after or query.started_before) is not None
