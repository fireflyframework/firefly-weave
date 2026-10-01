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

"""Explicit privileged identity bootstrap. Imports stay lazy for offline CLI use."""

from pathlib import Path

import click


@click.group()
def admin() -> None:
    """Explicit local administrative operations."""


@admin.command("bootstrap")
@click.option("--provider", required=True)
@click.option("--issuer", required=True)
@click.option("--subject", required=True)
@click.option("--kind", type=click.Choice(["human", "application"]), required=True)
@click.option("--output", type=click.Path(path_type=Path), required=True)
def bootstrap(provider: str, issuer: str, subject: str, kind: str, output: Path) -> None:
    """Link an existing verified provider subject as platform administrator."""
    import asyncio
    import json
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from firefly_weave.access.service import bootstrap_identity

    async def run() -> None:
        url = os.environ.get("WEAVE_MIGRATION_DATABASE_URL")
        if not url:
            raise ValueError("Explicit migration credentials required")
        # Reserve a private receipt before mutating state; never overwrite caller files.
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        engine = create_async_engine(url, hide_parameters=True)
        try:
            with os.fdopen(descriptor, "w") as stream:
                identifier = await bootstrap_identity(
                    async_sessionmaker(engine), provider_id=provider, issuer=issuer, subject=subject, kind=kind
                )
                json.dump(
                    {"principal_id": str(identifier), "provider_id": provider, "issuer": issuer, "subject": subject},
                    stream,
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(run())
    except Exception:
        raise click.ClickException(
            "Bootstrap failed; verify explicit migration settings and new private output path"
        ) from None
    click.echo("Identity linked; private receipt written")


@admin.command("migrate")
def migrate() -> None:
    """Apply packaged migrations using explicit migration-owner credentials."""
    from firefly_weave.persistence.migrations import main

    main()
