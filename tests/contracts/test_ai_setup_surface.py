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

"""The AI discovery, readiness and setup wire contracts as the SDK sends and decodes them."""

import json
from uuid import uuid4

import httpx
import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.ai import (
    AIConnectionTestRequest,
    AIConnectionTestResult,
    AIEndpointsResult,
    AIModelsResult,
    AIReadinessResult,
    AISetupGrantRequest,
    AISetupGrantResult,
    AISetupPublishResult,
)
from firefly_weave.contracts.bindable_connections import BindableConnection, BindableConnectionQuery
from firefly_weave.contracts.catalog import RetirementRequest
from firefly_weave.contracts.public import Page
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.sdk.client import WeaveClient
from firefly_weave.sdk.errors import ContractError, WeaveError

ENV = "/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}"

# Method, canonical path, capability, success statuses, request and query contract of each operation.
OPERATION_WIRE = {
    "ai_endpoints.list": ("GET", ENV + "/ai/endpoints", "catalog.read", (200,), None, None),
    "ai_models.list": ("GET", ENV + "/ai/models", "catalog.read", (200,), None, "AIModelsQuery"),
    "ai_readiness.read": ("GET", ENV + "/ai/readiness", "catalog.read", (200,), None, None),
    "ai_setup.publish": (
        "POST",
        ENV + "/ai/setup/publish",
        "definition.publish",
        (200,),
        "RetirementRequest",
        None,
    ),
    "ai_setup.grant": ("POST", ENV + "/ai/setup/grant", "connection.manage", (200,), "AISetupGrantRequest", None),
    "bindable_connections.list": (
        "GET",
        ENV + "/bindable-connections",
        "connection.bind",
        (200,),
        None,
        "BindableConnectionQuery",
    ),
    "ai_connections.test": (
        "POST",
        ENV + "/ai/connections/{identifier}/test",
        "connection.manage",
        (200,),
        "AIConnectionTestRequest",
        None,
    ),
}

# Every wire field of each payload, with the ones a reader can rely on being present.
WIRE_FIELDS = {
    "AIEndpointsResult": ({"policy", "endpoints"}, {"policy"}),
    "AIEndpoint": (
        {"id", "label", "url", "providers", "compat", "credential", "models", "context_tokens", "development_only"},
        {"id", "label", "url", "providers", "compat", "credential", "models", "context_tokens", "development_only"},
    ),
    "AIModelsQuery": ({"connection", "refresh"}, {"connection"}),
    "AIModelsResult": ({"approval", "discovery", "discovered_at", "models"}, {"approval", "discovery"}),
    "AIModel": (
        {"name", "approved", "available", "tools", "context_tokens", "size_bytes", "family"},
        {"name", "approved", "available"},
    ),
    "AIReadinessResult": ({"items"}, {"items"}),
    "AIReadinessItem": ({"id", "status", "detail", "fix"}, {"id", "status", "detail"}),
    "AIReadinessFix": ({"kind", "label", "operation", "command", "route"}, {"kind", "label"}),
    "AISetupPublishResult": ({"connector", "actions"}, {"connector", "actions"}),
    "AISetupGrantRequest": ({"connection_revision_id"}, {"connection_revision_id"}),
    "AISetupGrantResult": ({"granted", "not_needed"}, {"granted"}),
    "BindableConnectionQuery": ({"connector", "limit", "cursor"}, set()),
    "BindableConnection": (
        {"connection_id", "revision_id", "name", "connector", "label", "enabled", "provider", "endpoint_origin"},
        {"connection_id", "revision_id", "name", "connector", "label", "enabled"},
    ),
    "AIConnectionTestRequest": ({"model", "probe_tools"}, {"model"}),
    "AIConnectionTestResult": (
        {"ok", "code", "latency_ms", "model", "tool_calling", "context_tokens"},
        {"ok", "code", "model"},
    ),
}


def scope():
    return Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


def schema_fields(model):
    schema = model.model_json_schema(mode="validation")
    names = {model.__name__: schema, **schema.get("$defs", {})}
    return {name: (set(value.get("properties", {})), set(value.get("required", ()))) for name, value in names.items()}


@pytest.mark.parametrize("operation", sorted(OPERATION_WIRE))
def test_operations_keep_their_registered_wire(operation):
    method, path, capability, statuses, request, query = OPERATION_WIRE[operation]
    row = OPERATIONS[operation]
    assert (row.method, row.canonical_path, row.capability, row.statuses) == (method, path, capability, statuses)
    assert (row.request.__name__ if row.request is not None else None) == request
    assert (row.query.__name__ if row.query is not None else None) == query
    assert row.idempotency is False and row.revision != "required"


@pytest.mark.parametrize(
    "model",
    [
        AIEndpointsResult,
        AIModelsResult,
        AIReadinessResult,
        AISetupPublishResult,
        AISetupGrantRequest,
        AISetupGrantResult,
        AIConnectionTestRequest,
        AIConnectionTestResult,
        Page[BindableConnection],
        OPERATIONS["ai_models.list"].query,
        BindableConnectionQuery,
    ],
    ids=lambda model: model.__name__,
)
def test_payload_fields_and_required_fields_do_not_drift(model):
    checked = {name: fields for name, fields in schema_fields(model).items() if name in WIRE_FIELDS}
    assert checked, model.__name__
    for name, fields in checked.items():
        assert fields == WIRE_FIELDS[name], name


def test_existing_connection_test_defaults_are_unchanged():
    request = AIConnectionTestRequest(model="qwen3:4b")
    assert request.model_dump(mode="json") == {"model": "qwen3:4b", "probe_tools": True}
    result = AIConnectionTestResult.model_validate_json('{"ok":true,"code":"ok","model":"qwen3:4b"}')
    assert (result.latency_ms, result.tool_calling, result.context_tokens) == (None, "unknown", None)
    query = OPERATIONS["ai_models.list"].query.model_validate_json(json.dumps({"connection": str(uuid4())}))
    assert query.refresh is False
    assert OPERATIONS["bindable_connections.list"].query.model_validate_json("{}").limit == 50


class Recorder:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.requests = payload, status, []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)


def client(recorder, selected=None):
    return WeaveClient(
        "https://api.example", lambda: "token", selected or scope(), transport=httpx.MockTransport(recorder)
    )


async def test_cached_models_keep_viewer_wire_defaults():
    received = []

    def answer(request):
        received.append(request)
        return httpx.Response(
            200, json={"approval": "unknown", "discovery": "unknown", "discovered_at": None, "models": []}
        )

    async with WeaveClient(
        "https://api.example",
        lambda: "token",
        Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        transport=httpx.MockTransport(answer),
    ) as client:
        result = await client.invoke("ai_models.list", query={"connection": str(uuid4())})
    assert isinstance(result, AIModelsResult)
    assert result.approval == "unknown" and result.models == []
    assert "refresh" not in received[0].url.params


async def test_model_refresh_and_full_metadata_round_trip():
    revision, selected = uuid4(), scope()
    payload = {
        "approval": "served",
        "discovery": "ok",
        "discovered_at": "2026-10-11T10:00:00Z",
        "models": [
            {
                "name": "qwen3:4b",
                "approved": True,
                "available": True,
                "tools": "yes",
                "context_tokens": 8192,
                "size_bytes": 2500000000,
                "family": "qwen3",
            }
        ],
    }
    recorder = Recorder(payload)
    async with client(recorder, selected) as weave:
        result = await weave.invoke("ai_models.list", query={"connection": str(revision), "refresh": "true"})
    request = recorder.requests[0]
    assert request.method == "GET" and request.headers["authorization"] == "Bearer token"
    assert request.url.path == (
        f"/api/v1/tenants/{selected.tenant_id}/projects/{selected.project_id}"
        f"/environments/{selected.environment_id}/ai/models"
    )
    assert dict(request.url.params) == {"connection": str(revision), "refresh": "true"}
    assert result.model_dump(mode="json") == {**payload, "discovered_at": "2026-10-11T10:00:00Z"}


@pytest.mark.parametrize(
    "payload",
    [
        {"approval": "unknown", "discovery": "unknown", "models": [], "secret": "x"},
        {"approval": "maybe", "discovery": "unknown", "models": []},
        {"approval": "listed", "discovery": "ok", "models": [{"name": "m", "approved": True}]},
        {
            "approval": "listed",
            "discovery": "ok",
            "models": [{"name": "m", "approved": True, "available": True, "tools": "maybe"}],
        },
    ],
    ids=["unknown-field", "unknown-approval", "missing-availability", "unknown-tool-support"],
)
async def test_model_answers_outside_the_contract_are_rejected(payload):
    async with client(Recorder(payload)) as weave:
        with pytest.raises(ContractError):
            await weave.invoke("ai_models.list", query={"connection": str(uuid4())})


async def test_endpoint_and_readiness_answers_decode_without_optional_fields():
    endpoints = {
        "policy": "loaded",
        "endpoints": [
            {
                "id": "ollama-local",
                "label": "Ollama",
                "url": "http://ollama:11434/v1",
                "providers": ["openai-chat"],
                "compat": "ollama",
                "credential": "none",
                "models": "served",
                "context_tokens": 8192,
                "development_only": True,
            }
        ],
    }
    async with client(Recorder(endpoints)) as weave:
        result = await weave.invoke("ai_endpoints.list")
    assert isinstance(result, AIEndpointsResult) and result.model_dump(mode="json") == endpoints
    ids = [
        "worker_installed",
        "actions_published",
        "worker_online",
        "connection",
        "key_available",
        "connection_authorized",
        "model_responds",
        "weave_ai",
    ]
    readiness = {"items": [{"id": name, "status": "unknown", "detail": "requires_viewer"} for name in ids]}
    async with client(Recorder(readiness)) as weave:
        checklist = await weave.invoke("ai_readiness.read")
    assert isinstance(checklist, AIReadinessResult) and [item.id for item in checklist.items] == ids
    readiness["items"].reverse()
    async with client(Recorder(readiness)) as weave:
        with pytest.raises(ContractError):
            await weave.invoke("ai_readiness.read")


async def test_setup_requests_send_only_their_typed_bodies():
    def version(kind, name):
        return {
            "id": str(uuid4()),
            "kind": kind,
            "name": name,
            "version": "1.0.0",
            "digest": "a" * 64,
            "definition_digest": "b" * 64,
        }

    connector = version("Connector", "weave-agentic-provider")
    published = {"connector": connector, "actions": [version("Action", "weave-agentic-generate")]}
    recorder = Recorder(published)
    async with client(recorder) as weave:
        with pytest.raises(ValueError, match="typed request"):
            await weave.invoke("ai_setup.publish")
        result = await weave.invoke("ai_setup.publish", body=RetirementRequest())
    assert isinstance(result, AISetupPublishResult) and str(result.connector.id) == connector["id"]
    assert len(recorder.requests) == 1 and json.loads(recorder.requests[0].content) == {}
    assert "idempotency-key" not in recorder.requests[0].headers
    revision = uuid4()
    recorder = Recorder({"granted": True})
    async with client(recorder) as weave:
        granted = await weave.invoke("ai_setup.grant", body=AISetupGrantRequest(connection_revision_id=revision))
    assert granted == AISetupGrantResult(granted=True, not_needed=False)
    assert json.loads(recorder.requests[0].content) == {"connection_revision_id": str(revision)}


async def test_bindable_pages_send_no_limit_unless_asked_and_carry_no_configuration():
    revision = str(uuid4())
    item = {
        "connection_id": revision,
        "revision_id": revision,
        "name": "ollama-local",
        "connector": "weave-agentic-provider@1.0.0",
        "label": "ollama-local",
        "enabled": True,
    }
    recorder = Recorder({"items": [item], "next_cursor": None})
    async with client(recorder) as weave:
        page = await weave.invoke("bindable_connections.list")
        await weave.invoke(
            "bindable_connections.list", query={"connector": "weave-agentic-provider@1.0.0", "limit": 50}
        )
    assert dict(recorder.requests[0].url.params) == {}
    assert dict(recorder.requests[1].url.params) == {"connector": "weave-agentic-provider@1.0.0", "limit": "50"}
    assert page.items[0].connection_id == page.items[0].revision_id
    for extra in ({"config": {"endpoint": "x"}}, {"secretRef": {"apiKey": "handle"}}):
        async with client(Recorder({"items": [{**item, **extra}], "next_cursor": None})) as weave:
            with pytest.raises(ContractError):
                await weave.invoke("bindable_connections.list")


@pytest.mark.parametrize(
    "operation,status,code",
    [
        ("ai_setup.publish", 422, "WV-AI-WORKER-MISSING"),
        ("ai_endpoints.list", 503, "WV-AI-POLICY"),
        ("ai_models.list", 429, "WV-AI-RATE-LIMITED"),
    ],
)
async def test_refusals_keep_their_problem_codes(operation, status, code):
    problem = {"status": status, "code": code, "message": "Refused"}
    arguments = {
        "ai_setup.publish": {"body": RetirementRequest()},
        "ai_endpoints.list": {},
        "ai_models.list": {"query": {"connection": str(uuid4()), "refresh": "true"}},
    }[operation]
    async with client(Recorder(problem, status)) as weave:
        with pytest.raises(WeaveError) as refused:
            await weave.invoke(operation, **arguments)
    assert (refused.value.status, refused.value.code) == (status, code)
