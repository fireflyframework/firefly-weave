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

"""Change request states, plan lifetimes and the drain window: pure rules the service and Studio share."""

from datetime import datetime, timedelta
from uuid import UUID

from firefly_weave.contracts.deployments import ChangeHistory, ChangeRequest, ChangeState
from firefly_weave.definitions.models import CatalogError

ACTIVE: tuple[ChangeState, ...] = (
    "observing",
    "ready_to_plan",
    "awaiting_approval",
    "approved",
    "draining",
    "applying",
)
ENDED: tuple[ChangeState, ...] = (
    "succeeded",
    "failed",
    "reconciliation_required",
    "expired",
    "declined",
    "cancelled",
    "superseded",
)
# Ended change requests are kept this long, then the operations loop removes them.
RETENTION = timedelta(days=90)
# A drain starts only while the plan outlives the drain timeout by this much, so a forced apply still lands in time.
DRAIN_HANDOFF_SECONDS = 120
# A plan for a change that drains must outlive the drain timeout by this much: approval handoff, drain and apply.
DRAIN_PLAN_MARGIN_SECONDS = 180
DEFAULT_PLAN_SECONDS = 600
DRAINING_PLAN_SECONDS = 900
HISTORY = 50
# Failures that only mean the plan no longer fits the target; observing again with the same intent fixes them.
REPLANNABLE_FAILURES = frozenset({"WV-PLAN-STALE", "WV-PRECONDITION-FAILED", "WV-DRAIN-WINDOW", "WV-JOB-CANCELLED"})
# States whose plan has not been applied yet; once its plan expires, such a change reads as expired.
_PLANNED: frozenset[ChangeState] = frozenset({"awaiting_approval", "approved", "draining"})
TRANSITIONS: dict[ChangeState, frozenset[ChangeState]] = {
    "observing": frozenset({"ready_to_plan", "failed", "cancelled", "superseded"}),
    "ready_to_plan": frozenset({"awaiting_approval", "failed", "cancelled", "superseded"}),
    "awaiting_approval": frozenset({"approved", "declined", "cancelled", "expired", "superseded"}),
    "approved": frozenset({"draining", "applying", "declined", "cancelled", "expired", "superseded"}),
    "draining": frozenset({"applying", "declined", "cancelled", "expired", "superseded"}),
    "applying": frozenset({"succeeded", "failed", "reconciliation_required"}),
    "succeeded": frozenset(),
    "failed": frozenset({"observing"}),
    "reconciliation_required": frozenset(),
    "expired": frozenset({"observing"}),
    "declined": frozenset(),
    "cancelled": frozenset(),
    "superseded": frozenset({"observing"}),
}


def transition(record: ChangeRequest, state: ChangeState, *, actor: UUID | None, at: datetime) -> ChangeRequest:
    """The record in its next state, with the move in its history; 409 WV-CHANGE-STATE when it may not move."""
    if state not in TRANSITIONS[record.state]:
        raise CatalogError(409, "WV-CHANGE-STATE", f"A change request cannot go from {record.state} to {state}")
    history = [*record.history, ChangeHistory(at=at, state=state, actor_id=actor)][-HISTORY:]
    return record.model_copy(update={"state": state, "revision": record.revision + 1, "history": history})


def effective(record: ChangeRequest, now: datetime) -> ChangeRequest:
    """The record as readers see it: an unapplied plan past its expiry reads as expired until a write persists it."""
    if record.state in _PLANNED and record.plan_expires_at is not None and record.plan_expires_at <= now:
        return record.model_copy(update={"state": "expired"})
    return record


def drains(record: ChangeRequest) -> bool:
    return record.drain.mode == "drain_first"


def require_drain_window(record: ChangeRequest, now: datetime) -> None:
    """Refuse a drain that could not finish, time out and still apply before the plan expires."""
    assert record.plan_expires_at is not None
    left = (record.plan_expires_at - now).total_seconds()
    if left < record.drain.timeout_seconds + DRAIN_HANDOFF_SECONDS:
        raise CatalogError(
            409, "WV-DRAIN-WINDOW", "Not enough plan lifetime is left to drain and apply; observe and plan again"
        )


def approve_by(record: ChangeRequest) -> datetime | None:
    """When approval must happen: the plan's expiry, earlier by the drain timeout and handoff when it drains."""
    if record.plan_expires_at is None:
        return None
    if not drains(record):
        return record.plan_expires_at
    return record.plan_expires_at - timedelta(seconds=record.drain.timeout_seconds + DRAIN_HANDOFF_SECONDS)


def plan_lifetime(requested: int | None, *, drains: bool, timeout: int) -> int:
    """The plan lifetime a change request uses; 422 when a draining change's plan cannot cover its drain."""
    seconds = requested if requested is not None else DRAINING_PLAN_SECONDS if drains else DEFAULT_PLAN_SECONDS
    if drains and seconds < timeout + DRAIN_PLAN_MARGIN_SECONDS:
        raise CatalogError(
            422,
            "WV-VALIDATION",
            f"A plan for a change that drains must live at least {timeout + DRAIN_PLAN_MARGIN_SECONDS} seconds",
        )
    return seconds


def replannable(record: ChangeRequest) -> bool:
    """Expired and superseded changes, and failures that only mean the plan went stale, may be planned again."""
    if record.state in {"expired", "superseded"}:
        return True
    return record.state == "failed" and record.failure is not None and record.failure.code in REPLANNABLE_FAILURES
