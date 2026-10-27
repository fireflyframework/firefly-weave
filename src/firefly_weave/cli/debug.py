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
from firefly_weave.compiler.ir import UnsupportedIR
from firefly_weave.operations.debug.models import (
    CalledWorkflow,
    DebugCommand,
    DebugCreate,
    DebugError,
    DebugLimits,
    DebugView,
    ScriptedError,
)
from firefly_weave.operations.debug.simulator import Simulator


@click.command("simulate")
@click.argument("request_file", type=click.Path(path_type=Path))
@click.option(
    "--commands",
    type=click.Path(path_type=Path),
    help="JSON array of next/continue/step_out/signal/human_decision/advance_time/breakpoints commands.",
)
@click.option(
    "--workflow",
    "called",
    multiple=True,
    metavar="REF=ARTIFACT.json",
    help="The compiled artifact of a workflow version a call runs, to step into it; repeat for each version.",
)
@click.option(
    "--error",
    "failures",
    multiple=True,
    metavar="KEY=CODE",
    help="Make a step fail with CODE instead of using a mock, such as node:send[2]=RATE_LIMITED; repeatable.",
)
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
def simulate(
    request_file: Path, commands: Path | None, called: tuple[str, ...], failures: tuple[str, ...], output: str
) -> None:
    """Simulate a local JSON {artifact,mocks,errors,input,now} request with no external effects."""
    scripted: dict[str, ScriptedError] = {}
    for value in failures:
        key, separator, code = value.rpartition("=")
        try:
            if not separator or not key:
                raise ValueError(value)
            scripted[key] = ScriptedError(code=code)
        except ValueError:
            raise click.BadParameter("Use KEY=CODE, such as node:send=RATE_LIMITED.", param_hint="--error") from None
    artifacts: dict[str, Path] = {}
    for value in called:
        reference, separator, path = value.partition("=")
        if not separator or "@" not in reference or not path:
            raise click.BadParameter(
                "Use REF=ARTIFACT.json, such as enrich@1.0.0=enrich.json.", param_hint="--workflow"
            )
        artifacts[reference] = Path(path)
    try:
        request_bytes = read_bounded(request_file, DebugLimits().session_bytes)
        if len(request_bytes) > DebugLimits().session_bytes:
            raise DebugError("WV-DEBUG-LIMIT")
        request = DebugCreate.model_validate_json(request_bytes)
        if request.resolve_workflows:
            # Published versions live on a platform; a local simulation takes their artifacts from --workflow.
            raise DebugError("WV-DEBUG-WORKFLOW")
        workflows = dict(request.workflows)
        for reference, location in artifacts.items():
            mocks = workflows[reference].mocks if reference in workflows else {}
            artifact = json.loads(read_bounded(location, DebugLimits().session_bytes))
            workflows[reference] = CalledWorkflow(artifact=artifact, mocks=mocks)
        simulator = Simulator(
            import_artifact(request.artifact),
            mocks=request.mocks,
            input=request.input,
            now=request.now,
            workflows=workflows,
            errors={**request.errors, **scripted},
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
    except UnsupportedIR as error:
        message = "This version does not run the IR version or language features of this workflow."
        if error.missing:
            message += " Missing language features: " + ", ".join(error.missing) + "."
        emit_result(error_result("WV-IR-UNSUPPORTED", message), output)
        raise SystemExit(1) from None
    except (ValueError, LocalError) as error:
        code = error.code if isinstance(error, DebugError) else "WV-DEBUG-REQUEST"
        emit_result(error_result(code, "Invalid or over-budget simulation request."), output)
        raise SystemExit(1) from None
    if output == "json":
        click.echo(view.model_dump_json())
    else:
        echo_view(view)
    # A called workflow its caller waits for that is behind an incident never returns; a detached one fails nothing.
    stuck = any(f.mode == "wait" and f.status == "suspended" for f in view.frames)
    if view.status in {"failed", "suspended", "timed_out"} or stuck:
        raise SystemExit(1)


def echo_view(view: DebugView) -> None:
    click.echo(f"Simulation {view.status}; {len(view.events)} accepted facts; {len(view.boundaries)} boundaries.")
    frames = {frame.key: frame for frame in view.frames}
    if view.selected_frame is not None and frames[view.selected_frame].selected_node:
        click.echo(f"Ready: {view.selected_frame}/{frames[view.selected_frame].selected_node}")
    elif view.selected_node:
        click.echo(f"Ready: {view.selected_node}")
    loops = [("", loop) for loop in view.loops] + [(f.key + "/", loop) for f in view.frames for loop in f.loops]
    for prefix, loop in loops:
        click.echo(
            f"Loop {prefix}{loop.instance_key or loop.node_id}: {loop.completed} of {loop.count} items finished, "
            f"{loop.running} running."
        )
    for frame in view.frames:
        status = frame.status.replace("not_simulated", "not simulated")
        click.echo(f"Called workflow {frame.key} ({frame.workflow}): {status}.")
    for diagnostic in view.diagnostics:
        click.echo(f"{diagnostic.code}: {diagnostic.message}")
