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

"""Cancelling a caller while its child finishes and the scans run never deadlocks and never loses an outcome."""

import asyncio
import os

import pytest
from sqlalchemy import text
from test_workflow_call_runtime import HOLD, activate, call, caller, calls, release, start, state
from test_workflow_call_runtime import platform as platform

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration
REPETITIONS = int(os.environ.get("WEAVE_CALL_SOAK_REPETITIONS", "200"))


async def retrying(operation) -> None:
    """Run ``operation`` again after a capacity refusal, as clients and the recovery loop do."""
    from firefly_weave.definitions.models import capacity_rejected

    for _ in range(50):
        try:
            return await operation()
        except Exception as error:  # noqa: BLE001 - only a capacity refusal is retried
            if not capacity_rejected(error):
                raise
            await asyncio.sleep(0.02)
    raise AssertionError("Capacity refusals did not stop")


async def race(platform, authority, run_id, child_id) -> None:
    """Cancel the caller, finish the child and scan, all at once."""
    from firefly_weave.definitions.models import CatalogError

    async def cancel_caller() -> None:
        async with platform.runtime.definitions.transaction(platform.scope, None) as tx:
            try:
                await platform.runtime.cancel(
                    tx, run_id, "race", actor=platform.actor, scope=platform.scope, context=AuditContext()
                )
            except CatalogError as error:
                # The caller may already have succeeded with its child's result.
                if error.code != "WV-RUNTIME-TERMINAL":
                    raise

    async def finish_child() -> None:
        try:
            await release(platform, child_id)
        except CatalogError as error:
            # The child may already be cancelled because its caller ended.
            if error.status != 409:
                raise

    await asyncio.wait_for(
        asyncio.gather(
            retrying(cancel_caller),
            retrying(finish_child),
            retrying(lambda: platform.recovery._scan_scheduled(authority, 10)),
        ),
        timeout=30,
    )


async def test_caller_cancel_child_completion_and_scans_race_without_deadlock(platform):
    from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope

    leaf = await activate(platform, HOLD)
    flow = await activate(platform, caller("orders", call("hold", "hold@1.0.0")), hold=leaf.id)
    authority = _SchedulerScope(platform.scope, _CATALOG_AUTHORITY)
    outcomes = {"cancelled": 0, "succeeded": 0}
    for _ in range(REPETITIONS):
        run = await start(platform, flow)
        await retrying(lambda: platform.recovery._scan_scheduled(authority, 10))
        [row] = await calls(platform, run.id)
        await race(platform, authority, run.id, row["child_run_id"])
        for _ in range(3):
            await retrying(lambda: platform.recovery._scan_scheduled(authority, 10))
        caller_state = await state(platform, run.id)
        child_state = await state(platform, row["child_run_id"])
        [row] = await calls(platform, run.id)
        assert caller_state["status"] in {"cancelled", "succeeded"}
        assert child_state["status"] in {"cancelled", "succeeded"}
        # Whatever won, the call ends in a final state and nothing is left to deliver or to cancel.
        assert row["status"] in {"succeeded", "cancelled", "abandoned"}
        outcomes[caller_state["status"]] += 1
    async with platform.access_db[1]() as session:
        waiting = await session.scalar(
            text("SELECT count(*) FROM runs WHERE state->>'status' IN ('queued','waiting','suspended')")
        )
    assert waiting == 0, outcomes
    assert sum(outcomes.values()) == REPETITIONS
