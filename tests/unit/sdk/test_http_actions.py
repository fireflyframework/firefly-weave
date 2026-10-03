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
"""No-code HTTP actions on weave-http@2.0.0: locked effects, bounded inputs, leak-free inference."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.sdk import http_actions
from firefly_weave.sdk.http_actions import (
    HttpActionError,
    author_http_action,
    build_connection_request,
    build_http_action,
    check_http_action,
    infer_schema,
)

CANARY = "SAMPLE-VALUE-CANARY-7731"


@pytest.fixture
def local_rules(monkeypatch):
    """Exercise the local mirror, as in a build whose descriptor has no Action checks."""
    monkeypatch.setattr(http_actions, "_server_hook", lambda: None)


def get_pet(**changes):
    request = {
        "name": "get-pet",
        "version": "1.0.0",
        "method": "GET",
        "pathTemplate": "/v1/pets/{petId}",
        "parameters": [
            {"name": "petId", "location": "path", "type": "string"},
            {"name": "limit", "location": "query", "type": "integer"},
        ],
        "responseSample": {"id": 7, "name": CANARY, "tags": [{"label": CANARY}], "owner": None},
        "statuses": [200],
    }
    request.update(changes)
    return request


def codes(error):
    return {d.code for d in error.value.diagnostics}


def test_read_action_is_locked_read_only_and_compiles():
    result = author_http_action(get_pet())
    assert result.ok, result.diagnostics
    assert result.compiled
    action = result.action
    spec = action["spec"]
    assert spec["implementation"]["uses"] == "weave-http@2.0.0"
    assert spec["implementation"]["action"] == "read"
    assert spec["sideEffect"] == "read_only"
    assert spec["implementation"]["config"]["sideEffect"] == "read_only"
    assert spec["implementation"]["config"]["profileVersion"] == "2.0.0"
    assert spec["connection"] == {"connector": "weave-http@2.0.0"}
    assert "retry" not in spec
    path = spec["inputSchema"]["properties"]["path"]
    assert path["required"] == ["petId"]
    assert path["properties"]["petId"] == {"type": "string", "minLength": 1, "maxLength": 256}
    assert "path" in spec["inputSchema"]["required"]
    assert "query" not in spec["inputSchema"].get("required", [])
    assert CANARY not in json.dumps(action)
    output = spec["outputSchema"]
    assert not validate_payload(output, {"status": 200, "body": {"id": 1, "name": "x", "tags": [], "owner": None}}, {})
    assert validate_payload(output, {"status": 201, "body": {"id": 1, "name": "x", "tags": [], "owner": None}}, {})


def test_write_action_is_non_idempotent_without_retries_and_infers_body():
    result = author_http_action(
        {
            "name": "create-pet",
            "method": "POST",
            "pathTemplate": "/v1/pets",
            "bodySample": {"name": CANARY, "age": 3},
            "statuses": [201],
        }
    )
    assert result.ok, result.diagnostics
    spec = result.action["spec"]
    assert spec["implementation"]["action"] == "write"
    assert spec["sideEffect"] == "non_idempotent"
    assert spec["retry"] == {"maxAttempts": 1}
    assert spec["inputSchema"]["required"] == ["body"]
    assert spec["inputSchema"]["properties"]["body"]["properties"]["age"] == {"type": "integer"}
    assert CANARY not in json.dumps(result.action)


@pytest.mark.parametrize("field,value", [("sideEffect", "read_only"), ("retry", {"maxAttempts": 3})])
def test_authors_cannot_choose_side_effect_or_retries(field, value):
    result = author_http_action(
        {"name": "create-pet", "method": "POST", "pathTemplate": "/v1/pets", "statuses": [201], field: value}
    )
    assert not result.ok and result.action is None
    assert {d.code for d in result.diagnostics} == {"WV-HTTP-ACTION-REQUEST"}


def test_missing_path_parameter_is_added_required_and_extra_is_rejected():
    result = author_http_action(get_pet(parameters=[]))
    assert result.ok, result.diagnostics
    assert "WV-HTTP-ACTION-PATH_PARAMETER_ADDED" in {d.code for d in result.diagnostics}
    config = result.action["spec"]["implementation"]["config"]
    assert config["parameters"] == [
        {"name": "petId", "location": "path", "type": "string", "array": False, "required": True}
    ]
    with pytest.raises(HttpActionError) as error:
        build_http_action(get_pet(parameters=[{"name": "other", "location": "path", "type": "string"}]))
    assert "WV-HTTP-ACTION-PARAMETER" in codes(error)


@pytest.mark.parametrize(
    "header", ["Authorization", "content-type", "X-Forwarded-For", "Proxy-Token", "Host", "Cookie", "Accept"]
)
def test_protected_headers_are_rejected(header):
    with pytest.raises(HttpActionError) as error:
        build_http_action(get_pet(parameters=[{"name": header, "location": "header", "type": "string"}]))
    assert "WV-HTTP-ACTION-PROTECTED_HEADER" in codes(error)


def test_auth_header_cannot_be_a_parameter():
    request = get_pet(
        parameters=[{"name": "x-api-key", "location": "header", "type": "string"}],
        auth={"kind": "api-key", "header": "X-API-Key"},
    )
    with pytest.raises(HttpActionError) as error:
        build_http_action(request)
    assert "WV-HTTP-ACTION-PROTECTED_HEADER" in codes(error)


def test_arrays_are_query_only_and_bounded():
    result = author_http_action(
        get_pet(parameters=[{"name": "tag", "location": "query", "type": "string", "array": True, "maxLength": 12}])
    )
    assert result.ok, result.diagnostics
    tag = result.action["spec"]["inputSchema"]["properties"]["query"]["properties"]["tag"]
    assert tag == {"type": "array", "items": {"type": "string", "maxLength": 12}, "maxItems": 20}
    with pytest.raises(HttpActionError) as error:
        build_http_action(
            get_pet(parameters=[{"name": "X-Tag", "location": "header", "type": "string", "array": True}])
        )
    assert "WV-HTTP-ACTION-PARAMETER" in codes(error)


def test_string_bounds_are_configurable_and_capped():
    result = author_http_action(get_pet(stringMaxLength=64))
    assert result.action["spec"]["inputSchema"]["properties"]["path"]["properties"]["petId"]["maxLength"] == 64
    assert not author_http_action(get_pet(stringMaxLength=5000)).ok


def test_body_is_rejected_for_reads_and_schema_sources_are_exclusive():
    with pytest.raises(HttpActionError) as error:
        build_http_action(get_pet(bodySample={"a": 1}))
    assert "WV-HTTP-ACTION-BODY" in codes(error)
    both = author_http_action(get_pet(responseSchema={"type": "object"}))
    assert not both.ok and {d.code for d in both.diagnostics} == {"WV-HTTP-ACTION-REQUEST"}


def test_invalid_response_schema_is_reported_with_pointer():
    with pytest.raises(HttpActionError) as error:
        build_http_action(get_pet(responseSample=None, responseSchema={"type": "object", "format": "int32"}))
    diagnostic = error.value.diagnostics[0]
    assert diagnostic.path.startswith("/responseSchema")


def test_head_and_no_content_statuses_are_empty():
    result = author_http_action(get_pet(method="HEAD", responseSample=None, statuses=[200, 204]))
    assert result.ok, result.diagnostics
    config = result.action["spec"]["implementation"]["config"]
    assert config["emptyStatuses"] == [200, 204]
    output = result.action["spec"]["outputSchema"]
    assert not validate_payload(output, {"status": 204, "body": None}, {})
    deleted = author_http_action(
        {"name": "delete-pet", "method": "DELETE", "pathTemplate": "/v1/pets/{petId}", "statuses": [200, 204]}
    )
    assert deleted.ok, deleted.diagnostics
    assert deleted.action["spec"]["implementation"]["config"]["emptyStatuses"] == [204]


def test_inference_types_nesting_and_required_keys():
    schema, truncated = infer_schema({"a": 1, "b": 1.5, "c": True, "d": None, "e": "x", "f": [{"g": []}], "h": {}})
    assert not truncated
    assert schema == {
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "number"},
            "c": {"type": "boolean"},
            "d": {"type": "null"},
            "e": {"type": "string"},
            "f": {
                "type": "array",
                "items": {"type": "object", "properties": {"g": {"type": "array"}}, "required": ["g"]},
            },
            "h": {"type": "object"},
        },
        "required": ["a", "b", "c", "d", "e", "f", "h"],
    }
    assert "additionalProperties" not in json.dumps(schema)


def test_inference_never_copies_values_or_data_keys():
    sample = {
        "users": {
            "3f0e3c4e-1b9a-4f43-9a8c-0b1d2e3f4a5b": {"email": CANARY},
            "6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d": {"email": CANARY},
        },
        "byEmail": {"person@example.com": 1},
        "secret": CANARY,
    }
    schema, _ = infer_schema(sample)
    text = json.dumps(schema)
    assert CANARY not in text and "3f0e3c4e" not in text and "example.com" not in text
    users = schema["properties"]["users"]
    assert users == {
        "type": "object",
        "additionalProperties": {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]},
    }


def test_inference_is_bounded():
    deep = current = {}
    for _ in range(40):
        current["n"] = {}
        current = current["n"]
    schema, truncated = infer_schema(deep)
    assert truncated
    assert len(json.dumps(schema)) < 2000
    wide, truncated = infer_schema({f"k{i}": {f"j{j}": j for j in range(100)} for i in range(100)})
    assert truncated and len(json.dumps(wide)) < 200000
    many, truncated = infer_schema({f"k{i}": i for i in range(5000)})
    # Only the first map values are examined, and the result says so.
    assert many == {"type": "object", "additionalProperties": {"type": "integer"}} and truncated
    few, truncated = infer_schema({str(i): i for i in range(90)})
    assert few == many and not truncated


@pytest.mark.parametrize(
    "tamper,code",
    [
        (lambda s: s["implementation"].update(config={"foo": 1}), "WV-HTTP-ACTION-CONFIG"),
        (
            lambda s: s["implementation"]["config"].update(method="POST", sideEffect="non_idempotent"),
            "WV-HTTP-ACTION-SIDE_EFFECT",
        ),
        (lambda s: s.update(sideEffect="idempotent"), "WV-HTTP-ACTION-SIDE_EFFECT"),
        (lambda s: s["inputSchema"]["properties"]["path"]["properties"].pop("petId"), "WV-HTTP-ACTION-INPUT"),
        (lambda s: s["inputSchema"]["properties"]["path"].update(required=[]), "WV-HTTP-ACTION-INPUT"),
        (lambda s: s["inputSchema"]["properties"].update(body={"type": "object"}), "WV-HTTP-ACTION-INPUT"),
        (lambda s: s["implementation"].update(uses="other@1.0.0"), "WV-HTTP-ACTION-CONNECTOR"),
        (lambda s: s.update(connection={"connector": "other@1.0.0"}), "WV-HTTP-ACTION-CONNECTION"),
        (lambda s: s.pop("connection"), "WV-HTTP-ACTION-CONNECTION"),
        (lambda s: s["implementation"].update(action="write"), "WV-HTTP-ACTION-SIDE_EFFECT"),
    ],
)
def test_local_rules_detect_inconsistent_documents(local_rules, tamper, code):
    action = build_http_action(get_pet())
    assert check_http_action(action) == []
    changed = copy.deepcopy(action)
    tamper(changed["spec"])
    assert code in {d.code for d in check_http_action(changed)}


def test_write_retries_are_rejected_by_check():
    action = build_http_action({"name": "create-pet", "method": "POST", "pathTemplate": "/v1/pets", "statuses": [201]})
    action["spec"]["retry"] = {"maxAttempts": 3}
    assert "WV-HTTP-ACTION-RETRY" in {d.code for d in check_http_action(action)}


def test_empty_status_must_accept_null_body(local_rules):
    action = build_http_action(
        {"name": "delete-pet", "method": "DELETE", "pathTemplate": "/v1/pets/{petId}", "statuses": [204]}
    )
    action["spec"]["outputSchema"] = {"type": "object", "properties": {"body": {"type": "object"}}}
    assert "WV-HTTP-ACTION-OUTPUT" in {d.code for d in check_http_action(action)}


def test_server_hook_is_the_authority_when_available(monkeypatch):
    from firefly_weave.compiler.action_config import ActionConfigIssue

    seen = []

    def hook(check):
        seen.append((check.action, check.config["method"]))
        return [ActionConfigIssue("/spec/implementation/config/path", "Rejected by the installed rules.")]

    monkeypatch.setattr(http_actions, "_server_hook", lambda: hook)
    result = author_http_action(get_pet())
    assert seen == [("read", "GET")]
    assert not result.ok
    issue = next(d for d in result.diagnostics if d.code == "WV-COMP-CONFIG_CONTRACT")
    assert issue.path == "/spec/implementation/config/path" and issue.message == "Rejected by the installed rules."

    def broken(check):
        raise RuntimeError("descriptor bug")

    monkeypatch.setattr(http_actions, "_server_hook", lambda: broken)
    assert {d.code for d in check_http_action(build_http_action(get_pet()))} == {"WV-COMP-CONFIG_CONTRACT"}


@pytest.mark.skipif(http_actions._server_hook() is None, reason="This build has no installed Action checks")
@pytest.mark.parametrize(
    "tamper",
    [
        lambda s: s["implementation"].update(config={"foo": 1}),
        lambda s: s["implementation"]["config"].update(method="POST", sideEffect="non_idempotent"),
        lambda s: s["inputSchema"]["properties"]["path"]["properties"].pop("petId"),
    ],
)
def test_installed_rules_accept_built_actions_and_reject_tampering(tamper):
    for request in (
        get_pet(),
        {"name": "create-pet", "method": "POST", "pathTemplate": "/v1/pets", "bodySample": {"a": 1}, "statuses": [201]},
        {"name": "delete-pet", "method": "DELETE", "pathTemplate": "/v1/pets/{petId}", "statuses": [204]},
        get_pet(method="HEAD", responseSample=None),
    ):
        assert author_http_action(request).ok, request
    action = build_http_action(get_pet())
    tamper(action["spec"])
    assert any(d.severity == "error" for d in check_http_action(action))


def test_connection_request_prefills_destinations_and_uses_handles_only():
    identifier = UUID(int=9)
    request = build_connection_request(
        "pets",
        "https://api.example.com",
        {"kind": "api-key", "header": "X-API-Key"},
        {"api_key": "pets-key"},
        identifier,
    )
    assert request == {
        "name": "pets",
        "connector_version_id": str(identifier),
        "config": {"baseUrl": "https://api.example.com", "auth": {"kind": "api-key", "header": "X-API-Key"}},
        "secretRef": {"api_key": "pets-key"},
        "allowed_destinations": ["https://api.example.com"],
    }
    machine = build_connection_request(
        "pets",
        "https://api.example.com",
        {"kind": "machine-token", "client_id": "weave", "endpoint": "https://login.example.com/oauth/token"},
        {"client_secret": "pets-client"},
        identifier,
    )
    assert machine["allowed_destinations"] == ["https://api.example.com", "https://login.example.com"]


@pytest.mark.parametrize(
    "base,auth,secrets,allow",
    [
        ("https://api.example.com/v1", {"kind": "none"}, {}, ()),
        ("http://api.example.com", {"kind": "none"}, {}, ()),
        ("https://user:pw@api.example.com", {"kind": "none"}, {}, ()),
        ("https://api.example.com", {"kind": "bearer"}, {}, ()),
        ("https://api.example.com", {"kind": "bearer"}, {"token": "sk_live/abc=="}, ()),
        ("https://api.example.com", {"kind": "none"}, {"token": "x"}, ()),
        ("https://api.example.com", {"kind": "none"}, {}, ("https://*.example.com",)),
        ("https://api.example.com", {"kind": "none"}, {}, ("https://other.example.com/path",)),
        ("https://api.example.com", {"kind": "api-key", "header": "Authorization"}, {"api_key": "k"}, ()),
    ],
)
def test_connection_request_rejects_unsafe_inputs(base, auth, secrets, allow):
    with pytest.raises(HttpActionError) as error:
        build_connection_request("pets", base, auth, secrets, UUID(int=9), allow)
    assert "sk_live" not in error.value.diagnostics[0].model_dump_json()


def test_module_has_no_infrastructure_or_network_imports():
    code = """
import sys
import firefly_weave.sdk.http_actions
assert not any(n.split('.')[0] in {'pyfly', 'httpx', 'httpcore', 'sqlalchemy', 'asyncpg'} for n in sys.modules), sorted(
    n for n in sys.modules if n.split('.')[0] in {'pyfly', 'httpx', 'httpcore', 'sqlalchemy', 'asyncpg'}
)
"""
    env = {"PYTHONPATH": str(Path(http_actions.__file__).parents[2])}
    subprocess.run([sys.executable, "-c", code], cwd=Path(sys.executable).parent, check=True, env=env)


def test_builtin_manifest_digest_is_pinned():
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR

    # Actions and releases pin this digest; validation must stay in callbacks, never in the manifest.
    assert HTTP_PROFILE_DESCRIPTOR.manifest.digest == "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8"


def test_empty_statuses_must_be_declared_statuses():
    with pytest.raises(HttpActionError) as error:
        build_http_action(
            {
                "name": "delete-pet",
                "method": "DELETE",
                "pathTemplate": "/v1/pets/{petId}",
                "statuses": [200],
                "emptyStatuses": [204],
            }
        )
    assert {(d.code, d.path) for d in error.value.diagnostics} == {("WV-HTTP-ACTION-STATUS", "/emptyStatuses/0")}


@pytest.mark.parametrize(
    "sample",
    [
        {"name": "x", "2fa": True},
        {"größe": 1, "name": "x"},
        [{"a": 1, "tag": "x"}, {"a": 2}],
        [1, "a", None, 2.5],
        [{"o": None}, {"o": {"a": 1}}],
        {"items": [{"id": 1, "n": None}, {"id": 2, "n": "x"}, {"id": 3}]},
        [[1, 2], [], ["x"]],
        [{"a": {"b": [1]}}, {"a": {"b": [], "c": True}}, {"a": None}],
        [{"k": 1}, "text", [1], None],
    ],
)
def test_inferred_schemas_accept_their_own_sample(sample):
    schema, truncated = infer_schema(sample)
    assert not truncated
    assert not validate_payload(schema, sample, {})


def test_inference_merges_elements_into_the_common_shape():
    schema, _ = infer_schema([{"id": 1, "tag": CANARY}, {"id": 2, "owner": None}, {"id": 3, "owner": {"n": CANARY}}])
    items = schema["items"]
    assert items["required"] == ["id"]
    assert items["properties"]["id"] == {"type": "integer"}
    assert items["properties"]["tag"] == {"type": "string"}
    assert items["properties"]["owner"] == {
        "anyOf": [
            {"type": "object", "properties": {"n": {"type": "string"}}, "required": ["n"]},
            {"type": "null"},
        ]
    }
    assert infer_schema([1, 2.5])[0] == {"type": "array", "items": {"type": "number"}}
    assert infer_schema([1, "a"])[0] == {"type": "array", "items": {"type": ["integer", "string"]}}
    assert CANARY not in json.dumps(schema)
