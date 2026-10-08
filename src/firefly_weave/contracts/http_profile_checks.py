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

"""Plain-language, pointer-precise checks for HTTP profile 2.0.0 Actions and connections.

Configuration and connection findings only explain rejections. The profile models in
``http_profiles`` stay the single authority for them: a check never rejects a value the models
accept, and every value the models reject gets at least one finding. Action findings add the
executor's own rules (action effect and method, inputSchema groups and outputSchema shape),
which otherwise fail only at run time; a schema that cannot be checked in isolation is left
to the runtime validation. Echoed names are bounded and printable, and findings are capped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypeGuard

from pydantic import ValidationError

from firefly_weave.compiler.action_config import ActionConfigCheck, ActionConfigCode, ActionConfigIssue
from firefly_weave.compiler.source_map import pointer_child
from firefly_weave.contracts.connectors import ConnectionIssue, ConnectionIssueCode, ConnectionRequest
from firefly_weave.contracts.http_profiles import (
    HEADER_NAME,
    NAME,
    PLAIN_HTTP,
    AuthProfile,
    HttpOperation,
    HttpParameter,
    check_profile_connection,
    fixed_server,
    plain_http_allowed,
    protected,
)
from firefly_weave.contracts.values import JsonObject

CONFIG = "/spec/implementation/config"
INPUT = "/spec/inputSchema"
OUTPUT = "/spec/outputSchema"
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
SAFE_METHODS = frozenset({"GET", "HEAD"})
EFFECTS = frozenset({"read_only", "non_idempotent"})
# The built-in weave-http@2.0.0 actions and the only effect each one may run with.
ACTION_EFFECTS = {"read": "read_only", "write": "non_idempotent"}
GROUPS = {"path": "path", "query": "query", "header": "headers"}
LABELS = {"path": "path parameter", "query": "query parameter", "header": "header"}
PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_-]{0,127}\}")
SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]{1,256}")
COMPOSITE = ("$ref", "$dynamicRef", "allOf", "anyOf", "oneOf", "not", "if", "then", "else")
# Keywords whose meaning depends on the enclosing document; a subschema using them cannot be
# checked on its own without risking a false rejection.
RELATIVE = frozenset({"$dynamicRef", "$id", "$anchor", "$dynamicAnchor"})
# Findings past this bound add nothing an author can act on (the compiler reports at most 100).
MAX_ISSUES = 100


def _printable(value: object, limit: int = 128) -> str:
    """Echo tenant-supplied text safely: bounded and without control or formatting characters."""
    return "".join(c if c.isprintable() else "?" for c in str(value)[:limit])


def _context_dependent(schema: object) -> bool:
    """Whether a subschema resolves anything against its root (local ``#`` references, anchors, IDs)."""
    pending = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if (isinstance(reference, str) and reference.startswith("#")) or RELATIVE.intersection(node):
                return True
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return False


def _unexplained(found: list[tuple[str, str]], explained: list[str]) -> list[tuple[str, str]]:
    """Drop model findings at, or above, a pointer that already has a specific explanation."""
    covered: set[str] = set()
    for pointer in explained:
        while pointer and pointer not in covered:
            covered.add(pointer)
            pointer = pointer.rsplit("/", 1)[0]
    return [(path, message) for path, message in found if path not in covered]


def _model_errors(error: ValidationError, base: str, label: str) -> list[tuple[str, str]]:
    """Map field-level model errors to pointers; root model checks are explained elsewhere."""
    result = []
    for item in error.errors(include_input=False, include_url=False):
        location = item["loc"]
        pointer = base
        for part in location:
            pointer = pointer_child(pointer, str(part))
        name = _printable(location[-1]) if location else ""
        kind = item["type"]
        if kind == "missing":
            message = f"Add the required {label} field '{name}'."
        elif kind == "extra_forbidden":
            message = f"'{name}' is not a field of the {label}; remove it."
        elif kind == "value_error":
            message = _printable(str(item["msg"]).removeprefix("Value error, ").rstrip("."), 256) + "."
        else:
            detail = _printable(item["msg"], 256)
            message = f"'{name}' has an unsupported value: {detail}." if name else detail + "."
        result.append((pointer, message))
    return result


def _template_problem(path: str) -> tuple[str | None, list[str]]:
    """Mirror ``template_names`` with a specific explanation for each rejection."""
    if not path.startswith("/") or path.startswith("//"):
        return "The path must start with a single '/', for example /v1/pets/{petId}.", []
    if len(path) > 4096:
        return "The path is longer than 4096 characters.", []
    if any(c in path for c in ("%", "?", "#", "\\")):
        return "The path cannot contain '%', '?', '#' or '\\'; send query values as query parameters.", []
    if any(ord(c) <= 32 or ord(c) >= 127 for c in path):
        return "The path must use visible ASCII characters only.", []
    names = []
    for part in path[1:].split("/"):
        if part in {".", ".."}:
            return "The path cannot contain '.' or '..' segments.", []
        if "{" in part or "}" in part:
            if not PLACEHOLDER.fullmatch(part):
                return f"'{_printable(part, 64)}' must be a whole path segment placeholder such as {{petId}}.", []
            names.append(part[1:-1])
    if len(names) != len(set(names)):
        return "Each path placeholder can appear only once.", []
    return None, names


def _parameter_problems(config: dict[str, Any], names: list[str] | None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    parameters = config.get("parameters", [])
    if not isinstance(parameters, list):
        return found
    seen: set[tuple[str, str]] = set()
    path_parameters: set[str] = set()
    for index, parameter in enumerate(parameters):
        here = f"{CONFIG}/parameters/{index}"
        if not isinstance(parameter, dict):
            continue
        name, location = parameter.get("name"), parameter.get("location")
        if not isinstance(name, str) or location not in GROUPS:
            continue
        if location == "header":
            if protected(name):
                found.append(
                    (
                        here + "/name",
                        f"Header '{_printable(name)}' is set by the platform or the connection and cannot be an "
                        "action parameter.",
                    )
                )
            elif not HEADER_NAME.fullmatch(name):
                found.append((here + "/name", "Header names must be HTTP tokens such as X-Request-Id."))
        elif not NAME.fullmatch(name):
            found.append(
                (
                    here + "/name",
                    "Parameter names start with a letter or '_' and use letters, digits, '_' or '-' "
                    "(at most 128 characters).",
                )
            )
        if parameter.get("array") is True and location != "query":
            found.append((here + "/array", "Only query parameters can be arrays."))
        if location == "path":
            path_parameters.add(name)
            if parameter.get("required") is not True:
                found.append((here + "/required", "Path parameters must be required."))
            if names is not None and name not in names:
                found.append(
                    (
                        here + "/name",
                        f"Path parameter '{_printable(name)}' does not appear in the path template; add "
                        f"{{{_printable(name)}}} to the path or remove the parameter.",
                    )
                )
        key = (location, name.lower() if location == "header" else name)
        if key in seen:
            found.append((here + "/name", f"The {LABELS[location]} '{_printable(name)}' is declared more than once."))
        seen.add(key)
    for name in names or []:
        if name not in path_parameters:
            found.append(
                (CONFIG + "/path", f"Path variable {{{name}}} needs a required path parameter named '{name}'.")
            )
    return found


def _status_problems(config: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    statuses, empty = config.get("statuses"), config.get("emptyStatuses", [])
    if not isinstance(statuses, list) or any(type(s) is not int for s in statuses):
        return found
    seen: set[int] = set()
    for index, status in enumerate(statuses):
        if not 200 <= status <= 299:
            found.append((f"{CONFIG}/statuses/{index}", f"Status {status} is not a 2xx status; only 2xx is supported."))
        elif status in seen:
            found.append((f"{CONFIG}/statuses/{index}", f"Status {status} is listed more than once."))
        seen.add(status)
    if not isinstance(empty, list) or any(type(s) is not int for s in empty):
        return found
    for index, status in enumerate(empty):
        if status not in statuses:
            found.append(
                (f"{CONFIG}/emptyStatuses/{index}", f"Status {status} is listed as empty; also add it to statuses.")
            )
    for index, status in enumerate(statuses):
        if status in {204, 205} and status not in empty:
            found.append((f"{CONFIG}/statuses/{index}", f"Status {status} never has a body; list it in emptyStatuses."))
    if config.get("method") == "HEAD" and set(statuses) != set(empty):
        found.append((CONFIG + "/emptyStatuses", "HEAD responses have no body; list every status in emptyStatuses."))
    return found


def _config_problems(config: dict[str, Any]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    names: list[str] | None = None
    path = config.get("path")
    if isinstance(path, str):
        problem, parsed = _template_problem(path)
        if problem is not None:
            found.append((CONFIG + "/path", problem))
        else:
            names = parsed
    found += _parameter_problems(config, names)
    found += _status_problems(config)
    method, effect = config.get("method"), config.get("sideEffect")
    if effect == "read_only" and method in METHODS and method not in SAFE_METHODS:
        found.append(
            (
                CONFIG + "/sideEffect",
                f"{method} requests can change data; use sideEffect non_idempotent (read_only allows only "
                "GET or HEAD).",
            )
        )
    return found


def _plain(schema: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(schema, dict) and not any(keyword in schema for keyword in COMPOSITE)


def _expected(parameter: HttpParameter) -> JsonObject:
    scalar: JsonObject = {"type": parameter.type}
    return {"type": "array", "items": scalar} if parameter.array else scalar


def _incompatible(source: JsonObject, target: JsonObject, schemas: Mapping[str, JsonObject]) -> bool:
    from firefly_weave.compiler.typecheck import TypeCheckLimit, check_compatibility

    try:
        return check_compatibility(source, target, dict(schemas)) == "incompatible"
    except TypeCheckLimit:
        return False


def _input_problems(operation: HttpOperation, check: ActionConfigCheck) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    schema = check.spec.get("inputSchema")
    if not _plain(schema):
        return found
    declared_groups = schema.get("properties")
    properties: dict[str, Any] = declared_groups if isinstance(declared_groups, dict) else {}
    required = set(schema["required"]) if isinstance(schema.get("required"), list) else set()
    for key in properties:
        if key not in {"path", "query", "headers", "body"}:
            found.append(
                (
                    pointer_child(INPUT + "/properties", key),
                    f"HTTP actions accept only path, query, headers and body inputs; '{_printable(key)}' would "
                    "be rejected.",
                )
            )
    if "body" in properties and operation.method in SAFE_METHODS:
        found.append((INPUT + "/properties/body", f"{operation.method} requests cannot send a body; remove body."))
    for location, group in GROUPS.items():
        parameters = {p.name: p for p in operation.parameters if p.location == location}
        group_path = INPUT + "/properties/" + group
        group_schema = properties.get(group)
        if group_schema is None:
            for parameter in parameters.values():
                if parameter.required:
                    found.append(
                        (
                            INPUT,
                            f"Required {LABELS[location]} '{parameter.name}' is missing from inputSchema; declare "
                            f"it under properties.{group} and list it as required.",
                        )
                    )
            continue
        if not _plain(group_schema):
            continue
        declared_names = group_schema.get("properties")
        group_properties: dict[str, Any] = declared_names if isinstance(declared_names, dict) else {}
        group_required = set(group_schema["required"]) if isinstance(group_schema.get("required"), list) else set()
        for name in group_properties:
            if name not in parameters:
                found.append(
                    (
                        pointer_child(group_path + "/properties", name),
                        f"'{_printable(name)}' is not a {LABELS[location]} of this operation; the request would "
                        "be rejected.",
                    )
                )
        for parameter in parameters.values():
            declared = group_properties.get(parameter.name)
            if (
                isinstance(declared, dict)
                and not _context_dependent(declared)
                and _incompatible(declared, _expected(parameter), check.schemas)
            ):
                kind = f"an array of {parameter.type} values" if parameter.array else f"a {parameter.type}"
                found.append(
                    (
                        pointer_child(group_path + "/properties", parameter.name),
                        f"The {LABELS[location]} '{parameter.name}' takes {kind}; this schema allows other values.",
                    )
                )
            if not parameter.required:
                continue
            if group not in required:
                found.append(
                    (
                        INPUT + "/required",
                        f"inputSchema must require '{group}' because the operation has required {LABELS[location]}s.",
                    )
                )
            if parameter.name not in group_required:
                found.append(
                    (
                        group_path + "/required",
                        f"inputSchema.properties.{group} must require '{parameter.name}'; the request cannot be "
                        "sent without it.",
                    )
                )
    return found


def _output_problems(operation: HttpOperation, check: ActionConfigCheck) -> list[tuple[str, str]]:
    from firefly_weave.compiler.schemas import validate_payload

    found: list[tuple[str, str]] = []
    schema = check.spec.get("outputSchema")
    if not isinstance(schema, dict):
        return found
    bundle = dict(check.schemas)
    for status in operation.empty_statuses:
        if validate_payload(schema, {"status": status, "body": None}, bundle):
            found.append(
                (
                    OUTPUT,
                    f'Status {status} returns no body, so outputSchema must accept {{"status": {status}, '
                    '"body": null}.',
                )
            )
    if not _plain(schema):
        return found
    if isinstance(schema.get("required"), list):
        for key in schema["required"]:
            if key not in {"status", "body"}:
                found.append(
                    (
                        OUTPUT + "/required",
                        f"HTTP actions return only status and body; '{_printable(key)}' is never present.",
                    )
                )
    properties = schema.get("properties")
    status_schema = properties.get("status") if isinstance(properties, dict) else None
    # A status schema that resolves against the document root is left to the runtime check.
    if isinstance(status_schema, dict) and not _context_dependent(status_schema):
        for status in operation.statuses:
            if status not in operation.empty_statuses and validate_payload(status_schema, status, bundle):
                found.append(
                    (
                        OUTPUT + "/properties/status",
                        f"The operation accepts status {status}, but outputSchema.properties.status rejects it.",
                    )
                )
    return found


def action_issues(check: ActionConfigCheck) -> list[ActionConfigIssue]:
    """Explain why an Action cannot run on the built-in weave-http@2.0.0 profile executor."""
    issues: list[ActionConfigIssue] = []
    seen: set[tuple[str, str]] = set()

    def add(found: list[tuple[str, str]], code: ActionConfigCode = "CONFIG_CONTRACT") -> None:
        for path, message in found:
            if len(issues) < MAX_ISSUES and (path, message) not in seen:
                seen.add((path, message))
                issues.append(ActionConfigIssue(path, message, code))

    config = check.config
    if not isinstance(config, dict):
        add([(CONFIG, "The configuration must be an HTTP profile 2.0.0 operation object.")])
        return issues
    operation: HttpOperation | None = None
    try:
        operation = HttpOperation.model_validate(config)
    except ValidationError as error:
        add(_config_problems(config))
        explained = [item.path for item in issues]
        add(
            [
                (path, message)
                for path, message in _unexplained(_model_errors(error, CONFIG, "HTTP operation"), explained)
                if path != CONFIG
            ]
        )
        if not issues:
            add([(CONFIG, "The configuration is not a valid HTTP profile 2.0.0 operation.")])
    method, effect = config.get("method"), config.get("sideEffect")
    expected = ACTION_EFFECTS.get(check.action)
    effect_mismatch = expected is not None and effect in EFFECTS and effect != expected
    if effect_mismatch and check.action == "read":
        add(
            [
                (
                    CONFIG + "/sideEffect",
                    "The read action runs only read_only operations (GET or HEAD); use the write action for "
                    "requests that change data.",
                )
            ]
        )
    elif effect_mismatch:
        add(
            [
                (
                    CONFIG + "/sideEffect",
                    "The write action runs only non_idempotent operations; use the read action for read_only "
                    "GET or HEAD requests.",
                )
            ]
        )
    if check.action == "read" and method in METHODS and method not in SAFE_METHODS:
        add(
            [
                (
                    CONFIG + "/method",
                    f"The read action sends only GET or HEAD requests; use the write action for {method}.",
                )
            ]
        )
    if not effect_mismatch and effect in EFFECTS and check.spec.get("sideEffect") != effect:
        add(
            [("/spec/sideEffect", f"The action sideEffect must equal the HTTP operation's sideEffect ({effect}).")],
            "SIDE_EFFECT_CONTRACT",
        )
    if operation is not None:
        add(_input_problems(operation, check), "INPUT_CONTRACT")
        add(_output_problems(operation, check), "OUTPUT_CONTRACT")
    return issues


def _auth_problems(auth: dict[str, Any]) -> list[tuple[str, str]]:
    """Mirror ``AuthProfile.checked`` with one pointer per rejected field."""
    found: list[tuple[str, str]] = []
    kind, header = auth.get("kind"), auth.get("header")
    if kind == "api-key":
        if not header:
            found.append(
                ("/config/auth/header", "The api-key profile needs the header that carries the key, such as X-API-Key.")
            )
        elif isinstance(header, str) and (len(header) > 128 or not HEADER_NAME.fullmatch(header) or protected(header)):
            found.append(
                (
                    "/config/auth/header",
                    f"Header '{_printable(header)}' cannot carry an API key; choose a custom header such as X-API-Key.",
                )
            )
    elif header is not None:
        found.append(("/config/auth/header", "Only the api-key profile uses a header; remove it."))
    if kind == "machine-token":
        if not auth.get("client_id"):
            found.append(("/config/auth/client_id", "The machine-token profile needs a client_id."))
        endpoint = auth.get("endpoint")
        if not endpoint:
            found.append(("/config/auth/endpoint", "The machine-token profile needs the HTTPS token endpoint."))
        elif isinstance(endpoint, str):
            try:
                fixed_server(endpoint)
            except ValueError:
                found.append(
                    (
                        "/config/auth/endpoint",
                        "Use an HTTPS token endpoint such as https://auth.example.com/oauth/token.",
                    )
                )
        scopes = auth.get("scopes", [])
        if isinstance(scopes, list) and (
            len(set(map(str, scopes))) != len(scopes)
            or any(not isinstance(s, str) or not SCOPE.fullmatch(s) for s in scopes)
        ):
            found.append(("/config/auth/scopes", "Scopes must be unique OAuth scope tokens without spaces or quotes."))
    else:
        for name in ("client_id", "endpoint"):
            if auth.get(name) is not None:
                found.append(("/config/auth/" + name, f"Only the machine-token profile uses {name}; remove it."))
        if auth.get("scopes"):
            found.append(("/config/auth/scopes", "Only the machine-token profile uses scopes; remove them."))
        if auth.get("authentication", "client_secret_post") != "client_secret_post":
            found.append(
                ("/config/auth/authentication", "Only the machine-token profile chooses a client authentication.")
            )
    return found


def connection_issues(request: ConnectionRequest) -> list[ConnectionIssue]:
    """Explain why a connection request does not fit the HTTP profile 2.0.0 connection policy."""
    issues: list[ConnectionIssue] = []

    seen: set[tuple[str, str]] = set()

    def add(path: str, message: str, code: ConnectionIssueCode = "CONFIG") -> None:
        if len(issues) < MAX_ISSUES and (path, message) not in seen:
            seen.add((path, message))
            issues.append(ConnectionIssue(path, message, code))

    config = request.config
    origin: str | None = None
    base = config.get("baseUrl")
    if not isinstance(base, str):
        add("/config/baseUrl", "Enter the API origin, for example https://api.example.com.")
    else:
        try:
            origin, path = fixed_server(base, plain_http=plain_http_allowed(base))
        except ValueError:
            add(
                "/config/baseUrl",
                PLAIN_HTTP
                if base.strip().lower().startswith("http://")
                else "Use an HTTPS origin such as https://api.example.com, without credentials, a query or a fragment.",
            )
        else:
            if path:
                add(
                    "/config/baseUrl",
                    "Use only the origin here, for example https://api.example.com; put the base path in each "
                    "operation path.",
                )
    for key in config:
        if key not in {"baseUrl", "auth"}:
            add(
                pointer_child("/config", key),
                f"'{_printable(key)}' is not an HTTP profile connection setting; remove it.",
            )
    auth: AuthProfile | None = None
    value = config.get("auth")
    if not isinstance(value, dict):
        add("/config/auth", "Choose an authentication profile: none, api-key, basic, bearer or machine-token.", "AUTH")
    else:
        try:
            auth = AuthProfile.model_validate(value)
        except ValidationError as error:
            problems = _auth_problems(value)
            for path, message in problems:
                add(path, message, "AUTH")
            for path, message in _model_errors(error, "/config/auth", "authentication"):
                if path != "/config/auth" and not any(p == path or p.startswith(path + "/") for p, _ in problems):
                    add(path, message, "AUTH")
            if not problems and all(item.code != "AUTH" for item in issues):
                add("/config/auth", "The authentication profile is not valid for HTTP profile 2.0.0.", "AUTH")
    allowed: set[str] = set()
    for index, destination in enumerate(request.allowed_destinations):
        try:
            allowed.add(fixed_server(destination, plain_http=plain_http_allowed(destination))[0])
        except ValueError:
            add(
                f"/allowed_destinations/{index}",
                PLAIN_HTTP
                if destination.strip().lower().startswith("http://")
                else "HTTP profile connections can reach only HTTPS origins such as https://api.example.com.",
                "DESTINATION",
            )
    if origin is not None and origin not in allowed:
        add(
            "/allowed_destinations",
            f"Add {origin} to allowed_destinations so requests can reach the API.",
            "DESTINATION",
        )
    if auth is not None:
        expected, given = auth.slots(), set(request.secret_refs)
        for slot in sorted(expected - given):
            add(
                pointer_child("/secretRef", slot),
                f"The {auth.kind} profile needs a secret handle in slot '{slot}'.",
                "SECRET",
            )
        for slot in sorted(given - expected):
            add(
                pointer_child("/secretRef", slot),
                f"The {auth.kind} profile does not use slot '{_printable(slot)}'.",
                "SECRET",
            )
        if auth.endpoint:
            endpoint = fixed_server(auth.endpoint)[0]
            if endpoint not in allowed:
                add(
                    "/allowed_destinations",
                    f"Add the token endpoint origin {endpoint} to allowed_destinations.",
                    "DESTINATION",
                )
    if not issues:
        try:
            check_profile_connection(request)
        except ValueError:
            add("/config", "The HTTP profile does not accept this connection.")
    return issues
