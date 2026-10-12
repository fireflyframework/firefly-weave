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

"""The reserved no-credential handle is never resolved, and keyless AI connections need no secret."""

import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave import private_origins as po
from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authorization import AccessDenied
from firefly_weave.access.models import Principal
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.diagnostics import keyless_connection
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant, SecretUnavailable
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR
from firefly_weave.contracts.connectors import NO_CREDENTIAL, ConnectionRequest, ConnectionTestResult
from firefly_weave.definitions.models import CatalogError
from firefly_weave.sdk import platform

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
ACTOR = Principal(id=uuid4(), kind="human")
CONTEXT = AuditContext(request_id=uuid4())
OLLAMA = "http://ollama:11434"
POLICY = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
    [po.PrivateOrigin(origin=OLLAMA, purpose="model", networks=("10.246.21.0/24",), credentials="none")]
)


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class Session:
    """In-memory stand-in for the statements connection create, read and test issue."""

    def __init__(self):
        self.revisions, self.statements = {}, []

    async def execute(self, statement, values=None):
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("INSERT INTO connection_revisions"):
            self.revisions[values["id"]] = json.loads(values["payload"])
        if sql.startswith("SELECT id,kind,active FROM principals"):
            return Result([{"id": values["id"], "kind": "human", "active": True}])
        return Result()

    async def scalar(self, statement, values=None):
        sql = str(statement)
        if "max(revision)" in sql:
            return 1
        if sql.startswith("SELECT payload FROM connection_revisions"):
            return self.revisions.get(values["id"])
        if "platform_administrators" in sql:
            return None
        raise AssertionError(sql)


class Definitions:
    def __init__(self, session):
        self.session = session

    def require(self, actor, scope, capability, context):
        return None

    @asynccontextmanager
    async def transaction(self, scope, supplied, *, mutation=True):
        yield SimpleNamespace(session=self.session, scope=scope)

    async def connector_contract(self, actor, scope, identifier, *, capability, context, tx, resource=None):
        manifest = AGENTIC_DESCRIPTOR.manifest.value
        return {
            "kind": "Connector",
            "name": "weave-agentic-provider",
            "version": "1.0.0",
            "retired": False,
            "definition_digest": FrozenDocument.from_value(manifest).digest,
            "document": manifest,
            "artifact": {"executable": {"dependencies": []}},
        }


class Adapter:
    def __init__(self):
        self.tested = []

    async def execute(self, *args):
        raise AssertionError("Connections are never executed here")

    async def test_connection(self, connection):
        self.tested.append(connection)
        return ConnectionTestResult(ok=True)


class Unresolvable(ScopedSecrets):
    def resolve(self, scope, handle):
        raise AssertionError("A keyless connection must never resolve a secret")


def service(secrets=None):
    adapter = Adapter()
    registry = ConnectorRegistry()
    registry.register_descriptor(AGENTIC_DESCRIPTOR, adapter)
    session = Session()
    connections = ConnectionService(SimpleNamespace(), Definitions(session), registry, secrets or ScopedSecrets())

    class Open:
        @asynccontextmanager
        async def open(self, scope, mutation=True):
            yield SimpleNamespace(session=session, scope=scope)

    connections.uow = Open()
    return connections, adapter


def ollama_request():
    return ConnectionRequest(
        name="ollama-local",
        connector_version_id=uuid4(),
        config={"provider": "openai-chat", "endpoint": OLLAMA + "/v1", "secretSlot": "apiKey"},
        secretRef={"apiKey": NO_CREDENTIAL},
        allowed_destinations=(OLLAMA,),
    )


def test_an_operator_grant_under_the_reserved_name_is_ignored(caplog):
    with caplog.at_level(logging.WARNING, logger="weave.secrets"):
        secrets = ScopedSecrets({"env": EnvironmentSecretProvider()}, (SecretGrant(SCOPE, NO_CREDENTIAL, "env", "X"),))
    assert "reserved no-credential handle" in caplog.text
    with pytest.raises(CatalogError):
        secrets.check(SCOPE, NO_CREDENTIAL)
    with pytest.raises(SecretUnavailable):
        secrets.resolve(SCOPE, NO_CREDENTIAL)


def test_the_reserved_handle_is_present_only_for_keyless_connections():
    secrets = ScopedSecrets()
    secrets.check(SCOPE, NO_CREDENTIAL, keyless=True)
    with pytest.raises(CatalogError):
        secrets.check(SCOPE, NO_CREDENTIAL)
    with pytest.raises(CatalogError):
        secrets.check(SCOPE, "openai-api-key", keyless=True)


def test_keyless_connections_are_agentic_connections_on_an_approved_origin():
    config = {"provider": "openai-chat", "endpoint": OLLAMA + "/v1", "secretSlot": "apiKey"}
    with po.installed(POLICY):
        assert keyless_connection("weave-agentic-provider", config, {"apiKey": NO_CREDENTIAL})
        assert not keyless_connection("weave-http-v2", config, {"apiKey": NO_CREDENTIAL})
    with po.installed(po.PrivateOrigins.empty()):
        assert not keyless_connection("weave-agentic-provider", config, {"apiKey": NO_CREDENTIAL})


async def test_a_keyless_connection_is_created_and_tested_without_any_secret():
    connections, adapter = service(Unresolvable())
    with po.installed(POLICY):
        revision = await connections.create_revision(ACTOR, SCOPE, ollama_request(), context=CONTEXT)
        result = await connections.test_connection(ACTOR, SCOPE, revision.id, context=CONTEXT)
        ready = await connections.ready_revision(ACTOR, SCOPE, revision.id, "connection.manage", context=CONTEXT)
    assert revision.secret_refs == {"apiKey": NO_CREDENTIAL}
    assert result.ok is True and len(adapter.tested) == 1
    assert ready.id == revision.id
    with pytest.raises(SecretUnavailable):
        adapter.tested[0].credentials("apiKey")


async def test_ready_revisions_are_authorized_before_any_lookup():
    connections, _ = service()

    def refuse(actor, scope, capability, context):
        raise AccessDenied()

    connections.definitions.require = refuse
    with pytest.raises(AccessDenied):
        # An unknown revision is refused like a known one, so existence is not revealed.
        await connections.ready_revision(ACTOR, SCOPE, uuid4(), "connection.manage", context=CONTEXT)


async def test_without_the_private_origin_entry_the_connection_is_refused():
    connections, _ = service()
    with po.installed(po.PrivateOrigins.empty()), pytest.raises(CatalogError) as refused:
        await connections.create_revision(ACTOR, SCOPE, ollama_request(), context=CONTEXT)
    paths = {item["path"] for item in refused.value.result["diagnostics"]}
    assert {"/config/endpoint", "/secretRef/apiKey"} <= paths


def test_the_platform_secret_store_refuses_the_reserved_handle():
    with pytest.raises(platform.PlatformError, match="reserved"):
        platform._check_handle(NO_CREDENTIAL)
