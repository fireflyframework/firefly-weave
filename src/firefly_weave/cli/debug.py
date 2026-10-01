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

"""Offline simulation request and command-file transport."""

import json
from pathlib import Path

import click

from firefly_weave.cli import LocalError, emit_result, error_result, read_bounded
from firefly_weave.compiler.api import import_artifact
from firefly_weave.operations.debug.models import DebugCommand, DebugCreate, DebugError, DebugLimits
from firefly_weave.operations.debug.simulator import Simulator


@click.command("simulate")
@click.argument("request_file", type=click.Path(path_type=Path))
@click.option(
    "--commands",
    type=click.Path(path_type=Path),
    help="JSON array of next/continue/signal/advance_time/breakpoints commands.",
)
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
def simulate(request_file: Path, commands: Path | None, output: str) -> None:
    """Simulate a local JSON {artifact,mocks,input,now} request with no external effects."""
    try:
        request_bytes = read_bounded(request_file, DebugLimits().session_bytes)
        if len(request_bytes) > DebugLimits().session_bytes:
            raise DebugError("WV-DEBUG-LIMIT")
        request = DebugCreate.model_validate_json(request_bytes)
        simulator = Simulator(
            import_artifact(request.artifact), mocks=request.mocks, input=request.input, now=request.now
        )
        if commands is None:
            view = simulator.continue_until_breakpoint()
        else:
            raw = read_bounded(commands, DebugLimits().session_bytes)
            if len(raw) > DebugLimits().session_bytes:
                raise DebugError("WV-DEBUG-LIMIT")
            values = json.loads(raw)
            if not isinstance(values, list):
                raise DebugError("WV-DEBUG-COMMAND")
            if len(values) > DebugLimits().commands:
                raise DebugError("WV-DEBUG-LIMIT")
            view = simulator.inspect()
            for value in values:
                view = simulator.command(DebugCommand.model_validate_json(json.dumps(value)))
    except (ValueError, LocalError) as error:
        code = error.code if isinstance(error, DebugError) else "WV-DEBUG-REQUEST"
        emit_result(error_result(code, "Invalid or over-budget simulation request."), output)
        raise SystemExit(1) from None
    if output == "json":
        click.echo(view.model_dump_json())
    else:
        click.echo(f"Simulation {view.status}; {len(view.events)} accepted facts; {len(view.boundaries)} boundaries.")
        if view.selected_node:
            click.echo(f"Ready: {view.selected_node}")
        for diagnostic in view.diagnostics:
            click.echo(f"{diagnostic.code}: {diagnostic.message}")
    if view.status in {"failed", "suspended", "timed_out"}:
        raise SystemExit(1)
