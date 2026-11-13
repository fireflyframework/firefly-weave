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

"""Recorded transaction histories replay in a debug session to the same end.

``session_from_history`` is the rule that turns a recorded run into a session: what each recorded result becomes
(a mock, a scripted failure, a key that never answers) and what a person did (resolve, cancel) or what time did
(a deadline, a pause before the next result). A session that follows it ends where the recorded run ended.
"""

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from firefly_weave.compiler.api import import_artifact
from firefly_weave.contracts.instance_keys import InvalidInstanceKey
from firefly_weave.contracts.transactions import split_phase
from firefly_weave.operations.debug.models import DebugCommand, DebugCreate, ScriptedError
from firefly_weave.operations.debug.simulator import Simulator
from firefly_weave.operations.debug.transactions import phase_target
from firefly_weave.runtime.kernel import prepare, validate
from firefly_weave.runtime.models import RunState

CORPUS = json.loads(Path("tests/fixtures/golden/transactions.json").read_text(encoding="utf-8"))
# The recorder stamps each event one second after the one before it; a longer gap is time that passed between them.
RECORDED_STEP = timedelta(seconds=1)
REPLAYABLE = (
    "golden-move/completed",
    "golden-move/compensated",
    "golden-move/cancelled",
    "golden-move/undo-stuck-then-retried",
    "golden-move/undo-stuck-then-reconciled-as-compensated",
    "golden-move/pivot-unknown-then-it-did-not-happen",
    "golden-move/cancel-while-deciding-then-it-went-through",
    "golden-move/cancel-while-deciding-then-it-definitely-failed",
    "golden-move/force-ended",
    "golden-move/cancel-skipping-undo",
    "golden-move/mixed-definite-and-unknown-failures",
    "golden-move-short-hold/hold-insufficient-before-the-pivot",
    "golden-move-then-notify/pivot-unknown-then-it-happened",
    "golden-move-read-later/confirm-stuck-then-reconciled-as-completed",
    "golden-move-in-parallel/settle-timeout",
)


class Move(NamedTuple):
    """What happens to the session once it has accepted ``at`` events, as the recorded run had before it."""

    at: int
    kind: str  # resolve, cancel, until (a deadline's key) or later (seconds)
    value: Any


def is_phase(key: str | None) -> bool:
    """Whether ``key`` names a confirm, cancel or undo; a transaction's deadline keys (``move#hold``) never do."""
    if not key or "#" not in key:
        return False
    try:
        return split_phase(key) is not None
    except InvalidInstanceKey:
        return False


def scenario(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    source_name, _, scenario_name = name.partition("/")
    source = next(item for item in CORPUS["sources"] if item["name"] == source_name)
    return source, next(item for item in source["scenarios"] if item["name"] == scenario_name)


def accepts_empty(prepared: Any, key: str) -> bool:
    target = phase_target(prepared, key)
    if target is None:
        return False
    try:
        validate(prepared.ir, target.output_schema, {})
    except ValueError:
        return False
    return True


def session_from_history(source: dict[str, Any], recorded: dict[str, Any]) -> tuple[DebugCreate, list[Move]]:
    """The session request and the moves that replay ``recorded``, a run of ``source``'s workflow."""
    prepared = prepare(import_artifact(source["artifact"]))
    stream = recorded["stream"]
    mocks: dict[str, Any] = {}
    errors: dict[str, ScriptedError] = {}
    moves: list[Move] = []
    dispatched: set[str] = set()
    answered: set[str] = set()
    stamps = [datetime.fromisoformat(item["event"]["timestamp"]) for item in stream]
    for index, item in enumerate(stream):
        event = item["event"]
        kind, data = event["type"], event["data"]
        key = data.get("node_id")
        gap = stamps[index] - stamps[index - 1] if index else timedelta(0)
        if gap > RECORDED_STEP and kind != "wait_elapsed":
            # Time passed before this result; a deadline's own time comes from the deadline instead.
            moves.append(Move(index, "later", int((gap - RECORDED_STEP).total_seconds())))
        for command in item["transition"]["commands"]:
            if command["kind"] in ("task", "call"):
                dispatched.add(command["node_id"])
        if kind in ("task_completed", "call_completed"):
            answered.add(key)
            retried = data.get("generation", 1) > 1 or f"node:{key}" in errors
            if not retried:
                mocks[f"node:{key}"] = data["output"]
            elif not (data["output"] == {} and accepts_empty(prepared, key)):
                # The first attempt's failure holds node:<key>; the retried answer comes from the phase's action.
                target = phase_target(prepared, key)
                assert target is not None, key
                mocks[f"action:{target.reference}"] = data["output"]
        elif kind == "step_failed":
            answered.add(key)
            failure: dict[str, Any] = {"code": data["code"], "outcome": data["outcome"], "attempts": data["attempts"]}
            if not is_phase(key):
                failure["effect"] = data["effect"]
            errors[f"node:{key}"] = ScriptedError(**failure)
        elif kind == "incident_resolved":
            command = {"kind": "resolve", "incident_key": data["incident_key"], "resolution": data["kind"]}
            moves.append(Move(index, "resolve", DebugCommand(**command, payload=data.get("output"))))
        elif kind == "cancelled":
            moves.append(Move(index, "cancel", DebugCommand(kind="cancel", skip_undo=data.get("skip_undo", False))))
        elif kind == "wait_elapsed":
            moves.append(Move(index, "until", key))
    request = DebugCreate(
        artifact=source["artifact"],
        mocks=mocks,
        errors=errors,
        input=recorded["input"],
        now=stamps[0],
        pending=sorted(f"node:{key}" for key in dispatched - answered),
    )
    return request, moves


def replay(request: DebugCreate, moves: list[Move]) -> Simulator:
    """Run the session, making each move right after the event boundary that brings it to the recorded count."""
    simulator = Simulator(
        import_artifact(request.artifact),
        mocks=request.mocks,
        input=request.input,
        now=request.now,
        errors=request.errors,
        pending=request.pending,
    )
    for move in moves:
        for _ in range(200):
            if len(simulator.data.events) >= move.at:
                break
            simulator.next()
        assert len(simulator.data.events) == move.at, (move, [e.type for e in simulator.data.events])
        if move.kind in ("resolve", "cancel"):
            simulator.command(move.value)
        elif move.kind == "until":
            deadline = next(d for d in simulator.data.deadlines if d.node_id == move.value)
            simulator.advance_time(math.ceil((deadline.deadline - simulator.data.now).total_seconds()))
        else:
            simulator.advance_time(move.value)
    simulator.continue_until_breakpoint()
    return simulator


def ending(state: RunState | dict[str, Any]) -> dict[str, Any]:
    """A run's end as both sides record it: its status and each transaction's outcome, without values or times."""
    dumped = state.model_dump(mode="json") if isinstance(state, RunState) else state
    return {
        "status": dumped["status"],
        "transactions": {
            key: {
                "status": record["status"],
                "cause": record.get("cause"),
                "reconciled_as": record.get("reconciled_as"),
                "participants": [(p["key"], p["status"], p.get("effect")) for p in record["participants"]],
            }
            for key, record in dumped["transactions"].items()
        },
    }


@pytest.mark.parametrize("name", REPLAYABLE)
def test_recorded_histories_end_the_same_way_in_a_session(name):
    source, recorded = scenario(name)
    simulator = replay(*session_from_history(source, recorded))
    assert not simulator.data.diagnostics, simulator.data.diagnostics
    data = simulator.data
    state = data.cursor.result.state if data.cursor else data.state
    assert ending(state) == ending(recorded["stream"][-1]["transition"]["state"])
    phases = [e.data.get("node_id") for e in data.events if is_phase(e.data.get("node_id"))]
    assert phases == [
        i["event"]["data"]["node_id"] for i in recorded["stream"] if is_phase(i["event"]["data"].get("node_id"))
    ]


def test_the_short_hold_history_replays_through_the_time_between_two_results():
    _, moves = session_from_history(*scenario("golden-move-short-hold/hold-insufficient-before-the-pivot"))
    assert moves == [Move(2, "later", 21)]


def test_histories_a_session_cant_replay_are_named():
    # A new recorded history fails here until it is replayed above or named below with the reason.
    every = {f"{source['name']}/{item['name']}" for source in CORPUS["sources"] for item in source["scenarios"]}
    assert (every - set(REPLAYABLE), every & set(NOT_REPLAYABLE)) == (set(NOT_REPLAYABLE), set(NOT_REPLAYABLE))


# What a session can't show of each history it doesn't replay.
NOT_REPLAYABLE = {
    "golden-move/rollback-while-paused": "a pause",
    "golden-move-short-hold/hold-expired": "a result that arrives after the hold deadline",
    "golden-move-then-wait/rollback-while-another-incident-holds-the-run": (
        "an interim failure incident before the final failure"
    ),
    "golden-move-elsewhere/cancelled-with-a-called-step-undone": (
        "a child the platform cancelled; a session's call mock answers at once, so the participant's effect differs"
    ),
}
