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

"""One bounded provider operation, with binary data transferred only through live task leases."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from tempfile import TemporaryFile
from typing import BinaryIO, Protocol, cast
from uuid import UUID, uuid5

from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.file_connectors import CLOUD_ORIGINS, NAMES, OPERATIONS, input_schema, output_schema
from firefly_weave.contracts.files import CHUNK_BYTES, MAX_FILE_BYTES, FileChunk, FileCreate, FileReference, FileUpload
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import (
    CredentialLease,
    CredentialRequest,
    LeaseProof,
    TaskExecutionContext,
    TaskLease,
)
from firefly_weave.sdk.files import download_task_file, upload_task_file

from weave_files_worker.policy import WorkerPolicy


class Transport(Protocol):
    async def context(self, lease: LeaseProof) -> TaskExecutionContext: ...
    async def credentials(self, request: CredentialRequest) -> CredentialLease: ...
    async def create_file(self, lease: LeaseProof, request_id: UUID, file: FileCreate) -> FileUpload: ...
    async def put_file_chunk(self, lease: LeaseProof, file_id: UUID, chunk: FileChunk) -> FileUpload: ...
    async def finish_file(self, lease: LeaseProof, file_id: UUID) -> FileUpload: ...
    async def read_file(self, lease: LeaseProof, file_id: UUID) -> FileUpload: ...
    async def read_file_chunk(self, lease: LeaseProof, file_id: UUID, index: int) -> FileChunk: ...


class Provider(Protocol):
    async def list(self, location: str, limit: int, cursor: str | None) -> JsonObject: ...
    async def read(self, location: str) -> JsonObject: ...
    def download(self, location: str) -> AsyncIterator[bytes]: ...
    async def write(
        self, destination: str, name: str, source: AsyncIterator[bytes], file: FileReference
    ) -> JsonObject: ...
    async def move(self, location: str, destination: str, name: str) -> JsonObject: ...
    async def delete(self, location: str) -> JsonObject: ...


ProviderFactory = Callable[[str, JsonObject, str, WorkerPolicy], AbstractAsyncContextManager[Provider]]


async def chunks(file: BinaryIO) -> AsyncIterator[bytes]:
    file.seek(0)
    while value := file.read(CHUNK_BYTES):
        yield value


class FileTaskHandler:
    def __init__(self, transport: Transport, policy: WorkerPolicy, *, provider_factory: ProviderFactory | None = None):
        self.transport, self.policy = transport, policy
        if provider_factory is None:
            from weave_files_worker.providers import open_provider

            provider_factory = open_provider
        self.provider_factory = provider_factory

    async def __call__(self, lease: TaskLease) -> JsonValue:
        task, version = lease.capability.rsplit("@", 1)
        name, operation = task.rsplit(".", 1)
        if name not in NAMES or operation not in OPERATIONS or version != "1.0.0":
            raise ConnectorFailure("FILE_CAPABILITY", "not_started")
        measure_value(lease.input)
        if not isinstance(lease.input, dict) or validate_payload(input_schema(name, operation), lease.input, {}):
            raise ConnectorFailure("FILE_INPUT", "not_started")
        context = await self.transport.context(lease.proof)
        connection = context.connection
        now = datetime.now(UTC)
        remaining = min(300.0, (lease.deadline - now).total_seconds(), (context.expires_at - now).total_seconds())
        if connection is None or remaining <= 0 or connection.connector != name + "@1.0.0":
            raise ConnectorFailure("FILE_CONNECTION", "not_started")
        operations = connection.config.get("operations")
        if not isinstance(operations, list) or operation not in operations:
            raise ConnectorFailure("FILE_OPERATION", "not_started")
        config = connection.config
        origin = CLOUD_ORIGINS.get(name) or f"{name.removeprefix('weave-')}://{config.get('host')}:{config.get('port')}"
        self.policy.destination(origin)
        if origin not in connection.allowed_destinations:
            raise ConnectorFailure("FILE_POLICY", "not_started")
        slot = "accessToken" if name in CLOUD_ORIGINS else "password"
        if slot not in connection.secret_slots:
            raise ConnectorFailure("FILE_CONNECTION", "not_started")
        credential = await self.transport.credentials(
            CredentialRequest(lease=lease.proof, connection_revision_id=connection.revision_id, slot=slot)
        )
        remaining = min(remaining, (credential.expires_at - datetime.now(UTC)).total_seconds())
        if remaining <= 0:
            raise ConnectorFailure("FILE_CONNECTION", "not_started")
        try:
            async with asyncio.timeout(remaining):
                async with self.provider_factory(name, config, credential.value, self.policy) as provider:
                    result = await self.execute(provider, lease, operation, name in CLOUD_ORIGINS)
                    measure_value(result)
                    if validate_payload(output_schema(operation), result, {}):
                        raise ConnectorFailure("FILE_OUTPUT", "unknown")
                    return result
        except ConnectorFailure:
            raise
        except TimeoutError:
            raise ConnectorFailure("FILE_TIMEOUT", "unknown") from None
        except Exception:
            raise ConnectorFailure("FILE_PROVIDER", "unknown") from None

    async def execute(self, provider: Provider, lease: TaskLease, operation: str, cloud: bool) -> JsonValue:
        value = cast(JsonObject, lease.input)
        location = str(value.get("itemId" if cloud else "path", ""))
        if operation == "list":
            return await provider.list(
                location, int(cast(int, value.get("limit", 100))), cast(str | None, value.get("cursor"))
            )
        if operation == "read":
            return await provider.read(location)
        if operation == "delete":
            return await provider.delete(location)
        if operation == "move":
            return await provider.move(location, str(value["destination"]), str(value.get("name", "")))
        if operation == "write":
            reference = FileReference.model_validate(value["file"])
            async with download_task_file(self.transport, lease.proof, reference) as temporary:
                return await provider.write(
                    str(value["destination"]), str(value.get("name", reference.filename)), chunks(temporary), reference
                )
        metadata = await provider.read(location)
        if metadata.get("isFolder") or int(cast(int, metadata.get("sizeBytes", 0))) > MAX_FILE_BYTES:
            raise ConnectorFailure("FILE_LIMIT", "not_started")
        with TemporaryFile() as temporary:
            size = 0
            async for chunk in provider.download(location):
                size += len(chunk)
                if size > MAX_FILE_BYTES or len(chunk) > CHUNK_BYTES:
                    raise ConnectorFailure("FILE_LIMIT", "failed")
                temporary.write(chunk)
            if "sizeBytes" in metadata and size != metadata["sizeBytes"]:
                raise ConnectorFailure("FILE_CHANGED", "failed")
            reference = await upload_task_file(
                self.transport,
                lease.proof,
                temporary,
                request_id=uuid5(lease.proof.task_id, lease.operation_key),
                filename=str(metadata["name"]),
                content_type=str(metadata.get("contentType", "application/octet-stream")),
            )
            return cast(JsonObject, reference.model_dump(mode="json", by_alias=True))
