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
"""Configuring Weave AI checks `lumi.manage` on the principal stored now, and reads the provider connection and
audits the change as the credential that made the request."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import firefly_weave.operations.lumi as module
from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied, AuthorizationService
from firefly_weave.access.models import CredentialIdentity, Grant, Principal
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiConfigurationRequest
from firefly_weave.operations.lumi import LumiService

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
PRINCIPAL_ID = uuid4()
IDENTITY = CredentialIdentity(provider_id="corporate", issuer="https://id.example", subject="ada", client_id="studio")
REQUEST = LumiConfigurationRequest(
    connection_revision_id=uuid4(),
    profile={
        "provider": "openai-chat",
        "model": "gpt-4o",
        "options": {"max_tokens": 1024},
        "outputSchema": LUMI_REPLY_SCHEMA,
    },
)


def principal(*roles, identity=None):
    grants = tuple(Grant(role=role, scope=SCOPE) for role in roles)
    return Principal(id=PRINCIPAL_ID, kind="human", grants=grants, credential_identity=identity)


class Session:
    """The transaction's session, keeping each statement the configuration writes."""

    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    async def execute(self, statement, parameters):
        self.statements.append((str(statement), parameters))

    def audited(self):
        return [
            json.loads(parameters["event"])
            for statement, parameters in self.statements
            if statement.startswith("INSERT INTO access_audit")
        ]


def configurer(monkeypatch, stored):
    """A Weave AI service whose stored principal is ``stored``; it records who reads the provider connection."""
    session = Session()
    readers: list[Principal] = []

    @asynccontextmanager
    async def transaction(*args, **kwargs):
        yield SimpleNamespace(scope=SCOPE, session=session)

    async def load_principal(session, identifier):
        return stored

    async def read(actor, scope, identifier, *, context, tx):
        readers.append(actor)
        return SimpleNamespace()

    authorization = AuthorizationService()
    definitions = SimpleNamespace(
        transaction=transaction,
        require=lambda actor, scope, capability, context: authorization.require(
            actor, scope, capability, context=context
        ),
    )
    monkeypatch.setattr(module, "load_principal", load_principal)
    service = LumiService(
        SimpleNamespace(definitions=definitions, read=read),
        None,
        None,
        SimpleNamespace(configured=True),
        None,
        SimpleNamespace(current=lambda: None),
        None,
        None,
    )
    service._read = AsyncMock(return_value=None)
    service._connection = lambda *args: None
    return service, session, readers


async def test_configuring_reads_the_connection_and_audits_as_the_credential_that_asked(monkeypatch):
    # The stored principal is the same person without the credential: the connection's owning check and the
    # change's audit record must still name the identity that made the request.
    token = principal("lumi_manager", identity=IDENTITY)
    service, session, readers = configurer(monkeypatch, stored=principal("lumi_manager"))
    result = await service.configure(token, SCOPE, REQUEST, None, context=AuditContext())
    assert result.revision == 1
    assert readers == [token]
    [record] = session.audited()
    assert (record["action"], record["identity"]) == ("lumi.configure", IDENTITY.model_dump())


async def test_configuring_is_denied_when_the_stored_principal_lost_lumi_manage(monkeypatch):
    # The credential still carries the role, but the principal stored now does not: nothing is read or written.
    token = principal("lumi_manager", identity=IDENTITY)
    service, session, readers = configurer(monkeypatch, stored=principal("lumi_user"))
    with pytest.raises(AccessDenied):
        await service.configure(token, SCOPE, REQUEST, None, context=AuditContext())
    assert readers == []
    assert session.statements == []
