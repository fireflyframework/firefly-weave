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

"""Transaction-bound SQL for operational projections."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from firefly_weave.contracts.run_views import RunOrigin, RunSummaryCaller
from firefly_weave.persistence.uow import Transaction

FACT_TABLES: tuple[str, str, str, str, str] = (
    "run_facts",
    "step_facts",
    "task_facts",
    "incident_facts",
    "worker_facts",
)


@dataclass(frozen=True)
class RunStartFacts:
    origin: RunOrigin = "manual"
    test: bool = False
    caller: RunSummaryCaller | None = None
    retried_from_run_id: UUID | None = None


class FactRepository:
    def __init__(self, tx: Transaction) -> None:
        self.tx = tx
        self.params = {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
        }

    async def execute(self, sql: str, **values: Any) -> None:
        await self.tx.session.execute(text(sql), {**values, **self.params})

    async def rows(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        result = await self.tx.session.execute(text(sql), {**values, **self.params})
        return [dict(row) for row in result.mappings()]
