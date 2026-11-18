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

"""A native handler's log entries go through the same lease-checked write as a remote worker's, outside the
project's operation lock, as the native principal loaded inside that transaction."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.workers import LeaseProof, TaskLogAccepted, TaskLogEntry, TaskLogRequest

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())


class Recorded:
    def __init__(self) -> None:
        self.opened = []
        self.loaded = []
        self.logged = []

    @asynccontextmanager
    async def open(self, scope, *, mutation=True, admission=True):
        self.opened.append((scope, mutation, admission))
        yield SimpleNamespace(scope=scope)

    async def load_principal(self, principal_id, *, tx=None):
        self.loaded.append((principal_id, tx is not None))
        return SimpleNamespace(id=principal_id)

    async def log(self, tx, request, *, actor, scope, context, daily_limit):
        self.logged.append((request, actor.id, scope, daily_limit))
        return TaskLogAccepted(accepted=len(request.entries), dropped=0)


async def test_native_entries_are_written_outside_the_operation_lock_with_the_configured_daily_limit():
    recorded = Recorded()
    service = ConnectorExecutionService(recorded, recorded, recorded, None, None)  # type: ignore[arg-type]
    principal = uuid4()
    transport = ServiceTransport(service, SCOPE, principal, uuid4(), daily_log_entries=1234)
    request = TaskLogRequest(
        lease=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="opaque"),
        entries=[TaskLogEntry(at=datetime.now(UTC), source="connector", level="info", message="Called")],
    )
    accepted = await transport.log(request)
    assert (accepted.accepted, accepted.dropped) == (1, 0)
    assert recorded.opened == [(SCOPE, True, False)]
    assert recorded.loaded == [(principal, True)]
    assert recorded.logged == [(request, principal, SCOPE, 1234)]
