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

"""Operator-provisioned native runners; no self-granted roles or second recovery engine."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import Field

from firefly_weave.access.audit import AuditContext
from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.workers import InstanceRequest
from firefly_weave.sdk.worker import Worker
from firefly_weave.workers.repository import WorkerRepository


class ExecutorConfig(ContractModel):
    scope: Scope
    principal_id: UUID
    release_id: UUID
    task_types: list[str] = Field(min_length=1, max_length=100)
    capacity: int = Field(default=1, ge=1, le=100)


def verify_packaged_build() -> None:
    import firefly_weave

    root = Path(firefly_weave.__file__).parent
    try:
        manifest = json.loads(Path("/opt/weave/build.json").read_bytes())
        actual = {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*.py")
        }
        if actual != manifest["sources"]:
            raise ValueError
    except (OSError, ValueError, KeyError):
        raise RuntimeError("Native execution requires the unchanged built application image") from None


class NativeDispatcher:
    def __init__(
        self,
        service: ConnectorExecutionService,
        entries: tuple[ExecutorConfig, ...],
        image_digest: str | None,
        shutdown_seconds: float,
    ) -> None:
        self.service, self.entries, self.image_digest, self.shutdown_seconds = (
            service,
            entries,
            image_digest,
            shutdown_seconds,
        )
        self.workers: list[Worker] = []
        self.tasks: list[asyncio.Task[None]] = []

    async def open(self) -> None:
        if not self.entries:
            return
        verify_packaged_build()
        if self.image_digest is None:
            raise RuntimeError("Native dispatcher requires its operator-provisioned immutable image identity")
        for entry in self.entries:
            async with self.service.uow.open(entry.scope) as tx:
                actor = await self.service.access.load_principal(entry.principal_id, tx=tx)
                release = await WorkerRepository(tx).release(entry.release_id)
                if release.image_digest != self.image_digest or not release.connector_bindings:
                    raise RuntimeError("Native release does not match configured build")
                self.service.registry.validate_release(release)
                offered = {b.task_reference for b in release.connector_bindings}
                if not set(entry.task_types) <= offered:
                    raise RuntimeError("Native task selection does not match installed bindings")
                for operation in ("task.claim", "task.heartbeat", "task.complete", "credential.lease"):
                    for resource in (str(release.id), *entry.task_types):
                        await self.service.tasks.workers.require(
                            actor, entry.scope, operation, AuditContext(), tx, resource
                        )
                instance = await self.service.tasks.workers.register_instance(
                    actor,
                    entry.scope,
                    InstanceRequest(release_id=entry.release_id, task_types=entry.task_types, capacity=entry.capacity),
                    context=AuditContext(),
                    tx=tx,
                )

            async def execute(lease: Any, scope: Scope = entry.scope, principal: UUID = entry.principal_id) -> Any:
                return await self.service.execute(scope, principal, lease)

            transport = ServiceTransport(self.service, entry.scope, entry.principal_id, instance.id)
            self.workers.append(Worker(transport, {ref: execute for ref in entry.task_types}, entry.capacity))
        self.tasks = [asyncio.create_task(worker.run()) for worker in self.workers]

    def healthy(self) -> bool:
        return all(not task.done() for task in self.tasks)

    async def close(self) -> None:
        for worker in self.workers:
            await worker.stop()
        try:
            async with asyncio.timeout(self.shutdown_seconds):
                await asyncio.gather(*self.tasks)
        except (TimeoutError, Exception):
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
