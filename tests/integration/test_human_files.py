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

"""Task participants transfer only task-bound files without environment-wide file grants."""

import base64
import hashlib
from uuid import UUID

import pytest
import test_human_tasks
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.contracts.files import FileChunk, FileCreate
from firefly_weave.contracts.human_files import HumanFileChunk, HumanFileCommand, HumanFileCreate, HumanFileRead
from firefly_weave.files.human import HumanFileService

pytestmark = pytest.mark.integration
human_identity = test_human_tasks.human_identity
headers = test_human_tasks.headers
author = test_human_tasks.author
human_run = test_human_tasks.human_run


def metadata(data=b"evidence"):
    return FileCreate(
        filename="evidence.txt", contentType="text/plain", sizeBytes=len(data), sha256=hashlib.sha256(data).hexdigest()
    )


@pytest.fixture
async def file_form(monkeypatch):
    import json

    import yaml

    from firefly_weave.contracts.files import file_reference_schema

    document = yaml.safe_load(test_human_tasks.SOURCE)
    document["spec"]["steps"][0]["formSchema"]["properties"]["note"] = file_reference_schema()
    monkeypatch.setattr(test_human_tasks, "SOURCE", json.dumps(document))


@pytest.fixture
async def claimed(file_form, client, headers, env_url, human_run, author):
    _, task = human_run
    response = await test_human_tasks.mutate(
        client, headers, env_url, task, "claim", {"expected_revision": 1}, "claim-files"
    )
    assert response.status_code == 200, response.text
    return client._transport.app.state.pyfly.context.get_bean(HumanFileService), author[1], author[2], UUID(task["id"])


async def test_participant_upload_resume_download_without_file_role(claimed, client, headers, env_url):
    service, actor, scope, task = claimed
    request = HumanFileCreate(expected_revision=2, file=metadata())
    created = await service.create(actor, scope, task, request, "file-request", context=AuditContext())
    replayed = await service.create(actor, scope, task, request, "file-request", context=AuditContext())
    assert replayed.file.id == created.file.id
    chunk = HumanFileChunk(
        expected_revision=2,
        file_id=created.file.id,
        chunk=FileChunk(index=0, contentBase64=base64.b64encode(b"evidence").decode()),
    )
    await service.chunk(actor, scope, task, chunk, context=AuditContext())
    ready = await service.finish(
        actor, scope, task, HumanFileCommand(expected_revision=2, file_id=created.file.id), context=AuditContext()
    )
    assert ready.state == "ready"
    response = await client.post(
        f"{env_url}/human-tasks/{task}/files/download",
        headers=headers,
        json={"expected_revision": 2, "file_id": str(created.file.id), "chunk": {"index": 0}},
    )
    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()["contentBase64"]) == b"evidence"
    assert (await client.get(env_url + "/files/" + str(created.file.id), headers=headers)).status_code == 403


@pytest.mark.parametrize("fence", ["revision", "released", "expired", "cancelled", "grant"])
async def test_upload_checks_current_task_authority_on_every_chunk(claimed, client, headers, env_url, access_db, fence):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.definitions.models import CatalogError

    service, actor, scope, task = claimed
    created = await service.create(
        actor, scope, task, HumanFileCreate(expected_revision=2, file=metadata()), "file", context=AuditContext()
    )
    revision = 2
    if fence == "revision":
        revision = 1
    elif fence == "released":
        response = await client.post(
            f"{env_url}/human-tasks/{task}/release",
            headers={**headers, "Idempotency-Key": "release"},
            json={"expected_revision": 2},
        )
        assert response.status_code == 200
    else:
        async with access_db[1].begin() as session:
            if fence == "grant":
                await session.execute(
                    text("DELETE FROM role_bindings WHERE principal_id=:id AND role='task_participant'"),
                    {"id": actor.id},
                )
            elif fence == "expired":
                await session.execute(
                    text("UPDATE human_tasks SET expires_at=clock_timestamp()-interval '1 second' WHERE id=:id"),
                    {"id": task},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE runs SET state=jsonb_set(state,'{status}',to_jsonb(cast(:status AS text))) "
                        "WHERE id=(SELECT run_id FROM human_tasks WHERE id=:id)"
                    ),
                    {"id": task, "status": "cancelled"},
                )
    with pytest.raises((CatalogError, AccessDenied)):
        await service.chunk(
            actor,
            scope,
            task,
            HumanFileChunk(
                expected_revision=revision,
                file_id=created.file.id,
                chunk=FileChunk(index=0, contentBase64=base64.b64encode(b"evidence").decode()),
            ),
            context=AuditContext(),
        )


async def test_task_download_never_grants_ambient_file_access(claimed, access_db):
    from firefly_weave.definitions.models import CatalogError

    service, actor, scope, task = claimed
    async with service.tasks.runtime.definitions.transaction(scope, None) as tx:
        unrelated = await service.files.create_in(tx, metadata(), actor.id)
        await service.files.put_chunk_in(
            tx, unrelated.file.id, FileChunk(index=0, contentBase64=base64.b64encode(b"evidence").decode())
        )
        await service.files.finish_in(tx, unrelated.file.id)
    with pytest.raises(CatalogError):
        await service.download(
            actor,
            scope,
            task,
            HumanFileRead(expected_revision=2, file_id=unrelated.file.id, chunk={"index": 0}),
            context=AuditContext(),
        )


async def ready_file(claimed, data=b"evidence"):
    service, actor, scope, task = claimed
    created = await service.create(
        actor,
        scope,
        task,
        HumanFileCreate(expected_revision=2, file=metadata(data)),
        "ready-file",
        context=AuditContext(),
    )
    if data:
        await service.chunk(
            actor,
            scope,
            task,
            HumanFileChunk(
                expected_revision=2,
                file_id=created.file.id,
                chunk=FileChunk(index=0, contentBase64=base64.b64encode(data).decode()),
            ),
            context=AuditContext(),
        )
    return await service.finish(
        actor, scope, task, HumanFileCommand(expected_revision=2, file_id=created.file.id), context=AuditContext()
    )


async def test_task_attachment_can_complete_and_remains_readable_from_output(
    claimed, client, headers, env_url, access_db
):
    service, actor, scope, task = claimed
    ready = await ready_file(claimed)
    response = await client.post(
        f"{env_url}/human-tasks/{task}/complete",
        headers={**headers, "Idempotency-Key": "complete-file"},
        json={
            "expected_revision": 2,
            "decision": "approve",
            "data": {"note": ready.file.model_dump(mode="json", by_alias=True)},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    downloaded = await service.download(
        actor,
        scope,
        task,
        HumanFileRead(expected_revision=3, file_id=ready.file.id, chunk={"index": 0}),
        context=AuditContext(),
    )
    assert downloaded.content() == b"evidence"
    async with access_db[1]() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM weave_run_files WHERE file_id=:id"), {"id": ready.file.id})
            == 1
        )


async def test_empty_attachment_metadata_requires_current_task_authority(claimed, client, headers, env_url):
    _, _, _, task = claimed
    ready = await ready_file(claimed, b"")
    response = await client.post(
        f"{env_url}/human-tasks/{task}/files/read",
        headers=headers,
        json={"expected_revision": 2, "file_id": str(ready.file.id)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["file"]["sizeBytes"] == 0
    await client.post(
        f"{env_url}/human-tasks/{task}/release",
        headers={**headers, "Idempotency-Key": "empty-release"},
        json={"expected_revision": 2},
    )
    denied = await client.post(
        f"{env_url}/human-tasks/{task}/files/read",
        headers=headers,
        json={"expected_revision": 3, "file_id": str(ready.file.id)},
    )
    assert denied.status_code == 409


async def test_new_claimant_cannot_download_or_submit_previous_claimants_upload(claimed, access_db, provisioned):
    import json

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.human_tasks import CompleteHumanTask
    from firefly_weave.definitions.models import CatalogError

    service, actor, scope, task = claimed
    ready = await ready_file(claimed)
    other_id = await access_db[2].create_principal(provisioned[0], "human")
    await access_db[2].grant(provisioned[0], other_id, Grant(role="task_participant", scope=scope))
    other = await access_db[2].load_principal(other_id)
    async with access_db[1].begin() as session:
        await session.execute(
            text(
                "UPDATE human_tasks SET claimant_id=:actor,revision=revision+1,"
                "assignment=jsonb_set(assignment,'{principal_ids}',cast(:members AS jsonb)) WHERE id=:id"
            ),
            {"actor": other_id, "id": task, "members": json.dumps([str(actor.id), str(other_id)])},
        )
    with pytest.raises(CatalogError):
        await service.download(
            other,
            scope,
            task,
            HumanFileRead(expected_revision=3, file_id=ready.file.id, chunk={"index": 0}),
            context=AuditContext(),
        )
    with pytest.raises(AccessDenied):
        await service.tasks.command(
            other,
            scope,
            task,
            "complete",
            CompleteHumanTask(
                expected_revision=3,
                decision="approve",
                data={"note": ready.file.model_dump(mode="json", by_alias=True)},
            ),
            "other-complete",
            context=AuditContext(),
        )
