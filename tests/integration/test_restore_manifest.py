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

"""The restore manifest comparison against CHECK constraints that pg_restore re-created from their printed text."""

import asyncio
import importlib.util
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


def restore_module():
    path = Path(__file__).resolve().parents[2] / "scripts/restore_database.py"
    spec = importlib.util.spec_from_file_location("restore_database_checks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def recreate_checks(url: str, change: str | None = None) -> None:
    """Re-create every CHECK constraint from the text PostgreSQL prints for it, as pg_restore does, then ``change``."""
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.begin() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) FROM pg_constraint k "
                        "JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname='public' AND k.contype='c' AND k.coninhcount=0 ORDER BY 1,2"
                    )
                )
            ).all()
            for table, name, definition in rows:
                await connection.exec_driver_sql(
                    f'ALTER TABLE public."{table}" DROP CONSTRAINT "{name}", ADD CONSTRAINT "{name}" {definition}'
                )
            if change:
                await connection.exec_driver_sql(change)
    finally:
        await engine.dispose()


async def test_check_constraints_restored_from_their_printed_text_compare_equal(migration_settings):
    module = restore_module()
    url = migration_settings.database_url.get_secret_value()
    before = await module.manifest(url)
    await recreate_checks(url)
    after = await module.manifest(url)
    # The current schema has CHECKs that combine BETWEEN with another AND, which PostgreSQL prints with a nested
    # AND and parses back flat: the plain text comparison refuses this restore.
    assert before["data"] == after["data"]
    assert [name for name in before["catalog"] if before["catalog"][name] != after["catalog"][name]] == ["constraints"]

    await module.require_restored_manifest(before, after, partial(module.canonical_checks, url))

    assert await module.manifest(url) == after


@pytest.mark.parametrize(
    "change",
    [
        "ALTER TABLE alert_rules DROP CONSTRAINT alert_rules_for_seconds_check, ADD CONSTRAINT "
        "alert_rules_for_seconds_check CHECK (for_seconds BETWEEN 0 AND 7200 AND for_seconds % 30 = 0)",
        "ALTER TABLE alert_recipient_consents DROP CONSTRAINT alert_recipient_consents_address_check, ADD CONSTRAINT "
        "alert_recipient_consents_address_check CHECK (char_length(domain) BETWEEN 3 AND 254 AND strpos(domain,'@')>1)",
        "ALTER TABLE alert_rules DROP CONSTRAINT alert_rules_for_seconds_check",
        # The source's CHECK on for_seconds cannot be parsed on the restored table, so it keeps its own text.
        "ALTER TABLE alert_rules DROP COLUMN for_seconds",
    ],
    ids=["other-bounds", "other-column", "missing", "check-the-target-cannot-parse"],
)
async def test_a_check_constraint_that_really_differs_still_refuses_the_restore(migration_settings, change):
    module = restore_module()
    url = migration_settings.database_url.get_secret_value()
    before = await module.manifest(url)
    await recreate_checks(url, change)
    after = await module.manifest(url)

    with pytest.raises(ValueError, match="manifest differs"):
        await module.require_restored_manifest(before, after, partial(module.canonical_checks, url))


async def test_a_definition_carrying_a_second_command_gets_no_canonical_text(migration_settings):
    module = restore_module()
    url = migration_settings.database_url.get_secret_value()
    smuggled = ("alert_rules", "CHECK ((for_seconds > 0)); SELECT 1")

    assert await module.canonical_checks(url, {smuggled, ("alert_rules", "CHECK ((for_seconds > 0))")}) == {
        ("alert_rules", "CHECK ((for_seconds > 0))"): "CHECK ((for_seconds > 0))"
    }


async def test_a_locked_target_table_refuses_the_restore_instead_of_waiting(migration_settings):
    module = restore_module()
    url = migration_settings.database_url.get_secret_value()
    before = await module.manifest(url)
    await recreate_checks(url)
    after = await module.manifest(url)
    holder = create_async_engine(url, hide_parameters=True)
    try:
        async with holder.connect() as connection:
            await connection.execute(text("LOCK TABLE alert_rules IN ACCESS EXCLUSIVE MODE"))
            with pytest.raises(ValueError, match="manifest differs"):
                await asyncio.wait_for(
                    module.require_restored_manifest(before, after, partial(module.canonical_checks, url)),
                    timeout=module.CHECK_LOCK_TIMEOUT_SECONDS + 20,
                )
    finally:
        await holder.dispose()
