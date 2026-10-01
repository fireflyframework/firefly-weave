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

from pyfly.container import service

from firefly_weave.contracts.runtime import RecoveryReport
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.repository import RuntimeRepository
from firefly_weave.runtime.service import RuntimeService
from firefly_weave.runtime.waits import settle


@service
class DeadlineService:
    def __init__(self, runtime: RuntimeService) -> None:
        self.runtime = runtime

    async def tick(self, tx: Transaction, limit: int) -> int:
        report = await self.scan(tx, limit)
        return report.timed_out + report.elapsed

    async def scan(self, tx: Transaction, limit: int, *, terminal_only: bool = False) -> RecoveryReport:
        async with self.runtime.definitions.transaction(tx.scope, tx):
            repository = RuntimeRepository(tx, self.runtime.definitions.outbox)
            timed_out = elapsed = 0
            for candidate in await repository.scanner_runs(limit):
                from firefly_weave.runtime.service import event_hash
                from firefly_weave.runtime.terminal_capacity import due_terminal, oversized_row, persist_terminal

                oversized = await oversized_row(repository, candidate["id"])
                if oversized is not None:
                    event = await due_terminal(repository, oversized)
                    if event is not None:
                        await persist_terminal(repository, oversized, event, event_hash(event))
                        timed_out += 1
                    continue
                row = await repository.run(candidate["id"], lock=True)
                await self.runtime.observe_unavailable(tx, row)
                kind = await settle(repository, row, terminal_only=terminal_only)
                timed_out += int(kind == 1)
                elapsed += int(kind == 2)
            return RecoveryReport(timed_out=timed_out, elapsed=elapsed)
