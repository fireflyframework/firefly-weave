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

"""The agent conversation operations check capabilities, key hashes, confirmations and idempotency keys before any
read; what they read and write is checked against PostgreSQL in tests/integration/test_agent_conversations.py."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from starlette.datastructures import QueryParams

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.api.agent_conversations import AgentConversationController
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agent_conversations import AgentConversationQuery, ConversationPurgeRequest
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.agent_conversations import AgentConversationService

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
HASH = "a" * 64
CONTEXT = AuditContext()


class Unopened:
    """A unit of work no check may reach: every refusal here comes before the database."""

    @asynccontextmanager
    async def open(self, scope, *, mutation=True):
        raise AssertionError("A refused request opened a unit of work")
        yield


def principal(*roles: str) -> Principal:
    return Principal(id=uuid4(), kind="human", grants=tuple(Grant(role=role, scope=SCOPE) for role in roles))


def service() -> AgentConversationService:
    return AgentConversationService(Unopened(), AuthorizationService())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "call",
    [
        lambda s, actor: s.list(actor, SCOPE, AgentConversationQuery(), context=CONTEXT),
        lambda s, actor: s.read(actor, SCOPE, "support-chat", HASH, context=CONTEXT),
    ],
    ids=["list", "read"],
)
async def test_reading_needs_run_read_before_any_read(call):
    with pytest.raises(AccessDenied):
        await call(service(), principal("worker"))


@pytest.mark.parametrize(
    "call",
    [
        lambda s, actor: s.delete(actor, SCOPE, "support-chat", HASH, context=CONTEXT),
        lambda s, actor: s.purge_workflow(
            actor,
            SCOPE,
            "support-chat",
            ConversationPurgeRequest(confirm_workflow="support-chat"),
            "k",
            context=CONTEXT,
        ),
    ],
    ids=["delete", "purge"],
)
async def test_deleting_needs_run_purge_before_any_read(call):
    # A viewer reads conversations but can't delete them.
    with pytest.raises(AccessDenied):
        await call(service(), principal("viewer"))


@pytest.mark.parametrize("key_hash", ["C-4", HASH.upper(), HASH[:63], HASH + "a"])
async def test_a_key_hash_that_isnt_64_lowercase_hex_characters_is_refused_before_anything_is_read(key_hash):
    for call in (service().read, service().delete):
        with pytest.raises(CatalogError) as refused:
            await call(principal("execution_manager", "viewer"), SCOPE, "support-chat", key_hash, context=CONTEXT)
        assert (refused.value.status, refused.value.code) == (422, "WV-AGENT-CONVERSATION-KEY")


async def test_a_workflow_name_that_isnt_one_is_refused_before_anything_is_read():
    with pytest.raises(CatalogError) as refused:
        await service().read(principal("viewer"), SCOPE, "../other", HASH, context=CONTEXT)
    assert (refused.value.status, refused.value.code) == (422, "WV-AGENT-CONVERSATION-KEY")


async def test_a_purge_needs_the_workflow_confirmed_and_an_idempotency_key():
    actor = principal("execution_manager")
    with pytest.raises(CatalogError) as unconfirmed:
        await service().purge_workflow(
            actor,
            SCOPE,
            "support-chat",
            ConversationPurgeRequest(confirm_workflow="billing-chat"),
            "k",
            context=CONTEXT,
        )
    with pytest.raises(CatalogError) as keyless:
        await service().purge_workflow(
            actor, SCOPE, "support-chat", ConversationPurgeRequest(confirm_workflow="support-chat"), "", context=CONTEXT
        )

    assert (unconfirmed.value.status, unconfirmed.value.code) == (422, "WV-AGENT-CONVERSATION-CONFIRM")
    assert (keyless.value.status, keyless.value.code) == (422, "WV-IDEMPOTENCY")


async def test_the_controller_sends_the_path_and_the_idempotency_key_to_the_service():
    seen = {}

    class Recording:
        async def purge_workflow(self, actor, scope, workflow, request, key, *, context):
            seen.update(workflow=workflow, confirm=request.confirm_workflow, key=key)
            from firefly_weave.contracts.agent_conversations import ConversationPurge

            return ConversationPurge(workflow=workflow, deleted=0, more=False)

    controller = AgentConversationController(Recording())  # type: ignore[arg-type]
    request = SimpleNamespace(
        path_params={
            "tenant": str(SCOPE.tenant_id),
            "project": str(SCOPE.project_id),
            "environment": str(SCOPE.environment_id),
            "workflow": "support-chat",
        },
        headers=httpx.Headers({"Idempotency-Key": "purge-1"}),
        state=SimpleNamespace(principal=principal("execution_manager"), audit_context=CONTEXT),
    )

    async def body():
        return b'{"confirm_workflow":"support-chat"}'

    request.body = body
    response = await controller.purge(request)  # type: ignore[arg-type]

    assert seen == {"workflow": "support-chat", "confirm": "support-chat", "key": "purge-1"}
    assert response.status_code == 200


async def test_the_controller_decodes_the_list_query_as_the_wire_sends_it():
    seen = {}

    class Recording:
        async def list(self, actor, scope, query, *, context):
            seen["query"] = query
            from firefly_weave.contracts.public import Page

            return Page[object](items=[], next_cursor=None)

    controller = AgentConversationController(Recording())  # type: ignore[arg-type]
    request = SimpleNamespace(
        path_params={
            "tenant": str(SCOPE.tenant_id),
            "project": str(SCOPE.project_id),
            "environment": str(SCOPE.environment_id),
        },
        query_params=QueryParams({"workflow": "support-chat", "limit": "10"}),
        state=SimpleNamespace(principal=principal("viewer"), audit_context=CONTEXT),
    )

    response = await controller.list(request)  # type: ignore[arg-type]

    assert response.status_code == 200
    assert (seen["query"].workflow, seen["query"].limit, seen["query"].cursor) == ("support-chat", 10, None)
