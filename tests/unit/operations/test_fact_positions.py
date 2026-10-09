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

"""Typed run and step cursor boundaries and transport limits."""

import base64
import json
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID

import pytest

from firefly_weave.api.transport import decode_cursor_v2, encode_cursor_v2
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.run_views import RunSummaryQuery
from firefly_weave.operations.fact_positions import run_position, step_position

RUN = "00000000-0000-0000-0000-000000000001"
SCOPE = Scope(tenant_id=UUID(RUN), project_id=UUID(RUN), environment_id=UUID(RUN))


@pytest.mark.parametrize(
    "value",
    [
        3,
        True,
        {},
        [],
        ["time"],
        "2026-10-09",
        "2026-13-09T00:00:00Z",
        "2026-10-09 00:00:00Z",
        "2026-10-09T00:00:00",
        "2026-10-09T00:00:00+00:60",
        "9999-12-31T23:59:59-01:00",
        "0001-01-01T00:00:00+00:01",
    ],
)
def test_run_cursor_wrong_sql_type_is_validation(value):
    with pytest.raises(ValueError):
        run_position((value, RUN))


@pytest.mark.parametrize("identifier", [None, 1, True, {}, [], "bad", ""])
def test_run_cursor_uuid_must_be_a_string(identifier):
    with pytest.raises(ValueError):
        run_position((None, identifier))


def test_positions_normalize_times_preserve_null_and_are_immutable():
    assert run_position(None) is None
    assert step_position(None) is None
    run = run_position(("2026-10-09T02:00:00+02:00", RUN))
    assert (run.at, run.id) == (datetime(2026, 10, 9, tzinfo=UTC), UUID(RUN))
    with pytest.raises(FrozenInstanceError):
        run.at = None
    assert run_position((None, RUN)).at is None
    step = step_position(([None, "send", "send[3]#2~1"], "send[3]#2~1"))
    assert (step.scheduled_at, step.node_id, step.instance_key) == (None, "send", "send[3]#2~1")
    with pytest.raises(FrozenInstanceError):
        step.node_id = "other"
    plain = step_position((["2026-10-09T02:00:00+02:00", "send", ""], "send"))
    assert (plain.scheduled_at, plain.instance_key) == (datetime(2026, 10, 9, tzinfo=UTC), "")


@pytest.mark.parametrize(
    "value,identifier",
    [
        ([None, "send", "other[3]"], "send[3]"),
        ([None, "send", "send[3]"], "send"),
        ([None, "send", "send"], "send"),
        ([None, "@join:send", ""], "@join:send"),
        ([None, "send", "send[03]"], "send[03]"),
        ([None, 3, ""], "3"),
        ([None, True, ""], "true"),
        ([None, {}, ""], "x"),
        ([None, [], ""], "x"),
        ([None, "send", 3], "x"),
        ([None, "send", True], "x"),
        ([None, "send", {}], "x"),
        ([None, "send", []], "x"),
        ([True, "send", ""], "send"),
        ([3, "send", ""], "send"),
        ([{}, "send", ""], "send"),
        ([[], "send", ""], "send"),
        (None, "send"),
        ({}, "send"),
        ([], "send"),
        ([None, "send"], "send"),
        ([None, "send", "", "extra"], "send"),
        ([None, "send", ""], None),
    ],
)
def test_step_cursor_shape_and_identity_are_validated(value, identifier):
    with pytest.raises(ValueError):
        step_position((value, identifier))


def test_cursor_encoded_length_boundary_is_enforced_both_ways():
    collection = "runs"
    # A 1536-byte JSON envelope produces exactly 2048 base64url characters.
    envelope = json.dumps([2, SCOPE.model_dump(mode="json"), collection, "", "x"], separators=(",", ":"))
    value = "x" * (1536 - len(envelope.encode()))
    cursor = encode_cursor_v2(SCOPE, collection, value, "x")
    assert len(cursor) == 2048
    assert decode_cursor_v2(cursor, SCOPE, collection) == (value, "x")
    with pytest.raises(ValueError):
        encode_cursor_v2(SCOPE, collection, value + "x", "x")
    with pytest.raises(ValueError):
        decode_cursor_v2(cursor + "A", SCOPE, collection)
    assert RunSummaryQuery(cursor="A" * 2048).cursor is not None
    with pytest.raises(ValueError):
        RunSummaryQuery(cursor="A" * 2049)


def test_deep_json_cursor_is_a_validation_error():
    # Stay below the encoded length cap even when the caller has a smaller recursion budget.
    raw = "[2," + json.dumps(SCOPE.model_dump(mode="json")) + ',"runs",' + "[" * 600 + "0" + "]" * 600 + ',"x"]'
    cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    assert len(cursor) <= 2048
    previous = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(350)
        with pytest.raises(ValueError, match="Invalid scope-bound cursor"):
            decode_cursor_v2(cursor, SCOPE, "runs")
    finally:
        sys.setrecursionlimit(previous)
