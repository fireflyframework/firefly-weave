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

"""A platform that runs workflow transactions on real PostgreSQL, with worker releases for the test catalog.

Workers are driven by hand: a test claims, completes or fails each task, and runs the recovery loop's quanta.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import yaml
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext

CATALOG = Path(__file__).resolve().parents[1] / "fixtures/transactions/catalog.yaml"
INPUT = {
    "type": "object",
    "properties": {
        "moveId": {"type": "string", "minLength": 1, "maxLength": 200},
        "quantity": {"type": "integer", "minimum": 1},
    },
    "required": ["moveId", "quantity"],
    "additionalProperties": False,
}


def hold(identifier: str = "hold", **settings: Any) -> dict[str, Any]:
    return {
        "id": identifier,
        "kind": "action",
        "uses": "source-system.hold-quantity@1.0.0",
        "with": {"object": {"quantity": {"ref": "/input/quantity"}, "reference": {"ref": "/input/moveId"}}},
        "confirm": {
            "kind": "action",
            "uses": "source-system.confirm-hold@1.0.0",
            "with": {"object": {"holdId": {"ref": f"/steps/{identifier}/output/holdId"}}},
        },
        "cancel": {
            "kind": "action",
            "uses": "source-system.release-hold@1.0.0",
            "with": {
                "object": {"reference": {"ref": "/input/moveId"}, "holdOperation": {"ref": "/participant/operationKey"}}
            },
        },
        **settings,
    }


def add(identifier: str = "add", *, by_call: bool = False) -> dict[str, Any]:
    undo: dict[str, Any] = {
        "kind": "callWorkflow" if by_call else "action",
        "uses": "reverse-target-addition@1.0.0" if by_call else "target-system.remove-quantity@1.0.0",
        "with": {
            "object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}
        },
    }
    return {
        "id": identifier,
        "kind": "action",
        "uses": "target-system.add-quantity@1.0.0",
        "with": {"object": {"quantity": {"ref": "/input/quantity"}, "reference": {"ref": "/input/moveId"}}},
        "compensate": undo,
    }


def submit() -> dict[str, Any]:
    return {
        "id": "submit",
        "kind": "action",
        "uses": "orders.submit-order@1.0.0",
        "with": {"object": {"reference": {"ref": "/input/moveId"}}},
        "irreversible": True,
    }


def workflow(name: str, steps: list[dict[str, Any]], *, callable_: bool = False, timeout: int | None = None) -> dict:
    spec: dict[str, Any] = {"inputSchema": INPUT, "outputSchema": {}, "steps": steps, "output": {"literal": "done"}}
    if callable_:
        spec["callable"] = {}
    if timeout is not None:
        spec["timeoutSeconds"] = timeout
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


def transaction(*steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "move",
        "kind": "transaction",
        "businessKey": {"ref": "/input/moveId"},
        "steps": list(steps),
        "output": {"literal": "moved"},
    }


OUTPUTS = {
    "source-system-hold-quantity@1.0.0": {"holdId": "h-1"},
    "target-system-add-quantity@1.0.0": {"id": "a-1"},
    "orders-submit-order@1.0.0": {"orderId": "o-1"},
    "reports-lookup@1.0.0": {"count": 1},
}


class Platform(SimpleNamespace):
    """Services of one environment, its worker instances and the drivers the tests use."""

    async def publish(self, document: dict[str, Any], **pins: UUID):
        from firefly_weave.contracts.catalog import ActivationRequest

        published = await self.definitions.publish(
            self.actor, self.scope, "Workflow", json.dumps(document), "json", str(uuid4()), context=AuditContext()
        )
        request = ActivationRequest(
            scope=self.scope,
            version_id=published.id,
            artifact_digest=published.digest,
            worker_release_ids={task: self.release for task in self.needs(document)},
            workflow_activation_ids={f"{name}@1.0.0": identifier for name, identifier in pins.items()},
        )
        return await self.definitions.activate(self.actor, self.scope, request, str(uuid4()), context=AuditContext())

    def needs(self, document: Any) -> set[str]:
        """The task types the actions a workflow uses run on: activation pins exactly these."""
        found: set[str] = set()
        stack = [document]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if isinstance(item.get("uses"), str) and item["uses"] in self.actions:
                    found.add(self.actions[item["uses"]])
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return found

    async def start(self, activation, move: str = "M-1"):
        from firefly_weave.contracts.runtime import StartRunRequest

        return await self.runtime.start(
            self.actor,
            self.scope,
            StartRunRequest(activation_id=activation.id, input={"moveId": move, "quantity": 2}),
            str(uuid4()),
            context=AuditContext(),
        )

    async def claim(self, *, aware: bool | None = True, instance: int = 0, release: UUID | None = None) -> list:
        from firefly_weave.contracts.workers import ClaimWorker

        worker = None if aware is None else ClaimWorker(release=release or self.release, transaction_aware=aware)
        async with self.runtime.definitions.transaction(self.scope, None) as tx:
            return await self.tasks.claim(
                tx,
                self.instances[instance],
                10,
                actor=self.actor,
                scope=self.scope,
                context=AuditContext(),
                worker=worker,
            )

    async def lease(self, key: str, **claim: Any):
        """Claim until the task at instance key ``key`` is leased; other claimed leases stay with the test."""
        for lease in await self.claim(**claim):
            self.leased[lease.operation_key.partition(":")[2]] = lease
        return self.leased.pop(key)

    async def complete(self, lease, output: Any = None):
        async with self.runtime.definitions.transaction(self.scope, None) as tx:
            return await self.tasks.complete(
                tx,
                lease.proof,
                uuid4(),
                OUTPUTS.get(lease.capability, {}) if output is None else output,
                actor=self.actor,
                scope=self.scope,
                context=AuditContext(),
            )

    async def fail(self, lease, code: str = "UNAVAILABLE", **error: Any):
        from firefly_weave.contracts.workers import TaskError

        async with self.runtime.definitions.transaction(self.scope, None) as tx:
            return await self.tasks.fail(
                tx,
                lease.proof,
                TaskError(completion_id=uuid4(), code=code, **error),
                actor=self.actor,
                scope=self.scope,
                context=AuditContext(),
            )

    async def scan(self, rounds: int = 3) -> None:
        from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope

        for _ in range(rounds):
            await self.recovery._scan_scheduled(_SchedulerScope(self.scope, _CATALOG_AUTHORITY), 10)

    async def query(self, sql: str, **values: Any) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            return [dict(row) for row in (await session.execute(text(sql), values)).mappings()]

    async def execute(self, sql: str, **values: Any) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(text(sql), values)

    async def state(self, run_id: UUID) -> dict[str, Any]:
        return (await self.query("SELECT state FROM runs WHERE id=:id", id=run_id))[0]["state"]

    async def keys(self, run_id: UUID) -> list[str]:
        """Instance keys of the run's tasks, in the order they were issued."""
        rows = await self.query(
            "SELECT t.node_id FROM task_intents t JOIN task_facts f ON f.task_id=t.id WHERE t.run_id=:run "
            "ORDER BY f.created_at,t.node_id",
            run=run_id,
        )
        return [row["node_id"] for row in rows]

    async def record(self, run_id: UUID, key: str = "move") -> dict[str, Any]:
        rows = await self.query(
            "SELECT * FROM run_transactions WHERE run_id=:run AND instance_key=:key", run=run_id, key=key
        )
        return rows[0]

    async def expire(self, lease) -> None:
        """The worker went silent: its lease expired."""
        await self.execute(
            "UPDATE task_leases SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE task_id=:task AND generation=:generation",
            task=lease.proof.task_id,
            generation=lease.proof.generation,
        )


async def build(
    worker_setup, services, access_db, monkeypatch, *, releases: int = 1, aware_actions: frozenset[str] = frozenset()
) -> Platform:
    """Register the test catalog's actions and capabilities under ``releases`` worker releases.

    ``aware_actions`` names actions whose definitions claim ``transactionAware``.
    """
    from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.operations.incidents import IncidentService
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.runtime.service import RuntimeService

    tasks, _, workers, actor, scope, _, _ = worker_setup
    graph = services(access_db[0])
    definitions = graph.resolve(DefinitionService)
    fixture = yaml.safe_load(CATALOG.read_text())
    capabilities = fixture["tasks"]
    task_types = [f"{item['taskType']}@{item['taskVersion']}" for item in capabilities]
    registered = []
    for index in range(releases):
        release = await workers.register_release(
            actor,
            scope,
            ReleaseRequest.model_validate_json(
                json.dumps({"image_digest": "sha256:" + f"{index + 11:02x}" * 32, "capabilities": capabilities})
            ),
            context=AuditContext(),
        )
        registered.append(release.id)
    for definition in fixture["definitions"]:
        if definition["kind"] == "Action":
            if definition["metadata"]["name"] in aware_actions:
                definition["spec"]["transactionAware"] = True
            await definitions.publish(
                actor, scope, "Action", json.dumps(definition), "json", str(uuid4()), context=AuditContext()
            )
    instances = [
        (
            await workers.register_instance(
                actor,
                scope,
                InstanceRequest(release_id=registered[index % releases], task_types=task_types, capacity=50),
                context=AuditContext(),
            )
        ).id
        for index in range(max(2, releases))
    ]
    platform = Platform(
        definitions=definitions,
        runtime=graph.resolve(RuntimeService),
        recovery=graph.resolve(RecoveryService),
        incidents=graph.resolve(IncidentService),
        tasks=tasks,
        actor=actor,
        scope=scope,
        sessions=access_db[1],
        release=registered[0],
        releases=registered,
        instances=instances,
        task_types=[item["taskType"] for item in capabilities],
        actions={
            f"{item['metadata']['name']}@{item['metadata']['version']}": item["spec"]["implementation"]["taskType"]
            for item in fixture["definitions"]
            if item["kind"] == "Action"
        },
        leased={},
    )
    # The called reversal of an addition: a callable workflow whose own step removes the quantity.
    reverse = workflow(
        "reverse-target-addition",
        [
            {
                "id": "remove",
                "kind": "action",
                "uses": "target-system.remove-quantity@1.0.0",
                "with": {
                    "object": {"reference": {"ref": "/input/reference"}, "addOperation": {"ref": "/input/addOperation"}}
                },
            }
        ],
        callable_=True,
        timeout=300,
    )
    reverse["spec"]["inputSchema"] = next(
        item for item in fixture["definitions"] if item["metadata"]["name"] == "reverse-target-addition"
    )["spec"]["inputSchema"]
    platform.reverse = await platform.publish(reverse)
    return platform
