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
from firefly_weave.step_tests.repository import stop_listening

SCOPED = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"
# A retired test activation can go once nothing that retention keeps still names it.
PURGEABLE = (
    "NOT EXISTS(SELECT 1 FROM runs r WHERE r.tenant_id=d.tenant_id AND r.project_id=d.project_id "
    "AND r.environment_id=d.environment_id AND r.activation_id=d.activation_id) "
    "AND NOT EXISTS(SELECT 1 FROM run_calls c WHERE c.tenant_id=d.tenant_id AND c.project_id=d.project_id "
    "AND c.environment_id=d.environment_id AND c.activation_id=d.activation_id) "
    "AND NOT EXISTS(SELECT 1 FROM run_facts f WHERE f.tenant_id=d.tenant_id AND f.project_id=d.project_id "
    "AND f.environment_id=d.environment_id AND f.activation_id=d.activation_id)"
)


@dataclass(frozen=True)
class SweepResult:
    retired: int
    purged: int


async def sweep_test_activations(tx: Transaction, now: datetime, *, limit: int = 100) -> SweepResult:
    """Retire expired test activations and stop their listen routes; purge retired ones once no run names them.

    A retired test activation refuses new runs at once, and its runs keep their own timeout. Its rows go once
    retention purged its runs; ones whose runs retention still keeps are passed over, so newer ones are reached.
    """
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
    stopped = await stop_listening(tx, [row[0] for row in retired])
    candidates: Sequence[UUID] = (
        (
            await tx.session.execute(
                text(
                    "SELECT d.activation_id FROM test_activation_details d WHERE d.tenant_id=:tenant "
                    "AND d.project_id=:project AND d.environment_id=:environment AND d.retired_at IS NOT NULL "
                    # A route stopped in this pass releases its reserve when the pass commits: purge it next time.
                    f"AND {PURGEABLE} AND NOT d.activation_id=ANY(:stopped) ORDER BY d.retired_at LIMIT :limit"
                ),
                {**keys, "limit": limit, "stopped": stopped},
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
