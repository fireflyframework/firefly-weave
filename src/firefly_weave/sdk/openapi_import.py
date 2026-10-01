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
"""Pure selected-operation OpenAPI import with bounded local references and no I/O."""

import hashlib
import keyword
import re
from typing import Any, Literal, NoReturn, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, ValidationError

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.parser import ParseFailure, parse_source
from firefly_weave.compiler.schema_profile import DIALECT, SCHEMA_ARRAYS, SCHEMA_MAPS, SCHEMA_SINGLE
from firefly_weave.compiler.schemas import validate_schema
from firefly_weave.connectors.packages import PackageMetadata
from firefly_weave.contracts.definitions import ConnectorDefinition, ContractModel, ResourceName, SemVer
from firefly_weave.contracts.diagnostics import Diagnostic, RelatedLocation
from firefly_weave.contracts.http_profiles import (
    AuthProfile,
    HttpOperation,
    HttpParameter,
    fixed_server,
    object_schema,
    protected,
    template_names,
)
from firefly_weave.contracts.values import JsonObjectData

OAS_DIALECT = "https://spec.openapis.org/oas/3.1/dialect/base"
METHODS = ("get", "head", "post", "put", "patch", "delete", "options", "trace")
ANNOTATIONS = {"description", "summary", "title", "deprecated", "tags", "externalDocs"}


class OperationPolicy(ContractModel):
    name: ResourceName
    side_effect: Literal["read_only", "non_idempotent"] = Field(alias="sideEffect")
    server: str
    statuses: list[int] = Field(min_length=1, max_length=20)


class OpenAPIImportPolicy(ContractModel):
    name: ResourceName
    version: SemVer
    auth: AuthProfile
    operations: dict[str, OperationPolicy] = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(default=30, ge=1, le=30, alias="timeoutSeconds")
    max_request_bytes: int = Field(default=1048576, ge=1, le=1048576, alias="maxRequestBytes")
    max_response_bytes: int = Field(default=1048576, ge=1, le=1048576, alias="maxResponseBytes")


class OpenAPIImportResult(ContractModel):
    ok: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    truncated: bool = False
    connector: JsonObjectData | None = None
    actions: list[JsonObjectData] = Field(default_factory=list)
    package: JsonObjectData | None = None
    provenance: JsonObjectData | None = None
    source_map: dict[str, str] = Field(default_factory=dict, alias="sourceMap")


class _Failure(Exception):
    def __init__(
        self, code: str, path: str = "", diagnostic: Diagnostic | None = None, related: tuple[str, ...] = ()
    ) -> None:
        self.code, self.path, self.diagnostic, self.related = code, path, diagnostic, related


def _fail(code: str, path: str = "", *, related: tuple[str, ...] = ()) -> NoReturn:
    raise _Failure(code, path, related=related)


def _ptr(path: str, key: str | int) -> str:
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _credential_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        names = {
            "token",
            "access_token",
            "api_key",
            "apikey",
            "key",
            "password",
            "client_secret",
            "clientsecret",
            "authorization",
        }
        return (
            parsed.username is not None
            or parsed.password is not None
            or any(name.lower() in names for name, _ in parse_qsl(parsed.query, keep_blank_values=True))
        )
    except ValueError:
        return True


def sanitize_import_diagnostic(diagnostic: Diagnostic) -> Diagnostic:
    """Mask unsafe pointer tokens on every import error channel, retaining source ranges."""

    def safe(path: str) -> str:
        if len(path) > 16384:
            return ""
        tokens = path.split("/")
        for index, token in enumerate(tokens):
            decoded = token.replace("~1", "/").replace("~0", "~")
            unsafe = len(decoded) > 256 or any(ord(c) < 32 for c in decoded) or _credential_url(decoded)
            if index == 2 and tokens[1] == "paths":
                try:
                    template_names(decoded)
                except ValueError:
                    unsafe = True
            if unsafe:
                tokens[index] = "*"
        return "/".join(tokens)

    return diagnostic.model_copy(
        update={
            "path": safe(diagnostic.path),
            "related": [related.model_copy(update={"path": safe(related.path)}) for related in diagnostic.related],
            "suggested_edit": diagnostic.suggested_edit.model_copy(
                update={"path": safe(diagnostic.suggested_edit.path)}
            )
            if diagnostic.suggested_edit is not None
            else None,
        }
    )


def _fields(value: Any, allowed: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("UNSUPPORTED", path)
    for key in value:
        if key not in allowed | ANNOTATIONS:
            _fail("UNSUPPORTED", _ptr(path, key))
    return cast(dict[str, Any], value)


class _Importer:
    def __init__(self, value: dict[str, Any], policy: OpenAPIImportPolicy) -> None:
        self.value, self.policy = value, policy
        self.nodes: dict[str, tuple[Any, str]] = {}
        self.children: dict[str, list[str]] = {}
        self.refs: dict[str, str] = {}
        self.work = 0
        self.schema_work = 0
        self.origins: set[str] = set()
        self.source_pointers: dict[str, str] = {}
        self.schema_sources: dict[str, str] = {}
        self.generated_prefix = ""
        self.ref_context: dict[str, list[str]] = {}

    def charge(self, n: int = 1) -> None:
        self.work += n
        if self.work > 100000:
            _fail("RESOURCE_LIMIT")

    def node(self, value: Any, kind: str, path: str) -> None:
        if path in self.nodes:
            return
        self.charge()
        self.nodes[path] = (value, kind)
        self.children[path] = []
        if isinstance(value, bool) and kind == "schema":
            return
        if not isinstance(value, dict):
            _fail("UNSUPPORTED", path)
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                _fail(
                    "REMOTE_REF" if isinstance(ref, str) and not ref.startswith("#") else "INVALID_REF",
                    _ptr(path, "$ref"),
                )
            if "%" in ref or re.search(r"~(?![01])", ref):
                _fail("INVALID_REF", _ptr(path, "$ref"))
            self.refs[path] = ref[1:]
            if len(self.refs) > 10000:
                _fail("RESOURCE_LIMIT", path)
        if kind != "schema":
            for key in ("password", "token", "client_secret", "clientSecret", "access_token", "apiKey"):
                if key in value:
                    _fail("CREDENTIAL_METADATA", path)

        def child(v: Any, k: str, p: str) -> None:
            self.children[path].append(p)
            self.node(v, k, p)

        if kind == "schema":
            for key in SCHEMA_MAPS:
                if isinstance(value.get(key), dict):
                    for name, schema in value[key].items():
                        child(schema, "schema", _ptr(_ptr(path, key), name))
            for key in SCHEMA_ARRAYS:
                if isinstance(value.get(key), list):
                    for i, schema in enumerate(value[key]):
                        child(schema, "schema", _ptr(_ptr(path, key), i))
            for key in SCHEMA_SINGLE:
                if key in value:
                    child(value[key], "schema", _ptr(path, key))
        else:
            if kind in {"parameter", "header"} and "schema" in value:
                child(value["schema"], "schema", _ptr(path, "schema"))
            if kind in {"parameter", "header", "requestBody", "response"} and isinstance(value.get("content"), dict):
                for media, entry in value["content"].items():
                    media_path = _ptr(_ptr(path, "content"), media)
                    if isinstance(entry, dict):
                        if "schema" in entry:
                            child(entry["schema"], "schema", _ptr(media_path, "schema"))
                        if isinstance(entry.get("examples"), dict):
                            for name, example in entry["examples"].items():
                                child(example, "example", _ptr(_ptr(media_path, "examples"), name))
            if kind in {"parameter", "header"} and isinstance(value.get("examples"), dict):
                for name, example in value["examples"].items():
                    child(example, "example", _ptr(_ptr(path, "examples"), name))
            if kind == "response":
                for group, child_kind in (("headers", "header"), ("links", "link")):
                    if isinstance(value.get(group), dict):
                        for name, entry in value[group].items():
                            child(entry, child_kind, _ptr(_ptr(path, group), name))
            if kind in {"pathItem", "operation"} and isinstance(value.get("parameters"), list):
                for i, parameter in enumerate(value["parameters"]):
                    child(parameter, "parameter", _ptr(_ptr(path, "parameters"), i))
            if kind == "pathItem":
                for method in METHODS:
                    if method in value:
                        child(value[method], "operation", _ptr(path, method))
            if kind == "operation":
                if "requestBody" in value:
                    child(value["requestBody"], "requestBody", _ptr(path, "requestBody"))
                for group, child_kind in (("responses", "response"), ("callbacks", "callback")):
                    if isinstance(value.get(group), dict):
                        for name, entry in value[group].items():
                            child(entry, child_kind, _ptr(_ptr(path, group), name))
            if kind == "callback":
                for expression, entry in value.items():
                    if expression not in {"$ref", "description", "summary"} and not expression.startswith("x-"):
                        child(entry, "pathItem", _ptr(path, expression))

    def inventory(self) -> dict[str, tuple[str, str, dict[str, Any], dict[str, Any]]]:
        value = self.value
        if value.get("openapi") not in {"3.1.0", "3.1.1"}:
            _fail("VERSION", "/openapi")
        if value.get("jsonSchemaDialect", OAS_DIALECT) not in {DIALECT, OAS_DIALECT}:
            _fail("DIALECT", "/jsonSchemaDialect")
        if not isinstance(value.get("info"), dict) or any(
            not isinstance(value["info"].get(k), str) or not value["info"][k] or len(value["info"][k]) > 4096
            for k in ("title", "version")
        ):
            _fail("UNSUPPORTED", "/info")
        paths = value.get("paths")
        if not isinstance(paths, dict):
            _fail("UNSUPPORTED", "/paths")
        components = value.get("components", {})
        if not isinstance(components, dict):
            _fail("UNSUPPORTED", "/components")
        for group, kind in [
            ("schemas", "schema"),
            ("parameters", "parameter"),
            ("requestBodies", "requestBody"),
            ("responses", "response"),
            ("securitySchemes", "securityScheme"),
            ("headers", "header"),
            ("examples", "example"),
            ("links", "link"),
            ("callbacks", "callback"),
            ("pathItems", "pathItem"),
        ]:
            entries = components.get(group, {})
            if not isinstance(entries, dict):
                _fail("UNSUPPORTED", _ptr("/components", group))
            for name, entry in entries.items():
                self.node(entry, kind, _ptr(_ptr("/components", group), name))

        def metadata_guard(node: Any, where: str) -> None:
            if isinstance(node, list):
                for i, entry in enumerate(node):
                    metadata_guard(entry, _ptr(where, i))
            elif isinstance(node, dict):
                for key, entry in node.items():
                    if _credential_url(key):
                        _fail("CREDENTIAL_METADATA", _ptr(where, key))
                    if key in {"description", "summary", "title", "default", "examples", "example", "const", "enum"}:
                        continue
                    if (
                        key in {"url", "tokenUrl", "authorizationUrl", "refreshUrl"}
                        and isinstance(entry, str)
                        and _credential_url(entry)
                    ):
                        _fail("CREDENTIAL_METADATA", _ptr(where, key))
                    if key in {"client_secret", "clientSecret", "access_token", "password", "token"} and isinstance(
                        entry, str
                    ):
                        _fail("CREDENTIAL_METADATA", where)
                    metadata_guard(entry, _ptr(where, key))

        metadata_guard(value, "")
        operations: dict[str, tuple[str, str, dict[str, Any], dict[str, Any]]] = {}
        templates: set[str] = set()
        count = 0
        for path, item in sorted(paths.items()):
            p = _ptr("/paths", path)
            if path.startswith("x-"):
                continue
            if not isinstance(item, dict) or not path.startswith("/"):
                _fail("UNSUPPORTED", p)
            normal = re.sub(r"\{[^{}]+\}", "{}", path)
            if normal in templates:
                _fail("NAME_COLLISION", p)
            templates.add(normal)
            self.node(item, "pathItem", p)
            for owner, owner_path in [(item, p), *[(item[m], _ptr(p, m)) for m in METHODS if m in item]]:
                if not isinstance(owner, dict):
                    _fail("UNSUPPORTED", owner_path)
                params = owner.get("parameters", [])
                if not isinstance(params, list):
                    _fail("UNSUPPORTED", _ptr(owner_path, "parameters"))
                for i, param in enumerate(params):
                    self.node(param, "parameter", _ptr(_ptr(owner_path, "parameters"), i))
            for method in METHODS:
                if method not in item:
                    continue
                count += 1
                if count > 1000:
                    _fail("RESOURCE_LIMIT", p)
                op, q = item[method], _ptr(p, method)
                identifier = op.get("operationId")
                if identifier is not None:
                    if not isinstance(identifier, str) or not re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", identifier
                    ):
                        _fail("UNSUPPORTED", _ptr(q, "operationId"))
                    if identifier in operations:
                        _fail("DUPLICATE_OPERATION", _ptr(q, "operationId"))
                    operations[identifier] = (path, method, item, op)
                if "requestBody" in op:
                    self.node(op["requestBody"], "requestBody", _ptr(q, "requestBody"))
                responses = op.get("responses", {})
                if not isinstance(responses, dict):
                    _fail("UNSUPPORTED", _ptr(q, "responses"))
                for status, response in responses.items():
                    self.node(response, "response", _ptr(_ptr(q, "responses"), status))
        webhooks = value.get("webhooks", {})
        if not isinstance(webhooks, dict):
            _fail("UNSUPPORTED", "/webhooks")
        for name, item in webhooks.items():
            self.node(item, "pathItem", _ptr("/webhooks", name))
        for source, target in self.refs.items():
            if target not in self.nodes or self.nodes[target][1] != self.nodes[source][1]:
                _fail("INVALID_REF", _ptr(source, "$ref"))
            kind = self.nodes[source][1]
            groups = {
                "parameter": "parameters",
                "requestBody": "requestBodies",
                "response": "responses",
                "securityScheme": "securitySchemes",
                "header": "headers",
                "example": "examples",
                "link": "links",
                "callback": "callbacks",
                "pathItem": "pathItems",
            }
            if kind != "schema" and (
                kind not in groups
                or not target.startswith(f"/components/{groups[kind]}/")
                or len(target.split("/")) != 4
            ):
                _fail("INVALID_REF", _ptr(source, "$ref"))
            for endpoint in (source, target):
                ancestor = endpoint
                while ancestor:
                    enclosing, enclosing_kind = self.nodes.get(ancestor, (None, ""))
                    if (
                        enclosing_kind == "schema"
                        and isinstance(enclosing, dict)
                        and any(key in enclosing for key in ("$id", "$anchor", "$dynamicAnchor", "$recursiveAnchor"))
                    ):
                        _fail("INVALID_REF", _ptr(source, "$ref"))
                    ancestor = ancestor.rsplit("/", 1)[0]
        memo: dict[str, tuple[int, int]] = {}

        def walk(path: str, active: frozenset[str]) -> tuple[int, int]:
            if path in active:
                _fail("RECURSIVE_REF", _ptr(path, "$ref"))
            if len(active) >= 64:
                _fail("RESOURCE_LIMIT", path)
            if path in memo:
                return memo[path]
            value = self.nodes[path][0]
            if path in self.refs and isinstance(value, dict) and any(k in value for k in ("$id", "$anchor")):
                _fail("INVALID_REF", path)
            size, height = 1, 1
            for child in self.children[path] + ([self.refs[path]] if path in self.refs else []):
                n, h = walk(child, active | {path})
                size, height = size + n, max(height, 1 + h)
                if size > 100000 or height > 64:
                    _fail("RESOURCE_LIMIT", path)
            memo[path] = size, height
            return memo[path]

        for path in self.nodes:
            n, _ = walk(path, frozenset())
            self.charge(n)
        schema_children = {
            child for path, children in self.children.items() if self.nodes[path][1] == "schema" for child in children
        }
        for schema_path, (_, kind) in self.nodes.items():
            if kind == "schema" and schema_path not in schema_children:
                self.schema(schema_path, classification_only=True)
        schemes = components.get("securitySchemes", {})
        security_owners = [(value, "")] + [
            (node, path) for path, (node, kind) in self.nodes.items() if kind == "operation"
        ]
        for owner, path in security_owners:
            security = owner.get("security", [])
            if not isinstance(security, list):
                _fail("SECURITY", _ptr(path, "security"))
            for requirement in security:
                if not isinstance(requirement, dict) or any(name not in schemes for name in requirement):
                    _fail("SECURITY", _ptr(path, "security"))
        return operations

    def deref(self, path: str) -> tuple[dict[str, Any], str]:
        referring = []
        while path in self.refs:
            _fields(self.nodes[path][0], {"$ref"}, path)
            referring.append(_ptr(path, "$ref"))
            path = self.refs[path]
            self.ref_context.setdefault(path, []).extend(referring)
        value = self.nodes[path][0]
        if not isinstance(value, dict):
            _fail("UNSUPPORTED", path)
        return value, path

    def schema(self, path: str, generated: str = "", *, classification_only: bool = False) -> dict[str, Any]:
        pointers: dict[str, str] = {}
        referring: dict[str, tuple[str, ...]] = {}

        def lower(p: str, out: str = "", via: tuple[str, ...] = ()) -> Any:
            self.schema_work += 1
            if self.schema_work > 100000:
                _fail("RESOURCE_LIMIT", p, related=via)
            value = self.nodes[p][0]
            pointers[out] = p
            referring[out] = via
            if isinstance(value, bool):
                return value
            if not classification_only and any(
                k in value
                for k in ("$id", "$anchor", "$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$recursiveAnchor")
            ):
                _fail("INVALID_REF", p, related=via)
            if classification_only:
                value = {
                    k: v
                    for k, v in value.items()
                    if k
                    in SCHEMA_MAPS
                    | SCHEMA_ARRAYS
                    | SCHEMA_SINGLE
                    | {"$ref", "x-secret", "writeOnly", "default", "const", "enum", "examples"}
                }
            result = {key: child for key, child in value.items() if key != "$ref"}
            node_out = out + "/allOf/1" if "$ref" in value and result else out
            pointers[node_out] = p
            referring[node_out] = via
            if "$schema" in result:
                if result["$schema"] not in {DIALECT, OAS_DIALECT}:
                    _fail("DIALECT", _ptr(p, "$schema"), related=via)
                result["$schema"] = DIALECT
            for key in SCHEMA_MAPS:
                if isinstance(value.get(key), dict):
                    result[key] = {
                        name: lower(_ptr(_ptr(p, key), name), _ptr(_ptr(node_out, key), name), via)
                        for name in value[key]
                    }
            for key in SCHEMA_ARRAYS:
                if isinstance(value.get(key), list):
                    result[key] = [
                        lower(_ptr(_ptr(p, key), i), _ptr(_ptr(node_out, key), i), via) for i in range(len(value[key]))
                    ]
            for key in SCHEMA_SINGLE:
                if key in value:
                    result[key] = lower(_ptr(p, key), _ptr(node_out, key), via)
            if "$ref" in value:
                target = lower(self.refs[p], out + "/allOf/0" if result else out, via + (_ptr(p, "$ref"),))
                return {"allOf": [target, result]} if result else target
            return result

        root = lower(path)
        if isinstance(root, bool):
            root = {"allOf": [root]}
        issues = validate_schema(root, {})
        if issues:
            issue = issues[0]
            origin = path
            related = []
            for pointer in sorted(pointers, key=len, reverse=True):
                if issue.path == pointer or issue.path.startswith(pointer + "/"):
                    origin = pointers[pointer] + issue.path[len(pointer) :]
                    related = [RelatedLocation(path=p, message="Referring schema.") for p in referring[pointer]]
                    break
            raise _Failure("SCHEMA", origin, issue.model_copy(update={"path": origin, "related": related}))

        def strip(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            result = {
                k: v
                for k, v in value.items()
                if k not in {"title", "description", "default", "examples", "deprecated", "$comment"}
            }
            for k in SCHEMA_MAPS:
                if k in result:
                    result[k] = {n: strip(v) for n, v in result[k].items()}
            for k in SCHEMA_ARRAYS:
                if k in result:
                    result[k] = [strip(v) for v in result[k]]
            for k in SCHEMA_SINGLE:
                if k in result:
                    result[k] = strip(result[k])
            return result

        if generated:
            self.schema_sources.update({generated + key: value for key, value in pointers.items()})
        return dict(strip(root))

    def security(self, operation: dict[str, Any], path: str) -> None:
        path = path if "security" in operation else ""
        security = operation.get("security", self.value.get("security", []))
        auth = self.policy.auth
        if security in ([], [{}]):
            if auth.kind != "none":
                _fail("SECURITY", _ptr(path, "security"))
            return
        if not isinstance(security, list) or len(security) != 1 or len(security[0]) != 1:
            _fail("SECURITY", _ptr(path, "security"))
        name, scopes = next(iter(security[0].items()))
        scheme, p = self.deref(_ptr("/components/securitySchemes", name))
        _fields(scheme, {"type", "in", "name", "scheme", "bearerFormat", "flows"}, p)
        kind = scheme.get("type")
        if kind == "apiKey":
            if (
                auth.kind != "api-key"
                or scheme.get("in") != "header"
                or scheme.get("name") != auth.header
                or scopes != []
            ):
                _fail("SECURITY", p)
        elif kind == "http":
            if scheme.get("scheme") not in {"basic", "bearer"} or auth.kind != scheme["scheme"] or scopes != []:
                _fail("SECURITY", p)
        elif kind == "oauth2":
            flows = scheme.get("flows")
            if auth.kind != "machine-token" or not isinstance(flows, dict) or set(flows) != {"clientCredentials"}:
                _fail("SECURITY", p)
            flow = _fields(
                flows["clientCredentials"], {"tokenUrl", "scopes"}, _ptr(_ptr(p, "flows"), "clientCredentials")
            )
            if (
                flow.get("tokenUrl") != auth.endpoint
                or not isinstance(scopes, list)
                or any(not isinstance(s, str) for s in scopes)
                or len(set(scopes)) != len(scopes)
                or set(scopes) != set(auth.scopes)
                or not isinstance(flow.get("scopes"), dict)
                or not set(scopes) <= set(flow["scopes"])
            ):
                _fail("SECURITY", p)
        else:
            _fail("SECURITY", p)

    def operation(
        self, identifier: str, indexed: tuple[str, str, dict[str, Any], dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        path, method, item, op = indexed
        rule = self.policy.operations[identifier]
        p, q = _ptr("/paths", path), _ptr(_ptr("/paths", path), method)
        _fields(item, set(METHODS) | {"parameters", "servers"}, p)
        _fields(op, {"operationId", "parameters", "servers", "security", "requestBody", "responses"}, q)
        if method not in {"get", "head", "post", "put", "patch", "delete"}:
            _fail("UNSUPPORTED", q)
        self.security(op, q)
        server_owner = q if "servers" in op else p if "servers" in item else ""
        servers_path = _ptr(server_owner, "servers")
        servers = op.get("servers", item.get("servers", self.value.get("servers", [])))
        if not isinstance(servers, list) or not servers:
            _fail("POLICY", servers_path)
        found = False
        for i, server in enumerate(servers):
            _fields(server, {"url"}, _ptr(servers_path, i))
            if server.get("url") == rule.server:
                found = True
        if not found:
            _fail("POLICY", servers_path)
        try:
            origin, base_path = fixed_server(rule.server)
            template_names(path)
        except ValueError:
            _fail("POLICY", p)
        self.origins.add(origin)
        effective: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
        for owner, where in ((item, p), (op, q)):
            seen: set[tuple[str, str]] = set()
            for i in range(len(owner.get("parameters", []))):
                param, pp = self.deref(_ptr(_ptr(where, "parameters"), i))
                _fields(
                    param,
                    {"name", "in", "required", "schema", "style", "explode", "allowReserved", "allowEmptyValue"},
                    pp,
                )
                if not isinstance(param.get("name"), str) or param.get("in") not in {"path", "query", "header"}:
                    _fail("UNSUPPORTED", pp)
                key = param["in"], param["name"]
                if key in seen:
                    _fail("NAME_COLLISION", pp)
                seen.add(key)
                effective[key] = param, pp
        if len(effective) > 64:
            _fail("RESOURCE_LIMIT", q)
        groups: dict[str, dict[str, Any]] = {}
        required_groups: list[str] = []
        params: list[HttpParameter] = []
        for (location, name), (param, pp) in sorted(effective.items()):
            expected = "form" if location == "query" else "simple"
            if (
                param.get("style", expected) != expected
                or param.get("allowReserved", False) is not False
                or param.get("allowEmptyValue", False) is not False
            ):
                _fail("UNSUPPORTED", pp)
            if (
                type(param.get("required", False)) is not bool
                or type(param.get("explode", location == "query")) is not bool
            ):
                _fail("UNSUPPORTED", pp)
            generated = (
                self.generated_prefix
                + "/spec/inputSchema/properties/"
                + {"path": "path", "query": "query", "header": "headers"}[location]
                + "/properties/"
                + name.replace("~", "~0").replace("/", "~1")
            )
            schema = self.schema(_ptr(pp, "schema"), generated) if "schema" in param else _fail("UNSUPPORTED", pp)
            # Parameters have a deliberately small, statically serializable profile.
            array = schema.get("type") == "array"
            scalar = schema.get("items", {}) if array else schema
            if (
                not isinstance(scalar, dict)
                or scalar.get("type") not in {"string", "integer", "boolean"}
                or any(k in scalar for k in (*SCHEMA_ARRAYS, *SCHEMA_SINGLE, *SCHEMA_MAPS, "$ref"))
                or array
                and (
                    location != "query"
                    or param.get("explode", True) is not True
                    or type(schema.get("maxItems")) is not int
                    or not 0 <= schema["maxItems"] <= 100
                )
                or location != "query"
                and param.get("explode", False) is not False
                or scalar.get("type") == "string"
                and (type(scalar.get("maxLength")) is not int or not 0 <= scalar["maxLength"] <= 4096)
            ):
                _fail("UNSUPPORTED", pp)
            if array and set(schema) - {"type", "items", "minItems", "maxItems", "uniqueItems"}:
                _fail("UNSUPPORTED", pp)
            if location == "header" and (protected(name) or name.lower() == (self.policy.auth.header or "").lower()):
                _fail("SECURITY", pp)
            try:
                profile = HttpParameter(
                    name=name,
                    location=cast(Any, location),
                    type=scalar["type"],
                    array=array,
                    required=param.get("required", False),
                )
            except ValidationError:
                _fail("UNSUPPORTED", pp)
            params.append(profile)
            group_name = {"path": "path", "query": "query", "header": "headers"}[location]
            group = groups.setdefault(group_name, object_schema({}))
            group["properties"][name] = schema
            if profile.required:
                group.setdefault("required", []).append(name)
                if group_name not in required_groups:
                    required_groups.append(group_name)
        if "requestBody" in op:
            body, bp = self.deref(_ptr(q, "requestBody"))
            _fields(body, {"required", "content"}, bp)
            if method in {"get", "head"} or type(body.get("required", False)) is not bool:
                _fail("UNSUPPORTED", bp)
            groups["body"] = self.content_schema(body, bp, self.generated_prefix + "/spec/inputSchema/properties/body")
            if body.get("required", False):
                required_groups.append("body")
        branches, empty = [], []
        for response_index, status in enumerate(rule.statuses):
            response_path = _ptr(_ptr(q, "responses"), str(status))
            if response_path not in self.nodes:
                _fail("POLICY", response_path)
            response, rp = self.deref(response_path)
            _fields(response, {"content"}, rp)
            if "content" not in response:
                body_schema = {"type": "null"}
                empty.append(status)
            else:
                body_schema = self.content_schema(
                    response, rp, self.generated_prefix + f"/spec/outputSchema/oneOf/{response_index}/properties/body"
                )
            branches.append(object_schema({"status": {"const": status}, "body": body_schema}, ["status", "body"]))
        try:
            operation = HttpOperation(
                method=cast(Any, method.upper()),
                path=base_path + path,
                sideEffect=rule.side_effect,
                parameters=params,
                statuses=rule.statuses,
                emptyStatuses=empty,
            )
        except ValidationError:
            _fail("POLICY", q)
        config = operation.model_dump(by_alias=True)
        inputs, outputs = object_schema(groups, required_groups), {"oneOf": branches}
        action = {
            "apiVersion": "weave/v1alpha1",
            "kind": "Action",
            "metadata": {"name": rule.name, "version": self.policy.version},
            "spec": {
                "implementation": {
                    "kind": "connector",
                    "uses": f"{self.policy.name}@{self.policy.version}",
                    "action": rule.name,
                    "config": config,
                },
                "sideEffect": rule.side_effect,
                "timeoutSeconds": self.policy.timeout_seconds,
                "inputSchema": inputs,
                "outputSchema": outputs,
                "connection": {"connector": f"{self.policy.name}@{self.policy.version}"},
            },
        }
        declaration = {
            "configSchema": {"const": config},
            "inputSchema": inputs,
            "outputSchema": outputs,
            "sideEffect": rule.side_effect,
            "timeoutSeconds": self.policy.timeout_seconds,
        }
        self.source_pointers[rule.name] = q
        self.schema_sources[self.generated_prefix + "/spec/implementation/config/path"] = p
        return action, declaration

    def content_schema(self, owner: dict[str, Any], path: str, generated: str = "") -> dict[str, Any]:
        content = owner.get("content")
        if not isinstance(content, dict) or set(content) != {"application/json"}:
            _fail("UNSUPPORTED", _ptr(path, "content"))
        p = _ptr(_ptr(path, "content"), "application/json")
        media = _fields(content["application/json"], {"schema"}, p)
        if "schema" not in media:
            _fail("UNSUPPORTED", p)
        return self.schema(_ptr(p, "schema"), generated)

    def run(self, selected: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        operations = self.inventory()
        if (
            not selected
            or len(selected) > 100
            or any(not isinstance(i, str) for i in selected)
            or len(set(selected)) != len(selected)
            or set(selected) != set(self.policy.operations)
            or not set(selected) <= set(operations)
        ):
            _fail("POLICY")
        if (
            not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", self.policy.name)
            or self.policy.name.startswith("weave-")
            or len(self.policy.name) > 64
            or keyword.iskeyword(self.policy.name.replace("-", "_"))
        ):
            _fail("POLICY")
        if len({r.name for r in self.policy.operations.values()}) != len(selected):
            _fail("NAME_COLLISION")
        actions, declarations = [], {}
        for selected_index, identifier in enumerate(sorted(selected)):
            self.generated_prefix = f"/actions/{selected_index}"
            action, declaration = self.operation(identifier, operations[identifier])
            actions.append(action)
            declarations[self.policy.operations[identifier].name] = declaration
        if len(self.origins) != 1:
            _fail("POLICY")
        config = {"baseUrl": next(iter(self.origins)), "auth": self.policy.auth.model_dump(exclude_none=True)}
        connector: dict[str, Any] = {
            "apiVersion": "weave/v1alpha1",
            "kind": "Connector",
            "metadata": {"name": self.policy.name, "version": self.policy.version},
            "spec": {
                "adapter": self.policy.name,
                "configSchema": {"const": config},
                "authSchema": object_schema(
                    {s: {"type": "string"} for s in sorted(self.policy.auth.slots())}, sorted(self.policy.auth.slots())
                ),
                "actions": declarations,
                "compatibility": {"apiVersion": "weave/v1alpha1"},
                "limits": {
                    "maxRequestBytes": self.policy.max_request_bytes,
                    "maxResponseBytes": self.policy.max_response_bytes,
                    "maxTimeoutSeconds": self.policy.timeout_seconds,
                },
            },
        }
        connector = ConnectorDefinition.model_validate(connector).model_dump(by_alias=True)
        digest = FrozenDocument.from_value(connector).digest
        capabilities: list[Any] = []
        bindings: list[Any] = []
        for name, decl in declarations.items():
            task = f"weave-connector-{self.policy.name}-{name}"
            capabilities.append(
                {
                    "taskType": task,
                    "taskVersion": self.policy.version,
                    **{k: decl[k] for k in ("inputSchema", "outputSchema", "sideEffect", "timeoutSeconds")},
                }
            )
            bindings.append(
                {
                    "connector_digest": digest,
                    "action": name,
                    "adapter": self.policy.name,
                    "implementation_version": self.policy.version,
                    "task_reference": f"{task}@{self.policy.version}",
                }
            )
        metadata = PackageMetadata(
            FrozenDocument.from_value(
                {
                    "format": "weave/connector-package-v1",
                    "distribution": self.policy.name,
                    "version": self.policy.version,
                    "family": "http",
                    "protocol_versions": ["openapi-3.1", "http-profile-2"],
                    "service": self.policy.name.replace("-", "_") + ":ImportedHttpConnector",
                    "manifest": connector,
                    "capabilities": capabilities,
                    "bindings": bindings,
                }
            )
        )
        catalog = CatalogSnapshot.from_definitions(
            [metadata.model.manifest], tasks=metadata.model.capabilities, adapters=[self.policy.name]
        )
        for action in actions:
            result = compile_source(action, format="object", catalog=catalog)
            if not result.ok:
                _fail("COMPILE")
        package = metadata.document
        if (
            len(
                canonical_bytes(
                    cast(
                        Any,
                        {
                            "connector": connector,
                            "actions": actions,
                            "package": package,
                            "sourceMap": self.schema_sources,
                        },
                    )
                )
            )
            > 1048576
        ):
            _fail("RESOURCE_LIMIT")
        return connector, actions, package


def import_openapi(
    document: bytes | str | dict[str, Any], operation_ids: list[str], policy: OpenAPIImportPolicy | dict[str, Any]
) -> OpenAPIImportResult:
    """Convert selected local operations without executing or installing their package."""
    locations: dict[str, Any] = {}
    engine: _Importer | None = None
    try:
        parsed = parse_source(document, format="object" if isinstance(document, dict) else "json")
        locations = parsed.locations
        policy_value = policy.model_dump(by_alias=True) if isinstance(policy, OpenAPIImportPolicy) else policy
        bounded = parse_source(policy_value, format="object")
        rules = OpenAPIImportPolicy.model_validate(bounded.value)
        engine = _Importer(parsed.value, rules)
        connector, actions, package = engine.run(operation_ids)
        return OpenAPIImportResult(
            ok=True,
            connector=connector,
            actions=actions,
            package=package,
            sourceMap=engine.schema_sources,
            provenance={
                "profileVersion": "2.0.0",
                "sourceDigest": parsed.source_hash,
                "policyDigest": hashlib.sha256(canonical_bytes(bounded.value)).hexdigest(),
                "operations": dict(engine.source_pointers),
            },
        )
    except ParseFailure as exc:
        return OpenAPIImportResult(
            ok=False, diagnostics=[sanitize_import_diagnostic(d) for d in exc.diagnostics], truncated=exc.truncated
        )
    except _Failure as exc:
        diagnostic = exc.diagnostic or Diagnostic(
            code="WV-IMPORT-" + exc.code,
            severity="error",
            stage="semantic",
            message="OpenAPI input is outside the supported import policy.",
            path=exc.path,
        )
        related = list(diagnostic.related) + [RelatedLocation(path=p, message="Referring schema.") for p in exc.related]
        if engine is not None:
            for target, references in engine.ref_context.items():
                if exc.path == target or exc.path.startswith(target + "/"):
                    related.extend(
                        RelatedLocation(path=p, message="Referring object.") for p in dict.fromkeys(references)
                    )
        diagnostic = diagnostic.model_copy(
            update={
                "source": locations.get(exc.path),
                "related": [r.model_copy(update={"source": locations.get(r.path)}) for r in related],
            }
        )
        diagnostic = sanitize_import_diagnostic(diagnostic)
        return OpenAPIImportResult(ok=False, diagnostics=[diagnostic])
    except (ValueError, TypeError, KeyError, RecursionError):
        return OpenAPIImportResult(
            ok=False,
            diagnostics=[
                Diagnostic(
                    code="WV-IMPORT-POLICY",
                    severity="error",
                    stage="semantic",
                    message="OpenAPI input is outside the supported import policy.",
                    path="",
                )
            ],
        )
