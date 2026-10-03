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

"""Worker observation and drain commands share typed, scoped SDK operations."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.workers import InstanceRequest, WorkerControlRequest, WorkerInstance
from firefly_weave.sdk.client import WeaveClient
from firefly_weave.workers.models import observed_worker


async def test_worker_registration_stays_compatible_and_control_is_typed():
    calls = []
    instance = WorkerInstance(
        id=uuid4(), principal_id=uuid4(), release_id=uuid4(), task_types=["echo@1.0.0"], capacity=2
    )
    status = observed_worker(
        instance, last_seen_at=None, draining=False, revision=1, active_leases=0, observed_at=datetime.now(UTC)
    )

    def receive(request):
        calls.append(request)
        payload = status.model_dump(mode="json")
        if request.method == "POST" and request.url.path.endswith("/workers"):
            return httpx.Response(201, json=instance.model_dump(mode="json"))
        if request.method == "POST":
            assert json.loads(request.content) == {"expected_revision": 1}
            assert request.headers["idempotency-key"] == request.url.path.rsplit("/", 1)[-1]
        if request.url.path.endswith("/workers"):
            payload = {"items": [payload], "next_cursor": None}
        return httpx.Response(200, json=payload)

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    async with WeaveClient(
        "https://platform.example", lambda: "token", scope, transport=httpx.MockTransport(receive)
    ) as client:
        registered = await client.register_worker(
            InstanceRequest(release_id=instance.release_id, task_types=instance.task_types, capacity=2)
        )
        assert type(registered) is WorkerInstance
        assert (await client.read_worker(instance.id)).presence == "unknown"
        assert (await client.list_workers()).items[0].available_capacity is None
        for method, key in ((client.drain_worker, "drain"), (client.resume_worker, "resume")):
            assert (
                await method(instance.id, WorkerControlRequest(expected_revision=1), idempotency_key=key)
            ).revision == 1
    assert len(calls) == 5
    assert all(f"/environments/{scope.environment_id}/workers" in str(call.url) for call in calls)


def test_worker_observation_contracts_are_exported():
    schemas = export_schemas()
    assert "worker-status" in schemas and "worker-control-request" in schemas
