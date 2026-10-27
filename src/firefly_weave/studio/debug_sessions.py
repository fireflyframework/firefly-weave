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

"""Step-through sessions of a simulated run, kept in the Studio host's memory for the run log.

A session keeps the simulator's serialized state, never a live simulator, so its bound is measured on what is kept:
at most four sessions, each at most 16 MiB, each expiring after 30 minutes without use. Commands carry the revision
they were made against, so a stale view never moves a session. Nothing here reads the network or files; the caller
passes the time (``time.monotonic()``), so tests choose it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from firefly_weave.studio.simulation import SimulationRefused, simulator_for, stuck_at, unsimulated

if TYPE_CHECKING:
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.operations.debug.models import DebugCommand, DebugSession
    from firefly_weave.operations.debug.simulator import Simulator
    from firefly_weave.studio.local_contracts import ExecuteScripts, SimulationRequest

logger = logging.getLogger(__name__)

SESSIONS = 4
IDLE_SECONDS = 1800
SESSION_BYTES = 16 * 1024 * 1024


@dataclass
class _Kept:
    state: str
    revision: int
    created_at: datetime
    touched: float
    # Test answers and waits that pass by themselves, for Continue; None steps through time by hand.
    scripts: ExecuteScripts | None = None


class HostSessions:
    """The host's step-through sessions, by ID."""

    def __init__(self) -> None:
        self._kept: dict[UUID, _Kept] = {}

    def count(self) -> int:
        return len(self._kept)

    def clear(self) -> None:
        self._kept.clear()

    def create(self, request: SimulationRequest, project: CatalogSnapshot | None, now: float) -> DebugSession:
        """A session paused at the start of the whole workflow, with the request's test data as its mocks."""
        from firefly_weave.compiler.parser import ParseFailure, parse_source
        from firefly_weave.operations.debug.models import DebugError

        self._expire(now)
        if len(self._kept) >= SESSIONS:
            raise SimulationRefused(
                429, "WV-STUDIO-SESSIONS-FULL", "Studio keeps at most 4 run logs at once; close one first"
            )
        try:
            document = parse_source(request.source, format=request.format).value
        except ParseFailure:
            raise SimulationRefused(422, "WV-STUDIO-REQUEST", "The workflow doesn't read as YAML or JSON") from None
        try:
            _, simulator, _ = simulator_for(request, project, document, whole=document, sliced=False)
        except DebugError as error:
            raise SimulationRefused(422, error.code, "The simulation couldn't use this test data") from None
        if simulator is None:
            raise SimulationRefused(
                422, "WV-STUDIO-REQUEST", "The workflow doesn't compile; execute it to see what to fix"
            )
        identifier = uuid4()
        kept = _Kept(
            state="",
            revision=1,
            created_at=datetime.now(UTC),
            touched=now,
            scripts=request.scripts if request.scripts.autoAdvanceWaits else None,
        )
        self._keep(identifier, kept, simulator)
        return self._session(identifier, kept, simulator)

    def read(self, identifier: UUID, now: float) -> DebugSession:
        kept = self._find(identifier, now)
        kept.touched = now
        return self._session(identifier, kept, self._restored(kept))

    def command(self, identifier: UUID, revision: int, command: DebugCommand, now: float) -> DebugSession:
        from firefly_weave.operations.debug.models import DebugError
        from firefly_weave.runtime.kernel import KernelError

        kept = self._find(identifier, now)
        if revision != kept.revision:
            raise SimulationRefused(409, "WV-DEBUG-REVISION", "The run log changed since; reload it")
        simulator = self._restored(kept)
        _refuse_secret_answer(simulator, command)
        try:
            if command.kind == "continue" and kept.scripts is not None:
                # As Execute does: waits pass by themselves and test answers
                # are used, up to a breakpoint, the end, or a question.
                _continue(simulator, kept.scripts)
            else:
                simulator.command(command)
        except DebugError as error:
            raise SimulationRefused(422, error.code, "The run log couldn't do that here") from None
        except KernelError as error:
            raise SimulationRefused(422, str(error), "The simulated run refused that") from None
        except SimulationRefused:
            raise
        except Exception as error:  # noqa: BLE001 - never a 500: the session stops where the simulator did
            place, _ = stuck_at(self._restored(kept), kept.scripts)
            # The kind of failure and where, never its message (it can hold values).
            logger.debug("Studio couldn't simulate %s: %s", place, type(error).__name__)
            raise SimulationRefused(422, "WV-STUDIO-SIMULATION", unsimulated(place)) from None
        moved = _Kept(
            state="", revision=kept.revision + 1, created_at=kept.created_at, touched=now, scripts=kept.scripts
        )
        self._keep(identifier, moved, simulator)
        return self._session(identifier, moved, simulator)

    def delete(self, identifier: UUID) -> None:
        if self._kept.pop(identifier, None) is None:
            raise _not_found()

    def _keep(self, identifier: UUID, kept: _Kept, simulator: Simulator) -> None:
        state = simulator.serialize()
        if len(state.encode()) > SESSION_BYTES:
            # The previous state stays as it was: the command that grew it past the bound didn't happen.
            raise SimulationRefused(422, "WV-DEBUG-LIMIT", "This run grew past what a run log can keep")
        kept.state = state
        self._kept[identifier] = kept

    def _find(self, identifier: UUID, now: float) -> _Kept:
        kept = self._kept.get(identifier)
        if kept is None:
            raise _not_found()
        if now - kept.touched > IDLE_SECONDS:
            del self._kept[identifier]
            raise SimulationRefused(410, "WV-DEBUG-EXPIRED", "This run log expired; execute again")
        # Every request frees the other sessions left idle, not only a create.
        self._expire(now)
        return kept

    def _expire(self, now: float) -> None:
        for identifier in [i for i, kept in self._kept.items() if now - kept.touched > IDLE_SECONDS]:
            del self._kept[identifier]

    @staticmethod
    def _restored(kept: _Kept) -> Simulator:
        from firefly_weave.operations.debug.simulator import Simulator

        return Simulator.restore(kept.state)

    @staticmethod
    def _session(identifier: UUID, kept: _Kept, simulator: Simulator) -> DebugSession:
        from firefly_weave.operations.debug.models import DebugSession

        return DebugSession(
            id=identifier,
            revision=kept.revision,
            created_at=kept.created_at,
            expires_at=datetime.now(UTC) + timedelta(seconds=IDLE_SECONDS),
            view=simulator.inspect(),
        )


def _not_found() -> SimulationRefused:
    return SimulationRefused(404, "WV-STUDIO-DEBUG-NOT-FOUND", "This run log isn't open anymore")


def _refuse_secret_answer(simulator: Simulator, command: DebugCommand) -> None:
    """A signal's payload or a person's answer holding a value marked secret is refused by pointer, never kept."""
    from firefly_weave.compiler.ir import HumanTaskNode, SignalNode
    from firefly_weave.compiler.schemas import _Failure
    from firefly_weave.operations.redaction import classify

    ir = simulator.ir
    bundle = dict(ir.schemas)
    checks: list[tuple[str, object, object]] = []
    if command.kind == "signal":
        for node in ir.graph.nodes:
            if isinstance(node, SignalNode) and node.name == command.name:
                checks.append(("/payload", ir.schemas.get(node.schema_ref), command.payload))
    elif command.kind == "human_decision" and isinstance(command.payload, dict):
        step = (command.name or "").split("[", 1)[0].rsplit("/", 1)[-1]
        for node in ir.graph.nodes:
            if isinstance(node, HumanTaskNode) and node.id == step:
                checks.append(("/payload/data", node.form_schema, command.payload.get("data")))
    for path, schema, value in checks:
        if not isinstance(schema, dict):
            continue
        try:
            classify(schema, value, bundle)  # type: ignore[arg-type]
        except _Failure as failure:
            code = "WV-SCHEMA-SECRET_VALUE" if failure.code == "SECRET_VALUE" else "WV-SCHEMA-RESOURCE_LIMIT"
            raise SimulationRefused(422, code, "A test answer can't hold values marked secret", [path]) from None
        except (RecursionError, ArithmeticError):
            raise SimulationRefused(
                422, "WV-SCHEMA-RESOURCE_LIMIT", "This answer is too large to check", [path]
            ) from None


def _continue(simulator: Simulator, scripts: ExecuteScripts) -> None:
    from firefly_weave.compiler.ir import HumanTaskNode, SignalNode
    from firefly_weave.operations.debug.simulator import TERMINAL
    from firefly_weave.studio.simulation import _drive

    _drive(simulator, scripts, SignalNode, HumanTaskNode, TERMINAL)
