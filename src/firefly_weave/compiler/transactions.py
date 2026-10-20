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


"""Publish-time rules for transactions: which orderings of steps Weave can always finish or undo.

``check_transaction`` reads one transaction body whose catalog facts are already resolved (side effects, time limits,
hold times), so the compiler and the IR validator share one implementation and neither reads a catalog here. It
walks every execution path: decision paths one at a time, parallel branches together, and loop bodies as repeated.

Vocabulary used below, never shown in Studio: a step that can't be undone is *irreversible*; the first one on a path
is its *pivot*, the point after which nothing can be undone.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Final, Literal

from firefly_weave.compiler.language_support import path_order
from firefly_weave.contracts.definitions import SideEffect
from firefly_weave.contracts.transactions import HOLD_MARGIN_SECONDS, MAX_TRANSACTION_PARTICIPANTS, UndoMode, UndoPhase
from firefly_weave.contracts.values import JsonObject, JsonValue

type Severity = Literal["error", "warning", "info"]

RULE_MESSAGES: Final[dict[str, str]] = {
    "TXN_ORDER": (
        "{step} can be undone but runs after {pivot}, which can't. "
        "Steps that can't be undone must come last in a transaction."
    ),
    "TXN_PIVOT_PLACEMENT": (
        "Only one step that can't be undone can decide the transaction. Move {step} out of the {place}."
    ),
    "TXN_AFTER_PIVOT_UNSAFE": (
        "After {pivot} nothing can be undone, so {step} must be safe to retry. "
        "Move it after the transaction, or use an action that is safe to retry."
    ),
    "TXN_AFTER_PIVOT_PURE": (
        "If {step} fails after {pivot}, nothing can be undone and the transaction is left in doubt."
    ),
    "TXN_AFTER_PIVOT_WAIT": (
        "{step} waits after {pivot}, which can't be undone; if nobody answers in time, the transaction is left in "
        "doubt."
    ),
    "TXN_UNDO_SIDE_EFFECT": "Weave retries undo steps until they succeed, so {action} must be safe to retry.",
    "TXN_RESERVE_PAIR": "A step that reserves needs both confirm and cancel.",
    "TXN_HOLD_RESERVE": "holdSeconds applies only to a step that reserves with confirm and cancel.",
    "TXN_UNDO_FORMS": (
        "Choose one way to undo a step: reserve then confirm, an action that reverses it, or can't be undone."
    ),
    "TXN_CANCEL_OUTPUT": (
        "cancel must work even if the reservation didn't go through, so it can't read {step}'s output; "
        "identify the reservation by its inputs or /participant/operationKey."
    ),
    "TXN_OUTSIDE": (
        "confirm, cancel, compensate, irreversible and holdSeconds apply only to steps inside a transaction."
    ),
    "TXN_NESTED": "A transaction can't contain another transaction; call a workflow that has its own transaction.",
    "TXN_DETACH": "A call inside a transaction must wait for its result; a call that runs on its own can't be undone.",
    "TXN_AGENT": "An AI agent can't run inside a transaction, because its tool calls aren't known before it runs.",
    "TXN_UNDECLARED": (
        "{step} changes data. Choose how it's undone: reserve then confirm, an action that reverses it, "
        "or can't be undone."
    ),
    "TXN_HOLD_WAIT": "{step} can wait up to {wait}, but reservations are held for {hold}.",
    "TXN_HOLD_WAIT_UNBOUNDED": "{step} can wait without a time limit, but reservations are held for {hold}.",
    "TXN_SIZE": (
        "This transaction can include up to {count:,} steps that change data; the limit is {limit:,}. "
        "Lower maxItems on a loop inside it."
    ),
    "TXN_NOTHING_TO_UNDO": "Nothing in this transaction can be undone.",
    "TXN_CALL_TIMEOUT": (
        "Called workflows inside a transaction must have a time limit. Set timeoutSeconds on {name} in a new version."
    ),
    "TXN_HOLD_PIVOT": (
        "Reservations are held for {hold}, but {pivot} may take up to {timeout}. Hold them for at least {needed}."
    ),
    "TXN_SCOPE": "/participant and /transaction are available only in a step's confirm, cancel and compensate.",
    "ACTION_DEFINITE_TRANSIENT": "{code} is a temporary failure, so it can't mean that nothing was applied.",
}
# The code each message key reports; two message keys share a code.
RULE_CODES: Final[dict[str, str]] = {
    key: {"TXN_HOLD_RESERVE": "TXN_RESERVE_PAIR", "TXN_HOLD_WAIT_UNBOUNDED": "TXN_HOLD_WAIT"}.get(key, key)
    for key in RULE_MESSAGES
}
_SAFE_UNDO: Final = frozenset({"idempotent", "idempotency_key"})
_WAITS: Final = frozenset({"wait", "signal", "humanTask"})
# Steps that change nothing but can still fail; read-only actions are "action" here, since they never participate.
_PURE: Final = frozenset({"transform", "decisionTable", "llm", "action"})


def duration_text(seconds: int) -> str:
    """Seconds as words: ``930`` is "15 minutes 30 seconds", ``172800`` is "2 days"."""
    parts = []
    for size, name in ((86_400, "day"), (3_600, "hour"), (60, "minute"), (1, "second")):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count:,} {name}{'' if count == 1 else 's'}")
    return " ".join(parts) or "0 seconds"


@dataclass(frozen=True)
class UndoFacts:
    """One confirm, cancel or compensate of a step, with what the catalog says about it (``None`` when unknown)."""

    phase: UndoPhase
    path: str
    kind: Literal["action", "callWorkflow"]
    reference: str
    side_effect: SideEffect | None = None
    callee_timeout: int | None = None
    resolved: bool = False


@dataclass(frozen=True)
class Participant:
    """A step inside the body that changes data, or may: an action that isn't read-only, or a call."""

    id: str
    path: str
    kind: Literal["action", "callWorkflow"]
    mode: UndoMode | None
    # The action's side effect; None for calls and for actions the catalog doesn't know.
    side_effect: SideEffect | None = None
    resolved: bool = False
    # The action's timeoutSeconds, or the called workflow's spec.timeoutSeconds.
    timeout: int | None = None
    # A reservation's hold, its own holdSeconds or the transaction's.
    hold: int | None = None
    undo: tuple[UndoFacts, ...] = ()


@dataclass(frozen=True)
class Other:
    """A step that changes nothing: a pure step, a wait, a read-only action, or a fail step."""

    id: str
    path: str
    kind: str
    # The longest a wait, signal or human task can wait; None for no limit.
    wait_seconds: int | None = None


@dataclass(frozen=True)
class Choice:
    """A decision: one of its paths runs."""

    id: str
    path: str
    paths: tuple[Sequence, ...]


@dataclass(frozen=True)
class Fork:
    """A parallel step: all of its branches run, in no fixed order."""

    id: str
    path: str
    branches: tuple[Sequence, ...]


@dataclass(frozen=True)
class Repeat:
    """A loop: its body runs up to ``max_items`` times, possibly at once."""

    id: str
    path: str
    body: Sequence
    max_items: int


type Element = Participant | Other | Choice | Fork | Repeat


@dataclass(frozen=True)
class Sequence:
    """Steps that run one after another, at the definition path of their list."""

    path: str
    elements: tuple[Element, ...]


@dataclass(frozen=True)
class TransactionBody:
    id: str
    path: str
    body: Sequence


@dataclass(frozen=True)
class TransactionIssue:
    """One finding; ``code`` has no ``WV-COMP-`` prefix. ``move`` names a step list and the index to move last."""

    code: str
    path: str
    severity: Severity
    message: str
    related: str | None = None
    edit: tuple[str, int | bool] | None = None
    move: tuple[str, int] | None = None


@dataclass(frozen=True)
class TransactionAnalysis:
    issues: tuple[TransactionIssue, ...]
    # Participants that are the first irreversible step on some path.
    pivots: frozenset[str]
    # Steps reached only after a pivot on every path that reaches them.
    after_pivot: tuple[str, ...]


@dataclass(frozen=True)
class _State:
    pivot: Participant | None = None
    hold: int | None = None
    holder: Participant | None = None


@dataclass
class _Walk:
    issues: dict[tuple[str, str], TransactionIssue] = field(default_factory=dict)
    pivots: set[str] = field(default_factory=set)
    # For each step: whether every state that reached it had a pivot.
    reached: dict[str, bool] = field(default_factory=dict)
    # Loop bodies already walked, by loop, entry states, placement and reservations held beside them: each is walked
    # once per distinct entry.
    walked: dict[tuple[int, frozenset[_State], bool, int | None], frozenset[_State]] = field(default_factory=dict)
    # For each branch: the shortest hold it can reserve before a point of no return, and whether it can end without one.
    open_holds: dict[int, tuple[int | None, bool]] = field(default_factory=dict)

    def add(self, key: str, path: str, severity: Severity, **values: object) -> None:
        code = RULE_CODES[key]
        related = values.pop("related", None)
        edit = values.pop("edit", None)
        move = values.pop("move", None)
        if (code, path) not in self.issues:
            message = RULE_MESSAGES[key].format(**values)
            self.issues[code, path] = TransactionIssue(
                code,
                path,
                severity,
                message,
                related if isinstance(related, str) else None,
                edit if isinstance(edit, tuple) else None,
                move if isinstance(move, tuple) else None,
            )

    def sequence(
        self, sequence: Sequence, states: frozenset[_State], *, loop: bool, shared: bool, beside: int | None = None
    ) -> frozenset[_State]:
        """Walk ``sequence`` from ``states``; ``beside`` is the shortest hold reserved in a parallel sibling."""
        for index, element in enumerate(sequence.elements):
            if not states:
                break
            states = self.element(element, states, sequence, index, loop=loop, shared=shared, beside=beside)
        return states

    def element(
        self,
        element: Element,
        states: frozenset[_State],
        sequence: Sequence,
        index: int,
        *,
        loop: bool,
        shared: bool,
        beside: int | None,
    ) -> frozenset[_State]:
        if isinstance(element, Choice):
            return frozenset().union(
                *(self.sequence(path, states, loop=loop, shared=shared, beside=beside) for path in element.paths)
            )
        if isinstance(element, Fork):
            participating = [_participants(branch) > 0 for branch in element.branches]
            alongside = _shortest_beside([self.open_hold(branch)[0] for branch in element.branches])
            results = [
                self.sequence(
                    branch,
                    states,
                    loop=loop,
                    shared=shared or any(other for number, other in enumerate(participating) if number != position),
                    beside=_shortest(beside, alongside[position]),
                )
                for position, branch in enumerate(element.branches)
            ]
            merged: set[_State] = set()
            for state in states:
                combined = {state}
                for result in results:
                    combined = {_merge(left, right) for left in combined for right in result}
                merged |= combined
            return frozenset(merged) if all(results) else frozenset()
        if isinstance(element, Repeat):
            once = self.repeat(element, states, shared=shared, beside=beside)
            # A second iteration starts where the first ended; it differs only when the first changed the state.
            twice = self.repeat(element, once, shared=shared, beside=beside) if once != states else once
            return states | once | twice
        self.reached[element.id] = self.reached.get(element.id, True) and all(s.pivot is not None for s in states)
        if isinstance(element, Other):
            return self.other(element, states, beside)
        return self.participant(element, states, sequence, index, loop=loop, shared=shared)

    def repeat(self, loop: Repeat, states: frozenset[_State], *, shared: bool, beside: int | None) -> frozenset[_State]:
        """One iteration of a loop body from ``states``; memoized, so nested loops cost no more than flat ones."""
        key = (id(loop), states, shared, beside)
        if key not in self.walked:
            self.walked[key] = self.sequence(loop.body, states, loop=True, shared=shared, beside=beside)
        return self.walked[key]

    def open_hold(self, sequence: Sequence) -> tuple[int | None, bool]:
        """The shortest hold ``sequence`` can reserve before a point of no return, and whether it can end without one.

        A wait in a parallel branch runs while reservations in its sibling branches are held.
        """
        key = id(sequence)
        if key in self.open_holds:
            return self.open_holds[key]
        shortest: int | None = None
        open_ = True
        for element in sequence.elements:
            if not open_:
                break
            if isinstance(element, Participant):
                if element.mode == "reserve":
                    shortest = _shortest(shortest, element.hold)
                elif element.mode in (None, "irreversible"):
                    open_ = False
            elif isinstance(element, Other):
                open_ = element.kind != "fail"
            elif isinstance(element, Repeat):
                shortest = _shortest(shortest, self.open_hold(element.body)[0])
            else:
                parts = [
                    self.open_hold(part)
                    for part in (element.paths if isinstance(element, Choice) else element.branches)
                ]
                for hold, _ in parts:
                    shortest = _shortest(shortest, hold)
                ends = [ends for _, ends in parts]
                open_ = any(ends) if isinstance(element, Choice) else all(ends)
        self.open_holds[key] = (shortest, open_)
        return shortest, open_

    def other(self, step: Other, states: frozenset[_State], beside: int | None) -> frozenset[_State]:
        if step.kind == "fail":
            return frozenset()
        pivots = sorted((s.pivot for s in states if s.pivot is not None), key=lambda p: path_order(p.path))
        if step.kind in _WAITS:
            holds = [hold for s in states if s.pivot is None and (hold := _shortest(s.hold, beside)) is not None]
            if holds and (step.wait_seconds is None or step.wait_seconds > min(holds)):
                key = "TXN_HOLD_WAIT_UNBOUNDED" if step.wait_seconds is None else "TXN_HOLD_WAIT"
                wait = duration_text(step.wait_seconds) if step.wait_seconds is not None else ""
                self.add(key, step.path, "warning", step=step.id, wait=wait, hold=duration_text(min(holds)))
            if pivots:
                self.add("TXN_AFTER_PIVOT_WAIT", step.path, "warning", step=step.id, pivot=pivots[0].id)
        elif step.kind in _PURE and pivots:
            self.add("TXN_AFTER_PIVOT_PURE", step.path, "info", step=step.id, pivot=pivots[0].id)
        return states

    def participant(
        self,
        step: Participant,
        states: frozenset[_State],
        sequence: Sequence,
        index: int,
        *,
        loop: bool,
        shared: bool,
    ) -> frozenset[_State]:
        irreversible = step.mode in (None, "irreversible")
        # Only a step that can decide the transaction is restricted; after a point of no return, rule 3 applies.
        if irreversible and (loop or shared) and any(state.pivot is None for state in states):
            self.add(
                "TXN_PIVOT_PLACEMENT", step.path, "error", step=step.id, place="loop" if loop else "parallel branch"
            )
        after: list[_State] = []
        result: set[_State] = set()
        for state in states:
            if state.pivot is not None:
                after.append(state)
                result.add(state)
            elif irreversible:
                self.pivots.add(step.id)
                if state.hold is not None and state.holder is not None and step.timeout is not None:
                    needed = step.timeout + HOLD_MARGIN_SECONDS
                    if state.hold < needed:
                        self.add(
                            "TXN_HOLD_PIVOT",
                            state.holder.path + "/holdSeconds",
                            "error",
                            hold=duration_text(state.hold),
                            pivot=step.id,
                            timeout=duration_text(step.timeout),
                            needed=duration_text(needed),
                            edit=(state.holder.path + "/holdSeconds", needed),
                        )
                result.add(_State(step, state.hold, state.holder))
            elif step.mode == "reserve" and step.hold is not None and (state.hold is None or step.hold < state.hold):
                result.add(_State(None, step.hold, step))
            else:
                result.add(state)
        if after:
            pivot = sorted((s.pivot for s in after if s.pivot is not None), key=lambda p: path_order(p.path))[0]
            if step.mode in ("reserve", "compensate"):
                field = "/confirm" if step.mode == "reserve" else "/compensate"
                move = None
                if pivot.path.rpartition("/")[0] == sequence.path:
                    move = (sequence.path, int(pivot.path.rpartition("/")[2]))
                self.add(
                    "TXN_ORDER", step.path + field, "error", step=step.id, pivot=pivot.id, related=pivot.path, move=move
                )
            elif step.kind == "callWorkflow":
                self.add("TXN_AFTER_PIVOT_PURE", step.path, "info", step=step.id, pivot=pivot.id)
            elif step.side_effect == "non_idempotent":
                self.add("TXN_AFTER_PIVOT_UNSAFE", step.path, "error", step=step.id, pivot=pivot.id)
        return frozenset(result)


def _shortest(first: int | None, second: int | None) -> int | None:
    """The smaller of two holds, either of which may be absent."""
    if first is None:
        return second
    return first if second is None else min(first, second)


def _shortest_beside(holds: list[int | None]) -> list[int | None]:
    """For each branch, the shortest hold among the other branches, in linear time."""
    before: list[int | None] = [None]
    for hold in holds[:-1]:
        before.append(_shortest(before[-1], hold))
    after: list[int | None] = [None]
    for hold in reversed(holds[1:]):
        after.append(_shortest(after[-1], hold))
    after.reverse()
    return [_shortest(left, right) for left, right in zip(before, after, strict=True)]


def _merge(left: _State, right: _State) -> _State:
    """The state after two parallel branches that both started from the same state."""
    pivot = left.pivot or right.pivot
    if left.hold is None or (right.hold is not None and right.hold < left.hold):
        return _State(pivot, right.hold, right.holder)
    return _State(pivot, left.hold, left.holder)


def _participants(sequence: Sequence, lowered: tuple[Repeat, int] | None = None) -> int:
    """The most participants one run of ``sequence`` can register, loops multiplied out.

    ``lowered`` counts one loop as if its ``maxItems`` were the given number instead.
    """
    total = 0
    for element in sequence.elements:
        if isinstance(element, Participant):
            total += 1
        elif isinstance(element, Choice):
            total += max((_participants(path, lowered) for path in element.paths), default=0)
        elif isinstance(element, Fork):
            total += sum(_participants(branch, lowered) for branch in element.branches)
        elif isinstance(element, Repeat):
            items = lowered[1] if lowered is not None and lowered[0] is element else element.max_items
            total += _participants(element.body, lowered) * items
    return total


def _lowered_max_items(body: Sequence, loop: Repeat, limit: int) -> int | None:
    """The largest ``maxItems`` below the current one that brings ``body`` within ``limit``, if one does."""
    low, high, found = 1, loop.max_items - 1, None
    while low <= high:
        middle = (low + high) // 2
        if _participants(body, (loop, middle)) <= limit:
            found, low = middle, middle + 1
        else:
            high = middle - 1
    return found


def _all_participants(sequence: Sequence) -> Iterable[Participant]:
    for element in sequence.elements:
        if isinstance(element, Participant):
            yield element
        elif isinstance(element, Choice):
            for path in element.paths:
                yield from _all_participants(path)
        elif isinstance(element, Fork):
            for branch in element.branches:
                yield from _all_participants(branch)
        elif isinstance(element, Repeat):
            yield from _all_participants(element.body)


def _loops(sequence: Sequence, outer: int = 1) -> Iterable[tuple[Repeat, int]]:
    """Every loop with the product of the loops around it."""
    for element in sequence.elements:
        if isinstance(element, Repeat):
            yield element, outer
            yield from _loops(element.body, outer * element.max_items)
        elif isinstance(element, Choice):
            for path in element.paths:
                yield from _loops(path, outer)
        elif isinstance(element, Fork):
            for branch in element.branches:
                yield from _loops(branch, outer)


def check_transaction(
    transaction: TransactionBody, *, max_participants: int = MAX_TRANSACTION_PARTICIPANTS
) -> TransactionAnalysis:
    """Every ordering, placement, undo and hold rule for one transaction, in path order."""
    walk = _Walk()
    participants = list(_all_participants(transaction.body))
    for step in participants:
        if step.mode is None and (step.kind == "callWorkflow" or step.resolved):
            walk.add("TXN_UNDECLARED", step.path, "error", step=step.id, edit=(step.path + "/irreversible", True))
        if step.kind == "callWorkflow" and step.resolved and step.timeout is None:
            walk.add("TXN_CALL_TIMEOUT", step.path + "/uses", "error", name=step.id)
        for undo in step.undo:
            if not undo.resolved:
                continue
            if undo.kind == "action" and undo.side_effect not in _SAFE_UNDO:
                walk.add("TXN_UNDO_SIDE_EFFECT", undo.path + "/uses", "error", action=undo.reference)
            if undo.kind == "callWorkflow" and undo.callee_timeout is None:
                walk.add("TXN_CALL_TIMEOUT", undo.path + "/uses", "error", name=undo.reference.partition("@")[0])
    if not any(step.mode in ("reserve", "compensate") for step in participants):
        walk.add("TXN_NOTHING_TO_UNDO", transaction.path, "warning")
    count = _participants(transaction.body)
    if count > max_participants:
        edit = None
        for loop, _outer in sorted(_loops(transaction.body), key=lambda item: -item[1] * item[0].max_items):
            lowered = _lowered_max_items(transaction.body, loop, max_participants)
            if lowered is not None:
                edit = (loop.path + "/maxItems", lowered)
                break
        walk.add("TXN_SIZE", transaction.path, "error", count=count, limit=max_participants, edit=edit)
    # Undeclared steps count as irreversible for ordering, so every problem shows in one pass.
    walk.sequence(transaction.body, frozenset({_State()}), loop=False, shared=False)
    issues = sorted(walk.issues.values(), key=lambda issue: (path_order(issue.path), issue.code))
    after = tuple(step for step, only_after in walk.reached.items() if only_after)
    return TransactionAnalysis(tuple(issues), frozenset(walk.pivots), after)


_UNDO_PHASES: Final = ("confirm", "cancel", "compensate")


def undo_shape_issues(document: JsonValue) -> list[tuple[str, str]]:
    """Ways of undoing a step that the definition model rejects, as (message key, path), before contract validation.

    The compiler reports them at their field instead of as an anonymous contract violation.
    """
    from firefly_weave.compiler.calls import workflow_steps

    found: list[tuple[str, str]] = []
    for step, path in workflow_steps(document):
        if step.get("kind") not in ("action", "callWorkflow"):
            continue
        if ("confirm" in step) != ("cancel" in step):
            found.append(("TXN_RESERVE_PAIR", path + ("/confirm" if "confirm" in step else "/cancel")))
        if "compensate" in step and ("confirm" in step or "cancel" in step):
            found.append(("TXN_UNDO_FORMS", path + "/compensate"))
        if "irreversible" in step and any(name in step for name in _UNDO_PHASES):
            found.append(("TXN_UNDO_FORMS", path + "/irreversible"))
        if "holdSeconds" in step and "confirm" not in step and "cancel" not in step:
            found.append(("TXN_HOLD_RESERVE", path + "/holdSeconds"))
        undo = any(name in step for name in (*_UNDO_PHASES, "irreversible", "holdSeconds"))
        if step.get("kind") == "callWorkflow" and step.get("mode") == "detach" and undo:
            found.append(("TXN_DETACH", path + "/mode"))
    return found


def without_undo_shape_issues(document: JsonObject) -> JsonObject:
    """A copy of a workflow definition without the undo fields of steps that ``undo_shape_issues`` reports."""
    from firefly_weave.compiler.calls import workflow_steps

    copy = deepcopy(document)
    flagged = {path.rpartition("/")[0] for _, path in undo_shape_issues(copy)}
    for step, path in workflow_steps(copy):
        if path in flagged:
            for name in (*_UNDO_PHASES, "irreversible", "holdSeconds"):
                step.pop(name, None)
    return copy
