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

"""Recovery owns its pure execution slot apart from request admission and names its failures."""

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.runtime import RecoveryReport
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.execution import CONTROL_SLOTS, WORK_SLOTS, execute_pure, request_execution
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.kernel import transition_async
from firefly_weave.runtime.models import RuntimeEvent
from firefly_weave.runtime.recovery import RecoveryService
from firefly_weave.runtime.scheduler import RECOVERY_EXECUTION, RecoveryLoop

SCHEDULER_LOGGER = "firefly_weave.runtime.scheduler"


class _Session:
    """Just enough session for the catalog authority check and the candidate marker."""

    def __init__(self, tenant):
        self.tenant, self.info = tenant, {}

    def get_transaction(self):
        return SimpleNamespace(is_active=True)

    async def execute(self, statement, params=None):
        return None

    async def scalar(self, statement, params=None):
        return str(self.tenant)


class _View:
    def __init__(self, state):
        self.state = state

    def model_copy(self, *, update):
        return _View(update["state"])


class _Harness:
    """Real loop, recovery quanta and kernel admission; storage and the pure kernel are fakes."""

    def __init__(self, monkeypatch, *, expired=1, deadlines=0, recovery_errors=(), schedule_errors=()):
        self.tenant = uuid4()
        self.scope = Scope(tenant_id=self.tenant, project_id=uuid4(), environment_id=uuid4())
        self.authority = _SchedulerScope(scope=self.scope, issuer=_CATALOG_AUTHORITY)
        self.expired = [uuid4() for _ in range(expired)]
        self.deadlines = deadlines
        self.transitions, self.persisted, self.schedule_scans = [], [], []
        self.recovery_failures = []
        self.recovery_errors = list(recovery_errors)
        self.schedule_errors = list(schedule_errors)
        harness = self

        class Repository:
            def __init__(self, tx, outbox):
                pass

            async def expired_ready(self, limit):
                return harness.expired[:limit]

            async def now(self):
                return datetime.now(UTC)

            async def persist(self, view, event, result, digest, **timing):
                harness.persisted.append(event.type)

            async def task_status(self, identifier, status, *, at=None):
                harness.expired.remove(identifier)

        def transition(state, event, artifact):
            harness.transitions.append(event.type)
            return SimpleNamespace(state=state)

        monkeypatch.setattr("firefly_weave.runtime.recovery.RuntimeRepository", Repository)
        monkeypatch.setattr("firefly_weave.runtime.recovery.view_of", lambda row: _View(row["state"]))
        monkeypatch.setattr("firefly_weave.runtime.recovery.import_artifact", lambda artifact: None)
        # `transition_async` resolves the pure reducer at call time, inside the admitted thread.
        monkeypatch.setattr("firefly_weave.runtime.kernel.transition", transition)

        async def tenant_page(engine):
            return [self.tenant]

        async def next_scope(uow, tenant):
            return self.authority

        monkeypatch.setattr("firefly_weave.runtime.scheduler.tenant_page", tenant_page)
        monkeypatch.setattr("firefly_weave.runtime.scheduler.next_scope", next_scope)

        @asynccontextmanager
        async def transaction(scope, principal):
            yield Transaction(session=_Session(scope.tenant_id), scope=scope)

        async def lock_task(tx, identifier, *, skip_locked):
            row = {"id": uuid4(), "artifact": {}, "state": SimpleNamespace(status="waiting", accepted_sequence=3)}
            return row, {"status": "ready", "node_id": "approval"}

        async def observe_unavailable(tx, row):
            return False

        async def deadlines(tx, limit, *, terminal_only=False):
            if self.recovery_errors:
                raise self.recovery_errors.pop(0)
            if not self.deadlines:
                return RecoveryReport()
            # Like `waits.settle`: a timed-out deadline reduces on the control pool.
            self.deadlines -= 1
            state = SimpleNamespace(status="waiting", accepted_sequence=3)
            event = RuntimeEvent(id=uuid4(), type="timed_out", timestamp=datetime.now(UTC), sequence=4)
            await transition_async(state, event, None)  # type: ignore[arg-type]
            return RecoveryReport(timed_out=1)

        async def recovery_candidates(tx, limit):
            return []

        runtime = SimpleNamespace(
            definitions=SimpleNamespace(
                transaction=transaction, registry=SimpleNamespace(require_operational=lambda: None), outbox=None
            ),
            lock_task=lock_task,
            observe_unavailable=observe_unavailable,
        )
        self.recovery = RecoveryService(
            runtime,
            SimpleNamespace(recovery_candidates=recovery_candidates),
            SimpleNamespace(scan=deadlines),
            None,
        )
        scan = self.recovery._scan_scheduled

        async def observed_scan(authority, limit):
            try:
                return await scan(authority, limit)
            except Exception as error:
                self.recovery_failures.append(error)
                raise

        monkeypatch.setattr(self.recovery, "_scan_scheduled", observed_scan)

        async def schedule_scan(authority, limit):
            self.schedule_scans.append(authority.scope)
            if self.schedule_errors:
                raise self.schedule_errors.pop(0)

        self.loop = RecoveryLoop(
            SimpleNamespace(database_timeout_seconds=5.0, scheduler_poll_seconds=0.01),
            None,
            self.recovery,
            SimpleNamespace(_scan_scheduled=schedule_scan),
        )
        self.loop.engine = object()

    async def cycle_while_requests_hold_every_slot(self, *, control=False):
        admitted = [asyncio.Event() for _ in range(CONTROL_SLOTS if control else WORK_SLOTS)]
        finish = asyncio.Event()

        async def in_flight_request(ready):
            # Each request owns one slot for its whole response, like `BodyBoundary._serve`.
            async with request_execution(control=control):
                ready.set()
                await finish.wait()

        # Separate tasks, so the loop does not inherit a request lease from this context.
        requests = [asyncio.create_task(in_flight_request(ready)) for ready in admitted]
        try:
            async with asyncio.timeout(2):
                for ready in admitted:
                    await ready.wait()
            await self.loop.cycle()
        finally:
            finish.set()
            await asyncio.gather(*requests)


async def test_recovery_cycle_progresses_while_in_flight_requests_hold_every_work_slot(monkeypatch):
    harness = _Harness(monkeypatch)

    await harness.cycle_while_requests_hold_every_slot()

    assert harness.recovery_failures == []
    assert harness.transitions == ["incident_opened"] and harness.persisted == ["incident_opened"]
    assert harness.schedule_scans == [harness.scope]


async def test_recovery_deadlines_progress_while_in_flight_requests_hold_every_control_slot(monkeypatch):
    harness = _Harness(monkeypatch, expired=0, deadlines=1)

    await harness.cycle_while_requests_hold_every_slot(control=True)

    assert harness.recovery_failures == []
    assert harness.transitions == ["timed_out"]
    assert harness.schedule_scans == [harness.scope]


async def orphaned_recovery_calls(count):
    """Recovery pure calls that outlived their cancelled quanta and still run in their threads."""
    entered, release = threading.Semaphore(0), threading.Event()

    async def quantum():
        async with RECOVERY_EXECUTION.execution():
            await execute_pure(lambda: entered.release() or release.wait(5))

    quanta = [asyncio.create_task(quantum()) for _ in range(count)]
    for _ in quanta:
        assert await asyncio.to_thread(entered.acquire, True, 2)
    for task in quanta:
        task.cancel()
    return release, quanta


async def test_recovery_tolerates_one_call_that_outlived_its_cancelled_quantum(monkeypatch):
    harness = _Harness(monkeypatch)
    release, quanta = await orphaned_recovery_calls(1)
    try:
        await harness.cycle_while_requests_hold_every_slot()
    finally:
        release.set()
        await asyncio.gather(*quanta, return_exceptions=True)

    assert harness.recovery_failures == []
    assert harness.transitions == ["incident_opened"]


async def test_recovery_quanta_are_refused_while_orphaned_calls_hold_the_whole_reservation(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=SCHEDULER_LOGGER)
    harness = _Harness(monkeypatch)
    release, quanta = await orphaned_recovery_calls(RECOVERY_EXECUTION.size)
    try:
        await harness.loop.cycle()
    finally:
        release.set()
        await asyncio.gather(*quanta, return_exceptions=True)

    # The bound holds: no third recovery thread starts, and the refusal is named.
    [failure] = harness.recovery_failures
    assert (failure.status, failure.code) == (429, "WV-OPERATION-CAPACITY")
    assert [record.getMessage() for record in caplog.records] == [
        "Tenant recovery scan failed (CatalogError WV-OPERATION-CAPACITY); traversal will continue"
    ]
    assert harness.transitions == [] and harness.schedule_scans == []
    async with asyncio.timeout(2):
        while not harness.transitions:
            # The released threads give their slots back just after their calls return.
            await harness.loop.cycle()
            await asyncio.sleep(0.01)
    assert harness.transitions == ["incident_opened"]


async def test_poll_logs_a_failed_catalog_scan_by_class_and_code(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=SCHEDULER_LOGGER)
    harness = _Harness(monkeypatch)

    async def tenant_page(engine):
        raise CatalogError(429, "WV-OPERATION-CAPACITY", "Catalog sentinel unavailable")

    monkeypatch.setattr("firefly_weave.runtime.scheduler.tenant_page", tenant_page)
    poll = asyncio.create_task(harness.loop.poll())
    try:
        async with asyncio.timeout(2):
            while not caplog.records:
                await asyncio.sleep(0.01)
    finally:
        poll.cancel()
        await asyncio.gather(poll, return_exceptions=True)

    assert caplog.records[0].getMessage() == (
        "Recovery catalog scan failed (CatalogError WV-OPERATION-CAPACITY); retrying on next poll"
    )
    assert "sentinel" not in caplog.text


async def test_tenant_scan_failures_log_error_class_and_code_without_values(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=SCHEDULER_LOGGER)
    harness = _Harness(
        monkeypatch,
        recovery_errors=[CatalogError(429, "WV-OPERATION-CAPACITY", "Operation capacity sentinel")],
        schedule_errors=[
            CatalogError(409, "WV-OPERATION-UNAVAILABLE", "Schedule nightly-sentinel unavailable"),
            RuntimeError("tenant-sentinel"),
        ],
    )

    for _ in range(3):
        await harness.loop.cycle()

    messages = [record.getMessage() for record in caplog.records if record.name == SCHEDULER_LOGGER]
    assert messages == [
        "Tenant recovery scan failed (CatalogError WV-OPERATION-CAPACITY); traversal will continue",
        "Tenant schedule scan failed (CatalogError WV-OPERATION-UNAVAILABLE); traversal will continue",
        "Tenant schedule scan failed (RuntimeError); traversal will continue",
    ]
    assert "sentinel" not in caplog.text
