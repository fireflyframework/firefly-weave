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

"""Properties of rollback, commit and the scoped barrier over generated bodies, failures, pauses and cancels, and of
what a person can decide about every incident of a transaction."""

from functools import cache
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from transaction_support import Run, add, compiled, hold, lookup, notify, parallel, resolve, stored, submit, transaction

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.contracts.transactions import IN_DOUBT_CODES, TERMINAL_TRANSACTION_STATES, split_phase
from firefly_weave.operations.exports import transition_digest
from firefly_weave.runtime import kernel
from firefly_weave.runtime.kernel import PreparedWorkflow, prepare, transition, unsaid_steps
from firefly_weave.runtime.models import RunState, RuntimeEvent, TaskIntent, Transition
from firefly_weave.runtime.transactions import deadline_key, doubted_steps, incident_transaction, open_doubts

OUTPUTS = {
    "hold": {"holdId": "h"},
    "add": {"id": "a"},
    "submit": {"orderId": "o"},
    "notify": {},
    "read": {"count": 1},
    "beside": {"count": 1},
}
TERMINAL = {"completed", "compensated", "cancelled"}
ENDED = {"succeeded", "failed", "cancelled", "timed_out"}


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


@pytest.mark.parametrize("rules", [1, 2])
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_every_rollback_undoes_in_reverse_once_and_the_barrier_holds_back_only_forward_steps(rules, data):
    steps = body(data.draw)
    run = Run(compiled([transaction(steps)]))
    run.start(rules)
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


class Checked(Run):
    """A run of the second rules that checks, after every event, what has to hold of its incidents and results, and
    that the same transition comes from the state as the database gives it back."""

    def __init__(self, flow) -> None:
        super().__init__(flow)
        facts = prepare(flow).transactions
        assert facts is not None
        self.facts = facts
        # The attempt each step or phase is at.
        self.attempt: dict[str, int] = {}

    def send(self, kind: str, /, **data: Any) -> Transition:
        before = self.state
        result = super().send(kind, **data)
        event = self.events[-1]
        again = transition(stored(before), event, self.flow)
        assert transition_digest(again) == transition_digest(result)
        if self.state.status not in ENDED:
            self.doubts_and_names(before)
            self.incidents_are_somebodys()
            if self.state.incidents != before.incidents:
                self.every_doubt_can_be_ended()
            self.no_result_is_lost(before, event)
        return result

    def doubts_and_names(self, before: RunState) -> None:
        for key, item in self.state.transactions.items():
            doubts = open_doubts(self.facts, self.state, key)
            assert (item.status == "in_doubt") == bool(doubts), (key, item.status, doubts)
            if not doubts:
                assert item.incident is None, (key, item.incident)
                continue
            was = before.transactions[key].incident if key in before.transactions else None
            if was is None:
                assert item.incident in doubts
            else:
                assert item.incident == (was if was in doubts else min(doubts))

    def incidents_are_somebodys(self) -> None:
        state = self.state
        assert state.status != "suspended" or state.incidents or state.manual_paused
        for name, item in state.incidents.items():
            owner = incident_transaction(self.facts, item.node_id, item.code)
            assert owner is None or state.transactions[owner].status not in TERMINAL_TRANSACTION_STATES, name
            assert item.code in IN_DOUBT_CODES or (item.code == "WV-TASK-FAILED" and item.node_id in state.active), name

    def every_doubt_can_be_ended(self) -> None:
        for name, item in self.state.incidents.items():
            if item.code in IN_DOUBT_CODES:
                tried = Run(self.flow)
                tried.state, tried.now = self.state, self.now
                resolve(tried, name, "terminate")
                owner = incident_transaction(self.facts, item.node_id, item.code)
                assert owner is not None and name not in tried.state.incidents
                assert tried.txn(owner).status == "force_ended"

    def no_result_is_lost(self, before: RunState, event: RuntimeEvent) -> None:
        waiting = [item.data["node_id"] for item in self.state.deferred_results]
        assert len(waiting) == len(set(waiting))
        if event.type in {"task_completed", "step_failed"}:
            # Applied (the run no longer waits for it), or deferred.
            assert event.data["node_id"] not in self.state.active or event.data["node_id"] in waiting
        for key, held in self.state.transactions.items():
            if held.decided_at_sequence is not None and held.status not in TERMINAL_TRANSACTION_STATES:
                # Nothing can be undone any more: a step that failed for good waits for a person.
                failed = {step.key for step in held.participants if step.status in {"failed", "unknown"}}
                assert failed <= doubted_steps(self.facts, self.state, key), (key, failed)
        for item in before.deferred_results:
            assert item in self.state.deferred_results or item.data["node_id"] not in self.state.active

    def flying(self) -> list[str]:
        """The steps and phases a worker runs now: no result of theirs waits, and recovery has no failure to take."""
        held = {event.data.get("node_id") for event in self.state.deferred_results}
        failed = {item.node_id for item in self.state.incidents.values()}
        return sorted(key for key in self.state.active if key not in held | failed)

    def failing(self) -> list[str]:
        return sorted(item.node_id for item in self.state.incidents.values() if item.code == "WV-TASK-FAILED")

    def generation(self, key: str) -> int:
        return self.attempt.get(key, 1)

    def finish(self, key: str) -> None:
        self.done(key, output_of(key), generation=self.generation(key))

    def fail_for_good(self, key: str, effect: str) -> None:
        self.failed(key, "UNAVAILABLE", effect=effect, generation=self.generation(key))

    def fail_attempt(self, key: str, effect: str) -> None:
        generation = self.generation(key)
        self.send("task_failed", node_id=key, generation=generation, output={"code": "UNAVAILABLE"}, effect=effect)

    def recover(self, key: str, effect: str) -> None:
        generation = self.generation(key)
        when = self.now.isoformat()
        self.send("recovery_scheduled", node_id=key, generation=generation, next_attempt_at=when, effect=effect)
        self.attempt[key] = generation + 1

    def decide(self, name: str, kind: str, ending: str = "completed") -> None:
        """A person resolves an incident, with the record or the output it needs."""
        item = self.state.incidents[name]
        owner = incident_transaction(self.facts, item.node_id, item.code)
        if kind != "accept_reconciled_result":
            resolve(self, name, kind)
        elif item.code in IN_DOUBT_CODES and item.code != "WV-TXN-PIVOT-UNKNOWN":
            assert owner is not None
            resolve(self, name, kind, self.said(owner, ending))
        else:
            resolve(self, name, kind, output_of(item.node_id))
        if kind == "retry_safe" and item.node_id in self.state.active:
            self.attempt[item.node_id] = self.generation(item.node_id) + 1

    def said(self, key: str, ending: str) -> dict[str, Any]:
        """A record that names every step a person must say something about, and no other."""
        empty: dict[str, Any] = {"as": ending, "participants": []}
        if ending == "completed":
            empty["blockOutput"] = "moved"
        held = self.txn(key)
        modes = {item.key: item.mode for item in held.participants}
        words = {"reserve": "confirmed"} if ending == "completed" else {}
        other = "done" if ending == "completed" else "undone"
        steps = [
            {"instance_key": name, "outcome": words.get(modes[name], other)}
            for name in unsaid_steps(held, empty, doubted_steps(self.facts, self.state, key))
        ]
        return {**empty, "participants": steps}


# The bodies a doubt can come from: how many reservations, a read beside a step that changes data, a read beside the
# step that can't be undone, how many steps run side by side after it, and a second transaction beside the first.
BODIES = (
    (2, False, False, 0, False),
    (3, False, False, 2, False),
    (2, False, True, 0, True),
    (2, True, False, 3, True),
    (3, True, True, 1, True),
    (2, False, True, 2, False),
    (0, False, False, 3, False),
    (1, True, False, 0, False),
    (0, True, True, 3, False),
    (1, False, True, 1, True),
)


@cache
def doubtful(
    holds: int, read: bool, beside: bool, after: int, second: bool
) -> tuple[CompiledArtifact, PreparedWorkflow]:
    """The workflow of a body, and what the kernel prepares of it: once, as a simulation does, not per transition."""
    steps = [hold(f"hold{index}") for index in range(holds)]
    if read:
        steps.append(parallel(add(), lookup("read"), identifier="pair"))
    steps.append(parallel(submit(), lookup("beside"), identifier="decision") if beside else submit())
    notices = [notify(f"notify{index}") for index in range(after)]
    steps.extend(notices if after < 2 else [parallel(*notices, identifier="notices")])
    block = transaction(steps)
    # A second transaction, whose key the database gives back before the first one's.
    other = transaction([parallel(hold("hold9"), lookup("read9"), identifier="pair9")], identifier="aa")
    flow = compiled([parallel(block, other, identifier="fork")] if second else [block])
    return flow, prepare(flow)


DECISIONS = ("retry_safe", "accept_reconciled_result", "accept_reconciled_failure", "terminate")


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_every_incident_of_a_transaction_can_be_resolved(data):
    flow, prepared = doubtful(*data.draw(st.sampled_from(BODIES)))
    with patch.object(kernel, "prepare", lambda artifact: prepared):
        run = Checked(flow)
        run.start(2)
        advance(run, data.draw(st.sampled_from(["", "add", "add", "submit", *["#confirm"] * 3, *["notify"] * 3])))
        for _ in range(data.draw(st.sampled_from([0, 1, 2, 2, 3, 3]))):
            # Several things fail before anybody decides anything.
            if run.state.status not in ENDED and run.flying():
                fail(run, data.draw)
        # Half of the runs only see their steps end and people decide, so several incidents meet more often.
        quiet = data.draw(st.booleans())
        for _ in range(data.draw(st.sampled_from([4, 8, 16]))):
            if run.state.status in ENDED:
                break
            act(run, data.draw, quiet)
        settle(run, data.draw(st.sampled_from(["retry_safe", "retry_safe", "completed", "compensated", "terminate"])))
        assert run.state.status in ENDED, (run.state.status, run.state.active, run.state.incidents)
        # Replaying the same events from the start reproduces every transition byte for byte.
        state = RunState(input=run.state.input)
        for event, recorded in zip(run.events, run.transitions, strict=True):
            replayed = transition(state, event, run.flow)
            assert replayed.model_dump_json() == recorded.model_dump_json()
            state = replayed.state


def advance(run: Checked, until: str) -> None:
    """The run goes forward with nothing failing until a step or phase named so is in flight, or as far as it can;
    the reads that run beside its steps stay in flight."""
    while until and not any(until in key for key in run.flying()):
        ahead = [key for key in run.flying() if key not in {"beside", "read", "read9", "hold9"}]
        if not ahead:
            return
        run.finish(ahead[0])


def fail(run: Checked, draw) -> None:
    """A step or phase in flight fails: for good, or an attempt that recovery has yet to decide about."""
    key, effect = draw(st.sampled_from(run.flying())), draw(st.sampled_from(["none", "unknown"]))
    if draw(st.booleans()):
        run.fail_for_good(key, effect)
    else:
        run.fail_attempt(key, effect)


def act(run: Checked, draw, quiet: bool) -> None:
    """One thing that can happen to a run in flight; in a ``quiet`` one, nobody pauses it and no time passes."""
    flying, failing = run.flying(), run.failing()
    if not flying and not run.state.incidents:
        quiet = False
    choices = [] if quiet else ["resume" if run.state.manual_paused else "pause"]
    if flying:
        choices += ["complete"] * 3 + ["fail"] * 5
    if failing:
        choices += ["recover", *["fail what failed for good"] * 3]
    if run.state.incidents:
        choices.append("decide")
    # What stops steps comes more often while recovery still has a failure to decide about.
    often = 3 if failing else 1
    if run.state.ending is None and not quiet:
        choices += ["cancel"] * often
    for key, item in () if quiet else run.state.transactions.items():
        if item.hold_by is not None and item.status in {"open", "deciding"}:
            choices += [f"{key}#hold"] * often
        if item.settle_by is not None and item.status == "compensating":
            choices.append(deadline_key(key, "settle", item.generation))
    choice = draw(st.sampled_from(choices))
    effect = draw(st.sampled_from(["none", "unknown"])) if "fail" in choice or choice == "recover" else "unknown"
    revision = run.state.control_revision + 1
    if choice == "pause":
        run.send("paused", reason="p", control_revision=revision)
    elif choice == "resume":
        run.send("resumed", reason="r", control_revision=revision)
    elif choice == "cancel":
        run.send("cancelled", reason="c")
    elif choice == "complete":
        run.finish(draw(st.sampled_from(flying)))
    elif choice == "fail":
        fail(run, draw)
    elif choice == "recover":
        run.recover(draw(st.sampled_from(failing)), effect)
    elif choice == "fail what failed for good":
        run.fail_for_good(draw(st.sampled_from(failing)), effect)
    elif choice == "decide":
        name = draw(st.sampled_from(sorted(run.state.incidents)))
        doubted = run.state.incidents[name].code in IN_DOUBT_CODES
        # Only the step that can't be undone, or one with an onError setting, can be said not to have happened.
        kind = draw(st.sampled_from(DECISIONS if doubted else [k for k in DECISIONS if not k.endswith("failure")]))
        run.decide(name, kind, draw(st.sampled_from(["completed", "compensated"])))
    else:
        run.fire(choice)


def settle(run: Checked, decision: str) -> None:
    """The run goes on until it ends: each incident in doubt takes the same decision, and every step gets its result."""
    for _ in range(80):
        if run.state.status in ENDED:
            return
        doubts = sorted(name for name, item in run.state.incidents.items() if item.code in IN_DOUBT_CODES)
        if run.state.manual_paused:
            run.send("resumed", reason="r", control_revision=run.state.control_revision + 1)
        elif doubts and decision == "terminate":
            run.decide(doubts[0], "terminate")
        elif doubts and run.state.incidents[doubts[0]].code == "WV-TXN-PIVOT-UNKNOWN":
            happened = decision != "compensated"
            run.decide(doubts[0], "accept_reconciled_result" if happened else "accept_reconciled_failure")
        elif doubts and decision == "retry_safe":
            run.decide(doubts[0], "retry_safe")
        elif doubts:
            run.decide(doubts[0], "accept_reconciled_result", decision)
        elif run.failing():
            run.recover(run.failing()[0], "unknown")
        elif run.flying():
            run.finish(run.flying()[0])
        else:
            return
