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

"""Explicit operator-only identity linking through the native authorized access graph."""

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from pyfly.container import Container
from pyfly.container.scanner import scan_package
from sqlalchemy.ext.asyncio import async_sessionmaker

from firefly_weave.access.identity_links import IdentityResolver
from firefly_weave.access.oidc import OIDCVerifier
from firefly_weave.access.service import AccessService
from firefly_weave.persistence.resources import DatabaseResources
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings


async def provision(output: Path, provider_id: str):
    settings = Settings.from_env()
    configs = [config for config in settings.providers if config.provider_id == provider_id]
    if len(configs) != 1:
        raise ValueError("Select one configured identity provider")
    config = configs[0]
    identities = []
    verifier = OIDCVerifier(config)
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for client_id, secret_name in (("weave-host", "WEAVE_HOST_SECRET"), ("weave-worker", "WEAVE_WORKER_SECRET")):
            response = await client.post(
                config.issuer + "/protocol/openid-connect/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, os.environ[secret_name]),
            )
            response.raise_for_status()
            identity = await verifier.verify(response.json()["access_token"])
            if identity.client_id != client_id or identity.actor_kind != "application":
                raise ValueError("Expected the explicitly configured machine identity")
            identities.append(identity)
    resources = DatabaseResources(settings)
    try:
        await resources.check_startup()
        graph = Container()
        for package in (
            "firefly_weave.access.service",
            "firefly_weave.access.authorization",
            "firefly_weave.access.identity_links",
        ):
            scan_package(package, graph)
        graph.register_instance(async_sessionmaker, resources.sessions)
        graph.register_instance(UnitOfWork, UnitOfWork(resources.sessions))
        actor = await graph.resolve(IdentityResolver).resolve(identities[0])
        access = graph.resolve(AccessService)
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            principal = await access.create_principal(actor, "worker")
            await access.link_identity(actor, principal, identities[1])
            json.dump(
                {
                    "complete": True,
                    "principal_id": str(principal),
                    "provider_id": identities[1].provider_id,
                    "issuer": identities[1].issuer,
                    "subject": identities[1].subject,
                    "kind": "worker",
                },
                stream,
                indent=2,
            )
            stream.write("\n")
        print("Verified worker identity linked; private receipt written")
    finally:
        await resources.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default="local-keycloak")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(provision(args.output, args.provider))
    except Exception:
        parser.exit(
            1,
            "Worker provisioning failed; verify the linked administrator, provider and new receipt path. "
            "Retain partial state for inspection.\n",
        )
