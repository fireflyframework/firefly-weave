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

"""File helpers never expose unverified bytes or allocate a whole file in memory."""

import asyncio
import base64
import hashlib
import io
from uuid import uuid4

import pytest

from firefly_weave.contracts.files import CHUNK_BYTES, MAX_FILE_BYTES, FileChunk, FileReference, FileUpload
from firefly_weave.contracts.workers import LeaseProof
from firefly_weave.sdk.files import download_file, download_task_file, upload_file, upload_task_file


class Files:
    def __init__(self, data=b"", received=()):
        self.data = data
        self.received = list(received)
        self.calls = []
        self.file = None
        self.chunks = {}
        self.hook = None

    async def create_file(self, request, *, idempotency_key):
        self.calls.append(("create", idempotency_key))
        self.file = FileReference(**request.model_dump(by_alias=True), id=uuid4())
        return FileUpload(file=self.file, state="uploading", received_chunks=self.received)

    async def put_file_chunk(self, identifier, chunk):
        self.calls.append(("put", chunk.index))
        self.chunks[chunk.index] = chunk.content()
        self.received.append(chunk.index)
        if self.hook:
            await self.hook()
        return FileUpload(file=self.file, state="uploading", received_chunks=self.received)

    async def finish_file(self, identifier):
        self.calls.append(("finish", identifier))
        return FileUpload(file=self.file, state="ready", received_chunks=self.received)

    async def read_file(self, identifier):
        self.calls.append(("metadata", identifier))
        value = self.file or reference(self.data).model_copy(update={"id": identifier})
        return FileUpload(file=value, state="ready")

    async def read_file_chunk(self, identifier, index):
        self.calls.append(("read", index))
        return FileChunk(
            index=index,
            contentBase64=base64.b64encode(self.data[index * CHUNK_BYTES : (index + 1) * CHUNK_BYTES]).decode(),
        )


def reference(data):
    return FileReference(
        id=uuid4(),
        filename="report.bin",
        contentType="application/octet-stream",
        sizeBytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


async def test_upload_resumes_chunks_checks_metadata_and_restores_stream_position():
    data = b"a" * CHUNK_BYTES + b"tail"
    stream = io.BytesIO(data)
    stream.seek(12)
    client = Files(received=[0])
    result = await upload_file(
        client, stream, filename="report.bin", content_type="application/octet-stream", idempotency_key="stable-request"
    )
    assert result.sha256 == hashlib.sha256(data).hexdigest() and result.size_bytes == len(data)
    assert client.chunks == {1: b"tail"}
    assert stream.tell() == 12 and not stream.closed
    assert client.calls[0] == ("create", "stable-request")


async def test_oversized_stream_fails_before_creating_remote_file():
    class Sparse(io.BytesIO):
        def seek(self, offset, whence=0):
            if whence == 2:
                return MAX_FILE_BYTES + 1
            return super().seek(offset, whence)

        def tell(self):
            return MAX_FILE_BYTES + 1

    client = Files()
    with pytest.raises(ValueError):
        await upload_file(client, Sparse(), filename="x", content_type="application/octet-stream", idempotency_key="x")
    assert not client.calls


async def test_changed_stream_fails_before_finish():
    stream = io.BytesIO(b"a" * CHUNK_BYTES + b"tail")
    client = Files()

    async def change():
        position = stream.tell()
        stream.seek(CHUNK_BYTES)
        stream.write(b"evil")
        stream.seek(position)

    client.hook = change
    with pytest.raises(ValueError):
        await upload_file(client, stream, filename="x", content_type="application/octet-stream", idempotency_key="x")
    assert not any(call[0] == "finish" for call in client.calls)


async def test_download_yields_only_after_digest_verification_and_closes_stream():
    data = b"a" * CHUNK_BYTES + b"tail"
    client = Files(data)
    async with download_file(client, reference(data)) as result:
        assert len(client.calls) == 3
        assert result.read() == data
        assert result.seekable()
    assert result.closed


async def test_corrupt_download_never_yields_bytes():
    client = Files(b"corrupt")
    with pytest.raises(ValueError):
        async with download_file(client, reference(b"correct")):
            pytest.fail("Corrupt data was exposed")


async def test_cancelled_upload_preserves_cancellation_and_stream_ownership():
    client = Files()
    entered = asyncio.Event()
    stream = io.BytesIO(b"hello")

    async def pause():
        entered.set()
        await asyncio.Event().wait()

    client.hook = pause
    task = asyncio.create_task(
        upload_file(client, stream, filename="x", content_type="application/octet-stream", idempotency_key="x")
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.tell() == 0 and not stream.closed
    assert not any(call[0] == "finish" for call in client.calls)


async def test_worker_helpers_keep_lease_and_stable_request_id_on_every_operation():
    lease = LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="proof")
    request_id = uuid4()
    backend = Files(b"hello")
    proofs = []

    class Worker:
        async def create_file(self, proof, request, file):
            proofs.append(proof)
            assert request == request_id
            return await backend.create_file(file, idempotency_key=str(request))

        async def put_file_chunk(self, proof, identifier, chunk):
            proofs.append(proof)
            return await backend.put_file_chunk(identifier, chunk)

        async def finish_file(self, proof, identifier):
            proofs.append(proof)
            return await backend.finish_file(identifier)

        async def read_file(self, proof, identifier):
            proofs.append(proof)
            return await backend.read_file(identifier)

        async def read_file_chunk(self, proof, identifier, index):
            proofs.append(proof)
            return await backend.read_file_chunk(identifier, index)

    transport = Worker()
    result = await upload_task_file(
        transport, lease, io.BytesIO(b"hello"), request_id=request_id, filename="hello.txt", content_type="text/plain"
    )
    async with download_task_file(transport, lease, result) as stream:
        assert stream.read() == b"hello"
    assert len(proofs) == 5 and all(item == lease for item in proofs)


@pytest.mark.parametrize(
    "field,value", [("filename", "other.txt"), ("content_type", "text/plain"), ("size_bytes", 1), ("sha256", "0" * 64)]
)
async def test_upload_rejects_changed_server_metadata_before_sending_content(field, value):
    class Mismatch(Files):
        async def create_file(self, request, *, idempotency_key):
            result = await super().create_file(request, idempotency_key=idempotency_key)
            return result.model_copy(update={"file": result.file.model_copy(update={field: value})})

    client = Mismatch()
    with pytest.raises(ValueError):
        await upload_file(
            client,
            io.BytesIO(b"hello"),
            filename="report.bin",
            content_type="application/octet-stream",
            idempotency_key="stable",
        )
    assert len(client.calls) == 1


async def test_cancelled_download_closes_private_spool_before_any_yield(monkeypatch):
    import tempfile

    created = []
    factory = tempfile.SpooledTemporaryFile
    entered = asyncio.Event()

    def capture(*args, **kwargs):
        result = factory(*args, **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr("firefly_weave.sdk.files.tempfile.SpooledTemporaryFile", capture)

    class Slow(Files):
        async def read_file_chunk(self, identifier, index):
            entered.set()
            await asyncio.Event().wait()

    async def download():
        async with download_file(Slow(b"hello"), reference(b"hello")):
            pytest.fail("Incomplete file exposed")

    task = asyncio.create_task(download())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(created) == 1 and created[0].closed


async def test_human_file_sdk_uses_task_scope_revision_and_canonical_metadata():
    import json

    import httpx

    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.files import FileCreate
    from firefly_weave.contracts.human_files import HumanFileChunk, HumanFileCommand, HumanFileCreate, HumanFileRead
    from firefly_weave.sdk.client import WeaveClient

    task = uuid4()
    file = reference(b"hello")
    calls = []
    chunk = FileChunk(index=0, contentBase64=base64.b64encode(b"hello").decode())

    def respond(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["expected_revision"] == 2
        assert f"/human-tasks/{task}/files/" in str(request.url)
        if request.url.path.endswith("/create"):
            assert request.headers["idempotency-key"] == "stable-human-file"
        if request.url.path.endswith("/download"):
            return httpx.Response(200, json=chunk.model_dump(mode="json", by_alias=True))
        return httpx.Response(
            200, json=FileUpload(file=file, state="ready", received_chunks=[0]).model_dump(mode="json", by_alias=True)
        )

    async with WeaveClient(
        "https://platform.example",
        lambda: "token",
        Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        transport=httpx.MockTransport(respond),
    ) as client:
        created = await client.create_human_file(
            task,
            HumanFileCreate(
                expected_revision=2, file=FileCreate(**file.model_dump(by_alias=True, exclude={"kind", "id"}))
            ),
            idempotency_key="stable-human-file",
        )
        assert created.file == file
        await client.put_human_file_chunk(task, HumanFileChunk(expected_revision=2, file_id=file.id, chunk=chunk))
        await client.finish_human_file(task, HumanFileCommand(expected_revision=2, file_id=file.id))
        assert (await client.read_human_file(task, HumanFileCommand(expected_revision=2, file_id=file.id))).file == file
        assert (
            await client.read_human_file_chunk(
                task, HumanFileRead(expected_revision=2, file_id=file.id, chunk={"index": 0})
            )
        ).content() == b"hello"
    assert len(calls) == 5


async def test_empty_download_still_requires_authorized_ready_metadata():
    expected = reference(b"")
    calls = []

    class Denied(Files):
        async def read_file(self, identifier):
            calls.append(identifier)
            raise PermissionError("access denied")

    with pytest.raises(PermissionError):
        async with download_file(Denied(), expected):
            pytest.fail("No metadata authority was checked")
    assert calls == [expected.id]
