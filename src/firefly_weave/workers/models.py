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

"""Lease identities and shared safe failures."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from firefly_weave.contracts.workers import WorkerInstance, WorkerStatus
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.runtime.kernel import checked_deadline


def new_lease_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def capped_extension(now: datetime, ttl: int, absolute_deadline: datetime) -> datetime:
    return min(checked_deadline(now, ttl), absolute_deadline)


def unavailable() -> CatalogError:
    return CatalogError(409, "WV-LEASE", "Lease or worker authority unavailable")


def observed_worker(
    instance: WorkerInstance,
    *,
    last_seen_at: datetime | None,
    draining: bool,
    revision: int,
    active_leases: int,
    observed_at: datetime,
) -> WorkerStatus:
    expires = last_seen_at + timedelta(seconds=60) if last_seen_at else None
    presence: Literal["unknown", "recent", "stale"] = (
        "unknown" if expires is None else "recent" if expires > observed_at else "stale"
    )
    available = None if presence != "recent" else max(0, instance.capacity - active_leases)
    if draining or instance.revoked:
        available = 0
    return WorkerStatus(
        **instance.model_dump(),
        revision=revision,
        draining=draining,
        presence=presence,
        last_seen_at=last_seen_at,
        presence_expires_at=expires,
        observed_at=observed_at,
        active_leases=active_leases,
        available_capacity=available,
    )


@dataclass(frozen=True)
class VerifiedTask:
    """In-process lease authority. Never deserialized from requests or exposed by HTTP."""

    transaction: Transaction
    run: dict[str, Any]
    task: dict[str, Any]
    attempt: dict[str, Any]
    checked_at: datetime | None = None
