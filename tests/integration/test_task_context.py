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

"""Task context obeys the same database lease fence as completion and credentials."""

from uuid import uuid4

import pytest
import test_leases
from sqlalchemy import text

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.definitions.models import CatalogError

pytestmark = pytest.mark.integration
connections = test_leases.connections
credential_task = test_leases.credential_task


async def test_unbound_task_context_exposes_no_other_connections(
    task_service, transaction_factory, queued_task, worker_ids
):
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
        context = await task_service.context(tx, lease.proof)
    assert context.connection is None
    assert context.expires_at == lease.expires_at


@pytest.mark.parametrize("mutation", ["token", "owner", "generation", "expiry", "revoked", "settled"])
async def test_task_context_denies_stale_or_foreign_authority(
    mutation, task_service, transaction_factory, queued_task, worker_ids, access_db
):
    async with transaction_factory() as tx:
        lease = (await task_service.claim(tx, worker_ids[0], 1))[0]
    proof = lease.proof
    if mutation == "token":
        proof = proof.model_copy(update={"token": "forged"})
    elif mutation == "owner":
        proof = proof.model_copy(update={"owner": worker_ids[1]})
    elif mutation == "generation":
        proof = proof.model_copy(update={"generation": proof.generation + 1})
    elif mutation == "settled":
        async with transaction_factory() as tx:
            await task_service.complete(tx, proof, uuid4(), 42)
    else:
        async with access_db[1].begin() as tx:
            await tx.execute(
                text(
                    "UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"
                    if mutation == "expiry"
                    else "UPDATE worker_instances SET revoked=true"
                )
            )
    with pytest.raises((AccessDenied, CatalogError)):
        async with transaction_factory() as tx:
            await task_service.context(tx, proof)


async def test_bound_context_returns_only_pinned_revision_and_secret_slot_names(credential_task):
    tasks, workers, authority, grant, request, run = credential_task
    async with tasks.runtime.definitions.transaction(authority["scope"], None) as tx:
        result = await tasks.context(tx, request.lease, **authority)
    assert result.connection.revision_id == request.connection_revision_id
    assert result.connection.connector == "example@1.0.0"
    assert result.connection.secret_slots == ["token"]
    assert "secret_refs" not in result.model_dump_json()
    assert "WEAVE_CONNECTION_SECRET_TEST" not in result.model_dump_json()
