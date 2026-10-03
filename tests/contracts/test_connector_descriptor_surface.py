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

"""Public contracts for connector discovery, connection idempotency and HTTP profile schemas."""

import json

import pytest
from jsonschema import Draft202012Validator

from firefly_weave.contracts.openapi import export_openapi
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.surface import OPERATIONS

PROJECT = "/api/v1/tenants/{tenant}/projects/{project}"


def resolve(spec, value):
    while "$ref" in value:
        node = spec
        for part in value["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        value = node
    return value


def test_descriptor_operations_are_catalog_reads_on_exact_paths():
    assert OPERATIONS["connector_descriptors.list"].canonical_path == PROJECT + "/connector-descriptors"
    assert OPERATIONS["connector_descriptors.read"].canonical_path == PROJECT + "/connector-descriptors/{adapter}"
    for identifier in ("connector_descriptors.list", "connector_descriptors.read"):
        operation = OPERATIONS[identifier]
        assert operation.method == "GET" and operation.capability == "catalog.read" and not operation.public
    ordered = list(OPERATIONS)
    # First-match consumers (telemetry labels, the Studio bridge) see the static path first.
    assert ordered.index("connector_descriptors.list") < ordered.index("definitions.list")


def test_openapi_documents_bounded_adapter_paging_and_bearer_security():
    spec = export_openapi()
    listing = spec["paths"][PROJECT + "/connector-descriptors"]["get"]
    single = spec["paths"][PROJECT + "/connector-descriptors/{adapter}"]["get"]
    assert listing["operationId"] == "connector_descriptors.list"
    assert single["operationId"] == "connector_descriptors.read"
    assert listing["security"] == single["security"] == [{"bearer": []}]
    assert {p["name"] for p in listing["parameters"] if p["in"] == "query"} == {"limit", "cursor"}
    adapter = next(p for p in single["parameters"] if p["name"] == "adapter")
    schema = resolve(spec, adapter["schema"])
    assert adapter["required"] is True and schema["pattern"] == r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    response = single["responses"]["200"]["content"]["application/json"]["schema"]
    assert response["$ref"].endswith("/ConnectorDescriptorView")
    assert "Required capability: catalog.read" in single["description"]


def test_connection_create_documents_an_optional_idempotency_key():
    spec = export_openapi()
    create = spec["paths"][OPERATIONS["connections.create"].canonical_path]["post"]
    header = next(p for p in create["parameters"] if p["name"] == "Idempotency-Key")
    assert header["in"] == "header" and header["required"] is False
    publish = spec["paths"][OPERATIONS["definitions.publish"].canonical_path]["post"]
    assert next(p for p in publish["parameters"] if p["name"] == "Idempotency-Key")["required"] is True


VALID = {
    "http-operation": {
        "profileVersion": "2.0.0",
        "method": "GET",
        "path": "/v1/pets/{petId}",
        "sideEffect": "read_only",
        "parameters": [{"name": "petId", "location": "path", "type": "string", "required": True}],
        "statuses": [200],
    },
    "http-profile-connection": {
        "baseUrl": "https://api.example.com",
        "auth": {"kind": "api-key", "header": "X-API-Key"},
    },
    "auth-profile": {
        "kind": "machine-token",
        "client_id": "client",
        "endpoint": "https://auth.example.com/token",
        "scopes": ["pets.read"],
    },
}
INVALID = {
    "http-operation": {"foo": 1},
    "http-profile-connection": {"baseUrl": "https://api.example.com", "auth": {"kind": "oauth"}},
    "auth-profile": {"kind": "api-key", "header": "X-API-Key", "password": "inline"},
}


@pytest.mark.parametrize("name", sorted(VALID))
def test_http_profile_authoring_schemas_are_exported(name):
    schemas = export_schemas()
    validator = Draft202012Validator(schemas[name])
    assert validator.is_valid(VALID[name])
    assert not validator.is_valid(INVALID[name])


def test_descriptor_and_import_policy_schemas_are_exported():
    from firefly_weave.contracts.connector_descriptors import descriptor_view
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR

    schemas = export_schemas()
    view = descriptor_view(HTTP_PROFILE_DESCRIPTOR, None).model_dump(mode="json", by_alias=True)
    Draft202012Validator(schemas["connector-descriptor"]).validate(json.loads(json.dumps(view)))
    assert {"name", "version", "auth", "operations"} <= set(schemas["openapi-import-policy"]["required"])
