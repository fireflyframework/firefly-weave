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


"""A fence for workers whose changes take part in Weave transactions, kept in the worker's own PostgreSQL database.

Weave sends each request of a step at least once, and the network may reorder them: a repeat, a cancel or undo of
a step that never arrived, the step itself after its cancel. A handler wraps its change in ``Fence.begin`` and
``Fence.finish`` inside its own database transaction. ``begin`` records the request by its operation key and says
what to do: answer with the first answer (``replay``), undo the step (``applied``), or nothing; it refuses a step
that arrives after its cancel or undo, and a confirm or cancel in the other direction. The fence's row commits with
the handler's change, or rolls back with it.

The fence speaks asyncpg's connection API (``$1`` placeholders, ``fetchrow``, ``execute``, ``is_in_transaction``)
and imports no driver.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.transactions import PhaseOperation
from firefly_weave.contracts.values import JsonValue
from firefly_weave.contracts.workers import LeaseTransaction

FENCE_TABLE: Final = "weave_fence.operations"
type FenceState = Literal["tried", "confirmed", "cancelled", "tombstone"]
FENCE_STATES: Final[tuple[FenceState, ...]] = ("tried", "confirmed", "cancelled", "tombstone")

_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}\.[a-z_][a-z0-9_]{0,62}")
_FORWARD: Final = frozenset({"try", "forward"})
# The state a phase's own row records: the direction it takes the step in.
_PHASE_STATES: Final[Mapping[PhaseOperation, FenceState]] = {
    "try": "tried",
    "forward": "tried",
    "confirm": "confirmed",
    "cancel": "cancelled",
    "compensate": "cancelled",
}

_DDL: Final = """CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {table} (
  operation_key text PRIMARY KEY,
  branch_key text NOT NULL,
  phase text NOT NULL CHECK (phase IN ('try', 'forward', 'confirm', 'cancel', 'compensate')),
  status text NOT NULL CHECK (status IN ('tried', 'confirmed', 'cancelled', 'tombstone')),
  result jsonb
);
"""
# A concurrent insert of the same key waits for the first one's transaction, then does nothing.
_INSERT: Final = (
    "INSERT INTO {table} (operation_key, branch_key, phase, status) VALUES ($1, $2, $3, $4) "
    "ON CONFLICT (operation_key) DO NOTHING RETURNING operation_key"
)
_READ: Final = "SELECT status, result::text AS result FROM {table} WHERE operation_key = $1 FOR UPDATE"
_ANSWER: Final = "UPDATE {table} SET result = $2::text::jsonb WHERE operation_key = $1 AND result IS NULL"
_SETTLE: Final = "UPDATE {table} SET status = $2 WHERE operation_key = $1 AND status = 'tried'"


class FenceConnection(Protocol):
    """What the fence needs from a connection; asyncpg's ``Connection`` already offers it."""

    async def fetchrow(self, query: str, *args: object) -> Mapping[str, Any] | None: ...

    async def execute(self, query: str, *args: object) -> str: ...

    def is_in_transaction(self) -> bool: ...


@dataclass(frozen=True)
class FenceEntry:
    """What ``begin`` decided for one request."""

    operation_key: str
    branch_key: str
    phase: PhaseOperation
    # An earlier request with this key finished: return ``result`` and change nothing.
    replay: bool
    # The earlier answer when ``replay``, else None.
    result: JsonValue
    # For a confirm, cancel or undo: whether the step it belongs to was applied. Always False for a try or forward.
    applied: bool


class Fence:
    def __init__(self, table: str = FENCE_TABLE) -> None:
        if not isinstance(table, str) or _NAME.fullmatch(table) is None:
            raise ValueError("The fence table is a lowercase schema.table name")
        self.table = table
        schema, name = table.split(".")
        quoted = f'"{schema}"."{name}"'
        self._schema = f'"{schema}"'
        self._insert = _INSERT.format(table=quoted)
        self._read = _READ.format(table=quoted)
        self._answer = _ANSWER.format(table=quoted)
        self._settle = _SETTLE.format(table=quoted)
        self._ddl = _DDL.format(schema=self._schema, table=quoted)

    def ddl(self) -> str:
        """The statements that create the fence's schema and table; the database owner runs them once."""
        return self._ddl

    async def begin(self, connection: FenceConnection, operation_key: str, transaction: LeaseTransaction) -> FenceEntry:
        """Record this request in the caller's open transaction and decide what the handler does.

        Raises ``ConnectorFailure(code, "failed", effect="none")`` with a code of ``FENCE_CODES`` when the request must
        change nothing; the caller's transaction then rolls back with this request's row.
        """
        if not connection.is_in_transaction():
            raise RuntimeError("The fence runs inside the transaction that makes the change")
        if not isinstance(transaction, LeaseTransaction):
            raise ValueError("The fence needs the lease's transaction")
        phase, branch = transaction.phase, transaction.branch_key
        if (operation_key == branch) != (phase in _FORWARD):
            raise ValueError("A step's own request uses its branch key; its confirm, cancel or undo use their own")
        if await self._insert_row(connection, operation_key, branch, phase, _PHASE_STATES[phase]) is False:
            return await self._earlier(connection, operation_key, branch, phase)
        if phase in _FORWARD:
            return FenceEntry(operation_key, branch, phase, replay=False, result=None, applied=False)
        if phase == "confirm":
            step = await self._step_status(connection, branch)
            if step in {None, "tombstone"}:
                raise ConnectorFailure("TXN_NOT_APPLIED", "failed", effect="none")
            if step == "cancelled":
                raise ConnectorFailure("TXN_OTHER_DIRECTION", "failed", effect="none")
            return FenceEntry(operation_key, branch, phase, replay=False, result=None, applied=True)
        # A cancel or undo of a step that never arrived records the step as undone, so a late one is refused.
        forward: PhaseOperation = "try" if transaction.mode == "reserve" else "forward"
        if await self._insert_row(connection, branch, branch, forward, "tombstone"):
            return FenceEntry(operation_key, branch, phase, replay=False, result=None, applied=False)
        step = await self._step_status(connection, branch)
        if step == "confirmed":
            raise ConnectorFailure("TXN_OTHER_DIRECTION", "failed", effect="none")
        return FenceEntry(operation_key, branch, phase, replay=False, result=None, applied=step == "tried")

    async def finish(self, connection: FenceConnection, entry: FenceEntry, result: JsonValue) -> JsonValue:
        """Store this request's answer and move its step to confirmed or cancelled; returns ``result``."""
        if entry.replay:
            raise ValueError("A replayed request already has its answer")
        if not connection.is_in_transaction():
            raise RuntimeError("The fence runs inside the transaction that makes the change")
        if await connection.execute(self._answer, entry.operation_key, json.dumps(result)) != "UPDATE 1":
            raise RuntimeError("The fence has no open request for this operation key")
        if entry.applied:
            await connection.execute(self._settle, entry.branch_key, _PHASE_STATES[entry.phase])
        return result

    async def _insert_row(
        self, connection: FenceConnection, key: str, branch: str, phase: PhaseOperation, status: FenceState
    ) -> bool:
        return await connection.fetchrow(self._insert, key, branch, phase, status) is not None

    async def _step_status(self, connection: FenceConnection, branch: str) -> str | None:
        row = await connection.fetchrow(self._read, branch)
        return None if row is None else str(row["status"])

    async def _earlier(
        self, connection: FenceConnection, operation_key: str, branch: str, phase: PhaseOperation
    ) -> FenceEntry:
        row = await connection.fetchrow(self._read, operation_key)
        if row is None:
            raise RuntimeError("The fence row for this operation key changed while it was read")
        if row["status"] == "tombstone":
            raise ConnectorFailure("TXN_ALREADY_UNDONE", "failed", effect="none")
        if row["result"] is None:
            # Only a caller that committed without finish leaves a row without an answer.
            raise RuntimeError("An earlier request with this operation key committed without its answer")
        return FenceEntry(operation_key, branch, phase, replay=True, result=json.loads(row["result"]), applied=False)
