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

"""Which errors of a scan are one item's own, and how long that item then waits."""

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError, PendingRollbackError

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.access import Scope
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.telemetry import COUNTERS, attributes
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime import run_failures
from firefly_weave.runtime.capacity import RuntimeCapacityError
from firefly_weave.runtime.kernel import KernelError, checked_deadline
from firefly_weave.runtime.run_failures import FIRST_WAIT, LONGEST_WAIT, RunFailures, unexpected


def refused(sqlstate, *, lost=False):
    original = Exception()
    original.sqlstate = sqlstate
    return DBAPIError("statement", {}, original, connection_invalidated=lost)


@pytest.mark.parametrize(
    ("error", "contained"),
    [
        (KeyError("@join:move"), True),
        (KernelError("WV-RUNTIME-EVENT"), True),
        (RecursionError(), True),
        (refused("23505"), True),
        (refused("22P02"), True),
        (refused("54001"), True),
        (refused("WQ002"), True),
        (CatalogError(404, "WV-NOT-FOUND", "no"), True),
        (CatalogError(409, "WV-CONFLICT", "no"), True),
        (CatalogError(422, "WV-VALIDATION", "no"), True),
        (refused("23505", lost=True), False),
        (refused("WQ002", lost=True), False),
        (refused("WQ003"), False),
        (refused("53200"), False),
        (refused("08006"), False),
        (refused("57P01"), False),
        (refused("40P01"), False),
        (refused("55P03"), False),
        (refused(None), False),
        (PendingRollbackError(), False),
        (ConnectionResetError(), False),
        (TimeoutError(), False),
        (CatalogError(429, "WV-OPERATION-CAPACITY", "no"), False),
        (CatalogError(503, "WV-UNAVAILABLE", "no"), False),
        (RuntimeCapacityError(), False),
        (AccessDenied(), False),
    ],
)
def test_only_an_items_own_error_is_contained(error, contained):
    assert unexpected(error) is contained


@pytest.mark.parametrize(
    ("error", "contained"),
    [
        (refused("WQ001"), True),
        (refused("55P03"), True),
        (refused("WQ001", lost=True), False),
        (refused("40P01"), False),
        (CatalogError(429, "WV-OPERATION-CAPACITY", "no"), False),
    ],
)
def test_only_the_databases_refusal_of_an_items_write_passes_for_now(error, contained):
    assert run_failures.refused(error) is contained


def test_the_log_names_an_error_by_class_code_and_line_never_by_its_message():
    with pytest.raises(KernelError) as raised:
        checked_deadline(datetime.max.replace(tzinfo=UTC), 1)
    assert re.fullmatch(
        r"KernelError WV-[A-Z-]+ at firefly_weave/runtime/kernel\.py:\d+", run_failures.cause(raised.value)
    )
    with pytest.raises(KeyError) as outside:
        {}["secret"]
    assert run_failures.cause(outside.value) == "KeyError"
    assert run_failures.cause(CatalogError(422, "WV-VALIDATION", "secret")) == "CatalogError WV-VALIDATION"
    assert run_failures.cause(refused("54001")) == "DBAPIError 54001"


async def test_the_log_names_the_environment_and_the_run_behind_an_item(caplog):
    failures, tx, run, call, caller = RunFailures(), opened(), uuid4(), uuid4(), uuid4()
    await failures.guard(tx, "retry", run, fails)
    await failures.guard(tx, "dispatch", call, fails, name=f"call {call} of run {caller}")
    where = f"in environment {tx.scope.environment_id} (KeyError at firefly_weave/runtime/run_failures.py:"
    first, second = caplog.messages
    assert first.startswith(f"Recovery failed: retry of run {run} {where}")
    assert second.startswith(f"Recovery failed: dispatch of call {call} of run {caller} {where}")


async def test_a_refusal_that_passes_is_no_defect_in_the_log(caplog):
    async def spent():
        raise refused("WQ001")

    failures, tx, item = RunFailures(), opened(), uuid4()
    assert await failures.guard(tx, "retry", item, spent) is None
    assert failures.waiting(tx, "retry") == [item]
    [record] = caplog.records
    assert record.levelname == "WARNING" and "refused for now" in record.getMessage() and "WQ001" in record.getMessage()


class Session:
    def __init__(self):
        self.info = {}

    @asynccontextmanager
    async def begin_nested(self):
        yield

    def get_transaction(self):
        return SimpleNamespace(is_active=True)


def opened(environment=None):
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=environment or uuid4())
    return Transaction(session=Session(), scope=scope)


async def fails():
    raise KeyError("x")


async def works():
    return 7


async def test_the_wait_doubles_to_a_bound_and_a_success_forgets_it():
    failures, now = RunFailures(), [0.0]
    failures.clock = lambda: now[0]
    tx, item, waits = opened(), uuid4(), []
    for _ in range(9):
        assert await failures.guard(tx, "retry", item, fails) is None
        start = now[0]
        while failures.waiting(tx, "retry"):
            now[0] += 5
        waits.append(now[0] - start)
    assert waits == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert await failures.guard(tx, "retry", item, works) == 7
    await failures.guard(tx, "retry", item, fails)
    now[0] += 5
    assert failures.waiting(tx, "retry") == []


async def test_an_item_waits_only_for_its_kind_of_work_and_in_its_environment():
    failures, tx, item = RunFailures(), opened(), uuid4()
    await failures.guard(tx, "retry", item, fails)
    assert failures.waiting(tx, "retry") == [item]
    assert failures.waiting(tx, "deadline") == failures.waiting(opened(), "retry") == []


async def test_an_item_that_still_waits_is_never_forgotten():
    # However many fail, each one keeps its own wait.
    failures, tx = RunFailures(), opened()
    items = [uuid4() for _ in range(1001)]
    for item in items:
        await failures.guard(tx, "retry", item, fails)
    assert failures.waiting(tx, "retry") == items


@pytest.mark.parametrize(("idle", "wait"), [(LONGEST_WAIT - 1, 4 * FIRST_WAIT), (LONGEST_WAIT, FIRST_WAIT)])
async def test_an_item_not_tried_again_long_after_its_wait_is_forgotten(idle, wait):
    failures, now = RunFailures(), [0.0]
    failures.clock = lambda: now[0]
    tx, item = opened(), uuid4()
    await failures.guard(tx, "retry", item, fails)
    now[0] += FIRST_WAIT
    await failures.guard(tx, "retry", item, fails)
    now[0] += 2 * FIRST_WAIT + idle
    assert failures.waiting(tx, "retry") == []
    await failures.guard(tx, "retry", item, fails)
    now[0] += wait - 1
    assert failures.waiting(tx, "retry") == [item]
    now[0] += 1
    assert failures.waiting(tx, "retry") == []


async def test_an_error_with_no_run_to_put_off_ends_the_scan():
    with pytest.raises(KeyError):
        await RunFailures().guard(opened(), "retry", None, fails)


async def test_a_scan_canceled_while_an_item_fails_stops_there():
    failures, tx, item = RunFailures(), opened(), uuid4()

    async def replaces_its_cancellation():
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise KeyError from None

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(failures.guard(tx, "retry", item, replaces_its_cancellation))
    assert failures.waiting(tx, "retry") == []


async def test_effects_registered_before_the_error_are_dropped():
    failures, tx = RunFailures(), opened()
    tx.session.info["weave_after_commit"] = ["kept"]

    async def half():
        tx.session.info["weave_after_commit"].append("dropped")
        tx.session.info["weave_runtime_candidate"] = uuid4()
        raise KeyError("x")

    await failures.guard(tx, "deadline", uuid4(), half)
    assert tx.session.info["weave_after_commit"] == ["kept"]
    assert tx.session.info["weave_runtime_candidate"] is None


@pytest.mark.parametrize("kind", ["deadline", "cancel", "expired", "retry", "dispatch", "event_start"])
def test_every_kind_of_work_has_its_own_metric_label(kind):
    assert COUNTERS["recovery_failure"] == "weave.recovery_failures"
    assert attributes(f"recovery.{kind}", "failed")["operation"] == f"recovery.{kind}"
