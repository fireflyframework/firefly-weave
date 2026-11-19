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

"""Agent conversations in an environment: the lifetime sweep."""

from datetime import datetime

from sqlalchemy import text

from firefly_weave.contracts.agent_conversations import SWEEP_BATCH
from firefly_weave.persistence.uow import Transaction

_SCOPE = "tenant_id=:tenant AND project_id=:project AND environment_id=:environment"


async def sweep(tx: Transaction, now: datetime, *, batch: int = SWEEP_BATCH) -> int:
    """Delete one batch of the environment's expired conversations, the longest expired first; returns how many went.

    Rows another transaction holds (an append in flight) are skipped, and the delete checks the expiry again, so a
    conversation an append refreshed meanwhile is kept. An expired conversation is never loaded, listed or read, so a
    sweep that falls behind leaves nothing visible.
    """
    deleted = await tx.session.scalar(
        text(
            f"WITH gone AS (DELETE FROM agent_conversations WHERE {_SCOPE} AND (workflow,key_hash) IN "
            f"(SELECT workflow,key_hash FROM agent_conversations WHERE {_SCOPE} AND expires_at<=:now "
            "ORDER BY expires_at LIMIT :batch FOR UPDATE SKIP LOCKED) AND expires_at<=:now RETURNING 1) "
            "SELECT count(*) FROM gone"
        ),
        {
            "tenant": tx.scope.tenant_id,
            "project": tx.scope.project_id,
            "environment": tx.scope.environment_id,
            "now": now,
            "batch": batch,
        },
    )
    return int(deleted or 0)
