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
"""Explicit local connector authoring commands with safe machine-readable errors."""

import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import click

from firefly_weave.cli import EXIT_INVALID, emit_result, error_result
from firefly_weave.cli.http_actions import descriptor, http_action
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.sdk import connectors

DOCUMENT_LIMIT = 8 * 1024 * 1024
POLICY_LIMIT = 1024 * 1024
# Fixed messages raised by trusted local code; anything else is summarized, never echoed.
SAFE_REASONS = frozenset(
    {
        "Package metadata exceeds the source budget",
        "Use a nonreserved lowercase package name with optional hyphens",
        "Scaffold target must be absent or an empty directory",
        "Incomplete imported package cannot be built",
        "Select a local project containing pyproject.toml",
        "Packaged metadata resource differs from connector.json",
        "Build project and package declaration differ",
        "Build output must be absent or an empty directory",
        "Unknown conformance mode",
        "Installed connector does not declare fixture conformance tests",
        "A successful reviewed import is required",
        "A successful reviewed built-in import is required",
        "Invalid HTTP package identity",
        "Invalid scaffold target",
        "Scaffold directory was replaced",
        "Scaffold file was replaced or modified",
        "Scaffold directory was contaminated",
        "Connector template has an unrendered placeholder",
        "Duplicate connector allowlist identity",
        "Allowlisted connector entry point is unavailable or ambiguous",
        "Installed package distribution/version differs from its declaration",
        "Duplicate or mismatched adapter identity",
        "Reserved declaration requires the code-owned first-party catalog",
        "Protocol versions must be explicit and unique",
        "Invalid connector manifest/schema",
        "Duplicate or missing capability binding",
        "Manifest/capability binding drift",
        "Manifest/capability contract drift",
        "Unbound capability",
        "Reserved adapter identity",
        "Package must declare the exact native @service type",
        "Connector services must have singleton scope",
        "Invalid connector service contract",
    }
)


def classify(error: BaseException) -> tuple[str, str]:
    """A stable code and a value-free reason for a failed local connector operation."""
    from pydantic import ValidationError

    from firefly_weave.compiler.parser import ParseFailure

    if isinstance(error, subprocess.CalledProcessError):
        return "WV-CONNECTOR-BUILD", "The build backend failed; run 'python -m build' in the project to see why."
    if isinstance(error, ImportError):
        return (
            "WV-CONNECTOR-DEPENDENCY",
            "A required package is not installed; install the connector and the build or server extras.",
        )
    if isinstance(error, FileExistsError):
        return "WV-CONNECTOR-IO", "A target file already exists; choose an absent or empty directory."
    if isinstance(error, PermissionError):
        return "WV-CONNECTOR-IO", "Permission was denied for a local file or directory."
    if isinstance(error, OSError):
        return "WV-CONNECTOR-IO", "A local file or directory could not be read or written."
    if isinstance(error, ParseFailure):
        found = ", ".join(sorted({d.code for d in error.diagnostics})[:5])
        return "WV-CONNECTOR-INVALID", f"The declaration is not bounded, valid JSON ({found})."
    if isinstance(error, ValidationError):
        fields = sorted({".".join(str(p) for p in e["loc"] if isinstance(p, str))[:80] for e in error.errors()})[:5]
        return "WV-CONNECTOR-INVALID", "The declaration has invalid fields: " + (", ".join(fields) or "root") + "."
    if isinstance(error, ValueError) and str(error) in SAFE_REASONS:
        return "WV-CONNECTOR-INVALID", str(error) + "."
    if isinstance(error, ValueError):
        return "WV-CONNECTOR-INVALID", "The declaration or its inputs are invalid; check names, versions and schemas."
    return "WV-CONNECTOR-FAILED", "The connector operation failed; check the declaration and installed dependencies."


def _result(ctx: click.Context, output: str, operation: Any) -> None:
    try:
        value = operation()
    except Exception as error:
        code, reason = classify(error)
        emit_result(error_result(code, reason), output)
        ctx.exit(EXIT_INVALID)
    if output == "json":
        click.echo(canonical_bytes(value).decode())
    else:
        click.echo(f"Connector operation passed ({value['mode']}).")


@click.group()
def connector() -> None:
    """Build trusted connectors, import OpenAPI, and create no-code HTTP actions.

    import-openapi and http-action produce reviewable Actions for the built-in
    weave-http@2.0.0 connector (no Python) or, with --target package, a
    connector package. Everything runs locally; nothing is fetched or sent.
    """


@connector.command("init")
@click.argument("target", type=click.Path(path_type=Path))
@click.option("--name", required=True)
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def init(ctx: click.Context, target: Path, name: str, output: str) -> None:
    """Create an installable echo package in an absent or empty directory."""

    def run() -> dict[str, Any]:
        connectors.scaffold(target, name)
        return {"mode": "scaffold", "directory": str(target)}

    _result(ctx, output, run)


@connector.command("validate")
@click.argument("metadata", type=click.Path(path_type=Path))
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def validate(ctx: click.Context, metadata: Path, output: str) -> None:
    """Validate bounded local JSON without executing package code."""

    def run() -> dict[str, Any]:
        result = connectors.validate(metadata)
        return {
            "mode": "offline-data",
            "manifest_digest": result.descriptor.manifest.digest,
            "package_digest": result.canonical.digest,
        }

    _result(ctx, output, run)


@connector.command("test")
@click.argument("identity")
@click.option("--mode", type=click.Choice(["fixtures", "native"]), default="fixtures")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def test(ctx: click.Context, identity: str, output: str, mode: Literal["fixtures", "native"]) -> None:
    """Execute trusted installed native/fixture tests; no live verification claim."""
    _result(ctx, output, lambda: connectors.test_installed(identity, mode=mode))


@connector.command("package")
@click.argument("project", type=click.Path(path_type=Path))
@click.option("--directory", required=True, type=click.Path(path_type=Path))
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def package(ctx: click.Context, project: Path, directory: Path, output: str) -> None:
    """Execute the selected local project's build backend (requires build)."""

    def run() -> dict[str, Any]:
        connectors.package(project, directory)
        return {"mode": "local-build", "directory": str(directory)}

    _result(ctx, output, run)


def _read(path: Path, limit: int) -> bytes:
    if str(path) == "-":
        data = sys.stdin.buffer.read(limit + 1)
    else:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Local input exceeds its bound")
    return data


def _format(path: Path, data: bytes, selected: str) -> Literal["json", "yaml"]:
    if selected in {"json", "yaml"}:
        return "json" if selected == "json" else "yaml"
    if path.suffix.lower() in {".yaml", ".yml"}:
        return "yaml"
    if path.suffix.lower() == ".json":
        return "json"
    return "json" if data.lstrip()[:1] in {b"{", b"["} else "yaml"


def _relaxations(
    default_string_max_length: int | None,
    numeric_formats: bool,
    ignore_response_headers: bool,
    json_media_only: bool,
    upgrade_openapi_30: bool,
) -> dict[str, Any]:
    flags: dict[str, Any] = {
        "numericFormats": numeric_formats,
        "ignoreResponseHeaders": ignore_response_headers,
        "jsonMediaOnly": json_media_only,
        "upgradeOpenapi30": upgrade_openapi_30,
    }
    if default_string_max_length is not None:
        flags["defaultStringMaxLength"] = default_string_max_length
    return {key: value for key, value in flags.items() if value not in (False, None)}


def _io_failure() -> Any:
    from firefly_weave.contracts.diagnostics import Diagnostic

    return Diagnostic(
        code="WV-IMPORT-IO",
        severity="error",
        stage="parse",
        message="Local import files could not be read or written safely.",
        path="",
        hint="Use local files (or - for standard input) within the size bounds; outputs must not exist yet.",
    )


def _lines(diagnostics: list[Any]) -> list[str]:
    lines = []
    for issue in diagnostics:
        location = f"{issue.source.line}:{issue.source.column} " if issue.source else ""
        lines.append(f"{issue.severity} {issue.code} {location}{issue.path or '/'}: {issue.message}")
        if issue.hint:
            lines.append(f"  hint: {issue.hint}")
    return lines


@connector.command("import-openapi")
@click.argument("document", type=click.Path(path_type=Path, allow_dash=True, dir_okay=False))
@click.option("--operation", "operations", multiple=True, help="operationId to import (default: the policy's).")
@click.option("--policy", "policy_path", type=click.Path(path_type=Path, dir_okay=False), help="Reviewed policy.")
@click.option("--list", "list_operations", is_flag=True, help="List operations with a verdict and reasons.")
@click.option("--init-policy", type=click.Path(path_type=Path, dir_okay=False), help="Scaffold a policy file here.")
@click.option(
    "--target",
    type=click.Choice(["package", "builtin"]),
    default="package",
    show_default=True,
    help="builtin: Actions on weave-http@2.0.0, no package. package: a connector package to build.",
)
@click.option("--all-diagnostics", is_flag=True, help="Report every failing operation, not just the first.")
@click.option("--format", "source_format", type=click.Choice(["auto", "json", "yaml"]), default="auto")
@click.option("--default-string-max-length", type=click.IntRange(1, 4096), help="Relaxation for --list/--init-policy.")
@click.option("--numeric-formats", is_flag=True, help="Relaxation: int32/int64 become bounds; float/double dropped.")
@click.option("--ignore-response-headers", is_flag=True, help="Relaxation: import response bodies only.")
@click.option("--json-media-only", is_flag=True, help="Relaxation: drop non-JSON media types.")
@click.option("--upgrade-openapi-30", is_flag=True, help="Relaxation: upgrade an OpenAPI 3.0.x document.")
@click.option("--name", "policy_name", help="Policy name for --init-policy (default: from the title).")
@click.option("--directory", type=click.Path(path_type=Path, file_okay=False), help="Write reviewed files here.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def import_openapi_command(
    ctx: click.Context,
    document: Path,
    operations: tuple[str, ...],
    policy_path: Path | None,
    list_operations: bool,
    init_policy: Path | None,
    target: Literal["package", "builtin"],
    all_diagnostics: bool,
    source_format: str,
    default_string_max_length: int | None,
    numeric_formats: bool,
    ignore_response_headers: bool,
    json_media_only: bool,
    upgrade_openapi_30: bool,
    policy_name: str | None,
    directory: Path | None,
    output: str,
) -> None:
    """Inventory, scaffold a policy for, or import operations from a local OpenAPI document.

    DOCUMENT is a local JSON or YAML file, or - for standard input; nothing is
    fetched. --list shows every operation with a verdict and the reasons.
    --init-policy writes a policy to review. Importing needs --policy; relaxations
    are explicit in the policy (or the flags) and each one used is reported.
    """
    from firefly_weave.compiler.parser import ParseFailure, parse_source
    from firefly_weave.sdk import openapi_import as importer

    if list_operations and init_policy is not None:
        raise click.UsageError("Use either --list or --init-policy.")
    if (list_operations or init_policy is not None) and directory is not None:
        raise click.UsageError("--directory applies to imports only.")
    if not (list_operations or init_policy is not None) and policy_path is None:
        raise click.UsageError("Importing needs --policy; scaffold one with --init-policy.")
    relax = _relaxations(
        default_string_max_length, numeric_formats, ignore_response_headers, json_media_only, upgrade_openapi_30
    )
    try:
        source = _read(document, DOCUMENT_LIMIT)
        fmt = _format(document, source, source_format)
        rules: Any = None
        if policy_path is not None:
            raw = _read(policy_path, POLICY_LIMIT)
            rules = parse_source(raw, format=_format(policy_path, raw, "auto")).value
            current = rules.get("relaxations")
            # A malformed relaxations value is left in place for the importer to reject.
            if relax and (current is None or isinstance(current, dict)):
                rules = {**rules, "relaxations": {**(current or {}), **relax}}
    except ParseFailure as exc:
        failed = importer.OpenAPIImportResult(
            ok=False,
            diagnostics=[importer.sanitize_import_diagnostic(d) for d in exc.diagnostics],
            truncated=exc.truncated,
        )
        _finish(ctx, output, False, failed, ["OpenAPI input rejected.", *_lines(failed.diagnostics)])
        return
    except (ValueError, OSError):
        failed = importer.OpenAPIImportResult(ok=False, diagnostics=[_io_failure()])
        _finish(ctx, output, False, failed, ["OpenAPI input rejected.", *_lines(failed.diagnostics)])
        return
    selected = list(operations) or None
    if list_operations:
        relaxations = rules.get("relaxations") if isinstance(rules, dict) else None
        listing = importer.inventory(source, source_format=fmt, relaxations=relaxations or relax)
        rows = [
            f"{'yes' if op.supported else 'no ':<4}{op.method:<7}{op.path}  {op.key}"
            + ("" if op.supported else "  (" + ", ".join(sorted({r.code for r in op.reasons})) + ")")
            for op in listing.operations
        ]
        heading = (
            f"{sum(op.supported for op in listing.operations)} of {len(listing.operations)} operations import as is."
            if listing.ok
            else "The document cannot be imported."
        )
        _finish(ctx, output, listing.ok, listing, [heading, *rows, *_lines(listing.diagnostics)])
        return
    if init_policy is not None:
        scaffold = importer.init_policy(source, selected, source_format=fmt, relaxations=relax, name=policy_name)
        written = False
        if scaffold.ok and scaffold.policy is not None:
            try:
                with init_policy.open("x", encoding="utf-8") as stream:
                    stream.write(_pretty(scaffold.policy))
                written = True
            except OSError:
                scaffold = scaffold.model_copy(update={"ok": False, "diagnostics": [_io_failure()]})
        lines = (
            [f"Wrote {init_policy}. Review names, statuses and auth before importing with --policy."]
            if written
            else ["No policy was written."]
        )
        _finish(ctx, output, scaffold.ok, scaffold, lines + _lines(scaffold.diagnostics))
        return
    result = importer.import_openapi(
        source, selected, rules, source_format=fmt, target=target, all_diagnostics=all_diagnostics
    )
    files: list[str] = []
    if directory is not None and result.ok:
        try:
            if target == "builtin":
                files = connectors.scaffold_builtin(directory, result)
            else:
                connectors.scaffold_import(directory, result)
        except (ValueError, OSError):
            result = importer.OpenAPIImportResult(ok=False, diagnostics=[_io_failure()])
    value = result.model_dump(by_alias=True, mode="json")
    if files:
        value["files"] = files
    if result.ok:
        summary = f"OpenAPI import passed ({len(result.actions)} actions, target {target}); review before publishing."
    else:
        summary = "OpenAPI import rejected; nothing was written."
    lines = [summary] + [f"  wrote {directory}/{name}" for name in files] + _lines(result.diagnostics)
    from firefly_weave.contracts.connectors import plain_text_warning

    # The connection template names the API origin: an http:// one works but is never silent. Standard output
    # keeps the command's output in every mode; the warning is one line on standard error.
    example = (result.connection_example or {}).get("config")
    warning = plain_text_warning(example) if isinstance(example, dict) else None
    if warning is not None:
        click.echo(warning, err=True)
    _finish(ctx, output, result.ok, value, lines)


def _pretty(value: Any) -> str:
    import json

    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _finish(ctx: click.Context, output: str, ok: bool, value: Any, lines: list[str]) -> None:
    if output == "json":
        data = value.model_dump(by_alias=True, mode="json") if hasattr(value, "model_dump") else value
        click.echo(canonical_bytes(data).decode())
    else:
        for line in lines:
            click.echo(line)
    if not ok:
        ctx.exit(EXIT_INVALID)


connector.add_command(http_action)
connector.add_command(descriptor)
