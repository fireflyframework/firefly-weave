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

"""A subscriber needs the read capability of every event type it subscribes to."""

from uuid import uuid4

from firefly_weave.contracts.integration_events import SubscriptionRequest
from firefly_weave.operations.subscriptions import event_capability


def test_subscribing_to_alert_changes_needs_alert_read():
    assert event_capability("alert.transition") == "alert.read"
    request = SubscriptionRequest(
        name="alerts",
        connection_revision_id=uuid4(),
        signing_slot="signing",
        path="/hooks/weave",
        event_types=("alert.transition", "run.transition"),
    )
    assert [event_capability(kind) for kind in request.event_types] == ["alert.read", "run.read"]
