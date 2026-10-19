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

"""Every operation added after 0.1.0a14 has an authorization row the security journey can send as any person."""

import importlib.util
import json
import sys
from pathlib import Path
from uuid import UUID

import pytest
from starlette.datastructures import QueryParams

from firefly_weave.access.roles import ROLE_CAPABILITIES
from firefly_weave.api.transport import parse_query
from firefly_weave.contracts.surface import OPERATIONS

ROOT = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location(
        "acceptance_authorization", ROOT / "tests/acceptance/acceptance_authorization.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix = load()
BASELINE, ROWS = matrix.load()


def test_the_baseline_is_the_0_1_0a14_operation_list():
    document = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert document["version"] == "0.1.0a14" and len(document["operations"]) == 198
    assert document["operations"] == sorted(document["operations"])


def test_every_operation_added_after_the_baseline_is_in_the_matrix():
    added = set(OPERATIONS) - matrix.baseline_operations(BASELINE)
    rows = {row.operation for row in ROWS if row.table == "operations"}
    assert sorted(added - rows) == [], "add a row to tests/acceptance/authorization.toml for each operation"
    assert sorted(rows - added) == []


def test_matrix_capabilities_match_the_operations():
    for row in ROWS:
        assert row.capability == OPERATIONS[row.operation].capability, row.operation
        assert any(matrix.holds((role,), row.capability, ROLE_CAPABILITIES) for role in ROLE_CAPABILITIES), row


def test_holds_reads_alternative_capabilities():
    roles = {"tenant_admin": frozenset({"grant.manage"}), "viewer": frozenset({"run.read"})}
    assert matrix.holds({"tenant_admin"}, "grant.manage or grant.admin", roles)
    assert not matrix.holds({"viewer"}, "grant.manage or grant.admin", roles)
    assert not matrix.holds({"unknown-role"}, "run.read", roles)


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.operation)
def test_matrix_requests_fit_the_operation_contracts(row):
    operation = OPERATIONS[row.operation]
    assert row.identifier == ("{identifier}" in operation.path), row.operation
    assert (row.collection is not None) == ("{collection}" in operation.path), row.operation
    if operation.request is None:
        assert row.body is None, row.operation
    else:
        # JSON mode, as the wire sends it: strict models take UUIDs as strings only there.
        operation.request.model_validate_json(json.dumps(matrix.materialize(dict(row.body or {}))))
    query = matrix.materialize(dict(row.query))
    for value in query.values():
        assert isinstance(value, str | int)
    if operation.query is None:
        assert query == {}, row.operation
    elif query:
        # Decoded as the controller decodes it, so a misspelled or mistyped name fails here, not as a 422 live.
        parse_query(QueryParams({name: str(value) for name, value in query.items()}), operation.query)


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.operation)
def test_a_permitted_probe_previews_where_the_request_can_preview(row):
    # A permitted person's call must change nothing: a request that can preview (retiring stale workers) sends
    # dry_run = true, so the owner's and operator's probes never retire a worker.
    request = OPERATIONS[row.operation].request
    if request is not None and "dry_run" in request.model_fields:
        assert (row.body or {}).get("dry_run") is True, row.operation


def test_the_preview_rule_covers_retiring_stale_workers():
    (row,) = [row for row in ROWS if row.operation == "workers.retire_stale"]
    assert "dry_run" in OPERATIONS[row.operation].request.model_fields


def test_materialize_names_a_new_uuid_for_each_random_value():
    first, second = matrix.materialize(["random", "random"])
    assert UUID(first) != UUID(second)
    assert matrix.materialize({"a": {"b": "random"}, "c": 1})["c"] == 1
