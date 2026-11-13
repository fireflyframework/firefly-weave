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
"""Retire expired test activations, then purge retired ones that no run uses; one environment, bounded per visit."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from firefly_weave.persistence.uow import Transaction

SCOPED = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"


@dataclass(frozen=True)
class SweepResult:
    retired: int
    purged: int


async def sweep_test_activations(tx: Transaction, now: datetime, *, limit: int = 100) -> SweepResult:
    """A retired test activation refuses new runs at once; its rows go once retention purged its runs."""
    keys = {"tenant": tx.scope.tenant_id, "project": tx.scope.project_id, "environment": tx.scope.environment_id}
    retired = (
        await tx.session.execute(
            text(
                f"UPDATE test_activation_details SET retired_at=:now WHERE {SCOPED} AND activation_id IN ("
                f"SELECT activation_id FROM test_activation_details WHERE {SCOPED} AND retired_at IS NULL "
                "AND expires_at<=:now ORDER BY expires_at LIMIT :limit) RETURNING activation_id"
            ),
            {**keys, "now": now, "limit": limit},
        )
    ).all()
    candidates: Sequence[UUID] = (
        (
            await tx.session.execute(
                text(
                    f"SELECT activation_id FROM test_activation_details WHERE {SCOPED} AND retired_at IS NOT NULL "
                    "ORDER BY retired_at LIMIT :limit"
                ),
                {**keys, "limit": limit},
            )
        )
        .scalars()
        .all()
    )
    purged = 0
    for activation in candidates:
        if await tx.session.scalar(
            text("SELECT weave_purge_test_activation(:tenant,:project,:environment,:activation)"),
            {**keys, "activation": activation},
        ):
            purged += 1
    return SweepResult(retired=len(retired), purged=purged)
