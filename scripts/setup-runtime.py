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

"""Provision a fresh retained local runtime database with separate app/migration identities."""

import argparse
import asyncio
import json
import os
import re
import secrets
from pathlib import Path
from uuid import uuid4

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from firefly_weave.persistence.migrations import migrate
from firefly_weave.settings import Settings


async def setup(output: Path) -> None:
    value = os.environ.get("WEAVE_TEST_DATABASE_URL", "")
    url = make_url(value)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"localhost", "127.0.0.1"}
        or url.database != "weave_b1_control"
        or url.username != "weave_b1_owner"
        or url.port in {None, 5432, 5434}
    ):
        raise ValueError("Explicit guarded local backend required")
    keycloak = os.environ.get("WEAVE_KEYCLOAK_TEST_URL", "http://localhost:18080")
    endpoint = re.fullmatch(r"http://localhost:([1-9][0-9]{3,4})", keycloak)
    if endpoint is None or not 1024 <= int(endpoint[1]) <= 65535:
        raise ValueError("Explicit guarded local Keycloak endpoint required")
    password = secrets.token_urlsafe(36)
    scheduler_password = secrets.token_urlsafe(36)
    suffix = uuid4().hex
    database, username = "weave_b2_dev_" + suffix, "weave_runtime_" + suffix
    scheduler_username = "weave_scheduler_" + suffix
    control = create_async_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    # Reserve the new owner-only destination before creating retained resources.
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        try:
            async with control.connect() as connection:
                marker = await connection.scalar(text("SELECT identity FROM weave_test_backend_guard"))
                if marker != "firefly-weave-b1-local-integration":
                    raise ValueError("Missing local backend guard")
                await connection.execute(text(f'CREATE DATABASE "{database}"'))
            migration = url.set(database=database)
            await migrate(Settings(database_url=migration.render_as_string(hide_password=False)))
            async with control.connect() as connection:
                await connection.execute(text(f"CREATE ROLE {username} LOGIN PASSWORD '{password}' IN ROLE weave_app"))
                await connection.execute(
                    text(
                        f"CREATE ROLE {scheduler_username} LOGIN PASSWORD '{scheduler_password}' "
                        "IN ROLE weave_scheduler"
                    )
                )
            runtime = migration.set(username=username, password=password)
            scheduler = migration.set(username=scheduler_username, password=scheduler_password)
            providers = [
                dict(
                    provider_id="local-keycloak",
                    issuer=keycloak + "/realms/weave",
                    jwks_uri=keycloak + "/realms/weave/protocol/openid-connect/certs",
                    audience="weave-api",
                    clients={"weave-host": "application", "weave-worker": "application", "weave-cli": "human"},
                    local_development=True,
                )
            ]
            stream.write("WEAVE_DATABASE_URL=" + runtime.render_as_string(hide_password=False) + "\n")
            stream.write("WEAVE_SCHEDULER_DATABASE_URL=" + scheduler.render_as_string(hide_password=False) + "\n")
            stream.write("WEAVE_MIGRATION_DATABASE_URL=" + migration.render_as_string(hide_password=False) + "\n")
            stream.write("WEAVE_OIDC_PROVIDERS='" + json.dumps(providers, separators=(",", ":")) + "'\n")
        finally:
            await control.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(setup(args.output))
    except Exception:
        raise SystemExit(
            "Runtime setup failed; check guarded backend/new secret destination. Retained resources are never dropped."
        ) from None
    print("Fresh retained runtime database provisioned; owner-only configuration written")
