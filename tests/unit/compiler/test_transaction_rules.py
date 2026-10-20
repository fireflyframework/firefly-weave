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


"""Publish-time rules for transactions, on bodies whose catalog facts are already resolved."""

from itertools import product

from hypothesis import given, settings
from hypothesis import strategies as st

from firefly_weave.compiler.transactions import (
    Choice,
    Fork,
    Other,
    Participant,
    Repeat,
    Sequence,
    TransactionBody,
    UndoFacts,
    check_transaction,
    duration_text,
)

BODY = "/spec/steps/0/steps"


def at(index: int, base: str = BODY) -> str:
    return f"{base}/{index}"


def reserve(index: int, hold: int = 900, *, step: str = "hold", base: str = BODY, **facts) -> Participant:
    path = at(index, base)
    undo = (
        UndoFacts("confirm", path + "/confirm", "action", "source.confirm@1.0.0", "idempotent", resolved=True),
        UndoFacts("cancel", path + "/cancel", "action", "source.release@1.0.0", "idempotency_key", resolved=True),
    )
    return Participant(step, path, "action", "reserve", "idempotency_key", True, 30, hold, undo, **facts)


def compensate(index: int, *, step: str = "add", base: str = BODY, side_effect: str = "non_idempotent") -> Participant:
    path = at(index, base)
    undo = (
        UndoFacts("compensate", path + "/compensate", "action", "target.remove@1.0.0", "idempotent", resolved=True),
    )
    return Participant(step, path, "action", "compensate", side_effect, True, 30, None, undo)


def irreversible(index: int, *, step: str = "submit", base: str = BODY, timeout: int = 60, side_effect="idempotent"):
    return Participant(step, at(index, base), "action", "irreversible", side_effect, True, timeout)


def check(*elements, base: str = BODY):
    analysis = check_transaction(TransactionBody("move", "/spec/steps/0", Sequence(base, tuple(elements))))
    return [(issue.code, issue.path, issue.severity) for issue in analysis.issues], analysis


def test_reserve_then_compensate_then_the_step_that_cant_be_undone_is_clean():
    issues, analysis = check(reserve(0), compensate(1), irreversible(2))
    assert issues == []
    assert analysis.pivots == {"submit"}
    assert analysis.after_pivot == ()


def test_a_step_that_can_be_undone_after_one_that_cant_is_an_ordering_error_with_a_move():
    issues, analysis = check(reserve(0), irreversible(1), compensate(2))
    assert issues == [("TXN_ORDER", at(2) + "/compensate", "error")]
    order = analysis.issues[0]
    assert order.message == (
        "add can be undone but runs after submit, which can't. "
        "Steps that can't be undone must come last in a transaction."
    )
    assert (order.related, order.move) == (at(1), (BODY, 1))
    assert analysis.after_pivot == ("add",)


def test_ordering_holds_on_every_decision_path_but_not_across_them():
    yes = Sequence(at(0) + "/cases/0/steps", (irreversible(0, base=at(0) + "/cases/0/steps"),))
    no = Sequence(at(0) + "/default/steps", ())
    issues, analysis = check(Choice("route", at(0), (yes, no)), compensate(1))
    assert issues == [("TXN_ORDER", at(1) + "/compensate", "error")]
    assert analysis.after_pivot == ()
    other = Sequence(at(0) + "/default/steps", (compensate(0, base=at(0) + "/default/steps"),))
    assert check(Choice("route", at(0), (yes, other)))[0] == []


def test_the_step_that_cant_be_undone_may_not_sit_in_a_loop_or_beside_parallel_steps_that_change_data():
    body = Sequence(at(0) + "/body/steps", (irreversible(0, base=at(0) + "/body/steps"),))
    issues, analysis = check(Repeat("each", at(0), body, 10), reserve(1))
    assert issues == [
        ("TXN_PIVOT_PLACEMENT", at(0, at(0) + "/body/steps"), "error"),
        ("TXN_ORDER", at(1) + "/confirm", "error"),
    ]
    assert analysis.issues[0].message == (
        "Only one step that can't be undone can decide the transaction. Move submit out of the loop."
    )
    left = Sequence(at(0) + "/branches/a/steps", (irreversible(0, base=at(0) + "/branches/a/steps"),))
    right = Sequence(at(0) + "/branches/b/steps", (compensate(0, base=at(0) + "/branches/b/steps"),))
    issues, analysis = check(Fork("both", at(0), (left, right)))
    assert issues == [("TXN_PIVOT_PLACEMENT", at(0, at(0) + "/branches/a/steps"), "error")]
    assert "parallel branch" in analysis.issues[0].message
    alone = Sequence(at(0) + "/branches/b/steps", (Other("note", at(0, at(0) + "/branches/b/steps"), "transform"),))
    assert check(Fork("both", at(0), (left, alone)), reserve(1))[0] == [("TXN_ORDER", at(1) + "/confirm", "error")]


def test_after_the_point_of_no_return_steps_that_cant_be_undone_may_repeat_or_run_side_by_side():
    inner = at(2) + "/body/steps"
    safe = Sequence(inner, (irreversible(0, step="send", base=inner),))
    issues, analysis = check(reserve(0), irreversible(1, step="submit"), Repeat("each", at(2), safe, 10))
    assert issues == []
    assert analysis.pivots == {"submit"}
    unsafe = Sequence(inner, (irreversible(0, step="send", base=inner, side_effect="non_idempotent"),))
    issues, _ = check(reserve(0), irreversible(1, step="submit"), Repeat("each", at(2), unsafe, 10))
    assert issues == [("TXN_AFTER_PIVOT_UNSAFE", at(0, inner), "error")]
    left, right = at(2) + "/branches/a/steps", at(2) + "/branches/b/steps"
    branches = (
        Sequence(left, (irreversible(0, step="send-a", base=left),)),
        Sequence(right, (irreversible(0, step="send-b", base=right),)),
    )
    assert check(reserve(0), irreversible(1, step="submit"), Fork("both", at(2), branches))[0] == []


def test_a_step_that_can_be_reached_before_the_point_of_no_return_still_may_not_repeat():
    first, second = at(0) + "/cases/0/steps", at(0) + "/cases/1/steps"
    route = Choice(
        "route",
        at(0),
        (Sequence(first, (irreversible(0, step="submit", base=first),)), Sequence(second, (reserve(0, base=second),))),
    )
    inner = at(1) + "/body/steps"
    issues, _ = check(route, Repeat("each", at(1), Sequence(inner, (irreversible(0, step="send", base=inner),)), 10))
    assert ("TXN_PIVOT_PLACEMENT", at(0, inner), "error") in issues


def test_after_the_point_of_no_return_only_work_that_is_safe_to_retry():
    issues, analysis = check(
        reserve(0),
        irreversible(1),
        irreversible(2, step="notify", side_effect="non_idempotent"),
        irreversible(3, step="audit", side_effect="idempotency_key"),
        Other("summary", at(4), "transform"),
        Other("approve", at(5), "humanTask", None),
        Participant("follow", at(6), "callWorkflow", "irreversible", None, True, 120),
    )
    assert issues == [
        ("TXN_AFTER_PIVOT_UNSAFE", at(2), "error"),
        ("TXN_AFTER_PIVOT_PURE", at(4), "info"),
        ("TXN_AFTER_PIVOT_WAIT", at(5), "warning"),
        ("TXN_AFTER_PIVOT_PURE", at(6), "info"),
    ]
    assert analysis.after_pivot == ("notify", "audit", "summary", "approve", "follow")


def test_undo_steps_must_be_safe_to_retry_and_called_workflows_must_have_a_time_limit():
    path = at(0)
    unsafe = Participant(
        "add",
        path,
        "action",
        "compensate",
        "non_idempotent",
        True,
        30,
        None,
        (UndoFacts("compensate", path + "/compensate", "action", "target.remove@1.0.0", "read_only", resolved=True),),
    )
    call = Participant(
        "reserve",
        at(1),
        "callWorkflow",
        "compensate",
        None,
        True,
        None,
        None,
        (UndoFacts("compensate", at(1) + "/compensate", "callWorkflow", "release-slot@1.0.0", resolved=True),),
    )
    issues, analysis = check(unsafe, call)
    assert issues == [
        ("TXN_UNDO_SIDE_EFFECT", path + "/compensate/uses", "error"),
        ("TXN_CALL_TIMEOUT", at(1) + "/compensate/uses", "error"),
        ("TXN_CALL_TIMEOUT", at(1) + "/uses", "error"),
    ]
    messages = {issue.path: issue.message for issue in analysis.issues}
    assert messages[path + "/compensate/uses"] == (
        "Weave retries undo steps until they succeed, so target.remove@1.0.0 must be safe to retry."
    )
    assert messages[at(1) + "/compensate/uses"] == (
        "Called workflows inside a transaction must have a time limit. "
        "Set timeoutSeconds on release-slot in a new version."
    )


def test_the_step_named_as_the_point_of_no_return_is_the_first_in_step_order():
    cases = tuple(
        Sequence(
            f"{at(0)}/cases/{number}/steps",
            (irreversible(0, step=f"submit{number}", base=f"{at(0)}/cases/{number}/steps"),)
            if number in (2, 10)
            else (),
        )
        for number in range(11)
    )
    _, analysis = check(Choice("route", at(0), cases), compensate(1))
    assert analysis.issues[0].code == "TXN_ORDER"
    assert analysis.issues[0].related == f"{at(0)}/cases/2/steps/0"
    assert "runs after submit2," in analysis.issues[0].message


def test_a_step_that_changes_data_must_say_how_it_is_undone_and_counts_as_irreversible_meanwhile():
    undeclared = Participant("add", at(0), "action", None, "non_idempotent", True, 30)
    issues, analysis = check(undeclared, compensate(1, step="add2"))
    assert issues == [("TXN_UNDECLARED", at(0), "error"), ("TXN_ORDER", at(1) + "/compensate", "error")]
    assert analysis.issues[0].edit == (at(0) + "/irreversible", True)
    unknown = Participant("lookup", at(0), "callWorkflow", None)
    assert ("TXN_UNDECLARED", at(0), "error") in check(unknown, compensate(1))[0]


def test_reservations_must_outlast_the_step_that_cant_be_undone():
    issues, analysis = check(reserve(0, 600), irreversible(1, timeout=900))
    assert issues == [("TXN_HOLD_PIVOT", at(0) + "/holdSeconds", "error")]
    issue = analysis.issues[0]
    assert issue.message == (
        "Reservations are held for 10 minutes, but submit may take up to 15 minutes. "
        "Hold them for at least 15 minutes 30 seconds."
    )
    assert issue.edit == (at(0) + "/holdSeconds", 930)
    assert check(reserve(0, 930), irreversible(1, timeout=900))[0] == []


def test_waits_before_the_decision_warn_when_they_can_outlast_reservations():
    issues, analysis = check(
        reserve(0, 900),
        Other("pause", at(1), "wait", 600),
        Other("approve", at(2), "humanTask", 172_800),
        Other("answer", at(3), "signal", None),
        irreversible(4),
    )
    assert issues == [("TXN_HOLD_WAIT", at(2), "warning"), ("TXN_HOLD_WAIT", at(3), "warning")]
    assert analysis.issues[0].message == "approve can wait up to 2 days, but reservations are held for 15 minutes."
    assert (
        analysis.issues[1].message == "answer can wait without a time limit, but reservations are held for 15 minutes."
    )
    assert check(Other("approve", at(0), "humanTask", None), reserve(1), irreversible(2))[0] == []


def test_size_is_bounded_with_loops_multiplied_out_and_an_edit_on_maxitems():
    body = Sequence(at(0) + "/body/steps", (compensate(0, base=at(0) + "/body/steps"),))
    issues, analysis = check(Repeat("each", at(0), body, 2000))
    assert issues == [("TXN_SIZE", "/spec/steps/0", "error")]
    assert analysis.issues[0].edit == (at(0) + "/maxItems", 1000)
    assert "up to 2,000 steps" in analysis.issues[0].message
    assert check(Repeat("each", at(0), body, 1000))[0] == []


def _loop(name: str, index: int, max_items: int, base: str = BODY) -> Repeat:
    inner = f"{at(index, base)}/body/steps"
    return Repeat(name, at(index, base), Sequence(inner, (compensate(0, step=f"{name}-add", base=inner),)), max_items)


def test_the_suggested_maxitems_brings_the_path_through_that_loop_within_the_limit():
    first, second = f"{at(0)}/cases/0/steps", f"{at(0)}/cases/1/steps"

    def body(large: int, small: int, after: int) -> tuple:
        paths = (
            Sequence(first, (_loop("large", 0, large, first),)),
            Sequence(second, (_loop("small", 0, small, second),)),
        )
        return Choice("route", at(0), paths), _loop("after", 1, after)

    # Lowering the larger loop to 700 still leaves 900 + 300 on the other path; no single maxItems fits.
    _, analysis = check(*body(1100, 900, 300))
    assert [issue.code for issue in analysis.issues] == ["TXN_SIZE"]
    assert analysis.issues[0].edit is None
    # The other path stays within the limit, so lowering the larger loop works.
    _, analysis = check(*body(1100, 600, 300))
    assert analysis.issues[0].edit == (f"{first}/0/maxItems", 700)
    _, analysis = check(*body(1100, 500, 300))
    assert analysis.issues[0].edit == (f"{first}/0/maxItems", 700)


def test_a_transaction_with_nothing_to_undo_warns():
    issues, _ = check(irreversible(0))
    assert issues == [("TXN_NOTHING_TO_UNDO", "/spec/steps/0", "warning")]
    assert check(Other("note", at(0), "transform"))[0] == [("TXN_NOTHING_TO_UNDO", "/spec/steps/0", "warning")]


def test_a_fail_step_ends_its_path():
    issues, _ = check(reserve(0), Other("stop", at(1), "fail"), irreversible(2), compensate(3))
    assert issues == []


def test_durations_read_as_words():
    assert [duration_text(value) for value in (1, 60, 930, 3600, 90_061, 172_800, 0)] == [
        "1 second",
        "1 minute",
        "15 minutes 30 seconds",
        "1 hour",
        "1 day 1 hour 1 minute 1 second",
        "2 days",
        "0 seconds",
    ]


# Bodies of decisions, parallel steps, loops, waits and steps, compared with a brute-force enumeration of every run.
_RULE_CODES = {
    "TXN_ORDER",
    "TXN_PIVOT_PLACEMENT",
    "TXN_AFTER_PIVOT_UNSAFE",
    "TXN_AFTER_PIVOT_PURE",
    "TXN_AFTER_PIVOT_WAIT",
    "TXN_HOLD_PIVOT",
    "TXN_HOLD_WAIT",
}
_MAX_GENERATED_PARTICIPANTS = 9
_UNROLLED_ITERATIONS = 3


@st.composite
def _bodies(draw, base: str = BODY, depth: int = 0, budget: list[int] | None = None, numbers: list[int] | None = None):
    budget = [_MAX_GENERATED_PARTICIPANTS] if budget is None else budget
    numbers = [0] if numbers is None else numbers

    def number() -> int:
        numbers[0] += 1
        return numbers[0]

    elements = []
    for index in range(draw(st.integers(0, 3 if depth == 0 else 2))):
        path = at(index, base)
        shape = draw(st.sampled_from(["choice", "fork", "loop", "step", "step", "step"] if depth < 2 else ["step"]))
        if shape == "choice":
            paths = draw(st.integers(1, 2))
            nested = tuple(draw(_bodies(f"{path}/cases/{n}/steps", depth + 1, budget, numbers)) for n in range(paths))
            elements.append(Choice(f"c{number()}", path, nested))
            continue
        if shape == "fork":
            branches = draw(st.integers(1, 3))
            nested = tuple(
                draw(_bodies(f"{path}/branches/{n}/steps", depth + 1, budget, numbers)) for n in range(branches)
            )
            elements.append(Fork(f"f{number()}", path, nested))
            continue
        if shape == "loop":
            body = draw(_bodies(f"{path}/body/steps", depth + 1, budget, numbers))
            elements.append(Repeat(f"r{number()}", path, body, draw(st.integers(2, 5))))
            continue
        kinds = ["wait", "pure", "fail", "read"]
        if budget[0] > 0:
            kinds += ["reserve", "compensate", "irreversible", "undeclared", "call"] * 2
        kind = draw(st.sampled_from(kinds))
        step = f"s{number()}"
        if kind in ("reserve", "compensate", "irreversible", "undeclared", "call"):
            budget[0] -= 1
        if kind == "reserve":
            # Distinct holds, so the reservation a finding names is never a tie.
            hold = 60 + 10 * draw(st.integers(0, 100)) + number() % 10
            elements.append(reserve(index, hold, step=step, base=base))
        elif kind == "compensate":
            effect = draw(st.sampled_from(["non_idempotent", "idempotent"]))
            elements.append(compensate(index, step=step, base=base, side_effect=effect))
        elif kind == "irreversible":
            effect = draw(st.sampled_from(["non_idempotent", "idempotent", "idempotency_key"]))
            timeout = draw(st.sampled_from([10, 300, 2_000]))
            elements.append(irreversible(index, step=step, base=base, timeout=timeout, side_effect=effect))
        elif kind == "undeclared":
            effect = draw(st.sampled_from(["non_idempotent", "idempotent"]))
            elements.append(Participant(step, path, "action", None, effect, True, draw(st.sampled_from([10, 2_000]))))
        elif kind == "call":
            mode = draw(st.sampled_from(["irreversible", "compensate"]))
            elements.append(Participant(step, path, "callWorkflow", mode, None, True, 60))
        elif kind == "wait":
            waiting = draw(st.sampled_from(["wait", "signal", "humanTask"]))
            elements.append(Other(step, path, waiting, draw(st.sampled_from([None, 50, 5_000]))))
        elif kind == "pure":
            elements.append(Other(step, path, draw(st.sampled_from(["transform", "llm", "decisionTable"]))))
        elif kind == "fail":
            elements.append(Other(step, path, "fail"))
        else:
            elements.append(Other(step, path, "action"))
    return Sequence(base, tuple(elements))


# One run as a graph: its steps, the steps that happen before each, and whether it always ends in a fail step.
type _Run = tuple[list, list[set[int]], bool]
_EMPTY: _Run = ([], [], False)


def _then(first: _Run, second: _Run) -> _Run:
    size = len(first[0])
    before = set(range(size))
    preds = [set(p) for p in first[1]] + [{index + size for index in p} | before for p in second[1]]
    return first[0] + second[0], preds, first[2] or second[2]


def _beside(first: _Run, second: _Run) -> _Run:
    size = len(first[0])
    preds = [set(p) for p in first[1]] + [{index + size for index in p} for p in second[1]]
    return first[0] + second[0], preds, first[2] or second[2]


def _runs(sequence: Sequence) -> list[_Run]:
    runs = [_EMPTY]
    for element in sequence.elements:
        runs = [run if run[2] else _then(run, part) for run in runs for part in ([None] if run[2] else _parts(element))]
    return runs


def _parts(element) -> list[_Run]:
    if isinstance(element, Choice):
        return [run for path in element.paths for run in _runs(path)]
    if isinstance(element, Fork):
        combined = []
        for runs in product(*(_runs(branch) for branch in element.branches)):
            run = _EMPTY
            for part in runs:
                run = _beside(run, part)
            combined.append(run)
        return combined
    if isinstance(element, Repeat):
        body = _runs(element.body)
        unrolled, level = [_EMPTY], [_EMPTY]
        for _ in range(min(_UNROLLED_ITERATIONS, element.max_items)):
            level = [_then(run, part) for run in level if not run[2] for part in body]
            unrolled.extend(level)
        return unrolled
    if isinstance(element, Other) and element.kind == "fail":
        return [([], [], True)]
    return [([element], [set()], False)]


def _members(sequence: Sequence):
    for element in sequence.elements:
        if isinstance(element, (Participant, Other)):
            yield element
        for nested in getattr(element, "paths", ()) + getattr(element, "branches", ()):
            yield from _members(nested)
        if isinstance(element, Repeat):
            yield from _members(element.body)


def _misplaced(sequence: Sequence, *, loop: bool = False, shared: bool = False):
    """Steps that can't be undone inside a loop, or in a parallel branch beside branches that change data."""
    for element in sequence.elements:
        if isinstance(element, Participant) and element.mode in (None, "irreversible") and (loop or shared):
            yield element.path
        elif isinstance(element, Choice):
            for path in element.paths:
                yield from _misplaced(path, loop=loop, shared=shared)
        elif isinstance(element, Fork):
            for position, branch in enumerate(element.branches):
                beside = any(
                    isinstance(member, Participant)
                    for number, other in enumerate(element.branches)
                    if number != position
                    for member in _members(other)
                )
                yield from _misplaced(branch, loop=loop, shared=shared or beside)
        elif isinstance(element, Repeat):
            yield from _misplaced(element.body, loop=True, shared=shared)


def _expected(body: Sequence):
    """Every finding, the pivots and the steps reached only after one, read off each run directly."""
    # Rule 2 applies before the point of no return; after it, rule 3 decides what may repeat or run side by side.
    found, pivots, before, only_after = set(), set(), set(), {}
    for nodes, preds, _ in _runs(body):
        decides = [isinstance(node, Participant) and node.mode in (None, "irreversible") for node in nodes]
        for index, node in enumerate(nodes):
            after = any(decides[other] for other in preds[index])
            if not after:
                before.add(node.path)
            only_after[node.id] = only_after.get(node.id, True) and after
            holds = sorted(
                (nodes[other].hold, nodes[other].path)
                for other in preds[index]
                if isinstance(nodes[other], Participant)
                and nodes[other].mode == "reserve"
                and not any(decides[earlier] for earlier in preds[other])
            )
            if isinstance(node, Participant):
                if after:
                    if node.mode in ("reserve", "compensate"):
                        found.add(("TXN_ORDER", node.path + ("/confirm" if node.mode == "reserve" else "/compensate")))
                    elif node.kind == "callWorkflow":
                        found.add(("TXN_AFTER_PIVOT_PURE", node.path))
                    elif node.side_effect == "non_idempotent":
                        found.add(("TXN_AFTER_PIVOT_UNSAFE", node.path))
                elif decides[index]:
                    pivots.add(node.id)
                    if holds and node.timeout is not None and holds[0][0] < node.timeout + 30:
                        found.add(("TXN_HOLD_PIVOT", holds[0][1] + "/holdSeconds"))
            elif node.kind in ("wait", "signal", "humanTask"):
                if not after and holds and (node.wait_seconds is None or node.wait_seconds > holds[0][0]):
                    found.add(("TXN_HOLD_WAIT", node.path))
                if after:
                    found.add(("TXN_AFTER_PIVOT_WAIT", node.path))
            elif node.kind in ("transform", "llm", "decisionTable") and after:
                found.add(("TXN_AFTER_PIVOT_PURE", node.path))
    found |= {("TXN_PIVOT_PLACEMENT", path) for path in _misplaced(body) if path in before}
    return found, pivots, {step for step, only in only_after.items() if only}


@settings(max_examples=400, deadline=None)
@given(_bodies())
def test_rule_verdicts_match_every_run_enumerated(body):
    expected, pivots, only_after = _expected(body)
    analysis = check_transaction(TransactionBody("move", "/spec/steps/0", body))
    assert {(issue.code, issue.path) for issue in analysis.issues if issue.code in _RULE_CODES} == expected
    assert analysis.pivots == pivots
    fails = {member.id for member in _members(body) if isinstance(member, Other) and member.kind == "fail"}
    assert set(analysis.after_pivot) - fails == only_after


def test_parallel_reservations_must_each_outlast_the_step_that_cant_be_undone():
    short, long = f"{at(0)}/branches/0/steps", f"{at(0)}/branches/1/steps"
    branches = (
        Sequence(short, (reserve(0, 60, step="hold-short", base=short),)),
        Sequence(long, (reserve(0, 900, step="hold-long", base=long),)),
    )
    issues, analysis = check(Fork("both", at(0), branches), irreversible(1, timeout=300))
    assert issues == [("TXN_HOLD_PIVOT", f"{short}/0/holdSeconds", "error")]
    assert analysis.issues[0].edit == (f"{short}/0/holdSeconds", 330)


def test_a_loop_is_checked_on_its_second_round_too():
    inner = f"{at(0)}/body/steps"
    body = Sequence(
        inner,
        (
            compensate(0, step="add", base=inner),
            Participant("note", f"{inner}/1", "action", None, "idempotent", True, 10),
        ),
    )
    issues, _ = check(Repeat("each", at(0), body, 3))
    assert ("TXN_ORDER", f"{inner}/0/compensate", "error") in issues


def test_nested_loops_are_walked_once_per_distinct_entry_state(monkeypatch):
    from firefly_weave.compiler import transactions

    calls = 0
    original = transactions._Walk.sequence

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(transactions._Walk, "sequence", counted)
    inner = Sequence(BODY + "/x", (compensate(0, base=BODY + "/x"),))
    for level in range(32):
        holder = reserve(0, 900 - level, step=f"hold{level}", base=f"/l{level}")
        inner = Sequence(f"/l{level}", (holder, Repeat(f"loop{level}", f"/l{level}/1", inner, 2)))
    check_transaction(TransactionBody("move", "/spec/steps/0", inner), max_participants=10**12)
    # Walking each body twice per iteration would take 2**32 walks; one walk per distinct entry stays linear.
    assert calls < 32 * 8
