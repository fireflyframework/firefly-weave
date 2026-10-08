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

"""Public sign-in settings come only from validated server configuration and never from a request."""

import json
import os

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from firefly_weave.access.client_configuration import ClientConfigurationService
from firefly_weave.app import make_app
from firefly_weave.settings import Settings

DATABASE_URL = "postgresql+asyncpg://user@localhost:1234/db"
PATH = "/api/v1/client-configuration"
INVALID = "Invalid WEAVE_CLIENT_SIGN_IN configuration"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ("WEAVE_OIDC_PROVIDERS", "WEAVE_CLIENT_SIGN_IN", "WEAVE_DISPLAY_NAME"):
        monkeypatch.delenv(name, raising=False)


def acme(**overrides: object) -> dict[str, object]:
    return {
        "provider_id": "acme",
        "issuer": "https://login.example.com/acme/v2.0",
        "jwks_uri": "https://login.example.com/acme/discovery/keys",
        "audience": "api://weave-verifier",
        "clients": {"weave-cli": "human", "weave-portal": "human", "weave-host": "application"},
        "client_claim": "azp",
        **overrides,
    }


def local(**overrides: object) -> dict[str, object]:
    return {
        "provider_id": "local-keycloak",
        "issuer": "http://localhost:18080/realms/weave",
        "jwks_uri": "http://localhost:18080/realms/weave/protocol/openid-connect/certs",
        "audience": "weave-api",
        "clients": {"weave-cli": "human", "weave-host": "application", "weave-worker": "application"},
        "local_development": True,
        **overrides,
    }


def entry(**overrides: object) -> dict[str, object]:
    return {
        "provider_id": "acme",
        "display_name": "Acme (Microsoft Entra ID)",
        "client_id": "weave-cli",
        "scopes": ["openid", "api://weave/access"],
        **overrides,
    }


def settings(*entries: dict[str, object], providers: tuple[dict[str, object], ...] | None = None, **kw: object):
    return Settings(
        database_url=DATABASE_URL,
        providers=(acme(), local()) if providers is None else providers,
        client_sign_in=entries,
        **kw,
    )


def test_unconfigured_server_publishes_versioned_document_without_sign_in():
    configured = Settings(database_url=DATABASE_URL)
    assert configured.client_sign_in == ()
    assert configured.display_name is None
    assert ClientConfigurationService(configured).read().model_dump(mode="json") == {
        "service": "firefly-weave",
        "configuration_version": 1,
        "api_version": "weave/api-v1",
        "display_name": None,
        "sign_in": [],
    }


def test_issuer_and_loopback_trust_come_only_from_the_matching_provider():
    configured = settings(
        entry(trusted_endpoint_origins=["https://microsoft.com"], flows=["browser"]),
        entry(
            provider_id="local-keycloak",
            display_name="Local Keycloak (development)",
            scopes=["openid"],
            trusted_endpoint_origins=["http://127.0.0.1:18080"],
            require_refresh_rotation=False,
        ),
        display_name="Acme Weave",
    )
    service = ClientConfigurationService(configured)
    assert service.read() is service.read()
    assert service.read().model_dump(mode="json") == {
        "service": "firefly-weave",
        "configuration_version": 1,
        "api_version": "weave/api-v1",
        "display_name": "Acme Weave",
        "sign_in": [
            {
                "provider_id": "acme",
                "display_name": "Acme (Microsoft Entra ID)",
                "issuer": "https://login.example.com/acme/v2.0",
                "client_id": "weave-cli",
                "scopes": ["openid", "api://weave/access"],
                "trusted_endpoint_origins": ["https://microsoft.com"],
                "allow_loopback_http": False,
                "flows": ["browser"],
                "require_refresh_rotation": True,
            },
            {
                "provider_id": "local-keycloak",
                "display_name": "Local Keycloak (development)",
                "issuer": "http://localhost:18080/realms/weave",
                "client_id": "weave-cli",
                "scopes": ["openid"],
                "trusted_endpoint_origins": ["http://127.0.0.1:18080"],
                "allow_loopback_http": True,
                "flows": ["browser", "device"],
                "require_refresh_rotation": False,
            },
        ],
    }


def test_plain_http_issuer_is_published_only_for_a_local_development_provider():
    plain = {"issuer": "http://localhost:8080/realms/acme", "jwks_uri": "http://localhost:8080/realms/acme/certs"}
    with pytest.raises(ValidationError):
        settings(entry(), providers=(acme(**plain),))
    option = ClientConfigurationService(settings(entry(), providers=(acme(**plain, local_development=True),)))
    assert option.read().sign_in[0].issuer == plain["issuer"]
    assert option.read().sign_in[0].allow_loopback_http is True
    hosted = ClientConfigurationService(settings(entry(), providers=(acme(local_development=True),)))
    assert hosted.read().sign_in[0].allow_loopback_http is True


def test_published_document_carries_no_verifier_internals_or_application_clients():
    text = json.dumps(ClientConfigurationService(settings(entry())).read().model_dump(mode="json"))
    for hidden in (
        "jwks_uri",
        "discovery/keys",
        "audience",
        "api://weave-verifier",
        "client_claim",
        "azp",
        "token_class",
        "clients",
        "weave-host",
        "weave-portal",
        "local-keycloak",
        "secret",
    ):
        assert hidden not in text


@pytest.mark.parametrize(
    "value",
    [
        entry(provider_id="unknown"),
        entry(client_id="weave-host"),
        entry(client_id="not-a-configured-client"),
        entry(trusted_endpoint_origins=["http://device.example.com"]),
        entry(trusted_endpoint_origins=["http://localhost:9000"]),
        entry(provider_id="local-keycloak", trusted_endpoint_origins=["http://device.example.com"]),
        entry(trusted_endpoint_origins=["https://device.example.com/path"]),
        entry(trusted_endpoint_origins=["https://user@device.example.com"]),
        entry(trusted_endpoint_origins=["https://device.example.com", "https://device.example.com"]),
        entry(scopes=["open id"]),
        entry(scopes=["openid", "openid"]),
        entry(scopes=["x" * 201]),
        entry(flows=["browser", "browser"]),
        entry(display_name="Acme\nAdmin"),
        entry(display_name=" Acme"),
        entry(display_name="Acme\u007fAdmin"),
        entry(display_name="Acme\u0085Admin"),
        entry(display_name="Acme\u009b31mAdmin"),
        entry(display_name="Acme\u009fAdmin"),
    ],
)
def test_settings_reject_entries_the_server_could_not_publish(value):
    with pytest.raises(ValidationError, match=INVALID):
        settings(value)


@pytest.mark.parametrize("control", ["\u0000", "\u001b", "\u007f", "\u0080", "\u0085", "\u009b", "\u009f"])
def test_published_labels_and_identifiers_reject_c0_del_and_c1_controls(control):
    from firefly_weave.contracts.client_configuration import ClientConfiguration, SignInOption

    option = {
        "provider_id": "acme",
        "display_name": "Acme",
        "issuer": "https://login.example.com/acme/v2.0",
        "client_id": "weave-cli",
        "scopes": ["openid"],
    }
    assert SignInOption.model_validate(option).display_name == "Acme"
    for field in ("provider_id", "display_name", "client_id"):
        with pytest.raises(ValidationError):
            SignInOption.model_validate({**option, field: f"a{control}b"})
    with pytest.raises(ValidationError):
        ClientConfiguration(display_name=f"Acme{control}Weave")
    # Printable text beyond Latin-1 controls stays allowed.
    assert (
        ClientConfiguration(display_name="Acme \u00a0Weave \u00e9\u4e2d").display_name
        == "Acme \u00a0Weave \u00e9\u4e2d"
    )


@pytest.mark.parametrize(
    "value",
    [
        entry(scopes=[]),
        entry(scopes=[f"scope-{index}" for index in range(33)]),
        entry(flows=[]),
        entry(flows=["password"]),
        entry(display_name="x" * 101),
        entry(trusted_endpoint_origins=[f"https://device{index}.example.com" for index in range(9)]),
    ],
)
def test_settings_bound_each_entry_shape(value):
    with pytest.raises(ValidationError):
        settings(value)


def test_settings_reject_duplicate_providers_and_more_than_eight_entries():
    with pytest.raises(ValidationError, match=INVALID):
        settings(entry(), entry(display_name="Acme again"))
    providers = tuple(acme(provider_id=f"acme-{index}") for index in range(9))
    with pytest.raises(ValidationError):
        settings(*(entry(provider_id=f"acme-{index}") for index in range(9)), providers=providers)
    eight = settings(*(entry(provider_id=f"acme-{index}") for index in range(8)), providers=providers[:8])
    assert len(eight.client_sign_in) == 8


def test_settings_reject_an_ambiguous_provider_identifier():
    with pytest.raises(ValidationError, match=INVALID):
        settings(entry(), providers=(acme(), acme(issuer="https://other.example.com/acme")))


@pytest.mark.parametrize(
    "extra",
    [
        {"client_secret": "s3cr3t-value"},
        {"issuer": "https://attacker.example.com"},
        {"allow_loopback_http": True},
        {"jwks_uri": "https://attacker.example.com/keys"},
    ],
)
def test_entries_cannot_express_secrets_or_override_provider_trust(extra):
    with pytest.raises(ValidationError) as error:
        settings(entry(**extra))
    assert "s3cr3t-value" not in str(error.value)
    assert "attacker" not in str(error.value)


@pytest.mark.parametrize("name", ["", None])
def test_display_name_is_optional(monkeypatch, name):
    monkeypatch.setenv("WEAVE_DATABASE_URL", DATABASE_URL)
    if name is not None:
        monkeypatch.setenv("WEAVE_DISPLAY_NAME", name)
    configured = Settings.from_env()
    assert configured.display_name is None
    assert configured.client_sign_in == ()


def test_environment_round_trip(monkeypatch):
    monkeypatch.setenv("WEAVE_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("WEAVE_OIDC_PROVIDERS", json.dumps([acme(), local()]))
    monkeypatch.setenv("WEAVE_CLIENT_SIGN_IN", json.dumps([entry(), entry(provider_id="local-keycloak")]))
    monkeypatch.setenv("WEAVE_DISPLAY_NAME", "Acme Weave")
    configured = Settings.from_env()
    assert [item.provider_id for item in configured.client_sign_in] == ["acme", "local-keycloak"]
    assert configured.client_sign_in[0].flows == ("browser", "device")
    assert configured.client_sign_in[0].require_refresh_rotation is True
    assert configured.display_name == "Acme Weave"


@pytest.mark.parametrize(
    "raw",
    [
        "{not json s3cr3t-value",
        json.dumps({"provider_id": "s3cr3t-value"}),
        json.dumps([{**entry(), "client_secret": "s3cr3t-value"}]),
        json.dumps([entry(scopes="s3cr3t-value")]),
        pytest.param(json.dumps([entry(display_name="s3cr3t-value" * 4000)]), id="oversized-display-name"),
        json.dumps([entry(provider_id=f"s3cr3t-{index}") for index in range(9)]),
        b"[\xff s3cr3t-value]",
    ],
)
def test_environment_errors_do_not_echo_values(monkeypatch, raw):
    monkeypatch.setenv("WEAVE_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("WEAVE_OIDC_PROVIDERS", json.dumps([acme()]))
    if isinstance(raw, bytes):
        # Undecodable bytes reach os.environ as lone surrogates.
        monkeypatch.setitem(os.environb, b"WEAVE_CLIENT_SIGN_IN", raw)
    else:
        monkeypatch.setenv("WEAVE_CLIENT_SIGN_IN", raw)
    with pytest.raises(ValueError) as error:
        Settings.from_env()
    assert str(error.value) == INVALID
    assert "s3cr3t" not in str(error.value)


@pytest.mark.parametrize(
    "variable,raw",
    [
        ("WEAVE_CLIENT_SIGN_IN", json.dumps([entry(provider_id="s3cr3t-provider")])),
        ("WEAVE_CLIENT_SIGN_IN", json.dumps([entry(client_id="weave-host", display_name="s3cr3t-label")])),
        ("WEAVE_DISPLAY_NAME", "s3cr3t\u0007label"),
        ("WEAVE_DISPLAY_NAME", "s3cr3t\u0085label"),
        ("WEAVE_DISPLAY_NAME", "s3cr3t\u009b31mlabel"),
    ],
)
def test_cross_reference_errors_do_not_echo_values(monkeypatch, variable, raw):
    monkeypatch.setenv("WEAVE_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("WEAVE_OIDC_PROVIDERS", json.dumps([acme()]))
    monkeypatch.setenv(variable, raw)
    with pytest.raises(ValueError, match="Invalid " + variable + " configuration") as error:
        Settings.from_env()
    assert "s3cr3t" not in str(error.value)


@pytest.mark.parametrize("configured", [settings(entry(), display_name="Acme Weave"), settings()])
async def test_native_route_serves_the_document_built_at_startup(configured):
    app = make_app(configured)
    try:
        route = next(route for route in app.routes if getattr(route, "name", None) == "client_configuration.read")
        assert route.path == PATH
        assert route.methods == {"GET", "HEAD"}
        async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
            response = await client.get(PATH)
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/json"
            assert response.json() == ClientConfigurationService(configured).read().model_dump(mode="json")
            assert [option["issuer"] for option in response.json()["sign_in"]] == (
                ["https://login.example.com/acme/v2.0"] if configured.client_sign_in else []
            )
            head = await client.head(PATH)
            assert head.status_code == 200
            assert head.content == b""
    finally:
        await app.state.resources.close()


async def test_production_filter_chain_serves_the_document_anonymously(monkeypatch):
    from firefly_weave.persistence.resources import DatabaseResources

    async def database_ready(self):
        pass

    # Startup runs the real PyFly filter chain; only the schema probe is replaced, and compatibility stays unready.
    monkeypatch.setattr(DatabaseResources, "check_startup", database_ready)
    configured = settings(entry(), display_name="Acme Weave", scheduler_enabled=False)
    app = make_app(configured)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client,
    ):
        # A stale credential on the public read is ignored, never verified or reflected.
        for headers in ({}, {"Authorization": "Bearer stale-token-canary"}):
            response = await client.get(PATH, headers=headers)
            assert response.status_code == 200
            assert response.headers["x-weave-wire-version"] == "weave/api-v1"
            assert response.json() == ClientConfigurationService(configured).read().model_dump(mode="json")
            assert "stale-token-canary" not in str(response.headers) + response.text
        head = await client.head(PATH)
        assert head.status_code == 200
        assert head.content == b""
        for method, path in (("GET", PATH + "/"), ("OPTIONS", PATH), ("GET", "/api/v1/identity")):
            denied = await client.request(method, path)
            assert denied.status_code == 401, (method, path)
            assert denied.headers["www-authenticate"] == "Bearer"


async def test_anonymous_access_is_exact_path_and_read_methods():
    from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from firefly_weave.access.authentication import AuthenticationFilter, AuthenticationService, VerifierSet
    from firefly_weave.api.client_configuration import ClientConfigurationController

    controller = ClientConfigurationController(ClientConfigurationService(settings(entry())))

    async def read(request):
        return JSONResponse((await controller.read()).model_dump(mode="json"))

    app = Starlette(routes=[Route(PATH, read, methods=["GET"])])
    # Empty trusted verifier set makes authentication fail without database or OIDC IO.
    authentication = AuthenticationService(VerifierSet(()), None)
    app.add_middleware(WebFilterChainMiddleware, filters=[AuthenticationFilter(authentication)])
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        response = await client.get(PATH)
        assert response.status_code == 200
        assert response.headers["x-weave-wire-version"] == "weave/api-v1"
        assert response.json()["sign_in"][0]["issuer"] == "https://login.example.com/acme/v2.0"
        assert (await client.head(PATH)).status_code == 200
        for method, path in (
            ("POST", PATH),
            ("PUT", PATH),
            ("PATCH", PATH),
            ("DELETE", PATH),
            ("OPTIONS", PATH),
            ("GET", PATH + "/"),
            ("GET", PATH + "/private"),
            ("GET", PATH + "-private"),
            ("GET", "/client-configuration"),
            ("GET", "/api/v1/api/v1/client-configuration"),
            ("GET", "/api/v1/identity"),
        ):
            denied = await client.request(method, path)
            assert denied.status_code == 401, (method, path)
            assert denied.headers["www-authenticate"] == "Bearer"
            assert denied.json()["code"] == "WV-UNAUTHENTICATED"
    async with AsyncClient(transport=ASGITransport(app, root_path="/weave"), base_url="http://localhost") as client:
        assert (await client.get("/weave" + PATH)).status_code == 200
        assert (await client.head("/weave" + PATH)).status_code == 200
        assert (await client.post("/weave" + PATH)).status_code == 401
        assert (await client.get("/weave" + PATH + "/")).status_code == 401
        assert (await client.get("/weave/weave" + PATH)).status_code == 401


def test_operation_is_documented_public_without_bearer_or_problem_responses():
    from firefly_weave.contracts.openapi import export_openapi
    from firefly_weave.contracts.surface import OPERATIONS

    assert OPERATIONS["client_configuration.read"].public
    spec = export_openapi()
    operation = spec["paths"][PATH]["get"]
    assert operation["operationId"] == "client_configuration.read"
    assert operation["security"] == []
    assert operation["description"] == "Public sign-in settings for CLI and Studio onboarding."
    assert set(operation["responses"]) == {"200"}
    assert spec["paths"]["/health/live"]["get"]["description"] == "Public health probe."
    assert spec["paths"]["/health/ready"]["get"]["security"] == []
    assert spec["paths"]["/provider-ingress/{identifier}"]["post"]["security"] == []
    assert spec["paths"]["/webhooks/{identifier}"]["post"]["security"] == [{"webhookSignature": []}]
    assert spec["paths"]["/api/v1/identity"]["get"]["security"] == [{"bearer": []}]


def test_an_operation_without_a_capability_must_be_declared_public_and_described():
    from firefly_weave.contracts.surface import Operation

    # An empty capability can no longer silently document a bearer route as a public probe.
    with pytest.raises(ValueError, match="must be public and described"):
        Operation("example.read", "/api/v1/example", "GET", dict, "")
    with pytest.raises(ValueError, match="must be public and described"):
        Operation("example.read", "/api/v1/example", "GET", dict, "", public=True)
    assert Operation("example.read", "/api/v1/example", "GET", dict, "", public=True, description="Example.").public


async def test_only_public_operations_pass_the_filter_without_a_token():
    import re
    from uuid import uuid4

    from pyfly.web.adapters.starlette.filter_chain import WebFilterChainMiddleware
    from starlette.applications import Starlette

    from firefly_weave.access.authentication import AuthenticationFilter, AuthenticationService, VerifierSet
    from firefly_weave.contracts.surface import OPERATIONS

    app = Starlette()
    app.add_middleware(
        WebFilterChainMiddleware, filters=[AuthenticationFilter(AuthenticationService(VerifierSet(()), None))]
    )
    async with AsyncClient(transport=ASGITransport(app), base_url="http://localhost") as client:
        for operation in OPERATIONS.values():
            path = re.sub(
                r"{([^}]+)}",
                lambda match: "workflows" if match.group(1) == "collection" else str(uuid4()),
                operation.canonical_path,
            )
            response = await client.request(operation.method, path)
            # Without routes, a request the filter lets through reaches the router and is not found.
            if operation.public or operation.id == "webhooks.receive":
                assert response.status_code == 404, operation.id
            else:
                assert response.status_code == 401, operation.id


def test_standalone_schema_export_publishes_the_client_contract():
    from firefly_weave.contracts.schema_export import export_schemas

    schema = export_schemas()["client-configuration"]
    assert set(schema["properties"]) == {"service", "configuration_version", "api_version", "display_name", "sign_in"}
    assert schema["additionalProperties"] is False
