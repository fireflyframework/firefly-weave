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


"""Worker handlers that take part in Weave transactions, made safe with a fence in their own PostgreSQL database.

Each handler makes its change and records the request in one database transaction, so a repeated request answers
as the first, a cancel or undo of a step that never arrived leaves a tombstone, and a step that arrives after its
cancel or undo is refused with nothing applied.

The database needs the fence's table (``Fence().ddl()``) and the tables in ``TABLES``. Check the handlers before
you deploy them:

    weave transactions conformance move.workflow.yaml --step hold --catalog catalog.lock.json \\
        --input input.json --handlers transaction_participant:handlers
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from firefly_weave.contracts.values import JsonValue
from firefly_weave.contracts.workers import TaskLease
from firefly_weave.sdk.participants import Fence
from firefly_weave.sdk.worker import TaskHandler

TABLES = """
CREATE TABLE IF NOT EXISTS holds (
  hold_id text PRIMARY KEY,
  reference text NOT NULL,
  quantity integer NOT NULL,
  status text NOT NULL
);
CREATE TABLE IF NOT EXISTS additions (
  add_id text PRIMARY KEY,
  reference text NOT NULL,
  quantity integer NOT NULL
);
"""

fence = Fence()


@asynccontextmanager
async def transaction() -> AsyncIterator[asyncpg.Connection]:
    """One database transaction for one request: the change and the fence's row commit together."""
    connection = await asyncpg.connect(os.environ["PARTICIPANT_DATABASE_URL"])
    try:
        async with connection.transaction():
            yield connection
    finally:
        await connection.close()


async def hold(lease: TaskLease) -> JsonValue:
    async with transaction() as connection:
        entry = await fence.begin(connection, lease.operation_key, lease.transaction)
        if entry.replay:
            return entry.result
        await connection.execute(
            "INSERT INTO holds (hold_id, reference, quantity, status) VALUES ($1, $2, $3, 'held')",
            lease.operation_key,
            lease.input["reference"],
            lease.input["quantity"],
        )
        return await fence.finish(connection, entry, {"holdId": lease.operation_key})


async def confirm_hold(lease: TaskLease) -> JsonValue:
    async with transaction() as connection:
        entry = await fence.begin(connection, lease.operation_key, lease.transaction)
        if entry.replay:
            return entry.result
        await connection.execute("UPDATE holds SET status = 'confirmed' WHERE hold_id = $1", lease.input["holdId"])
        return await fence.finish(connection, entry, {})


async def release_hold(lease: TaskLease) -> JsonValue:
    async with transaction() as connection:
        entry = await fence.begin(connection, lease.operation_key, lease.transaction)
        if entry.replay:
            return entry.result
        # Without the hold there is nothing to release; the fence has recorded that it must never be made.
        if entry.applied:
            await connection.execute("DELETE FROM holds WHERE hold_id = $1", lease.input["holdOperation"])
        return await fence.finish(connection, entry, {})


async def add(lease: TaskLease) -> JsonValue:
    async with transaction() as connection:
        entry = await fence.begin(connection, lease.operation_key, lease.transaction)
        if entry.replay:
            return entry.result
        await connection.execute(
            "INSERT INTO additions (add_id, reference, quantity) VALUES ($1, $2, $3)",
            lease.operation_key,
            lease.input["reference"],
            lease.input["quantity"],
        )
        return await fence.finish(connection, entry, {"id": lease.operation_key})


async def remove(lease: TaskLease) -> JsonValue:
    async with transaction() as connection:
        entry = await fence.begin(connection, lease.operation_key, lease.transaction)
        if entry.replay:
            return entry.result
        if entry.applied:
            await connection.execute("DELETE FROM additions WHERE add_id = $1", lease.input["addOperation"])
        return await fence.finish(connection, entry, {})


def handlers() -> dict[str, TaskHandler]:
    """The handlers by capability, as ``Worker(handlers=...)`` and ``--handlers`` take them."""
    return {
        "source-system-hold-quantity@1.0.0": hold,
        "source-system-confirm-hold@1.0.0": confirm_hold,
        "source-system-release-hold@1.0.0": release_hold,
        "target-system-add-quantity@1.0.0": add,
        "target-system-remove-quantity@1.0.0": remove,
    }
