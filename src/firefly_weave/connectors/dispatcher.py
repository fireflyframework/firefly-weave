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
import logging
import sys
import time
from contextvars import Context
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from firefly_weave.access.audit import AuditContext
from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.workers import InstanceRequest
from firefly_weave.sdk.worker import Worker
from firefly_weave.workers.repository import WorkerRepository

LOCAL_BUILD_FORMAT = "weave/local-build-v1"
# A local development build runs only the first-party declarative HTTP executor; tenant data
# selects an operation inside its validated profile, never code, so no package is trusted here.
LOCAL_DEVELOPMENT_ADAPTERS = frozenset({"weave-http-v2"})


class ExecutorConfig(ContractModel):
    scope: Scope
    principal_id: UUID
    release_id: UUID
    task_types: list[str] = Field(min_length=1, max_length=100)
    capacity: int = Field(default=1, ge=1, le=100)
    # "local-development" is accepted only by loopback development settings (see Settings).
    build: Literal["image", "local-development"] = "image"


def local_build_identity(root: Path | None = None) -> str:
    """Return the content identity of an installed package tree for local development releases.

    Every installed file except bytecode caches is covered, so a changed, added, or removed
    file yields a different identity. Symbolic links are refused rather than followed.
    """
    import firefly_weave

    root = Path(firefly_weave.__file__).parent if root is None else root
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise RuntimeError("Local development builds cannot contain symbolic links")
        if path.is_file():
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    document = json.dumps({"format": LOCAL_BUILD_FORMAT, "files": files}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(document.encode()).hexdigest()


def verify_local_build(image_digest: str | None) -> None:
    """Require the running package to be the unchanged virtual-environment install registered for the release."""
    import firefly_weave

    try:
        root = Path(firefly_weave.__file__).resolve().parent
        if sys.prefix == sys.base_prefix or not root.is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("Local development builds run only from an isolated virtual environment")
        if image_digest is None or local_build_identity(root) != image_digest:
            raise ValueError("Local development build identity mismatch")
    except (OSError, ValueError, RuntimeError):
        raise RuntimeError("Native execution requires the unchanged local development runtime") from None


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
        self.closing = False
        # Pause before restarting a worker whose run ended with a task it could not settle. Each
        # failure in a row doubles it, up to `max_restart_seconds`; a run that lasted that long
        # counts as recovered.
        self.restart_seconds = 1.0
        self.max_restart_seconds = 30.0
        # Readiness reports a worker that keeps failing: this many failures in a row, until a run
        # lasts `max_restart_seconds`.
        self.unready_after = 3
        self.failing: dict[int, tuple[int, float]] = {}

    async def open(self) -> None:
        if not self.entries:
            return
        builds = {entry.build for entry in self.entries}
        if builds == {"image"}:
            verify_packaged_build()
        elif builds == {"local-development"}:
            verify_local_build(self.image_digest)
        else:
            raise RuntimeError("Native executors must share one build attestation")
        if self.image_digest is None:
            raise RuntimeError("Native dispatcher requires its operator-provisioned immutable image identity")
        for entry in self.entries:
            async with self.service.uow.open(entry.scope) as tx:
                actor = await self.service.access.load_principal(entry.principal_id, tx=tx)
                release = await WorkerRepository(tx).release(entry.release_id)
                if release.image_digest != self.image_digest or not release.connector_bindings:
                    raise RuntimeError("Native release does not match configured build")
                if (
                    entry.build == "local-development"
                    and not {binding.adapter for binding in release.connector_bindings} <= LOCAL_DEVELOPMENT_ADAPTERS
                ):
                    raise RuntimeError("Local development execution serves only built-in declarative connectors")
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
        # A request can open the dispatcher. An empty context keeps the request's execution
        # lease and identity, which end with its response, out of the long-lived workers.
        self.tasks = [asyncio.create_task(self._supervise(worker), context=Context()) for worker in self.workers]

    async def _supervise(self, worker: Worker) -> None:
        """Keep one in-process worker serving after a task it could not settle.

        The worker fails fast on an ambiguous completion or a lost lease and never re-executes
        that task: server lease recovery decides about it. Restarting only the polling loop keeps
        native execution (and readiness) alive for every other task instead of ending it until the
        process restarts. Only the exception type is logged, because its text can carry task data.
        Failures in a row back off exponentially, and readiness reports a worker that keeps failing.
        """
        failures = 0
        while True:
            started = time.monotonic()
            try:
                await worker.run()
                return
            except Exception as error:
                if self.closing:
                    return
                failures = 1 if time.monotonic() - started >= self.max_restart_seconds else failures + 1
                logging.getLogger(__name__).error(
                    "Native connector worker stopped after %s (%d in a row); restarting it",
                    type(error).__name__,
                    failures,
                )
            self.failing[id(worker)] = (failures, time.monotonic())
            await asyncio.sleep(self.restart_delay(failures))
            if self.closing:
                return
            # The restarted run proves itself over the next `max_restart_seconds`.
            self.failing[id(worker)] = (failures, time.monotonic())

    def restart_delay(self, failures: int) -> float:
        """The pause before restarting after `failures` failures in a row: doubling, capped."""
        return min(self.restart_seconds * 2.0 ** min(failures - 1, 32), self.max_restart_seconds)

    def healthy(self) -> bool:
        now = time.monotonic()
        return all(not task.done() for task in self.tasks) and not any(
            failures >= self.unready_after and now - restarted < self.max_restart_seconds
            for failures, restarted in self.failing.values()
        )

    async def close(self) -> None:
        self.closing = True
        for worker in self.workers:
            await worker.stop()
        try:
            async with asyncio.timeout(self.shutdown_seconds):
                await asyncio.gather(*self.tasks)
        except (TimeoutError, Exception):
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
