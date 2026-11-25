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

"""Pattern searches are bounded by the CPU time their thread spends, so a busy machine refuses no valid document."""

import math
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from firefly_weave.compiler import schemas
from firefly_weave.compiler.schema_profile import SchemaLimits
from firefly_weave.contracts.values import JsonObject
from firefly_weave.operations.redaction import classify

REFUSED = ["WV-SCHEMA-RESOURCE_LIMIT"]
# Windows adds a whole 1/64 s to the thread that runs when its clock ticks.
TICK = 1 / 64


class Machine:
    """A machine that gives the thread ``share`` of a processor, and an engine whose timeout counts elapsed time.

    Every search needs ``cost`` seconds of CPU. With ``step``, the thread's CPU clock moves by whole steps.
    """

    def __init__(self, cost: float, *, share: float = 1.0, step: float = 0.0) -> None:
        self.cost, self.share, self.step = cost, share, step
        self.cpu = self.elapsed = 0.0
        self.timeouts: list[float] = []

    def thread_time(self) -> float:
        return self.cpu // self.step * self.step if self.step else self.cpu

    def perf_counter(self) -> float:
        return self.elapsed

    monotonic = perf_counter

    def search(self, value: str, *, timeout: float, concurrent: bool | None = None) -> object:
        self.timeouts.append(timeout)
        used = min(self.cost / self.share, timeout)
        self.elapsed += used
        self.cpu += used * self.share
        if self.cost / self.share > timeout:
            raise TimeoutError
        return value


def run(machine: Machine, monkeypatch: pytest.MonkeyPatch, *, searches: int = 1) -> list[str]:
    """The codes of validating ``searches`` strings against a pattern on ``machine``."""
    monkeypatch.setattr(schemas, "time", machine)
    monkeypatch.setattr(schemas, "_pattern", lambda *_, **__: machine)
    monkeypatch.setattr(schemas, "_CPU_STEP", machine.step, raising=False)
    # A validator alone, so that redaction's own searches do not count twice.
    limits = SchemaLimits()
    return [issue.code for issue in schemas._payload({"items": {"pattern": "a"}}, ["a"] * searches, {}, limits)]


def contract() -> JsonObject:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "orders", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "steps": [{"id": "one", "kind": "transform", "value": {"literal": 1}}],
            "output": {"literal": {}},
        },
    }


@pytest.mark.parametrize("check", ["payload", "contract", "redaction", "issues"])
def test_elapsed_time_without_cpu_time_refuses_no_valid_document(check, monkeypatch):
    # Every look at an elapsed clock is 200 ms after the one before: the thread waits for a processor, and the
    # thread's own CPU clock runs as it really does.
    now = [0.0]

    def paused() -> float:
        now[0] += 0.2
        return now[0]

    monkeypatch.setattr(
        schemas, "time", SimpleNamespace(monotonic=paused, perf_counter=paused, thread_time=time.thread_time)
    )
    schema: JsonObject = {"type": "object", "patternProperties": {"^[a-z]+$": {"pattern": "^[a-z]+$"}}}
    value: JsonObject = {"first": "a", "second": "b"}
    if check == "payload":
        assert schemas.validate_payload(schema, value, {}) == ()
    elif check == "contract":
        assert schemas.validate_contract_payload("workflow", contract()) == ()
    elif check == "redaction":
        assert classify(schema, value, {}) is None
    else:
        assert schemas.payload_issues(schema, {"first": 1, "second": "B"}, {}) == [("/*", "pattern")]


@pytest.mark.parametrize("step", [0.0, TICK])
def test_every_machine_accepts_a_valid_search_and_ends_a_runaway_one(step, monkeypatch):
    # How long a runaway search may last: 10 ms of a whole processor, then about four times what that share needs, and
    # on a stepped clock about four steps more.
    for share, longest in ((1.0, 0.011), (0.5, 0.08), (0.25, 0.16), (0.05, 0.8), (0.01, 2.6)):
        machine = Machine(0.001, share=share, step=step)
        assert run(machine, monkeypatch, searches=20) == [], share
        # Less processor only means more elapsed time.
        assert machine.elapsed >= 20 * 0.001 / share
        machine = Machine(math.inf, share=share, step=step)
        assert run(machine, monkeypatch) == REFUSED, share
        assert machine.elapsed <= longest + 4 * step / share, share


def test_a_machine_that_gives_almost_no_processor_is_refused_after_a_bounded_wait(monkeypatch):
    machine = Machine(0.001, share=1e-6)
    assert run(machine, monkeypatch) == REFUSED
    # The tries are bounded: each one lasts twice the one before, far past the 10 ms a search may spend.
    assert machine.timeouts == pytest.approx([0.01 * 2**attempt for attempt in range(8)])


def test_repeated_timeouts_share_one_bound_for_the_whole_validation(monkeypatch):
    # Each search needs 0.02 ms of CPU and 40 ms of elapsed time, so it stops twice (30 ms) before it finishes.
    machine = Machine(0.000_02, share=0.000_5)
    assert run(machine, monkeypatch, searches=1000) == REFUSED
    waited = sum(timeout for timeout in machine.timeouts if timeout < 0.04)
    assert 10 <= waited <= 10.1
    # Only stopped tries count: 300 such searches last 21 s, 9 s of them in stopped tries, and are accepted.
    machine = Machine(0.000_02, share=0.000_5)
    assert run(machine, monkeypatch, searches=300) == []
    assert machine.elapsed == pytest.approx(21)


def test_the_cpu_time_of_all_searches_has_one_budget_whatever_the_machine_gives(monkeypatch):
    # 4 ms each: 24 searches spend 96 ms of the 100 ms, and 26 would spend 104 ms. Tries that were stopped early say
    # nothing about the document, so they never count.
    for share in (1.0, 0.5, 0.1):
        assert run(Machine(0.004, share=share), monkeypatch, searches=24) == [], share
        assert run(Machine(0.004, share=share), monkeypatch, searches=26) == REFUSED, share
    # With a stepped clock the last search may spend past the budget. The engine takes a negative timeout as none, so
    # the next search would never end.
    machine = Machine(0.009, step=TICK)
    assert run(machine, monkeypatch, searches=40) == REFUSED
    assert len(machine.timeouts) < 40
    assert min(machine.timeouts) > 0


def test_a_search_over_its_cpu_budget_is_refused_even_when_a_longer_try_finishes(monkeypatch):
    for share in (1.0, 0.5, 0.1):
        assert run(Machine(0.009, share=share), monkeypatch) == [], share
        assert run(Machine(0.015, share=share), monkeypatch) == REFUSED, share


def test_a_search_that_reports_no_timeout_still_spends_its_cpu_budget(monkeypatch):
    # The CPU time that a search spent decides, also when the engine reports no timeout.
    machine = Machine(0.0)

    def late(value: str, *, timeout: float, concurrent: bool | None = None) -> object:
        machine.cpu += 0.02
        machine.elapsed += 0.02
        return value

    machine.search = late  # type: ignore[method-assign]
    assert run(machine, monkeypatch) == REFUSED


def test_a_stepped_cpu_clock_refuses_no_valid_search(monkeypatch):
    # A 1 ms search is interrupted for 30 ms as the clock ticks, and the whole tick lands on its second try, which spent
    # 0.6 ms and lasted 20 ms.
    machine = Machine(0.001, share=1 / 31, step=TICK)
    machine.cpu = TICK - 0.000_5
    assert run(machine, monkeypatch) == []

    # A search of 9 ms is interrupted twice, for 7 ms in all, and both ticks of those 16 ms land on it.
    machine = Machine(0.009, step=TICK)

    def ticked_twice(value: str, *, timeout: float, concurrent: bool | None = None) -> object:
        machine.elapsed += 0.016
        machine.cpu += 2 * TICK
        return value

    machine.search = ticked_twice  # type: ignore[method-assign]
    assert run(machine, monkeypatch) == []

    # 20 searches of 0.1 ms, and a whole tick lands on every one: 2 ms really spent, 312 ms on the stepped clock.
    machine = Machine(0.0001, step=TICK)

    def ticked(value: str, *, timeout: float, concurrent: bool | None = None) -> object:
        machine.elapsed += machine.cost
        machine.cpu += TICK
        return value

    machine.search = ticked  # type: ignore[method-assign]
    assert run(machine, monkeypatch, searches=20) == []


@pytest.mark.parametrize("cost", [0.000_1, 0.000_125, 0.000_5, 0.003])
def test_a_stepped_cpu_clock_still_bounds_the_cpu_time_of_all_searches(cost, monkeypatch):
    # The clock shows nothing of most searches and a whole tick of a few, and those sums tell what all of them spent.
    for start in (0.0, TICK / 2, TICK - cost / 2):
        machine = Machine(cost, step=TICK)
        machine.cpu = start
        assert run(machine, monkeypatch, searches=5000) == REFUSED, start
        assert 0.1 <= machine.cpu - start <= 0.1 + 3 * TICK + cost, start
        # A document that spends less is accepted, also from a start that makes the clock show a tick too many.
        machine = Machine(cost, step=TICK)
        machine.cpu = start
        assert run(machine, monkeypatch, searches=int(0.096 / cost)) == [], start


def test_a_tick_that_another_thread_spent_refuses_no_valid_document(monkeypatch):
    # 33 searches of 3 ms spend 99 ms and show seven ticks, 109 ms, and an eighth one lands on a search that waited.
    machine = Machine(0.003, step=TICK)
    machine.cpu = TICK - 0.001
    calls = 0
    search = machine.search

    def interrupted(value: str, *, timeout: float, concurrent: bool | None = None) -> object:
        nonlocal calls
        calls += 1
        if calls == 3:
            machine.cpu += TICK
            machine.elapsed += 0.1
        return search(value, timeout=timeout, concurrent=concurrent)

    machine.search = interrupted  # type: ignore[method-assign]
    assert run(machine, monkeypatch, searches=33) == []


def test_a_runaway_search_spends_little_cpu_time_and_never_waits_for_the_interpreter():
    go, ran = threading.Event(), threading.Event()
    other = threading.Thread(target=lambda: go.wait() and ran.set())
    interval = sys.getswitchinterval()
    budget, pattern = schemas._Budget(SchemaLimits()), "(a|aa)+$"
    assert budget.search(pattern, "a")
    # From here on only a search that lets go of the interpreter lets the other thread run.
    sys.setswitchinterval(60)
    try:
        other.start()
        go.set()
        before = time.thread_time()
        with pytest.raises(schemas._Failure, match="RESOURCE_LIMIT"):
            budget.search(pattern, "a" * 5000 + "!")
        # 10 ms, and on Windows no more than a few ticks of the CPU clock.
        assert time.thread_time() - before < 0.5
        assert not ran.is_set()
    finally:
        sys.setswitchinterval(interval)
        other.join()


def test_a_search_keeps_the_interpreter_up_to_its_size_bound_and_not_past_it():
    go, ran = threading.Event(), threading.Event()
    other = threading.Thread(target=lambda: go.wait() and ran.set())
    interval, pattern = sys.getswitchinterval(), "(a|aa)+$"

    def runaway(length: int) -> None:
        with pytest.raises(schemas._Failure, match="RESOURCE_LIMIT"):
            schemas._Budget(SchemaLimits()).search(pattern, "a" * (length - 1) + "!")

    # From here on only a search that lets go of the interpreter lets the other thread run.
    sys.setswitchinterval(60)
    try:
        other.start()
        go.set()
        for _ in range(20):
            runaway(32_768)
        assert not ran.is_set()
        # A busy machine may not give the other thread a processor during the first search.
        for _ in range(500):
            if not ran.is_set():
                runaway(32_769)
        assert ran.is_set()
    finally:
        sys.setswitchinterval(interval)
        other.join()


def test_a_search_of_a_long_string_lets_the_other_threads_of_the_process_run():
    # A search that keeps the interpreter stops the other threads while it goes through the whole string.
    longest, done = 0.0, threading.Event()

    def beat() -> None:
        nonlocal longest
        last = time.perf_counter()
        while not done.is_set():
            time.sleep(0.001)
            now = time.perf_counter()
            longest, last = max(longest, now - last), now

    other = threading.Thread(target=beat)
    other.start()
    started = time.perf_counter()
    issues = schemas.validate_payload({"pattern": r"\w+\s"}, "a" * 1_000_000, {})
    lasted = time.perf_counter() - started
    done.set()
    other.join()
    assert [issue.code for issue in issues] == REFUSED
    # A search that keeps the interpreter stops the other thread from its start to its end, however busy the machine is.
    assert longest < lasted / 2, (longest, lasted)


def test_a_search_near_its_limit_is_judged_by_this_computers_own_cpu_clock(monkeypatch):
    steps = []
    for _ in range(5):
        before = time.thread_time()
        while (now := time.thread_time()) == before:
            pass
        steps.append(now - before)
    # The smallest one: a virtual machine may charge its own pauses to a thread.
    step = min(steps)

    def validated(spent: float) -> list[str]:
        """The codes of two searches of ``spent`` seconds of CPU and 200 ms, the first one as the clock moves."""
        machine = Machine(0.0, step=step)
        machine.cpu = step * 0.999

        def search(value: str, *, timeout: float, concurrent: bool | None = None) -> object:
            machine.cpu += spent
            machine.elapsed += 0.2
            return value

        machine.search = search  # type: ignore[method-assign]
        monkeypatch.setattr(schemas, "time", machine)
        monkeypatch.setattr(schemas, "_pattern", lambda *_, **__: machine)
        # The allowance for the clock is the one that the module takes on this computer.
        return [issue.code for issue in schemas._payload({"items": {"pattern": "a"}}, ["a"] * 2, {}, SchemaLimits())]

    assert validated(0.009) == []
    assert validated(0.01 + 3 * step) == REFUSED
