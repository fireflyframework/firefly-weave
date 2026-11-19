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


"""`weave transactions conformance`: check one step of a local workflow against the developer's own system.

Everything runs on this computer. The command imports only the handlers module named with ``--handlers`` and reads
secrets only from the environment variables named with ``--secret``. Its report and its errors carry check names,
phases, operation keys, answers and codes, never an input, an output, a file's content or a secret.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import click

from firefly_weave.cli import (
    EXIT_INVALID,
    EXIT_SUCCESS,
    EXIT_USAGE,
    LocalError,
    emit_result,
    error_result,
    read_bounded,
)
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.catalog import CatalogLock, CatalogSnapshot
from firefly_weave.compiler.parser import ParseFailure, parse_source
from firefly_weave.contracts.connectors import ConnectionRequest, ConnectorAdapter
from firefly_weave.contracts.definitions import ConnectorImplementation, WorkerImplementation
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.participants import CHECK_REASONS, ConformanceReport
from firefly_weave.contracts.values import JsonObject

if TYPE_CHECKING:
    # The check imports asyncio; the CLI loads it only when the command runs, so every other command starts lean.
    from firefly_weave.sdk.participant_conformance import ConnectorParticipant, HandlerParticipant, ParticipantStep

POSTGRESQL = "weave-postgresql@1.0.0"
# Local errors of this command that aren't the check's own; fixed text, never a value.
_USAGE = "Invalid invocation; use --help for supported arguments."
_SOURCE = "The workflow, input or steps file isn't a valid document of its kind."


@click.group()
def transactions() -> None:
    """Check that a step Weave undoes handles every request it can get."""


@transactions.command("conformance")
@click.argument("workflow", type=click.Path(path_type=Path))
@click.option("--step", "step_id", required=True, help="The step to check, inside a transaction block.")
@click.option(
    "--catalog", required=True, type=click.Path(path_type=Path), help="The catalog lock with the step's actions."
)
@click.option("--input", "input_file", required=True, type=click.Path(path_type=Path), help="The run's input.")
@click.option(
    "--steps", "steps_file", type=click.Path(path_type=Path), help="Outputs of earlier steps the step reads, by ID."
)
@click.option("--vary", multiple=True, help="A JSON pointer to an input string that gets a new suffix in every check.")
@click.option("--handlers", help="MODULE:ATTRIBUTE naming the worker handlers, or a function that returns them.")
@click.option("--connection", "connections", multiple=True, help="SLOT=FILE: a connection request for a slot.")
@click.option("--secret", "secrets", multiple=True, help="SLOT.NAME=VARIABLE: read a connection secret from VARIABLE.")
@click.option("--private-network", multiple=True, help="A private network PostgreSQL may reach (CIDR).")
@click.option("--plaintext-network", multiple=True, help="A network PostgreSQL may reach without TLS (CIDR).")
@click.option("--ca-file", type=click.Path(path_type=Path), help="CA certificates PostgreSQL's TLS trusts.")
@click.option("--output", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def conformance(
    ctx: click.Context,
    workflow: Path,
    step_id: str,
    catalog: Path,
    input_file: Path,
    steps_file: Path | None,
    vary: tuple[str, ...],
    handlers: str | None,
    connections: tuple[str, ...],
    secrets: tuple[str, ...],
    private_network: tuple[str, ...],
    plaintext_network: tuple[str, ...],
    ca_file: Path | None,
    output: Literal["text", "json"],
) -> None:
    """Send a step's requests in every order Weave can deliver them, and report each check.

    The check sends real requests: run it against a test system, never production. It passes a worker's
    handlers or the built-in PostgreSQL connector the requests Weave would build (a step then its confirm or
    cancel, a cancel or undo without the step, the step after its cancel or undo, each request twice), and reports
    which checks passed. Exit 0 when every check passes, 1 when one fails, 2 when the check can't start.
    """
    import asyncio

    from firefly_weave.sdk.participant_conformance import ConformanceError

    try:
        report = asyncio.run(
            _check(
                workflow,
                step_id,
                catalog,
                input_file,
                steps_file,
                vary,
                handlers,
                connections,
                secrets,
                private_network,
                plaintext_network,
                ca_file,
            )
        )
    except ConformanceError as error:
        emit_result(error_result(error.code, str(error)), output)
        ctx.exit(EXIT_USAGE)
    except LocalError as error:
        emit_result(error_result(error.code, str(error)), output)
        ctx.exit(EXIT_USAGE)
    if output == "json":
        click.echo(canonical_bytes(report.model_dump(mode="json")).decode())
    else:
        for item in report.checks:
            reason = "" if item.reason is None else f": {CHECK_REASONS[item.reason]}"
            click.echo(f"{'passed' if item.passed else 'FAILED'}  {item.check}{reason}")
        passed = sum(item.passed for item in report.checks)
        click.echo(f"{passed} of {len(report.checks)} checks passed.")
    ctx.exit(EXIT_SUCCESS if report.passed else EXIT_INVALID)


async def _check(
    workflow: Path,
    step_id: str,
    catalog: Path,
    input_file: Path,
    steps_file: Path | None,
    vary: tuple[str, ...],
    handlers: str | None,
    connections: tuple[str, ...],
    secrets: tuple[str, ...],
    private_network: tuple[str, ...],
    plaintext_network: tuple[str, ...],
    ca_file: Path | None,
) -> ConformanceReport:
    from firefly_weave.sdk.participant_conformance import (
        ConformanceError,
        HandlerParticipant,
        Participants,
        check_step,
        participant_step,
    )

    snapshot = _catalog(catalog)
    try:
        step = participant_step(_document(workflow), step_id, snapshot)
    except ConformanceError:
        raise
    except (ValueError, TypeError) as error:
        raise LocalError("WV-CLI-SOURCE", _SOURCE) from error
    run_input = _document(input_file, any_value=True)
    earlier = _document(steps_file) if steps_file is not None else None
    parts: list[HandlerParticipant | ConnectorParticipant] = []
    kinds = {item.action.spec.implementation.kind for item in step.actions}
    if "worker" in kinds and handlers is not None:
        parts.append(HandlerParticipant(await _handlers(handlers)))
    if "connector" in kinds:
        parts.append(_connectors(step, connections, secrets, private_network, plaintext_network, ca_file, snapshot))
    return await check_step(step, Participants(*parts), run_input, steps=earlier, vary=vary)


def _catalog(path: Path) -> CatalogSnapshot:
    data = read_bounded(path, Limits().max_source_bytes)
    try:
        lock = parse_source(data, format="json", filename=str(path)).value
        CatalogLock.model_validate(lock)
        return CatalogSnapshot.from_lock(lock)
    except (ValueError, TypeError, AttributeError, RecursionError) as error:
        raise LocalError("WV-CLI-CATALOG", "Catalog lock is malformed, unsupported or has invalid digests.") from error


def _document(path: Path, *, any_value: bool = False) -> Any:
    """A bounded JSON or YAML file: an object, or any JSON value for a run's input."""
    data = read_bounded(path, Limits().max_source_bytes)
    try:
        value = parse_source(data, format="json" if path.suffix.lower() == ".json" else "yaml", filename=str(path))
    except (ParseFailure, ValueError, RecursionError) as error:
        raise LocalError("WV-CLI-SOURCE", _SOURCE) from error
    if not any_value and not isinstance(value.value, dict):
        raise LocalError("WV-CLI-SOURCE", _SOURCE)
    return value.value


async def _handlers(target: str) -> Mapping[str, Any]:
    """The handlers ``MODULE:ATTRIBUTE`` names: a mapping, or a function (sync or async) that returns one."""
    from firefly_weave.sdk.participant_conformance import ConformanceError

    module_name, separator, attribute = target.partition(":")
    if not separator or not module_name or not attribute:
        raise LocalError("WV-CLI-USAGE", _USAGE)
    try:
        found: Any = getattr(importlib.import_module(module_name), attribute)
        if callable(found) and not isinstance(found, Mapping):
            found = found()
            if inspect.isawaitable(found):
                found = await found
    except Exception:
        raise ConformanceError("WV-CONFORMANCE-HANDLER") from None
    if not isinstance(found, Mapping) or not all(
        isinstance(name, str) and callable(handler) for name, handler in found.items()
    ):
        raise ConformanceError("WV-CONFORMANCE-HANDLER")
    return found


def _connectors(
    step: ParticipantStep,
    connections: tuple[str, ...],
    secrets: tuple[str, ...],
    private_network: tuple[str, ...],
    plaintext_network: tuple[str, ...],
    ca_file: Path | None,
    snapshot: CatalogSnapshot,
) -> ConnectorParticipant:
    from firefly_weave.sdk.participant_conformance import ConformanceError, ConnectorParticipant

    requests: dict[str, ConnectionRequest] = {}
    for pair in connections:
        slot, separator, file = pair.partition("=")
        if not separator or not slot or not file:
            raise LocalError("WV-CLI-USAGE", _USAGE)
        try:
            requests[slot] = ConnectionRequest.model_validate_json(json.dumps(_document(Path(file))))
        except (LocalError, ValueError):
            raise ConformanceError("WV-CONFORMANCE-CONNECTION") from None
    variables: dict[tuple[str, str], str] = {}
    for pair in secrets:
        name, separator, variable = pair.partition("=")
        slot, dot, handle = name.partition(".")
        if not separator or not variable or not dot or not slot or not handle:
            raise LocalError("WV-CLI-USAGE", _USAGE)
        variables[slot, handle] = variable
    # Every connection the step uses, and each of its secrets, must be there before anything is imported or sent.
    for item in step.actions:
        if not isinstance(item.action.spec.implementation, ConnectorImplementation):
            continue
        request = requests.get(item.connection or "")
        if request is None:
            raise ConformanceError("WV-CONFORMANCE-CONNECTION")
        for handle in request.secret_refs:
            named = variables.get((item.connection or "", handle))
            if named is None or not os.environ.get(named):
                raise ConformanceError("WV-CONFORMANCE-CONNECTION")
    adapters: dict[str, ConnectorAdapter] = {}
    if any(_uses(item.action.spec.implementation) == POSTGRESQL for item in step.actions):
        try:
            from firefly_weave.connectors.postgresql import PostgresConnector, PostgresPolicy
        except ImportError:
            raise ConformanceError("WV-CONFORMANCE-DEPENDENCY") from None
        try:
            policy = PostgresPolicy(
                private_networks=private_network,
                plaintext_networks=plaintext_network,
                ca_file=None if ca_file is None else str(ca_file),
            )
        except ValueError:
            raise LocalError("WV-CLI-USAGE", _USAGE) from None
        adapters[POSTGRESQL] = PostgresConnector(policy)

    def secret(slot: str, name: str) -> str:
        return os.environ[variables[slot, name]]

    schemas: dict[str, JsonObject] = {name: document.value for name, document in snapshot.schemas.items()}
    return ConnectorParticipant(adapters, requests, secret, schemas)


def _uses(implementation: WorkerImplementation | ConnectorImplementation) -> str | None:
    return implementation.uses if isinstance(implementation, ConnectorImplementation) else None
