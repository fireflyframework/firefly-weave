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

"""Actual pinned local Keycloak client-credentials acceptance (never fixture JWTs)."""

import os

import httpx
import pytest

pytestmark = pytest.mark.integration


async def test_live_keycloak_links_and_scoped_authorization(services, access_db, provisioned, release_backends):
    from firefly_weave.access.authentication import AuthenticationService, VerifierSet
    from firefly_weave.access.authorization import AccessDenied, AuthorizationService
    from firefly_weave.access.models import Grant
    from firefly_weave.access.oidc import AuthenticationFailed, OIDCVerifier, ProviderConfig

    endpoint = release_backends["keycloak_endpoint"](legacy_ports=(18080, 18081))
    secrets = [os.environ.get(name) for name in ("WEAVE_HOST_SECRET", "WEAVE_WORKER_SECRET", "WEAVE_DENIED_SECRET")]
    if not all(secrets):
        pytest.fail("Live Keycloak requires generated local client secret environment")
    tokens = []
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        try:
            discovery = await client.get(endpoint + "/realms/weave/.well-known/openid-configuration")
            if discovery.status_code != 200:
                pytest.fail("Local Keycloak realm discovery unavailable")
            assert discovery.json()["issuer"] == endpoint + "/realms/weave"
            for name, secret in zip(("weave-host", "weave-worker", "weave-denied"), secrets, strict=True):
                response = await client.post(
                    endpoint + "/realms/weave/protocol/openid-connect/token",
                    data={"grant_type": "client_credentials"},
                    auth=(name, secret),
                )
                if response.status_code != 200:
                    pytest.fail(
                        "Live client-credentials grant failed; retained realm may differ from local secrets/template"
                    )
                tokens.append(response.json()["access_token"])
        except httpx.HTTPError:
            pytest.fail("Local Keycloak backend unavailable", pytrace=False)
    config = ProviderConfig(
        provider_id="local-keycloak",
        issuer=endpoint + "/realms/weave",
        jwks_uri=endpoint + "/realms/weave/protocol/openid-connect/certs",
        audience="weave-api",
        clients={"weave-host": "application", "weave-worker": "application"},
        local_development=True,
    )
    verifier = OIDCVerifier(config)
    identities = [await verifier.verify(token) for token in tokens[:2]]
    with pytest.raises(AuthenticationFailed):
        await verifier.verify(tokens[2])
    wrong_audience = OIDCVerifier(config.model_copy(update={"audience": "wrong-audience"}))
    with pytest.raises(AuthenticationFailed):
        await wrong_audience.verify(tokens[0])
    sessions, _, service, _ = access_db
    admin, scopes = provisioned
    graph = services(sessions, verifiers=VerifierSet((verifier,)))
    authentication = graph.resolve(AuthenticationService)
    with pytest.raises(AuthenticationFailed):
        await authentication.authenticate(tokens[0])
    for identity, kind, role in zip(identities, ("application", "worker"), ("developer", "worker"), strict=True):
        principal_id = await service.create_principal(admin, kind)
        await service.link_identity(admin, principal_id, identity)
        await service.grant(admin, principal_id, Grant(role=role, scope=scopes[0]))
    host = await authentication.authenticate(tokens[0])
    worker = await authentication.authenticate(tokens[1])
    assert host.kind == "application" and worker.kind == "worker"
    authorization = graph.resolve(AuthorizationService)
    authorization.require(host, scopes[0], "definition.write")
    with pytest.raises(AccessDenied):
        authorization.require(host, scopes[1], "definition.write")
    with pytest.raises(AccessDenied):
        authorization.require(worker, scopes[0], "definition.write")
    # Exercise native routing with the real provider token and configured HTTP JWKS fetch.
    from httpx import ASGITransport, AsyncClient

    from firefly_weave.app import make_app
    from firefly_weave.settings import Settings

    await service.grant(admin, host.id, Grant(role="viewer", scope=scopes[0]))
    app = make_app(
        Settings(
            scheduler_enabled=False,
            database_url=access_db[3].render_as_string(hide_password=False),
            providers=(config,),
        )
    )
    own = f"/tenants/{scopes[0].tenant_id}/projects/{scopes[0].project_id}/environments/{scopes[0].environment_id}"
    other = f"/tenants/{scopes[1].tenant_id}/projects/{scopes[1].project_id}/environments/{scopes[1].environment_id}"
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        headers = {"Authorization": "Bearer " + tokens[0]}
        assert (await client.get(own, headers=headers)).status_code == 200
        assert (await client.get(other, headers=headers)).status_code == 403
    await service.set_active(admin, host.id, False)
    with pytest.raises(AuthenticationFailed):
        await authentication.authenticate(tokens[0])
