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
"""Independent selected-operation import expectations and rejection boundaries."""

import copy
import importlib.util
import json

import pytest


def importer():
    assert importlib.util.find_spec("firefly_weave.sdk.openapi_import"), "Offline OpenAPI importer is missing"
    from firefly_weave.sdk.openapi_import import import_openapi

    return import_openapi


def document():
    return {
        "openapi": "3.1.1",
        "info": {"title": "Fixture", "version": "1"},
        "servers": [{"url": "https://api.example.test/v1"}],
        "paths": {
            "/items/{id}": {
                "parameters": [
                    {"in": "path", "name": "id", "required": True, "schema": {"type": "string", "maxLength": 128}}
                ],
                "get": {
                    "operationId": "getItem",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"name": {"type": "string"}},
                                        "required": ["name"],
                                        "additionalProperties": False,
                                    }
                                }
                            },
                        }
                    },
                },
            }
        },
    }


def policy():
    return {
        "name": "acme-items",
        "version": "1.0.0",
        "auth": {"kind": "none"},
        "operations": {
            "getItem": {
                "name": "get-item",
                "sideEffect": "read_only",
                "server": "https://api.example.test/v1",
                "statuses": [200],
            }
        },
    }


def test_selected_import_has_exact_schemas_and_no_metadata_prose():
    result = importer()(document(), ["getItem"], policy())
    assert result.ok, result.diagnostics
    connector = result.connector
    action = result.actions[0]
    assert connector["metadata"] == {"name": "acme-items", "version": "1.0.0"}
    assert action["spec"]["sideEffect"] == "read_only"
    assert action["spec"]["inputSchema"] == {
        "type": "object",
        "properties": {
            "path": {
                "type": "object",
                "properties": {"id": {"type": "string", "maxLength": 128}},
                "required": ["id"],
                "additionalProperties": False,
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    assert action["spec"]["implementation"]["config"]["path"] == "/v1/items/{id}"
    assert "Fixture" not in json.dumps(result.model_dump())


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda d: d.update(openapi="3.0.3"), "WV-IMPORT-VERSION"),
        (lambda d: d["paths"]["/items/{id}"]["get"].update(security=[{}, {"key": []}]), "WV-IMPORT-SECURITY"),
        (
            lambda d: d.update(components={"schemas": {"Unused": {"$ref": "https://evil.test/schema"}}}),
            "WV-IMPORT-REMOTE_REF",
        ),
        (
            lambda d: d.update(components={"schemas": {"Unused": {"$ref": "#/components/schemas/Unused"}}}),
            "WV-IMPORT-RECURSIVE_REF",
        ),
        (
            lambda d: d["paths"]["/items/{id}"]["parameters"][0]["schema"].update(nullable=True),
            "WV-SCHEMA-UNSUPPORTED_KEYWORD",
        ),
        (lambda d: d["paths"]["/items/{id}"]["get"].update(callbacks={"callback": {}}), "WV-IMPORT-UNSUPPORTED"),
    ],
)
def test_rejected_document_has_no_deployable_artifacts(mutation, code):
    value = document()
    mutation(value)
    result = importer()(value, ["getItem"], policy())
    assert not result.ok
    assert code in {d.code for d in result.diagnostics}
    assert result.connector is None and result.actions == [] and result.package is None


def test_duplicate_json_keys_keep_parse_diagnostic_and_source_range():
    result = importer()('{"openapi":"3.1.0","openapi":"3.1.1"}', ["getItem"], policy())
    assert not result.ok and result.diagnostics[0].code == "WV-PARSE-DUPLICATE_KEY"
    assert result.diagnostics[0].source.line == 1


def test_inherited_override_and_bounded_query_array():
    value = document()
    operation = value["paths"]["/items/{id}"]["get"]
    operation["parameters"] = [
        {"in": "path", "name": "id", "required": True, "schema": {"type": "integer"}},
        {
            "in": "query",
            "name": "tag",
            "schema": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 10}},
        },
    ]
    result = importer()(value, ["getItem"], policy())
    assert result.ok, result.diagnostics
    assert result.actions[0]["spec"]["inputSchema"]["properties"]["path"]["properties"]["id"] == {"type": "integer"}


def test_second_selected_failure_never_returns_first_success():
    value = document()
    value["paths"]["/broken"] = {"get": {"operationId": "broken", "responses": {}}}
    rules = policy()
    rules["operations"]["broken"] = {
        "name": "broken",
        "sideEffect": "read_only",
        "server": "https://api.example.test/v1",
        "statuses": [200],
    }
    result = importer()(value, ["getItem", "broken"], rules)
    assert not result.ok and result.connector is None and result.actions == []


def test_unused_unsupported_schema_but_literals_are_not_references():
    value = document()
    value["components"] = {
        "schemas": {
            "Unused": {"type": "string", "format": "unknown", "default": {"$ref": "https://not-a-reference.test"}}
        }
    }
    assert importer()(value, ["getItem"], policy()).ok


def test_duplicate_ids_and_names_do_not_get_silent_suffixes():
    value = document()
    value["paths"]["/other"] = copy.deepcopy(value["paths"]["/items/{id}"])
    result = importer()(value, ["getItem"], policy())
    assert not result.ok and result.diagnostics[0].code == "WV-IMPORT-DUPLICATE_OPERATION"


def test_response_refs_keep_sibling_constraints_after_embedding():
    from firefly_weave.compiler.schemas import validate_payload

    value = document()
    schema = value["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]["application/json"]
    schema["schema"] = {"$ref": "#/components/schemas/Name", "maxLength": 3}
    value["components"] = {"schemas": {"Name": {"type": "string"}}}
    result = importer()(value, ["getItem"], policy())
    assert result.ok, result.diagnostics
    output = result.actions[0]["spec"]["outputSchema"]
    assert not validate_payload(output, {"status": 200, "body": "abc"}, {})
    assert validate_payload(output, {"status": 200, "body": "long"}, {})
    assert validate_payload(output, {"status": 201, "body": "abc"}, {})


def test_parameter_ref_cannot_drop_sibling_constraints():
    value = document()
    value["components"] = {"schemas": {"Name": {"type": "string", "maxLength": 128}}}
    value["paths"]["/items/{id}"]["parameters"][0]["schema"] = {"$ref": "#/components/schemas/Name", "maxLength": 3}
    result = importer()(value, ["getItem"], policy())
    assert not result.ok


def test_cli_canonical_result_and_explicit_scaffold(tmp_path):
    from click.testing import CliRunner

    from firefly_weave.cli.main import cli

    source = tmp_path / "source.json"
    source.write_text(json.dumps(document()))
    rules = tmp_path / "policy.json"
    rules.write_text(json.dumps(policy()))
    result = CliRunner().invoke(
        cli,
        [
            "connector",
            "import-openapi",
            str(source),
            "--operation",
            "getItem",
            "--policy",
            str(rules),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["actions"] == importer()(source.read_bytes(), ["getItem"], policy()).actions
    result = CliRunner().invoke(
        cli,
        [
            "connector",
            "import-openapi",
            str(source),
            "--operation",
            "getItem",
            "--policy",
            str(rules),
            "--directory",
            str(tmp_path / "package"),
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "package/pyproject.toml").is_file()
    assert (tmp_path / "package/src/acme_items/__init__.py").is_file()


@pytest.mark.parametrize(
    "security,auth",
    [
        ({"type": "http", "scheme": "bearer"}, {"kind": "bearer"}),
        ({"type": "http", "scheme": "basic"}, {"kind": "basic"}),
        ({"type": "apiKey", "in": "header", "name": "X-API-Key"}, {"kind": "api-key", "header": "X-API-Key"}),
        (
            {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.test/token",
                        "scopes": {"read:items": "read"},
                    }
                },
            },
            {
                "kind": "machine-token",
                "client_id": "fixture",
                "endpoint": "https://auth.example.test/token",
                "scopes": ["read:items"],
            },
        ),
    ],
)
def test_supported_inherited_security_and_explicit_disable(security, auth):
    value = document()
    rules = policy()
    value["components"] = {"securitySchemes": {"auth": security}}
    value["security"] = [{"auth": auth.get("scopes", [])}]
    rules["auth"] = auth
    result = importer()(value, ["getItem"], rules)
    assert result.ok, result.diagnostics
    value["paths"]["/items/{id}"]["get"]["security"] = []
    assert not importer()(value, ["getItem"], rules).ok
    rules["auth"] = {"kind": "none"}
    assert importer()(value, ["getItem"], rules).ok


@pytest.mark.parametrize(
    "ref",
    [
        "#/components/schemas/missing",
        "#thing",
        "#/components/schemas/a~2b",
        "#/%61",
        "other.json#/foo",
        "file:///secret",
    ],
)
def test_invalid_local_reference_never_resolves_files_or_urls(ref):
    value = document()
    value["components"] = {"schemas": {"Bad": {"$ref": ref}}}
    assert not importer()(value, ["getItem"], policy()).ok


def test_unused_server_userinfo_and_schema_secret_literals_fail_globally():
    value = document()
    value["paths"]["/unused"] = {
        "get": {"operationId": "unused", "servers": [{"url": "https://USER:SECRET@host.test"}]}
    }
    result = importer()(value, ["getItem"], policy())
    assert not result.ok and "SECRET" not in result.model_dump_json()
    value = document()
    value["components"] = {
        "schemas": {"Unused": {"type": "string", "x-secret": True, "default": "UNUSED-CREDENTIAL-CANARY"}}
    }
    result = importer()(value, ["getItem"], policy())
    assert not result.ok and "UNUSED-CREDENTIAL-CANARY" not in result.model_dump_json()


def test_input_budgets_and_object_nonfinite_fail_without_exception():
    result = importer()(b" " * 1048577, ["getItem"], policy())
    assert not result.ok
    value = document()
    value["info"]["x-data"] = float("nan")
    assert not importer()(value, ["getItem"], policy()).ok


def test_nested_defs_local_pointer_survives_generated_embedding():
    value = document()
    value["components"] = {
        "schemas": {
            "Envelope": {
                "type": "object",
                "$defs": {"Label": {"type": "string", "maxLength": 8}},
                "properties": {"label": {"$ref": "#/components/schemas/Envelope/$defs/Label"}},
                "required": ["label"],
            }
        }
    }
    value["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/Envelope"
    }
    result = importer()(value, ["getItem"], policy())
    assert result.ok, result.diagnostics
    from firefly_weave.compiler.schemas import validate_payload

    schema = result.actions[0]["spec"]["outputSchema"]
    assert not validate_payload(schema, {"status": 200, "body": {"label": "good"}}, {})
    assert validate_payload(schema, {"status": 200, "body": {"label": "too-long-label"}}, {})


def test_small_ref_dag_amplification_is_rejected_before_expansion():
    value = document()
    nodes = {"a0": {"type": "string"}}
    for i in range(1, 22):
        ref = {"$ref": f"#/components/schemas/a{i - 1}"}
        nodes[f"a{i}"] = {"allOf": [ref, ref]}
    value["components"] = {"schemas": nodes}
    result = importer()(value, ["getItem"], policy())
    assert not result.ok and result.diagnostics[0].code == "WV-IMPORT-RESOURCE_LIMIT"


def test_unused_operation_without_id_still_checks_security_references():
    value = document()
    value["paths"]["/unused"] = {"get": {"security": [{"missing": []}]}}
    assert not importer()(value, ["getItem"], policy()).ok


def test_declared_credentials_and_source_values_are_absent_from_failure():
    value = document()
    value["paths"]["/items/{id}"]["get"]["parameters"] = [
        {
            "in": "header",
            "name": "Authorization",
            "example": "Bearer HIDDEN-CANARY",
            "schema": {"type": "string", "maxLength": 50},
        }
    ]
    result = importer()(value, ["getItem"], policy())
    assert not result.ok and "HIDDEN-CANARY" not in result.model_dump_json()


def test_scaffold_rejects_incomplete_artifact_before_build(tmp_path):
    from firefly_weave.sdk.connectors import package, scaffold_import

    result = importer()(document(), ["getItem"], policy())
    target = tmp_path / "project"
    scaffold_import(target, result)
    (target / ".weave-import-incomplete").write_text("incomplete")
    with pytest.raises(ValueError, match="Incomplete"):
        package(target, tmp_path / "dist")


def test_ref_sibling_error_points_to_original_keyword():
    value = document()
    value["components"] = {"schemas": {"Base": {"type": "string"}}}
    value["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/Base",
        "nullable": True,
    }
    result = importer()(value, ["getItem"], policy())
    assert not result.ok
    assert (
        result.diagnostics[0].path == "/paths/~1items~1{id}/get/responses/200/content/application~1json/schema/nullable"
    )


def test_credential_bearing_path_key_is_masked_in_diagnostics():
    value = document()
    value["paths"]["/items?token=HIDDEN-CANARY"] = value["paths"].pop("/items/{id}")
    result = importer()(json.dumps(value), ["getItem"], policy())
    assert not result.ok and "HIDDEN-CANARY" not in result.model_dump_json()
    assert result.diagnostics[0].source is not None


@pytest.mark.parametrize(
    "components",
    [
        {"headers": {"Unused": {"$ref": "https://remote.invalid/header"}}},
        {
            "parameters": {
                "Unused": {
                    "name": "x",
                    "in": "query",
                    "content": {"application/json": {"schema": {"$ref": "https://remote.invalid/schema"}}},
                }
            }
        },
        {
            "callbacks": {
                "Unused": {
                    "{$request.body#/url}": {
                        "post": {"responses": {"200": {"$ref": "https://remote.invalid/response"}}}
                    }
                }
            }
        },
        {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$id": "https://remote.invalid/base", "type": "string"},
            }
        },
    ],
)
def test_unused_structural_reference_locations_remain_global_gates(components):
    value = document()
    value["components"] = components
    assert not importer()(value, ["getItem"], policy()).ok


@pytest.mark.parametrize("kind", ["duplicate", "component", "policy"])
def test_all_diagnostic_paths_mask_credential_keys_with_ranges(kind, tmp_path):
    from click.testing import CliRunner

    from firefly_weave.cli.main import cli

    canary = "REVIEW-CANARY"
    if kind == "component":
        value = document()
        value["components"] = {"parameters": {f"https://u:{canary}@host.test": {"$ref": "https://remote.test/x"}}}
        source = json.dumps(value)
    else:
        source = '{"paths":{"/items?token=REVIEW-CANARY":{},"/items?token=REVIEW-CANARY":{}}}'
    if kind == "policy":
        (tmp_path / "doc.json").write_text(json.dumps(document()))
        (tmp_path / "policy.json").write_text(source)
        result = CliRunner().invoke(
            cli,
            [
                "connector",
                "import-openapi",
                str(tmp_path / "doc.json"),
                "--operation",
                "getItem",
                "--policy",
                str(tmp_path / "policy.json"),
                "--output",
                "json",
            ],
        )
        assert result.exit_code != 0
        data = json.loads(result.output)
    else:
        result = importer()(source, ["getItem"], policy())
        assert not result.ok
        data = result.model_dump(by_alias=True)
    assert canary not in json.dumps(data)
    assert data["diagnostics"][0]["source"] is not None
    if kind != "component":
        assert data["diagnostics"][0]["related"][0]["source"] is not None


@pytest.mark.parametrize(
    "case", ["query", "literal", "ref_literal", "webhook", "callback", "pathitem", "ancestor", "response"]
)
def test_review_global_metadata_and_reference_gates(case):
    value = document()
    if case == "query":
        value["paths"]["/unused"] = {"get": {"servers": [{"url": "https://api.example.test?token=REVIEW-CANARY"}]}}
    elif case in {"literal", "ref_literal"}:
        branches = [{"x-secret": True}, {"default": "REVIEW-CANARY"}]
        value["components"] = {"schemas": {"Unused": {"allOf": branches, "unknownUnusedKeyword": True}}}
        if case == "ref_literal":
            branches[0] = {"$ref": "#/components/schemas/Secret"}
            value["components"]["schemas"]["Secret"] = {"x-secret": True}
    elif case in {"webhook", "callback", "pathitem"}:
        item = {"post": {"security": [{"missing": []}]}}
        if case == "webhook":
            value["webhooks"] = {"unused": item}
        elif case == "pathitem":
            value["components"] = {"pathItems": {"unused": item}}
        else:
            value["components"] = {"callbacks": {"unused": {"{$request.body#/url}": item}}}
    elif case == "ancestor":
        value["components"] = {
            "schemas": {"Base": {"$id": "https://other.test/schema", "properties": {"X": {"type": "string"}}}}
        }
        value["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
            "$ref": "#/components/schemas/Base/properties/X"
        }
    else:
        value["paths"]["/unused"] = {"get": {"responses": {"200": {"description": "ok"}}}}
        value["paths"]["/items/{id}"]["get"]["responses"]["200"] = {"$ref": "#/paths/~1unused/get/responses/200"}
    result = importer()(json.dumps(value), ["getItem"], policy())
    assert not result.ok
    assert result.connector is None and not result.actions and result.package is None
    assert "REVIEW-CANARY" not in result.model_dump_json()


def test_unused_unknown_schema_semantics_without_credentials_are_not_selected():
    value = document()
    value["components"] = {"schemas": {"Unused": {"unknownUnusedKeyword": True, "default": "ordinary"}}}
    assert importer()(value, ["getItem"], policy()).ok


@pytest.mark.parametrize("owner", ["root", "path"])
def test_inherited_server_error_keeps_actual_owner_source(owner):
    value = document()
    servers = [{"url": "https://api.example.test/v1", "variables": {}}]
    if owner == "root":
        value["servers"] = servers
        expected = "/servers/0/variables"
    else:
        value["paths"]["/items/{id}"]["servers"] = servers
        expected = "/paths/~1items~1{id}/servers/0/variables"
    result = importer()(json.dumps(value), ["getItem"], policy())
    assert not result.ok and result.diagnostics[0].path == expected
    assert result.diagnostics[0].source is not None


def test_schema_error_retains_referring_context_and_ranges():
    value = document()
    value["components"] = {"schemas": {"Base": {"type": "object", "unknownConstraint": True}}}
    value["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/Base"
    }
    result = importer()(json.dumps(value), ["getItem"], policy())
    diagnostic = result.diagnostics[0]
    assert diagnostic.path == "/components/schemas/Base/unknownConstraint" and diagnostic.source is not None
    assert any(r.path.endswith("/schema/$ref") and r.source is not None for r in diagnostic.related)


def test_oauth_flow_diagnostic_uses_separate_pointer_tokens():
    value = document()
    value["components"] = {
        "securitySchemes": {
            "auth": {
                "type": "oauth2",
                "flows": {
                    "clientCredentials": {
                        "tokenUrl": "https://auth.example.test/token",
                        "scopes": {},
                        "refreshUrl": "https://auth.example.test/refresh",
                    }
                },
            }
        }
    }
    value["security"] = [{"auth": []}]
    rules = policy()
    rules["auth"] = {
        "kind": "machine-token",
        "client_id": "client",
        "endpoint": "https://auth.example.test/token",
        "scopes": [],
    }
    result = importer()(json.dumps(value), ["getItem"], rules)
    issue = result.diagnostics[0]
    assert issue.path == "/components/securitySchemes/auth/flows/clientCredentials/refreshUrl"
    assert issue.source is not None


def test_header_source_map_escapes_legal_tilde_name():
    value = document()
    value["paths"]["/items/{id}"]["get"]["parameters"] = [
        {"in": "header", "name": "X~Review", "schema": {"type": "string", "maxLength": 8}}
    ]
    result = importer()(value, ["getItem"], policy())
    assert result.ok
    assert any(p.endswith("/headers/properties/X~0Review") for p in result.source_map)
    assert all(not p.endswith("/headers/properties/X~Review") for p in result.source_map)


@pytest.mark.parametrize("race", ["target", "ancestor", "contamination", "replacement"])
def test_scaffold_directory_races_fail_closed(race, tmp_path, monkeypatch):
    import os

    from firefly_weave.sdk.connectors import scaffold_import

    result = importer()(document(), ["getItem"], policy())
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "project"
    other = tmp_path / "other"
    other.mkdir()
    original_mkdir = os.mkdir
    original_open = os.open
    triggered = False

    def raced_mkdir(path, *args, **kwargs):
        nonlocal triggered
        if str(path) in {str(target), "project"} and not triggered:
            triggered = True
            if race == "target":
                target.symlink_to(other, target_is_directory=True)
                return
            if race == "contamination":
                original_mkdir(path, *args, **kwargs)
                (target / "unreviewed.txt").write_text("unrelated")
                return
        return original_mkdir(path, *args, **kwargs)

    def raced_open(path, *args, **kwargs):
        nonlocal triggered
        if str(path) == ".weave-import-incomplete" and race in {"ancestor", "replacement"}:
            triggered = True
            if race == "ancestor":
                parent.rename(tmp_path / "moved")
                parent.symlink_to(other, target_is_directory=True)
            else:
                target.rename(parent / "moved")
                target.mkdir()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", raced_mkdir)
    monkeypatch.setattr(os, "open", raced_open)
    with pytest.raises((ValueError, OSError)):
        scaffold_import(target, result)
    assert triggered
    assert not (other / "pyproject.toml").exists()
    assert not (target / "pyproject.toml").exists()


def test_scaffold_refuses_symlinked_ancestor(tmp_path):
    from firefly_weave.sdk.connectors import scaffold_import

    (tmp_path / "actual").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "actual", target_is_directory=True)
    result = importer()(document(), ["getItem"], policy())
    with pytest.raises((ValueError, OSError)):
        scaffold_import(tmp_path / "link" / "project", result)
    assert not (tmp_path / "actual" / "project").exists()


def test_credential_component_key_cannot_enter_successful_source_map():
    value = document()
    name = "https://user:REVIEW-CANARY@host.test"
    value["components"] = {"schemas": {name: {"type": "string"}}}
    value["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/" + name.replace("~", "~0").replace("/", "~1")
    }
    result = importer()(value, ["getItem"], policy())
    assert not result.ok and "REVIEW-CANARY" not in result.model_dump_json()


def test_diagnostic_suggested_and_related_locations_use_same_sanitizer():
    from firefly_weave.contracts.diagnostics import Diagnostic, RelatedLocation, SourceRange, SuggestedEdit
    from firefly_weave.sdk.openapi_import import sanitize_import_diagnostic

    path = "/components/https:~1~1u:REVIEW-CANARY@host.test"
    span = SourceRange(line=3, column=8)
    issue = Diagnostic(
        code="WV-IMPORT-SECURITY",
        severity="error",
        stage="semantic",
        message="Unsupported.",
        path=path,
        source=span,
        related=[RelatedLocation(path=path, source=span)],
        suggestedEdit=SuggestedEdit(path=path, value=None),
    )
    result = sanitize_import_diagnostic(issue)
    assert "REVIEW-CANARY" not in result.model_dump_json()
    assert result.source == span and result.related[0].source == span


def test_scaffold_rejects_leaf_replacement_before_publication(tmp_path, monkeypatch):
    import os

    from firefly_weave.sdk.connectors import scaffold_import

    result = importer()(document(), ["getItem"], policy())
    target = tmp_path / "project"
    original_open = os.open
    triggered = False

    def raced_open(path, *args, **kwargs):
        nonlocal triggered
        if str(path) == "pyproject.toml":
            triggered = True
            (target / "README.md").rename(target / "old-readme")
            (target / "README.md").write_text("Unreviewed replacement")
            (target / "old-readme").unlink()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", raced_open)
    with pytest.raises((ValueError, OSError)):
        scaffold_import(target, result)
    assert triggered and (target / ".weave-import-incomplete").exists()


@pytest.mark.parametrize(
    "schema,code",
    [
        ({"$schema": "https://other.test/dialect"}, "WV-IMPORT-DIALECT"),
        ({"$dynamicRef": "#x"}, "WV-IMPORT-INVALID_REF"),
        ({"unknownConstraint": True}, "WV-SCHEMA-UNSUPPORTED_KEYWORD"),
    ],
)
@pytest.mark.parametrize("chained", [False, True])
def test_early_lowering_errors_preserve_all_referring_ranges(schema, code, chained):
    value = document()
    value["components"] = {"schemas": {"Base": schema}}
    ref_path = "/paths/~1items~1{id}/get/responses/200/content/application~1json/schema/$ref"
    expected = {ref_path}
    if chained:
        value["components"]["schemas"]["Hop"] = {"$ref": "#/components/schemas/Base"}
        expected.add("/components/schemas/Hop/$ref")
    value["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/" + ("Hop" if chained else "Base")
    }
    result = importer()(json.dumps(value), ["getItem"], policy())
    assert not result.ok and result.connector is None and result.package is None and not result.actions
    issue = result.diagnostics[0]
    assert issue.code == code and issue.path.startswith("/components/schemas/Base") and issue.source is not None
    assert {related.path for related in issue.related} == expected
    assert all(related.source is not None for related in issue.related)


def test_early_lowering_budget_error_preserves_referring_range(monkeypatch):
    from firefly_weave.sdk.openapi_import import _Importer

    value = document()
    value["components"] = {"schemas": {"Base": {"type": "string"}}}
    schema_path = "/paths/~1items~1{id}/get/responses/200/content/application~1json/schema"
    value["paths"]["/items/{id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/Base"
    }
    original = _Importer.schema

    def exhausted(self, path, generated="", *, classification_only=False):
        if path == schema_path and not classification_only:
            self.schema_work = 99999
        return original(self, path, generated, classification_only=classification_only)

    monkeypatch.setattr(_Importer, "schema", exhausted)
    result = importer()(json.dumps(value), ["getItem"], policy())
    assert not result.ok
    issue = result.diagnostics[0]
    assert issue.code == "WV-IMPORT-RESOURCE_LIMIT" and issue.source is not None
    assert len(issue.related) == 1 and issue.related[0].path == schema_path + "/$ref"
    assert issue.related[0].source is not None
