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

"""Workers request only the context bound to a live lease, never an arbitrary connection."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from firefly_weave.contracts.workers import LeaseProof
from firefly_weave.sdk.transport import WorkerTransport


async def test_context_posts_only_proof_and_returns_typed_binding():
    proof = LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="fixture-proof")
    revision = uuid4()
    expires = datetime.now(UTC) + timedelta(seconds=15)

    def receive(request):
        assert request.url.path == "/environment/tasks/context"
        assert json.loads(request.content) == proof.model_dump(mode="json")
        return httpx.Response(
            200,
            json={
                "connection": {
                    "revision_id": str(revision),
                    "connector": "fixture-model@1.0.0",
                    "config": {"model": "fixture"},
                    "allowed_destinations": ["https://model.example"],
                    "secret_slots": ["api-key"],
                },
                "expires_at": expires.isoformat(),
            },
        )

    async with httpx.AsyncClient(base_url="https://platform.example", transport=httpx.MockTransport(receive)) as client:
        response = await WorkerTransport(client, "/environment", proof.owner).context(proof)
    assert response.connection.revision_id == revision
    assert response.connection.secret_slots == ["api-key"]
    assert response.expires_at == expires
