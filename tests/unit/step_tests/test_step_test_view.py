# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""What a step test's creator reads: the run's status and the one step's classified output, error and timing."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from firefly_weave.contracts.run_views import StepFact
from firefly_weave.contracts.step_tests import StepTestView
from firefly_weave.operations.redaction import Omission, SafeProjection
from firefly_weave.step_tests.view import StepTestDetails, step_test_view

CREATED = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
DETAILS = StepTestDetails(
    activation_id=UUID("0123456789ab4cde8f0123456789abcd"),
    run_id=UUID("1123456789ab4cde8f0123456789abcd"),
    side_effect="read_only",
    created_at=CREATED,
)


def step(status="succeeded", *, error=None, attempts=1, ended=True):
    started = CREATED + timedelta(seconds=1)
    ended_at = started + timedelta(milliseconds=250) if ended else None
    return StepFact(
        node_id="fetch-customer",
        instance_key="",
        iteration=[],
        kind="action",
        status=status,
        scheduled_at=CREATED,
        started_at=started,
        ended_at=ended_at,
        duration_ms=250 if ended else None,
        queue_wait_ms=1000,
        attempts=attempts,
        worker_id=None,
        error_code=error,
        child_run_id=None,
        log_entries=0,
    )


def test_a_running_run_reads_running_with_no_output_yet():
    view = step_test_view(DETAILS, "running", None, None)
    assert (view.status, view.output, view.omitted, view.attempts) == ("running", None, None, 0)
    assert (view.started_at, view.ended_at, view.pending) == (CREATED, None, [])


@pytest.mark.parametrize("status", ["waiting", "suspended"])
def test_a_waiting_run_reads_running_because_actions_and_ai_tasks_never_ask_for_anything(status):
    view = step_test_view(DETAILS, status, step("running", ended=False), None)
    assert (view.status, view.pending) == ("running", [])


def test_a_succeeded_run_carries_the_steps_output_and_timing():
    output = SafeProjection(available=True, value={"name": "Acme"})
    view = step_test_view(DETAILS, "succeeded", step(), output)
    assert (view.status, view.output, view.omitted, view.error) == ("succeeded", {"name": "Acme"}, None, None)
    assert (view.attempts, view.duration_ms) == (1, 250)
    assert view.started_at == CREATED + timedelta(seconds=1)
    assert view.ended_at == CREATED + timedelta(seconds=1, milliseconds=250)


def test_an_omitted_output_says_why_and_carries_no_value():
    output = SafeProjection(available=False, omissions=[Omission(path="/token", reason="classified_secret")])
    view = step_test_view(DETAILS, "succeeded", step(), output)
    assert (view.output, view.omitted) == (None, "classified_secret")
    assert view.model_dump(mode="json")["omitted"] == "classified_secret"
    assert view.model_dump(mode="json").get("output") is None


def test_a_failed_step_names_its_code_and_message():
    view = step_test_view(DETAILS, "failed", step("failed", error="SMTP_REJECTED", attempts=2), None)
    assert view.status == "failed" and view.attempts == 2
    assert view.error is not None
    assert (view.error.code, view.error.message) == ("WV-STEP-SMTP_REJECTED", "The step failed: SMTP_REJECTED.")


@pytest.mark.parametrize("status", ["timed_out", "cancelled"])
def test_a_run_that_timed_out_or_was_canceled_reads_so(status):
    view = step_test_view(DETAILS, status, step("running", ended=False), None)
    assert (view.status, view.output, view.error) == (status, None, None)


def test_an_omitted_view_never_holds_an_output():
    with pytest.raises(ValueError):
        StepTestView.model_validate(
            {
                **step_test_view(DETAILS, "succeeded", step(), None).model_dump(mode="json"),
                "output": {"token": "x"},
                "omitted": "classified_secret",
            }
        )
