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

"""Alert change events in the outbox on real PostgreSQL: numbers and states only, one per change."""

import json

import pytest
from test_alert_loop_integration import at, create, draft, evaluator  # noqa: F401
from test_operations_statistics import owner
from test_operations_summary import add_incident

from firefly_weave.operations.alert_rules import event_id

pytestmark = pytest.mark.integration


async def test_a_transition_event_carries_numbers_and_states_only(evaluator):  # noqa: F811
    case = evaluator
    run = await case.start("alert-event")
    incident = await add_incident(case, run.id, "first")
    rule = await create(
        case, draft(name="Incidents in checkout-service are open", selector={}, severity="critical", threshold=0)
    )
    await at(case, 0)
    await owner(case, "UPDATE incidents SET status='resolved' WHERE id=:id", id=incident)
    await at(case, 30)
    await at(case, 90)
    events = await owner(
        case,
        "SELECT id,payload FROM integration_events WHERE tenant_id=:tenant AND payload->>'type'='alert.transition' "
        "ORDER BY payload->>'emitted_at'",
    )
    (alert,) = await owner(case, "SELECT id FROM alerts WHERE rule_id=:rule", rule=rule.id)
    assert [row["id"] for row in events] == [event_id(alert["id"], "firing", 0), event_id(alert["id"], "resolved", 0)]
    for row, state, value in zip(events, ("firing", "resolved"), (1.0, 0.0), strict=True):
        payload = row["payload"]
        assert (payload["resource_id"], payload["correlation_id"]) == (str(alert["id"]), str(rule.id))
        # The metadata every event carries stays empty; the alert part holds numbers and states.
        assert payload["payload"] == {
            "status": None,
            "sequence": None,
            "revision": None,
            "alert": {
                "state": state,
                "severity": "critical",
                "signal": "incidents.active",
                "value": value,
                "threshold": 0.0,
            },
        }
        # No rule name, selector or recipient reaches a subscriber.
        assert "checkout" not in json.dumps(payload)
    # A delivery per subscription would follow; the event itself is appended once per change.
    assert len(events) == 2
