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

"""Simulated transactions keep the undo rule in every end state, and a session's events alone give its state."""

import sys
from pathlib import Path

from hypothesis import event, example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "debug"))

from simulation_support import NOW  # noqa: E402
from transaction_support import INPUT, add, compiled, hold, notify, submit, transaction  # noqa: E402

from firefly_weave.contracts.instance_keys import InvalidInstanceKey  # noqa: E402
from firefly_weave.contracts.transactions import split_phase  # noqa: E402
from firefly_weave.operations.debug.models import ScriptedError  # noqa: E402
from firefly_weave.operations.debug.simulator import Simulator  # noqa: E402
from firefly_weave.runtime.kernel import transition  # noqa: E402
from firefly_weave.runtime.models import RunState  # noqa: E402

FLOW = compiled([transaction([hold(), add(), add("add2"), submit(), notify()])])
FORWARD = ("hold", "add", "add2", "submit", "notify")
# The participants that can be undone, in registration order, and the phase that undoes each.
UNDO = {"hold": "hold#cancel", "add": "add#undo", "add2": "add2#undo"}
PHASES = ("hold#confirm", *UNDO.values())
OUTPUTS = {"hold": {"holdId": "h-1"}, "add": {"id": "a-1"}, "add2": {"id": "a-2"}, "submit": {"orderId": "o-1"}}
FAILURES = {
    "fails_definitely": ScriptedError(code="REFUSED", effect="none"),
    "fails_unknown": ScriptedError(code="UNAVAILABLE", outcome="unknown"),
}
ROLLBACK_SIDE = {"WV-TXN-UNDO-STUCK", "WV-TXN-CANCEL-STUCK", "WV-TXN-SETTLE-TIMEOUT"}
COMMIT_SIDE = {"WV-TXN-CONFIRM-STUCK", "WV-TXN-AFTER-PIVOT-STUCK"}

# "done" is drawn more often than each other answer, so the pivot and the steps after it are reached too.
answer = st.one_of(st.just("done"), st.sampled_from(("done", "fails_definitely", "fails_unknown", "pending")))
steps = st.fixed_dictionaries({key: answer for key in FORWARD})
phases = st.fixed_dictionaries({key: st.sampled_from(("done", "stuck")) for key in PHASES})
DONE = dict.fromkeys(FORWARD, "done")
ANSWERED = dict.fromkeys(PHASES, "done")


def is_phase(key: object) -> bool:
    """A phase event's key is a confirm, cancel or undo; a transaction's deadline keys (``move#hold``) never are."""
    if not isinstance(key, str) or "#" not in key:
        return False
    try:
        return split_phase(key) is not None
    except InvalidInstanceKey:
        return False


def session(forward: dict[str, str], answers: dict[str, str]) -> Simulator:
    """Each step and phase answers as drawn; phases that are done take the empty result their actions accept."""
    mocks = {f"node:{key}": OUTPUTS.get(key, {}) for key, drawn in forward.items() if drawn == "done"}
    errors = {f"node:{key}": FAILURES[drawn] for key, drawn in forward.items() if drawn in FAILURES}
    errors |= {f"node:{key}": ScriptedError(code="UNAVAILABLE") for key, drawn in answers.items() if drawn == "stuck"}
    pending = [f"node:{key}" for key, drawn in forward.items() if drawn == "pending"]
    simulator = Simulator(FLOW, mocks=mocks, input=INPUT, now=NOW, errors=errors, pending=pending)
    simulator.continue_until_breakpoint()
    simulator.advance_time(3600)
    simulator.continue_until_breakpoint()
    return simulator


def expected_rollback(forward: dict[str, str]) -> list[str]:
    """The undo phases a rollback runs, worked out from the draws alone: every participant before the first step
    that didn't succeed, plus that step when it may have applied something, in reverse registration order."""
    first = next(key for key in FORWARD if forward[key] != "done")
    before = list(FORWARD[: FORWARD.index(first)])
    if forward[first] == "fails_unknown":
        before.append(first)
    return [UNDO[key] for key in reversed(before) if key in UNDO]


@settings(max_examples=60, deadline=None)
@given(steps, phases)
# The end states random draws reach least often, every run.
@example({**DONE, "add2": "fails_unknown"}, ANSWERED)  # compensated
@example({**DONE, "submit": "fails_definitely"}, ANSWERED)  # compensated, every participant undone
@example({**DONE, "submit": "fails_unknown"}, ANSWERED)  # the pivot's outcome unknown
@example(DONE, {**ANSWERED, "hold#confirm": "stuck"})  # a confirm stuck
@example({**DONE, "submit": "pending"}, ANSWERED)  # deciding
def test_every_end_state_keeps_its_undo_rule(forward, answers):
    simulator = session(forward, answers)
    state = simulator.data.state
    [record] = state.transactions.values()
    events = [e.data["node_id"] for e in simulator.data.events if is_phase(e.data.get("node_id"))]
    confirms = [key for key in events if key.endswith("#confirm")]
    undos = [key for key in events if not key.endswith("#confirm")]
    event(record.status if record.status != "in_doubt" else next(iter(state.incidents.values())).code)
    # In no end state is a participant both confirmed and undone.
    assert not (confirms and undos), events
    if record.status == "completed":
        assert (confirms, undos) == (["hold#confirm"], [])
    elif record.status in ("compensated", "cancelled"):
        assert (confirms, undos) == ([], expected_rollback(forward))
    elif record.status == "in_doubt":
        [incident] = state.incidents.values()
        if incident.code in ROLLBACK_SIDE:
            expected = expected_rollback(forward)
            assert confirms == [] and undos == expected[: len(undos)], events
            if incident.code != "WV-TXN-SETTLE-TIMEOUT":
                # Only a phase scripted to fail gets stuck.
                assert undos[-1] == incident.node_id and answers[incident.node_id] == "stuck"
        elif incident.code == "WV-TXN-PIVOT-UNKNOWN":
            assert events == [] and forward["submit"] == "fails_unknown"
        else:
            assert incident.code in COMMIT_SIDE, incident.code
            assert undos == [] and len(confirms) <= 1
            if incident.code == "WV-TXN-CONFIRM-STUCK":
                assert answers["hold#confirm"] == "stuck"
            else:
                assert forward["notify"] in FAILURES
    elif record.status == "deciding":
        # The pivot never answers; the hold running out is the abort cause it waits with.
        assert events == [] and record.pending_abort is not None and record.pending_abort.code == "WV-TXN-HOLD-EXPIRED"
    elif record.status in ("open", "compensating"):
        assert undos == [] and confirms == []
    elif record.status == "committing":
        assert (confirms, undos) == (["hold#confirm"], []) and forward["notify"] == "pending"
    else:
        raise AssertionError(f"unexpected end state {record.status}")


@settings(max_examples=60, deadline=None)
@given(steps, phases)
def test_a_sessions_events_replay_to_its_state(forward, answers):
    simulator = session(forward, answers)
    state = RunState(input=INPUT)
    for recorded in simulator.data.events:
        state = transition(state, recorded, FLOW).state
    assert state == simulator.data.state
