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

"""Export the model-derived offline contracts to an explicit local destination."""

from pathlib import Path

import click

from firefly_weave.cli import EXIT_USAGE, LocalError, emit_result, error_result, export_files
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.contracts.schema_export import export_schemas


@click.group()
def schema() -> None:
    """Inspect published language and IR contracts."""


@schema.command("export")
@click.option("--directory", required=True, type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Replace existing regular export files.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def export(ctx: click.Context, directory: Path, force: bool, output: str) -> None:
    """Export definition, diagnostic, catalog lock and executable schemas."""
    files = {f"{name}.schema.json": canonical_bytes(value) for name, value in export_schemas().items()}
    try:
        export_files(directory, files, force=force)
    except LocalError as error:
        emit_result(error_result(error.code, str(error)), output)
        ctx.exit(EXIT_USAGE)
    if output == "json":
        click.echo(canonical_bytes({"directory": str(directory), "files": [name for name in sorted(files)]}).decode())
    else:
        click.echo(f"Exported {len(files)} schemas to {directory}.")
