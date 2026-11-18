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

"""A Kafka trigger moves to a new version: a new source binding for the mover, the old one revoked, the lease fenced.

Records are handed to the consumer as the Kafka loop hands them, so no broker is needed here.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_webhook_retarget import ORDER, activate_version, person, publish_version, refused

pytestmark = pytest.mark.integration
TOPIC = "orders"


async def tenant_admin(owner, principal, scope):
    """Connection management, which binding a source needs, is granted at the tenant."""
    async with owner.begin() as session:
        await session.execute(
            text(
                "INSERT INTO role_bindings(id,principal_id,role,tenant_id,project_id,environment_id,resources) "
                "VALUES(:id,:principal,'tenant_admin',:tenant,NULL,NULL,'{}')"
            ),
            {"id": uuid4(), "principal": principal, "tenant": scope.tenant_id},
        )


@pytest.fixture
async def kafka(worker_setup, worker_runtime_fixture, access_db, provisioned, services):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.manifest import KAFKA_DESCRIPTOR
    from firefly_weave.contracts.broker import BrokerTriggerRequest
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.triggers.kafka import KafkaTrigger

    _, _, _, creator, scope, first, _ = worker_setup
    sessions, owner, access, _ = access_db
    admin, _ = provisioned
    await tenant_admin(owner, creator.id, scope)
    creator = await access.load_principal(creator.id)
    registry = ConnectorRegistry()

    class UnusedPublisher:
        async def execute(self, input, context):
            raise AssertionError("Retarget tests never publish through the connector")

        async def test_connection(self, connection):
            raise AssertionError("Retarget tests never test the connection")

    registry.register_descriptor(KAFKA_DESCRIPTOR, UnusedPublisher())
    graph = services(sessions, registry=registry)
    definitions = graph.resolve(DefinitionService)
    published = await definitions.publish(
        creator,
        scope,
        "Connector",
        json.dumps(KAFKA_DESCRIPTOR.manifest.value),
        "json",
        "broker",
        context=AuditContext(),
    )
    connection = await graph.resolve(ConnectionService).create_revision(
        creator,
        scope,
        ConnectionRequest(
            name="broker",
            connector_version_id=published.id,
            config={
                "driver": "kafka",
                "cluster_id": "orders-cluster",
                "topics": [TOPIC],
                "bootstrap": ["kafka://127.0.0.1:9"],
                "security_protocol": "PLAINTEXT",
            },
            allowed_destinations=("kafka://127.0.0.1:9",),
        ),
        context=AuditContext(),
    )
    source = worker_runtime_fixture["source"]
    second = await activate_version(
        definitions,
        creator,
        scope,
        first,
        await publish_version(definitions, creator, scope, source, version="1.1.0"),
        expected=1,
    )
    service = graph.resolve(KafkaTrigger)
    route = await service.create(
        creator,
        scope,
        BrokerTriggerRequest(
            name="orders",
            connection_revision_id=connection.id,
            cluster_id="orders-cluster",
            topic=TOPIC,
            kind="run",
            activation_id=first.id,
            payload_schema={"type": "integer"},
            dead_letter_policy="receipt",
        ),
        context=AuditContext(),
    )
    # A consumer holds the route's lease, as the Kafka loop does while it reads the topic.
    consumer = await service.claim(creator, scope, route.id, context=AuditContext())
    mover = await person(access, admin, scope, "deployer", "operator", "viewer")
    await tenant_admin(owner, mover.id, scope)
    mover = await access.load_principal(mover.id)
    return SimpleNamespace(
        service=service,
        definitions=definitions,
        source=source,
        route=route,
        consumer=consumer,
        scope=scope,
        creator=creator,
        mover=mover,
        first=first,
        second=second,
        owner=owner,
        access=access,
        admin=admin,
    )


async def retarget(env, activation_id, revision, *, actor=None):
    from firefly_weave.access.audit import AuditContext

    return await env.service.retarget(
        actor or env.mover, env.scope, env.route.id, activation_id, revision, context=AuditContext()
    )


async def stored(env, sql, **values):
    async with env.owner() as session:
        return (await session.execute(text(sql), {"id": env.route.id, **values})).mappings().all()


def record(event=None):
    from firefly_weave.contracts.broker import BrokerRecord

    return BrokerRecord(
        cluster_id="orders-cluster", topic=TOPIC, partition=0, offset=7, event_id=event or str(uuid4()), raw_value=b"3"
    )


async def test_a_retarget_binds_the_mover_revokes_the_old_binding_and_fences_the_consumer(kafka):
    env = kafka
    from firefly_weave.compiler.canonical import canonical_digest
    from firefly_weave.contracts.broker import BrokerTriggerRequest

    [before] = await stored(env, "SELECT generation,owner_token,lease_until FROM broker_routes WHERE id=:id")
    assert before["owner_token"] is not None and before["lease_until"] is not None
    moved = await retarget(env, env.second.id, 1)
    assert (moved.id, moved.revision, moved.activation_id, moved.principal_id) == (
        env.route.id,
        2,
        env.second.id,
        env.mover.id,
    )
    assert moved.binding_id != env.route.binding_id
    [after] = await stored(
        env, "SELECT generation,owner_token,lease_until,binding_id,principal_id,revision FROM broker_routes"
    )
    assert after["generation"] == before["generation"] + 1
    assert after["owner_token"] is None and after["lease_until"] is None
    assert (after["binding_id"], after["principal_id"], after["revision"]) == (moved.binding_id, env.mover.id, 2)
    bindings = {
        row["id"]: row
        for row in await stored(
            env,
            "SELECT id,revoked,principal_id,source_id,source_fingerprint FROM connection_source_bindings "
            "WHERE source_id=:id",
        )
    }
    assert bindings[env.route.binding_id]["revoked"] is True
    new = bindings[moved.binding_id]
    assert (new["revoked"], new["principal_id"], new["source_id"]) == (False, env.mover.id, env.route.id)
    request = BrokerTriggerRequest(**{k: getattr(moved, k) for k in BrokerTriggerRequest.model_fields}).model_dump(
        mode="json"
    )
    assert new["source_fingerprint"] == canonical_digest(request)
    # A retarget moves the route; it never adds one, so the environment's route capacity is unchanged.
    assert len(await stored(env, "SELECT id FROM broker_routes")) == 1


async def test_the_old_consumer_stops_and_a_new_claim_starts_the_new_version_as_the_mover(kafka):
    env = kafka
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.definitions.models import CatalogError

    await retarget(env, env.second.id, 1)
    with pytest.raises(CatalogError):
        await env.consumer.consume(record())
    claimed = await env.service.claim(env.mover, env.scope, env.route.id, context=AuditContext())
    receipt = await claimed.consume(record())
    assert receipt.status == "accepted"
    [run] = await stored(env, "SELECT activation_id,principal_id FROM runs WHERE id=:run", run=receipt.run_id)
    assert (run["activation_id"], run["principal_id"]) == (env.second.id, env.mover.id)


async def test_the_audit_names_activations_principals_and_bindings_only(kafka):
    env = kafka
    moved = await retarget(env, env.second.id, 1)
    [audit] = await stored(env, "SELECT target,event FROM access_audit WHERE action='broker.retarget'")
    assert audit["target"] == str(env.route.id)
    assert audit["event"]["details"] == {
        "kind": "kafka",
        "from_activation_id": str(env.first.id),
        "to_activation_id": str(env.second.id),
        "from_principal_id": str(env.creator.id),
        "to_principal_id": str(env.mover.id),
        "from_binding_id": str(env.route.binding_id),
        "to_binding_id": str(moved.binding_id),
    }


async def test_a_stale_or_missing_revision_and_a_stopped_trigger_are_refused(kafka):
    env = kafka
    from firefly_weave.access.audit import AuditContext

    assert await refused(retarget(env, env.second.id, None)) == (422, "WV-ETAG")
    assert await refused(retarget(env, env.second.id, 2)) == (409, "WV-REVISION")
    await env.service.control(env.creator, env.scope, env.route.id, "disable", context=AuditContext())
    stopped = await env.service.read(env.creator, env.scope, env.route.id, context=AuditContext())
    assert (stopped.disabled, stopped.revision) == (True, 2)
    assert await refused(retarget(env, env.second.id, 1)) == (409, "WV-REVISION")
    assert await refused(retarget(env, env.second.id, 2)) == (409, "WV-TRIGGER-DISABLED")


async def test_a_changed_input_and_another_workflow_are_refused(kafka):
    env = kafka
    changed = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(env.definitions, env.creator, env.scope, env.source, version="2.0.0", input_schema=ORDER),
        expected=2,
    )
    assert await refused(retarget(env, changed.id, 1)) == (409, "WV-TRIGGER-SCHEMA-CHANGED")
    other = await activate_version(
        env.definitions,
        env.creator,
        env.scope,
        env.first,
        await publish_version(env.definitions, env.creator, env.scope, env.source, version="1.0.0", name="other-flow"),
    )
    assert await refused(retarget(env, other.id, 1)) == (409, "WV-TRIGGER-WORKFLOW")
    # Nothing moved: the consumer that holds the lease keeps reading.
    assert (await env.consumer.consume(record())).status == "accepted"


async def test_the_mover_needs_what_creating_a_kafka_trigger_needs(kafka):
    env = kafka
    from firefly_weave.access.authorization import AccessDenied

    # Without connection management (no tenant grant), without binding, and without starting runs.
    for roles in (("deployer", "operator", "viewer"), ("operator", "viewer"), ("deployer", "viewer")):
        lacking = await person(env.access, env.admin, env.scope, *roles)
        if roles != ("deployer", "operator", "viewer"):
            await tenant_admin(env.owner, lacking.id, env.scope)
            lacking = await env.access.load_principal(lacking.id)
        with pytest.raises(AccessDenied):
            await retarget(env, env.second.id, 1, actor=lacking)
    [row] = await stored(env, "SELECT revision,binding_id FROM broker_routes WHERE id=:id")
    assert (row["revision"], row["binding_id"]) == (1, env.route.binding_id)


async def test_a_route_whose_binding_was_revoked_stays_stopped(kafka):
    env = kafka
    from firefly_weave.access.audit import AuditContext

    await env.service.bindings.revoke(env.creator, env.scope, env.route.binding_id, context=AuditContext())
    assert await refused(retarget(env, env.second.id, 1)) == (409, "WV-TRIGGER-DISABLED")
    [row] = await stored(env, "SELECT revision,binding_id FROM broker_routes WHERE id=:id")
    assert (row["revision"], row["binding_id"]) == (1, env.route.binding_id)
    assert await stored(env, "SELECT id FROM connection_source_bindings WHERE source_id=:id AND NOT revoked") == []


async def test_a_mover_without_connection_management_is_refused_before_the_route_is_read(kafka):
    env = kafka
    from firefly_weave.access.authorization import AccessDenied

    lacking = await person(env.access, env.admin, env.scope, "deployer", "operator", "viewer")
    # A stale revision would answer 409; the missing capability is refused first.
    with pytest.raises(AccessDenied):
        await retarget(env, env.second.id, 5, actor=lacking)
