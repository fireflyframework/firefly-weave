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
"""No-code HTTP Actions on the built-in ``weave-http@2.0.0`` connector.

Everything here is pure data shaping: no network, no file access and no code
selection. An author describes one HTTP or HTTPS request (method, path template, typed
parameters, a JSON body and response shape); the builder emits an Action whose
``implementation.config`` is an ``HttpOperation`` executed by the fixed,
first-party HTTP profile executor. The side effect follows the method and
cannot be chosen: GET and HEAD are ``read_only``; every other method is
``non_idempotent`` with exactly one attempt. Connections carry secret handle
names only, never secret values.
"""

import copy
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from firefly_weave.compiler.api import CompileResult, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.schemas import validate_payload, validate_schema
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.definitions import (
    NAME_PATTERN,
    ActionDefinition,
    ConnectorDefinition,
    ContractModel,
    ResourceName,
    SemVer,
)
from firefly_weave.contracts.diagnostics import Diagnostic
from firefly_weave.contracts.http_profiles import (
    HTTP_PROFILE_DESCRIPTOR,
    PROFILE_VERSION,
    AuthProfile,
    HttpOperation,
    HttpParameter,
    ProfileConnection,
    fixed_server,
    object_schema,
    protected,
    template_names,
    validate_profile_connection,
)
from firefly_weave.contracts.values import JsonData, JsonObject, JsonObjectData, JsonValue

HTTP_CONNECTOR = f"weave-http@{PROFILE_VERSION}"
HTTP_ADAPTER = "weave-http-v2"
DEFAULT_STRING_MAX_LENGTH = 256
DEFAULT_ARRAY_MAX_ITEMS = 20
INFERENCE_MAX_DEPTH = 12
INFERENCE_MAX_NODES = 2000
INFERENCE_MAX_PROPERTIES = 128
INFERENCE_MAX_ITEMS = 100
READ_METHODS = frozenset({"GET", "HEAD"})
_GROUPS = {"path": "path", "query": "query", "header": "headers"}
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$.-]{0,63}")
_UUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_RESOURCE_NAME = re.compile(rf"{NAME_PATTERN}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?|[0-9A-Fa-f:.]{2,45}")

type Method = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


class HttpActionError(ValueError):
    """The request cannot become a valid Action; every problem is a value-free diagnostic."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = diagnostics
        super().__init__("HTTP action request is invalid")


def _issue(code: str, path: str, message: str, hint: str | None = None, severity: str = "error") -> Diagnostic:
    return Diagnostic(
        code="WV-HTTP-ACTION-" + code,
        severity=cast(Any, severity),
        stage="semantic",
        message=message,
        path=path,
        hint=hint,
    )


def _ptr(path: str, key: str | int) -> str:
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


class ActionParameter(ContractModel):
    """One request parameter; values arrive at run time through the Action input."""

    name: str = Field(min_length=1, max_length=128)
    location: Literal["path", "query", "header"]
    type: Literal["string", "integer", "boolean"] = "string"
    array: bool = False
    required: bool = False
    max_length: int | None = Field(default=None, alias="maxLength", ge=1, le=4096)
    max_items: int | None = Field(default=None, alias="maxItems", ge=1, le=100)
    description: str | None = Field(default=None, max_length=1024)


class HttpActionRequest(ContractModel):
    """What an author describes; the side effect, retries and connector are derived, never chosen."""

    name: ResourceName
    version: SemVer = "1.0.0"
    description: str | None = Field(default=None, max_length=1024)
    method: Method
    path_template: str = Field(alias="pathTemplate", min_length=1, max_length=4096)
    parameters: list[ActionParameter] = Field(default_factory=list, max_length=64)
    body_schema: JsonObjectData | None = Field(default=None, alias="bodySchema")
    body_sample: JsonData | None = Field(default=None, alias="bodySample")
    body_required: bool = Field(default=True, alias="bodyRequired")
    response_schema: JsonObjectData | None = Field(default=None, alias="responseSchema")
    response_sample: JsonData | None = Field(default=None, alias="responseSample")
    statuses: list[int] = Field(default_factory=lambda: [200], min_length=1, max_length=20)
    empty_statuses: list[int] = Field(default_factory=list, alias="emptyStatuses", max_length=20)
    timeout_seconds: int = Field(default=30, alias="timeoutSeconds", ge=1, le=30)
    string_max_length: int = Field(default=DEFAULT_STRING_MAX_LENGTH, alias="stringMaxLength", ge=1, le=4096)
    auth: AuthProfile | None = None

    @model_validator(mode="after")
    def one_source_each(self) -> "HttpActionRequest":
        if self.body_schema is not None and self.body_sample is not None:
            raise ValueError("Use either bodySchema or bodySample")
        if self.response_schema is not None and self.response_sample is not None:
            raise ValueError("Use either responseSchema or responseSample")
        return self


class HttpActionResult(ContractModel):
    """Builder outcome shared by the CLI and Studio; ``compiled`` means it compiled against the built-in catalog."""

    ok: bool
    action: JsonObjectData | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    compiled: bool = False


def infer_schema(
    sample: JsonValue,
    *,
    max_depth: int = INFERENCE_MAX_DEPTH,
    max_nodes: int = INFERENCE_MAX_NODES,
    max_properties: int = INFERENCE_MAX_PROPERTIES,
) -> tuple[JsonObject, bool]:
    """Derive a structural schema from a JSON sample; returns ``(schema, truncated)``.

    Only types and identifier-like property names are kept: values, enums,
    lengths and examples are never copied, so a pasted response cannot leak
    into a published definition. Objects whose keys look like data (UUIDs,
    e-mail addresses, numbers, long or free-form text) become maps described by
    ``additionalProperties``. Array elements and map values are merged into
    the narrowest shape accepting every examined one: ``required`` keeps the
    keys present in all of them, differing scalar types become a type list and
    a value seen both as ``null`` and as a structure becomes nullable.
    ``additionalProperties`` is otherwise left open, so the schema accepts the
    sample it came from. Beyond the depth or node budget a node becomes ``{}``
    (any JSON value) and later elements are not examined; ``truncated`` says so.
    """
    nodes = 0
    truncated = False

    def data_keys(keys: list[str]) -> bool:
        return len(keys) > max_properties or any(
            not _IDENTIFIER.fullmatch(key) or _UUID.fullmatch(key) or key.replace("-", "").isdigit() for key in keys
        )

    def merged(values: list[JsonValue], depth: int) -> JsonObject:
        nonlocal truncated
        result: JsonObject | None = None
        for index, value in enumerate(values):
            if index >= INFERENCE_MAX_ITEMS or index and nodes >= max_nodes:
                # Unexamined elements could differ from the merged shape; the caller warns about it.
                truncated = True
                break
            shape = infer(value, depth)
            result = shape if result is None else _merge(result, shape)
        return result if result is not None else {}

    def infer(value: JsonValue, depth: int) -> JsonObject:
        nonlocal nodes, truncated
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            truncated = True
            return {}
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "boolean"}
        if isinstance(value, int):
            return {"type": "integer"}
        if isinstance(value, float):
            return {"type": "number"}
        if isinstance(value, str):
            return {"type": "string"}
        if isinstance(value, list):
            if not value:
                return {"type": "array"}
            return {"type": "array", "items": merged(value, depth + 1)}
        keys = list(value)
        if not keys:
            return {"type": "object"}
        if data_keys(keys):
            return {"type": "object", "additionalProperties": merged([value[k] for k in keys], depth + 1)}
        properties: dict[str, JsonValue] = {}
        for key in keys:
            properties[key] = infer(value[key], depth + 1)
        return {"type": "object", "properties": properties, "required": cast(JsonValue, sorted(keys))}

    schema = infer(sample, 0)
    return schema, truncated


_SCALARS = frozenset({"string", "integer", "number", "boolean", "null"})


def _scalar_types(schema: JsonObject) -> set[str] | None:
    """The type set of a schema that is only ``{"type": ...}`` over scalar types, else ``None``."""
    if set(schema) != {"type"}:
        return None
    kind = schema["type"]
    types = {kind} if isinstance(kind, str) else set(cast(list[str], kind)) if isinstance(kind, list) else set()
    return types if types and types <= _SCALARS else None


def _typed(types: set[str]) -> JsonObject:
    if {"integer", "number"} <= types:
        types = types - {"integer"}
    ordered = sorted(types)
    return {"type": ordered[0] if len(ordered) == 1 else cast(JsonValue, ordered)}


def _split_null(schema: JsonObject) -> tuple[bool, JsonObject | None]:
    """``(nullable, the non-null part or None)`` for shapes produced by inference."""
    if schema == {"type": "null"}:
        return True, None
    scalars = _scalar_types(schema)
    if scalars is not None and "null" in scalars:
        return True, _typed(scalars - {"null"})
    options = schema.get("anyOf")
    if set(schema) == {"anyOf"} and isinstance(options, list) and len(options) == 2 and {"type": "null"} in options:
        return True, cast(JsonObject, next(o for o in options if o != {"type": "null"}))
    return False, schema


def _merge(left: JsonObject, right: JsonObject) -> JsonObject:
    """The narrowest inferred shape accepting both; ``{}`` (any JSON value) when they cannot be combined."""
    if left == right:
        return left
    if not left or not right:
        return {}
    left_null, left_core = _split_null(left)
    right_null, right_core = _split_null(right)
    if left_null or right_null:
        core = right_core if left_core is None else left_core if right_core is None else _merge(left_core, right_core)
        if core is None:
            return {"type": "null"}
        if not core:
            return {}
        scalars = _scalar_types(core)
        if scalars is not None:
            return _typed(scalars | {"null"})
        return {"anyOf": [cast(JsonValue, core), {"type": "null"}]}
    left_scalars, right_scalars = _scalar_types(left), _scalar_types(right)
    if left_scalars is not None and right_scalars is not None:
        return _typed(left_scalars | right_scalars)
    kinds = left.get("type"), right.get("type")
    if kinds == ("array", "array"):
        if "items" not in left or "items" not in right:
            # An empty array accepts any items, so the other side's items describe both.
            return left if "items" in left else right
        return {"type": "array", "items": _merge(cast(JsonObject, left["items"]), cast(JsonObject, right["items"]))}
    if kinds == ("object", "object"):
        if left == {"type": "object"} or right == {"type": "object"}:
            # An empty object has none of the other side's keys: keep the shape, require nothing.
            other = dict(right if left == {"type": "object"} else left)
            other.pop("required", None)
            return other
        left_properties, right_properties = left.get("properties"), right.get("properties")
        if isinstance(left_properties, dict) and isinstance(right_properties, dict):
            properties = dict(left_properties)
            for key, value in right_properties.items():
                properties[key] = (
                    _merge(cast(JsonObject, properties[key]), cast(JsonObject, value)) if key in properties else value
                )
            required = sorted(
                set(cast(list[str], left.get("required", []))) & set(cast(list[str], right.get("required", [])))
            )
            result: JsonObject = {"type": "object", "properties": properties}
            if required:
                result["required"] = cast(JsonValue, required)
            return result
        if "additionalProperties" in left and "additionalProperties" in right:
            return {
                "type": "object",
                "additionalProperties": _merge(
                    cast(JsonObject, left["additionalProperties"]), cast(JsonObject, right["additionalProperties"])
                ),
            }
        return {"type": "object"}
    return {}


def _schema_issues(schema: JsonObject, path: str) -> list[Diagnostic]:
    return [
        issue.model_copy(update={"path": path + issue.path, "related": []}) for issue in validate_schema(schema, {})
    ]


def _parameter_schema(parameter: ActionParameter, default_length: int) -> JsonObject:
    scalar: JsonObject = {"type": parameter.type}
    if parameter.type == "string":
        if parameter.location == "path":
            scalar["minLength"] = 1
        scalar["maxLength"] = parameter.max_length or default_length
    if parameter.array:
        result: JsonObject = {
            "type": "array",
            "items": scalar,
            "maxItems": parameter.max_items or DEFAULT_ARRAY_MAX_ITEMS,
        }
    else:
        result = scalar
    if parameter.description:
        result["description"] = parameter.description
    return result


def action_document(
    name: str,
    version: str,
    operation: HttpOperation,
    input_schema: JsonObject,
    output_schema: JsonObject,
    timeout_seconds: int,
) -> JsonObject:
    """Assemble an Action on ``weave-http@2.0.0``; the action and retries follow the operation's method."""
    write = operation.method not in READ_METHODS
    spec: JsonObject = {
        "implementation": {
            "kind": "connector",
            "uses": HTTP_CONNECTOR,
            "action": "write" if write else "read",
            "config": cast(JsonValue, operation.model_dump(by_alias=True)),
        },
        "sideEffect": "non_idempotent" if write else "read_only",
        "timeoutSeconds": timeout_seconds,
        "inputSchema": cast(JsonValue, input_schema),
        "outputSchema": cast(JsonValue, output_schema),
        "connection": {"connector": HTTP_CONNECTOR},
    }
    if write:
        # The outcome of a sent write is unknown; a hidden retry could repeat it.
        spec["retry"] = {"maxAttempts": 1}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": name, "version": version},
        "spec": spec,
    }


def output_schema(statuses: Iterable[int], empty_statuses: Iterable[int], body: JsonObject) -> JsonObject:
    """``{status, body}`` per declared status; empty statuses carry a ``null`` body."""
    empty = set(empty_statuses)
    branches: list[JsonValue] = [
        object_schema(
            {"status": {"const": status}, "body": {"type": "null"} if status in empty else body}, ["status", "body"]
        )
        for status in statuses
    ]
    return cast(JsonObject, branches[0]) if len(branches) == 1 else {"oneOf": branches}


def build_http_action(request: HttpActionRequest | Mapping[str, Any]) -> JsonObject:
    """Build one Action document, or raise ``HttpActionError`` with every problem found."""
    return _build(request)[0]


def _build(request: HttpActionRequest | Mapping[str, Any]) -> tuple[JsonObject, list[Diagnostic]]:
    req = _request(request)
    errors: list[Diagnostic] = []
    notes: list[Diagnostic] = []
    method = req.method
    try:
        names = template_names(req.path_template)
    except ValueError:
        names = []
        errors.append(
            _issue(
                "PATH",
                "/pathTemplate",
                "The path template is not a valid absolute path.",
                "Use a path such as /v1/pets/{petId}: no query, fragment, percent-encoding or dot segments.",
            )
        )
    parameters = list(req.parameters)
    declared_paths = {p.name for p in parameters if p.location == "path"}
    for name in names:
        if name not in declared_paths:
            parameters.append(ActionParameter(name=name, location="path", type="string", required=True))
            notes.append(
                _issue(
                    "PATH_PARAMETER_ADDED",
                    "/pathTemplate",
                    "A path placeholder without a declared parameter was added as a required string.",
                    "Declare the path parameter explicitly to choose its type or length.",
                    severity="info",
                )
            )
    auth_header = (req.auth.header or "").lower() if req.auth is not None else ""
    profile: list[HttpParameter] = []
    groups: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for index, parameter in enumerate(parameters):
        where = _ptr("/parameters", index) if index < len(req.parameters) else "/pathTemplate"
        key = parameter.location, parameter.name.lower() if parameter.location == "header" else parameter.name
        if key in seen:
            errors.append(_issue("PARAMETER", where, "The parameter is declared twice.", "Remove the duplicate."))
            continue
        seen.add(key)
        if parameter.location == "header" and (protected(parameter.name) or parameter.name.lower() == auth_header):
            errors.append(
                _issue(
                    "PROTECTED_HEADER",
                    where,
                    "This header is reserved for the platform or the connection's authentication.",
                    "Authentication comes from the connection; transport headers are set by the executor.",
                )
            )
            continue
        if parameter.location == "path" and parameter.name not in names:
            errors.append(
                _issue(
                    "PARAMETER",
                    where,
                    "A path parameter has no matching {placeholder} in the path template.",
                    "Add the placeholder to the path template or change the parameter location.",
                )
            )
            continue
        if parameter.array and parameter.location != "query":
            errors.append(_issue("PARAMETER", where, "Only query parameters can be arrays.", "Use a scalar parameter."))
            continue
        try:
            profile.append(
                HttpParameter(
                    name=parameter.name,
                    location=parameter.location,
                    type=parameter.type,
                    array=parameter.array,
                    required=parameter.location == "path" or parameter.required,
                )
            )
        except ValidationError:
            errors.append(
                _issue(
                    "PARAMETER",
                    where,
                    "The parameter name is not allowed in this location.",
                    "Use letters, digits, underscores or hyphens; headers use RFC 9110 token characters.",
                )
            )
            continue
        group = groups.setdefault(_GROUPS[parameter.location], object_schema({}))
        group["properties"][parameter.name] = _parameter_schema(parameter, req.string_max_length)
        if profile[-1].required:
            group.setdefault("required", []).append(parameter.name)
    if method in READ_METHODS and (req.body_schema is not None or req.body_sample is not None):
        errors.append(
            _issue(
                "BODY", "/bodySample" if req.body_sample is not None else "/bodySchema", "GET and HEAD send no body."
            )
        )
    elif req.body_schema is not None:
        errors.extend(_schema_issues(req.body_schema, "/bodySchema"))
        groups["body"] = dict(req.body_schema)
    elif req.body_sample is not None:
        body, truncated = infer_schema(req.body_sample)
        groups["body"] = body
        if truncated:
            notes.append(_truncated("/bodySample"))
    if req.response_schema is not None:
        errors.extend(_schema_issues(req.response_schema, "/responseSchema"))
        body_schema: JsonObject = dict(req.response_schema)
    elif req.response_sample is not None:
        body_schema, truncated = infer_schema(req.response_sample)
        if truncated:
            notes.append(_truncated("/responseSample"))
    else:
        body_schema = {}
        if method not in READ_METHODS or method == "GET":
            notes.append(
                _issue(
                    "UNTYPED_RESPONSE",
                    "/responseSample",
                    "The response body is untyped; workflows can read any JSON value from it.",
                    "Paste a response sample or a response schema to type the output.",
                    severity="info",
                )
            )
    statuses = list(dict.fromkeys(req.statuses))
    for index, status in enumerate(req.empty_statuses):
        if status not in statuses:
            # Never drop it silently: the Action would reject the response it was meant to accept.
            errors.append(
                _issue(
                    "STATUS",
                    _ptr("/emptyStatuses", index),
                    "An empty status is not one of the declared statuses.",
                    "Add it to statuses as well, for example statuses [200, 204] with emptyStatuses [204].",
                )
            )
    empty = set(req.empty_statuses) | {s for s in statuses if s in {204, 205}}
    if method == "HEAD":
        empty |= set(statuses)
    empty_statuses = [s for s in statuses if s in empty]
    if errors:
        raise HttpActionError(errors)
    try:
        operation = HttpOperation(
            method=method,
            path=req.path_template,
            sideEffect="read_only" if method in READ_METHODS else "non_idempotent",
            parameters=profile,
            statuses=statuses,
            emptyStatuses=empty_statuses,
        )
    except ValidationError:
        raise HttpActionError(
            [
                _issue(
                    "CONFIG",
                    "/statuses",
                    "The request does not fit the HTTP profile.",
                    "Use distinct 2xx statuses; empty statuses must be among them.",
                )
            ]
        ) from None
    required = [g for g in ("path", "query", "headers") if groups.get(g, {}).get("required")]
    if "body" in groups and req.body_required:
        required.append("body")
    inputs = object_schema(groups, required)
    if req.description:
        inputs["description"] = req.description
    outputs = output_schema(statuses, empty_statuses, body_schema)
    document = action_document(req.name, req.version, operation, inputs, outputs, req.timeout_seconds)
    return document, notes


def _truncated(path: str) -> Diagnostic:
    return _issue(
        "SAMPLE_TRUNCATED",
        path,
        "The sample exceeds the inference budget: deep parts are typed as any JSON value and only the first "
        "array elements or map values were examined.",
        "Provide a schema for the deep or wide parts if they matter to the workflow.",
        severity="warning",
    )


def _request(request: HttpActionRequest | Mapping[str, Any]) -> HttpActionRequest:
    if isinstance(request, HttpActionRequest):
        return request
    try:
        return HttpActionRequest.model_validate(dict(request))
    except ValidationError as error:
        raise HttpActionError([_request_issue(error)]) from None


def _request_issue(error: ValidationError) -> Diagnostic:
    # Only field locations are reported; pydantic messages may echo the rejected input.
    location = error.errors(include_input=False, include_url=False)[0].get("loc", ())
    path = "".join(_ptr("", part) for part in location if isinstance(part, (str, int)))
    return _issue(
        "REQUEST",
        path,
        "The HTTP action request has a missing, unknown or out-of-range field.",
        "The side effect, retries and connector follow the method and cannot be set.",
    )


def _server_hook() -> Callable[[Any], Iterable[Any]] | None:
    """The installed descriptor's compile-time Action checks, when this build provides them."""
    hook = getattr(HTTP_PROFILE_DESCRIPTOR, "validate_action_config", None)
    return hook if callable(hook) else None


_SERVER_CODES = frozenset({"CONFIG_CONTRACT", "SIDE_EFFECT_CONTRACT", "INPUT_CONTRACT", "OUTPUT_CONTRACT"})
_POINTER = re.compile(r"(?:/(?:[^~/]|~[01])*)*")


def _server_issues(hook: Callable[[Any], Iterable[Any]], action: str, document: Mapping[str, Any]) -> list[Diagnostic]:
    """Run the descriptor's own checks and report them exactly as server-side compilation would."""
    from firefly_weave.compiler.action_config import ActionConfigCheck

    spec = copy.deepcopy(dict(document)["spec"])
    config_path = "/spec/implementation/config"
    try:
        found = list(
            hook(
                ActionConfigCheck(
                    action=action,
                    config=copy.deepcopy(spec["implementation"].get("config", {})),
                    spec=spec,
                    schemas={},
                )
            )
        )
    except Exception:
        # Trusted descriptor code fails closed with a stable diagnostic, as in the compiler.
        found = [None]
    issues = []
    for item in found[:100]:
        path, message = getattr(item, "path", None), getattr(item, "message", None)
        code, severity = getattr(item, "code", None), getattr(item, "severity", "error")
        issues.append(
            Diagnostic(
                code="WV-COMP-" + (code if code in _SERVER_CODES else "CONFIG_CONTRACT"),
                severity="warning" if severity == "warning" else "error",
                stage="semantic",
                message=message[:1000]
                if isinstance(message, str) and message.strip()
                else "Definition violates the connector configuration contract.",
                path=path if isinstance(path, str) and _POINTER.fullmatch(path) else config_path,
            )
        )
    return issues


def check_http_action(document: Mapping[str, Any]) -> list[Diagnostic]:
    """Rules for a ``weave-http@2.0.0`` Action beyond what generic compilation enforces.

    The connector, connection and retry rules always run here. When the
    installed descriptor provides compile-time checks (the rules the server runs
    at publish time), their findings are reported as ``WV-COMP-*``. A local mirror
    of the executor's rules also runs: config is an ``HttpOperation``; action,
    method and effect agree; input groups match the parameters; empty statuses
    accept a ``null`` body. A local finding is dropped when the installed checks
    already reported its category.
    """
    issues: list[Diagnostic] = []
    try:
        action = ActionDefinition.model_validate(dict(document))
    except ValidationError:
        return [_issue("DOCUMENT", "", "The document is not a valid Action definition.")]
    spec = action.spec
    implementation = spec.implementation
    if implementation.kind != "connector" or implementation.uses != HTTP_CONNECTOR:
        return [
            _issue(
                "CONNECTOR",
                "/spec/implementation",
                "The Action does not use the built-in weave-http@2.0.0 connector.",
            )
        ]
    if spec.connection is None or spec.connection.connector != HTTP_CONNECTOR:
        issues.append(
            _issue(
                "CONNECTION",
                "/spec/connection",
                "The Action must require a weave-http@2.0.0 connection.",
                'Set connection to {"connector": "weave-http@2.0.0"}.',
            )
        )
    if implementation.action not in {"read", "write"}:
        issues.append(_issue("ACTION", "/spec/implementation/action", "The action must be read or write."))
        return issues
    if implementation.action == "write" and spec.retry.max_attempts != 1:
        issues.append(
            _issue(
                "RETRY",
                "/spec/retry/maxAttempts",
                "A write must not be retried: a sent request may already have taken effect.",
                "Set retry.maxAttempts to 1.",
            )
        )
    hook = _server_hook()
    installed = _server_issues(hook, implementation.action, document) if hook is not None else []
    # Installed findings win per category; the local mirror only adds what they did not cover.
    covered = {d.code.removeprefix("WV-COMP-").removesuffix("_CONTRACT") for d in installed}
    local = _local_issues(spec, implementation.action, implementation.config)
    return issues + installed + [d for d in local if d.code.removeprefix("WV-HTTP-ACTION-") not in covered]


def _local_issues(spec: Any, action: str, config: Any) -> list[Diagnostic]:
    issues: list[Diagnostic] = []
    try:
        operation = HttpOperation.model_validate(config or {})
    except ValidationError:
        return [
            _issue(
                "CONFIG",
                "/spec/implementation/config",
                "The config is not a valid HTTP profile 2.0.0 operation.",
                "Regenerate the Action with weave connector http-action or the OpenAPI importer.",
            )
        ]
    expected = "read_only" if action == "read" else "non_idempotent"
    methods_ok = (operation.method in READ_METHODS) == (action == "read")
    if not methods_ok or operation.side_effect != expected or spec.side_effect != expected:
        issues.append(
            _issue(
                "SIDE_EFFECT",
                "/spec/sideEffect",
                "The action, method and side effect disagree.",
                "GET and HEAD use action read (read_only); other methods use action write (non_idempotent).",
            )
        )
    issues.extend(_input_issues(spec.input_schema, operation))
    for status in operation.empty_statuses:
        if validate_payload(spec.output_schema, {"status": status, "body": None}, {}):
            issues.append(
                _issue(
                    "OUTPUT",
                    "/spec/outputSchema",
                    "The output schema rejects the null body of an empty status.",
                    "Accept {status, body: null} for every empty status.",
                )
            )
            break
    return issues


def _input_issues(schema: JsonObject, operation: HttpOperation) -> list[Diagnostic]:
    base = "/spec/inputSchema"
    properties = schema.get("properties")
    if schema.get("type") != "object" or not isinstance(properties, dict):
        return [
            _issue(
                "INPUT_UNCHECKED",
                base,
                "The input schema is not a plain object; parameter groups were not checked.",
                severity="warning",
            )
        ]
    issues: list[Diagnostic] = []
    allowed = {"path", "query", "headers"} | ({"body"} if operation.method not in READ_METHODS else set())
    if set(properties) - allowed:
        issues.append(
            _issue(
                "INPUT",
                base + "/properties",
                "The input has groups the request cannot send.",
                "Use only path, query, headers and (for writes) body.",
            )
        )
    root_required = schema.get("required", [])
    root_required = root_required if isinstance(root_required, list) else []
    for location, group_name in _GROUPS.items():
        declared = [p for p in operation.parameters if p.location == location]
        group = properties.get(group_name)
        where = _ptr(base + "/properties", group_name)
        if not declared:
            if group is not None:
                issues.append(_issue("INPUT", where, "The input declares a group without parameters."))
            continue
        if not isinstance(group, dict) or not isinstance(group.get("properties"), dict):
            issues.append(_issue("INPUT", where, "The input does not describe the declared parameters."))
            continue
        names = {p.name for p in declared}
        if set(cast(dict[str, Any], group["properties"])) != names:
            issues.append(
                _issue(
                    "INPUT",
                    where + "/properties",
                    "The input properties differ from the declared parameters.",
                    "List exactly the declared parameters.",
                )
            )
        listed = group.get("required", [])
        required = {name for name in listed if isinstance(name, str)} if isinstance(listed, list) else set()
        needed = {p.name for p in declared if p.required}
        if required != needed or (needed and group_name not in root_required):
            issues.append(
                _issue(
                    "INPUT",
                    where + "/required",
                    "Required input does not match the required parameters.",
                    "Path parameters are always required.",
                )
            )
    return issues


def http_catalog() -> CatalogSnapshot:
    """A catalog holding only the bundled ``weave-http@2.0.0`` descriptor (offline validation)."""
    descriptor = HTTP_PROFILE_DESCRIPTOR
    return CatalogSnapshot.from_definitions(
        [ConnectorDefinition.model_validate(descriptor.manifest.value)],
        tasks=descriptor.capabilities,
        adapters=[HTTP_ADAPTER],
    )


def compile_http_action(document: Mapping[str, Any]) -> CompileResult:
    return compile_source(dict(document), format="object", catalog=http_catalog())


def validate_http_action(document: Mapping[str, Any]) -> HttpActionResult:
    """Check and compile an existing document against the bundled built-in catalog."""
    issues = check_http_action(document)
    compiled = compile_http_action(document)
    issues.extend(compiled.diagnostics)
    ok = compiled.ok and not any(d.severity == "error" for d in issues)
    return HttpActionResult(ok=ok, action=dict(document) if ok else None, diagnostics=issues, compiled=compiled.ok)


def author_http_action(request: HttpActionRequest | Mapping[str, Any]) -> HttpActionResult:
    """Build, check and compile; never raises for author input."""
    try:
        document, notes = _build(request)
    except HttpActionError as error:
        return HttpActionResult(ok=False, diagnostics=error.diagnostics)
    checked = validate_http_action(document)
    return checked.model_copy(update={"diagnostics": notes + checked.diagnostics})


def _connection_issue(code: str, path: str, message: str, hint: str | None = None) -> Diagnostic:
    return Diagnostic(
        code="WV-HTTP-CONNECTION-" + code, severity="error", stage="semantic", message=message, path=path, hint=hint
    )


def _origin(value: str, path: str) -> str:
    try:
        origin, base = fixed_server(value, plain_http=True)
        if not _HOST.fullmatch(urlsplit(value).hostname or ""):
            raise ValueError("Destination hosts are literal names or addresses")
    except ValueError:
        raise HttpActionError(
            [
                _connection_issue(
                    "DESTINATION",
                    path,
                    "Destinations must be literal HTTPS or HTTP origins.",
                    "Use https://host[:port], or http:// (not encrypted), with no wildcard, path, query or user "
                    "information.",
                )
            ]
        ) from None
    if base or value.rstrip("/") != origin:
        raise HttpActionError(
            [_connection_issue("DESTINATION", path, "Destinations are origins only; put base paths in the Action.")]
        )
    return origin


def build_connection_request(
    name: str,
    base_url: str,
    auth: AuthProfile | Mapping[str, Any],
    secret_handles: Mapping[str, str],
    connector_version_id: UUID | str,
    allowed_destinations: Iterable[str] = (),
) -> JsonObject:
    """A ``weave-http@2.0.0`` ConnectionRequest with destinations prefilled and secret *handles* only."""
    try:
        profile_auth = auth if isinstance(auth, AuthProfile) else AuthProfile.model_validate(dict(auth))
    except ValidationError:
        raise HttpActionError(
            [
                _connection_issue(
                    "AUTH",
                    "/config/auth",
                    "The authentication settings are not supported.",
                    "Use none, api-key (with a non-reserved header), basic, bearer or machine-token "
                    "(client ID, HTTPS token endpoint, scopes).",
                )
            ]
        ) from None
    origin = _origin(base_url, "/config/baseUrl")
    slots = profile_auth.slots()
    if set(secret_handles) != slots:
        expected = ", ".join(sorted(slots)) or "none"
        raise HttpActionError(
            [
                _connection_issue(
                    "SECRET",
                    "/secretRef",
                    f"This authentication needs exactly these secret slots: {expected}.",
                    "Map each slot to an operator-provided secret handle, never to the secret value.",
                )
            ]
        )
    for slot, handle in secret_handles.items():
        if not isinstance(handle, str) or len(handle) > 128 or not _RESOURCE_NAME.fullmatch(handle):
            raise HttpActionError(
                [
                    _connection_issue(
                        "SECRET",
                        _ptr("/secretRef", slot),
                        "A secret handle must be a name such as pets-api-key.",
                        "Ask your operator for the handle; the secret value stays in the platform secret store.",
                    )
                ]
            )
    destinations = [origin]
    if profile_auth.endpoint:
        destinations.append(fixed_server(profile_auth.endpoint)[0])
    for index, extra in enumerate(allowed_destinations):
        destinations.append(_origin(extra, _ptr("/allowed_destinations", index)))
    config: JsonObject = {
        "baseUrl": origin,
        "auth": cast(JsonValue, profile_auth.model_dump(exclude_defaults=True)),
    }
    try:
        ProfileConnection.model_validate(config)
        request = ConnectionRequest(
            name=name,
            connector_version_id=UUID(str(connector_version_id)),
            config=config,
            secretRef=dict(secret_handles),
            allowed_destinations=tuple(dict.fromkeys(destinations)),
        )
        validate_profile_connection(request)
    except (ValidationError, ValueError) as error:
        # The descriptor explains rejected fields with request pointers when this build provides them.
        explained = [
            _connection_issue(
                code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,40}", code) else "REQUEST",
                path,
                message[:1000],
            )
            for code, path, message in (
                (getattr(i, "code", None), getattr(i, "path", ""), getattr(i, "message", ""))
                for i in getattr(error, "issues", ())
            )
            if isinstance(path, str) and _POINTER.fullmatch(path) and isinstance(message, str) and message.strip()
        ]
        raise HttpActionError(
            explained
            or [
                _connection_issue(
                    "REQUEST",
                    "",
                    "The connection name, connector version or settings are invalid.",
                    "Use a name such as pets and the weave-http@2.0.0 connector version ID.",
                )
            ]
        ) from None
    return cast(JsonObject, request.model_dump(by_alias=True, mode="json"))


def connection_example(name: str, base_url: str, auth: AuthProfile) -> JsonObject:
    """A reviewable connection template: placeholders stand in for the version ID and secret handles."""
    origin = fixed_server(base_url, plain_http=True)[0]
    destinations = [origin]
    if auth.endpoint:
        destinations.append(fixed_server(auth.endpoint)[0])
    return {
        "name": name,
        "connector_version_id": "<weave-http@2.0.0 connector version ID>",
        "config": {"baseUrl": origin, "auth": cast(JsonValue, auth.model_dump(exclude_defaults=True))},
        "secretRef": {slot: f"<operator secret handle for {slot}>" for slot in sorted(auth.slots())},
        "allowed_destinations": cast(JsonValue, list(dict.fromkeys(destinations))),
    }
