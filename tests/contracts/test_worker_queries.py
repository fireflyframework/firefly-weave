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

"""Worker fleet queries, stale-worker retirement and release usage contracts; earlier worker bytes are kept."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from starlette.datastructures import QueryParams

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.workers import WorkerInstance, WorkerStatus
from firefly_weave.workers.models import observed_worker

ENVIRONMENT = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"


def query(model, values):
    return model.model_validate_json(json.dumps(values))


def status(**changes):
    instance = WorkerInstance(
        id=UUID("6f1d2c3b-4a59-4e8f-9d7c-1b2a3c4d5e6f"),
        principal_id=UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"),
        release_id=UUID("1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"),
        task_types=["echo@1.2.0"],
        capacity=2,
    )
    return observed_worker(
        instance,
        last_seen_at=None,
        draining=False,
        revision=1,
        active_leases=0,
        observed_at=datetime(2026, 10, 12, tzinfo=UTC),
        **changes,
    )


def test_list_defaults_are_current_workers_most_recently_seen_first():
    from firefly_weave.contracts.worker_queries import WorkerListQuery

    default = WorkerListQuery()
    assert (default.state, default.order, default.limit, default.cursor) == ("current", "last_seen_desc", 50, None)
    assert all(
        getattr(default, name) is None
        for name in ("presence", "draining", "release_id", "target_id", "task_type", "host_prefix")
    )


@pytest.mark.parametrize(
    "values",
    [
        {"presence": "online"},
        {"state": "retired"},
        {"order": "id"},
        {"order": "registered_asc"},
        {"limit": 0},
        {"limit": 101},
        {"task_type": ""},
        {"task_type": "t" * 201},
        {"host_prefix": ""},
        {"host_prefix": "h" * 254},
        {"host_prefix": "worker\n1"},
        {"release_id": "not-a-uuid"},
        {"cursor": "not a cursor"},
        {"draining": "yes"},
        {"identity": {}},
    ],
)
def test_malformed_list_values_are_validation_errors(values):
    from firefly_weave.contracts.worker_queries import WorkerListQuery

    with pytest.raises(ValidationError):
        query(WorkerListQuery, values)


def test_every_filter_and_bound_is_accepted_together():
    from firefly_weave.contracts.worker_queries import WorkerListQuery

    values = {
        "presence": "stale",
        "draining": True,
        "release_id": str(uuid4()),
        "target_id": str(uuid4()),
        "task_type": "t" * 200,
        "host_prefix": "%_" + "h" * 251,
        "state": "all",
        "order": "registered_desc",
        "limit": 100,
    }
    parsed = query(WorkerListQuery, values)
    assert parsed.model_dump(mode="json", exclude={"cursor"}) == values
    for limit in (1, 100):
        assert query(WorkerListQuery, {"limit": limit}).limit == limit


def test_route_decoding_is_strict():
    from firefly_weave.api.transport import parse_query
    from firefly_weave.contracts.worker_queries import WorkerListQuery

    parsed = parse_query(QueryParams("draining=false&presence=recent&state=all&order=registered_desc"), WorkerListQuery)
    assert (parsed.draining, parsed.presence, parsed.state, parsed.order) == (False, "recent", "all", "registered_desc")
    for raw in ("colour=red", "limit=1&limit=2", "draining=yes", "presence=online", "limit=0"):
        with pytest.raises(ValueError):
            parse_query(QueryParams(raw), WorkerListQuery)


def test_cursor_collection_binds_every_filter_and_the_order():
    from firefly_weave.contracts.worker_queries import WorkerListQuery

    release, target = str(uuid4()), str(uuid4())
    variants = [
        {},
        {"order": "registered_desc"},
        {"presence": "recent"},
        {"presence": "unknown"},
        {"draining": True},
        {"draining": False},
        {"release_id": release},
        {"target_id": target},
        {"task_type": "echo@1.2.0"},
        {"host_prefix": "worker-"},
        {"state": "all"},
    ]
    collections = {query(WorkerListQuery, values).cursor_collection() for values in variants}
    assert len(collections) == len(variants)
    assert all(item.startswith("workers:") for item in collections)
    # Page size and position never change what a cursor continues.
    assert WorkerListQuery(limit=3).cursor_collection() == WorkerListQuery().cursor_collection()


@pytest.mark.parametrize(
    "seconds,valid", [(3599, False), (3600, True), (86400, True), (2592000, True), (2592001, False)]
)
def test_retirement_threshold_is_one_hour_to_thirty_days(seconds, valid):
    from firefly_weave.contracts.worker_queries import RetireStaleRequest

    values = {"older_than_seconds": seconds, "dry_run": True}
    if valid:
        assert query(RetireStaleRequest, values).older_than_seconds == seconds
    else:
        with pytest.raises(ValidationError):
            query(RetireStaleRequest, values)


@pytest.mark.parametrize(
    "values",
    [
        {"older_than_seconds": 86400},
        {"dry_run": False},
        {"older_than_seconds": "86400", "dry_run": False},
        {"older_than_seconds": 86400, "dry_run": "false"},
        {"older_than_seconds": 86400, "dry_run": False, "worker_ids": []},
    ],
)
def test_retirement_states_both_its_threshold_and_whether_it_is_a_preview(values):
    from firefly_weave.contracts.worker_queries import RetireStaleRequest

    with pytest.raises(ValidationError):
        query(RetireStaleRequest, values)


def test_retirement_result_lists_at_most_one_batch():
    from firefly_weave.contracts.worker_queries import RetireStaleResult

    batch = [str(uuid4()) for _ in range(100)]
    result = query(RetireStaleResult, {"matched": 250, "revoked": 100, "worker_ids": batch})
    assert result.model_dump(mode="json") == {"matched": 250, "revoked": 100, "worker_ids": batch}
    for invalid in (
        {"matched": 250, "revoked": 100, "worker_ids": [*batch, str(uuid4())]},
        {"matched": -1, "revoked": 0, "worker_ids": []},
        {"matched": 0, "revoked": 101, "worker_ids": []},
    ):
        with pytest.raises(ValidationError):
            query(RetireStaleResult, invalid)


def test_release_usage_page_is_bounded_and_bound_to_release_and_target_scope():
    from firefly_weave.contracts.worker_queries import ReleaseUsageQuery

    assert ReleaseUsageQuery().limit == 100
    for limit in (0, 101):
        with pytest.raises(ValidationError):
            query(ReleaseUsageQuery, {"limit": limit})
    with pytest.raises(ValidationError):
        query(ReleaseUsageQuery, {"target_id": str(uuid4())})
    release, first, second = uuid4(), uuid4(), uuid4()
    usage = ReleaseUsageQuery()
    assert usage.cursor_collection(release, (first, second)) == usage.cursor_collection(release, (second, first))
    assert ReleaseUsageQuery(limit=1).cursor_collection(release, (first,)) == usage.cursor_collection(release, (first,))
    collections = {
        usage.cursor_collection(release, None),
        usage.cursor_collection(release, ()),
        usage.cursor_collection(release, (first,)),
        usage.cursor_collection(release, (first, second)),
        usage.cursor_collection(uuid4(), (first,)),
    }
    assert len(collections) == 5


@pytest.mark.parametrize(
    "position",
    [
        ("worker", "not-a-uuid"),
        (7, str(uuid4())),
        (None, str(uuid4())),
        ("bad name", str(uuid4())),
        ("w" * 101, str(uuid4())),
        ("worker", 7),
    ],
)
def test_release_usage_positions_are_typed_before_sql(position):
    from firefly_weave.contracts.worker_queries import release_usage_position

    with pytest.raises(ValueError):
        release_usage_position(position)


def test_release_usage_position_keeps_the_component_and_deployment():
    from firefly_weave.contracts.worker_queries import ReleaseUsagePosition, release_usage_position

    deployment = uuid4()
    assert release_usage_position(None) is None
    assert release_usage_position(("engine.workers", str(deployment))) == ReleaseUsagePosition(
        deployment, "engine.workers"
    )


def test_release_usage_shape_and_bounds():
    from firefly_weave.contracts.worker_queries import ReleaseUsage

    row = {
        "target_id": str(uuid4()),
        "target_name": "local",
        "deployment_id": str(uuid4()),
        "deployment_name": "weave",
        "component": "engine",
        "replicas": 3,
    }
    payload = {
        "workers": {"online": 1, "offline": 2, "draining": 0, "revoked": 4},
        "deployments": [row],
        "activations": 2,
        "next_cursor": None,
    }
    assert query(ReleaseUsage, payload).model_dump(mode="json") == payload
    for invalid in (
        {**payload, "deployments": [row] * 101},
        {**payload, "activations": -1},
        {**payload, "workers": {**payload["workers"], "online": -1}},
        {**payload, "deployments": [{**row, "replicas": 101}]},
        {**payload, "deployments": [{**row, "target_name": "../x"}]},
    ):
        with pytest.raises(ValidationError):
            query(ReleaseUsage, invalid)


def test_earlier_worker_status_bytes_are_kept_and_new_fields_are_optional():
    from firefly_weave.contracts.workers import WorkerPlacement

    plain = status()
    assert not {"placement", "drained_at"} & set(plain.model_dump(mode="json"))
    placement = WorkerPlacement(target_id=uuid4(), target_name="local", deployment_id=uuid4(), component="engine")
    drained = datetime(2026, 10, 11, 8, tzinfo=UTC)
    enriched = status(placement=placement, drained_at=drained)
    dumped = enriched.model_dump(mode="json")
    assert dumped["placement"] == placement.model_dump(mode="json")
    assert dumped["drained_at"] == "2026-10-11T08:00:00Z"
    assert WorkerStatus.model_validate_json(json.dumps(dumped)) == enriched
    # The stored instance and the registration answer never carry derived placement.
    stored = plain.model_dump(mode="json", include=set(WorkerInstance.model_fields))
    with pytest.raises(ValidationError):
        WorkerInstance.model_validate_json(json.dumps({**stored, "placement": dumped["placement"]}))


def test_placement_names_follow_deployment_names():
    from firefly_weave.contracts.deployments import Deployment
    from firefly_weave.contracts.workers import WorkerPlacement

    def pattern(schema, name):
        reference = schema["properties"][name]["$ref"].rsplit("/", 1)[-1]
        return schema["$defs"][reference]["pattern"]

    deployment_name = pattern(Deployment.model_json_schema(), "name")
    placement = WorkerPlacement.model_json_schema()
    assert pattern(placement, "target_name") == pattern(placement, "component") == deployment_name
    for name in ("../etc", "", "a" * 101, "a b"):
        with pytest.raises(ValidationError):
            WorkerPlacement(target_id=uuid4(), target_name=name, deployment_id=uuid4(), component="engine")


def test_worker_operations_declare_queries_commands_and_capabilities():
    from firefly_weave.contracts.openapi import export_openapi
    from firefly_weave.contracts.public import Page
    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.contracts.worker_queries import (
        ReleaseUsage,
        ReleaseUsageQuery,
        RetireStaleRequest,
        RetireStaleResult,
        WorkerListQuery,
    )

    spec = export_openapi()
    listed = OPERATIONS["workers.list"]
    assert (listed.query, listed.page, listed.response, listed.capability) == (
        WorkerListQuery,
        True,
        Page[WorkerStatus],
        "status.read",
    )
    parameters = spec["paths"][listed.canonical_path]["get"]["parameters"]
    assert [item["name"] for item in parameters if item["in"] == "query"] == list(WorkerListQuery.model_fields)
    retire = OPERATIONS["workers.retire_stale"]
    assert (retire.method, retire.canonical_path, retire.capability, retire.request, retire.response) == (
        "POST",
        ENVIRONMENT + "/workers/retire-stale",
        "release.retire",
        RetireStaleRequest,
        RetireStaleResult,
    )
    assert retire.idempotency and retire.query is None and not retire.page
    usage = OPERATIONS["releases.usage"]
    assert (usage.method, usage.canonical_path, usage.capability, usage.response, usage.query) == (
        "GET",
        ENVIRONMENT + "/worker-releases/{identifier}/usage",
        "catalog.read",
        ReleaseUsage,
        ReleaseUsageQuery,
    )
    assert usage.request is None and not usage.page
    parameters = spec["paths"][usage.canonical_path]["get"]["parameters"]
    assert [item["name"] for item in parameters if item["in"] == "query"] == ["limit", "cursor"]


def test_worker_query_schemas_are_exported():
    from firefly_weave.contracts.schema_export import export_schemas

    schemas = export_schemas()
    assert schemas["worker-list-query"]["properties"]["state"]["default"] == "current"
    assert schemas["worker-list-query"]["properties"]["order"]["default"] == "last_seen_desc"
    assert set(schemas["retire-stale-request"]["required"]) == {"older_than_seconds", "dry_run"}
    assert schemas["retire-stale-result"]["properties"]["worker_ids"]["maxItems"] == 100
    assert schemas["release-usage"]["properties"]["deployments"]["maxItems"] == 100
    assert "worker-placement" in schemas and "release-usage-query" in schemas
    assert not {"placement", "drained_at"} & set(schemas["worker-status"]["required"])
    assert {"placement", "drained_at"} <= set(schemas["worker-status"]["properties"])


async def test_sdk_sends_typed_worker_queries_and_commands():
    from firefly_weave.contracts.worker_queries import (
        ReleaseUsageQuery,
        RetireStaleRequest,
        WorkerListQuery,
    )
    from firefly_weave.sdk.client import WeaveClient

    calls = []
    release = uuid4()
    worker = status().model_dump(mode="json")

    def receive(request):
        calls.append(request)
        if request.url.path.endswith("/retire-stale"):
            assert json.loads(request.content) == {"older_than_seconds": 86400, "dry_run": True}
            assert request.headers["idempotency-key"] == "preview"
            return httpx.Response(200, json={"matched": 1, "revoked": 0, "worker_ids": [worker["id"]]})
        if request.url.path.endswith("/usage"):
            return httpx.Response(
                200,
                json={
                    "workers": {"online": 0, "offline": 1, "draining": 0, "revoked": 0},
                    "deployments": [],
                    "activations": 1,
                    "next_cursor": None,
                },
            )
        return httpx.Response(200, json={"items": [worker], "next_cursor": None})

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    async with WeaveClient(
        "https://platform.example", lambda: "token", scope, transport=httpx.MockTransport(receive)
    ) as client:
        page = await client.list_workers(
            query=WorkerListQuery(draining=False, presence="stale", state="all", host_prefix="w", limit=5)
        )
        assert page.items[0].id == UUID(worker["id"])
        await client.list_workers(limit=7, cursor="abc")
        with pytest.raises(ValueError):
            await client.list_workers(limit=7, query=WorkerListQuery())
        result = await client.retire_stale_workers(
            RetireStaleRequest(older_than_seconds=86400, dry_run=True), idempotency_key="preview"
        )
        assert (result.matched, result.revoked) == (1, 0)
        usage = await client.release_usage(release, query=ReleaseUsageQuery(limit=10))
        assert usage.activations == 1
        await client.release_usage(release)
    first, second, retire, usage_call, default_usage = calls
    assert dict(first.url.params) == {
        "draining": "false",
        "presence": "stale",
        "state": "all",
        "host_prefix": "w",
        "order": "last_seen_desc",
        "limit": "5",
    }
    assert dict(second.url.params) == {"limit": "7", "cursor": "abc"}
    assert retire.method == "POST" and retire.url.path.endswith(
        f"/environments/{scope.environment_id}/workers/retire-stale"
    )
    assert usage_call.url.path.endswith(f"/worker-releases/{release}/usage")
    assert dict(usage_call.url.params) == {"limit": "10"}
    assert dict(default_usage.url.params) == {"limit": "100"}
