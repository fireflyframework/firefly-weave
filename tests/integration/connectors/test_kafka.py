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

"""Actual task-owned Kafka and PostgreSQL; absent backends are explicit failures."""

import asyncio
import importlib.util
import json
import os
import socket
import stat
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

pytestmark = pytest.mark.integration


def kafka_data_directory():
    location = os.environ.get("WEAVE_TEST_KAFKA_DATA_ROOT")
    if not location:
        return None
    root = Path(location)
    info = root.lstat()
    if not root.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Kafka host data requires an absolute private owned directory")
    data = Path(tempfile.mkdtemp(prefix="broker-", dir=root))
    # The bind target must be writable by the image UID; its host parent remains private.
    data.chmod(0o777)
    return data


async def docker(*args):
    if os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") != "colima-weave-tests":
        pytest.fail("Kafka tests require WEAVE_TEST_DOCKER_CONTEXT=colima-weave-tests", pytrace=False)
    process = await asyncio.create_subprocess_exec(
        "docker",
        "--context",
        "colima-weave-tests",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert process.returncode == 0, "Owned Kafka Docker command failed; configuration withheld"
    return stdout.decode().strip()


@pytest.fixture(scope="module")
async def kafka_backend(tmp_path_factory):
    image = os.environ.get("WEAVE_TEST_KAFKA_IMAGE")
    if not image or not image.startswith("apache/kafka@sha256:"):
        pytest.fail("Real Kafka required: set WEAVE_TEST_KAFKA_IMAGE to the owned pinned Apache Kafka digest")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    project = "weave-d2-" + uuid4().hex[:16]
    env = tmp_path_factory.mktemp("d2-kafka") / "kafka.env"
    env.write_text(f"WEAVE_KAFKA_IMAGE={image}\nWEAVE_KAFKA_PORT={port}\n")
    # Retained projects must not consume a fresh daemon subnet on every test module.
    override = env.parent / "bridge.yaml"
    service = {"network_mode": "bridge"}
    data = kafka_data_directory()
    if data is not None:
        service["volumes"] = [{"type": "bind", "source": str(data), "target": "/tmp/kafka-logs"}]
    override.write_text(json.dumps({"services": {"kafka": service}}))
    args = (
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(env),
        "-f",
        "compose.kafka.yaml",
        "-f",
        str(override),
    )
    await docker(*args, "--profile", "kafka", "up", "-d", "kafka")
    identifier = await docker(*args, "ps", "-q", "kafka")
    try:
        for _ in range(90):
            producer = AIOKafkaProducer(bootstrap_servers=f"127.0.0.1:{port}", request_timeout_ms=1000)
            try:
                await producer.start()
                await producer.send_and_wait("weave-d2-readiness", b"owned")
                break
            except Exception:
                await asyncio.sleep(0.5)
            finally:
                await producer.stop()
        else:
            pytest.fail("Real owned Kafka did not become ready")
        proof = {"container": identifier, "image": image, "port": port, "project": project}
        target = os.environ.get("WEAVE_D2_KAFKA_PROOF_PATH")
        if target:
            Path(target).write_text(json.dumps(proof, indent=2) + "\n")
        yield proof
    finally:
        await docker(*args, "stop", "kafka")


@pytest.fixture
async def broker_record(kafka_backend):
    topic = "weave-d2-" + uuid4().hex
    event_id = str(uuid4())
    producer = AIOKafkaProducer(bootstrap_servers=f"127.0.0.1:{kafka_backend['port']}", acks="all")
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=f"127.0.0.1:{kafka_backend['port']}",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        group_id="fixture-" + uuid4().hex,
    )
    try:
        await producer.start()
        metadata = await producer.send_and_wait(topic, b"3", headers=[("weave-event-id", event_id.encode())])
        await consumer.start()
        record = await asyncio.wait_for(consumer.getone(), 10)
        assert (record.topic, record.partition, record.offset, record.value) == (
            topic,
            metadata.partition,
            metadata.offset,
            b"3",
        )
        yield {
            "cluster_id": "owned-d2",
            "topic": topic,
            "partition": record.partition,
            "offset": record.offset,
            "event_id": event_id,
            "raw_value": record.value,
        }
    finally:
        await consumer.stop()
        await producer.stop()


async def test_real_backend_record_roundtrip(kafka_backend, broker_record, access_db):
    assert broker_record["raw_value"] == b"3"
    assert kafka_backend["container"]


@pytest.fixture
async def kafka_setup(worker_setup, access_db, services, kafka_backend, broker_record):
    assert importlib.util.find_spec("firefly_weave.triggers.kafka") is not None, "Durable Kafka trigger absent"
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.manifest import KAFKA_DESCRIPTOR
    from firefly_weave.contracts.broker import BrokerTriggerRequest
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.triggers.kafka import KafkaTrigger

    _, _, _, actor, scope, activation, _ = worker_setup
    # Fixture-owner provisioning adds the explicitly required connection management grant.
    async with access_db[1].begin() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO role_bindings(id,principal_id,role,tenant_id,project_id,environment_id,resources) "
                "VALUES(:id,:principal,'tenant_admin',:tenant,NULL,NULL,'{}')"
            ),
            {"id": uuid4(), "principal": actor.id, "tenant": scope.tenant_id},
        )
    actor = await access_db[2].load_principal(actor.id)
    registry = ConnectorRegistry()

    class UnusedPublisher:
        async def execute(self, input, context):
            raise AssertionError("Receipt tests never publish through the connector")

        async def test_connection(self, connection):
            raise AssertionError("Receipt tests never test connection")

    registry.register_descriptor(KAFKA_DESCRIPTOR, UnusedPublisher())
    graph = services(access_db[0], registry=registry)
    definitions = graph.resolve(DefinitionService)
    published = await definitions.publish(
        actor, scope, "Connector", json.dumps(KAFKA_DESCRIPTOR.manifest.value), "json", "broker", context=AuditContext()
    )
    connection = await graph.resolve(ConnectionService).create_revision(
        actor,
        scope,
        ConnectionRequest(
            name="broker",
            connector_version_id=published.id,
            config={
                "driver": "kafka",
                "cluster_id": "owned-d2",
                "topics": [broker_record["topic"]],
                "bootstrap": [f"kafka://127.0.0.1:{kafka_backend['port']}"],
                "security_protocol": "PLAINTEXT",
            },
            allowed_destinations=(f"kafka://127.0.0.1:{kafka_backend['port']}",),
        ),
        context=AuditContext(),
    )
    service = graph.resolve(KafkaTrigger)
    route = await service.create(
        actor,
        scope,
        BrokerTriggerRequest(
            name="source",
            connection_revision_id=connection.id,
            cluster_id="owned-d2",
            topic=broker_record["topic"],
            kind="run",
            activation_id=activation.id,
            payload_schema={"type": "integer"},
            dead_letter_policy="receipt",
        ),
        context=AuditContext(),
    )
    source = await service.claim(actor, scope, route.id, context=AuditContext())
    return source, route, actor, scope, graph, connection


@pytest.fixture
async def kafka_trigger(kafka_setup):
    return kafka_setup[0]


async def test_readonly_consumer_authority_and_credentials_observe_revocation(kafka_setup):
    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.source_bindings import SourceBindingService
    from firefly_weave.definitions.models import CatalogError

    source, route, actor, scope, graph, connection = kafka_setup
    await source.service.authorize(source.authority)
    revision, secrets = await source.service.credentials(source.authority)
    assert revision == connection and secrets == {}
    # The loop's direct authority check uses the same PostgreSQL read-only mode.
    async with source.service.runtime.definitions.transaction(scope, None, mutation=False) as tx:
        assert await tx.session.scalar(text("SHOW transaction_read_only")) == "on"
        checked, current = await source.service.checked(tx, source.authority)
    assert checked.id == route.id and current.id == actor.id
    await graph.resolve(SourceBindingService).revoke(actor, scope, route.binding_id, context=AuditContext())
    for read in (source.service.authorize, source.service.credentials):
        with pytest.raises(CatalogError):
            await read(source.authority)


async def test_redelivery_after_receipt_commit_creates_one_run(kafka_trigger, broker_record, access_db):
    from sqlalchemy import text

    from firefly_weave.triggers.kafka import BrokerRecord

    record = BrokerRecord(**broker_record)
    first = await kafka_trigger.consume(record)
    second = await kafka_trigger.consume(record)
    assert first.run_id == second.run_id
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 1


async def test_duplicate_event_new_offset_and_changed_payload(kafka_trigger, broker_record, access_db):
    from dataclasses import replace

    from sqlalchemy import text

    from firefly_weave.contracts.broker import BrokerRecord

    record = BrokerRecord(**broker_record)
    first = await kafka_trigger.consume(record)
    duplicate = await kafka_trigger.consume(replace(record, offset=record.offset + 1))
    conflict = await kafka_trigger.consume(replace(record, offset=record.offset + 2, raw_value=b"4"))
    assert duplicate.run_id == first.run_id
    assert conflict.status == "rejected" and conflict.code == "BROKER_EVENT_CONFLICT"
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
        assert await session.scalar(text("SELECT count(*) FROM broker_receipts")) == 3
        assert await session.scalar(text("SELECT count(*) FROM broker_incidents")) == 1


async def test_invalid_schema_is_durable_metadata_only_rejection(kafka_trigger, broker_record, access_db):
    from dataclasses import replace

    from sqlalchemy import text

    from firefly_weave.contracts.broker import BrokerRecord

    record = replace(BrokerRecord(**broker_record), raw_value=b'{"password":"secret-canary"}')
    receipt = await kafka_trigger.consume(record)
    assert receipt.status == "rejected" and not hasattr(receipt, "run_id")
    assert await kafka_trigger.consume(record) == receipt
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
        stored = (await session.execute(text("SELECT request_hash,event_id,payload FROM broker_receipts"))).first()
        assert stored.request_hash is None and stored.event_id is None
        evidence = await session.scalar(text("SELECT evidence FROM broker_incidents"))
        assert evidence["available"] is False
        assert "secret-canary" not in json.dumps([stored.payload, evidence])


async def test_current_binding_revocation_denies_even_duplicate(kafka_setup, broker_record):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.source_bindings import SourceBindingService
    from firefly_weave.contracts.broker import BrokerRecord
    from firefly_weave.definitions.models import CatalogError

    source, route, actor, scope, graph, _ = kafka_setup
    record = BrokerRecord(**broker_record)
    await source.consume(record)
    await graph.resolve(SourceBindingService).revoke(actor, scope, route.binding_id, context=AuditContext())
    with pytest.raises(CatalogError):
        await source.consume(record)


async def test_publish_returns_actual_broker_ack_metadata(kafka_backend):
    from datetime import UTC, datetime, timedelta

    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka import KafkaConnector
    from firefly_weave.connectors.kafka_transport import BrokerClients
    from firefly_weave.contracts.connectors import ActionContext, ConnectionRevision, ConnectorInvocation

    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="publish",
        connector_version_id=uuid4(),
        connector="weave-kafka@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-kafka",
        config={
            "driver": "kafka",
            "cluster_id": "owned-d2",
            "topics": ["weave-d2-publish"],
            "bootstrap": [f"kafka://127.0.0.1:{kafka_backend['port']}"],
            "security_protocol": "PLAINTEXT",
        },
        allowed_destinations=(f"kafka://127.0.0.1:{kafka_backend['port']}",),
    )
    clients = BrokerClients(
        BrokerPolicy(
            enabled=True,
            routes=(
                BrokerRoutePin(
                    connection_revision_id=connection.id,
                    advertised=connection.allowed_destinations[0],
                    addresses=("127.0.0.1",),
                    plaintext=True,
                ),
            ),
        )
    )

    async def authorize():
        pass

    async def credentials(slot):
        raise AssertionError("Plaintext has no credentials")

    context = ActionContext(
        "owned-publish-operation",
        datetime.now(UTC) + timedelta(seconds=20),
        credentials,
        ConnectorInvocation(connection, {"topic": "weave-d2-publish"}, "publish", {}, {}, 1048576, 1048576),
        authorize=authorize,
    )
    result = await KafkaConnector(clients).execute({"payload": 3}, context)
    assert result["topic"] == "weave-d2-publish" and result["partition"] >= 0 and result["offset"] >= 0
    consumer = AIOKafkaConsumer(
        "weave-d2-publish",
        bootstrap_servers=f"127.0.0.1:{kafka_backend['port']}",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    try:
        await consumer.start()
        record = await asyncio.wait_for(consumer.getone(), 10)
        assert record.value == b'{"payload":3}'
        assert tuple(record.headers) == (("weave-event-id", result["event_id"].encode()),)
    finally:
        await consumer.stop()
        await clients.close()
    assert not clients.owners


async def test_owned_consumer_commit_after_receipt_and_crash_replay(
    kafka_setup, broker_record, kafka_backend, access_db
):
    from sqlalchemy import text

    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerClients

    source, route, actor, scope, graph, connection = kafka_setup
    clients = BrokerClients(
        BrokerPolicy(
            enabled=True,
            routes=(
                BrokerRoutePin(
                    connection_revision_id=connection.id,
                    advertised=connection.allowed_destinations[0],
                    addresses=("127.0.0.1",),
                    plaintext=True,
                ),
            ),
            max_clients=2,
        )
    )
    owner = await clients.acquire(connection, consumer=True)
    group = "weave-" + str(route.id)
    try:
        await owner.start_consumer(connection, None, route.topic, group)
        records = ()
        async with asyncio.timeout(10):
            while not records:
                records = await owner.fetch(route.cluster_id)
        record, generation = records[0]
        # Closing before receipt models process death before the local transaction.
        await clients.release(owner)
        async with access_db[1]() as session:
            assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
        owner = await clients.acquire(connection, consumer=True)
        await owner.start_consumer(connection, None, route.topic, group)
        records = ()
        async with asyncio.timeout(10):
            while not records:
                records = await owner.fetch(route.cluster_id)
        receipt = await source.consume(records[0][0])
        # The acknowledged DB receipt survives death before committing Kafka offset.
        await clients.release(owner)
        owner = await clients.acquire(connection, consumer=True)
        await owner.start_consumer(connection, None, route.topic, group)
        records = ()
        async with asyncio.timeout(10):
            while not records:
                records = await owner.fetch(route.cluster_id)
        replay = await source.consume(records[0][0])
        assert replay.run_id == receipt.run_id
        assert await owner.commit(*records[0]) is True
        async with access_db[1]() as session:
            assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
        observer = AIOKafkaConsumer(
            bootstrap_servers=f"127.0.0.1:{kafka_backend['port']}", group_id=group, enable_auto_commit=False
        )
        try:
            from aiokafka import TopicPartition

            await observer.start()
            assert await observer.committed(TopicPartition(record.topic, record.partition)) == record.offset + 1
        finally:
            await observer.stop()
    finally:
        await clients.close()


async def test_background_traversal_restart_and_execute_only_limits(kafka_setup, access_db, scheduler_url):
    from pydantic import SecretStr
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerClients
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.settings import Settings
    from firefly_weave.triggers.kafka_loop import KafkaLoop

    source, _, _, _, graph, connection = kafka_setup
    await source.service.release(source.authority)
    async with access_db[1].begin() as session:
        await session.execute(text("UPDATE broker_routes SET eligible_at=clock_timestamp()"))
    policy = BrokerPolicy(
        enabled=True,
        max_clients=2,
        routes=(
            BrokerRoutePin(
                connection_revision_id=connection.id,
                advertised=connection.allowed_destinations[0],
                addresses=("127.0.0.1",),
                plaintext=True,
            ),
        ),
    )
    clients = BrokerClients(policy)
    settings = Settings(
        database_url=access_db[3].render_as_string(hide_password=False),
        scheduler_database_url=SecretStr(scheduler_url),
        broker=policy,
        kafka_consumer_enabled=True,
        scheduler_enabled=False,
    )
    loop = KafkaLoop(settings, UnitOfWork(access_db[0]), source.service, clients)
    try:
        await loop.open()
        async with asyncio.timeout(20):
            while True:
                async with access_db[1]() as session:
                    if await session.scalar(text("SELECT count(*) FROM runs")) == 1:
                        break
                await asyncio.sleep(0.1)
        assert loop.turns
    finally:
        await loop.close()
        await clients.close()
    engine = create_async_engine(scheduler_url, hide_parameters=True)
    try:
        for size in (None, 0, 17):
            async with engine.connect() as conn:
                with pytest.raises(__import__("sqlalchemy").exc.DBAPIError):
                    await conn.execute(text("SELECT weave_broker_tenants(:size)"), {"size": size})
        async with engine.connect() as conn:
            with pytest.raises(__import__("sqlalchemy").exc.DBAPIError):
                await conn.execute(text("SELECT * FROM broker_routes"))
    finally:
        await engine.dispose()


@pytest.fixture
async def ack_loss_proxy(kafka_backend):
    import struct
    from contextlib import suppress

    port = kafka_backend["port"]
    accepted = asyncio.Event()
    handlers = set()

    async def proxy(reader, writer):
        handlers.add(asyncio.current_task())
        backend_reader, backend_writer = await asyncio.open_connection("127.0.0.1", port)
        apis = {}

        async def requests():
            while True:
                size = await reader.readexactly(4)
                frame = await reader.readexactly(struct.unpack(">i", size)[0])
                api, _, correlation = struct.unpack(">hhi", frame[:8])
                apis[correlation] = api
                backend_writer.write(size + frame)
                await backend_writer.drain()

        async def responses():
            while True:
                size = await backend_reader.readexactly(4)
                frame = await backend_reader.readexactly(struct.unpack(">i", size)[0])
                correlation = struct.unpack(">i", frame[:4])[0]
                if apis.get(correlation) == 0:
                    accepted.set()
                    await asyncio.Event().wait()
                writer.write(size + frame)
                await writer.drain()

        tasks = [asyncio.create_task(requests()), asyncio.create_task(responses())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            backend_writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
                await backend_writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(proxy, "::1", port, family=socket.AF_INET6)
    try:
        yield accepted
    finally:
        server.close()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()


async def test_real_accept_then_lost_produce_ack_is_unknown(kafka_backend, ack_loss_proxy):
    from datetime import UTC, datetime, timedelta

    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka import KafkaConnector
    from firefly_weave.connectors.kafka_transport import BrokerClients
    from firefly_weave.contracts.connectors import (
        ActionContext,
        ConnectionRevision,
        ConnectorFailure,
        ConnectorInvocation,
    )

    port = kafka_backend["port"]
    accepted = ack_loss_proxy
    topic = "weave-d2-ackloss-" + uuid4().hex
    revision = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="ambiguous",
        connector_version_id=uuid4(),
        connector="weave-kafka@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-kafka",
        config={
            "driver": "kafka",
            "cluster_id": "owned-d2",
            "topics": [topic],
            "bootstrap": [f"kafka://127.0.0.1:{port}"],
            "security_protocol": "PLAINTEXT",
        },
        allowed_destinations=(f"kafka://127.0.0.1:{port}",),
    )
    clients = BrokerClients(
        BrokerPolicy(
            enabled=True,
            routes=(
                BrokerRoutePin(
                    connection_revision_id=revision.id,
                    advertised=revision.allowed_destinations[0],
                    addresses=("::1",),
                    plaintext=True,
                ),
            ),
        )
    )

    async def authorize():
        pass

    async def credentials(slot):
        raise AssertionError

    context = ActionContext(
        "ack-loss-" + uuid4().hex,
        datetime.now(UTC) + timedelta(seconds=10),
        credentials,
        ConnectorInvocation(revision, {"topic": topic}, "publish", {}, {}, 1048576, 1048576),
        authorize=authorize,
    )
    try:
        with pytest.raises(ConnectorFailure) as error:
            await KafkaConnector(clients).execute({"payload": 3}, context)
        assert accepted.is_set()
        assert error.value.outcome == "unknown"
        consumer = AIOKafkaConsumer(
            topic, bootstrap_servers=f"127.0.0.1:{port}", enable_auto_commit=False, auto_offset_reset="earliest"
        )
        try:
            await consumer.start()
            record = await asyncio.wait_for(consumer.getone(), 10)
            assert record.value == b'{"payload":3}'
        finally:
            await consumer.stop()
    finally:
        await clients.close()


async def test_receipt_transaction_failure_rolls_back_run_and_replays(
    kafka_trigger, broker_record, access_db, monkeypatch
):
    from sqlalchemy import text

    from firefly_weave.contracts.broker import BrokerRecord
    from firefly_weave.runtime.repository import RuntimeRepository

    original = RuntimeRepository.execute

    async def fail_receipt(self, sql, **values):
        if sql.startswith("INSERT INTO broker_receipts"):
            raise RuntimeError("controlled failure before DB commit")
        await original(self, sql, **values)

    with monkeypatch.context() as patch:
        patch.setattr(RuntimeRepository, "execute", fail_receipt)
        with pytest.raises(RuntimeError, match="controlled failure"):
            await kafka_trigger.consume(BrokerRecord(**broker_record))
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
        assert await session.scalar(text("SELECT count(*) FROM broker_events")) == 0
        assert await session.scalar(text("SELECT count(*) FROM broker_receipts")) == 0
    assert (await kafka_trigger.consume(BrokerRecord(**broker_record))).status == "accepted"


async def test_secret_rejection_omits_hash_before_semantic_identity(kafka_setup, broker_record, access_db):
    from dataclasses import replace

    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.broker import BrokerRecord, BrokerTriggerRequest

    source, route, actor, scope, _, connection = kafka_setup
    request = BrokerTriggerRequest(
        name="secret-source",
        connection_revision_id=connection.id,
        cluster_id=route.cluster_id,
        topic=route.topic,
        kind="run",
        activation_id=route.activation_id,
        payload_schema={"x-secret": True},
        dead_letter_policy="receipt",
    )
    secret_route = await source.service.create(actor, scope, request, context=AuditContext())
    secret_source = await source.service.claim(actor, scope, secret_route.id, context=AuditContext())
    record = replace(BrokerRecord(**broker_record), raw_value=b'"sensitive-rejected-canary"')
    result = await secret_source.consume(record)
    assert result.code == "BROKER_SECRET"
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM broker_events")) == 0
        assert await session.scalar(text("SELECT request_hash FROM broker_receipts")) is None
        evidence = await session.scalar(text("SELECT evidence FROM broker_incidents"))
        assert evidence["omissions"] == [{"path": "", "reason": "classified_secret"}]


async def test_consumer_slot_reservation_allows_real_publish(kafka_setup):
    from datetime import UTC, datetime, timedelta

    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka import KafkaConnector
    from firefly_weave.connectors.kafka_transport import BrokerClients
    from firefly_weave.contracts.connectors import ActionContext, ConnectorFailure, ConnectorInvocation

    _, route, _, _, _, revision = kafka_setup
    clients = BrokerClients(
        BrokerPolicy(
            enabled=True,
            max_clients=2,
            routes=(
                BrokerRoutePin(
                    connection_revision_id=revision.id,
                    advertised=revision.allowed_destinations[0],
                    addresses=("127.0.0.1",),
                    plaintext=True,
                ),
            ),
        )
    )
    consumer = await clients.acquire(revision, consumer=True)
    try:
        await consumer.start_consumer(revision, None, route.topic, "reserved-" + str(uuid4()))
        with pytest.raises(ConnectorFailure, match="BROKER_CONSUMER_CAPACITY"):
            await clients.acquire(revision, consumer=True)

        async def authorize():
            pass

        async def credentials(slot):
            raise AssertionError

        context = ActionContext(
            str(uuid4()),
            datetime.now(UTC) + timedelta(seconds=10),
            credentials,
            ConnectorInvocation(revision, {"topic": route.topic}, "publish", {}, {}, 1048576, 1048576),
            authorize=authorize,
        )
        result = await KafkaConnector(clients).execute({"payload": 4}, context)
        assert result["offset"] >= 1
        assert consumer in clients.owners
    finally:
        await clients.close()


async def test_binding_revocation_during_provider_io_discards_credentials(kafka_setup, access_db, services):
    import threading

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.registry import ConnectorRegistry
    from firefly_weave.connections.secrets import ScopedSecrets, SecretGrant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connections.source_bindings import SourceBindingService
    from firefly_weave.contracts.broker import BrokerTriggerRequest
    from firefly_weave.contracts.connectors import ConnectionRequest, ResolvedSecret
    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.triggers.kafka import KafkaTrigger

    source, route, actor, scope, graph, connection = kafka_setup
    entered, release = threading.Event(), threading.Event()

    class Provider:
        def resolve(self, handle):
            entered.set()
            assert release.wait(3)
            return ResolvedSecret(value="provider-race-canary", provider_version="version-one")

    scoped = ScopedSecrets({"test": Provider()}, (SecretGrant(scope, "broker-secret", "test", "owned"),))
    fresh = services(access_db[0], registry=graph.resolve(ConnectorRegistry), secrets=scoped)
    revision = await fresh.resolve(ConnectionService).create_revision(
        actor,
        scope,
        ConnectionRequest(
            name="authenticated",
            connector_version_id=connection.connector_version_id,
            config=connection.config | {"security_protocol": "SASL_SSL", "username": "owned"},
            secretRef={"password": "broker-secret"},
            allowed_destinations=connection.allowed_destinations,
        ),
        context=AuditContext(),
    )
    trigger = fresh.resolve(KafkaTrigger)
    bound = await trigger.create(
        actor,
        scope,
        BrokerTriggerRequest(
            name="authenticated-source",
            connection_revision_id=revision.id,
            cluster_id=route.cluster_id,
            topic=route.topic,
            kind="run",
            activation_id=route.activation_id,
            dead_letter_policy="receipt",
        ),
        context=AuditContext(),
    )
    claimed = await trigger.claim(actor, scope, bound.id, context=AuditContext())
    pending = asyncio.create_task(trigger.credentials(claimed.authority))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await fresh.resolve(SourceBindingService).revoke(actor, scope, bound.binding_id, context=AuditContext())
        release.set()
        with pytest.raises(CatalogError):
            await pending
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_cached_consumer_revocation_aborts_socket_lifetime(kafka_setup, access_db):
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.connections.source_bindings import SourceBindingService
    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerClients
    from firefly_weave.persistence.uow import UnitOfWork
    from firefly_weave.settings import Settings
    from firefly_weave.triggers.kafka_loop import KafkaLoop

    source, route, actor, scope, graph, connection = kafka_setup
    policy = BrokerPolicy(
        enabled=True,
        max_clients=2,
        routes=(
            BrokerRoutePin(
                connection_revision_id=connection.id,
                advertised=connection.allowed_destinations[0],
                addresses=("127.0.0.1",),
                plaintext=True,
            ),
        ),
    )
    clients = BrokerClients(policy)
    loop = KafkaLoop(
        Settings(database_url=access_db[3].render_as_string(hide_password=False), broker=policy),
        UnitOfWork(access_db[0]),
        source.service,
        clients,
    )
    turn = asyncio.create_task(loop.turn(source))
    try:
        async with asyncio.timeout(10):
            while not clients.owners or not next(iter(clients.owners)).client:
                await asyncio.sleep(0.05)
        owner = next(iter(clients.owners))
        await graph.resolve(SourceBindingService).revoke(actor, scope, route.binding_id, context=AuditContext())
        async with asyncio.timeout(5):
            while not turn.done():
                await asyncio.sleep(0.05)
        assert owner.loop.revoked.is_set()
        assert not owner.thread.is_alive()
        assert not clients.owners
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await clients.close()


async def test_real_rebalance_fences_old_assignment_commit(kafka_setup, kafka_backend):
    from aiokafka.admin import AIOKafkaAdminClient, NewPartitions

    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerClients

    source, route, _, _, _, revision = kafka_setup
    address = f"127.0.0.1:{kafka_backend['port']}"
    admin = AIOKafkaAdminClient(bootstrap_servers=address)
    await admin.start()
    try:
        await admin.create_partitions({route.topic: NewPartitions(2)})
    finally:
        await admin.close()
    producer = AIOKafkaProducer(bootstrap_servers=address)
    await producer.start()
    try:
        await producer.send_and_wait(
            route.topic, b"3", partition=1, headers=[("weave-event-id", str(uuid4()).encode())]
        )
    finally:
        await producer.stop()
    clients = BrokerClients(
        BrokerPolicy(
            enabled=True,
            max_clients=3,
            routes=(
                BrokerRoutePin(
                    connection_revision_id=revision.id,
                    advertised=revision.allowed_destinations[0],
                    addresses=("127.0.0.1",),
                    plaintext=True,
                ),
            ),
        )
    )
    first = await clients.acquire(revision, consumer=True)
    second = None
    try:
        await first.start_consumer(revision, None, route.topic, "rebalance-" + str(route.id))
        records = ()
        async with asyncio.timeout(10):
            while not records:
                records = await first.fetch(route.cluster_id)
        for record, _ in records:
            await source.consume(record)
        second = await clients.acquire(revision, consumer=True)
        await asyncio.wait_for(second.start_consumer(revision, None, route.topic, "rebalance-" + str(route.id)), 15)

        async def changed():
            return all(
                first.generations.get((record.topic, record.partition)) != generation for record, generation in records
            )

        async with asyncio.timeout(15):
            while not await first.call(changed):
                await asyncio.sleep(0.05)
        for record, generation in records:
            assert await first.commit(record, generation) is False
    finally:
        await clients.close()


async def test_halt_is_durably_blocked_and_retry_keeps_offset(kafka_setup, broker_record, access_db):
    from dataclasses import replace

    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.broker import BrokerRecord, BrokerTriggerRequest

    source, route, actor, scope, _, revision = kafka_setup
    halted = await source.service.create(
        actor,
        scope,
        BrokerTriggerRequest(
            name="halt-source",
            connection_revision_id=revision.id,
            cluster_id=route.cluster_id,
            topic=route.topic,
            kind="run",
            activation_id=route.activation_id,
            payload_schema={"type": "integer"},
            dead_letter_policy="halt",
        ),
        context=AuditContext(),
    )
    claimant = await source.service.claim(actor, scope, halted.id, context=AuditContext())
    invalid = replace(BrokerRecord(**broker_record), raw_value=b'"bad"')
    first = await claimant.consume(invalid)
    assert first.status == "rejected"
    assert (await source.service.read(actor, scope, halted.id, context=AuditContext())).blocked
    await source.service.control(actor, scope, halted.id, "retry", context=AuditContext())
    claimant = await source.service.claim(actor, scope, halted.id, context=AuditContext())
    assert await claimant.consume(invalid) == first
    assert (await source.service.read(actor, scope, halted.id, context=AuditContext())).blocked
    async with access_db[1]() as session:
        offsets = list((await session.execute(text("SELECT offset_id FROM broker_receipts"))).scalars())
        assert offsets == [broker_record["offset"]]
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0


@pytest.mark.parametrize("policy", ["receipt", "halt"])
async def test_wrapper_escape_is_rejected_without_runtime_mutation(kafka_setup, broker_record, access_db, policy):
    from dataclasses import replace

    from sqlalchemy import text

    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.broker import BrokerRecord, BrokerTriggerRequest

    source, route, actor, scope, _, revision = kafka_setup
    if policy == "halt":
        route = await source.service.create(
            actor,
            scope,
            BrokerTriggerRequest(
                name="malformed-halt",
                connection_revision_id=revision.id,
                cluster_id=route.cluster_id,
                topic=route.topic,
                kind="run",
                activation_id=route.activation_id,
                payload_schema={"type": "integer"},
                dead_letter_policy="halt",
            ),
            context=AuditContext(),
        )
        source = await source.service.claim(actor, scope, route.id, context=AuditContext())
    invalid = replace(BrokerRecord(**broker_record), raw_value=b'3,"discarded":{"credential":"wrapper-secret-canary"}')
    receipt = await source.consume(invalid)
    assert receipt.status == "rejected"
    assert (await source.service.read(actor, scope, route.id, context=AuditContext())).blocked == (policy == "halt")
    async with access_db[1]() as session:
        assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
        assert await session.scalar(text("SELECT count(*) FROM broker_events")) == 0
        row = (await session.execute(text("SELECT event_id,request_hash,payload FROM broker_receipts"))).one()
        assert row.event_id is None and row.request_hash is None
        assert "run_id" not in row.payload
        evidence = await session.scalar(text("SELECT evidence FROM broker_incidents"))
        assert "wrapper-secret-canary" not in json.dumps(evidence)
