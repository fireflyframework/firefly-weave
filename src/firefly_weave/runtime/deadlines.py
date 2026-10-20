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

"""Bounded, DB-time deadline consumption under run locks."""

from functools import partial
from uuid import UUID

from pyfly.container import service

from firefly_weave.contracts.runtime import RecoveryReport
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.waits import settle

# Due runs read per scan, so runs behind one whose write is refused for now are still reached.
SCAN_WINDOW = 10


@service
class DeadlineService:
    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    async def tick(self, tx: Transaction, limit: int) -> int:
        report = await self.scan(tx, limit)
        return report.timed_out + report.elapsed

    async def scan(self, tx: Transaction, limit: int, *, terminal_only: bool = False) -> RecoveryReport:
        """Settle at most ``limit`` due runs; a run whose write is refused for now is skipped, never the scan.

        Candidates come from a window of at least ten due runs in scan order, so a run that cannot afford its write
        (for example a stop under a spent quota) does not stop the runs after it, and stays first for the next scan.
        """
        from firefly_weave.persistence.uow import isolated

        async with self.runtime.definitions.transaction(tx.scope, tx):
            repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
            counts = {"timed_out": 0, "elapsed": 0}
            handled = 0
            for candidate in await repository.scanner_runs(min(100, max(limit, SCAN_WINDOW))):
                if handled >= limit:
                    break
                if await isolated(tx, partial(self._settle, repository, candidate["id"], counts, terminal_only)):
                    handled += 1
            return RecoveryReport(**counts)

    async def _settle(
        self, repository: RuntimeRepository, identifier: UUID, counts: dict[str, int], terminal_only: bool
    ) -> None:
        from firefly_weave.runtime.service import event_hash
        from firefly_weave.runtime.terminal_capacity import due_terminal, oversized_row, persist_terminal

        oversized = await oversized_row(repository, identifier)
        if oversized is not None:
            event = await due_terminal(repository, oversized)
            if event is not None:
                await persist_terminal(repository, oversized, event, event_hash(event))
                counts["timed_out"] += 1
            return
        row = await repository.run(identifier, lock=True)
        await self.runtime.observe_unavailable(repository.tx, row)
        kind = await settle(repository, row, terminal_only=terminal_only)
        counts["timed_out"] += int(kind == 1)
        counts["elapsed"] += int(kind == 2)
