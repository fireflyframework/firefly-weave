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

"""Worker fleet queries, stale-worker retirement and release usage.

`workers.list` lists current workers by default: not revoked, and seen (or, never seen, registered) in the last
hour. Cursors are bound to every filter and the order. `releases.usage` pages the deployments that run a release,
bound to the release and to the targets the reader may see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.deployments import DeploymentName
from firefly_weave.contracts.run_views import Count, Cursor, SummaryLimit, _digest
from firefly_weave.contracts.values import JsonValue

type WorkerPresence = Literal["unknown", "recent", "stale"]
type WorkerListState = Literal["current", "all"]
type WorkerOrder = Literal["last_seen_desc", "registered_desc"]
# A task capability exactly as workers register it, such as echo@1.2.0.
type TaskTypeFilter = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$")]
# A literal, case-sensitive prefix of a reported host; `%` and `_` match only themselves.
type HostPrefix = Annotated[str, Field(min_length=1, max_length=253, pattern=r"^[^\x00-\x1f\x7f-\x9f]+$")]
# One retirement command revokes at most this many workers.
RETIREMENT_BATCH = 100


class WorkerListQuery(ContractModel):
    """`workers.list` query parameters; every combination is a valid filter."""

    presence: WorkerPresence | None = None
    draining: bool | None = None
    release_id: UUID | None = None
    target_id: UUID | None = None
    task_type: TaskTypeFilter | None = None
    host_prefix: HostPrefix | None = None
    state: WorkerListState = "current"
    order: WorkerOrder = "last_seen_desc"
    limit: SummaryLimit = 50
    cursor: Cursor | None = None

    def cursor_collection(self) -> str:
        filters = self.model_dump(mode="json", exclude={"order", "limit", "cursor"})
        return f"workers:{_digest(filters)}:{self.order}"


class RetireStaleRequest(ContractModel):
    """Revoke workers without an active lease whose last contact is older than the threshold."""

    older_than_seconds: int = Field(ge=3600, le=2592000)
    dry_run: bool


class RetireStaleResult(ContractModel):
    """`matched` counts every eligible worker; `revoked` and `worker_ids` describe this command's batch."""

    matched: Count
    revoked: int = Field(ge=0, le=RETIREMENT_BATCH)
    worker_ids: list[UUID] = Field(max_length=RETIREMENT_BATCH)


class ReleaseUsageQuery(ContractModel):
    """`releases.usage` query parameters; a cursor continues only for the same release and readable targets."""

    limit: SummaryLimit = 100
    cursor: Cursor | None = None

    def cursor_collection(self, release_id: UUID, authorized_targets: tuple[UUID, ...] | None) -> str:
        targets = "*" if authorized_targets is None else sorted(str(target) for target in authorized_targets)
        return f"releases.usage:{release_id}:{_digest({'targets': targets})}"


class ReleaseWorkerCounts(ContractModel):
    """Every instance of the release once: revoked, else draining, else online (seen recently) or offline."""

    online: Count
    offline: Count
    draining: Count
    revoked: Count


class ReleaseDeploymentUsage(ContractModel):
    target_id: UUID
    target_name: DeploymentName
    deployment_id: UUID
    deployment_name: DeploymentName
    component: DeploymentName
    replicas: int = Field(ge=0, le=100)


class ReleaseUsage(ContractModel):
    workers: ReleaseWorkerCounts
    deployments: list[ReleaseDeploymentUsage] = Field(max_length=100)
    activations: Count
    next_cursor: Cursor | None = None


@dataclass(frozen=True)
class ReleaseUsagePosition:
    """The last deployment component of a release usage page."""

    deployment_id: UUID
    component: str


_COMPONENT: TypeAdapter[str] = TypeAdapter(DeploymentName)


def release_usage_position(position: tuple[JsonValue, str] | None) -> ReleaseUsagePosition | None:
    """Validate a decoded release usage cursor before using its values in SQL."""
    if position is None:
        return None
    component, deployment = position
    if type(component) is not str or type(deployment) is not str:
        raise ValueError("Invalid release usage position")
    return ReleaseUsagePosition(UUID(deployment), _COMPONENT.validate_python(component, strict=True))
