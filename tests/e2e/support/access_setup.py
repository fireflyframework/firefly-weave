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

"""Native administration executed in the interpreter owning the selected schema."""

import os

import httpx
from pyfly.container import Container
from pyfly.container.scanner import scan_package
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from firefly_weave.access.models import Grant
from firefly_weave.access.oidc import OIDCVerifier, ProviderConfig
from firefly_weave.access.service import AccessService, bootstrap_identity
from firefly_weave.contracts.access import Scope
from firefly_weave.persistence.uow import UnitOfWork


async def provision(self, keycloak):
    self.engine = create_async_engine(self.runtime["WEAVE_DATABASE_URL"], hide_parameters=True)
    sessions = async_sessionmaker(self.engine, expire_on_commit=False)
    graph = Container()
    scan_package("firefly_weave.access", graph)
    graph.register_instance(async_sessionmaker, sessions)
    graph.register_instance(UnitOfWork, UnitOfWork(sessions))
    self.access = graph.resolve(AccessService)
    config = ProviderConfig(
        provider_id="local-keycloak",
        issuer=keycloak + "/realms/weave",
        jwks_uri=keycloak + "/realms/weave/protocol/openid-connect/certs",
        audience="weave-api",
        clients={"weave-host": "application", "weave-worker": "application"},
        local_development=True,
    )
    self.token_url = config.issuer + "/protocol/openid-connect/token"
    async with httpx.AsyncClient(trust_env=False) as client:
        for client_id, key in (
            ("weave-host", "WEAVE_HOST_SECRET"),
            ("weave-worker", "WEAVE_WORKER_SECRET"),
            ("weave-denied", "WEAVE_DENIED_SECRET"),
        ):
            response = await client.post(
                self.token_url, data={"grant_type": "client_credentials"}, auth=(client_id, os.environ[key])
            )
            assert response.status_code == 200, "Live token grant failed; retained realm/secrets must match"
            self.tokens.append(response.json()["access_token"])
    verifier = OIDCVerifier(config)
    identities = [await verifier.verify(token) for token in self.tokens[:2]]
    owner = create_async_engine(self.owner_url, hide_parameters=True)
    try:
        admin_id = await bootstrap_identity(
            async_sessionmaker(owner),
            provider_id="operator-bootstrap",
            issuer="urn:weave:local-operator",
            subject=self.tag,
            kind="application",
        )
        async with owner.connect() as connection:
            self.migration_head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await owner.dispose()
    self.admin = await self.access.load_principal(admin_id)
    self.host_id = await self.access.create_principal(self.admin, "application")
    self.worker_id = await self.access.create_principal(self.admin, "worker")
    self.native_id = await self.access.create_principal(self.admin, "worker")
    for identifier, identity in zip((self.host_id, self.worker_id), identities, strict=True):
        await self.access.link_identity(self.admin, identifier, identity)
    tenant = await self.access.create_tenant(self.admin, self.tag)
    other = await self.access.create_tenant(self.admin, self.tag + "-other")
    self.other_tenant = str(other)
    await self.access.grant(self.admin, self.host_id, Grant(role="tenant_admin", scope=Scope(tenant_id=tenant)))
    return tenant
