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

"""Typed HTTP transport with bounded explicit-admission retries, never ambiguous replay."""

import asyncio
import json
from uuid import UUID

from httpx import AsyncClient, Response
from pydantic import TypeAdapter

from firefly_weave.contracts.file_workers import WorkerFileAccess
from firefly_weave.contracts.files import FileChunk, FileCreate, FileUpload
from firefly_weave.contracts.values import JsonValue
from firefly_weave.contracts.workers import (
    CompletionAcknowledgment,
    CredentialLease,
    CredentialRequest,
    LeaseProof,
    TaskError,
    TaskExecutionContext,
    TaskLease,
)


class WorkerTransport:
    def __init__(self, client: AsyncClient, environment_url: str, worker_id: UUID) -> None:
        self.client = client
        self.prefix = environment_url.rstrip("/")
        self.worker_id = worker_id

    async def claim(self, limit: int) -> list[TaskLease]:
        response = await self.client.post(
            self.prefix + "/tasks/claim", json={"worker_id": str(self.worker_id), "limit": limit}
        )
        if self._capacity_rejected(response):
            return []
        response.raise_for_status()

        return [TaskLease.model_validate_json(json.dumps(value)) for value in response.json()]

    async def heartbeat(self, lease: LeaseProof) -> TaskLease:
        response = await self._post_rejected("/tasks/heartbeat", lease.model_dump(mode="json"))
        response.raise_for_status()
        return TaskLease.model_validate_json(response.content)

    async def complete(self, lease: LeaseProof, completion_id: UUID, output: JsonValue) -> CompletionAcknowledgment:
        """Return committed delivery or an explicitly unavailable historical receipt fact."""
        response = await self._post_rejected(
            "/tasks/complete",
            {"lease": lease.model_dump(mode="json"), "completion_id": str(completion_id), "output": output},
            settlement=True,
        )
        response.raise_for_status()
        return TypeAdapter(CompletionAcknowledgment).validate_json(response.content)

    async def fail(self, lease: LeaseProof, error: TaskError) -> CompletionAcknowledgment:
        response = await self._post_rejected(
            "/tasks/fail",
            {"lease": lease.model_dump(mode="json"), "error": error.model_dump(mode="json")},
            settlement=True,
        )
        response.raise_for_status()
        return TypeAdapter(CompletionAcknowledgment).validate_json(response.content)

    @staticmethod
    def _capacity_rejected(response: Response) -> bool:
        if response.status_code != 429:
            return False
        try:
            problem = response.json()
        except ValueError:
            return False
        return isinstance(problem, dict) and problem.get("code") in (
            "WV-REQUEST-CAPACITY",
            "WV-OPERATION-CAPACITY",
        )

    async def _post_rejected(self, path: str, body: object, *, settlement: bool = False) -> Response:
        # Freeze caller-owned values before the first attempt, including the completion identity.
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        response = await self.client.post(self.prefix + path, content=content, headers=headers)
        if not self._capacity_rejected(response):
            return response
        # Only explicit rollback/admission rejection permits replay. Wire errors remain ambiguous.
        # Settlement can outlast a short admission burst; renewal retains its short window.
        # The Worker's lease watchdog can cancel either policy sooner, without rerunning work.
        attempts, seconds = (48, 10) if settlement else (3, 1)
        async with asyncio.timeout(seconds):
            for retry in range(attempts - 1):
                await asyncio.sleep(min(0.05 * 2 ** min(retry, 3), 0.25))
                response = await self.client.post(self.prefix + path, content=content, headers=headers)
                if not self._capacity_rejected(response):
                    return response
        return response

    async def context(self, lease: LeaseProof) -> TaskExecutionContext:
        """Read the activation's pinned connection through current task authority."""
        response = await self.client.post(self.prefix + "/tasks/context", json=lease.model_dump(mode="json"))
        response.raise_for_status()
        return TaskExecutionContext.model_validate_json(response.content)

    async def credentials(self, request: CredentialRequest) -> CredentialLease:
        response = await self.client.post(self.prefix + "/tasks/credentials", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return CredentialLease.model_validate_json(response.content)

    async def create_file(self, lease: LeaseProof, request_id: UUID, file: FileCreate) -> FileUpload:
        response = await self.client.post(
            self.prefix + "/tasks/files/create",
            json={
                "lease": lease.model_dump(mode="json"),
                "request_id": str(request_id),
                "file": file.model_dump(mode="json", by_alias=True),
            },
        )
        response.raise_for_status()
        return FileUpload.model_validate_json(response.content)

    async def put_file_chunk(self, lease: LeaseProof, file_id: UUID, chunk: FileChunk) -> FileUpload:
        response = await self.client.post(
            self.prefix + "/tasks/files/chunk",
            json={
                "lease": lease.model_dump(mode="json"),
                "file_id": str(file_id),
                "chunk": chunk.model_dump(mode="json", by_alias=True),
            },
        )
        response.raise_for_status()
        return FileUpload.model_validate_json(response.content)

    async def finish_file(self, lease: LeaseProof, file_id: UUID) -> FileUpload:
        response = await self.client.post(
            self.prefix + "/tasks/files/finish", json={"lease": lease.model_dump(mode="json"), "file_id": str(file_id)}
        )
        response.raise_for_status()
        return FileUpload.model_validate_json(response.content)

    async def read_file(self, lease: LeaseProof, file_id: UUID) -> FileUpload:
        response = await self.client.post(
            self.prefix + "/tasks/files/read",
            json=WorkerFileAccess(lease=lease, file_id=file_id).model_dump(mode="json", by_alias=True),
        )
        response.raise_for_status()
        return FileUpload.model_validate_json(response.content)

    async def read_file_chunk(self, lease: LeaseProof, file_id: UUID, index: int) -> FileChunk:
        response = await self.client.post(
            self.prefix + "/tasks/files/download",
            json={"lease": lease.model_dump(mode="json"), "file_id": str(file_id), "chunk": {"index": index}},
        )
        response.raise_for_status()
        return FileChunk.model_validate_json(response.content)
