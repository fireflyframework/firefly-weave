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

"""Loop counters: collected and cancelled iterations and yields, counted per transition."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from test_loop_runtime import NOW, Run, compiled, each, send

from firefly_weave.contracts.access import Scope
from firefly_weave.operations.telemetry import TelemetryService, WeaveMetricsRecorder
from firefly_weave.persistence.uow import Transaction, UnitOfWork
from firefly_weave.runtime.models import RuntimeEvent
from firefly_weave.runtime.repository import RuntimeRepository, loop_activity


def test_a_transition_counts_the_iterations_it_collected_and_the_yields_it_registered():
    flow = compiled([each("each", [], {"ref": "/item"}, maxItems=10_000)], {"ref": "/steps/each/output"})
    run = Run(flow, list(range(2100)))
    assert loop_activity(run.start()) == (2000, 0, 1)
    assert loop_activity(
        run.send("wait_elapsed", node_id="each~1", deadline=run.state.waits["each~1"].isoformat())
    ) == (
        100,
        0,
        0,
    )


def test_a_run_that_ends_counts_its_cancelled_iterations(worker_runtime_fixture):
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"}, concurrency=3)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, [1, 2, 3, 4])
    assert loop_activity(run.start()) == (0, 0, 0)
    assert loop_activity(run.complete("send[1]", 2)) == (1, 0, 0)
    assert loop_activity(run.send("cancelled")) == (0, 3, 0)


def test_loop_counters_add_their_counts_after_commit():
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    service = TelemetryService(WeaveMetricsRecorder(meters), TracerProvider(shutdown_on_exit=False))
    pending: list = []
    service.after_commit(pending.append, "loop_iteration", operation="loop", status="ok", amount=2000)
    service.after_commit(pending.append, "loop_yield", operation="loop", status="ok", amount=1)
    for callback in pending:
        callback()
    points = {
        metric.name: [(dict(point.attributes), point.value) for point in metric.data.data_points]
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    labels = {"operation": "loop", "status": "ok", "error_code": "none"}
    assert points == {"weave.loop_iterations": [(labels, 2000)], "weave.loop_yields": [(labels, 1)]}
    for amount in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            service.record("loop_iteration", operation="loop", amount=amount)
    meters.shutdown()


def test_every_way_a_run_ends_counts_the_iterations_it_stopped(worker_runtime_fixture):
    check = {
        "id": "check",
        "kind": "switch",
        "cases": [
            {
                "when": {"op": {"name": "eq", "args": [{"ref": "/steps/send/output"}, {"literal": 0}]}},
                "steps": [{"id": "stop", "kind": "fail", "code": "ZERO", "message": "Zero result."}],
                "output": {"literal": None},
            }
        ],
        "default": {"steps": [], "output": {"ref": "/steps/send/output"}},
    }
    flow = compiled(
        [each("each", [send(), check], {"ref": "/steps/check/output"}, concurrency=3)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    failed = Run(flow, [1, 2, 3, 4])
    failed.start()
    # The failing iteration and the two still running all end with the run.
    assert loop_activity(failed.complete("send[1]", 0)) == (0, 3, 0)
    timed_out = Run(flow, [1, 2, 3, 4])
    timed_out.start()
    assert loop_activity(timed_out.send("timed_out", deadline=NOW.isoformat())) == (0, 3, 0)


class Session:
    """The parts of an async session a transaction and a repository use, recording nothing but statements."""

    def __init__(self) -> None:
        self.info: dict = {}

    async def execute(self, statement, parameters=None):
        return SimpleNamespace(mappings=lambda: [], scalar=lambda: 0)

    def get_transaction(self):
        return SimpleNamespace(is_active=True)

    def in_transaction(self) -> bool:
        return True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    @asynccontextmanager
    async def begin(self):
        yield self

    @asynccontextmanager
    async def begin_nested(self):
        yield self


@pytest.fixture
def counters():
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    service = TelemetryService(WeaveMetricsRecorder(meters), TracerProvider(shutdown_on_exit=False))

    def points() -> dict:
        data = reader.get_metrics_data()
        return {
            (metric.name, point.attributes["status"]): point.value
            for resource in (data.resource_metrics if data else [])
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name.startswith("weave.loop")
            for point in metric.data.data_points
        }

    yield service, points
    meters.shutdown()


SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


async def persisted(tx: Transaction, monkeypatch, view, event, result, **options) -> None:
    from firefly_weave.files import authority
    from firefly_weave.human_tasks import persistence
    from firefly_weave.workers import control

    nothing = AsyncMock(return_value=None)
    for module, name in ((authority, "require_run_files"), (persistence, "close_tasks"), (control, "revoke_attempts")):
        monkeypatch.setattr(module, name, nothing)
    repository = RuntimeRepository(tx, SimpleNamespace(append=nothing))
    monkeypatch.setattr(repository, "project_incidents", nothing)
    monkeypatch.setattr(repository, "stopped_iterations", AsyncMock(return_value=3), raising=False)
    await repository.persist(view, event, result, "hash", **options)


def view() -> SimpleNamespace:
    """A run view as persistence reads it: an identifier and a stored response."""
    return SimpleNamespace(id=uuid4(), model_dump_json=lambda: "{}")


def pure_loop_start() -> tuple:
    flow = compiled([each("each", [], {"ref": "/item"}, maxItems=10_000)], {"ref": "/steps/each/output"})
    run = Run(flow, list(range(2100)))
    result = run.start()
    return run, result


def test_a_persisted_transition_records_its_loop_counters_only_once_it_commits(counters, monkeypatch):
    service, points = counters
    run, result = pure_loop_start()
    session = Session()
    tx = Transaction(session=session, scope=SCOPE, telemetry=service)
    event = RuntimeEvent(id=uuid4(), sequence=1, type="started", data={}, timestamp=NOW)
    asyncio.run(persisted(tx, monkeypatch, view(), event, result))
    assert points() == {}
    for callback in session.info.pop("weave_after_commit"):
        callback()
    assert points() == {("weave.loop_iterations", "ok"): 2000, ("weave.loop_yields", "ok"): 1}


def test_a_rolled_back_transition_records_no_loop_counters(counters, monkeypatch):
    service, points = counters
    run, result = pure_loop_start()
    session = Session()
    uow = UnitOfWork(lambda: session, service)  # type: ignore[arg-type]
    event = RuntimeEvent(id=uuid4(), sequence=1, type="started", data={}, timestamp=NOW)

    async def rolled_back() -> None:
        async with uow.open(SCOPE) as tx:
            await persisted(tx, monkeypatch, view(), event, result)
            raise LookupError("roll back")

    with pytest.raises(LookupError):
        asyncio.run(rolled_back())
    assert points() == {}


def test_parallel_branch_outputs_are_not_loop_iterations(worker_runtime_fixture):
    both = {
        "id": "both",
        "kind": "parallel",
        "concurrency": 2,
        "branches": {
            "x": {"steps": [], "output": {"literal": 1}},
            "y": {"steps": [], "output": {"literal": 2}},
        },
    }
    result = Run(compiled([both], {"ref": "/steps/both/output"}), None).start()
    assert sum(getattr(c, "kind", None) == "accept_output" for c in result.commands) == 2
    assert loop_activity(result) == (0, 0, 0)


def test_cancelling_an_unavailable_run_counts_the_iterations_its_stored_state_was_running(counters, monkeypatch):
    from firefly_weave.contracts.runtime import UnavailableRunAcknowledgment
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, TerminalControl

    service, points = counters
    run_id = uuid4()
    state = RunState(status="waiting", accepted_sequence=1, unavailable=True)
    event = RuntimeEvent(id=uuid4(), type="cancelled", timestamp=NOW, sequence=2, data={})
    result = transition(state, event, None)
    session = Session()
    tx = Transaction(session=session, scope=SCOPE, telemetry=service)
    view = TerminalControl(id=run_id, scope=SCOPE, state=state)
    acknowledgment = UnavailableRunAcknowledgment(id=run_id, accepted_sequence=2)
    asyncio.run(persisted(tx, monkeypatch, view, event, result, safe_response=acknowledgment))
    for callback in session.info.pop("weave_after_commit"):
        callback()
    assert points() == {("weave.loop_iterations", "cancelled"): 3}


def test_iterations_a_cancelled_run_stopped_are_recorded_as_cancelled(counters, monkeypatch, worker_runtime_fixture):
    service, points = counters
    flow = compiled(
        [each("each", [send()], {"ref": "/steps/send/output"}, concurrency=3)],
        {"ref": "/steps/each/output"},
        catalog=worker_runtime_fixture["catalog"],
    )
    run = Run(flow, [1, 2, 3, 4])
    run.start()
    result = run.send("cancelled")
    session = Session()
    tx = Transaction(session=session, scope=SCOPE, telemetry=service)
    event = RuntimeEvent(id=uuid4(), sequence=2, type="cancelled", data={}, timestamp=NOW)
    asyncio.run(persisted(tx, monkeypatch, view(), event, result))
    for callback in session.info.pop("weave_after_commit"):
        callback()
    assert points() == {("weave.loop_iterations", "cancelled"): 3}
