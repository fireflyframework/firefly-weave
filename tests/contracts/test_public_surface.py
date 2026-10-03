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

"""Public transport acceptance, independent of server persistence fixtures."""

import importlib
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.access import Scope

SOURCE = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: public, version: 1.0.0}
spec:
  inputSchema: {}
  outputSchema: {}
  steps: []
  output: {literal: null}
"""


def client_type():
    return importlib.import_module("firefly_weave.sdk.client").WeaveClient


async def test_remote_and_local_validation_match():
    catalog = CatalogSnapshot.empty()
    local = compile_source(SOURCE, format="yaml", catalog=catalog, filename="editor.yaml")
    calls = []

    def receive(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["catalog"] == {"definitions": [], "tasks": [], "adapters": [], "schemas": {}}
        result = compile_source(
            payload["source"], format=payload["format"], catalog=catalog, filename=payload["filename"]
        )
        return httpx.Response(200, content=result.to_bytes())

    async with client_type()(
        "https://api.example",
        lambda: "access",
        Scope(tenant_id=uuid4(), project_id=uuid4()),
        transport=httpx.MockTransport(receive),
    ) as sdk:
        remote = await sdk.compile(source=SOURCE, format="yaml", catalog=catalog, filename="editor.yaml")
    assert remote.artifact.digest == local.artifact.digest
    assert remote.diagnostics == local.diagnostics
    assert remote.artifact.source_map == local.artifact.source_map
    assert calls[0].url.path.startswith("/api/v1/tenants/")
    assert sdk.closed


async def test_rotating_callable_and_typed_stale_problem():
    errors = importlib.import_module("firefly_weave.sdk.errors")
    credentials = iter(("first", "second"))
    received = []

    def receive(request):
        received.append(request.headers["authorization"])
        return httpx.Response(
            412,
            json={
                "status": 412,
                "code": "WV-ETAG",
                "message": "Revision precondition failed",
                "request_id": "request-1",
                "diagnostics": [],
            },
        )

    async with client_type()(
        "https://api.example",
        lambda: next(credentials),
        Scope(tenant_id=uuid4(), project_id=uuid4()),
        transport=httpx.MockTransport(receive),
    ) as sdk:
        for _ in range(2):
            with pytest.raises(errors.PreconditionFailed) as failure:
                await sdk.save_draft(uuid4(), {}, revision=1)
            assert failure.value.status == 412
            assert failure.value.code == "WV-ETAG"
            assert failure.value.request_id == "request-1"
    assert received == ["Bearer first", "Bearer second"]


async def test_mutation_timeout_is_not_retried():
    errors = importlib.import_module("firefly_weave.sdk.errors")
    calls = []

    def receive(request):
        calls.append(request)
        raise httpx.ReadTimeout("sensitive transport detail", request=request)

    async with client_type()(
        "https://api.example",
        lambda: "access",
        Scope(tenant_id=uuid4(), project_id=uuid4()),
        transport=httpx.MockTransport(receive),
    ) as sdk:
        with pytest.raises(errors.TransportError) as failure:
            await sdk.save_draft(uuid4(), {})
        assert "sensitive" not in str(failure.value)
    assert len(calls) == 1


def test_sync_context_rejected_clearly():
    with (
        pytest.raises(TypeError, match="async"),
        client_type()("https://api.example", lambda: "access", Scope(tenant_id=uuid4(), project_id=uuid4())),
    ):
        pass


@pytest.mark.parametrize("value", ['"1"', '"9999999999"', "1"])
def test_revision_parser_accepts_canonical_and_legacy(value):
    parse = importlib.import_module("firefly_weave.api.transport").parse_revision
    assert parse(value) == int(value.strip('"'))


@pytest.mark.parametrize("value", ['W/"1"', "*", '"1","2"', '"0"', "01", "-1", "1.0", " 1", "１"])
def test_revision_parser_rejects_ambiguous_preconditions(value):
    parse = importlib.import_module("firefly_weave.api.transport").parse_revision
    with pytest.raises(ValueError):
        parse(value)


def test_compile_wire_rejects_unknown_versions():
    wire = importlib.import_module("firefly_weave.contracts.public")
    value = json.loads(compile_source(SOURCE, format="yaml", catalog=CatalogSnapshot.empty()).to_bytes())
    assert value["artifact"] is not None
    value["artifact"]["executable"]["irVersion"] = "weave/future"
    with pytest.raises((ValidationError, ValueError)):
        wire.CompileResponse.model_validate_json(json.dumps(value))


def test_full_native_openapi_references_and_operation_identity():
    registry = importlib.import_module("firefly_weave.contracts.surface")
    spec = importlib.import_module("firefly_weave.contracts.openapi").export_openapi()
    operations = [
        value
        for path in spec["paths"].values()
        for key, value in path.items()
        if key in {"get", "put", "post", "delete"}
    ]
    assert len(operations) == len(registry.OPERATIONS)
    assert len({o["operationId"] for o in operations}) == len(operations)
    assert all(
        path.startswith("/api/v1/tenants/")
        or path.startswith("/health/")
        or path in {"/webhooks/{identifier}", "/provider-ingress/{identifier}"}
        or path in {"/admin/tenants", "/admin/grants", "/api/v1/identity", "/api/v1/client-configuration"}
        or path
        in {
            "/api/v1/admin/principals",
            "/api/v1/admin/principals/{identifier}/identity-links",
            "/api/v1/admin/principals/{identifier}/status",
        }
        for path in spec["paths"]
    )

    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = spec
                for component in value["$ref"].removeprefix("#/").split("/"):
                    target = target[component.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(spec)
    assert any(o["operationId"] == "compiler.validate" for o in operations)
    assert any(o["operationId"] == "drafts.retire" for o in operations)


async def test_debug_sdk_consumes_session_envelope_not_inner_view():
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    identifier = uuid4()
    document = {
        "id": str(identifier),
        "revision": 1,
        "created_at": now,
        "expires_at": now,
        "view": {
            "status": "running",
            "selected_node": None,
            "current_nodes": [],
            "active_nodes": [],
            "variables": {},
            "diagnostics": [],
            "events": [],
            "boundary": None,
            "boundaries": [],
            "now": now,
        },
    }
    async with client_type()(
        "https://api.example",
        lambda: "access",
        Scope(tenant_id=uuid4(), project_id=uuid4()),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=document)),
    ) as sdk:
        session = await sdk.inspect_debug(identifier)
    assert session.id == identifier and session.revision == 1 and session.view.status == "running"


def test_compiler_response_rejects_contradictory_flags():
    wire = importlib.import_module("firefly_weave.contracts.public")
    value = json.loads(compile_source(SOURCE, format="yaml", catalog=CatalogSnapshot.empty()).to_bytes())
    value["validationOk"] = False
    with pytest.raises(ValidationError):
        wire.CompileResponse.model_validate_json(json.dumps(value))


def test_native_contracts_describe_webhook_optional_header_and_debug_session():
    spec = importlib.import_module("firefly_weave.contracts.openapi").export_openapi()
    webhook = spec["paths"]["/webhooks/{identifier}"]["post"]
    headers = {p["name"]: p for p in webhook["parameters"] if p["in"] == "header"}
    assert headers["X-Weave-Event-ID"]["required"] is False
    response = next(value["post"] for path, value in spec["paths"].items() if path.endswith("/debug/sessions"))
    assert response["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("/DebugSession")
    assert "410" in response["responses"]


def test_hand_authored_schema_fixtures_and_native_discriminators():
    from pathlib import Path

    from jsonschema import Draft202012Validator, FormatChecker
    from pydantic import TypeAdapter

    from firefly_weave.contracts.openapi import export_openapi
    from firefly_weave.contracts.schema_export import contract_models, export_schemas

    models, schemas = contract_models(), export_schemas()
    for case in json.loads(Path("tests/fixtures/public/c6-wire.json").read_text()):
        validator = Draft202012Validator(schemas[case["schema"]], format_checker=FormatChecker())
        assert validator.is_valid(case["value"]) is case["valid"], case["schema"]
        model = models[case["schema"]]
        adapter = model if isinstance(model, TypeAdapter) else TypeAdapter(model)
        if case["valid"]:
            adapter.validate_json(json.dumps(case["value"]))
        else:
            with pytest.raises(ValidationError):
                adapter.validate_json(json.dumps(case["value"]))
    spec = export_openapi()
    discriminators = []

    def walk(value):
        if isinstance(value, dict):
            if "discriminator" in value:
                discriminators.append(value["discriminator"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(spec)
    assert discriminators
    for discriminator in discriminators:
        assert discriminator["propertyName"]
        for target in discriminator.get("mapping", {}).values():
            node = spec
            for name in target.removeprefix("#/").split("/"):
                node = node[name]


@pytest.mark.parametrize("unknown", ["weave/api-v2", "weave/unknown"])
async def test_sdk_rejects_wire_version_before_decoding_body(unknown):
    from firefly_weave.sdk.errors import ContractError

    async with client_type()(
        "https://api.example",
        lambda: "access",
        Scope(tenant_id=uuid4(), project_id=uuid4()),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={}, headers={"X-Weave-Wire-Version": unknown})
        ),
    ) as sdk:
        with pytest.raises(ContractError) as failure:
            await sdk.capabilities()
        assert failure.value.code == "WV-WIRE-VERSION"


def test_optional_empty_request_bodies_retain_schema_without_requiring_presence():
    from firefly_weave.contracts.openapi import export_openapi

    spec = export_openapi()
    optional = {"connections.test", "definitions.retire", "activations.retire"}
    found = set()
    for path in spec["paths"].values():
        for method, operation in path.items():
            if method not in {"get", "post", "put", "delete"} or "requestBody" not in operation:
                continue
            required = operation["requestBody"].get("required", False)
            if operation["operationId"] in optional:
                found.add(operation["operationId"])
                assert required is False, operation["operationId"]
                schema = operation["requestBody"]["content"]["application/json"]["schema"]
                assert schema["$ref"].endswith("/RetirementRequest")
            else:
                assert required is True, operation["operationId"]
    assert found == optional
