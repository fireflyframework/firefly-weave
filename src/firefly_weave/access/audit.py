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

"""Whitelisted structured events and immutable per-operation correlation."""

import json
import logging
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from firefly_weave.access.models import Principal
from firefly_weave.contracts.access import Scope


class AuditContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation_id: UUID = Field(default_factory=uuid4)
    request_id: UUID | None = None
    run_id: UUID | None = None


def event(
    principal: Principal,
    scope: Scope | None,
    capability: str,
    outcome: Literal["allow", "deny", "success"],
    context: AuditContext,
    *,
    action: str = "authorization",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "action": action,
        "principal_id": str(principal.id),
        "identity": principal.credential_identity.model_dump() if principal.credential_identity else None,
        "scope": scope.model_dump(mode="json") if scope else None,
        "capability": capability,
        "outcome": outcome,
        "correlation": context.model_dump(mode="json"),
        "details": details or {},
    }


def emit(record: dict[str, Any]) -> None:
    # The message itself is structured, so standard formatters cannot discard fields.
    logging.getLogger("weave.authorization").info(json.dumps(record, separators=(",", ":"), sort_keys=True))
