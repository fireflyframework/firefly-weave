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

"""Incident queries and enriched incident views; earlier incident payloads keep their bytes."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError
from starlette.datastructures import QueryParams

from firefly_weave.contracts.operations import IncidentView

# The exact bytes an incident view serialized to before incidents carried facts.
OLD_RESOLVED = (
    '{"id":"0f8b5c1e-7d1a-4a52-9c39-3b1f4f7e2a10","run_id":"5a7e2c44-91b3-4c0e-8f6d-2d3c4b5a6e7f",'
    '"incident_key":"work:1","node_id":"work","generation":1,"origin_code":"WV-TASK-AMBIGUOUS",'
    '"code":"WV-TASK-AMBIGUOUS","status":"resolved","revision":2,"actor_id":"9d2f6a3b-1c4e-4f7a-8b9c-0d1e2f3a4b5c",'
    '"resolved_at":"2026-10-07T12:00:00Z","resolution":{"receipt_id":"3c2b1a09-8f7e-4d6c-9b5a-4e3d2c1b0a99",'
    '"kind":"terminate","reason":"Stop the run","output":null,"evidence_reference":null},'
    '"external_effects_may_continue":true}'
)
OLD_ACTIVE = (
    '{"id":"0f8b5c1e-7d1a-4a52-9c39-3b1f4f7e2a10","run_id":"5a7e2c44-91b3-4c0e-8f6d-2d3c4b5a6e7f",'
    '"incident_key":"work:1","node_id":"work","generation":1,"origin_code":"WV-TASK-AMBIGUOUS",'
    '"code":"WV-TASK-AMBIGUOUS","status":"active","revision":1,"actor_id":null,"resolved_at":null,'
    '"resolution":null,"external_effects_may_continue":false}'
)
ENRICHED = {
    **json.loads(OLD_ACTIVE),
    "node_id": "send",
    "instance_key": "send[2]",
    "opened_at": "2026-10-07T11:00:00Z",
    "updated_at": "2026-10-07T11:30:00Z",
    "workflow": {"name": "invoice-approval", "version": "1.3.0"},
    "run_status": "suspended",
    "test": True,
}
ADDITIVE = {"instance_key", "opened_at", "opened_at_estimated", "updated_at", "workflow", "run_status", "test"}


def query(model, values):
    return model.model_validate_json(json.dumps(values))


@pytest.mark.parametrize("raw", [OLD_RESOLVED, OLD_ACTIVE])
def test_earlier_incident_payloads_keep_their_bytes(raw):
    old = IncidentView.model_validate_json(raw)
    assert old.model_dump_json() == raw
    assert "opened_at_estimated" not in old.model_dump(mode="json")
    estimated = old.model_copy(update={"opened_at_estimated": True})
    assert estimated.model_dump(mode="json")["opened_at_estimated"] is True


@pytest.mark.parametrize("estimated", [False, True])
def test_exact_and_estimated_opening_times_round_trip_through_generated_schemas(estimated):
    from jsonschema import Draft202012Validator

    from firefly_weave.contracts.schema_export import export_schemas

    payload = {**ENRICHED, **({"opened_at_estimated": True} if estimated else {})}
    view = IncidentView.model_validate_json(json.dumps(payload))
    assert view.model_dump(mode="json") == payload
    schema = export_schemas()["incident-view"]
    assert not list(Draft202012Validator(schema).iter_errors(payload))
    assert list(Draft202012Validator(schema).iter_errors({**payload, "instance_key": "send[02]"}))
    assert set(schema["properties"]) >= ADDITIVE
    assert not ADDITIVE & set(schema["required"])


def test_unknown_opening_time_is_never_estimated():
    with pytest.raises(ValidationError):
        IncidentView.model_validate_json(json.dumps({**json.loads(OLD_ACTIVE), "opened_at_estimated": True}))


@pytest.mark.parametrize(
    "node_id,instance_key,valid",
    [
        ("send", "send[2]", True),
        ("support", "support[3]#2.review", True),
        ("send", "", True),
        (None, "", True),
        ("send", "send", False),
        ("send[2]", "send[2]", False),
        ("other", "send[2]", False),
        (None, "send[2]", False),
        ("send", "send[", False),
    ],
)
def test_instance_key_is_the_full_key_of_the_static_step(node_id, instance_key, valid):
    payload = {**json.loads(OLD_ACTIVE), "node_id": node_id, "instance_key": instance_key}
    if valid:
        view = IncidentView.model_validate_json(json.dumps(payload))
        assert ("instance_key" in view.model_dump(mode="json")) is bool(instance_key)
    else:
        with pytest.raises(ValidationError):
            IncidentView.model_validate_json(json.dumps(payload))


def test_query_defaults_list_every_status_newest_opened_first():
    from firefly_weave.contracts.incident_queries import IncidentQuery

    default = IncidentQuery()
    assert (default.status, default.order, default.limit, default.include_test, default.cursor) == (
        [],
        "opened_desc",
        50,
        False,
        None,
    )
    assert list(IncidentQuery.model_fields) == [
        "status",
        "code",
        "workflow",
        "node_id",
        "opened_after",
        "opened_before",
        "include_test",
        "order",
        "limit",
        "cursor",
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"opened_after": "2026-10-07T12:00:00Z", "opened_before": "2026-10-07T12:00:00Z"},
        {"opened_after": "2026-10-07T12:00:00Z", "opened_before": "2026-10-07T11:59:59Z"},
        {"opened_after": "2025-08-01T00:00:00Z", "opened_before": "2026-10-07T00:00:00Z"},
    ],
)
def test_contradictory_opening_ranges_are_filter_errors(values):
    from firefly_weave.contracts.incident_queries import IncidentQuery

    with pytest.raises(ValidationError) as failure:
        query(IncidentQuery, values)
    assert [item["type"] for item in failure.value.errors()] == ["weave_filter"]


@pytest.mark.parametrize(
    "values",
    [
        {"status": ["active", "closed", "resolved", "active"]},
        {"status": ["open"]},
        {"opened_after": "2026-10-07T12:00:00"},
        {"opened_before": "2026-10-07 12:00:00Z"},
        {"opened_after": "9999-12-31T23:59:59-23:59"},
        {"code": ""},
        {"code": "C" * 101},
        {"workflow": "bad name"},
        {"node_id": "send[2]"},
        {"order": "started_desc"},
        {"limit": 0},
        {"limit": 101},
        {"cursor": "not/base64"},
    ],
)
def test_invalid_query_values_are_validation_errors(values):
    from firefly_weave.contracts.incident_queries import IncidentQuery

    with pytest.raises(ValidationError) as failure:
        query(IncidentQuery, values)
    assert "weave_filter" not in {item["type"] for item in failure.value.errors()}


def test_opening_range_accepts_exactly_four_hundred_days_in_utc():
    from firefly_weave.contracts.incident_queries import IncidentQuery

    values = {"opened_after": "2026-01-01T01:00:00+01:00", "opened_before": "2027-02-05T00:00:00Z"}
    parsed = query(IncidentQuery, values)
    assert parsed.opened_after is not None and parsed.opened_after.isoformat() == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValidationError):
        query(IncidentQuery, {**values, "opened_before": "2027-02-05T00:00:00.000001Z"})


def test_route_decoding_reports_filter_errors_and_repeated_statuses():
    from firefly_weave.api.transport import parse_query
    from firefly_weave.contracts.incident_queries import IncidentQuery
    from firefly_weave.definitions.models import CatalogError

    parsed = parse_query(QueryParams("status=resolved&status=active&status=resolved&order=opened_asc"), IncidentQuery)
    assert (parsed.status, parsed.order) == (["active", "resolved"], "opened_asc")
    with pytest.raises(CatalogError) as failure:
        parse_query(QueryParams("opened_after=2026-10-07T12:00:00Z&opened_before=2026-10-07T12:00:00Z"), IncidentQuery)
    assert (failure.value.status, failure.value.code) == (422, "WV-FILTER")
    for raw in ("colour=red", "limit=1&limit=2", "include_test=yes"):
        with pytest.raises(ValueError):
            parse_query(QueryParams(raw), IncidentQuery)


def test_cursor_collection_keeps_id_cursors_and_binds_filters_order_and_run():
    from firefly_weave.contracts.incident_queries import IncidentQuery

    run = uuid4()
    legacy = IncidentQuery(order="id")
    # Explicit ID order without filters continues cursors issued before incidents had filters.
    assert legacy.cursor_collection() == "incidents"
    assert legacy.cursor_collection(run) == "incidents.run/" + str(run)
    assert IncidentQuery(order="id", limit=7).cursor_collection() == "incidents"
    collections = {
        IncidentQuery().cursor_collection(),
        IncidentQuery(order="opened_asc").cursor_collection(),
        IncidentQuery().cursor_collection(run),
        IncidentQuery().cursor_collection(uuid4()),
        IncidentQuery(order="id", status=["active"]).cursor_collection(),
        IncidentQuery(order="id", include_test=True).cursor_collection(),
        IncidentQuery(status=["active"]).cursor_collection(),
        IncidentQuery(code="WV-TASK-AMBIGUOUS").cursor_collection(),
        IncidentQuery(workflow="invoice-approval").cursor_collection(),
        IncidentQuery(node_id="send").cursor_collection(),
        IncidentQuery(include_test=True).cursor_collection(),
        query(IncidentQuery, {"opened_after": "2026-10-07T12:00:00Z"}).cursor_collection(),
        query(IncidentQuery, {"opened_before": "2026-10-07T12:00:00Z"}).cursor_collection(),
    }
    assert len(collections) == 13 and "incidents" not in collections
    assert (
        query(IncidentQuery, {"status": ["resolved", "active"], "limit": 3}).cursor_collection()
        == query(IncidentQuery, {"status": ["active", "resolved", "active"]}).cursor_collection()
    )


def test_incident_operations_declare_the_query_and_the_direct_read():
    from firefly_weave.contracts.incident_queries import IncidentQuery
    from firefly_weave.contracts.openapi import export_openapi
    from firefly_weave.contracts.public import UnavailableResource
    from firefly_weave.contracts.surface import OPERATIONS

    environment = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"
    spec = export_openapi()
    for identifier in ("incidents.list", "incidents.run_list"):
        operation = OPERATIONS[identifier]
        assert operation.query is IncidentQuery and operation.page
        parameters = spec["paths"][operation.canonical_path]["get"]["parameters"]
        assert [item["name"] for item in parameters if item["in"] == "query"] == list(IncidentQuery.model_fields)
    read = OPERATIONS["incidents.read"]
    assert (read.method, read.canonical_path, read.capability, read.response, read.etag) == (
        "GET",
        environment + "/incidents/{identifier}",
        "incident.read",
        IncidentView | UnavailableResource,
        True,
    )
    assert read.request is None and read.query is None and not read.page
    document = spec["paths"][read.canonical_path]["get"]
    assert "ETag" in document["responses"]["200"]["headers"]


def test_incident_schemas_are_exported():
    from firefly_weave.contracts.schema_export import export_schemas

    schemas = export_schemas()
    assert schemas["incident-query"]["properties"]["order"]["default"] == "opened_desc"
    definitions = schemas["incident-query"]["$defs"]
    assert definitions["IncidentStatusFilter"]["maxItems"] == 3
    assert definitions["IncidentStatus"]["enum"] == ["active", "closed", "resolved"]
    assert set(schemas["incident-view"]["required"]) == {
        "id",
        "run_id",
        "incident_key",
        "node_id",
        "generation",
        "origin_code",
        "code",
        "status",
        "revision",
    }
