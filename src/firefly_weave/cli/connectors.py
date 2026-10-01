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

from pathlib import Path
from typing import Any, Literal

import click

from firefly_weave.cli import EXIT_INVALID, emit_result, error_result
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.sdk import connectors


def _result(ctx: click.Context, output: str, operation: Any) -> None:
    try:
        value = operation()
    except Exception:
        emit_result(
            error_result("WV-CONNECTOR-INVALID", "Connector operation failed; check declaration and dependencies."),
            output,
        )
        ctx.exit(EXIT_INVALID)
    if output == "json":
        click.echo(canonical_bytes(value).decode())
    else:
        click.echo(f"Connector operation passed ({value['mode']}).")


@click.group()
def connector() -> None:
    """Scaffold, validate, test, or package trusted connectors locally."""


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


@connector.command("import-openapi")
@click.argument("document", type=click.Path(path_type=Path))
@click.option("--operation", "operations", multiple=True, required=True)
@click.option("--policy", "policy_path", required=True, type=click.Path(path_type=Path))
@click.option("--directory", type=click.Path(path_type=Path))
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def import_openapi_command(
    ctx: click.Context,
    document: Path,
    operations: tuple[str, ...],
    policy_path: Path,
    directory: Path | None,
    output: str,
) -> None:
    """Import explicitly selected operations from local JSON without loading a package."""
    from firefly_weave.compiler.parser import ParseFailure, parse_source
    from firefly_weave.contracts.diagnostics import Diagnostic
    from firefly_weave.sdk.openapi_import import OpenAPIImportResult, import_openapi, sanitize_import_diagnostic

    try:
        with document.open("rb") as stream:
            source = stream.read(1048577)
        with policy_path.open("rb") as stream:
            policy_source = stream.read(1048577)
        rules = parse_source(policy_source, format="json").value
        result = import_openapi(source, list(operations), rules)
        if directory is not None and result.ok:
            connectors.scaffold_import(directory, result)
    except ParseFailure as exc:
        result = OpenAPIImportResult(
            ok=False, diagnostics=[sanitize_import_diagnostic(d) for d in exc.diagnostics], truncated=exc.truncated
        )
    except (ValueError, OSError):
        result = OpenAPIImportResult(
            ok=False,
            diagnostics=[
                Diagnostic(
                    code="WV-IMPORT-IO",
                    severity="error",
                    stage="parse",
                    message="Local import files could not be read or written safely.",
                    path="",
                )
            ],
        )
    value = result.model_dump(by_alias=True)
    if output == "json":
        click.echo(canonical_bytes(value).decode())
    else:
        click.echo("OpenAPI import passed; artifacts require review." if result.ok else "OpenAPI import rejected.")
        for issue in result.diagnostics:
            click.echo(f"{issue.code} {issue.path}: {issue.message}")
    if not result.ok:
        ctx.exit(EXIT_INVALID)
