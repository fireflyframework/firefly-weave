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
"""Teams profile rejects unbound identities before any provider side effect."""

from uuid import uuid4

import pytest


def config():
    return dict(
        account_id=str(uuid4()),
        registration_tenant=str(uuid4()),
        tenant_id=str(uuid4()),
        conversation_id="conv/1",
        bot_id="28:bot",
        user_id="29:user",
        installation_generation=1,
        allowed_tenants=[],
    )


def test_source_policy_requires_exact_complete_installation():
    from firefly_weave.providers.teams.policy import TeamsProfile

    data = config()
    data["allowed_tenants"] = [data["tenant_id"]]
    profile = TeamsProfile.model_validate(data)
    assert profile.installation_generation == 1
    for key in ("account_id", "conversation_id", "installation_generation"):
        with pytest.raises(ValueError):
            TeamsProfile.model_validate({k: v for k, v in data.items() if k != key})
    with pytest.raises(ValueError):
        TeamsProfile.model_validate({**data, "unexpected": None})
    with pytest.raises(ValueError):
        TeamsProfile.model_validate({**data, "allowed_tenants": []})


def test_service_url_binding_preserves_authenticated_path():
    from firefly_weave.providers.teams.policy import service_url

    assert service_url("https://smba.trafficmanager.net/emea/") == "https://smba.trafficmanager.net/emea/"
    for bad in ("http://example.com", "https://x/a/../b", "https://x/a%2fb", "https://u:p@x", "https://x/a?q=1"):
        with pytest.raises(ValueError):
            service_url(bad)


async def test_signed_profile_normalizes_without_outbound_token(signed_activity):
    import json
    from datetime import UTC, datetime

    import httpx

    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.providers import ProviderSource
    from firefly_weave.providers.teams.bridge import TeamsVerifier

    data, activity, token, jwks = signed_activity

    class Client:
        calls = []

        async def request_bounded(self, method, url, **kwargs):
            self.calls.append((method, url))
            assert method == "GET" and url == "https://login.botframework.com/v1/.well-known/keys"
            return httpx.Response(200, content=jwks)

    source = ProviderSource(
        id=uuid4(),
        name="teams",
        provider="teams",
        package="firefly-weave",
        package_version="0.1.0a1",
        adapter_version="1.0.0",
        schema_digest="a" * 64,
        connection_revision_id=uuid4(),
        policy=data,
        kind="run",
        activation_id=uuid4(),
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        binding_id=uuid4(),
        principal_id=uuid4(),
    )
    client = Client()
    verifier = TeamsVerifier(client, None)
    events, ack = await verifier.verify(
        source, json.dumps(activity).encode(), {"authorization": "Bearer " + token()}, datetime.now(UTC)
    )
    assert ack.status_code == 202 and events[0].payload["text"] == "hello"
    assert len(client.calls) == 1
    from firefly_weave.definitions.models import CatalogError

    for changes in (
        {"iss": "wrong"},
        {"aud": "wrong"},
        {"exp": 1},
        {"serviceurl": "https://smba.trafficmanager.net/other/"},
        {"iat": None},
        {"nbf": None},
        {"_headers": {"alg": "RS256"}},
        {"_headers": {"alg": "RS256", "kid": "unknown"}},
        {"_headers": {"alg": "HS256", "kid": "fixture"}},
    ):
        with pytest.raises(CatalogError):
            await verifier.verify(
                source,
                json.dumps(activity).encode(),
                {"authorization": "Bearer " + token(**changes)},
                datetime.now(UTC),
            )
    assert len(client.calls) == 2


async def test_machine_token_is_scoped_uncached_and_opaque(monkeypatch, caplog):
    import json
    from datetime import UTC, datetime, timedelta

    import httpx

    from firefly_weave.connections.machine_tokens import MachineProfile, MachineTokenService
    from firefly_weave.connectors.http import HttpPolicy
    from firefly_weave.contracts.connectors import (
        ActionContext,
        ConnectionRevision,
        ConnectorInvocation,
        ResolvedSecret,
    )

    calls = []
    authority = []

    async def auth():
        authority.append(True)

    async def credentials(slot):
        assert slot == "client_secret"
        return ResolvedSecret(value="private-secret", provider_version=None)

    response_payload = {"access_token": "opaque-bearer", "token_type": "Bearer", "expires_in": 3600}

    def receive(request):
        calls.append(request)
        assert request.url.path.endswith("/oauth2/v2.0/token")
        assert b"grant_type=client_credentials" in request.content and b"private-secret" in request.content
        return httpx.Response(200, json=response_payload)

    monkeypatch.setattr(
        "firefly_weave.connections.machine_tokens.PinnedTransport", lambda *args, **kwargs: httpx.MockTransport(receive)
    )
    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="teams",
        connector_version_id=uuid4(),
        connector="teams",
        connector_digest="a" * 64,
        adapter="weave-teams",
        secretRef={"client_secret": "handle"},
        allowed_destinations=("https://login.microsoftonline.com",),
    )
    context = ActionContext(
        "op",
        datetime.now(UTC) + timedelta(seconds=30),
        credentials,
        ConnectorInvocation(connection, {}, "send", {}, {}, 1048576, 1048576),
        auth,
    )
    profile = MachineProfile(
        client_id=str(uuid4()),
        endpoint="https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        scopes=("https://api.botframework.com/.default",),
    )
    service = MachineTokenService(HttpPolicy())
    first = await service.acquire(context, profile)
    second = await service.acquire(context, profile)
    assert len(calls) == 2 and len(authority) >= 4
    assert "opaque-bearer" not in repr(first)
    with pytest.raises(TypeError):
        json.dumps(first)
    assert first is not second
    from firefly_weave.contracts.connectors import ConnectorFailure

    response_payload["scope"] = "unrequested.scope"
    with pytest.raises(ConnectorFailure):
        await service.acquire(context, profile)
    assert len(calls) == 3
    response_payload.pop("scope")
    for lifetime in (0, -1, 5):
        response_payload["expires_in"] = lifetime
        with pytest.raises(ConnectorFailure):
            await service.acquire(context, profile)
    assert first.consume() == "opaque-bearer"
    with pytest.raises(ConnectorFailure):
        first.consume()

    import asyncio
    import logging

    caplog.set_level(logging.DEBUG)
    entered = asyncio.Event()
    closed = []

    class CancelledTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            logging.getLogger("httpcore.http11").debug("private-secret opaque-bearer")
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            logging.getLogger("httpcore.http11").debug("private-secret opaque-bearer cleanup")
            closed.append(True)

    monkeypatch.setattr(
        "firefly_weave.connections.machine_tokens.PinnedTransport", lambda *args, **kwargs: CancelledTransport()
    )
    task = asyncio.create_task(service.acquire(context, profile))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
    assert "private-secret" not in caplog.text and "opaque-bearer" not in caplog.text


def test_optional_builtin_declares_true_versions_and_native_types():
    from importlib.metadata import version

    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connectors.packages import PackageMetadata
    from firefly_weave.connectors.teams import TeamsConnector, package
    from firefly_weave.providers.teams.bridge import TeamsVerifier

    assert package.metadata.model.distribution_version == version("firefly-weave")
    assert package.metadata.model.version == "1.0.0"
    assert package.metadata.model.dispatch_event_kinds == ["message"]
    assert package.service_type is TeamsConnector and package.verifier_service_type is TeamsVerifier
    with pytest.raises(ValueError):
        PackageMetadata(package.metadata.canonical)
    registry = ConnectorRegistry(("firefly-weave:weave-teams:firefly_weave.connectors.teams:package",))
    assert registry.packages == (package,)


def test_disabled_provider_graph_does_not_import_optional_modules():
    import subprocess
    import sys

    source = """
import asyncio, importlib.abc, sys
class NoTeams(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('microsoft_agents', 'firefly_weave.providers.teams', 'firefly_weave.connectors.teams')):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoTeams())
from firefly_weave.app import make_app
from firefly_weave.settings import Settings
from firefly_weave.connections.registry import ConnectorRegistry
from firefly_weave.api.teams import TeamsController
from firefly_weave.connectors.execution import ConnectorExecutionService
from firefly_weave.definitions.models import CatalogError
app = make_app(Settings(scheduler_enabled=False, database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"))
async def run():
    await app.state.pyfly.startup()
    try:
        ctx = app.state.pyfly.context
        ctx.get_bean(TeamsController)
        ctx.get_bean(ConnectorExecutionService)
        try:
            ctx.get_bean(ConnectorRegistry).builtin_verifier('weave-teams')
        except CatalogError as error:
            assert error.status == 409
        else:
            raise AssertionError('Disabled provider resolved')
        forbidden = ('microsoft_agents', 'firefly_weave.providers.teams', 'firefly_weave.connectors.teams')
        assert not any(name.startswith(forbidden) for name in sys.modules)
    finally:
        await app.state.pyfly.shutdown()
asyncio.run(run())
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_jwks_fetch_uses_only_fixed_bounded_identity_endpoint():
    import asyncio

    import httpx

    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.providers.teams.bridge import TeamsVerifier
    from firefly_weave.providers.teams.policy import JWKS_URL

    class Client:
        response = httpx.Response(200, content=b'{"keys":[]}')
        delay = False
        calls = 0

        async def request_bounded(self, method, uri, **kwargs):
            self.calls += 1
            assert method == "GET" and uri == JWKS_URL
            assert kwargs["max_response_bytes"] == 262144
            assert kwargs["follow_redirects"] is False
            assert kwargs["headers"] == {"Accept-Encoding": "identity"}
            if self.delay:
                await asyncio.Event().wait()
            return self.response

    client = Client()
    verifier = TeamsVerifier(client, None)
    with pytest.raises(CatalogError):
        await verifier.fetch(JWKS_URL + "?substitute=1", timeout=1, max_bytes=999999)
    assert client.calls == 0
    for response in (
        httpx.Response(302, headers={"location": "https://127.0.0.1/private"}),
        httpx.Response(200, headers={"content-encoding": "gzip"}),
        httpx.Response(500),
    ):
        client.response = response
        with pytest.raises(CatalogError):
            await verifier.fetch(JWKS_URL, timeout=1, max_bytes=999999)
    client.delay = True
    with pytest.raises(TimeoutError):
        await verifier.fetch(JWKS_URL, timeout=0.01, max_bytes=999999)
