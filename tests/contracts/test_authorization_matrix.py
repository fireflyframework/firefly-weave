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
from uuid import UUID, uuid4

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
    assert (row.kind is not None) == ("{kind}" in operation.path), row.operation
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


def test_activate_is_sent_as_each_person_on_the_demo_environment():
    # Studio's Activate… is a control the viewer cannot use; J9.1 sends the same call so the server's refusal is
    # checked too. Its body names the environment of the path, so a permitted person passes the scope rule.
    (row,) = [row for row in ROWS if row.operation == "activations.create"]
    assert (row.table, row.capability) == ("controls", "release.activate")
    scope = {"tenant_id": str(uuid4()), "project_id": str(uuid4()), "environment_id": str(uuid4())}
    body = matrix.materialize(dict(row.body), scope=scope)
    assert body["scope"] == scope and UUID(body["version_id"])
    assert set(matrix.materialize(dict(row.body))["scope"]) == set(scope)


def grant(role, *levels):
    ids = {"tenant_id": "t", "project_id": "p", "environment_id": "e"}
    return {"role": role, "scope": {key: ids[key] for key in ("tenant_id", *levels)}}


def test_a_grant_covers_operations_at_its_level_and_below_only():
    # The live run showed it: a viewer's environment grant does not read the project's language or telemetry.
    roles = {"viewer": frozenset({"catalog.read"}), "developer": frozenset({"catalog.read"})}
    environment = "/tenants/{tenant}/projects/{project}/environments/{environment}/x"
    project = "/tenants/{tenant}/projects/{project}/language"
    tenant = "/tenants/{tenant}/members"
    viewer = [grant("viewer", "project_id", "environment_id")]
    developer = [grant("developer", "project_id")]
    assert matrix.holds_at(viewer, "catalog.read", roles, environment)
    assert not matrix.holds_at(viewer, "catalog.read", roles, project)
    assert matrix.holds_at(developer, "catalog.read", roles, project)
    assert matrix.holds_at(developer, "catalog.read", roles, environment)
    assert not matrix.holds_at(developer, "catalog.read", roles, tenant)
    assert not matrix.holds_at(viewer, "run.read", roles, environment)


def test_the_publish_probe_is_a_workflow_that_compiles():
    # The live run showed a permitted publish answering 422 WV-COMPILE: the probe must be publishable, so a
    # permitted person's call succeeds (or meets the version an earlier person published).
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot

    (row,) = [row for row in ROWS if row.operation == "definitions.publish"]
    compiled = compile_source(row.body["source"], format=row.body["format"], catalog=CatalogSnapshot.empty())
    assert compiled.ok, [diagnostic.code for diagnostic in compiled.diagnostics]


def test_rows_name_the_answers_a_permitted_call_may_give_and_until_when():
    # Seen live: publishing the AI setup needs an AI worker the pr profile does not install, and run logs answer
    # 501 until the logs capability lands. Once it lands, 501 is wrong again.
    by_operation = {row.operation: row for row in ROWS}
    assert matrix.accepted(by_operation["ai_setup.publish"], {}) == {(422, "WV-AI-WORKER-MISSING")}
    assert matrix.accepted(by_operation["runs.logs"], {"logs": False}) == {(501, "WV-UNAVAILABLE")}
    assert matrix.accepted(by_operation["runs.logs"], {"logs": True}) == set()
    assert matrix.accepted(by_operation["language.read"], {}) == set()
    journeys = __import__("tomllib").loads((ROOT / "tests/acceptance/journeys.toml").read_text(encoding="utf-8"))
    for row in ROWS:
        for answer in row.answers:
            assert answer["code"].startswith("WV-") and 400 <= answer["status"] < 600, row.operation
            assert answer.get("until", "logs") in journeys["capabilities"], row.operation
