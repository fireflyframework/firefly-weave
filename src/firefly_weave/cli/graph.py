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

"""Export a workflow's verified compiled graph for terminal or browser reading."""

from pathlib import Path
from typing import Literal

import click

from firefly_weave.cli import EXIT_USAGE, LocalError, emit_result, error_result, export_files, read_bounded
from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.lowering import DEFAULT_ARTIFACT_LIMITS
from firefly_weave.sdk.visualization import render_graph


@click.command("graph")
@click.argument("artifact", type=click.Path(path_type=Path))
@click.option("--format", type=click.Choice(["text", "mermaid", "svg"]), default="text", show_default=True)
@click.option("--directory", type=click.Path(path_type=Path), help="Save workflow.txt, workflow.mmd, or workflow.svg.")
@click.option("--force", is_flag=True, help="Replace an existing regular export file.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
def graph(
    artifact: Path, format: Literal["text", "mermaid", "svg"], directory: Path | None, force: bool, output: str
) -> None:
    """Draw ARTIFACT (compiled-artifact.json) without executing its actions.

    First compile your YAML/JSON with workflow compile --catalog ... --directory build.
    Then run: weave workflow graph build/compiled-artifact.json --format svg --directory diagrams
    """
    try:
        data = read_bounded(artifact, DEFAULT_ARTIFACT_LIMITS.max_bytes)
        try:
            compiled = import_artifact(data)
            content = render_graph(compiled, format)
        except (ValueError, TypeError, RecursionError):
            raise LocalError(
                "WV-CLI-GRAPH", "Use a valid compiled Workflow artifact; SVG supports up to 200 nodes."
            ) from None
        name = "workflow." + {"text": "txt", "mermaid": "mmd", "svg": "svg"}[format]
        if directory is not None:
            export_files(directory, {name: content.encode()}, force=force)
    except LocalError as error:
        emit_result(error_result(error.code, str(error)), output)
        raise SystemExit(EXIT_USAGE) from None
    if output == "json":
        click.echo(
            canonical_bytes(
                {"ok": True, "format": format, "content": content, "files": [name] if directory else []}
            ).decode()
        )
    elif directory is None:
        click.echo(content, nl=False)
    else:
        click.echo(f"Saved {name}. Open it to explore the compiled flow; no actions were executed.")
