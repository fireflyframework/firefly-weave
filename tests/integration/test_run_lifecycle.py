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

"""Real database archive, atomic purge, permission, and replay boundaries."""

from uuid import UUID

import pytest
import test_runs as runs
from sqlalchemy import text

from firefly_weave.access.models import Grant

pytestmark = pytest.mark.integration
author = runs.author
publication_request = runs.publication_request
start_request = runs.start_request


async def test_archive_restore_purge_and_no_resurrection(
    client, headers, env_url, start_request, access_db, provisioned, author
):
    started = await client.post(
        env_url + "/runs", headers={**headers, "Idempotency-Key": "lifecycle-start"}, json=start_request
    )
    assert started.status_code == 201, started.text
    identifier = started.json()["id"]
    url = env_url + "/runs/" + identifier
    archive = {"expected_revision": 0, "reason": "Completed case"}
    h = {**headers, "Idempotency-Key": "archive-case"}
    archived = await client.post(url + "/archive", headers=h, json=archive)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] and archived.json()["revision"] == 1
    assert (await client.post(url + "/archive", headers=h, json=archive)).json() == archived.json()
    assert not (await client.get(env_url + "/runs", headers=headers)).json()["items"]
    assert len((await client.get(env_url + "/runs?include_archived=true", headers=headers)).json()["items"]) == 1
    assert (await client.get(url, headers=headers)).status_code == 200
    conflict = await client.post(url + "/restore", headers={**headers, "Idempotency-Key": "stale"}, json=archive)
    assert conflict.status_code == 409
    restored = await client.post(
        url + "/restore", headers={**headers, "Idempotency-Key": "restore"}, json={**archive, "expected_revision": 1}
    )
    assert restored.status_code == 200, restored.text
    assert not restored.json()["archived"] and restored.json()["revision"] == 2
    purge = {"expected_revision": 2, "reason": "Retention completed", "confirm_run_id": identifier}
    denied = await client.post(url + "/purge", headers={**headers, "Idempotency-Key": "purge"}, json=purge)
    assert denied.status_code == 403
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="execution_manager", scope=author[2]))
    not_archived = await client.post(url + "/purge", headers={**headers, "Idempotency-Key": "purge"}, json=purge)
    assert not_archived.status_code == 409
    archived = await client.post(
        url + "/archive",
        headers={**headers, "Idempotency-Key": "archive-again"},
        json={**archive, "expected_revision": 2},
    )
    assert archived.status_code == 200, archived.text
    purge["expected_revision"] = 3
    wrong = await client.post(
        url + "/purge",
        headers={**headers, "Idempotency-Key": "wrong"},
        json={**purge, "confirm_run_id": "00000000-0000-0000-0000-000000000001"},
    )
    assert wrong.status_code == 422
    purged = await client.post(url + "/purge", headers={**headers, "Idempotency-Key": "purge"}, json=purge)
    assert purged.status_code == 200, purged.text
    assert purged.json()["purged"] and purged.json()["revision"] == 4
    assert (
        await client.post(url + "/purge", headers={**headers, "Idempotency-Key": "purge"}, json=purge)
    ).json() == purged.json()
    assert (await client.get(url, headers=headers)).status_code == 404
    assert (await client.get(url + "/lifecycle", headers=headers)).json()["purged"]
    replay = await client.post(
        env_url + "/runs", headers={**headers, "Idempotency-Key": "lifecycle-start"}, json=start_request
    )
    assert replay.status_code == 410, replay.text
    async with access_db[1]() as observer:
        for table in ("runs", "run_events", "run_event_evidence", "step_instances", "runtime_capacity_blocks"):
            assert await observer.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        assert await observer.scalar(text("SELECT count(*) FROM access_audit WHERE action='run.purge'")) == 1
        assert not await observer.scalar(text("SELECT has_table_privilege('weave_app','runs','DELETE')"))


async def test_purge_reference_failure_is_atomic(
    client, headers, env_url, start_request, access_db, provisioned, author
):
    async def start(key):
        response = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": key}, json=start_request)
        assert response.status_code == 201, response.text
        return response.json()["id"]

    parent, child = await start("parent"), await start("child")
    # A retained retry relationship is independent evidence and must prevent purge.
    async with access_db[1]() as observer, observer.begin():
        scope = author[2]
        await observer.execute(
            text("INSERT INTO run_retry_links VALUES(:t,:p,:e,:child,:parent)"),
            {
                "t": scope.tenant_id,
                "p": scope.project_id,
                "e": scope.environment_id,
                "child": UUID(child),
                "parent": UUID(parent),
            },
        )
    await access_db[2].grant(provisioned[0], author[1].id, Grant(role="execution_manager", scope=author[2]))
    url = env_url + "/runs/" + parent
    response = await client.post(
        url + "/archive",
        headers={**headers, "Idempotency-Key": "archive-parent"},
        json={"expected_revision": 0, "reason": "Done"},
    )
    assert response.status_code == 200, response.text
    response = await client.post(
        url + "/purge",
        headers={**headers, "Idempotency-Key": "purge-parent"},
        json={"expected_revision": 1, "reason": "Done", "confirm_run_id": parent},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "WV-RUN-PURGE-BLOCKED"
    assert (await client.get(url, headers=headers)).status_code == 200
    replay = await client.post(env_url + "/runs", headers={**headers, "Idempotency-Key": "parent"}, json=start_request)
    assert replay.status_code == 201 and replay.json()["id"] == parent


async def test_archive_tombstone_is_charged_to_retained_bytes(access_db, author):
    from uuid import uuid4

    scope = author[2]
    async with access_db[1]() as observer, observer.begin():
        for key, value in scope.model_dump().items():
            await observer.execute(
                text("SELECT set_config(:key,:value,true)"), {"key": "weave." + key, "value": str(value)}
            )
        counter = text(
            "SELECT coalesce(sum(value),0) FROM operation_usage "
            "WHERE tenant_id=:t AND project_id=:p AND metric='ordinary_bytes'"
        )
        values = {"t": scope.tenant_id, "p": scope.project_id, "e": scope.environment_id, "id": uuid4()}
        before = await observer.scalar(counter, values)
        await observer.execute(
            text("INSERT INTO run_archives(tenant_id,project_id,environment_id,run_id) VALUES(:t,:p,:e,:id)"), values
        )
        size = await observer.scalar(
            text("SELECT octet_length(to_jsonb(a)::text) FROM run_archives a WHERE run_id=:id"), values
        )
        assert await observer.scalar(counter, values) == before + size
