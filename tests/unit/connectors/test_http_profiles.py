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
"""Wire serialization, credential authority and versioned outcome boundaries."""

import copy
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from firefly_weave.connections.secrets import ResolvedSecret
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.contracts.connectors import ActionContext, ConnectionRevision, ConnectorFailure, ConnectorInvocation


def module():
    assert importlib.util.find_spec("firefly_weave.connectors.http_profiles"), "HTTP profile executor is missing"
    from firefly_weave.connectors import http_profiles

    return http_profiles


class Wire:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or httpx.Response(200, json={"name": "ok"})

    async def request_bounded(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def context(auth=None, *, method="GET", effect="read_only", params=None, slots=None, output=None):
    auth = auth or {"kind": "none"}
    events = []

    async def authorize():
        events.append("authorize")

    async def credentials(slot):
        events.append(slot)
        return ResolvedSecret(
            value={"token": "SECRET-CANARY", "username": "user", "password": "pass", "api_key": "KEY"}[slot]
        )

    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="profile",
        connector_version_id=uuid4(),
        connector="fixture@1.0.0",
        connector_digest="a" * 64,
        adapter="fixture",
        config={"baseUrl": "https://api.example.test", "auth": auth},
        secretRef=slots or {},
        allowed_destinations=("https://api.example.test",),
    )
    config = {
        "profileVersion": "2.0.0",
        "method": method,
        "path": "/items/{id}",
        "sideEffect": effect,
        "statuses": [200],
        "parameters": params or [{"name": "id", "location": "path", "type": "string", "required": True}],
    }
    invocation = ConnectorInvocation(
        connection, config, "fetch", {"type": "object"}, output or {"type": "object"}, 1048576, 1048576
    )
    return ActionContext("op", datetime.now(UTC) + timedelta(seconds=10), credentials, invocation, authorize), events


async def test_path_is_encoded_with_fixed_origin_and_repeated_query_keys():
    ctx, events = context(
        params=[
            {"name": "id", "location": "path", "type": "string", "required": True},
            {"name": "tag", "location": "query", "type": "string", "array": True},
            {"name": "X-Request", "location": "header", "type": "string"},
        ]
    )
    wire = Wire()
    result = (
        await module()
        .HttpProfileConnector(wire, HttpPolicy(), None)
        .execute(
            {
                "path": {"id": "//attacker.invalid/a?token=x"},
                "query": {"tag": ["a b", "x&y"]},
                "headers": {"X-Request": "one"},
            },
            ctx,
        )
    )
    assert result == {"status": 200, "body": {"name": "ok"}}
    assert (
        wire.calls[0][1] == "https://api.example.test/items/%2F%2Fattacker.invalid%2Fa%3Ftoken%3Dx?tag=a%20b&tag=x%26y"
    )
    assert wire.calls[0][2]["headers"]["X-Request"] == "one"
    assert events == ["authorize", "authorize"]


@pytest.mark.parametrize("value", [".", "..", ""])
async def test_dot_and_empty_path_values_never_dispatch(value):
    ctx, _ = context()
    wire = Wire()
    with pytest.raises(ConnectorFailure):
        await module().HttpProfileConnector(wire, HttpPolicy(), None).execute({"path": {"id": value}}, ctx)
    assert wire.calls == []


async def test_bearer_is_slot_only_and_rechecks_authority():
    ctx, events = context({"kind": "bearer"}, slots={"token": "handle"})
    wire = Wire()
    await module().HttpProfileConnector(wire, HttpPolicy(), None).execute({"path": {"id": "one"}}, ctx)
    assert wire.calls[0][2]["headers"]["Authorization"] == "Bearer SECRET-CANARY"
    assert events == ["authorize", "token", "authorize"]


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(302, headers={"Location": "https://other.test"}), "HTTP_PROFILE_STATUS"),
        (httpx.Response(200, json={"name": "SECRET-CANARY"}), "HTTP_PROFILE_SENSITIVE"),
        (
            httpx.Response(200, content=b'{"x":1,"x":2}', headers={"Content-Type": "application/json"}),
            "HTTP_PROFILE_OUTPUT",
        ),
        (httpx.Response(200, content=b"not json"), "HTTP_PROFILE_OUTPUT"),
        (TimeoutError(), "HTTP_PROFILE_TIMEOUT"),
    ],
)
async def test_unsafe_started_response_failures_remain_unknown(response, code):
    ctx, _ = context({"kind": "bearer"}, method="POST", effect="non_idempotent", slots={"token": "handle"})
    wire = Wire(response)
    with pytest.raises(ConnectorFailure) as error:
        await module().HttpProfileConnector(wire, HttpPolicy(), None).execute({"path": {"id": "one"}}, ctx)
    assert error.value.code == code and error.value.outcome == "unknown" and len(wire.calls) == 1


async def test_no_authority_no_credentials_or_wire():
    ctx, events = context({"kind": "bearer"}, slots={"token": "handle"})
    wire = Wire()
    with pytest.raises(ConnectorFailure):
        await (
            module()
            .HttpProfileConnector(wire, HttpPolicy(), None)
            .execute({"path": {"id": "one"}}, replace(ctx, authorize=None))
        )
    assert not events and not wire.calls


@pytest.mark.parametrize("headers", [{"Authorization": "evil"}, {"X-Request": "one\r\nInjected: true"}])
async def test_undeclared_and_injected_headers_never_dispatch(headers):
    ctx, _ = context()
    wire = Wire()
    with pytest.raises(ConnectorFailure):
        await (
            module()
            .HttpProfileConnector(wire, HttpPolicy(), None)
            .execute({"path": {"id": "one"}, "headers": headers}, ctx)
        )
    assert wire.calls == []


def test_versioned_descriptor_normalizes_and_old_manifest_is_unchanged():
    from firefly_weave.compiler.catalog import FrozenDocument
    from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR
    from firefly_weave.contracts.definitions import ConnectorDefinition
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR

    normalized = ConnectorDefinition.model_validate(HTTP_PROFILE_DESCRIPTOR.manifest.value).model_dump(by_alias=True)
    assert FrozenDocument.from_value(normalized).digest == HTTP_PROFILE_DESCRIPTOR.manifest.digest
    assert HTTP_DESCRIPTOR.manifest.value["metadata"] == {"name": "weave-http", "version": "1.0.0"}
    assert HTTP_DESCRIPTOR.manifest.value["spec"]["adapter"] == "weave-http"
    assert HTTP_PROFILE_DESCRIPTOR.manifest.value["spec"]["adapter"] == "weave-http-v2"


async def test_native_registration_preserves_existing_machine_singleton():
    from pyfly.client.ports.outbound import BoundedHttpClientPort
    from pyfly.context import ApplicationContext
    from pyfly.core.config import Config

    from firefly_weave.connections.machine_tokens import MachineTokenService

    ctx = ApplicationContext(Config({}))
    ctx.container.register_instance(BoundedHttpClientPort, Wire())
    ctx.container.register_instance(HttpPolicy, HttpPolicy())
    ctx.register_bean(MachineTokenService)
    machine = ctx.get_bean(MachineTokenService)
    module().register_http_profile_services(ctx)
    module().register_http_profile_services(ctx)
    await ctx.start()
    try:
        adapter = ctx.get_bean(module().HttpProfileConnector)
        assert adapter.tokens is machine and ctx.get_bean(MachineTokenService) is machine
    finally:
        await ctx.stop()


async def test_processing_cannot_return_success_after_attempt_deadline(monkeypatch):
    import time

    ctx, _ = context(method="POST", effect="non_idempotent")
    ctx = replace(ctx, attempt_deadline=datetime.now(UTC) + timedelta(seconds=0.04))
    calls = []

    def validate(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            time.sleep(0.06)
        return ()

    monkeypatch.setattr(module(), "validate_payload", validate)
    with pytest.raises(ConnectorFailure) as error:
        await module().HttpProfileConnector(Wire(), HttpPolicy(), None).execute({"path": {"id": "one"}}, ctx)
    assert error.value.code == "HTTP_PROFILE_TIMEOUT" and error.value.outcome == "unknown"


async def test_machine_profile_uses_shared_service_and_rejects_client_secret_echo():
    import time

    from firefly_weave.connections.machine_tokens import MachineToken

    profiles = []

    class Tokens:
        async def acquire(self, ctx, profile):
            profiles.append(profile)
            return MachineToken("MACHINE-CANARY", time.monotonic() + 30, ("CLIENT-CANARY",))

    ctx, events = context(
        {
            "kind": "machine-token",
            "client_id": "client",
            "endpoint": "https://auth.example.test/token",
            "scopes": ["read:items"],
            "authentication": "client_secret_basic",
        },
        slots={"client_secret": "handle"},
    )
    wire = Wire(httpx.Response(200, json={"name": "CLIENT-CANARY"}))
    with pytest.raises(ConnectorFailure) as error:
        await module().HttpProfileConnector(wire, HttpPolicy(), Tokens()).execute({"path": {"id": "one"}}, ctx)
    assert error.value.code == "HTTP_PROFILE_SENSITIVE"
    assert len(profiles) == 1 and profiles[0].client_id == "client" and profiles[0].scopes == ("read:items",)
    assert profiles[0].authentication == "client_secret_basic"
    assert wire.calls[0][2]["headers"]["Authorization"] == "Bearer MACHINE-CANARY"
    assert events == ["authorize", "authorize"]


@pytest.mark.parametrize("secret,body", [("123456789", 123456789), ("true", True), ("null", None)])
async def test_generated_scalar_response_cannot_echo_resolved_credential(secret, body):
    import json

    from firefly_weave.sdk.openapi_import import import_openapi

    source = {
        "openapi": "3.1.1",
        "info": {"title": "Echo", "version": "1"},
        "servers": [{"url": "https://api.example.test"}],
        "components": {"securitySchemes": {"auth": {"type": "http", "scheme": "bearer"}}},
        "security": [{"auth": []}],
        "paths": {
            "/items/{id}": {
                "post": {
                    "operationId": "send",
                    "parameters": [
                        {"in": "path", "name": "id", "required": True, "schema": {"type": "string", "maxLength": 10}}
                    ],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "integer"
                                        if type(body) is int
                                        else "boolean"
                                        if body is True
                                        else "null"
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    rules = {
        "name": "scalar-echo",
        "version": "1.0.0",
        "auth": {"kind": "bearer"},
        "operations": {
            "send": {
                "name": "send",
                "sideEffect": "non_idempotent",
                "server": "https://api.example.test",
                "statuses": [200],
            }
        },
    }
    generated = import_openapi(source, ["send"], rules)
    assert generated.ok, generated.diagnostics
    ctx, _ = context(
        {"kind": "bearer"},
        method="POST",
        effect="non_idempotent",
        slots={"token": "handle"},
        output=generated.actions[0]["spec"]["outputSchema"],
    )

    async def credentials(slot):
        return ResolvedSecret(value=secret)

    ctx = replace(ctx, credentials=credentials)
    wire = Wire(httpx.Response(200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))
    with pytest.raises(ConnectorFailure) as error:
        await module().HttpProfileConnector(wire, HttpPolicy(), None).execute({"path": {"id": "x"}}, ctx)
    assert error.value.code == "HTTP_PROFILE_SENSITIVE" and error.value.outcome == "unknown"


@pytest.mark.parametrize("body", [b'{},"unexpected":1', b'null,"unexpected":1'])
async def test_response_must_be_exactly_one_standalone_json_value(body):
    ctx, _ = context(method="POST", effect="non_idempotent")
    wire = Wire(httpx.Response(200, content=body, headers={"Content-Type": "application/json"}))
    with pytest.raises(ConnectorFailure) as error:
        await module().HttpProfileConnector(wire, HttpPolicy(), None).execute({"path": {"id": "x"}}, ctx)
    assert error.value.code == "HTTP_PROFILE_OUTPUT" and error.value.outcome == "unknown"


@pytest.mark.parametrize("value,secret", [(123456789, "123456789"), (True, "true"), (None, "null"), (12.5, "12.5")])
def test_legacy_http_scalar_credential_guard(value, secret):
    from firefly_weave.connectors.http import _contains_credential

    assert _contains_credential({"echo": [value]}, secret)
    assert not _contains_credential({"echo": "ordinary-value"}, secret)


@pytest.mark.parametrize(
    "auth,slots",
    [
        ({"kind": "api-key", "header": "X-Key"}, {"api_key": "handle"}),
        ({"kind": "basic"}, {"username": "handle", "password": "handle"}),
    ],
)
async def test_other_auth_profiles_withhold_scalar_credential_echo(auth, slots):
    ctx, _ = context(auth, method="POST", effect="non_idempotent", slots=slots)

    async def credentials(slot):
        return ResolvedSecret(value="123456789")

    ctx = replace(ctx, credentials=credentials)
    wire = Wire(httpx.Response(200, json={"echo": 123456789}))
    with pytest.raises(ConnectorFailure) as error:
        await module().HttpProfileConnector(wire, HttpPolicy(), None).execute({"path": {"id": "x"}}, ctx)
    assert error.value.code == "HTTP_PROFILE_SENSITIVE" and error.value.outcome == "unknown"


def test_opaque_machine_token_and_client_secret_match_scalar_output():
    import time

    from firefly_weave.connections.machine_tokens import MachineToken

    token = MachineToken("123456789", time.monotonic() + 30, ("true",))
    assert module()._sensitive({"echo": 123456789}, [], token)
    assert module()._sensitive({"echo": True}, [], token)
    assert not module()._sensitive({"echo": "ordinary-value"}, [], token)
    token.discard()
