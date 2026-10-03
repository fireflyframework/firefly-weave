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

"""Bounded binary stream helpers; downloads become visible only after full verification."""

import asyncio
import base64
import hashlib
import io
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import BinaryIO, Protocol, cast
from uuid import UUID

from firefly_weave.contracts.files import CHUNK_BYTES, MAX_FILE_BYTES, FileChunk, FileCreate, FileReference, FileUpload
from firefly_weave.contracts.workers import LeaseProof


class FileClient(Protocol):
    async def read_file(self, identifier: UUID) -> FileUpload: ...

    async def create_file(self, request: FileCreate, *, idempotency_key: str) -> FileUpload: ...
    async def put_file_chunk(self, identifier: UUID, chunk: FileChunk) -> FileUpload: ...
    async def finish_file(self, identifier: UUID) -> FileUpload: ...
    async def read_file_chunk(self, identifier: UUID, index: int) -> FileChunk: ...


class TaskFiles(Protocol):
    async def read_file(self, lease: LeaseProof, file_id: UUID) -> FileUpload: ...

    async def create_file(self, lease: LeaseProof, request_id: UUID, file: FileCreate) -> FileUpload: ...
    async def put_file_chunk(self, lease: LeaseProof, file_id: UUID, chunk: FileChunk) -> FileUpload: ...
    async def finish_file(self, lease: LeaseProof, file_id: UUID) -> FileUpload: ...
    async def read_file_chunk(self, lease: LeaseProof, file_id: UUID, index: int) -> FileChunk: ...


def _read(stream: BinaryIO, length: int) -> bytes:
    value = bytearray()
    while len(value) < length:
        part = stream.read(length - len(value))
        if not isinstance(part, bytes) or len(part) > length - len(value):
            raise ValueError("Use a bounded binary stream")
        if not part:
            raise ValueError("File size changed during transfer")
        value.extend(part)
    return bytes(value)


def _validate(upload: FileUpload, expected: FileCreate, identifier: UUID | None = None) -> None:
    if upload.file.model_dump(exclude={"id", "kind"}) != expected.model_dump() or (
        identifier is not None and upload.file.id != identifier
    ):
        raise ValueError("File response metadata does not match the transfer")
    count = (expected.size_bytes + CHUNK_BYTES - 1) // CHUNK_BYTES
    if (
        upload.state == "deleted"
        or len(set(upload.received_chunks)) != len(upload.received_chunks)
        or any(type(index) is not int or not 0 <= index < count for index in upload.received_chunks)
    ):
        raise ValueError("Invalid file transfer state")


async def _upload(
    stream: BinaryIO,
    filename: str,
    content_type: str,
    create: Callable[[FileCreate], Awaitable[FileUpload]],
    put: Callable[[UUID, FileChunk], Awaitable[FileUpload]],
    finish: Callable[[UUID], Awaitable[FileUpload]],
) -> FileReference:
    if not stream.seekable() or not stream.readable():
        raise ValueError("A readable seekable binary stream is required")
    original = stream.tell()
    try:
        stream.seek(0, io.SEEK_END)
        size = stream.tell()
        if not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("File exceeds the upload limit")
        expected = FileCreate(filename=filename, contentType=content_type, sizeBytes=size, sha256="0" * 64)
        digest = hashlib.sha256()
        stream.seek(0)
        for offset in range(0, size, CHUNK_BYTES):
            digest.update(_read(stream, min(CHUNK_BYTES, size - offset)))
            await asyncio.sleep(0)
        if stream.read(1) != b"":
            raise ValueError("File size changed during transfer")
        expected = expected.model_copy(update={"sha256": digest.hexdigest()})
        uploaded = await create(expected)
        _validate(uploaded, expected)
        if uploaded.state == "ready":
            return uploaded.file
        identifier = uploaded.file.id
        received = set(uploaded.received_chunks)
        stream.seek(0)
        transmitted = hashlib.sha256()
        for index, offset in enumerate(range(0, size, CHUNK_BYTES)):
            data = _read(stream, min(CHUNK_BYTES, size - offset))
            transmitted.update(data)
            if index not in received:
                response = await put(
                    identifier, FileChunk(index=index, contentBase64=base64.b64encode(data).decode("ascii"))
                )
                _validate(response, expected, identifier)
                if response.state != "uploading" or index not in response.received_chunks:
                    raise ValueError("Invalid file chunk acknowledgement")
            await asyncio.sleep(0)
        if stream.read(1) != b"" or transmitted.hexdigest() != expected.sha256:
            raise ValueError("File content changed during transfer")
        result = await finish(identifier)
        _validate(result, expected, identifier)
        if result.state != "ready":
            raise ValueError("File upload is not ready")
        return result.file
    finally:
        stream.seek(original)


async def upload_file(
    client: FileClient, stream: BinaryIO, *, filename: str, content_type: str, idempotency_key: str
) -> FileReference:
    async def create(request: FileCreate) -> FileUpload:
        return await client.create_file(request, idempotency_key=idempotency_key)

    return await _upload(stream, filename, content_type, create, client.put_file_chunk, client.finish_file)


async def upload_task_file(
    transport: TaskFiles, lease: LeaseProof, stream: BinaryIO, *, request_id: UUID, filename: str, content_type: str
) -> FileReference:
    async def create(request: FileCreate) -> FileUpload:
        return await transport.create_file(lease, request_id, request)

    async def put(identifier: UUID, chunk: FileChunk) -> FileUpload:
        return await transport.put_file_chunk(lease, identifier, chunk)

    async def finish(identifier: UUID) -> FileUpload:
        return await transport.finish_file(lease, identifier)

    return await _upload(stream, filename, content_type, create, put, finish)


@asynccontextmanager
async def _download(
    reference: FileReference,
    read: Callable[[UUID, int], Awaitable[FileChunk]],
    metadata: Callable[[UUID], Awaitable[FileUpload]],
) -> AsyncIterator[BinaryIO]:
    # The temporary object is never yielded, named, or moved until all metadata agrees.
    if not 0 <= reference.size_bytes <= MAX_FILE_BYTES:
        raise ValueError("File exceeds the download limit")
    current = await metadata(reference.id)
    if current.state != "ready" or current.file != reference:
        raise ValueError("File is not ready or its metadata has changed")
    with tempfile.SpooledTemporaryFile(max_size=CHUNK_BYTES, mode="w+b") as temporary:
        stream = cast(BinaryIO, temporary)
        digest = hashlib.sha256()
        for index, offset in enumerate(range(0, reference.size_bytes, CHUNK_BYTES)):
            chunk = await read(reference.id, index)
            data = chunk.content()
            if chunk.index != index or len(data) != min(CHUNK_BYTES, reference.size_bytes - offset):
                raise ValueError("File chunk does not match the declared size and position")
            digest.update(data)
            stream.write(data)
            await asyncio.sleep(0)
        if digest.hexdigest() != reference.sha256:
            raise ValueError("File checksum does not match")
        stream.seek(0)
        yield stream


@asynccontextmanager
async def download_file(client: FileClient, reference: FileReference) -> AsyncIterator[BinaryIO]:
    async with _download(reference, client.read_file_chunk, client.read_file) as stream:
        yield stream


@asynccontextmanager
async def download_task_file(
    transport: TaskFiles, lease: LeaseProof, reference: FileReference
) -> AsyncIterator[BinaryIO]:
    async def read(identifier: UUID, index: int) -> FileChunk:
        return await transport.read_file_chunk(lease, identifier, index)

    async def metadata(identifier: UUID) -> FileUpload:
        return await transport.read_file(lease, identifier)

    async with _download(reference, read, metadata) as stream:
        yield stream
