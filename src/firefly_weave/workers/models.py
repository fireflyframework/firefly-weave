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
from datetime import datetime
from typing import Any

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


@dataclass(frozen=True)
class VerifiedTask:
    """In-process lease authority. Never deserialized from requests or exposed by HTTP."""

    transaction: Transaction
    run: dict[str, Any]
    task: dict[str, Any]
    attempt: dict[str, Any]
    checked_at: datetime | None = None
