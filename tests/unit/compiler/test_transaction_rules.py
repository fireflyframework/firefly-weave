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
        (UndoFacts("compensate", at(1) + "/compensate", "callWorkflow", "release-stock@1.0.0", resolved=True),),
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
        "Set timeoutSeconds on release-stock in a new version."
    )


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


# Bodies of decisions and steps, compared with a brute-force enumeration of every execution path.
_MODES = st.sampled_from(["reserve", "compensate", "irreversible", "irreversible-unsafe"])


@st.composite
def _bodies(draw, base: str = BODY, depth: int = 0):
    elements = []
    for index in range(draw(st.integers(0, 4))):
        if depth < 2 and draw(st.booleans()):
            paths = tuple(
                draw(_bodies(f"{at(index, base)}/cases/{number}/steps", depth + 1))
                for number in range(draw(st.integers(1, 3)))
            )
            elements.append(Choice(f"c{depth}{index}", at(index, base), paths))
        else:
            mode = draw(_MODES)
            step = f"s{len(base)}x{index}"
            if mode == "reserve":
                elements.append(reserve(index, step=step, base=base))
            elif mode == "compensate":
                elements.append(compensate(index, step=step, base=base))
            else:
                effect = "non_idempotent" if mode == "irreversible-unsafe" else "idempotent"
                elements.append(irreversible(index, step=step, base=base, side_effect=effect))
    return Sequence(base, tuple(elements))


def _paths(sequence: Sequence):
    options = []
    for element in sequence.elements:
        if isinstance(element, Choice):
            options.append([path for branch in element.paths for path in _paths(branch)])
        else:
            options.append([[element]])
    for combination in product(*options):
        yield [step for part in combination for step in part]


@settings(max_examples=300, deadline=None)
@given(_bodies())
def test_ordering_verdicts_match_every_path_enumerated(body):
    expected = set()
    for path in _paths(body):
        pivot = None
        for step in path:
            if pivot is None:
                if step.mode == "irreversible":
                    pivot = step
            elif step.mode in ("reserve", "compensate"):
                expected.add(("TXN_ORDER", step.path + ("/confirm" if step.mode == "reserve" else "/compensate")))
            elif step.side_effect == "non_idempotent":
                expected.add(("TXN_AFTER_PIVOT_UNSAFE", step.path))
    analysis = check_transaction(TransactionBody("move", "/spec/steps/0", body))
    found = {
        (issue.code, issue.path) for issue in analysis.issues if issue.code in {"TXN_ORDER", "TXN_AFTER_PIVOT_UNSAFE"}
    }
    assert found == expected


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
