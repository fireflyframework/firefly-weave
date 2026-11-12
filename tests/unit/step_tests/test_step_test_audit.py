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
"""Step test audits say what ran and on what, never a value of its input or output; busy people are told to wait."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from firefly_weave.contracts.step_tests import StepTestRequest
from firefly_weave.step_tests.admission import MAX_PER_MINUTE, MAX_RUNNING, busy_refusal, start_audit

MARKER = "9f2c-marker"
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def test_audit_records_hold_no_value_of_the_input_and_name_what_ran():
    crm = uuid4()
    request = StepTestRequest.model_validate_json(
        json.dumps(
            {
                "kind": "action",
                "step_id": "fetch-customer",
                "uses": "fetch-customer@1.0.0",
                "input": {"customerId": MARKER, "nested": [MARKER]},
                "connections": {"crm": str(crm)},
                "acknowledged_side_effect": "non_idempotent",
                "draft_id": str(uuid4()),
                "draft_revision": 3,
                "timeout_seconds": 60,
            }
        )
    )
    run = uuid4()
    details = start_audit(request, "non_idempotent", run)
    assert set(details) == {
        "step_id",
        "uses",
        "kind",
        "side_effect",
        "acknowledged_side_effect",
        "connection_revision_ids",
        "draft_id",
        "draft_revision",
        "run_id",
    }
    assert details["connection_revision_ids"] == [str(crm)] and details["run_id"] == str(run)
    assert MARKER not in json.dumps(details)


def test_a_fifth_running_step_test_waits_until_the_first_one_ends():
    running = [NOW + timedelta(seconds=seconds) for seconds in (90, 30, 200, 61)]
    refused = busy_refusal(running, [], NOW)
    assert refused is not None
    assert (refused.status, refused.code, refused.retry_after) == (429, "WV-STEPTEST-BUSY", 30)
    assert busy_refusal(running[: MAX_RUNNING - 1], [], NOW) is None


def test_a_thirty_first_a_minute_waits_until_the_oldest_leaves_the_minute():
    recent = [NOW - timedelta(seconds=50)] + [NOW - timedelta(seconds=10)] * (MAX_PER_MINUTE - 1)
    refused = busy_refusal([], recent, NOW)
    assert refused is not None and (refused.code, refused.retry_after) == ("WV-STEPTEST-BUSY", 10)
    assert busy_refusal([], recent[1:], NOW) is None


def test_the_wait_is_at_least_one_second():
    refused = busy_refusal([NOW] * MAX_RUNNING, [], NOW)
    assert refused is not None and refused.retry_after == 1
