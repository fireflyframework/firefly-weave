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

"""Lease-bound transfers publish only complete verified files and close on cancellation."""

import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.files import FileReference, FileUpload
from firefly_weave.contracts.workers import CredentialLease, LeaseProof, TaskLease

from weave_files_worker.handler import FileTaskHandler
from weave_files_worker.policy import WorkerPolicy


def lease(operation="download", value=None):
    now = datetime.now(UTC)
    return TaskLease(
        proof=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="proof"),
        operation_key="files",
        deadline=now + timedelta(seconds=30),
        expires_at=now + timedelta(seconds=30),
        capability=f"weave-sftp.{operation}@1.0.0",
        worker_release_id=uuid4(),
        input=value or {"path": "invoice.txt"},
    )


class Transport:
    def __init__(self):
        self.content = b""
        self.access = []

    async def context(self, proof):
        return SimpleNamespace(
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            connection=SimpleNamespace(
                revision_id=uuid4(),
                connector="weave-sftp@1.0.0",
                config={
                    "host": "files.example",
                    "port": 22,
                    "username": "user",
                    "rootPath": "/",
                    "serverRootIsolated": True,
                    "hostKey": "pinned",
                    "operations": ["download", "write"],
                },
                secret_slots=["password"],
                allowed_destinations=("sftp://files.example:22",),
            ),
        )

    async def credentials(self, request):
        self.access.append(request)
        return CredentialLease(value="private-password", expires_at=datetime.now(UTC) + timedelta(seconds=30))

    async def create_file(self, proof, request_id, file):
        self.file = FileReference(id=uuid4(), **file.model_dump(by_alias=True))
        return FileUpload(file=self.file, state="uploading")

    async def put_file_chunk(self, proof, file_id, chunk):
        self.content += chunk.content()
        return FileUpload(file=self.file, state="uploading", received_chunks=[chunk.index])

    async def finish_file(self, proof, file_id):
        return FileUpload(file=self.file, state="ready")


class Provider:
    async def read(self, location):
        return {"id": location, "name": "invoice.txt", "isFolder": False, "sizeBytes": 5, "contentType": "text/plain"}

    async def download(self, location):
        yield b"hello"


@asynccontextmanager
async def provider(*args):
    yield Provider()


async def test_download_streams_to_a_scoped_file_reference():
    transport = Transport()
    result = await FileTaskHandler(
        transport, WorkerPolicy(frozenset({"sftp://files.example:22"})), provider_factory=provider
    )(lease())
    assert result["kind"] == "weave/file"
    assert result["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert result["sizeBytes"] == 5
    assert transport.content == b"hello"
    assert "private-password" not in str(result)
    assert len(transport.access) == 1


async def test_denied_operation_never_leases_credentials():
    transport = Transport()
    with pytest.raises(ConnectorFailure, match="FILE_OPERATION"):
        await FileTaskHandler(
            transport, WorkerPolicy(frozenset({"sftp://files.example:22"})), provider_factory=provider
        )(lease("delete"))
    assert transport.access == []


async def test_cancellation_closes_the_provider_without_publishing_a_partial_file():
    import asyncio

    entered = asyncio.Event()
    closed = asyncio.Event()

    class WaitingProvider(Provider):
        async def download(self, location):
            entered.set()
            await asyncio.Event().wait()
            yield b"never"

    @asynccontextmanager
    async def factory(*args):
        try:
            yield WaitingProvider()
        finally:
            closed.set()

    transport = Transport()
    task = asyncio.create_task(
        FileTaskHandler(transport, WorkerPolicy(frozenset({"sftp://files.example:22"})), provider_factory=factory)(
            lease()
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert not hasattr(transport, "file")


async def test_provider_size_change_never_creates_a_weave_file():
    class ChangedProvider(Provider):
        async def download(self, location):
            yield b"changed"

    @asynccontextmanager
    async def factory(*args):
        yield ChangedProvider()

    transport = Transport()
    with pytest.raises(ConnectorFailure, match="FILE_CHANGED"):
        await FileTaskHandler(
            transport, WorkerPolicy(frozenset({"sftp://files.example:22"})), provider_factory=factory
        )(lease())
    assert not hasattr(transport, "file")
