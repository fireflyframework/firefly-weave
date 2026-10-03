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
"""Pure selected-operation OpenAPI import with bounded local references and no I/O.

Two targets share one importer. ``package`` (the default) emits a reviewable
connector package: a new adapter identity, its manifest and one Action per
operation. ``builtin`` emits Actions on the built-in ``weave-http@2.0.0``
connector instead, plus a connection template with secret-handle placeholders,
so no code needs to be built, installed or allowlisted. Inputs are local text
or objects only: nothing is fetched. Unsupported constructs fail closed unless
the reviewed policy names an explicit relaxation, and every applied relaxation
is reported as a warning.
"""

import copy
import hashlib
import json
import keyword
import re
from collections.abc import Sequence
from typing import Any, Literal, NamedTuple, NoReturn, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, ValidationError

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.parser import ParsedSource, ParseFailure, parse_source
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
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonObjectData

OAS_DIALECT = "https://spec.openapis.org/oas/3.1/dialect/base"
METHODS = ("get", "head", "post", "put", "patch", "delete", "options", "trace")
ANNOTATIONS = {"description", "summary", "title", "deprecated", "tags", "externalDocs"}


INT_BOUNDS = {
    "int32": (-(2**31), 2**31 - 1),
    # JSON values are bounded to the interoperable safe-integer range.
    "int64": (-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER),
}
NUMERIC_FORMATS = frozenset({*INT_BOUNDS, "float", "double"})
LARGE_SOURCE_BYTES = 8 * 1024 * 1024
LARGE_LIMITS = Limits(max_source_bytes=LARGE_SOURCE_BYTES, max_document_nodes=1_000_000)
OPERATION_LIMIT = 1000
# Schema validity is pure, so an inventory reuses verdicts for schemas shared by many operations.
SCHEMA_CACHE_ENTRIES = 4096
SCHEMA_CACHE_KEY_CHARS = 262144


def _absent(value: object) -> bool:
    return value is None


class OperationPolicy(ContractModel):
    name: ResourceName
    side_effect: Literal["read_only", "non_idempotent"] = Field(alias="sideEffect")
    server: str
    statuses: list[int] = Field(min_length=1, max_length=20)


class ImportRelaxations(ContractModel):
    """Reviewed, opt-in departures from the strict profile; each one applied is reported as a warning."""

    default_string_max_length: int | None = Field(
        default=None, alias="defaultStringMaxLength", ge=1, le=4096, exclude_if=_absent
    )
    numeric_formats: bool = Field(default=False, alias="numericFormats")
    ignore_response_headers: bool = Field(default=False, alias="ignoreResponseHeaders")
    json_media_only: bool = Field(default=False, alias="jsonMediaOnly")
    upgrade_openapi_30: bool = Field(default=False, alias="upgradeOpenapi30")


class OpenAPIImportPolicy(ContractModel):
    name: ResourceName
    version: SemVer
    auth: AuthProfile
    operations: dict[str, OperationPolicy] = Field(min_length=1, max_length=100)
    timeout_seconds: int = Field(default=30, ge=1, le=30, alias="timeoutSeconds")
    max_request_bytes: int = Field(default=1048576, ge=1, le=1048576, alias="maxRequestBytes")
    max_response_bytes: int = Field(default=1048576, ge=1, le=1048576, alias="maxResponseBytes")
    relaxations: ImportRelaxations | None = Field(default=None, exclude_if=_absent)


class OpenAPIImportResult(ContractModel):
    ok: bool
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    truncated: bool = False
    connector: JsonObjectData | None = None
    actions: list[JsonObjectData] = Field(default_factory=list)
    package: JsonObjectData | None = None
    provenance: JsonObjectData | None = None
    source_map: dict[str, str] = Field(default_factory=dict, alias="sourceMap")
    connection_example: JsonObjectData | None = Field(default=None, alias="connectionExample", exclude_if=_absent)


class InventoryOperation(ContractModel):
    """One operation's verdict: would it import alone, with the given relaxations, and why not."""

    key: str
    operation_id: str | None = Field(default=None, alias="operationId")
    method: str
    path: str
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    supported: bool
    side_effect: Literal["read_only", "non_idempotent"] = Field(alias="sideEffect")
    statuses: list[int] = Field(default_factory=list)
    reasons: list[Diagnostic] = Field(default_factory=list)


class OpenAPIInventory(ContractModel):
    ok: bool
    openapi: str | None = None
    title: str | None = None
    servers: list[str] = Field(default_factory=list)
    operations: list[InventoryOperation] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    truncated: bool = False


class PolicyScaffold(ContractModel):
    ok: bool
    policy: JsonObjectData | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


_MESSAGES: dict[str, tuple[str, str]] = {
    "VERSION": (
        "Only OpenAPI 3.1 documents are imported.",
        "Convert the document to OpenAPI 3.1, or set relaxations.upgradeOpenapi30 to upgrade a 3.0.x document "
        "(nullable, boolean exclusive bounds, example) with warnings.",
    ),
    "DIALECT": (
        "The document's JSON Schema dialect is not supported.",
        "Use the OpenAPI 3.1 base dialect or JSON Schema 2020-12.",
    ),
    "UNSUPPORTED": (
        "This construct is outside the supported HTTP profile.",
        "Choose another operation, or change the document; see the HTTP profile limits.",
    ),
    "SECURITY": (
        "The security requirement does not match the policy's authentication.",
        "Use one scheme per operation (header API key, HTTP basic or bearer, OAuth client credentials) "
        "and set policy.auth to match it.",
    ),
    "POLICY": (
        "The import policy does not match the document.",
        "Check the selected operation IDs, the server URL and the declared 2xx statuses in the policy.",
    ),
    "REMOTE_REF": (
        "References to other documents are never fetched.",
        "Bundle the document so that every $ref is local (#/components/...).",
    ),
    "INVALID_REF": ("A local reference is invalid or ambiguous.", "Point each $ref at an existing component."),
    "RECURSIVE_REF": (
        "Recursive references are not supported.",
        "Choose operations whose schemas are not recursive.",
    ),
    "RESOURCE_LIMIT": (
        "The document exceeds an import budget.",
        "Select fewer operations or simplify the referenced schemas; --target builtin checks only what the "
        "selected operations reference.",
    ),
    "CREDENTIAL_METADATA": (
        "The document appears to contain credential material.",
        "Remove credentials from the document; connections reference secrets by handle later.",
    ),
    "NAME_COLLISION": ("Two names or path templates collide.", "Give every selected operation a distinct name."),
    "DUPLICATE_OPERATION": ("Two operations share an operationId.", "Make every operationId unique."),
    "COMPILE": (
        "A generated Action does not compile.",
        "Report this document to the platform team; the generated data is inconsistent.",
    ),
    "OPERATION_ID": (
        "The operation has no usable operationId and cannot be selected.",
        "Add an operationId of letters, digits, '_', '.' or '-' (at most 128 characters).",
    ),
    "STATUSES": (
        "The operation declares no 2xx status the profile can return.",
        "Declare a 2xx response with an application/json body or no content.",
    ),
    "SERVER": (
        "The operation has no fixed HTTPS server.",
        "Declare an https:// server URL without variables, credentials or query.",
    ),
}
_REASONS: dict[str, tuple[str, str]] = {
    "STRING_MAX_LENGTH": (
        "A string parameter has no maxLength.",
        "Add maxLength to the parameter schema, or set relaxations.defaultStringMaxLength.",
    ),
    "RESPONSE_HEADERS": (
        "The response declares headers, which the profile cannot return.",
        "Set relaxations.ignoreResponseHeaders to import the body only.",
    ),
    "MEDIA_TYPE": (
        "Only application/json content is supported.",
        "Set relaxations.jsonMediaOnly to drop other media types when application/json is present.",
    ),
    "BUILTIN_EFFECT": (
        "The built-in weave-http@2.0.0 connector derives the side effect from the method.",
        "Set sideEffect to read_only for GET and HEAD and to non_idempotent for other methods, or import "
        "with --target package to keep a non-idempotent GET.",
    ),
}
_WARNINGS: dict[str, tuple[str, str]] = {
    "RELAXED_FORMAT": (
        "A numeric format was replaced by bounds or dropped (relaxations.numericFormats).",
        "Review the bounds; int64 is narrowed to the JSON safe-integer range.",
    ),
    "RELAXED_MAX_LENGTH": (
        "A string parameter without maxLength received the policy default (relaxations.defaultStringMaxLength).",
        "Prefer an explicit maxLength in the document.",
    ),
    "RELAXED_RESPONSE_HEADERS": (
        "Response headers were ignored (relaxations.ignoreResponseHeaders).",
        "Workflows receive only the status and the JSON body.",
    ),
    "RELAXED_MEDIA": (
        "Non-JSON media types were dropped (relaxations.jsonMediaOnly).",
        "Only application/json is sent and accepted.",
    ),
    "RELAXED_OPENAPI_30": (
        "The OpenAPI 3.0.x document was upgraded to 3.1 (relaxations.upgradeOpenapi30).",
        "Review nullable, exclusive bounds and examples in the related locations.",
    ),
    "PRUNED": (
        "The document exceeds a whole-document import budget, so it was pruned to the selected operations "
        "and the components they reference.",
        "Credential checks still covered the whole document; structural checks covered the selection.",
    ),
}
_SCHEMA_HINTS = {
    "WV-SCHEMA-UNSUPPORTED_FORMAT": "Supported formats are date-time, uuid, uri and email; set "
    "relaxations.numericFormats to replace int32/int64 with bounds and drop float/double.",
    "WV-SCHEMA-UNSUPPORTED_KEYWORD": "Remove the keyword; for an OpenAPI 3.0.x document set "
    "relaxations.upgradeOpenapi30.",
}


class _Failure(Exception):
    def __init__(
        self,
        code: str,
        path: str = "",
        diagnostic: Diagnostic | None = None,
        related: tuple[str, ...] = (),
        reason: str = "",
    ) -> None:
        self.code, self.path, self.diagnostic, self.related, self.reason = code, path, diagnostic, related, reason


class _Failures(Exception):
    """Every failure found when diagnostics are collected instead of stopping at the first."""

    def __init__(self, failures: list[_Failure]) -> None:
        self.failures = failures


def _fail(code: str, path: str = "", *, related: tuple[str, ...] = (), reason: str = "") -> NoReturn:
    raise _Failure(code, path, related=related, reason=reason)


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


def _metadata_guard(node: Any, where: str) -> None:
    """Reject credential-bearing keys and URLs anywhere in the document, used or not."""
    if isinstance(node, list):
        for i, entry in enumerate(node):
            _metadata_guard(entry, _ptr(where, i))
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
            if key in {"client_secret", "clientSecret", "access_token", "password", "token"} and isinstance(entry, str):
                _fail("CREDENTIAL_METADATA", where)
            _metadata_guard(entry, _ptr(where, key))


class _Importer:
    def __init__(
        self,
        value: dict[str, Any],
        policy: OpenAPIImportPolicy,
        *,
        collect: bool = False,
        schema_cache: dict[str, tuple[Diagnostic, ...]] | None = None,
    ) -> None:
        self.value, self.policy = value, policy
        self.schema_cache = schema_cache
        self.inventoried = False
        self.relax = policy.relaxations or ImportRelaxations()
        self.collect = collect
        self.warnings: dict[tuple[str, str], tuple[str, str]] = {}
        self.built: dict[str, tuple[HttpOperation, dict[str, Any], dict[str, Any]]] = {}
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

    def warn(self, code: str, path: str) -> None:
        message, hint = _WARNINGS[code]
        self.warnings.setdefault((code, path), (message, hint))

    def attempt(self, errors: list[_Failure], step: Any) -> Any:
        """Run one independent check; collect its failure when diagnostics are collected."""
        try:
            return step()
        except _Failure as failure:
            if not self.collect:
                raise
            errors.append(failure)
        except _Failures as failures:
            if not self.collect:
                raise
            errors.extend(failures.failures)
        return None

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

        _metadata_guard(value, "")
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
            if (
                self.relax.numeric_formats
                and not classification_only
                and isinstance(result.get("format"), str)
                and result["format"] in NUMERIC_FORMATS
            ):
                bounds = INT_BOUNDS.get(result.pop("format"))
                if bounds is not None:
                    result.setdefault("minimum", bounds[0])
                    result.setdefault("maximum", bounds[1])
                self.warn("RELAXED_FORMAT", _ptr(p, "format"))
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
        issues = self.schema_issues(root)
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

    def schema_issues(self, root: Any) -> tuple[Diagnostic, ...]:
        """``validate_schema`` (pure), reusing an earlier verdict for an identical lowered schema."""
        if self.schema_cache is None:
            return validate_schema(root, {})
        key = json.dumps(root, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        cached = self.schema_cache.get(key)
        if cached is None:
            cached = validate_schema(root, {})
            if len(key) <= SCHEMA_CACHE_KEY_CHARS and len(self.schema_cache) < SCHEMA_CACHE_ENTRIES:
                self.schema_cache[key] = cached
        return cached

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
        errors: list[_Failure] = []

        def parameter(location: str, name: str, param: dict[str, Any], pp: str) -> None:
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
            if isinstance(scalar, dict) and scalar.get("type") == "string" and "maxLength" not in scalar:
                if self.relax.default_string_max_length is None:
                    _fail("UNSUPPORTED", pp, reason="STRING_MAX_LENGTH")
                scalar["maxLength"] = self.relax.default_string_max_length
                self.warn("RELAXED_MAX_LENGTH", _ptr(pp, "schema"))
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

        for (location, name), (param, pp) in sorted(effective.items()):
            self.attempt(errors, lambda: parameter(location, name, param, pp))  # noqa: B023

        def request_body() -> None:
            body, bp = self.deref(_ptr(q, "requestBody"))
            _fields(body, {"required", "content"}, bp)
            if method in {"get", "head"} or type(body.get("required", False)) is not bool:
                _fail("UNSUPPORTED", bp)
            groups["body"] = self.content_schema(body, bp, self.generated_prefix + "/spec/inputSchema/properties/body")
            if body.get("required", False):
                required_groups.append("body")

        if "requestBody" in op:
            self.attempt(errors, request_body)
        branches: list[dict[str, Any]] = []
        empty: list[int] = []

        def status_branch(response_index: int, status: int) -> None:
            response_path = _ptr(_ptr(q, "responses"), str(status))
            if response_path not in self.nodes:
                _fail("POLICY", response_path)
            response, rp = self.deref(response_path)
            if "headers" in response:
                if not self.relax.ignore_response_headers:
                    _fail("UNSUPPORTED", _ptr(rp, "headers"), reason="RESPONSE_HEADERS")
                self.warn("RELAXED_RESPONSE_HEADERS", _ptr(rp, "headers"))
            _fields(response, {"content", "headers"}, rp)
            if "content" not in response:
                body_schema: dict[str, Any] = {"type": "null"}
                empty.append(status)
            else:
                body_schema = self.content_schema(
                    response, rp, self.generated_prefix + f"/spec/outputSchema/oneOf/{response_index}/properties/body"
                )
            branches.append(object_schema({"status": {"const": status}, "body": body_schema}, ["status", "body"]))

        for response_index, status in enumerate(rule.statuses):
            self.attempt(errors, lambda: status_branch(response_index, status))  # noqa: B023
        if errors:
            raise _Failures(errors)
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
        self.built[rule.name] = operation, inputs, outputs
        return action, declaration

    def content_schema(self, owner: dict[str, Any], path: str, generated: str = "") -> dict[str, Any]:
        content = owner.get("content")
        if not isinstance(content, dict) or "application/json" not in content:
            _fail("UNSUPPORTED", _ptr(path, "content"), reason="MEDIA_TYPE")
        if set(content) != {"application/json"}:
            if not self.relax.json_media_only:
                _fail("UNSUPPORTED", _ptr(path, "content"), reason="MEDIA_TYPE")
            self.warn("RELAXED_MEDIA", _ptr(path, "content"))
        p = _ptr(_ptr(path, "content"), "application/json")
        media = _fields(content["application/json"], {"schema"}, p)
        if "schema" not in media:
            _fail("UNSUPPORTED", p)
        return self.schema(_ptr(p, "schema"), generated)

    def run(
        self, selected: list[str], *, target: Literal["package", "builtin"] = "package", compile_actions: bool = True
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
        operations = self.inventory()
        self.inventoried = True
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
        errors: list[_Failure] = []
        for selected_index, identifier in enumerate(sorted(selected)):
            self.generated_prefix = f"/actions/{selected_index}"
            built = self.attempt(errors, lambda: self.operation(identifier, operations[identifier]))  # noqa: B023
            if built is not None:
                actions.append(built[0])
                declarations[self.policy.operations[identifier].name] = built[1]
        if errors:
            raise _Failures(errors)
        if target == "builtin":
            return None, self.builtin_actions(sorted(selected), compile_actions), None
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

    def builtin_actions(self, selected: list[str], compile_actions: bool) -> list[dict[str, Any]]:
        """Actions on ``weave-http@2.0.0``: same operation, schemas and source map, no new adapter."""
        from firefly_weave.sdk.http_actions import (
            READ_METHODS,
            action_document,
            check_http_action,
            compile_http_action,
        )

        if len(self.origins) != 1:
            _fail("POLICY")
        actions = []
        errors: list[_Failure] = []

        def build(identifier: str) -> None:
            rule = self.policy.operations[identifier]
            operation, inputs, outputs = self.built[rule.name]
            if (operation.method in READ_METHODS) != (rule.side_effect == "read_only"):
                # The package target may keep a reviewed non-idempotent GET; the built-in read action cannot.
                _fail("POLICY", self.source_pointers[rule.name], reason="BUILTIN_EFFECT")
            action = action_document(
                rule.name, self.policy.version, operation, inputs, outputs, self.policy.timeout_seconds
            )
            checked = check_http_action(action)
            if any(d.severity == "error" for d in checked) or compile_actions and not compile_http_action(action).ok:
                _fail("COMPILE", self.source_pointers[rule.name])
            actions.append(action)

        for identifier in selected:
            self.attempt(errors, lambda: build(identifier))  # noqa: B023
        if errors:
            raise _Failures(errors)
        if len(canonical_bytes(cast(Any, {"actions": actions, "sourceMap": self.schema_sources}))) > 1048576:
            _fail("RESOURCE_LIMIT")
        return actions


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


type SourceFormat = Literal["json", "yaml"]
type ImportTarget = Literal["package", "builtin"]
_GENERIC = "OpenAPI input is outside the supported import policy."


def _diagnostic(failure: _Failure, engine: _Importer | None, locations: dict[str, Any]) -> Diagnostic:
    if failure.diagnostic is not None:
        diagnostic = failure.diagnostic
        if diagnostic.hint is None and diagnostic.code in _SCHEMA_HINTS:
            diagnostic = diagnostic.model_copy(update={"hint": _SCHEMA_HINTS[diagnostic.code]})
    else:
        message, hint = _REASONS.get(failure.reason) or _MESSAGES.get(failure.code) or (_GENERIC, "")
        diagnostic = Diagnostic(
            code="WV-IMPORT-" + failure.code,
            severity="error",
            stage="semantic",
            message=message,
            path=failure.path,
            hint=hint or None,
        )
    related = list(diagnostic.related) + [RelatedLocation(path=p, message="Referring schema.") for p in failure.related]
    if engine is not None:
        for target, references in engine.ref_context.items():
            if failure.path == target or failure.path.startswith(target + "/"):
                related.extend(RelatedLocation(path=p, message="Referring object.") for p in dict.fromkeys(references))
    diagnostic = diagnostic.model_copy(
        update={
            "source": locations.get(failure.path),
            "related": [r.model_copy(update={"source": locations.get(r.path)}) for r in related],
        }
    )
    return sanitize_import_diagnostic(diagnostic)


def _failures(error: _Failure | _Failures, engine: _Importer | None, locations: dict[str, Any]) -> list[Diagnostic]:
    failures = error.failures if isinstance(error, _Failures) else [error]
    return [_diagnostic(failure, engine, locations) for failure in failures]


def _warning(code: str, path: str, locations: dict[str, Any], related: Sequence[str] = ()) -> Diagnostic:
    message, hint = _WARNINGS[code]
    return sanitize_import_diagnostic(
        Diagnostic(
            code="WV-IMPORT-" + code,
            severity="warning",
            stage="semantic",
            message=message,
            path=path,
            hint=hint,
            source=locations.get(path),
            related=[RelatedLocation(path=p, source=locations.get(p)) for p in related[:20]],
        )
    )


def _engine_warnings(engine: _Importer | None, locations: dict[str, Any]) -> list[Diagnostic]:
    if engine is None:
        return []
    return [_warning(code, path, locations) for code, path in engine.warnings]


def _parse(document: bytes | str | dict[str, Any], source_format: SourceFormat) -> tuple[ParsedSource, bool]:
    """Parse within the default budget; a larger local document (up to 8 MiB) is marked for pruning."""
    if isinstance(document, dict):
        return parse_source(document, format="object"), False
    try:
        size = len(document) if isinstance(document, bytes) else len(document.encode("utf-8"))
    except UnicodeEncodeError:
        size = 0
    large = size > Limits().max_source_bytes
    return parse_source(document, format=source_format, limits=LARGE_LIMITS if large else Limits()), large


def _relaxations(value: ImportRelaxations | dict[str, Any] | None) -> ImportRelaxations:
    if isinstance(value, ImportRelaxations):
        return value
    return ImportRelaxations.model_validate(value or {})


def upgrade_openapi_30(value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a 3.1 copy of a 3.0.x document and the rewritten pointers; other documents are unchanged.

    Rewrites schema ``nullable`` into a ``null`` type (or ``anyOf`` beside ``$ref``),
    boolean ``exclusiveMinimum``/``exclusiveMaximum`` into numeric bounds and
    schema ``example`` into ``examples``. Nothing else is interpreted.
    """
    version = value.get("openapi")
    if not isinstance(version, str) or not re.fullmatch(r"3\.0\.\d{1,3}", version):
        return value, []
    result = copy.deepcopy(value)
    result["openapi"] = "3.1.0"
    changed: list[str] = []
    budget = [200000]

    def schema(node: Any, where: str) -> Any:
        budget[0] -= 1
        if budget[0] < 0:
            _fail("RESOURCE_LIMIT", where)
        if not isinstance(node, dict):
            return node
        if "nullable" in node:
            nullable = node.pop("nullable")
            changed.append(_ptr(where, "nullable"))
            if nullable is True:
                kind = node.get("type")
                if "$ref" in node:
                    return {"anyOf": [{"$ref": node["$ref"]}, {"type": "null"}]}
                if isinstance(kind, str):
                    node["type"] = [kind, "null"]
                elif isinstance(kind, list) and "null" not in kind:
                    node["type"] = [*kind, "null"]
                if isinstance(node.get("enum"), list) and None not in node["enum"]:
                    node["enum"] = [*node["enum"], None]
        for bound, exclusive in (("minimum", "exclusiveMinimum"), ("maximum", "exclusiveMaximum")):
            flag = node.get(exclusive)
            if isinstance(flag, bool):
                del node[exclusive]
                changed.append(_ptr(where, exclusive))
                if flag and bound in node:
                    node[exclusive] = node.pop(bound)
        if "example" in node and "$ref" not in node:
            example = node.pop("example")
            node.setdefault("examples", [example])
            changed.append(_ptr(where, "example"))
        for key in SCHEMA_MAPS:
            if isinstance(node.get(key), dict):
                node[key] = {name: schema(child, _ptr(_ptr(where, key), name)) for name, child in node[key].items()}
        for key in SCHEMA_ARRAYS:
            if isinstance(node.get(key), list):
                node[key] = [schema(child, _ptr(_ptr(where, key), i)) for i, child in enumerate(node[key])]
        for key in SCHEMA_SINGLE:
            if key in node:
                node[key] = schema(node[key], _ptr(where, key))
        return node

    def content(owner: Any, where: str) -> None:
        if isinstance(owner, dict) and isinstance(owner.get("content"), dict):
            for media, entry in owner["content"].items():
                if isinstance(entry, dict) and "schema" in entry:
                    entry["schema"] = schema(entry["schema"], _ptr(_ptr(_ptr(where, "content"), media), "schema"))

    def parameter(owner: Any, where: str) -> None:
        if isinstance(owner, dict):
            if "schema" in owner:
                owner["schema"] = schema(owner["schema"], _ptr(where, "schema"))
            content(owner, where)

    def response(owner: Any, where: str) -> None:
        content(owner, where)
        if isinstance(owner, dict) and isinstance(owner.get("headers"), dict):
            for name, header in owner["headers"].items():
                parameter(header, _ptr(_ptr(where, "headers"), name))

    def path_item(item: Any, where: str) -> None:
        if not isinstance(item, dict):
            return
        for i, entry in enumerate(_items(item.get("parameters"))):
            parameter(entry, _ptr(_ptr(where, "parameters"), i))
        for method in METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            owner = _ptr(where, method)
            parameters = operation.get("parameters")
            for i, entry in enumerate(_items(parameters)):
                parameter(entry, _ptr(_ptr(owner, "parameters"), i))
            content(operation.get("requestBody"), _ptr(owner, "requestBody"))
            responses = operation.get("responses")
            for status, entry in _mapping(responses).items():
                response(entry, _ptr(_ptr(owner, "responses"), status))

    for path, item in _mapping(result.get("paths")).items():
        path_item(item, _ptr("/paths", path))
    components = _mapping(result.get("components"))
    for group, visit in (
        ("parameters", parameter),
        ("headers", parameter),
        ("requestBodies", content),
        ("responses", response),
        ("pathItems", path_item),
    ):
        entries = components.get(group)
        for name, entry in _mapping(entries).items():
            visit(entry, _ptr(_ptr("/components", group), name))
    schemas = components.get("schemas")
    if isinstance(schemas, dict):
        components["schemas"] = {
            name: schema(entry, _ptr("/components/schemas", name)) for name, entry in schemas.items()
        }
    return result, changed


def _pointer_tokens(ref: str) -> list[str]:
    return [token.replace("~1", "/").replace("~0", "~") for token in ref[2:].split("/")] if ref.startswith("#/") else []


def prune_document(value: dict[str, Any], selected: set[str]) -> dict[str, Any]:
    """Keep the selected operations and the closure of what they reference; drop everything else.

    Used for documents above the 1 MiB import budget, after the credential
    guard has covered the whole document. Selected path items keep only the
    selected methods; components are kept whole when any part is referenced.
    """
    paths = _mapping(value.get("paths"))
    components = _mapping(value.get("components"))
    kept_paths: dict[str, Any] = {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        methods = {m for m in METHODS if isinstance(item.get(m), dict) and item[m].get("operationId") in selected}
        if methods:
            kept_paths[path] = {key: entry for key, entry in item.items() if key not in METHODS or key in methods}
    result: dict[str, Any] = {
        key: value[key] for key in ("openapi", "info", "jsonSchemaDialect", "servers", "security") if key in value
    }
    result["paths"] = kept_paths
    kept: dict[str, dict[str, Any]] = {}
    pending: list[Any] = [kept_paths, {"security": result.get("security", [])}]
    budget = 2_000_000

    def keep_scheme(name: Any) -> None:
        schemes = components.get("securitySchemes")
        known = kept.setdefault("securitySchemes", {})
        if isinstance(name, str) and isinstance(schemes, dict) and name in schemes and name not in known:
            known[name] = schemes[name]
            pending.append(schemes[name])

    while pending:
        node = pending.pop()
        budget -= 1
        if budget < 0:
            _fail("RESOURCE_LIMIT")
        if isinstance(node, list):
            pending.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        ref = node.get("$ref")
        tokens = _pointer_tokens(ref) if isinstance(ref, str) else []
        if len(tokens) >= 3 and tokens[0] == "components":
            group, name = tokens[1], tokens[2]
            entries = components.get(group)
            if isinstance(entries, dict) and name in entries and name not in kept.setdefault(group, {}):
                kept[group][name] = entries[name]
                pending.append(entries[name])
        elif len(tokens) >= 2 and tokens[0] == "paths" and tokens[1] in paths and tokens[1] not in kept_paths:
            kept_paths[tokens[1]] = paths[tokens[1]]
            pending.append(paths[tokens[1]])
        security = node.get("security")
        if isinstance(security, list):
            for requirement in security:
                if isinstance(requirement, dict):
                    for name in requirement:
                        keep_scheme(name)
        pending.extend(entry for key, entry in node.items() if key != "$ref")
    if "components" in value:
        result["components"] = {group: entries for group, entries in kept.items() if entries}
    return result


def import_openapi(
    document: bytes | str | dict[str, Any],
    operation_ids: list[str] | None,
    policy: OpenAPIImportPolicy | dict[str, Any],
    *,
    source_format: SourceFormat = "json",
    target: ImportTarget = "package",
    all_diagnostics: bool = False,
) -> OpenAPIImportResult:
    """Convert selected local operations without executing or installing anything.

    ``operation_ids=None`` selects the policy's operations. ``target="builtin"``
    emits Actions on ``weave-http@2.0.0`` and a connection template instead of a
    package. ``all_diagnostics`` reports every failing operation, parameter and
    status instead of stopping at the first.
    """
    from firefly_weave.sdk.http_actions import HTTP_CONNECTOR, connection_example

    locations: dict[str, Any] = {}
    engine: _Importer | None = None
    warnings: list[Diagnostic] = []
    try:
        parsed, large = _parse(document, source_format)
        locations = parsed.locations
        policy_value = policy.model_dump(by_alias=True) if isinstance(policy, OpenAPIImportPolicy) else policy
        bounded = parse_source(policy_value, format="object")
        rules = OpenAPIImportPolicy.model_validate(bounded.value)
        selected = list(rules.operations) if operation_ids is None else operation_ids
        value = parsed.value
        if rules.relaxations is not None and rules.relaxations.upgrade_openapi_30:
            value, upgraded = upgrade_openapi_30(value)
            if upgraded or value is not parsed.value:
                warnings.append(_warning("RELAXED_OPENAPI_30", "/openapi", locations, upgraded))
        if large:
            _metadata_guard(value, "")
            value = prune_document(value, set(selected))
            warnings.append(_warning("PRUNED", "", locations))
        engine = _Importer(value, rules, collect=all_diagnostics)
        try:
            connector, actions, package = engine.run(selected, target=target)
        except _Failure as failure:
            if large or target != "builtin" or engine.inventoried or failure.code != "RESOURCE_LIMIT":
                raise
            # The whole document exceeds the work budget: check credentials everywhere, then import
            # the selection with only what it references, exactly as for large documents. The package
            # target keeps its whole-document structural checks for documents within the source budget.
            _metadata_guard(value, "")
            warnings.append(_warning("PRUNED", "", locations))
            engine = _Importer(prune_document(value, set(selected)), rules, collect=all_diagnostics)
            connector, actions, package = engine.run(selected, target=target)
        warnings.extend(_engine_warnings(engine, locations))
        provenance: dict[str, Any] = {
            "profileVersion": "2.0.0",
            "sourceDigest": parsed.source_hash,
            "policyDigest": hashlib.sha256(canonical_bytes(bounded.value)).hexdigest(),
            "operations": dict(engine.source_pointers),
        }
        example = None
        if target == "builtin":
            provenance |= {"target": "builtin", "connector": HTTP_CONNECTOR}
            example = connection_example(rules.name, next(iter(engine.origins)), rules.auth)
        return OpenAPIImportResult(
            ok=True,
            diagnostics=warnings,
            connector=connector,
            actions=actions,
            package=package,
            sourceMap=engine.schema_sources,
            provenance=provenance,
            connectionExample=example,
        )
    except ParseFailure as exc:
        return OpenAPIImportResult(
            ok=False, diagnostics=[sanitize_import_diagnostic(d) for d in exc.diagnostics], truncated=exc.truncated
        )
    except (_Failure, _Failures) as exc:
        return OpenAPIImportResult(
            ok=False, diagnostics=_failures(exc, engine, locations) + warnings + _engine_warnings(engine, locations)
        )
    except (ValueError, TypeError, KeyError, RecursionError):
        return OpenAPIImportResult(
            ok=False,
            diagnostics=[
                Diagnostic(
                    code="WV-IMPORT-POLICY",
                    severity="error",
                    stage="semantic",
                    message=_GENERIC,
                    path="",
                    hint="Check that the policy matches the documented import policy fields.",
                )
            ],
        )


def _effective_server(value: dict[str, Any], item: dict[str, Any], operation: dict[str, Any]) -> str | None:
    servers = operation.get("servers", item.get("servers", value.get("servers", [])))
    for server in _items(servers):
        url = server.get("url") if isinstance(server, dict) else None
        if isinstance(url, str) and not _credential_url(url):
            try:
                fixed_server(url)
            except ValueError:
                continue
            return url
    return None


def _effective_auth(value: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any] | None:
    """The policy auth matching the operation's single security requirement, or None if unsupported."""
    security = operation.get("security", value.get("security", []))
    if security in ([], [{}]):
        return {"kind": "none"}
    if not isinstance(security, list) or len(security) != 1 or not isinstance(security[0], dict):
        return None
    if len(security[0]) != 1:
        return None
    name, scopes = next(iter(security[0].items()))
    components = _mapping(value.get("components"))
    schemes = _mapping(components.get("securitySchemes"))
    scheme = schemes.get(name)
    if isinstance(scheme, dict) and isinstance(scheme.get("$ref"), str):
        tokens = _pointer_tokens(scheme["$ref"])
        scheme = (
            schemes.get(tokens[2]) if tokens[:2] == ["components", "securitySchemes"] and len(tokens) == 3 else None
        )
    if not isinstance(scheme, dict):
        return None
    kind = scheme.get("type")
    auth: dict[str, Any] | None = None
    if kind == "apiKey" and scheme.get("in") == "header" and isinstance(scheme.get("name"), str):
        auth = {"kind": "api-key", "header": scheme["name"]}
    elif kind == "http" and scheme.get("scheme") in {"basic", "bearer"}:
        auth = {"kind": scheme["scheme"]}
    elif kind == "oauth2" and isinstance(scheme.get("flows"), dict) and set(scheme["flows"]) == {"clientCredentials"}:
        flow = scheme["flows"]["clientCredentials"]
        if isinstance(flow, dict) and isinstance(flow.get("tokenUrl"), str) and isinstance(scopes, list):
            auth = {
                "kind": "machine-token",
                "client_id": "replace-with-client-id",
                "endpoint": flow["tokenUrl"],
                "scopes": scopes,
            }
    if auth is None:
        return None
    try:
        AuthProfile.model_validate(auth)
    except ValidationError:
        return None
    return auth


def _statuses(operation: dict[str, Any]) -> list[int]:
    responses = operation.get("responses")
    codes = [int(key) for key in _mapping(responses) if re.fullmatch(r"2\d\d", key)]
    return sorted(codes)[:20]


def _action_name(identifier: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", identifier)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:64].strip("-")
    return text or "operation"


_OPERATION_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")


def _control(text: str) -> bool:
    """C0 and C1 control characters, including terminal escape introducers."""
    return any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in text)


def _text(value: Any, limit: int = 512) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value if not _control(c))
    return cleaned[:limit]


def _display_path(path: str) -> str:
    """A path safe to print: overlong, control-character or credential-bearing paths are masked."""
    return "*" if len(path) > 2048 or _control(path) or _credential_url(path) else path


class _Entry(NamedTuple):
    key: str
    identifier: str | None
    method: str
    path: str
    item: dict[str, Any]
    operation: dict[str, Any]


def _entries(value: dict[str, Any]) -> list[_Entry]:
    paths = _mapping(value.get("paths"))
    entries = []
    for path, item in paths.items():
        if not isinstance(path, str) or path.startswith("x-") or not isinstance(item, dict):
            continue
        for method in METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                identifier = operation.get("operationId")
                # Only selectable identifiers are kept; anything else could carry escapes or credentials.
                identifier = identifier if isinstance(identifier, str) and _OPERATION_ID.fullmatch(identifier) else None
                entries.append(
                    _Entry(
                        identifier or f"{method.upper()} {_display_path(path)}",
                        identifier,
                        method,
                        path,
                        item,
                        operation,
                    )
                )
                if len(entries) >= OPERATION_LIMIT:
                    return entries
    return entries


def _verdict(
    value: dict[str, Any],
    entry: _Entry,
    relax: ImportRelaxations,
    locations: dict[str, Any],
    cache: dict[str, tuple[Diagnostic, ...]] | None = None,
) -> tuple[bool, list[Diagnostic]]:
    where = _ptr(_ptr("/paths", entry.path), entry.method)

    def problem(code: str) -> tuple[bool, list[Diagnostic]]:
        return False, [_diagnostic(_Failure(code, where), None, locations)]

    if entry.identifier is None:
        return problem("OPERATION_ID")
    server = _effective_server(value, entry.item, entry.operation)
    if server is None:
        return problem("SERVER")
    statuses = _statuses(entry.operation)
    if not statuses:
        return problem("STATUSES")
    auth = _effective_auth(value, entry.operation)
    if auth is None:
        return problem("SECURITY")
    rules = OpenAPIImportPolicy.model_validate(
        {
            "name": "inventory-check",
            "version": "1.0.0",
            "auth": auth,
            "operations": {
                entry.identifier: {
                    "name": "operation",
                    "sideEffect": "read_only" if entry.method in {"get", "head"} else "non_idempotent",
                    "server": server,
                    "statuses": statuses,
                }
            },
            "relaxations": relax.model_dump(by_alias=True),
        }
    )
    engine: _Importer | None = None
    try:
        # Pruning stays inside the guard: its own budget failure is this operation's verdict.
        engine = _Importer(prune_document(value, {entry.identifier}), rules, collect=True, schema_cache=cache)
        engine.run([entry.identifier], target="builtin", compile_actions=False)
    except (_Failure, _Failures) as error:
        return False, _failures(error, engine, locations) + _engine_warnings(engine, locations)
    except (ValueError, TypeError, KeyError, RecursionError):
        return problem("UNSUPPORTED")
    return True, _engine_warnings(engine, locations)


def inventory(
    document: bytes | str | dict[str, Any],
    *,
    source_format: SourceFormat = "json",
    relaxations: ImportRelaxations | dict[str, Any] | None = None,
) -> OpenAPIInventory:
    """List every operation with a verdict: would it import alone with these relaxations, and why not.

    Document-level problems (version, credentials, broken references) are
    reported once in ``diagnostics`` and make every operation unsupported.
    """
    try:
        parsed, large = _parse(document, source_format)
        relax = _relaxations(relaxations)
    except ParseFailure as exc:
        return OpenAPIInventory(
            ok=False, diagnostics=[sanitize_import_diagnostic(d) for d in exc.diagnostics], truncated=exc.truncated
        )
    except ValidationError:
        return OpenAPIInventory(
            ok=False,
            diagnostics=[_diagnostic(_Failure("POLICY", ""), None, {})],
        )
    locations = parsed.locations
    value = parsed.value
    notes: list[Diagnostic] = []
    problems: list[Diagnostic] = []
    try:
        if relax.upgrade_openapi_30:
            value, upgraded = upgrade_openapi_30(value)
            if value is not parsed.value:
                notes.append(_warning("RELAXED_OPENAPI_30", "/openapi", locations, upgraded))
        _metadata_guard(value, "")
        if large:
            notes.append(_warning("PRUNED", "", locations))
        else:
            try:
                _Importer(value, _INVENTORY_POLICY.model_copy(update={"relaxations": relax})).inventory()
            except _Failure as failure:
                if failure.code != "RESOURCE_LIMIT":
                    raise
                # Imports prune such documents to their selection, so each verdict below does the same.
                notes.append(_warning("PRUNED", "", locations))
    except (_Failure, _Failures) as error:
        problems = _failures(error, None, locations)
    except (ValueError, TypeError, KeyError, RecursionError):
        problems = [_diagnostic(_Failure("UNSUPPORTED", ""), None, locations)]
    entries = _entries(value)
    operations = []
    cache: dict[str, tuple[Diagnostic, ...]] = {}
    for entry in entries:
        if problems:
            supported, reasons = False, list(problems)
        else:
            supported, reasons = _verdict(value, entry, relax, locations, cache)
        tags = entry.operation.get("tags")
        operations.append(
            InventoryOperation(
                key=entry.key,
                operationId=entry.identifier,
                method=entry.method.upper(),
                path=_display_path(entry.path),
                summary=_text(entry.operation.get("summary")),
                tags=[t for t in (_text(tag, 128) for tag in _items(tags)[:20]) if t],
                supported=supported,
                sideEffect="read_only" if entry.method in {"get", "head"} else "non_idempotent",
                statuses=_statuses(entry.operation),
                reasons=reasons,
            )
        )
    info = _mapping(value.get("info"))
    servers = _items(value.get("servers"))
    return OpenAPIInventory(
        ok=not problems,
        openapi=_text(parsed.value.get("openapi"), 32),
        title=_text(info.get("title"), 256),
        servers=[
            url
            for url in (s.get("url") for s in servers[:20] if isinstance(s, dict))
            if isinstance(url, str) and len(url) <= 2048 and not _control(url) and not _credential_url(url)
        ],
        operations=operations,
        diagnostics=notes + problems,
        truncated=len(entries) >= OPERATION_LIMIT,
    )


_INVENTORY_POLICY = OpenAPIImportPolicy.model_validate(
    {
        "name": "inventory-check",
        "version": "1.0.0",
        "auth": {"kind": "none"},
        "operations": {
            "any": {"name": "any", "sideEffect": "read_only", "server": "https://example.invalid", "statuses": [200]}
        },
    }
)


def _policy_name(value: dict[str, Any], requested: str | None) -> str:
    if requested is not None:
        return requested
    info = _mapping(value.get("info"))
    name = _action_name(_text(info.get("title"), 128) or "")
    name = re.sub(r"^[^a-z]+", "", name)
    if not name or name.startswith("weave-") or keyword.iskeyword(name.replace("-", "_")):
        return "imported-api"
    return name


def init_policy(
    document: bytes | str | dict[str, Any],
    operation_ids: list[str] | None = None,
    *,
    source_format: SourceFormat = "json",
    relaxations: ImportRelaxations | dict[str, Any] | None = None,
    name: str | None = None,
    version: str = "1.0.0",
) -> PolicyScaffold:
    """Scaffold a policy for review: servers, auth, effects and statuses come from the document.

    Without ``operation_ids`` every supported operation sharing the most common
    server and authentication is selected. The scaffold is a starting point; the
    operator reviews names, statuses and relaxations before importing.
    """
    listing = inventory(document, source_format=source_format, relaxations=relaxations)
    if not listing.ok:
        return PolicyScaffold(ok=False, diagnostics=listing.diagnostics)
    try:
        parsed, _ = _parse(document, source_format)
        relax = _relaxations(relaxations)
    except (ParseFailure, ValidationError):
        return PolicyScaffold(ok=False, diagnostics=[_diagnostic(_Failure("POLICY", ""), None, {})])
    value = parsed.value
    if relax.upgrade_openapi_30:
        value, _ = upgrade_openapi_30(value)
    by_id = {entry.identifier: entry for entry in _entries(value) if entry.identifier is not None}
    verdicts = {op.operation_id: op for op in listing.operations if op.operation_id is not None}
    notes: list[Diagnostic] = []
    if operation_ids is None:
        chosen = [identifier for identifier, op in verdicts.items() if op.supported][:100]
    else:
        unknown = [identifier for identifier in operation_ids if identifier not in by_id]
        if unknown or not operation_ids or len(set(operation_ids)) != len(operation_ids) or len(operation_ids) > 100:
            return PolicyScaffold(
                ok=False,
                diagnostics=[
                    Diagnostic(
                        code="WV-IMPORT-POLICY",
                        severity="error",
                        stage="semantic",
                        message="Select up to 100 distinct operations by operationId.",
                        path="/paths",
                        hint="List the operations with --list to see their operationIds.",
                    )
                ],
            )
        chosen = list(operation_ids)
        for identifier in chosen:
            notes.extend(
                reason.model_copy(update={"severity": "warning"})
                for reason in verdicts[identifier].reasons
                if reason.severity == "error"
            )
    if not chosen:
        return PolicyScaffold(
            ok=False,
            diagnostics=[
                Diagnostic(
                    code="WV-IMPORT-POLICY",
                    severity="error",
                    stage="semantic",
                    message="No operation can be imported as is.",
                    path="/paths",
                    hint="List the operations with --list to see the reasons and the relaxations that apply.",
                )
            ],
        )
    groups: dict[str, list[str]] = {}
    plans: dict[str, tuple[str, dict[str, Any]]] = {}
    for identifier in chosen:
        entry = by_id[identifier]
        server = _effective_server(value, entry.item, entry.operation)
        auth = _effective_auth(value, entry.operation)
        if server is None or auth is None:
            continue
        plans[identifier] = server, auth
        group = fixed_server(server)[0] + " " + canonical_bytes(auth).decode()
        groups.setdefault(group, []).append(identifier)
    if not groups:
        return PolicyScaffold(ok=False, diagnostics=notes or [_diagnostic(_Failure("SECURITY", "/paths"), None, {})])
    if len(groups) > 1 and operation_ids is not None:
        return PolicyScaffold(
            ok=False,
            diagnostics=[
                Diagnostic(
                    code="WV-IMPORT-POLICY",
                    severity="error",
                    stage="semantic",
                    message="The selected operations use different servers or authentication.",
                    path="/paths",
                    hint="Import each server and authentication combination with its own policy.",
                )
            ],
        )
    selected = max(groups.values(), key=len)
    if len(selected) < len(chosen):
        excluded = [_ptr(_ptr("/paths", by_id[i].path), by_id[i].method) for i in chosen if i not in selected]
        notes.append(
            Diagnostic(
                code="WV-IMPORT-POLICY_SUBSET",
                severity="warning",
                stage="semantic",
                message="Operations with another server or authentication were left out of this policy.",
                path="/paths",
                hint="Scaffold a separate policy for them with --operation.",
                related=[RelatedLocation(path=p) for p in excluded[:20]],
            )
        )
    operations: dict[str, Any] = {}
    used: set[str] = set()
    for identifier in selected:
        entry = by_id[identifier]
        base = _action_name(identifier)
        action_name, suffix = base, 2
        while action_name in used:
            action_name, suffix = f"{base[:60]}-{suffix}", suffix + 1
        used.add(action_name)
        statuses = _statuses(entry.operation) or [200]
        operations[identifier] = {
            "name": action_name,
            "sideEffect": "read_only" if entry.method in {"get", "head"} else "non_idempotent",
            "server": plans[identifier][0],
            "statuses": statuses,
        }
    policy: dict[str, Any] = {
        "name": _policy_name(value, name),
        "version": version,
        "auth": plans[selected[0]][1],
        "operations": operations,
    }
    explicit = relax.model_dump(by_alias=True, exclude_defaults=True)
    if explicit:
        policy["relaxations"] = explicit
    try:
        OpenAPIImportPolicy.model_validate(policy)
    except ValidationError:
        return PolicyScaffold(ok=False, diagnostics=[_diagnostic(_Failure("POLICY", ""), None, {})])
    return PolicyScaffold(ok=True, policy=policy, diagnostics=[sanitize_import_diagnostic(d) for d in notes])
