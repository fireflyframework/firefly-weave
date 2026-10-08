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

"""HTTP connector actions, HTTP profiles and signed webhooks reach private origins only through C8."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from firefly_weave import private_origins as po
from firefly_weave.connections.secrets import ResolvedSecret
from firefly_weave.connectors.egress import EgressDenied, EgressPolicy, SecureHttpClient
from firefly_weave.connectors.http import HttpConnector, HttpPolicy
from firefly_weave.connectors.http_profiles import HttpProfileConnector
from firefly_weave.contracts.connectors import ActionContext, ConnectionRevision, ConnectorFailure, ConnectorInvocation
from firefly_weave.operations.event_delivery import delivery_egress
from firefly_weave.settings import Settings

ACME = "http://acme.acceptance.test:8080"
FIXTURE = "10.231.1.37"


def acme_policy():
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [
            po.PrivateOrigin(origin=ACME, purpose=purpose, networks=("10.231.1.0/24",), credentials="bridge")
            for purpose in ("http-connector", "event-delivery")
        ]
    )


class Wire:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def request_bounded(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs["egress_policy"]))
        return self.responses.pop(0)


def v1_context(base, auth="none"):
    async def credentials(slot):
        return ResolvedSecret(value="token-value")

    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="acme",
        connector_version_id=uuid4(),
        connector="weave-http@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-http",
        config={"baseUrl": base, "auth": auth},
        secretRef={"token": "acme-token"} if auth == "bearer" else {},
        allowed_destinations=(base,),
    )
    invocation = ConnectorInvocation(
        connection=connection,
        config={"method": "GET", "path": "/items", "statuses": [200], "maxRedirects": 3},
        action="read",
        input_schema={"type": "object"},
        output_schema={},
        max_request_bytes=1024,
        max_response_bytes=1024,
    )
    return ActionContext("operation", datetime.now(UTC) + timedelta(seconds=10), credentials, invocation)


def test_an_approved_origin_is_reachable_only_inside_its_network():
    egress = HttpPolicy(origins=acme_policy()).egress("http-connector", (ACME,))
    egress.validate(ACME + "/orders/O-1/status", (FIXTURE,))
    for addresses in (("8.8.8.8",), ("10.231.2.5",), (FIXTURE, "8.8.8.8"), ("169.254.169.254",)):
        with pytest.raises(EgressDenied):
            egress.validate(ACME + "/orders/O-1/status", addresses)
    with pytest.raises(EgressDenied):
        egress.validate("http://other.acceptance.test:8080/", (FIXTURE,))


@pytest.mark.parametrize("purpose", ["http-connector", "event-delivery"])
@pytest.mark.parametrize("sends_credentials", [False, True])
def test_plain_http_to_a_public_address_keeps_working_as_today(purpose, sends_credentials):
    url, allowed, public = "http://api.example.test/x", ("http://api.example.test",), ("93.184.216.34",)
    for policy in (HttpPolicy(), HttpPolicy(("10.0.0.0/8",)), HttpPolicy(origins=acme_policy())):
        policy.egress(purpose, allowed, sends_credentials=sends_credentials).validate(url, public)
    HttpPolicy(("10.0.0.0/8",)).egress(purpose, allowed).validate(url, ("10.1.2.3",))
    with pytest.raises(EgressDenied):
        HttpPolicy().egress(purpose, allowed).validate(url, ("10.1.2.3",))
    # The connection's origin allowlist still applies to public addresses.
    with pytest.raises(EgressDenied):
        HttpPolicy().egress(purpose, ("http://other.example.test",)).validate(url, public)
    # A client that has not moved onto C8 keeps its legacy rule unchanged.
    EgressPolicy(allowed).validate(url, public)


def test_cgnat_keeps_needing_an_explicit_network():
    url, allowed = "http://tailnet.example.test/x", ("http://tailnet.example.test",)
    with pytest.raises(EgressDenied):
        HttpPolicy().egress("http-connector", allowed).validate(url, ("100.100.100.7",))
    listed = HttpPolicy(("100.64.0.0/10",)).egress("http-connector", allowed)
    listed.validate(url, ("100.100.100.7",))
    with pytest.raises(EgressDenied):
        listed.validate(url, ("100.100.100.200",))


def test_cluster_service_names_stay_refused_for_connectors_but_not_for_runners():
    origin = "http://weave-api.weave-system.svc:8000"
    policy = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [po.PrivateOrigin(origin=origin, purpose="runner", networks=("10.231.4.0/24",), credentials="bridge")]
    )
    EgressPolicy((origin,), purpose="runner", origins=policy).validate(origin + "/api", ("10.231.4.9",))
    with pytest.raises(EgressDenied):
        EgressPolicy((origin,), purpose="http-connector", origins=policy).validate(origin + "/api", ("10.231.4.9",))


async def test_mixed_answers_for_an_approved_origin_never_open_a_connection():
    async def resolver(host, port):
        return (FIXTURE, "8.8.8.8")

    with pytest.raises(EgressDenied):
        await SecureHttpClient().request_bounded(
            "GET",
            ACME + "/orders/O-1/status",
            max_response_bytes=100,
            egress_policy=HttpPolicy(origins=acme_policy()).egress("http-connector", (ACME,)),
            resolver=resolver,
        )


async def test_an_approved_development_origin_never_follows_redirects():
    wire = Wire(httpx.Response(302, headers={"location": ACME + "/items?page=2"}), httpx.Response(200, json={}))
    with pytest.raises(ConnectorFailure) as failure:
        await HttpConnector(wire, HttpPolicy(origins=acme_policy())).execute({}, v1_context(ACME))
    assert failure.value.code == "HTTP_REDIRECT" and len(wire.calls) == 1
    egress = wire.calls[0][2]
    assert egress.purpose == "http-connector" and egress.sends_credentials is False


async def test_legacy_private_networks_keep_same_origin_read_redirects():
    base = "http://api.internal.test"
    wire = Wire(
        httpx.Response(302, headers={"location": base + "/items?page=2"}), httpx.Response(200, json={"ok": True})
    )
    result = await HttpConnector(wire, HttpPolicy(("10.0.0.0/8",))).execute({}, v1_context(base))
    assert result == {"status": 200, "body": {"ok": True}} and len(wire.calls) == 2


async def test_bearer_requests_declare_their_credentials_to_the_policy():
    wire = Wire(httpx.Response(200, json={}))
    await HttpConnector(wire, HttpPolicy(origins=acme_policy())).execute({}, v1_context(ACME, auth="bearer"))
    assert wire.calls[0][2].sends_credentials is True


async def test_profile_requests_use_the_http_connector_purpose_and_declare_api_keys():
    async def credentials(slot):
        return ResolvedSecret(value="key-value")

    async def authorize():
        return None

    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="acme",
        connector_version_id=uuid4(),
        connector="weave-http@2.0.0",
        connector_digest="a" * 64,
        adapter="weave-http-v2",
        config={"baseUrl": ACME, "auth": {"kind": "api-key", "header": "X-Api-Key"}},
        secretRef={"api_key": "acme-api-key"},
        allowed_destinations=(ACME,),
    )
    config = {
        "profileVersion": "2.0.0",
        "method": "GET",
        "path": "/orders/{id}/status",
        "sideEffect": "read_only",
        "statuses": [200],
        "parameters": [{"name": "id", "location": "path", "type": "string", "required": True}],
    }
    invocation = ConnectorInvocation(
        connection, config, "read", {"type": "object"}, {"type": "object"}, 1048576, 1048576
    )
    context = ActionContext("op", datetime.now(UTC) + timedelta(seconds=10), credentials, invocation, authorize)
    wire = Wire(httpx.Response(200, json={"orderId": "O-1", "status": "shipped"}))
    result = await HttpProfileConnector(wire, HttpPolicy(origins=acme_policy()), None).execute(
        {"path": {"id": "O-1"}}, context
    )
    assert result == {"status": 200, "body": {"orderId": "O-1", "status": "shipped"}}
    _, url, egress = wire.calls[0]
    assert url == ACME + "/orders/O-1/status"
    assert egress.purpose == "http-connector" and egress.sends_credentials is True


def test_signed_webhooks_use_the_event_delivery_purpose():
    policy = HttpPolicy(origins=acme_policy())
    plain = delivery_egress(policy, (ACME,), "none")
    assert (plain.purpose, plain.sends_credentials) == ("event-delivery", False)
    assert delivery_egress(policy, (ACME,), "bearer").sends_credentials is True
    plain.validate(ACME + "/sink/alerts", (FIXTURE,))


@pytest.fixture
def clean_environment(monkeypatch):
    for key in [key for key in os.environ if key.startswith("WEAVE_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("WEAVE_DATABASE_URL", "postgresql+asyncpg://weave:pw@localhost:5432/weave")
    return monkeypatch


def test_server_settings_load_the_private_origin_file(tmp_path, clean_environment):
    path = tmp_path / "private-origins.json"
    path.write_bytes(po.render(acme_policy()))
    path.chmod(0o444)
    clean_environment.setenv("WEAVE_PRIVATE_ORIGINS_FILE", str(path))
    settings = Settings.from_env()
    assert [(entry.origin, entry.purpose) for entry in settings.private_origins.entries] == [
        (ACME, "event-delivery"),
        (ACME, "http-connector"),
    ]


def test_server_settings_refuse_an_unusable_private_origin_file(tmp_path, clean_environment):
    path = tmp_path / "private-origins.json"
    path.write_text('{"format": "weave/private-origins-v1"}')
    clean_environment.setenv("WEAVE_PRIVATE_ORIGINS_FILE", str(path))
    with pytest.raises(ValueError, match="private-origin"):
        Settings.from_env()


def test_the_server_installs_its_policy_for_connection_checks():
    from firefly_weave.app import make_app

    settings = Settings(database_url="postgresql+asyncpg://user@localhost:1234/db", private_origins=acme_policy())
    with po.installed(po.PrivateOrigins.empty()):
        make_app(settings)
        assert po.active() is settings.private_origins
