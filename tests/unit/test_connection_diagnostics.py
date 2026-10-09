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

"""Connection create and test rejections explain each field without revealing secret handles."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.responses import JSONResponse

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.authentication import AuthenticationFilter
from firefly_weave.access.authorization import AuthorizationService
from firefly_weave.access.models import Grant, Principal
from firefly_weave.api.errors import ErrorAdvice
from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.connections.secrets import EnvironmentSecretProvider, ScopedSecrets, SecretGrant
from firefly_weave.connections.service import ConnectionService
from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR, POSTGRES_DESCRIPTOR
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ConnectionRequest, ConnectionRevision
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
from firefly_weave.contracts.public import Problem
from firefly_weave.definitions.models import CatalogError

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
GRANTED = "WEAVE_CONNECTION_SECRET_PETS"
VERSION = uuid4()


class NeverExecute:
    async def execute(self, *args):
        raise AssertionError("Validation never executes connectors")

    async def test_connection(self, *args):
        raise AssertionError("Validation never contacts providers")


def contract(descriptor):
    manifest = descriptor.manifest.value
    return {
        "kind": "Connector",
        "name": manifest["metadata"]["name"],
        "version": manifest["metadata"]["version"],
        "retired": False,
        "definition_digest": FrozenDocument.from_value(manifest).digest,
        "document": manifest,
        "artifact": {"executable": {"dependencies": []}},
    }


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class Session:
    """In-memory stand-in for the statements the create path issues."""

    def __init__(self):
        self.statements, self.idempotency, self.revisions = [], {}, {}

    async def execute(self, statement, values=None):
        sql = str(statement)
        self.statements.append(sql)
        key = (values or {}).get("key")
        if sql.startswith("SELECT request_hash,response FROM mutation_idempotency"):
            return Result([self.idempotency[key]] if key in self.idempotency else [])
        if sql.startswith("INSERT INTO mutation_idempotency"):
            self.idempotency[key] = {"request_hash": values["hash"], "response": json.loads(values["response"])}
        if sql.startswith("INSERT INTO connection_revisions"):
            self.revisions[values["id"]] = json.loads(values["payload"])
        return Result()

    async def scalar(self, statement, values=None):
        sql = str(statement)
        if "max(revision)" in sql:
            return 1 + sum(1 for item in self.revisions.values() if item["name"] == values["name"])
        if sql.startswith("SELECT payload FROM connection_revisions"):
            return self.revisions.get(values["id"])
        raise AssertionError(sql)

    def inserts(self):
        return sum(1 for sql in self.statements if sql.startswith("INSERT INTO connection_revisions"))


class Definitions:
    def __init__(self, selected, session):
        self.selected, self.session = selected, session

    def require(self, actor, scope, capability, context):
        return None

    @asynccontextmanager
    async def transaction(self, scope, supplied, *, mutation=True):
        yield SimpleNamespace(session=self.session, scope=scope)

    async def connector_contract(self, actor, scope, identifier, *, capability, context, tx, resource=None):
        if self.selected is None:
            raise CatalogError(422, "WV-CONNECTION", "Connector contract unavailable")
        return self.selected


def service(descriptor=HTTP_PROFILE_DESCRIPTOR, *, installed=True, selected=True):
    registry = ConnectorRegistry()
    if installed:
        registry.register_descriptor(descriptor, NeverExecute())
    secrets = ScopedSecrets({"env": EnvironmentSecretProvider()}, (SecretGrant(SCOPE, GRANTED, "env", GRANTED),))
    session = Session()
    definitions = Definitions(contract(descriptor) if selected else None, session)
    return ConnectionService(SimpleNamespace(), definitions, registry, secrets), session


def request(**changes):
    value = {
        "name": "pets",
        "connector_version_id": VERSION,
        "config": {"baseUrl": "https://api.example.com", "auth": {"kind": "api-key", "header": "X-API-Key"}},
        "secretRef": {"api_key": GRANTED},
        "allowed_destinations": ("https://api.example.com",),
    }
    value.update(changes)
    return ConnectionRequest.model_validate(value)


ACTOR = Principal(id=uuid4(), kind="human")
CONTEXT = AuditContext(request_id=uuid4())


async def rejection(connections, value):
    with pytest.raises(CatalogError) as failure:
        await connections.create_revision(ACTOR, SCOPE, value, context=CONTEXT)
    error = failure.value
    assert error.status == 422 and error.code == "WV-CONNECTION"
    return error


def pointers(error):
    return {(item["code"], item["path"]): item["message"] for item in error.result["diagnostics"]}


async def test_valid_profile_connection_is_created():
    connections, session = service()
    revision = await connections.create_revision(ACTOR, SCOPE, request(), context=CONTEXT)
    assert revision.adapter == "weave-http-v2" and revision.revision == 1 and session.inserts() == 1


@pytest.mark.parametrize(
    "changes,expected",
    [
        (
            {"config": {"baseUrl": "https://api.example.com/v1", "auth": {"kind": "none"}}, "secretRef": {}},
            {("WV-CONNECTION-CONFIG", "/config/baseUrl")},
        ),
        (
            {"config": {"baseUrl": "https://api.example.com", "auth": {"kind": "bearer"}}},
            {("WV-CONNECTION-SECRET", "/secretRef/api_key"), ("WV-CONNECTION-SECRET", "/secretRef/token")},
        ),
        (
            {"allowed_destinations": ("https://api.example.com/path",)},
            {("WV-CONNECTION-DESTINATION", "/allowed_destinations/0")},
        ),
        (
            {"allowed_destinations": ("https://api.example.com", "https://*.example.com")},
            {("WV-CONNECTION-DESTINATION", "/allowed_destinations/1")},
        ),
        (
            {"allowed_destinations": ("https://other.example.com",)},
            {("WV-CONNECTION-DESTINATION", "/allowed_destinations")},
        ),
        ({"secretRef": {"api_key": "WEAVE_CONNECTION_SECRET_OTHER"}}, {("WV-CONNECTION-SECRET", "/secretRef/api_key")}),
        ({"config": {"baseUrl": 7, "auth": {"kind": "none"}}}, {("WV-CONNECTION-CONFIG", "/config/baseUrl")}),
    ],
)
async def test_create_rejections_point_at_each_field(changes, expected):
    connections, session = service()
    error = await rejection(connections, request(**changes))
    assert set(pointers(error)) == expected, pointers(error)
    assert session.inserts() == 0


async def test_unavailable_handles_get_one_generic_message_without_names():
    connections, _ = service()
    error = await rejection(
        connections,
        request(
            config={"baseUrl": "https://api.example.com", "auth": {"kind": "basic"}},
            secretRef={"username": "WEAVE_CONNECTION_SECRET_MISSING", "password": "WEAVE_CONNECTION_SECRET_OTHER"},
        ),
    )
    found = pointers(error)
    assert set(found) == {
        ("WV-CONNECTION-SECRET", "/secretRef/username"),
        ("WV-CONNECTION-SECRET", "/secretRef/password"),
    }
    assert set(found.values()) == {"This secret handle is not available in this environment."}
    assert "WEAVE_CONNECTION_SECRET" not in json.dumps(error.result)
    assert GRANTED not in json.dumps(error.result)


async def test_connector_choice_failures_point_at_the_version():
    connections, _ = service(selected=False)
    error = await rejection(connections, request())
    assert set(pointers(error)) == {("WV-CONNECTION-CONNECTOR", "/connector_version_id")}
    connections, _ = service(installed=False)
    error = await rejection(connections, request())
    assert set(pointers(error)) == {("WV-CONNECTION-CONNECTOR", "/connector_version_id")}


async def test_schema_and_legacy_descriptor_failures_are_explained():
    connections, _ = service(HTTP_DESCRIPTOR)
    error = await rejection(
        connections,
        request(config={"baseUrl": "https://api.example.com", "auth": "basic"}, secretRef={}),
    )
    assert ("WV-CONNECTION-CONFIG", "/config/auth") in pointers(error)
    error = await rejection(
        connections,
        request(config={"baseUrl": "https://api.example.com/v1", "auth": "bearer"}, secretRef={"token": GRANTED}),
    )
    assert set(pointers(error)) == {("WV-CONNECTION-CONFIG", "/config/baseUrl")}
    error = await rejection(
        connections, request(config={"baseUrl": "https://api.example.com", "auth": "bearer"}, secretRef={})
    )
    assert set(pointers(error)) == {("WV-CONNECTION-SECRET", "/secretRef/token")}
    connections, _ = service(POSTGRES_DESCRIPTOR)
    error = await rejection(connections, request(allowed_destinations=("https://db.example.com",)))
    assert ("WV-CONNECTION-DESTINATION", "/allowed_destinations/0") in pointers(error)


async def test_problem_body_carries_typed_diagnostics():
    connections, _ = service()
    error = await rejection(connections, request(allowed_destinations=("https://other.example.com",)))
    advised = await ErrorAdvice().catalog_error(error)
    state = SimpleNamespace(audit_context=SimpleNamespace(request_id=uuid4()))
    problem = AuthenticationFilter.finish(SimpleNamespace(state=state), advised)
    body = json.loads(problem.body)
    assert isinstance(advised, JSONResponse) and problem.status_code == 422
    parsed = Problem.model_validate_json(json.dumps(body))
    assert parsed.code == "WV-CONNECTION"
    assert [(d.code, d.path) for d in parsed.diagnostics] == [("WV-CONNECTION-DESTINATION", "/allowed_destinations")]
    assert parsed.diagnostics[0].message.startswith("Add https://api.example.com")


async def test_optional_idempotency_key_replays_without_a_second_revision():
    connections, session = service()
    first = await connections.create_revision(ACTOR, SCOPE, request(), context=CONTEXT, idempotency_key="create-1")
    again = await connections.create_revision(ACTOR, SCOPE, request(), context=CONTEXT, idempotency_key="create-1")
    assert again == first and isinstance(again, ConnectionRevision) and session.inserts() == 1
    with pytest.raises(CatalogError) as conflict:
        await connections.create_revision(
            ACTOR, SCOPE, request(name="other"), context=CONTEXT, idempotency_key="create-1"
        )
    assert conflict.value.code == "WV-IDEMPOTENCY-CONFLICT"
    plain = await connections.create_revision(ACTOR, SCOPE, request(), context=CONTEXT)
    assert plain.revision == 2 and session.inserts() == 2
    with pytest.raises(CatalogError) as empty:
        await connections.create_revision(ACTOR, SCOPE, request(), context=CONTEXT, idempotency_key="")
    assert empty.value.code == "WV-IDEMPOTENCY"


async def test_connection_test_explains_why_a_saved_revision_is_not_ready(monkeypatch):
    connections, session = service()
    revision = await connections.create_revision(ACTOR, SCOPE, request(), context=CONTEXT)

    class Open:
        @asynccontextmanager
        async def open(self, scope, mutation=True):
            yield SimpleNamespace(session=session, scope=scope)

    connections.uow = Open()
    connections.secrets = ScopedSecrets({"env": EnvironmentSecretProvider()}, ())
    with pytest.raises(CatalogError) as failure:
        await connections.test_connection(ACTOR, SCOPE, revision.id, context=CONTEXT)
    assert failure.value.code == "WV-CONNECTION"
    assert set(pointers(failure.value)) == {("WV-CONNECTION-SECRET", "/secretRef/api_key")}
    # Activation bindings keep the opaque problem without field details.
    actor = ACTOR.model_copy(update={"grants": (Grant(role="deployer", scope=SCOPE, resources=(str(revision.id),)),)})
    connections.definitions.authorization = AuthorizationService()
    monkeypatch.setattr("firefly_weave.connections.service.load_principal", AsyncMock(return_value=actor))
    with pytest.raises(CatalogError) as binding:
        await connections.resolve_binding(actor, SCOPE, "pets", revision.id, revision.connector, context=CONTEXT)
    assert binding.value.code == "WV-CONNECTION" and binding.value.result is None


async def test_connection_tests_flag_plain_http_connections():
    connections, session = service()
    plain = await connections.create_revision(
        ACTOR,
        SCOPE,
        request(
            config={"baseUrl": "http://api.example.com", "auth": {"kind": "none"}},
            secretRef={},
            allowed_destinations=("http://api.example.com",),
        ),
        context=CONTEXT,
    )

    class Open:
        @asynccontextmanager
        async def open(self, scope, mutation=True):
            yield SimpleNamespace(session=session, scope=scope)

    connections.uow = Open()
    result = await connections.test_connection(ACTOR, SCOPE, plain.id, context=CONTEXT)
    # The flag describes the saved configuration, so it is reported whatever the check's outcome.
    assert result.encrypted is False
    assert result.model_dump(mode="json")["encrypted"] is False
