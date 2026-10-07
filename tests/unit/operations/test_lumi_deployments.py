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

"""Operations context has an allowlist and no provider data or mutation proposals."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.lumi import LumiAskRequest
from firefly_weave.operations.lumi import LumiService
from firefly_weave.operations.lumi_deployments import deployment_context


@pytest.mark.parametrize(
    "kind", ["deployment-target", "deployment", "deployment-observation", "deployment-plan", "deployment-job"]
)
def test_projection_excludes_authority_provider_values_and_paths(kind):
    component = {
        "name": "worker",
        "kind": "worker",
        "replicas": 3,
        "cpu_millis": 500,
        "memory_mib": 1024,
        "image": "private.registry/image@sha256:" + "a" * 64,
        "configuration": "private-config",
        "external_identity": "/private/provider/path",
        "version": "private-version",
        "state": "ready",
        "ready_replicas": 2,
        "ownership": "managed",
    }
    payload = {
        "name": "target",
        "revision": 1,
        "adapter": "kubernetes",
        "capabilities": ["observe"],
        "disabled": False,
        "external_identity": "/private/provider/path",
        "boundary": "private-boundary",
        "runner_principal_id": "private-principal",
        "provider_config": "private-config",
        "logs": "private-logs",
        "components": [component],
        "resources": [component],
        "steps": [{"action": "scale_workers", "component": component}],
        "receipt": {
            "code": "applied",
            "external_effects_may_continue": False,
            "changed_resources": ["/private/resource"],
        },
    }
    result = deployment_context(kind, SimpleNamespace(model_dump=lambda **kwargs: payload))
    encoded = json.dumps(result)
    assert "private" not in encoded
    assert "provider_config" not in encoded and "runner_principal_id" not in encoded
    if kind == "deployment":
        assert result["components"][0]["replicas"] == 3
        assert result["components"][0]["image_digest"] == "sha256:" + "a" * 64
    if kind == "deployment-job":
        assert result["receipt"]["changed_resource_count"] == 1


async def test_attachment_uses_existing_scoped_read_and_propagates_denial():
    deployments = SimpleNamespace(read=AsyncMock(side_effect=AccessDenied()))
    service = LumiService(SimpleNamespace(definitions=None), None, None, None, deployments)
    actor = SimpleNamespace(id=uuid4())
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    identifier = uuid4()
    request = LumiAskRequest(message="Explain", attachments=[{"kind": "deployment-plan", "id": identifier}])
    context = AuditContext()
    with pytest.raises(AccessDenied):
        await service._attachments(actor, scope, request, context)
    deployments.read.assert_awaited_once_with("plans", identifier, actor=actor, scope=scope, context=context)


@pytest.mark.parametrize("revoke", [False, True])
async def test_explanation_discards_proposals_and_rechecks_access_after_gateway(monkeypatch, revoke):
    from contextlib import asynccontextmanager

    import firefly_weave.operations.lumi as module
    from firefly_weave.contracts.lumi import LumiReply

    actor = SimpleNamespace(id=uuid4())
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())

    @asynccontextmanager
    async def transaction(*args, **kwargs):
        yield SimpleNamespace(scope=scope)

    connections = SimpleNamespace(definitions=SimpleNamespace(transaction=transaction), secrets=None)
    record = SimpleNamespace(model_dump=lambda **kwargs: {"name": "target", "adapter": "kubernetes"})
    deployments = SimpleNamespace(read=AsyncMock(side_effect=[record, AccessDenied() if revoke else record]))
    reply = LumiReply(
        answer="Saved snapshot explanation",
        proposals=[{"title": "Unexpected", "kind": "workflow", "format": "yaml", "source": "kind: Workflow"}],
    )
    gateway = SimpleNamespace(configured=True, ask=AsyncMock(return_value=reply))
    service = LumiService(connections, None, None, gateway, deployments)
    service._require = AsyncMock(return_value=actor)
    service._read = AsyncMock(
        return_value=SimpleNamespace(
            enabled=True, revision=1, connection_revision_id=uuid4(), profile=SimpleNamespace(timeout_seconds=1)
        )
    )
    service._connection = lambda *args: "handle"
    monkeypatch.setattr(
        module,
        "ConnectionRepository",
        lambda tx: SimpleNamespace(revision=AsyncMock(return_value=SimpleNamespace(config={}))),
    )
    monkeypatch.setattr(module, "resolve_secret", AsyncMock(return_value=SimpleNamespace(value="private-provider-key")))
    request = LumiAskRequest(message="Explain", attachments=[{"kind": "deployment-target", "id": uuid4()}])
    if revoke:
        with pytest.raises(AccessDenied):
            await service.ask(actor, scope, request, context=AuditContext())
    else:
        result = await service.ask(actor, scope, request, context=AuditContext())
        assert result.answer == reply.answer and result.proposals == []
    assert deployments.read.await_count == 2
    gateway.ask.assert_awaited_once()
    context = gateway.ask.await_args.args[2]
    assert context["purpose"] == "explain-operations" and "authoringSchemas" not in context
    assert "private-provider-key" not in json.dumps(context)
