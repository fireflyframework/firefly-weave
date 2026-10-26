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

"""Properties of rollback, commit and the scoped barrier over generated bodies, failures, pauses and cancels."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from transaction_support import Run, add, compiled, hold, notify, submit, transaction

from firefly_weave.contracts.transactions import split_phase
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, TaskIntent

OUTPUTS = {"hold": {"holdId": "h"}, "add": {"id": "a"}, "submit": {"orderId": "o"}, "notify": {}}
TERMINAL = {"completed", "compensated", "cancelled"}


def body(draw) -> list[dict]:
    steps = []
    for index in range(draw(st.integers(1, 4))):
        steps.append(hold(f"hold{index}") if draw(st.booleans()) else add(f"add{index}"))
    if draw(st.booleans()):
        steps.append(submit())
        if draw(st.booleans()):
            steps.append(notify())
    return steps


def output_of(key: str) -> dict:
    if split_phase(key) is not None:
        return {}
    return OUTPUTS[key.rstrip("0123456789")]


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_every_rollback_undoes_in_reverse_once_and_the_barrier_holds_back_only_forward_steps(data):
    steps = body(data.draw)
    run = Run(compiled([transaction(steps)]))
    run.start()
    issued: list[TaskIntent] = []
    in_doubt_seen = False
    for _ in range(80):
        if run.state.status in {"succeeded", "failed", "cancelled", "timed_out"}:
            break
        # A step whose result a barrier already holds back gets no second result: workers report each attempt once.
        held = {event.data.get("node_id") for event in run.state.deferred_results}
        flying = [
            key for key in run.state.active if ("#" not in key or split_phase(key) is not None) and key not in held
        ]
        choices = ["pause" if not run.state.manual_paused else "resume"]
        if flying:
            choices += ["complete", "complete", "fail"]
        if run.state.ending is None:
            choices.append("cancel")
        if run.txn().hold_by is not None:
            choices.append("hold")
        choice = data.draw(st.sampled_from(choices))
        before = run.state.manual_paused
        if choice == "pause":
            result = run.send("paused", reason="p", control_revision=run.state.control_revision + 1)
        elif choice == "resume":
            result = run.send("resumed", reason="r", control_revision=run.state.control_revision + 1)
        elif choice == "cancel":
            result = run.send("cancelled", reason="c")
        elif choice == "hold":
            result = run.fire("move#hold")
        else:
            key = data.draw(st.sampled_from(sorted(flying)))
            if choice == "complete":
                result = run.done(key, output_of(key))
            else:
                result = run.failed(key, "UNAVAILABLE", effect=data.draw(st.sampled_from(["none", "unknown"])))
        new = [command for command in result.commands if isinstance(command, TaskIntent)]
        if before and run.state.manual_paused and choice not in {"pause", "resume"}:
            # While paused, only settlement work is dispatched.
            assert all(task.settlement for task in new), [task.node_id for task in new]
        issued.extend(new)
        # Only a transaction's own codes ever hold this run: every failure inside it is the transaction's to handle.
        assert all(item.code.startswith("WV-TXN-") for item in run.state.incidents.values()), run.state.incidents
        if in_doubt_seen:
            # Nothing but a resolution moves a transaction out of doubt.
            assert run.txn().status == "in_doubt"
        in_doubt_seen = run.txn().status == "in_doubt"
    # Liveness: once results keep arriving, every transaction not in doubt reaches a terminal state.
    if run.state.manual_paused and run.state.status not in {"succeeded", "failed", "cancelled", "timed_out"}:
        run.send("resumed", reason="r", control_revision=run.state.control_revision + 1)
    for _ in range(40):
        flying = sorted(key for key in run.state.active if "#" not in key or split_phase(key) is not None)
        if not flying or run.txn().status == "in_doubt":
            break
        issued.extend(c for c in run.done(flying[0], output_of(flying[0])).commands if isinstance(c, TaskIntent))
    assert run.txn().status in TERMINAL | {"in_doubt"}, run.txn().status
    keys = [task.node_id for task in issued]
    assert len(keys) == len(set(keys)), "every phase is issued once"
    confirmed = {split_phase(k)[0] for k in keys if k.endswith("#confirm")}
    undone = [split_phase(k)[0] for k in keys if k.endswith(("#cancel", "#undo"))]
    assert not confirmed & set(undone), "never both directions for one participant"
    registered = [item.key for item in run.txn().participants]
    assert undone == sorted(undone, key=registered.index, reverse=True), "undo in reverse registration order"
    transaction_state = run.txn()
    if transaction_state.status == "compensated" or transaction_state.status == "cancelled":
        must = [item.key for item in transaction_state.participants if item.status in {"undone", "cancelled"}]
        assert sorted(must) == sorted(undone)
    # Replaying the same events from the start reproduces every transition byte for byte.
    state = RunState(input=run.state.input)
    for event, recorded in zip(run.events, run.transitions, strict=True):
        replayed = transition(state, event, run.flow)
        assert replayed.model_dump_json() == recorded.model_dump_json()
        state = replayed.state
