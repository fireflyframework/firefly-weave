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

"""A template build drops only the other templates nobody is using."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


async def test_build_keeps_templates_in_use_or_recently_used(empty_settings, retain, template_sweep):
    sweep, used, stale_after = template_sweep
    control = make_url(os.environ["WEAVE_TEST_DATABASE_URL"])
    prefix = "weave_b1_template_" + uuid4().hex[:8]
    names = {kind: f"{prefix}_{kind}" for kind in ("recent", "connected", "old", "unmarked")}
    admin = create_async_engine(control, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            now = int(await connection.scalar(text("SELECT floor(extract(epoch FROM now()))")))
            for kind, name in names.items():
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
                retain("database", name)
                if kind != "unmarked":
                    seen = now - 60 if kind == "recent" else now - stale_after - 60
                    await connection.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{used}{seen}'"))
            holder = create_async_engine(control.set(database=names["connected"]))
            try:
                async with holder.connect() as held:
                    await held.execute(text("SELECT 1"))
                    await sweep(connection, "weave_b1_template_none")
            finally:
                await holder.dispose()
            left = set(
                await connection.scalars(
                    text("SELECT datname FROM pg_database WHERE datname LIKE :prefix"), {"prefix": prefix + "%"}
                )
            )
    finally:
        await admin.dispose()
    assert left == {names["recent"], names["connected"]}
