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

"""Participants and workflows for the end-to-end participant check on PostgreSQL.

``unfenced_handlers`` are the example's handlers without a fence: they replay a repeated request by its key, but a
cancel of a step that never arrived records nothing, so the step still applies when it arrives later.
"""

import json
import os
from copy import deepcopy
from pathlib import Path

import asyncpg
import yaml

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition

CATALOG = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "fixtures/transactions/catalog.yaml").read_text(encoding="utf-8")
)
PROCESSED = "CREATE TABLE IF NOT EXISTS processed (operation_key text PRIMARY KEY, result jsonb NOT NULL)"
SQL_SCHEMAS = {
    "inputSchema": {
        "type": "object",
        "properties": {"parameters": {"type": "object"}},
        "required": ["parameters"],
        "additionalProperties": False,
    },
    "outputSchema": {
        "type": "object",
        "properties": {"rowCount": {"type": "integer", "minimum": 0}, "rows": {"type": "array"}},
        "required": ["rowCount", "rows"],
        "additionalProperties": False,
    },
}


def unfenced_handlers() -> dict:
    async def once(lease, change):
        connection = await asyncpg.connect(os.environ["PARTICIPANT_DATABASE_URL"])
        try:
            async with connection.transaction():
                found = await connection.fetchval(
                    "SELECT result::text FROM processed WHERE operation_key = $1", lease.operation_key
                )
                if found is not None:
                    return json.loads(found)
                answer = await change(connection)
                await connection.execute(
                    "INSERT INTO processed VALUES ($1, $2::text::jsonb)", lease.operation_key, json.dumps(answer)
                )
                return answer
        finally:
            await connection.close()

    async def hold(lease):
        async def change(connection):
            await connection.execute(
                "INSERT INTO holds (hold_id, reference, quantity, status) VALUES ($1, $2, $3, 'held')",
                lease.operation_key,
                lease.input["reference"],
                lease.input["quantity"],
            )
            return {"holdId": lease.operation_key}

        return await once(lease, change)

    async def confirm_hold(lease):
        async def change(connection):
            await connection.execute("UPDATE holds SET status = 'confirmed' WHERE hold_id = $1", lease.input["holdId"])
            return {}

        return await once(lease, change)

    async def release_hold(lease):
        async def change(connection):
            await connection.execute("DELETE FROM holds WHERE hold_id = $1", lease.input["holdOperation"])
            return {}

        return await once(lease, change)

    return {
        "source-system-hold-quantity@1.0.0": hold,
        "source-system-confirm-hold@1.0.0": confirm_hold,
        "source-system-release-hold@1.0.0": release_hold,
    }


def sql_action(name: str, statement: str, parameters: dict) -> dict:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": {
            "implementation": {
                "kind": "connector",
                "uses": "weave-postgresql@1.0.0",
                "action": "idempotent-command",
                "config": {"statement": statement, "parameters": parameters},
            },
            "connection": {"connector": "weave-postgresql@1.0.0"},
            "sideEffect": "idempotency_key",
            "timeoutSeconds": 30,
            **SQL_SCHEMAS,
        },
    }


SQL_ACTIONS = [
    sql_action(
        "sql.hold",
        "INSERT INTO business.customers (customer_id, status) VALUES (:reference, :status)",
        {"reference": {"type": "string"}, "status": {"type": "string"}},
    ),
    sql_action(
        "sql.confirm-hold",
        "UPDATE business.customers SET status = :status WHERE customer_id = :reference",
        {"reference": {"type": "string"}, "status": {"type": "string"}},
    ),
    sql_action(
        "sql.release-hold",
        "DELETE FROM business.customers WHERE customer_id = :reference",
        {"reference": {"type": "string"}},
    ),
]


def catalog() -> CatalogSnapshot:
    return CatalogSnapshot.from_definitions(
        [load_definition(item) for item in [*CATALOG["definitions"], *SQL_ACTIONS]], tasks=CATALOG["tasks"]
    )


def workflow() -> dict:
    """A move whose hold reserves and whose add an action undoes, both on the example's workers, and a hold of a
    customer row through the PostgreSQL connector."""

    def parameters(status: str | None) -> dict:
        values = {"reference": {"ref": "/input/moveId"}}
        if status is not None:
            values["status"] = {"literal": status}
        return {"object": {"parameters": {"object": values}}}

    undo = {"object": {"reference": {"ref": "/input/moveId"}, "addOperation": {"ref": "/participant/operationKey"}}}
    release = {"object": {"reference": {"ref": "/input/moveId"}, "holdOperation": {"ref": "/participant/operationKey"}}}
    quantity = {"object": {"quantity": {"ref": "/input/quantity"}, "reference": {"ref": "/input/moveId"}}}
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "move-checked", "version": "1.0.0"},
        "spec": {
            "connections": {"sql": {"connector": "weave-postgresql@1.0.0"}},
            "inputSchema": {
                "type": "object",
                "properties": {"moveId": {"type": "string"}, "quantity": {"type": "integer"}},
                "required": ["moveId", "quantity"],
            },
            "outputSchema": {},
            "steps": [
                {
                    "id": "move",
                    "kind": "transaction",
                    "businessKey": {"ref": "/input/moveId"},
                    "steps": [
                        {
                            "id": "hold",
                            "kind": "action",
                            "uses": "source-system.hold-quantity@1.0.0",
                            "with": quantity,
                            "confirm": {
                                "kind": "action",
                                "uses": "source-system.confirm-hold@1.0.0",
                                "with": {"object": {"holdId": {"ref": "/steps/hold/output/holdId"}}},
                            },
                            "cancel": {"kind": "action", "uses": "source-system.release-hold@1.0.0", "with": release},
                        },
                        {
                            "id": "add",
                            "kind": "action",
                            "uses": "target-system.add-quantity@1.0.0",
                            "with": quantity,
                            "compensate": {
                                "kind": "action",
                                "uses": "target-system.remove-quantity@1.0.0",
                                "with": undo,
                            },
                        },
                        {
                            "id": "reserve",
                            "kind": "action",
                            "uses": "sql.hold@1.0.0",
                            "connection": "sql",
                            "with": parameters("held"),
                            "confirm": {"kind": "action", "uses": "sql.confirm-hold@1.0.0", "with": parameters("kept")},
                            "cancel": {"kind": "action", "uses": "sql.release-hold@1.0.0", "with": parameters(None)},
                        },
                    ],
                    "output": {"literal": "moved"},
                }
            ],
            "output": {"literal": "done"},
        },
    }
    # No shared objects, so YAML written from it has no aliases, which the workflow reader refuses.
    return json.loads(json.dumps(document))


def lock() -> dict:
    """The catalog lock of ``catalog()``, as ``--catalog`` reads it."""
    from firefly_weave.compiler.catalog import FrozenDocument

    documents = deepcopy([*CATALOG["definitions"], *SQL_ACTIONS])
    return {
        "definitions": [
            {
                "document": item,
                "digest": FrozenDocument.from_value(load_definition(item).model_dump(by_alias=True)).digest,
            }
            for item in documents
        ],
        "tasks": CATALOG["tasks"],
        "adapters": [],
        "schemas": {},
    }
