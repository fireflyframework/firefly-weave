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

"""Real PostgreSQL prefetch size, cursor, and nonblocking snapshot acceptance.

Fixtures retain task-owned databases and rows. This suite requires the explicitly
allocated integration backend; a missing backend is a failure, never a skip.
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text

from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.paging import PAGE_BYTES, page_ids
from firefly_weave.persistence.uow import UnitOfWork

pytestmark = pytest.mark.integration


async def insert_release(sessions, scope, identifier, characters, *, character="x"):
    # Raw retained rows deliberately exercise the pre-materialization boundary, before DTO validation.
    async with UnitOfWork(sessions).open(scope) as tx:
        await tx.session.execute(
            text(
                "INSERT INTO worker_releases(id,tenant_id,project_id,environment_id,image_digest,payload) "
                "VALUES(:id,:tenant,:project,:environment,:digest,jsonb_build_object('pad',repeat(:char,:count)))"
            ),
            {
                "id": identifier,
                "tenant": scope.tenant_id,
                "project": scope.project_id,
                "environment": scope.environment_id,
                "digest": "sha256:" + identifier.hex * 2,
                "char": character,
                "count": characters,
            },
        )


async def test_large_lookahead_is_not_charged_and_scope_filters_remain_exact(access_db, provisioned, monkeypatch):
    sessions, _, access, _ = access_db
    actor, scopes = provisioned
    scope, foreign = scopes
    neighbor_id, foreign_id, first, lookahead = sorted([uuid4(), uuid4(), uuid4(), uuid4()])
    current = await access.load_principal(actor.id)
    other_environment = await access.create_environment(
        current, scope.model_copy(update={"environment_id": None}), "other"
    )
    neighbor = scope.model_copy(update={"environment_id": other_environment})
    await insert_release(sessions, scope, first, 1)
    await insert_release(sessions, scope, lookahead, PAGE_BYTES + 1)
    await insert_release(sessions, foreign, foreign_id, PAGE_BYTES + 1)
    await insert_release(sessions, neighbor, neighbor_id, PAGE_BYTES + 1)
    async with UnitOfWork(sessions).open(scope, mutation=False) as tx:
        original = tx.session.execute
        selected_columns = []

        async def observed(statement, *args, **kwargs):
            result = await original(statement, *args, **kwargs)
            if str(statement).startswith("SELECT selected_row.id"):
                selected_columns.append(tuple(result.keys()))
            return result

        monkeypatch.setattr(tx.session, "execute", observed)
        ids = await page_ids(tx, "worker_releases", 1, None)
        assert ids == [first, lookahead]
        assert selected_columns == [("id", "logical_bytes")]
        with pytest.raises(CatalogError) as rejected:
            await page_ids(tx, "worker_releases", 1, first)
        assert rejected.value.code == "WV-PAGE-LIMIT"


async def test_server_uses_aggregate_utf8_bytes_not_characters(access_db, provisioned):
    sessions = access_db[0]
    scope = provisioned[1][0]
    first, second = sorted([uuid4(), uuid4()])
    for identifier in (first, second):
        await insert_release(sessions, scope, identifier, PAGE_BYTES // 4, character="é")
    async with UnitOfWork(sessions).open(scope, mutation=False) as tx:
        counts = (
            await tx.session.execute(
                text(
                    "SELECT sum(octet_length(to_jsonb(r)::text)),sum(char_length(to_jsonb(r)::text)) "
                    "FROM worker_releases r WHERE tenant_id=:tenant AND project_id=:project "
                    "AND environment_id=:environment"
                ),
                {"tenant": scope.tenant_id, "project": scope.project_id, "environment": scope.environment_id},
            )
        ).one()
        assert counts[0] > PAGE_BYTES > counts[1]
        assert await page_ids(tx, "worker_releases", 1, None) == [first, second]
        with pytest.raises(CatalogError) as rejected:
            await page_ids(tx, "worker_releases", 2, None)
        assert (rejected.value.status, rejected.value.code) == (429, "WV-PAGE-LIMIT")


async def test_exact_server_byte_ceiling_is_inclusive(access_db, provisioned):
    sessions = access_db[0]
    scope = provisioned[1][0]
    baseline, exact, overflow = sorted([uuid4(), uuid4(), uuid4()])
    await insert_release(sessions, scope, baseline, 0)
    async with UnitOfWork(sessions).open(scope, mutation=False) as tx:
        overhead = await tx.session.scalar(
            text("SELECT octet_length(to_jsonb(r)::text) FROM worker_releases r WHERE id=:id"), {"id": baseline}
        )
    await insert_release(sessions, scope, exact, PAGE_BYTES - overhead)
    await insert_release(sessions, scope, overflow, PAGE_BYTES - overhead + 1)
    async with UnitOfWork(sessions).open(scope, mutation=False) as tx:
        assert (
            await tx.session.scalar(
                text("SELECT octet_length(to_jsonb(r)::text) FROM worker_releases r WHERE id=:id"), {"id": exact}
            )
            == PAGE_BYTES
        )
        assert await page_ids(tx, "worker_releases", 1, baseline) == [exact, overflow]
        with pytest.raises(CatalogError) as rejected:
            await page_ids(tx, "worker_releases", 1, exact)
        assert rejected.value.code == "WV-PAGE-LIMIT"


async def test_repeatable_read_pins_checked_rows_without_blocking_a_writer(access_db, worker_setup, queued_task):
    sessions = access_db[0]
    scope = worker_setup[4]
    identifier = queued_task.id
    async with UnitOfWork(sessions).open(scope, mutation=False) as reader:
        assert await reader.session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await reader.session.scalar(text("SHOW transaction_read_only")) == "on"
        assert await page_ids(reader, "runs", 1, None) == [identifier]
        before = await reader.session.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": identifier})
        # Writer commits while the reader stays open; row locks or a read admission fence would block it.
        async with asyncio.timeout(10), UnitOfWork(sessions).open(scope) as writer:
            await writer.session.execute(
                text("UPDATE runs SET state=jsonb_set(state,'{input}',to_jsonb(repeat('é',:characters))) WHERE id=:id"),
                {"id": identifier, "characters": PAGE_BYTES // 2 + 1},
            )
        fetched = await reader.session.scalar(text("SELECT state FROM runs WHERE id=:id"), {"id": identifier})
        assert fetched == before
        assert await page_ids(reader, "runs", 1, None) == [identifier]
    async with UnitOfWork(sessions).open(scope, mutation=False) as fresh:
        with pytest.raises(CatalogError) as rejected:
            await page_ids(fresh, "runs", 1, None)
        assert rejected.value.code == "WV-PAGE-LIMIT"
