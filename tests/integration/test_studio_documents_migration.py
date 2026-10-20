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

"""The studio documents table on real PostgreSQL: upgrade, tenant isolation, narrow grants and usage."""

import json
from uuid import uuid4

import pytest
from operations_support import assert_usage, identifier, scope_as_app, seed_prior_head
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_incident_migration import upgrade

pytestmark = pytest.mark.integration
PRIOR_HEAD = "0033_language_workflow_calls"
HEAD = "0034_editor_studio_documents"
RUNS = text("SELECT to_jsonb(r)::text FROM runs r ORDER BY id")


@pytest.fixture
async def upgraded(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, HEAD)
        yield engine, ids
    finally:
        await engine.dispose()


async def insert(connection, ids, *, kind="test-data", revision=1, document=None, tenant=None):
    await connection.execute(
        text(
            "INSERT INTO studio_documents(tenant_id,project_id,draft_id,kind,revision,draft_revision,document,"
            "updated_by) VALUES(:tenant,:project,:draft,:kind,:revision,1,cast(:document AS jsonb),:principal)"
        ),
        {
            **ids,
            "tenant": tenant or ids["tenant"],
            "draft": ids.get("draft", identifier(9001)),
            "kind": kind,
            "revision": revision,
            "document": json.dumps(document if document is not None else {"schemaVersion": 1}),
        },
    )


async def test_the_upgrade_keeps_runs_and_records_the_schema_version(empty_settings):
    engine = create_async_engine(empty_settings.database_url.get_secret_value(), hide_parameters=True)
    try:
        async with engine.begin() as connection:
            await upgrade(connection, "0031_operations_key_collation")
            ids = await seed_prior_head(connection, count=20)
            await upgrade(connection, PRIOR_HEAD)
            before = (await connection.execute(RUNS)).scalars().all()
            await upgrade(connection, HEAD)
            after = (await connection.execute(RUNS)).scalars().all()
            assert before == after
            assert await connection.scalar(text("SELECT version FROM weave_schema_version")) == HEAD
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
            assert await connection.scalar(text("SELECT count(*) FROM studio_documents")) == 0
            await assert_usage(connection, ids)
    finally:
        await engine.dispose()


async def test_rows_belong_to_their_tenant_and_the_app_can_only_add_read_and_remove(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        policy = (
            await connection.execute(
                text(
                    "SELECT c.relrowsecurity,c.relforcerowsecurity,p.qual,p.with_check FROM pg_class c "
                    "JOIN pg_policies p ON p.tablename=c.relname AND p.policyname='tenant_scope' "
                    "WHERE c.relname='studio_documents'"
                )
            )
        ).one()
        assert policy[0] and policy[1] and "tenant_id" in policy[2] and policy[2] == policy[3]
        grants = {
            privilege: await connection.scalar(
                text("SELECT has_table_privilege('weave_app','studio_documents',:privilege)"),
                {"privilege": privilege},
            )
            for privilege in ("SELECT", "INSERT", "DELETE", "UPDATE")
        }
        assert grants == {"SELECT": True, "INSERT": True, "DELETE": True, "UPDATE": False}
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert(connection, ids)
        assert await connection.scalar(text("SELECT count(*) FROM studio_documents")) == 1
    other = identifier(4242)
    async with engine.begin() as connection:
        await scope_as_app(connection, ids, tenant=other)
        assert await connection.scalar(text("SELECT count(*) FROM studio_documents")) == 0
        with pytest.raises(DBAPIError):
            await insert(connection, ids)


@pytest.mark.parametrize(
    ("kind", "revision", "document"),
    [("layout", 1, {}), ("canvas", 0, {}), ("canvas", 1, [1])],
    ids=["an unknown kind", "revision zero", "a document that is not an object"],
)
async def test_the_table_refuses_what_the_api_never_writes(upgraded, kind, revision, document):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        with pytest.raises(DBAPIError):
            await insert(connection, ids, kind=kind, revision=revision, document=document)


async def test_documents_are_charged_to_usage_like_drafts(upgraded):
    engine, ids = upgraded
    async with engine.begin() as connection:
        await scope_as_app(connection, ids)
        await insert(connection, {**ids, "draft": uuid4()}, document={"schemaVersion": 1, "input": {"value": 1}})
        await insert(connection, {**ids, "draft": uuid4()}, kind="canvas", document={"version": 2})
    async with engine.begin() as connection:
        await assert_usage(connection, ids)
