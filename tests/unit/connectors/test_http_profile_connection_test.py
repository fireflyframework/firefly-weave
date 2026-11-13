# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Testing an HTTP profile connection sends one HEAD request through the same egress rules as its actions."""

import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpcore
import httpx
import pytest

from firefly_weave import private_origins as po
from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.connections.machine_tokens import MachineToken
from firefly_weave.connections.secrets import ResolvedSecret
from firefly_weave.connections.service import ConnectionService
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.connectors.http_profiles import HttpProfileConnector
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import BoundConnection, ConnectionRevision

BASE = "https://api.acme.test"
ACTOR = Principal(id=uuid4(), kind="human")
SECRETS = {"api_key": "KEY-CANARY", "client_secret": "CLIENT-CANARY", "token": "TOKEN-CANARY"}


class Wire:
    """Records each request it sends; a refused destination is refused before anything is sent."""

    def __init__(self, response=None, *, addresses=("93.184.216.34",), events=None):
        self.calls = []
        self.response = response if response is not None else httpx.Response(200)
        self.addresses = addresses
        self.events = events if events is not None else []

    async def request_bounded(self, method, url, **kwargs):
        kwargs["egress_policy"].validate(url, self.addresses)
        # Copy what was sent: the connector clears its headers once the request ends.
        self.calls.append((method, url, {**kwargs, "headers": dict(kwargs.get("headers", {}))}))
        self.events.append(method)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def bound(base=BASE, auth=None, slots=None, destinations=None):
    auth = auth or {"kind": "api-key", "header": "X-Api-Key"}
    revision = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="acme",
        connector_version_id=uuid4(),
        connector="weave-http@2.0.0",
        connector_digest="a" * 64,
        adapter="weave-http-v2",
        config={"baseUrl": base, "auth": auth},
        secretRef=slots if slots is not None else {"api_key": "acme-api-key"},
        allowed_destinations=destinations or (base,),
    )
    return BoundConnection("test", revision, lambda name: ResolvedSecret(value=SECRETS[name]))


async def probe(wire, connection=None, tokens=None, policy=None):
    return await HttpProfileConnector(wire, policy or HttpPolicy(), tokens).test_connection(connection or bound())


async def test_sends_one_head_to_the_base_url_with_the_connections_key():
    wire = Wire()
    result = await probe(wire)
    assert result.ok is True and result.code == "ok"
    assert len(wire.calls) == 1
    method, url, kwargs = wire.calls[0]
    assert (method, url) == ("HEAD", BASE)
    assert kwargs["headers"]["X-Api-Key"] == "KEY-CANARY"
    assert kwargs["follow_redirects"] is False
    assert kwargs["max_response_bytes"] == 0
    assert kwargs["egress_policy"].purpose == "http-connector"
    assert kwargs["egress_policy"].sends_credentials is True
    assert 0 < kwargs["timeout"] <= 10


@pytest.mark.parametrize(
    "status, ok, reason",
    [
        (200, True, None),
        (204, True, None),
        (302, True, None),
        (404, True, None),
        (401, False, "unauthorized"),
        (403, False, "unauthorized"),
        (407, False, "unauthorized"),
        (500, False, "status"),
        (503, False, "status"),
    ],
)
async def test_a_404_still_means_connected_and_a_401_means_refused(status, ok, reason):
    result = await probe(Wire(httpx.Response(status)))
    assert (result.ok, result.code, result.reason) == (ok, "ok" if ok else "failed", reason)


async def test_a_refused_destination_sends_nothing():
    wire = Wire(addresses=("10.1.2.3",))
    result = await probe(wire)
    assert (result.ok, result.reason) == (False, "destination")
    assert wire.calls == []


@pytest.mark.parametrize("error", [httpcore.ConnectError("refused"), httpcore.ConnectTimeout("slow"), TimeoutError()])
async def test_a_connection_error_is_unreachable(error):
    result = await probe(Wire(error))
    assert (result.ok, result.reason) == (False, "unreachable")


async def test_a_machine_token_connection_asks_for_its_token_then_sends_the_head():
    events, profiles = [], []

    class Tokens:
        async def acquire(self, context, profile):
            profiles.append(profile)
            events.append("token")
            assert (await context.credentials("client_secret")).value == "CLIENT-CANARY"
            return MachineToken("MACHINE-CANARY", time.monotonic() + 30, ("CLIENT-CANARY",))

    auth = {
        "kind": "machine-token",
        "client_id": "weave",
        "endpoint": "https://auth.acme.test/token",
        "scopes": ["read:items"],
    }
    connection = bound(auth=auth, slots={"client_secret": "acme-client"}, destinations=(BASE, "https://auth.acme.test"))
    wire = Wire(events=events)
    result = await probe(wire, connection, Tokens())
    assert result.ok is True
    assert events == ["token", "HEAD"]
    assert profiles[0].client_id == "weave" and profiles[0].scopes == ("read:items",)
    assert wire.calls[0][2]["headers"]["Authorization"] == "Bearer MACHINE-CANARY"


async def test_the_result_carries_no_response_content():
    wire = Wire(httpx.Response(500, headers={"X-Echo": "KEY-CANARY"}, content=b"KEY-CANARY body"))
    result = await probe(wire)
    assert len(wire.calls) == 1
    assert set(result.model_dump()) <= {"ok", "code", "job_id", "encrypted", "reason"}
    assert "CANARY" not in result.model_dump_json()


async def test_only_a_connection_inside_its_profile_is_sent():
    inside, outside = Wire(), Wire()
    assert (await probe(inside, bound(slots={"api_key": "acme-api-key"}))).ok is True
    assert (await probe(outside, bound(slots={}))).ok is False
    assert (len(inside.calls), len(outside.calls)) == (1, 0)


CONTROL_PLANE = "http://api.weave-system.svc:8000"
METADATA = "http://169.254.169.254"


@pytest.mark.parametrize(
    "base, addresses",
    [(CONTROL_PLANE, ("10.231.4.9",)), (METADATA, ("169.254.169.254",))],
    ids=["control-plane", "metadata"],
)
async def test_a_control_plane_or_metadata_address_is_refused_and_sends_nothing(base, addresses):
    # Even an approved development origin list or a broad legacy network never opens these to connectors.
    development = po.PrivateOrigins(platform=po.PLATFORM)
    for policy in (HttpPolicy(), HttpPolicy(("0.0.0.0/0",)), HttpPolicy(origins=development)):
        wire = Wire(addresses=addresses)
        result = await probe(wire, bound(base=base), policy=policy)
        assert (result.ok, result.reason) == (False, "destination")
        assert wire.calls == []


class Session:
    def __init__(self, revision):
        self.revision, self.writes = revision, []

    async def execute(self, statement, values=None):
        self.writes.append((str(statement), values))

    async def scalar(self, statement, values=None):
        assert "FROM connection_revisions" in str(statement)
        return json.loads(self.revision.model_dump_json(by_alias=True))


@pytest.mark.parametrize(
    "status, reason", [(401, "unauthorized"), (503, "status"), (204, None)], ids=["refused", "error", "ok"]
)
async def test_the_platform_keeps_why_a_connection_test_failed_and_never_the_response(monkeypatch, status, reason):
    connection = bound()
    session = Session(connection.revision)
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())

    @asynccontextmanager
    async def opened(selected, mutation=True):
        yield SimpleNamespace(session=session, scope=selected)

    wire = Wire(httpx.Response(status, headers={"X-Echo": "KEY-CANARY"}))
    registry = SimpleNamespace(get=lambda adapter: HttpProfileConnector(wire, HttpPolicy(), None))
    secrets = SimpleNamespace(resolve=lambda selected, handle: ResolvedSecret(value="KEY-CANARY"))
    definitions = SimpleNamespace(require=lambda *a: None)
    service = ConnectionService(SimpleNamespace(open=opened), definitions, registry, secrets)
    monkeypatch.setattr(service, "_ready", AsyncMock())
    monkeypatch.setattr(service, "revalidate", AsyncMock())
    monkeypatch.setattr("firefly_weave.connections.service.audit", AsyncMock())

    result = await service.test_connection(ACTOR, scope, connection.revision.id, context=AuditContext())

    assert len(wire.calls) == 1 and wire.calls[0][2]["headers"]["X-Api-Key"] == "KEY-CANARY"
    assert (result.ok, result.reason) == (reason is None, reason)
    stored = [values["payload"] for sql, values in session.writes if "connection_test_results" in sql]
    assert len(stored) == 1 and json.loads(stored[0]).get("reason") == reason
    assert "CANARY" not in stored[0] + result.model_dump_json()
