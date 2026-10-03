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

"""Real file persistence enforces scoped content, integrity and resumable chunks."""

import base64
import hashlib
from uuid import uuid4

import pytest
import test_definitions
import test_leases

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Grant
from firefly_weave.contracts.files import CHUNK_BYTES, FileChunk, FileCreate
from firefly_weave.definitions.models import CatalogError
from firefly_weave.definitions.service import DefinitionService
from firefly_weave.files.service import FileService

pytestmark = pytest.mark.integration
author = test_definitions.author
publication_request = test_definitions.publication_request
connections = test_leases.connections
credential_task = test_leases.credential_task


@pytest.fixture
async def files(services, access_db, provisioned):
    actor, scopes = provisioned
    scope = scopes[0]
    await access_db[2].grant(actor, actor.id, Grant(role="file_manager", scope=scope))
    actor = await access_db[2].load_principal(actor.id)
    service = FileService(services(access_db[0]).resolve(DefinitionService))
    return service, actor, scope, scopes[1]


def metadata(content=b"invoice"):
    return FileCreate(
        filename="invoice.pdf",
        contentType="application/pdf",
        sizeBytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def chunk(data, index=0):
    return FileChunk(index=index, contentBase64=base64.b64encode(data).decode())


async def test_upload_is_resumable_immutable_and_checksum_verified(files):
    service, actor, scope, other = files
    authority = dict(actor=actor, scope=scope, context=AuditContext())
    data = b"x" * CHUNK_BYTES + b"final"
    created = await service.create(request=metadata(data), idempotency_key="create", **authority)
    replay = await service.create(request=metadata(data), idempotency_key="create", **authority)
    assert created == replay
    with pytest.raises(CatalogError):
        await service.finish(identifier=created.file.id, **authority)
    await service.put_chunk(identifier=created.file.id, chunk=chunk(data[CHUNK_BYTES:], 1), **authority)
    await service.put_chunk(identifier=created.file.id, chunk=chunk(data[:CHUNK_BYTES]), **authority)
    await service.put_chunk(identifier=created.file.id, chunk=chunk(data[:CHUNK_BYTES]), **authority)
    with pytest.raises(CatalogError):
        await service.put_chunk(identifier=created.file.id, chunk=chunk(b"y" * CHUNK_BYTES), **authority)
    ready = await service.finish(identifier=created.file.id, **authority)
    assert ready.state == "ready"
    assert (await service.finish(identifier=created.file.id, **authority)) == ready
    assert (await service.read_chunk(identifier=ready.file.id, index=0, **authority)).content() == data[:CHUNK_BYTES]
    with pytest.raises(CatalogError):
        await service.put_chunk(identifier=created.file.id, chunk=chunk(data[:CHUNK_BYTES]), **authority)


async def test_checksum_mismatch_cannot_publish_content(files):
    service, actor, scope, _ = files
    authority = dict(actor=actor, scope=scope, context=AuditContext())
    created = await service.create(request=metadata(), idempotency_key="wrong", **authority)
    await service.put_chunk(identifier=created.file.id, chunk=chunk(b"not-real"[:7]), **authority)
    with pytest.raises(CatalogError, match="integrity"):
        await service.finish(identifier=created.file.id, **authority)
    with pytest.raises(CatalogError):
        await service.read_chunk(identifier=created.file.id, index=0, **authority)


async def test_empty_file_and_content_access_require_scope_and_grants(files, access_db, provisioned):
    from firefly_weave.access.authorization import AccessDenied

    service, actor, scope, other = files
    authority = dict(actor=actor, scope=scope, context=AuditContext())
    created = await service.create(request=metadata(b""), idempotency_key="empty", **authority)
    assert (await service.finish(identifier=created.file.id, **authority)).state == "ready"
    with pytest.raises(AccessDenied):
        await service.read(actor, other, created.file.id, context=AuditContext())
    await access_db[2].grant(provisioned[0], actor.id, Grant(role="file_reader", scope=other))
    actor = await access_db[2].load_principal(actor.id)
    with pytest.raises(CatalogError):
        await service.read(actor, other, created.file.id, context=AuditContext())


async def test_file_listing_is_bounded_and_deleted_upload_releases_quota(files):
    service, actor, scope, _ = files
    authority = dict(actor=actor, scope=scope, context=AuditContext())
    created = [await service.create(request=metadata(), idempotency_key=str(i), **authority) for i in range(3)]
    page = await service.list(limit=2, **authority)
    assert len(page["items"]) == 2 and page["next_cursor"]
    from uuid import UUID

    next_page = await service.list(limit=2, cursor=UUID(page["next_cursor"]), **authority)
    assert len(next_page["items"]) == 1 and next_page["next_cursor"] is None
    await service.delete(identifier=created[0].file.id, **authority)
    assert len((await service.list(**authority))["items"]) == 2
    with pytest.raises(CatalogError):
        await service.read(identifier=created[0].file.id, **authority)


async def test_worker_transfers_are_fenced_and_cannot_read_unrelated_files(credential_task, access_db):
    from sqlalchemy import text

    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.contracts.file_workers import WorkerFileAccess, WorkerFileChunk, WorkerFileCreate, WorkerFileRead
    from firefly_weave.contracts.files import FileChunkRead
    from firefly_weave.files.worker_service import WorkerFileService

    tasks, _, authority, _, credential, _ = credential_task
    service = FileService(tasks.runtime.definitions)
    worker_files = WorkerFileService(tasks, service)
    request = WorkerFileCreate(lease=credential.lease, request_id=uuid4(), file=metadata())
    created = await worker_files.create(request=request, **authority)
    assert await worker_files.create(request=request, **authority) == created
    assert (
        await worker_files.read(request=WorkerFileAccess(lease=credential.lease, file_id=created.file.id), **authority)
        == created
    )
    await worker_files.put_chunk(
        request=WorkerFileChunk(lease=credential.lease, file_id=created.file.id, chunk=chunk(b"invoice")), **authority
    )
    await worker_files.finish(request=WorkerFileAccess(lease=credential.lease, file_id=created.file.id), **authority)
    read = WorkerFileRead(lease=credential.lease, file_id=created.file.id, chunk=FileChunkRead(index=0))
    assert (await worker_files.read_chunk(request=read, **authority)).content() == b"invoice"
    async with service.definitions.transaction(authority["scope"], None) as tx:
        other = await service.create_in(tx, metadata(), authority["actor"].id)
        await service.put_chunk_in(tx, other.file.id, chunk(b"invoice"))
        await service.finish_in(tx, other.file.id)
    with pytest.raises(AccessDenied):
        await worker_files.read_chunk(request=read.model_copy(update={"file_id": other.file.id}), **authority)
    async with access_db[1].begin() as tx:
        await tx.execute(text("UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second'"))
    with pytest.raises(CatalogError):
        await worker_files.read_chunk(request=read, **authority)
    with pytest.raises(CatalogError):
        await worker_files.read(request=WorkerFileAccess(lease=credential.lease, file_id=created.file.id), **authority)


async def test_run_file_authority_rejects_forged_references_and_pins_accepted_files(credential_task):
    from firefly_weave.access.authorization import AccessDenied
    from firefly_weave.files.authority import admit_files, require_run_files, require_task_output_files

    tasks, _, authority, _, credential, _ = credential_task
    service = FileService(tasks.runtime.definitions)
    async with service.definitions.transaction(authority["scope"], None) as tx:
        verified = await tasks.verify_file_task(tx, credential.lease, **authority)
        unrelated = await service.create_in(tx, metadata(), authority["actor"].id)
        await service.put_chunk_in(tx, unrelated.file.id, chunk(b"invoice"))
        await service.finish_in(tx, unrelated.file.id)
        value = unrelated.file.model_dump(mode="json", by_alias=True)
        with pytest.raises(AccessDenied):
            await admit_files(tx, verified.run["id"], value, authority["actor"], context=authority["context"])
        with pytest.raises(CatalogError):
            await require_run_files(tx, verified.run["id"], value)
        with pytest.raises(CatalogError):
            await require_task_output_files(verified, value)
        own = await service.create_in(
            tx, metadata(), authority["actor"].id, task_id=verified.task["id"], run_id=verified.run["id"]
        )
        with pytest.raises(CatalogError):
            await require_run_files(tx, verified.run["id"], own.file.model_dump(mode="json", by_alias=True))
        with pytest.raises(CatalogError):
            await require_task_output_files(verified, own.file.model_dump(mode="json", by_alias=True))
        await service.put_chunk_in(tx, own.file.id, chunk(b"invoice"))
        await service.finish_in(tx, own.file.id)
        await require_run_files(tx, verified.run["id"], own.file.model_dump(mode="json", by_alias=True))
        await require_task_output_files(verified, own.file.model_dump(mode="json", by_alias=True))
        with pytest.raises(CatalogError):
            await require_run_files(
                tx, verified.run["id"], {**own.file.model_dump(mode="json", by_alias=True), "filename": "forged.pdf"}
            )


async def test_file_http_transfer_and_run_admission_are_authorized_and_retained(
    author, access_db, provisioned, headers, other_headers, env_url, project_url, publication_request
):
    client, actor, scope = author
    admin = provisioned[0]
    root = "/api/v1" + env_url
    create = metadata().model_dump(mode="json", by_alias=True)
    assert (
        await client.post(root + "/files", json=create, headers={**headers, "Idempotency-Key": "file"})
    ).status_code == 403
    binding = await access_db[2].grant(admin, actor.id, Grant(role="file_manager", scope=scope))
    response = await client.post(root + "/files", json=create, headers={**headers, "Idempotency-Key": "file"})
    assert response.status_code == 201, response.text
    reference = response.json()["file"]
    path = root + "/files/" + reference["id"]
    assert (await client.get(path, headers=other_headers)).status_code == 403
    response = await client.post(
        path + "/chunks", headers=headers, json=chunk(b"invoice").model_dump(mode="json", by_alias=True)
    )
    assert response.status_code == 200, response.text
    response = await client.post(path + "/finish", headers=headers, json={})
    assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    response = await client.post(path + "/download", headers=headers, json={"index": 0})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store", response.text
    assert base64.b64decode(response.json()["contentBase64"]) == b"invoice"
    await access_db[2].grant(admin, actor.id, Grant(role="operator", scope=scope))
    published = await client.post(
        project_url + "/workflows", headers={**headers, "Idempotency-Key": "fileflow"}, json=publication_request
    )
    assert published.status_code == 201, published.text
    activation = await client.post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "fileactivation"},
        json={
            "version_id": published.json()["id"],
            "artifact_digest": published.json()["digest"],
            "scope": scope.model_dump(mode="json"),
        },
    )
    assert activation.status_code == 201, activation.text
    await access_db[2].revoke_grant(admin, scope, binding)
    request = {"activation_id": activation.json()["id"], "input": {"invoice": reference}}
    response = await client.post(root + "/runs", headers={**headers, "Idempotency-Key": "filerun"}, json=request)
    assert response.status_code == 403, response.text
    await access_db[2].grant(admin, actor.id, Grant(role="file_reader", scope=scope))
    response = await client.post(root + "/runs", headers={**headers, "Idempotency-Key": "filerun"}, json=request)
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    await access_db[2].grant(admin, actor.id, Grant(role="file_manager", scope=scope))
    response = await client.delete(path, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "WV-FILE-IN-USE"

    await access_db[2].grant(admin, actor.id, Grant(role="execution_manager", scope=scope))
    archived = await client.post(
        root + "/runs/" + run_id + "/archive",
        headers={**headers, "Idempotency-Key": "file-archive"},
        json={"expected_revision": 0, "reason": "Test retained files"},
    )
    assert archived.status_code == 200, archived.text
    assert (await client.delete(path, headers=headers)).status_code == 409
    purged = await client.post(
        root + "/runs/" + run_id + "/purge",
        headers={**headers, "Idempotency-Key": "file-purge"},
        json={"expected_revision": 1, "reason": "Test retention expiry", "confirm_run_id": run_id},
    )
    assert purged.status_code == 200, purged.text
    assert (await client.delete(path, headers=headers)).status_code == 200
    assert (await client.get(path, headers=headers)).status_code == 404
