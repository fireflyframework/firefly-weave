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
"""No-code HTTP commands: build ``weave-http@2.0.0`` Actions and read installed descriptors.

``weave connector http-action`` lives in the ``connector`` group beside
``import-openapi``: both turn an HTTP API description into reviewable Actions,
locally and without a server. Remote steps (descriptor discovery, guided
connection creation) reuse the profile-aware target resolution of every other
remote command and report failures with the same value-free JSON problems.
"""

import io
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import click

from firefly_weave.cli import EXIT_INVALID, EXIT_USAGE
from firefly_weave.compiler.canonical import canonical_bytes

SAMPLE_LIMIT = 1024 * 1024


class FlowProblem(Exception):
    """A remote flow's own, value-free failure (for example: a prerequisite is not published)."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class InlineRequest:
    """A request body built in memory, handed to the generated remote commands in place of a file."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def open(self, mode: str = "rb") -> io.BytesIO:
        return io.BytesIO(self.data)


def remote_params(operation_id: str) -> list[click.Parameter]:
    """The exact target options (profile, server, workspace, sign-in) every remote command takes."""
    from firefly_weave.cli.remote import command

    keep = {
        "profile",
        "base_url",
        "tenant",
        "project",
        "environment",
        "auth_config",
        "credential_store",
        "credential_file",
    }
    return [param for param in command(operation_id).params if param.name in keep]


def run_remote(
    ctx: click.Context, operation_id: str, options: dict[str, Any], flow: Callable[[Any], Awaitable[Any]]
) -> Any:
    """Resolve the target like ``remote.command`` and run ``flow`` with an SDK client.

    Errors print one JSON problem on stdout and exit like other remote commands:
    1 for a 4xx answer, 3 for a server or identity-provider failure, 2 for local
    configuration problems.
    """
    import asyncio

    from firefly_weave.cli.remote import Target, TargetProblem, machine_result, resolve_target
    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.sdk import client
    from firefly_weave.sdk.auth import AuthError
    from firefly_weave.sdk.credentials import CredentialError
    from firefly_weave.sdk.errors import WeaveError

    target: Target | None = None
    try:
        target = resolve_target(ctx, OPERATIONS[operation_id], options)

        async def run(selected: Target) -> Any:
            async with client.WeaveClient(selected.base_url, selected.provider, selected.scope) as sdk:
                return await flow(sdk)

        return asyncio.run(run(target))
    except WeaveError as error:
        machine_result(error.problem)
        for diagnostic in error.diagnostics:
            click.echo(diagnostic.model_dump_json(by_alias=True), err=True)
        raise click.exceptions.Exit(1 if error.status < 500 else 3) from None
    except AuthError as error:
        message = "Authentication failed"
        if target is not None and target.profile is not None and error.exit_code != 3:
            message += f"; sign in with 'weave auth login --profile {shlex.quote(target.profile)}'"
        machine_result({"code": error.code, "message": message, "status": 401, "diagnostics": []})
        raise click.exceptions.Exit(error.exit_code) from None
    except TargetProblem as problem:
        machine_result({"code": "WV-CLI-CONFIG", "message": problem.message, "status": 422, "diagnostics": []})
        raise click.exceptions.Exit(EXIT_USAGE) from None
    except FlowProblem as problem:
        machine_result({"code": problem.code, "message": problem.message, "status": problem.status, "diagnostics": []})
        raise click.exceptions.Exit(EXIT_INVALID) from None
    except (ValueError, OSError, ImportError, CredentialError):
        machine_result(
            {
                "code": "WV-CLI-CONFIG",
                "message": "Invalid local configuration or request; install the client extra and check inputs",
                "status": 422,
                "diagnostics": [],
            }
        )
        raise click.exceptions.Exit(EXIT_USAGE) from None


def read_json_file(path: Path, limit: int = SAMPLE_LIMIT) -> Any:
    """Bounded local JSON or YAML (by extension); never a URL."""
    from firefly_weave.compiler.parser import parse_source

    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Local file exceeds its bound")
    if path.suffix.lower() in {".yaml", ".yml"}:
        return parse_source(data, format="yaml").value
    # A JSON sample may be any value; wrap it so the bounded parser accepts non-object roots, and
    # refuse anything that was not exactly one JSON value (such as text closing the wrapper early).
    wrapped = parse_source(b'{"value":' + data + b"}", format="json").value
    if list(wrapped) != ["value"]:
        raise ValueError("A JSON sample must be exactly one JSON value")
    return wrapped["value"]


def _parameter(value: str) -> dict[str, Any]:
    """``LOCATION:NAME[:TYPE][:required]``; TYPE is string, integer or boolean, with ``[]`` for a query array."""
    parts = value.split(":")
    if not 2 <= len(parts) <= 4 or parts[0] not in {"path", "query", "header"}:
        raise click.UsageError("Use --param LOCATION:NAME[:TYPE][:required], for example query:limit:integer.")
    kind = parts[2] if len(parts) > 2 and parts[2] else "string"
    array = kind.endswith("[]")
    kind = kind.removesuffix("[]")
    if kind not in {"string", "integer", "boolean"} or len(parts) == 4 and parts[3] != "required":
        raise click.UsageError("Parameter types are string, integer or boolean (query arrays add []).")
    return {
        "name": parts[1],
        "location": parts[0],
        "type": kind,
        "array": array,
        "required": parts[0] == "path" or len(parts) == 4,
    }


def _emit(output: str, value: Any, lines: list[str]) -> None:
    if output == "json":
        click.echo(canonical_bytes(value).decode())
    else:
        for line in lines:
            click.echo(line)


def _diagnostic_lines(diagnostics: list[Any]) -> list[str]:
    lines = []
    for issue in diagnostics:
        lines.append(f"{issue.severity} {issue.code} {issue.path or '/'}: {issue.message}")
        if issue.hint:
            lines.append(f"  hint: {issue.hint}")
    return lines


@click.command("http-action")
@click.option("--name", required=True, help="Action name, such as get-pet.")
@click.option("--version", "version", default="1.0.0", show_default=True, help="Action version (SemVer).")
@click.option(
    "--method",
    required=True,
    type=click.Choice(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], case_sensitive=False),
    help="GET and HEAD are read-only; other methods are non-idempotent writes and never retried.",
)
@click.option("--path", "path_template", required=True, help="Path template, such as /v1/pets/{petId}.")
@click.option(
    "--param",
    "params",
    multiple=True,
    help="LOCATION:NAME[:TYPE][:required], such as path:petId:string or query:tag:string[]. Repeatable.",
)
@click.option("--status", "statuses", multiple=True, type=click.IntRange(200, 299), help="2xx status (default 200).")
@click.option("--empty-status", "empty_statuses", multiple=True, type=click.IntRange(200, 299))
@click.option("--timeout", "timeout_seconds", type=click.IntRange(1, 30), default=30, show_default=True)
@click.option("--string-max-length", type=click.IntRange(1, 4096), default=256, show_default=True)
@click.option("--description", help="Shown to workflow authors with the Action's input.")
@click.option("--response-sample", type=click.Path(path_type=Path, dir_okay=False), help="JSON or YAML sample.")
@click.option("--response-schema", type=click.Path(path_type=Path, dir_okay=False), help="JSON Schema file.")
@click.option("--body-sample", type=click.Path(path_type=Path, dir_okay=False), help="JSON or YAML request sample.")
@click.option("--body-schema", type=click.Path(path_type=Path, dir_okay=False), help="Request body JSON Schema.")
@click.option("--optional-body", is_flag=True, help="Let workflows omit the request body.")
@click.option("--auth-header", help="The connection's API-key header, rejected as a parameter.")
@click.option("--output-dir", type=click.Path(path_type=Path, file_okay=False), help="Write NAME.action.json here.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def http_action(
    ctx: click.Context,
    name: str,
    version: str,
    method: str,
    path_template: str,
    params: tuple[str, ...],
    statuses: tuple[int, ...],
    empty_statuses: tuple[int, ...],
    timeout_seconds: int,
    string_max_length: int,
    description: str | None,
    response_sample: Path | None,
    response_schema: Path | None,
    body_sample: Path | None,
    body_schema: Path | None,
    optional_body: bool,
    auth_header: str | None,
    output_dir: Path | None,
    output: str,
) -> None:
    """Build and check an Action on the built-in weave-http@2.0.0 connector, offline.

    Samples are only used to infer types; their values never enter the Action.
    The Action compiles against the descriptor bundled with this CLI.
    """
    from firefly_weave.contracts.diagnostics import Diagnostic
    from firefly_weave.sdk.http_actions import HttpActionResult, author_http_action

    request: dict[str, Any] = {
        "name": name,
        "version": version,
        "method": method.upper(),
        "pathTemplate": path_template,
        "parameters": [_parameter(value) for value in params],
        "statuses": list(statuses) or [200],
        "emptyStatuses": list(empty_statuses),
        "timeoutSeconds": timeout_seconds,
        "stringMaxLength": string_max_length,
        "bodyRequired": not optional_body,
    }
    if description:
        request["description"] = description
    if auth_header:
        request["auth"] = {"kind": "api-key", "header": auth_header}
    written: Path | None = None
    try:
        for key, path in (
            ("responseSample", response_sample),
            ("responseSchema", response_schema),
            ("bodySample", body_sample),
            ("bodySchema", body_schema),
        ):
            if path is not None:
                request[key] = read_json_file(path)
        result = author_http_action(request)
        if result.ok and output_dir is not None and result.action is not None:
            written = _write_action(output_dir, name, result.action)
    except (ValueError, OSError):
        result = HttpActionResult(
            ok=False,
            diagnostics=[
                Diagnostic(
                    code="WV-HTTP-ACTION-IO",
                    severity="error",
                    stage="parse",
                    message="A local sample, schema or output file could not be read or written safely.",
                    path="",
                    hint="Use bounded JSON or YAML files; the output file must not exist yet.",
                )
            ],
        )
    value = result.model_dump(by_alias=True, mode="json")
    if written is not None:
        value["file"] = str(written)
    lines = [
        f"HTTP action {name}@{version} is valid on weave-http@2.0.0."
        if result.ok
        else "HTTP action rejected; nothing was written."
    ]
    if written is not None:
        lines.append(f"Wrote {written}. Review it, then publish it with weave definitions publish.")
    lines += _diagnostic_lines(result.diagnostics)
    _emit(output, value, lines)
    if not result.ok:
        ctx.exit(EXIT_INVALID)


def _write_action(directory: Path, name: str, action: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Output directory must not be a symbolic link")
    destination = directory / f"{name}.action.json"
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(canonical_bytes(action).decode() + "\n")
    return destination


def local_descriptor(adapter: str) -> dict[str, Any] | None:
    """The descriptor bundled with this CLI; only built-ins that need no server code are copied."""
    from firefly_weave import __version__
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
    from firefly_weave.sdk.http_actions import HTTP_ADAPTER

    if adapter != HTTP_ADAPTER:
        return None
    descriptor = HTTP_PROFILE_DESCRIPTOR
    manifest = cast(dict[str, Any], descriptor.manifest.value)
    return {
        "provenance": "local-copy",
        "note": (
            f"Copy bundled with Firefly Weave {__version__}. The platform's installed descriptor is authoritative: "
            "publish the manifest the platform reports, so its digest matches the installed adapter."
        ),
        "adapter": adapter,
        "reference": f"{manifest['metadata']['name']}@{manifest['metadata']['version']}",
        "digest": descriptor.manifest.digest,
        "implementation_version": descriptor.implementation_version,
        "manifest": manifest,
        "source": descriptor.manifest.canonical.decode(),
        "capabilities": [c.model_dump(by_alias=True, mode="json") for c in descriptor.capabilities],
        "bindings": [b.model_dump(by_alias=True, mode="json") for b in descriptor.bindings],
    }


def _descriptor_lines(value: dict[str, Any]) -> list[str]:
    lines = [
        f"{value['adapter']}: {value['reference']} (digest {value['digest']})",
        f"Read from: {value['provenance']}.",
    ]
    if value.get("note"):
        lines.append(value["note"])
    if value.get("published_version_id"):
        lines.append(f"Published as connector version {value['published_version_id']}.")
    elif value["provenance"] == "platform":
        lines.append("Not published in this project yet; publish its source with weave definitions publish.")
    lines.append("Use --output json for the manifest, capabilities and bindings.")
    return lines


# Descriptor reads are project-scoped catalog reads; capabilities.read has the same scope and options.
DESCRIPTOR_SCOPE = "capabilities.read"


def descriptor_command() -> click.Command:
    def run(adapter: str, local: bool, output: str, **options: Any) -> None:
        import re

        from firefly_weave.cli.remote import NO_TARGET, TargetProblem, machine_result, resolve_target
        from firefly_weave.contracts.surface import OPERATIONS

        ctx = click.get_current_context()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", adapter):
            raise click.UsageError("Adapter names look like weave-http-v2.")
        copy = local_descriptor(adapter)
        if not local:
            try:
                resolve_target(ctx, OPERATIONS[DESCRIPTOR_SCOPE], options)
            except TargetProblem as problem:
                if problem.message != NO_TARGET or copy is None:
                    machine_result(
                        {"code": "WV-CLI-CONFIG", "message": problem.message, "status": 422, "diagnostics": []}
                    )
                    raise click.exceptions.Exit(EXIT_USAGE) from None
                local = True
        if local:
            if copy is None:
                machine_result(
                    {
                        "code": "WV-CONNECTOR-DESCRIPTOR",
                        "message": "No local copy of this adapter; connect to a platform to read its descriptor",
                        "status": 404,
                        "diagnostics": [],
                    }
                )
                raise click.exceptions.Exit(EXIT_INVALID)
            _emit(output, copy, _descriptor_lines(copy))
            return

        async def read(sdk: Any) -> Any:
            reader = getattr(sdk, "read_connector_descriptor", None)
            if reader is None:
                raise FlowProblem(
                    "WV-CONNECTOR-DESCRIPTOR", "This client cannot read installed descriptors; use --local", 501
                )
            return await reader(adapter)

        view = run_remote(ctx, DESCRIPTOR_SCOPE, options, read)
        value = {**view.model_dump(by_alias=True, mode="json"), "provenance": "platform"}
        _emit(output, value, _descriptor_lines(value))

    return click.Command(
        "descriptor",
        params=[
            click.Argument(["adapter"]),
            click.Option(
                ["--local"], is_flag=True, help="Print the copy bundled with this CLI instead of asking the platform."
            ),
            click.Option(["--output"], type=click.Choice(["json", "text"]), default="json", show_default=True),
            *remote_params(DESCRIPTOR_SCOPE),
        ],
        callback=run,
        help=(
            "Print an installed connector descriptor (manifest, capabilities, bindings).\n\n"
            "Asks the selected platform; with no platform selected, or with --local, prints the copy of a "
            "built-in bundled with this CLI (weave-http-v2 only), marked as a local copy."
        ),
        short_help="Print an installed connector descriptor.",
    )


descriptor = descriptor_command()
