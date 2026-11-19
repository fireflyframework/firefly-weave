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

"""A real transaction through the native PostgreSQL connector: the cancel of a step that never applied reaches the
connector with its transaction and leaves the step's tombstone in the external database."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_postgresql import external_database as external_database

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration

HOLD = "INSERT INTO business.customers (customer_id, status) VALUES (:customerId, :status)"
CONFIRM = "UPDATE business.customers SET status = :status WHERE customer_id = :customerId"
RELEASE = "DELETE FROM business.customers WHERE customer_id = :customerId"


def parameters(*names: str) -> dict:
    return {"object": {"parameters": {"object": {name: {"ref": f"/input/parameters/{name}"} for name in names}}}}


@pytest.fixture
async def sql_transaction_setup(services, access_db, provisioned, external_database):
    from firefly_weave.access.models import Grant
    from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.manifest import POSTGRES_DESCRIPTOR
    from firefly_weave.connectors.postgresql import PostgresConnector, PostgresPolicy
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.connectors import ConnectionRequest, ResolvedSecret
    from firefly_weave.contracts.workers import CredentialGrantRequest, InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.runtime.recovery import RecoveryService
    from firefly_weave.workers.service import WorkerService

    scope = provisioned[1][0]
    admin = await access_db[2].load_principal(provisioned[0].id)
    actor_id = await access_db[2].create_principal(admin, "application")
    for role in ("developer", "deployer", "operator", "worker", "tenant_admin", "viewer"):
        await access_db[2].grant(
            admin,
            actor_id,
            Grant(role=role, scope=scope.model_copy(update={"environment_id": None}) if role == "developer" else scope),
        )
    actor = await access_db[2].load_principal(actor_id)

    class Provider:
        def resolve(self, handle):
            return ResolvedSecret(value=external_database["password"], provider_version="d1")

    secrets = ScopedSecrets({"test": Provider()}, (SecretGrant(scope, "sql-password", "test", "owned"),))
    graph = services(access_db[0], secrets=secrets)
    graph.register_instance(
        PostgresPolicy, PostgresPolicy(private_networks=("127.0.0.0/8",), plaintext_networks=("127.0.0.0/8",))
    )
    definitions = graph.resolve(DefinitionService)
    definitions.registry.register_descriptor(POSTGRES_DESCRIPTOR, graph.resolve(PostgresConnector))
    workers = graph.resolve(WorkerService)
    manifest = POSTGRES_DESCRIPTOR.manifest.value
    connector = await definitions.publish(
        actor, scope, "Connector", json.dumps(manifest), "json", "sql-connector", context=AuditContext()
    )
    binding = next(b for b in POSTGRES_DESCRIPTOR.bindings if b.action == "idempotent-command")
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest(
            image_digest="sha256:" + "e" * 64,
            capabilities=list(POSTGRES_DESCRIPTOR.capabilities),
            credential_capabilities=[binding.task_reference],
            connector_bindings=list(POSTGRES_DESCRIPTOR.bindings),
        ),
        context=AuditContext(),
    )
    port = external_database["kwargs"]["port"]
    revision = await graph.resolve(ConnectionService).create_revision(
        actor,
        scope,
        ConnectionRequest(
            name="sql",
            connector_version_id=connector.id,
            config={
                "dialect": "postgresql",
                "host": "127.0.0.1",
                "port": port,
                "database": external_database["name"],
                "user": external_database["roles"]["write"],
                "role": "command",
                "tls": "disable",
            },
            secretRef={"password": "sql-password"},
            allowed_destinations=(f"postgresql://127.0.0.1:{port}",),
        ),
        context=AuditContext(),
    )
    await workers.grant_connection(
        actor,
        scope,
        CredentialGrantRequest(
            release_id=release.id, connection_revision_id=revision.id, capability=binding.task_reference
        ),
        context=AuditContext(),
    )
    action_spec = manifest["spec"]["actions"]["idempotent-command"]
    # The hold's own parameter schema refuses a status longer than 3 characters, which the action's input schema
    # allows: the hold fails before it connects, so it never applies.
    for name, statement, schema in (
        ("sql-hold", HOLD, {"customerId": {"type": "string"}, "status": {"type": "string", "maxLength": 3}}),
        ("sql-confirm-hold", CONFIRM, {"customerId": {"type": "string"}, "status": {"type": "string"}}),
        ("sql-release-hold", RELEASE, {"customerId": {"type": "string"}}),
    ):
        action = {
            "apiVersion": "weave/v1alpha1",
            "kind": "Action",
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": {
                "implementation": {
                    "kind": "connector",
                    "uses": "weave-postgresql@1.0.0",
                    "action": "idempotent-command",
                    "config": {"statement": statement, "parameters": schema},
                },
                "connection": {"connector": "weave-postgresql@1.0.0"},
                "sideEffect": "idempotency_key",
                "timeoutSeconds": 30,
                "inputSchema": action_spec["inputSchema"],
                "outputSchema": action_spec["outputSchema"],
            },
        }
        await definitions.publish(actor, scope, "Action", json.dumps(action), "json", name, context=AuditContext())
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "sql-move", "version": "1.0.0"},
        "spec": {
            "connections": {"sql": {"connector": "weave-postgresql@1.0.0"}},
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {
                    "id": "move",
                    "kind": "transaction",
                    "steps": [
                        {
                            "id": "hold",
                            "kind": "action",
                            "uses": "sql-hold@1.0.0",
                            "connection": "sql",
                            "with": {"ref": "/input"},
                            "confirm": {
                                "kind": "action",
                                "uses": "sql-confirm-hold@1.0.0",
                                "with": parameters("customerId", "status"),
                            },
                            "cancel": {
                                "kind": "action",
                                "uses": "sql-release-hold@1.0.0",
                                "with": parameters("customerId"),
                            },
                        }
                    ],
                    "output": {"literal": "moved"},
                }
            ],
            "output": {"ref": "/steps/move/output"},
        },
    }
    version = await definitions.publish(
        actor, scope, "Workflow", json.dumps(workflow), "json", "sql-move", context=AuditContext()
    )
    activation = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            version_id=version.id,
            artifact_digest=version.digest,
            scope=scope,
            connection_revision_ids={"sql": revision.id},
            connector_release_ids={connector.id: release.id},
        ),
        "active",
        context=AuditContext(),
    )
    instance = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=release.id, task_types=[binding.task_reference], capacity=1),
        context=AuditContext(),
    )
    return graph, actor, scope, activation, instance, revision, graph.resolve(RecoveryService)


async def test_a_cancel_through_the_native_connector_carries_its_transaction_and_leaves_a_tombstone(
    sql_transaction_setup, access_db, external_database
):
    from firefly_weave.access.scheduler import _CATALOG_AUTHORITY, _SchedulerScope
    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.connectors import ConnectorFailure
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.contracts.workers import TaskError
    from firefly_weave.runtime.service import RuntimeService

    graph, actor, scope, activation, instance, revision, recovery = sql_transaction_setup
    who = uuid4().hex
    runtime = graph.resolve(RuntimeService)
    run = await runtime.start(
        actor,
        scope,
        StartRunRequest(activation_id=activation.id, input={"parameters": {"customerId": who, "status": "active"}}),
        str(uuid4()),
        context=AuditContext(),
    )
    executor = graph.resolve(ConnectorExecutionService)
    transport = ServiceTransport(executor, scope, actor.id, instance.id)
    hold = (await transport.claim(1))[0]
    assert (hold.operation_key, hold.transaction.phase) == (f"{run.id}:hold", "try")
    with pytest.raises(ConnectorFailure) as failed:
        await executor.execute(scope, actor.id, hold)
    assert (failed.value.code, failed.value.outcome) == ("SQL_INPUT", "not_started")
    await transport.fail(hold.proof, TaskError(completion_id=uuid4(), code="SQL_INPUT", outcome="not_started"))

    async def scan() -> None:
        for _ in range(3):
            await recovery._scan_scheduled(_SchedulerScope(scope, _CATALOG_AUTHORITY), 10)

    await scan()
    cancel = (await transport.claim(1))[0]
    assert cancel.operation_key == f"{run.id}:hold#cancel"
    assert cancel.transaction.model_dump(mode="json") == {
        "id": f"{run.id}:move",
        "branch_key": f"{run.id}:hold",
        "phase": "cancel",
        "mode": "reserve",
        "forward_outcome": "unknown",
    }
    result = await executor.execute(scope, actor.id, cancel)
    assert result == {"rowCount": 0, "rows": []}
    await transport.complete(cancel.proof, uuid4(), result)
    await scan()
    async with access_db[1]() as session:
        state = (
            await session.execute(
                text("SELECT state FROM run_transactions WHERE run_id=:run AND instance_key='move'"), {"run": run.id}
            )
        ).scalar_one()
    assert state == "cancelled"
    tombstone = await external_database["db"].fetchrow(
        "SELECT request_hash, result FROM weave_connector.operations WHERE scope = $1 AND operation_key = $2",
        str(revision.id),
        f"{run.id}:hold",
    )
    assert (None if tombstone is None else tuple(tombstone)) == ("tombstone", None)
    assert (
        await external_database["db"].fetchval("SELECT count(*) FROM business.customers WHERE customer_id = $1", who)
        == 0
    )
