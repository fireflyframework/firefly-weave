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

"""Atomic terminal task closing and append-only task audit facts."""

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from firefly_weave.runtime.repository import RuntimeRepository


async def close_tasks(repository: "RuntimeRepository", run_id: UUID, status: str, node_id: object) -> None:
    from firefly_weave.runtime.repository import SCOPE

    await repository.execute(
        f"WITH closed AS (UPDATE human_tasks SET status=CASE WHEN :status='timed_out' AND node_id=:node "
        f"THEN 'expired' ELSE 'cancelled' END,revision=revision+1 WHERE {SCOPE} "
        "AND run_id=:run AND status IN ('ready','claimed') RETURNING *) "
        "INSERT INTO human_task_audit(tenant_id,project_id,environment_id,task_id,revision,action,created_at) "
        "SELECT tenant_id,project_id,environment_id,id,revision,status,clock_timestamp() FROM closed",
        status=status,
        node=node_id,
        run=run_id,
    )
