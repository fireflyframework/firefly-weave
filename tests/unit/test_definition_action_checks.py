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

"""Server compile and publish apply installed descriptor checks without a database fixture."""

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService

MANIFEST = HTTP_PROFILE_DESCRIPTOR.manifest.value
ACTION = {
    "apiVersion": "weave/v1alpha1",
    "kind": "Action",
    "metadata": {"name": "get-pet", "version": "1.0.0"},
    "spec": {
        "implementation": {
            "kind": "connector",
            "uses": "weave-http@2.0.0",
            "action": "read",
            "config": {
                "method": "GET",
                "path": "/v1/pets/{petId}",
                "sideEffect": "read_only",
                "parameters": [{"name": "petId", "location": "path", "type": "string", "required": True}],
                "statuses": [200],
            },
        },
        "sideEffect": "read_only",
        "timeoutSeconds": 30,
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "object",
                    "properties": {"petId": {"type": "string", "maxLength": 64}},
                    "required": ["petId"],
                    "additionalProperties": False,
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "outputSchema": MANIFEST["spec"]["actions"]["read"]["outputSchema"],
        "connection": {"connector": "weave-http@2.0.0"},
    },
}


class Result:
    def __init__(self, rows=(), one=(0, 0)):
        self.rows, self.single = list(rows), one

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def one(self):
        return self.single

    def __iter__(self):
        return iter(self.rows)


class Session:
    def __init__(self):
        self.statements = []

    async def execute(self, statement, values=None):
        sql = str(statement)
        self.statements.append(sql)
        if "count(*)" in sql:
            return Result(one=(1, 1))
        if sql.startswith("SELECT kind,name,version,document FROM definition_versions"):
            return Result([{"kind": "Connector", "name": "weave-http", "version": "2.0.0", "document": MANIFEST}])
        return Result()


class Workers:
    async def capabilities(self, tx, snapshot):
        return snapshot


class NeverExecute:
    async def execute(self, *args):
        raise AssertionError("Compilation never executes connectors")

    async def test_connection(self, *args):
        raise AssertionError("Compilation never tests connections")


@pytest.fixture
def service(monkeypatch):
    import firefly_weave.access.repository as repository

    session = Session()
    scope = Scope(tenant_id=uuid4(), project_id=uuid4())

    class UnitOfWork:
        @asynccontextmanager
        async def open(self, selected, mutation=True):
            yield SimpleNamespace(session=session, scope=selected, telemetry=None)

    actor = Principal(id=uuid4(), kind="human")

    async def load_principal(session, identifier):
        return actor

    monkeypatch.setattr(repository, "load_principal", load_principal)
    registry = ConnectorRegistry()
    registry.register_descriptor(HTTP_PROFILE_DESCRIPTOR, NeverExecute())
    definitions = DefinitionService(
        UnitOfWork(),
        SimpleNamespace(require=lambda *args, **kwargs: None),
        registry,
        SimpleNamespace(get=lambda: None),
        SimpleNamespace(get=Workers),
        SimpleNamespace(),
    )
    return SimpleNamespace(definitions=definitions, actor=actor, scope=scope, session=session)


def broken_action():
    value = copy.deepcopy(ACTION)
    value["spec"]["implementation"]["config"].update(method="POST", sideEffect="non_idempotent")
    value["spec"]["implementation"]["config"]["parameters"][0]["name"] = "id"
    return value


async def test_server_compile_uses_installed_action_checks(service):
    context = AuditContext(request_id=uuid4())
    ok = await service.definitions.compile(service.actor, service.scope, json.dumps(ACTION), "json", context=context)
    assert ok.ok, ok.diagnostics
    result = await service.definitions.compile(
        service.actor, service.scope, json.dumps(broken_action()), "json", context=context
    )
    paths = {d.path for d in result.diagnostics if d.severity == "error"}
    assert {
        "/spec/implementation/config/method",
        "/spec/implementation/config/sideEffect",
        "/spec/implementation/config/path",
        "/spec/implementation/config/parameters/0/name",
    } <= paths


async def test_publish_rejects_invalid_profile_action_before_any_write(service):
    with pytest.raises(CatalogError) as failure:
        await service.definitions.publish(
            service.actor,
            service.scope,
            "Action",
            json.dumps(
                {
                    **ACTION,
                    "spec": {
                        **ACTION["spec"],
                        "implementation": {**ACTION["spec"]["implementation"], "config": {"foo": 1}},
                    },
                }
            ),
            "json",
            "publish-key",
            context=AuditContext(request_id=uuid4()),
        )
    assert failure.value.status == 422 and failure.value.code == "WV-COMPILE"
    diagnostics = failure.value.result["diagnostics"]
    assert {"code": "WV-COMP-CONFIG_CONTRACT", "path": "/spec/implementation/config/foo"} in [
        {"code": d["code"], "path": d["path"]} for d in diagnostics
    ]
    assert not any(statement.lstrip().upper().startswith("INSERT") for statement in service.session.statements)
