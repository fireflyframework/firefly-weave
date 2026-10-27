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

"""Transaction events: six types a subscription may name (up to 12 types), and run events that keep their bytes."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.integration_events import EventMetadata, IntegrationEvent, SubscriptionRequest
from firefly_weave.contracts.transactions import TRANSACTION_EVENT_TYPES
from firefly_weave.operations.subscriptions import event_capability

ALL = [
    "run.transition",
    "definition.published",
    "activation.activated",
    "activation.retired",
    "change_request.transition",
    *TRANSACTION_EVENT_TYPES,
]


def request(types):
    return SubscriptionRequest.model_validate_json(
        json.dumps(
            {
                "name": "reactions",
                "connection_revision_id": str(uuid4()),
                "signing_slot": "primary",
                "path": "/hooks",
                "event_types": types,
            }
        )
    )


def test_a_subscription_may_name_the_six_transaction_events_and_up_to_twelve_types():
    assert len(TRANSACTION_EVENT_TYPES) == 6
    assert request([*TRANSACTION_EVENT_TYPES]).event_types == TRANSACTION_EVENT_TYPES
    assert len(request([*ALL, "run.transition"]).event_types) == 12
    with pytest.raises(ValidationError):
        request([*ALL, "run.transition", "transaction.completed"])
    assert all(event_capability(kind) == "run.read" for kind in TRANSACTION_EVENT_TYPES)


def test_a_run_event_without_transaction_facts_keeps_its_bytes():
    event = IntegrationEvent(
        event_id=uuid4(),
        type="run.transition",
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        resource_id=uuid4(),
        correlation_id=uuid4(),
        emitted_at=datetime(2026, 10, 26, tzinfo=UTC),
        payload=EventMetadata(status="waiting", sequence=2),
    )
    assert event.model_dump(mode="json")["payload"] == {"status": "waiting", "sequence": 2, "revision": None}
