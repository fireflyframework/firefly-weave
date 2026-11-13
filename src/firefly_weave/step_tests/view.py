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
"""A step test's view, built from its details, its run's status and its one step's facts and classified output."""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from firefly_weave.contracts.definitions import SideEffect
from firefly_weave.contracts.run_views import StepFact
from firefly_weave.contracts.step_tests import OmissionReason, StepTestError, StepTestStatus, StepTestView
from firefly_weave.operations.redaction import SafeProjection

# Actions and AI tasks never wait for their creator, so a waiting or suspended run is still running for them.
STATUSES: Final[dict[str, StepTestStatus]] = {
    "running": "running",
    "waiting": "running",
    "suspended": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
}


@dataclass(frozen=True)
class StepTestDetails:
    activation_id: UUID
    run_id: UUID
    side_effect: SideEffect
    created_at: datetime


def step_test_view(
    details: StepTestDetails, run_status: str, step: StepFact | None, output: SafeProjection | None
) -> StepTestView:
    status = STATUSES.get(run_status, "running")
    value, omitted = None, None
    if output is not None and status == "succeeded":
        if output.available:
            value = output.value
        else:
            reason: OmissionReason = output.omissions[0].reason if output.omissions else "classification_unavailable"
            omitted = reason
    error = None
    if step is not None and step.status == "failed" and step.error_code:
        error = StepTestError(code=f"WV-STEP-{step.error_code}", message=f"The step failed: {step.error_code}.")
    return StepTestView(
        id=details.activation_id,
        run_id=details.run_id,
        status=status,
        side_effect=details.side_effect,
        pending=[],
        output=value,
        omitted=omitted,
        error=error,
        attempts=step.attempts if step is not None else 0,
        started_at=step.started_at if step is not None and step.started_at else details.created_at,
        ended_at=step.ended_at if step is not None else None,
        duration_ms=step.duration_ms if step is not None else None,
    )
