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

"""Thin file transport over the pure compiler API."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import click

from firefly_weave.cli import (
    EXIT_INVALID,
    EXIT_USAGE,
    LocalError,
    emit_result,
    error_result,
    export_files,
    read_bounded,
)
from firefly_weave.compiler.api import CompiledArtifact, compile_source, validate_source
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.catalog import CatalogLock, CatalogSnapshot
from firefly_weave.compiler.ir import (
    ActionNode,
    ArtifactEnvelope,
    BranchOutputNode,
    EndNode,
    SwitchNode,
    TransformNode,
)
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import (
    ArrayExpression,
    Expression,
    ObjectExpression,
    OpExpression,
    RefExpression,
)
from firefly_weave.contracts.limits import Limits


@click.group()
def workflow() -> None:
    """Validate, compile or explain a local definition without connecting to services."""


def _references(value: Expression) -> list[str]:
    if isinstance(value, RefExpression):
        return [value.ref]
    if isinstance(value, ObjectExpression):
        return [ref for child in value.object.values() for ref in _references(child)]
    if isinstance(value, ArrayExpression):
        return [ref for child in value.array for ref in _references(child)]
    if isinstance(value, OpExpression):
        return [ref for child in value.op.args for ref in _references(child)]
    return []


def _explain(artifact: CompiledArtifact) -> None:
    envelope = ArtifactEnvelope.model_validate_json(artifact.to_bytes())
    executable = envelope.executable
    dependencies = {dependency.digest: dependency for dependency in executable.dependencies}
    click.echo("Resolved dependencies:")
    for dependency in executable.dependencies:
        click.echo(f"  {dependency.kind} {dependency.reference} sha256={dependency.digest}")
    if executable.kind == "Workflow":
        click.echo("Flow and source positions:")
        for node in executable.graph.nodes:
            span = envelope.source_map.get(node.path)
            location = f"{span.file or '<source>'}:{span.line}:{span.column}" if span else node.path or "/"
            detail = f" uses={dependencies[node.dependency].reference}" if isinstance(node, ActionNode) else ""
            click.echo(f"  {node.id} ({node.kind}) {location}{detail}")
            expressions: list[Expression] = []
            if isinstance(node, ActionNode):
                expressions.append(node.input)
            elif isinstance(node, TransformNode):
                expressions.append(node.value)
            elif isinstance(node, EndNode | BranchOutputNode):
                expressions.append(node.output)
            elif isinstance(node, SwitchNode):
                expressions.extend(case.when for case in node.cases)
            for expression in expressions:
                for reference in _references(expression):
                    click.echo(f"    reads {reference}")
        for edge in executable.graph.edges:
            branch = f" branch={edge.branch}" if edge.branch is not None else ""
            click.echo(f"  {edge.source} -> {edge.target} ({edge.kind}{branch})")
    click.echo("Runtime guards:")
    for guard in executable.guards:
        click.echo(f"  {guard.path or '/'} {guard.purpose} schema={guard.schema_ref}")


def _run(
    ctx: click.Context,
    command: str,
    source: Path,
    catalog: Path | None,
    output: str,
    strict: bool,
    directory: Path | None = None,
    force: bool = False,
) -> None:
    try:
        data = read_bounded(source, Limits().max_source_bytes)
        source_format: Literal["json", "yaml"] = "json" if source.suffix.lower() == ".json" else "yaml"
        lock = None
        if catalog is None:
            result = validate_source(data, format=source_format, filename=str(source))
        else:
            lock_data = read_bounded(catalog, Limits().max_source_bytes)
            try:
                lock = parse_source(lock_data, format="json", filename=str(catalog)).value
                CatalogLock.model_validate(lock)
                snapshot = CatalogSnapshot.from_lock(lock)
            except (ValueError, TypeError, AttributeError, RecursionError) as error:
                raise LocalError(
                    "WV-CLI-CATALOG", "Catalog lock is malformed, unsupported or has invalid digests."
                ) from error
            result = compile_source(data, format=source_format, filename=str(source), catalog=snapshot, strict=strict)
        if result.ok and directory is not None:
            assert result.artifact is not None and lock is not None
            export_files(
                directory,
                {
                    "compiled-artifact.json": result.artifact.to_bytes(),
                    "executable.json": canonical_bytes(result.artifact.executable),
                    "catalog.lock.json": canonical_bytes(lock),
                },
                force=force,
            )
    except LocalError as error:
        emit_result(error_result(error.code, str(error)), output)
        ctx.exit(EXIT_USAGE)
    emit_result(result, output)
    if command == "explain" and result.artifact is not None and output == "text":
        _explain(result.artifact)
    if not (result.validation_ok if command == "validate" else result.ok):
        ctx.exit(EXIT_INVALID)


def _command(name: str) -> click.Command:
    @click.command(name)
    @click.argument("source", type=click.Path(path_type=Path))
    @click.option("--catalog", type=click.Path(path_type=Path), help="Explicit local JSON catalog lock.")
    @click.option("--output", type=click.Choice(["text", "json"]), default="text")
    @click.option("--strict", is_flag=True, help="Promote complete-analysis warnings to errors.")
    @click.pass_context
    def command(
        ctx: click.Context, /, source: Path, catalog: Path | None, output: str, strict: bool, **extra: object
    ) -> None:
        directory = extra.get("directory")
        _run(
            ctx,
            name,
            source,
            catalog,
            output,
            strict,
            directory=directory if isinstance(directory, Path) else None,
            force=extra.get("force") is True,
        )

    if name == "compile":
        command = click.option(
            "--directory", type=click.Path(path_type=Path), help="Export IR, artifact and catalog lock."
        )(command)
        command = click.option("--force", is_flag=True, help="Replace existing regular export files.")(command)
    return command


for _name in ("validate", "compile", "explain"):
    workflow.add_command(_command(_name))
