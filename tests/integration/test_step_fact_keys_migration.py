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

"""The step fact key migration on real PostgreSQL: the facts stay as they were, and the keys under an activation get
an index in byte order that holds only keys with a '#'."""

import importlib.util
from pathlib import Path

import pytest
from operations_support import seed_prior_head
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PATH = Path(__file__).resolve().parents[2] / "migrations/versions/0048_operations_step_fact_keys.py"
FACTS = "SELECT run_id,node_id,instance_key,kind,status FROM step_facts ORDER BY run_id,node_id,instance_key"


def migration():
    spec = importlib.util.spec_from_file_location("step_fact_keys_migration", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEAD = migration().revision
PRIOR_HEAD = migration().down_revision


async def test_upgrading_keeps_the_facts_and_indexes_the_keys_under_activations(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            for key in ("", "support#1", "support#1.1", "support#1.1.review", "support[2]", "support[2]#1"):
                await connection.execute(
                    text(
                        "INSERT INTO step_facts(tenant_id,project_id,environment_id,run_id,node_id,instance_key,"
                        "kind,status) VALUES(:tenant,:project,:environment,:first_run,'support',:key,'agent',"
                        "'succeeded')"
                    ),
                    {**ids, "key": key},
                )
            before = (await connection.execute(text(FACTS))).all()
            await upgrade(connection, HEAD)

            assert (await connection.execute(text(FACTS))).all() == before
            assert await connection.scalar(
                text("SELECT indexdef FROM pg_indexes WHERE indexname='step_facts_key_range'")
            ) == (
                "CREATE INDEX step_facts_key_range ON public.step_facts USING btree (run_id, node_id, instance_key "
                "COLLATE \"C\") WHERE (strpos(instance_key, '#'::text) > 0)"
            )
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
    finally:
        await engine.dispose()
