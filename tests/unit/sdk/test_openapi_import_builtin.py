# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""No-code OpenAPI import: built-in target, explicit relaxations, inventory, scaffolding and pruning."""

import copy
import hashlib
import json
import socket
from pathlib import Path

import pytest

from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.parser import parse_source
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.values import MAX_SAFE_INTEGER
from firefly_weave.sdk.http_actions import validate_http_action
from firefly_weave.sdk.openapi_import import import_openapi, init_policy, inventory, upgrade_openapi_30

FIXTURE = Path(__file__).parents[2] / "fixtures/openapi/petstore.yaml"
ALL = {"numericFormats": True, "ignoreResponseHeaders": True, "jsonMediaOnly": True, "defaultStringMaxLength": 128}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("OpenAPI import must never open a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def source():
    return FIXTURE.read_bytes()


def petstore():
    return parse_source(source(), format="yaml").value


def policy(relaxations=None, operations=("createPet", "deletePet", "listPets", "showPetById")):
    rules = {
        "operations": {
            "createPet": {
                "name": "create-pet",
                "sideEffect": "non_idempotent",
                "server": "https://api.petstore.test/v1",
                "statuses": [201],
            },
            "deletePet": {
                "name": "delete-pet",
                "sideEffect": "non_idempotent",
                "server": "https://api.petstore.test/v1",
                "statuses": [204],
            },
            "listPets": {
                "name": "list-pets",
                "sideEffect": "read_only",
                "server": "https://api.petstore.test/v1",
                "statuses": [200],
            },
            "showPetById": {
                "name": "show-pet",
                "sideEffect": "read_only",
                "server": "https://api.petstore.test/v1",
                "statuses": [200],
            },
        }
    }
    value = {
        "name": "pet-store",
        "version": "1.0.0",
        "auth": {"kind": "api-key", "header": "X-API-Key"},
        "operations": {k: v for k, v in rules["operations"].items() if k in operations},
    }
    if relaxations is not None:
        value["relaxations"] = relaxations
    return value


def codes(result):
    return [d.code for d in result.diagnostics]


def test_builtin_target_emits_weave_http_actions_and_a_handle_only_connection():
    result = import_openapi(source(), None, policy(ALL), source_format="yaml", target="builtin")
    assert result.ok, result.diagnostics
    assert result.connector is None and result.package is None
    by_name = {a["metadata"]["name"]: a for a in result.actions}
    assert set(by_name) == {"create-pet", "delete-pet", "list-pets", "show-pet"}
    for action in result.actions:
        spec = action["spec"]
        assert spec["implementation"]["uses"] == "weave-http@2.0.0"
        assert spec["connection"] == {"connector": "weave-http@2.0.0"}
        assert validate_http_action(action).ok
    assert by_name["list-pets"]["spec"]["implementation"]["action"] == "read"
    assert by_name["list-pets"]["spec"]["sideEffect"] == "read_only"
    assert by_name["create-pet"]["spec"]["implementation"]["action"] == "write"
    assert by_name["create-pet"]["spec"]["retry"] == {"maxAttempts": 1}
    assert by_name["show-pet"]["spec"]["implementation"]["config"]["path"] == "/v1/pets/{petId}"
    example = result.connection_example
    assert example["config"] == {
        "baseUrl": "https://api.petstore.test",
        "auth": {"kind": "api-key", "header": "X-API-Key"},
    }
    assert example["secretRef"] == {"api_key": "<operator secret handle for api_key>"}
    assert example["allowed_destinations"] == ["https://api.petstore.test"]
    assert result.provenance["target"] == "builtin" and result.provenance["connector"] == "weave-http@2.0.0"
    assert set(result.provenance["operations"]) == set(by_name)
    assert all(d.severity == "warning" for d in result.diagnostics)
    assert set(codes(result)) == {
        "WV-IMPORT-RELAXED_FORMAT",
        "WV-IMPORT-RELAXED_RESPONSE_HEADERS",
        "WV-IMPORT-RELAXED_MAX_LENGTH",
        "WV-IMPORT-RELAXED_MEDIA",
    }
    assert all(d.source is not None for d in result.diagnostics)
    dumped = result.model_dump_json()
    assert "Pet Store" not in dumped and "A page of pets" not in dumped


def test_package_target_stays_the_default():
    result = import_openapi(source(), None, policy(ALL), source_format="yaml")
    assert result.ok, result.diagnostics
    assert result.connector["spec"]["adapter"] == "pet-store" and result.package is not None
    assert result.actions[0]["spec"]["implementation"]["uses"] == "pet-store@1.0.0"
    assert result.connection_example is None and "connectionExample" not in result.model_dump(by_alias=True)
    assert "target" not in result.provenance


def test_policy_digest_of_existing_policies_is_unchanged():
    rules = policy(operations=("createPet", "deletePet"))
    result = import_openapi(source(), None, rules, source_format="yaml")
    assert result.ok, result.diagnostics
    assert result.provenance["policyDigest"] == hashlib.sha256(canonical_bytes(rules)).hexdigest()


def test_first_failure_by_default_and_every_failure_on_request():
    first = import_openapi(source(), None, policy(), source_format="yaml", target="builtin")
    assert not first.ok and len(first.diagnostics) == 1 and not first.actions
    every = import_openapi(source(), None, policy(), source_format="yaml", target="builtin", all_diagnostics=True)
    assert not every.ok and not every.actions
    found = {(d.code, d.path) for d in every.diagnostics}
    assert found == {
        ("WV-SCHEMA-UNSUPPORTED_FORMAT", "/paths/~1pets/get/parameters/0/schema/format"),
        ("WV-IMPORT-UNSUPPORTED", "/paths/~1pets/get/responses/200/headers"),
        ("WV-IMPORT-UNSUPPORTED", "/paths/~1pets~1{petId}/get/parameters/0"),
        ("WV-IMPORT-UNSUPPORTED", "/paths/~1pets~1{petId}/get/responses/200/content"),
    }
    by_path = {d.path: d for d in every.diagnostics}
    assert "defaultStringMaxLength" in by_path["/paths/~1pets~1{petId}/get/parameters/0"].hint
    assert "ignoreResponseHeaders" in by_path["/paths/~1pets/get/responses/200/headers"].hint
    assert "jsonMediaOnly" in by_path["/paths/~1pets~1{petId}/get/responses/200/content"].hint
    assert "numericFormats" in by_path["/paths/~1pets/get/parameters/0/schema/format"].hint
    assert len({d.message for d in every.diagnostics}) >= 3
    assert all(d.source is not None for d in every.diagnostics)


@pytest.mark.parametrize(
    "fmt,bounds",
    [("int32", (-(2**31), 2**31 - 1)), ("int64", (-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER)), ("float", None)],
)
def test_numeric_formats_become_bounds_with_a_warning(fmt, bounds):
    value = petstore()
    schema = value["paths"]["/pets"]["get"]["parameters"][0]["schema"]
    schema["format"] = fmt
    schema["type"] = "number" if fmt == "float" else "integer"
    if fmt == "float":
        value["paths"]["/pets"]["get"]["parameters"][0]["schema"] = {"type": "integer", "format": "int32"}
        value["components"]["schemas"]["Pet"]["properties"]["weight"] = {"type": "number", "format": fmt}
    relax = {"numericFormats": True, "ignoreResponseHeaders": True}
    result = import_openapi(value, ["listPets"], policy(relax, ("listPets",)), target="builtin")
    assert result.ok, result.diagnostics
    limit = result.actions[0]["spec"]["inputSchema"]["properties"]["query"]["properties"]["limit"]
    if bounds is not None:
        assert limit == {"type": "integer", "minimum": bounds[0], "maximum": bounds[1]}
    assert "format" not in json.dumps(result.actions[0]["spec"]["outputSchema"])
    assert "WV-IMPORT-RELAXED_FORMAT" in codes(result)


def test_yaml_and_json_sources_import_identically():
    as_json = json.dumps(petstore()).encode()
    from_yaml = import_openapi(source(), None, policy(ALL), source_format="yaml", target="builtin")
    from_json = import_openapi(as_json, None, policy(ALL), source_format="json", target="builtin")
    assert from_yaml.ok and from_json.ok
    assert from_yaml.actions == from_json.actions


def openapi_30():
    return {
        "openapi": "3.0.3",
        "info": {"title": "Legacy", "version": "1"},
        "servers": [{"url": "https://legacy.example.test"}],
        "paths": {
            "/things/{id}": {
                "get": {
                    "operationId": "getThing",
                    "parameters": [
                        {
                            "in": "path",
                            "name": "id",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 0, "exclusiveMinimum": True},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}},
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Thing": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "nullable": True, "example": "LEGACY-EXAMPLE-CANARY"},
                        "owner": {"$ref": "#/components/schemas/Owner", "nullable": True},
                    },
                },
                "Owner": {"type": "object", "properties": {"name": {"type": "string"}}},
            }
        },
    }


def legacy_policy(relax=None):
    value = {
        "name": "legacy",
        "version": "1.0.0",
        "auth": {"kind": "none"},
        "operations": {
            "getThing": {
                "name": "get-thing",
                "sideEffect": "read_only",
                "server": "https://legacy.example.test",
                "statuses": [200],
            }
        },
    }
    if relax:
        value["relaxations"] = relax
    return value


def test_openapi_30_is_upgraded_only_on_request():
    refused = import_openapi(openapi_30(), None, legacy_policy(), target="builtin")
    assert codes(refused) == ["WV-IMPORT-VERSION"] and "upgradeOpenapi30" in refused.diagnostics[0].hint
    result = import_openapi(openapi_30(), None, legacy_policy({"upgradeOpenapi30": True}), target="builtin")
    assert result.ok, result.diagnostics
    upgrade = next(d for d in result.diagnostics if d.code == "WV-IMPORT-RELAXED_OPENAPI_30")
    assert {r.path for r in upgrade.related} >= {
        "/components/schemas/Thing/properties/label/nullable",
        "/paths/~1things~1{id}/get/parameters/0/schema/exclusiveMinimum",
    }
    spec = result.actions[0]["spec"]
    assert spec["inputSchema"]["properties"]["path"]["properties"]["id"] == {"type": "integer", "exclusiveMinimum": 0}
    output = spec["outputSchema"]
    assert not validate_payload(output, {"status": 200, "body": {"label": None, "owner": None}}, {})
    assert validate_payload(output, {"status": 200, "body": {"label": 5}}, {})
    assert "LEGACY-EXAMPLE-CANARY" not in result.model_dump_json()


def test_upgrade_leaves_31_documents_and_the_input_untouched():
    original = openapi_30()
    snapshot = copy.deepcopy(original)
    upgraded, changed = upgrade_openapi_30(original)
    assert original == snapshot and upgraded["openapi"] == "3.1.0" and changed
    current = petstore()
    assert upgrade_openapi_30(current) == (current, [])


def test_inventory_lists_every_operation_with_a_verdict():
    listing = inventory(source(), source_format="yaml")
    assert listing.ok and listing.title == "Pet Store" and listing.servers == ["https://api.petstore.test/v1"]
    rows = {op.key: op for op in listing.operations}
    assert set(rows) == {"listPets", "createPet", "showPetById", "deletePet", "GET /health"}
    assert rows["createPet"].supported and rows["deletePet"].supported
    assert rows["createPet"].side_effect == "non_idempotent" and rows["createPet"].statuses == [201]
    assert rows["listPets"].summary == "List pets" and rows["listPets"].tags == ["pets"]
    assert not rows["listPets"].supported
    assert {r.code for r in rows["listPets"].reasons} == {"WV-SCHEMA-UNSUPPORTED_FORMAT", "WV-IMPORT-UNSUPPORTED"}
    assert [r.code for r in rows["GET /health"].reasons] == ["WV-IMPORT-OPERATION_ID"]
    relaxed = {op.key: op for op in inventory(source(), source_format="yaml", relaxations=ALL).operations}
    assert all(relaxed[key].supported for key in ("listPets", "createPet", "showPetById", "deletePet"))
    assert {r.severity for r in relaxed["listPets"].reasons} == {"warning"}


def test_inventory_reports_document_problems_once():
    listing = inventory(openapi_30())
    assert not listing.ok and [d.code for d in listing.diagnostics] == ["WV-IMPORT-VERSION"]
    assert all(not op.supported for op in listing.operations)
    assert inventory(openapi_30(), relaxations={"upgradeOpenapi30": True}).operations[0].supported
    remote = petstore()
    remote["components"]["schemas"]["Remote"] = {"$ref": "https://schemas.example.test/remote.json"}
    listing = inventory(remote)
    assert not listing.ok and [d.code for d in listing.diagnostics] == ["WV-IMPORT-REMOTE_REF"]
    credential = petstore()
    credential["servers"].append({"url": "https://user:INVENTORY-CANARY@api.petstore.test"})
    listing = inventory(credential)
    assert not listing.ok and "INVENTORY-CANARY" not in listing.model_dump_json()


def test_inventory_rejects_malformed_sources_without_operations():
    listing = inventory(b"openapi: [unclosed", source_format="yaml")
    assert not listing.ok and listing.operations == [] and listing.diagnostics[0].code.startswith("WV-PARSE-")


def test_init_policy_scaffolds_a_reviewable_policy():
    scaffold = init_policy(source(), source_format="yaml")
    assert scaffold.ok, scaffold.diagnostics
    assert scaffold.policy == {
        "name": "pet-store",
        "version": "1.0.0",
        "auth": {"kind": "api-key", "header": "X-API-Key"},
        "operations": {
            "createPet": {
                "name": "create-pet",
                "sideEffect": "non_idempotent",
                "server": "https://api.petstore.test/v1",
                "statuses": [201],
            },
            "deletePet": {
                "name": "delete-pet",
                "sideEffect": "non_idempotent",
                "server": "https://api.petstore.test/v1",
                "statuses": [204],
            },
        },
    }
    relaxed = init_policy(source(), source_format="yaml", relaxations=ALL)
    assert relaxed.ok and set(relaxed.policy["operations"]) == {"createPet", "deletePet", "listPets", "showPetById"}
    assert relaxed.policy["relaxations"] == ALL
    imported = import_openapi(source(), None, relaxed.policy, source_format="yaml", target="builtin")
    assert imported.ok, imported.diagnostics


def test_init_policy_keeps_explicit_selection_and_explains_it():
    scaffold = init_policy(source(), ["showPetById"], source_format="yaml")
    assert scaffold.ok and list(scaffold.policy["operations"]) == ["showPetById"]
    assert {d.severity for d in scaffold.diagnostics} == {"warning"}
    assert "WV-IMPORT-UNSUPPORTED" in {d.code for d in scaffold.diagnostics}
    unknown = init_policy(source(), ["missing"], source_format="yaml")
    assert not unknown.ok and unknown.diagnostics[0].code == "WV-IMPORT-POLICY"


def test_init_policy_refuses_mixed_authentication_in_one_policy():
    value = petstore()
    value["paths"]["/pets"]["post"]["security"] = []
    scaffold = init_policy(value, ["createPet", "deletePet"])
    assert not scaffold.ok and "different servers or authentication" in scaffold.diagnostics[0].message
    subset = init_policy(value)
    assert subset.ok and "WV-IMPORT-POLICY_SUBSET" in {d.code for d in subset.diagnostics}


def big_document(extra_bytes):
    value = petstore()
    value["components"]["schemas"]["Padding"] = {"type": "object", "description": "p" * extra_bytes}
    for i in range(50):
        value["paths"][f"/unused{i}"] = {
            "get": {
                "operationId": f"unused{i}",
                "responses": {"200": {"description": "u" * (extra_bytes // 50)}},
            }
        }
    return json.dumps(value).encode()


def test_large_documents_are_pruned_to_the_selection_after_a_full_credential_check():
    data = big_document(1_200_000)
    assert len(data) > 1_048_576
    result = import_openapi(data, None, policy(None, ("createPet", "deletePet")), target="builtin")
    assert result.ok, result.diagnostics
    assert "WV-IMPORT-PRUNED" in codes(result)
    assert {a["metadata"]["name"] for a in result.actions} == {"create-pet", "delete-pet"}
    value = json.loads(data)
    value["paths"]["/unused3"]["get"]["servers"] = [{"url": "https://user:PRUNE-CANARY@api.petstore.test"}]
    refused = import_openapi(json.dumps(value).encode(), None, policy(None, ("createPet",)), target="builtin")
    assert not refused.ok and codes(refused) == ["WV-IMPORT-CREDENTIAL_METADATA"]
    assert "PRUNE-CANARY" not in refused.model_dump_json()
    listing = inventory(data)
    assert listing.ok and {op.key for op in listing.operations if op.supported} >= {"createPet", "deletePet"}


def test_documents_beyond_the_local_ceiling_are_rejected():
    result = import_openapi(b" " * (8 * 1024 * 1024 + 1), None, policy(), target="builtin")
    assert not result.ok and codes(result) == ["WV-PARSE-SOURCE_LIMIT"]


def test_builtin_target_explains_a_policy_effect_the_method_cannot_have():
    rules = policy(ALL, ("listPets",))
    rules["operations"]["listPets"]["sideEffect"] = "non_idempotent"
    result = import_openapi(source(), None, rules, source_format="yaml", target="builtin")
    assert not result.ok and not result.actions
    problem = next(d for d in result.diagnostics if d.severity == "error")
    assert problem.code == "WV-IMPORT-POLICY" and problem.path == "/paths/~1pets/get"
    assert "read_only" in problem.hint and "WV-IMPORT-COMPILE" not in codes(result)
    assert import_openapi(source(), None, rules, source_format="yaml").ok


def crud_document(resources, *, credential=False):
    """A realistic CRUD API whose operations share schemas: small, but costly as one whole document."""
    schemas = {"Shared": {"type": "object", "properties": {f"s{i}": {"type": "string"} for i in range(40)}}}
    paths = {}
    for r in range(resources):
        properties = {f"p{i}": {"type": "string", "maxLength": 10} for i in range(20)}
        schemas[f"R{r}"] = {
            "type": "object",
            "properties": {**properties, "owner": {"$ref": "#/components/schemas/Shared"}},
        }
        ref = {"$ref": f"#/components/schemas/R{r}"}
        ok = {"description": "d", "content": {"application/json": {"schema": ref}}}
        paths[f"/r{r}"] = {
            "get": {"operationId": f"list{r}", "responses": {"200": ok}},
            "post": {
                "operationId": f"create{r}",
                "requestBody": {"content": {"application/json": {"schema": ref}}},
                "responses": {"201": ok},
            },
        }
        paths[f"/r{r}/{{id}}"] = {
            "parameters": [
                {"in": "path", "name": "id", "required": True, "schema": {"type": "string", "maxLength": 36}}
            ],
            "get": {"operationId": f"get{r}", "responses": {"200": ok}},
            "delete": {"operationId": f"delete{r}", "responses": {"204": {"description": "d"}}},
        }
    if credential:
        schemas["Leak"] = {"type": "object", "x-example-server": {"url": "https://user:CRUD-CANARY@a.test"}}
        paths["/r0"]["get"]["servers"] = [{"url": "https://a.test"}, {"url": "https://a.test/?api_key=CRUD-CANARY"}]
    return {
        "openapi": "3.1.0",
        "info": {"title": "Crud", "version": "1"},
        "servers": [{"url": "https://a.test"}],
        "paths": paths,
        "components": {"schemas": schemas},
    }


def crud_policy(*identifiers):
    effects = {"list": "read_only", "get": "read_only", "create": "non_idempotent", "delete": "non_idempotent"}
    operations = {}
    for identifier in identifiers:
        kind = identifier.rstrip("0123456789")
        statuses = {"create": [201], "delete": [204]}.get(kind, [200])
        operations[identifier] = {
            "name": identifier,
            "sideEffect": effects[kind],
            "server": "https://a.test",
            "statuses": statuses,
        }
    return {"name": "crud", "version": "1.0.0", "auth": {"kind": "none"}, "operations": operations}


def test_documents_over_the_whole_document_budget_are_pruned_to_the_selection():
    value = crud_document(120)
    assert len(json.dumps(value)) < 1_048_576
    listing = inventory(value)
    assert listing.ok, listing.diagnostics
    assert "WV-IMPORT-PRUNED" in {d.code for d in listing.diagnostics}
    assert sum(op.supported for op in listing.operations) == 480
    rules = crud_policy("list3", "create3", "get3", "delete3")
    result = import_openapi(value, None, rules, target="builtin")
    assert result.ok, result.diagnostics
    assert "WV-IMPORT-PRUNED" in codes(result) and len(result.actions) == 4
    # The package target keeps whole-document structural checks below the source budget.
    assert codes(import_openapi(value, None, rules)) == ["WV-IMPORT-RESOURCE_LIMIT"]
    refused = import_openapi(crud_document(120, credential=True), None, crud_policy("get5"), target="builtin")
    assert not refused.ok and codes(refused) == ["WV-IMPORT-CREDENTIAL_METADATA"]
    assert "CRUD-CANARY" not in refused.model_dump_json()
    hidden = inventory(crud_document(120, credential=True))
    assert not hidden.ok and "CRUD-CANARY" not in hidden.model_dump_json()


def test_inventory_validates_each_shared_schema_once(monkeypatch):
    from firefly_weave.sdk import openapi_import

    calls = []
    original = openapi_import.validate_schema

    def counted(schema, bundle):
        calls.append(1)
        return original(schema, bundle)

    monkeypatch.setattr(openapi_import, "validate_schema", counted)
    listing = inventory(crud_document(10))
    assert listing.ok and sum(op.supported for op in listing.operations) == 40
    # Ten resource schemas, the shared one and the parameter schemas, not once per operation and component.
    assert len(calls) < 120, len(calls)


def test_inventory_never_echoes_credentials_or_control_characters():
    value = petstore()
    ok = {"200": {"description": "d"}}
    value["paths"]["/pets?api_key=PATH-CANARY"] = {"get": {"operationId": "leaky", "responses": ok}}
    value["paths"]["/term\x1b[31mred"] = {"get": {"responses": ok}}
    value["paths"]["/ids"] = {"get": {"operationId": "bad\x1b]0;title\x07" + "x" * 300, "responses": ok}}
    value["paths"]["/c1"] = {
        "get": {"operationId": "c1", "summary": "a\x9b31mb", "tags": ["t\x1b", "ok"], "responses": ok}
    }
    value["servers"].append({"url": "https://api.petstore.test/\x1b[2J"})
    listing = inventory(value)
    dumped = listing.model_dump_json()
    assert "PATH-CANARY" not in dumped
    for control in ("\\u001b", "\\u0007", "\\u009b"):
        assert control not in dumped
    rows = {op.path: op for op in listing.operations}
    assert rows["*"].operation_id in {None, "leaky"} and all(len(op.key) <= 2100 for op in listing.operations)
    assert rows["/ids"].operation_id is None and rows["/ids"].key == "GET /ids"
    assert rows["/c1"].summary == "a31mb" and rows["/c1"].tags == ["t", "ok"]
    assert listing.servers == ["https://api.petstore.test/v1"]


def plain_http(document: bytes) -> bytes:
    return document.replace(b"https://api.petstore.test", b"http://api.petstore.test")


def test_builtin_import_accepts_a_plain_http_server():
    rules = json.loads(json.dumps(policy(ALL)).replace("https://api.petstore.test", "http://api.petstore.test"))
    result = import_openapi(plain_http(source()), None, rules, source_format="yaml", target="builtin")
    assert result.ok, result.diagnostics
    assert result.connection_example["config"]["baseUrl"] == "http://api.petstore.test"
    assert result.connection_example["allowed_destinations"] == ["http://api.petstore.test"]


def test_scaffolding_prefers_an_https_server_when_both_are_declared():
    both = source().replace(
        b"  - url: https://api.petstore.test/v1",
        b"  - url: http://api.petstore.test/v1\n  - url: https://api.petstore.test/v1",
    )
    scaffold = init_policy(both, source_format="yaml", relaxations=ALL)
    assert scaffold.ok, scaffold.diagnostics
    assert {rule["server"] for rule in scaffold.policy["operations"].values()} == {"https://api.petstore.test/v1"}
    only_http = init_policy(plain_http(source()), source_format="yaml", relaxations=ALL)
    assert only_http.ok, only_http.diagnostics
    assert {rule["server"] for rule in only_http.policy["operations"].values()} == {"http://api.petstore.test/v1"}
