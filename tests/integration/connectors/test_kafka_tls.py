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

"""Task-owned Kafka SASL/TLS and adversarial TLS broker protocol acceptance."""

import asyncio
import os
import secrets
import socket
import ssl
import struct
from uuid import uuid4

import pytest
from test_kafka import docker, kafka_data_directory

pytestmark = pytest.mark.integration


async def command(*args):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    assert await process.wait() == 0


@pytest.fixture
async def tls_material(tmp_path):
    key, cert, store = tmp_path / "broker.key", tmp_path / "broker.crt", tmp_path / "broker.p12"
    password = secrets.token_hex(20)
    password_file = tmp_path / "store-password"
    password_file.write_text(password)
    password_file.chmod(0o600)
    await command(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-days",
        "1",
        "-subj",
        "/CN=broker.invalid",
        "-addext",
        "subjectAltName=DNS:broker.invalid",
    )
    await command(
        "openssl",
        "pkcs12",
        "-export",
        "-in",
        str(cert),
        "-inkey",
        str(key),
        "-out",
        str(store),
        "-name",
        "kafka",
        "-passout",
        "file:" + str(password_file),
    )
    return key, cert, store, password


@pytest.fixture
async def tls_broker(tls_material, tmp_path):
    key, cert, store, password = tls_material
    image = os.environ.get("WEAVE_TEST_KAFKA_IMAGE")
    if not image or not image.startswith("apache/kafka@sha256:"):
        pytest.fail("Real Kafka TLS requires the owned immutable Kafka image")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    name = "weave-d2-tls-" + uuid4().hex[:12]
    sasl = secrets.token_hex(24)
    env = tmp_path / "kafka.env"
    config = {
        "KAFKA_NODE_ID": "1",
        "KAFKA_PROCESS_ROLES": "broker,controller",
        "KAFKA_LISTENERS": "INTERNAL://:9092,EXTERNAL://:9094,CONTROLLER://:9093",
        "KAFKA_ADVERTISED_LISTENERS": f"INTERNAL://localhost:9092,EXTERNAL://broker.invalid:{port}",
        "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "INTERNAL:PLAINTEXT,EXTERNAL:SASL_SSL,CONTROLLER:PLAINTEXT",
        "KAFKA_INTER_BROKER_LISTENER_NAME": "INTERNAL",
        "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
        "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
        "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
        "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
        "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
        "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
        "KAFKA_MESSAGE_MAX_BYTES": str(1048576 + 4096),
        "KAFKA_REPLICA_FETCH_MAX_BYTES": str(1048576 + 4096),
        "KAFKA_SSL_KEYSTORE_LOCATION": "/tmp/broker.p12",
        "KAFKA_SSL_KEYSTORE_PASSWORD": password,
        "KAFKA_SSL_KEY_PASSWORD": password,
        "KAFKA_SSL_KEYSTORE_TYPE": "PKCS12",
        "KAFKA_SASL_ENABLED_MECHANISMS": "PLAIN",
        "KAFKA_LISTENER_NAME_EXTERNAL_PLAIN_SASL_JAAS_CONFIG": (
            'org.apache.kafka.common.security.plain.PlainLoginModule required username="weave" '
            f'password="{sasl}" user_weave="{sasl}";'
        ),
    }
    env.write_text("\n".join(k + "=" + v for k, v in config.items()) + "\n")
    env.chmod(0o600)
    data = kafka_data_directory()
    storage = ("--mount", f"type=bind,source={data},target=/tmp/kafka-logs") if data is not None else ()
    identifier = await docker(
        "create", "--name", name, "-p", f"127.0.0.1:{port}:9094", "--env-file", str(env), *storage, image
    )
    store.chmod(0o644)
    await docker("cp", str(store), identifier + ":/tmp/broker.p12")
    await docker("start", identifier)
    try:
        for _ in range(90):
            ready = await docker(
                "exec",
                identifier,
                "bash",
                "-c",
                "(echo >/dev/tcp/127.0.0.1/9094) >/dev/null 2>&1 && echo ready || true",
            )
            if ready == "ready":
                break
            await asyncio.sleep(0.5)
        else:
            pytest.fail("Owned SASL TLS Kafka did not start")
        yield {"port": port, "cert": str(cert), "password": sasl, "container": identifier}
    finally:
        await docker("stop", "--time", "10", identifier)


def revision_for(port, *, host="broker.invalid"):
    from firefly_weave.contracts.connectors import ConnectionRevision

    return ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="tls",
        connector_version_id=uuid4(),
        connector="weave-kafka@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-kafka",
        config={
            "driver": "kafka",
            "cluster_id": "tls-owned",
            "bootstrap": [f"kafka://{host}:{port}"],
            "topics": ["tls-proof"],
            "security_protocol": "SASL_SSL",
            "username": "weave",
        },
        secretRef={"password": "tls-password"},
        allowed_destinations=(f"kafka://{host}:{port}",),
    )


async def test_real_sasl_tls_ack_wrong_ca_identity_and_max_value(tls_broker, caplog):
    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerClients

    revision = revision_for(tls_broker["port"])

    def clients_for(revision, ca):
        return BrokerClients(
            BrokerPolicy(
                enabled=True,
                routes=(
                    BrokerRoutePin(
                        connection_revision_id=revision.id,
                        advertised=revision.allowed_destinations[0],
                        addresses=("127.0.0.1",),
                        ca_file=ca,
                    ),
                ),
            )
        )

    clients = clients_for(revision, tls_broker["cert"])
    owner = await clients.acquire(revision)
    try:
        await asyncio.wait_for(owner.start_producer(revision, tls_broker["password"]), 15)
        topic, partition, offset = await asyncio.wait_for(owner.publish("tls-proof", b"x" * 1048576, str(uuid4())), 15)
        assert topic == "tls-proof" and partition >= 0 and offset >= 0
    finally:
        await clients.close()
    for hostile, ca in [(revision, None), (revision_for(tls_broker["port"], host="wrong.invalid"), tls_broker["cert"])]:
        clients = clients_for(hostile, ca)
        owner = await clients.acquire(hostile)
        try:
            with pytest.raises((__import__("aiokafka").errors.KafkaConnectionError, TimeoutError)):
                await asyncio.wait_for(owner.start_producer(hostile, tls_broker["password"]), 6)
        finally:
            await clients.close()
    assert tls_broker["password"] not in caplog.text


async def test_advertised_leader_and_coordinator_denied_before_network(tls_material):
    from aiokafka.protocol.admin import (
        ApiVersionResponse_v0,
        ApiVersionResponse_v1,
        SaslAuthenticateResponse_v0,
        SaslHandShakeResponse_v1,
    )
    from aiokafka.protocol.coordination import FindCoordinatorResponse_v0
    from aiokafka.protocol.metadata import MetadataResponse_v0

    from firefly_weave.connectors.broker import BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerOwner

    key, cert, _, _ = tls_material
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert, key)
    forbidden = {"connections": 0, "bytes": 0}

    async def trap(reader, writer):
        forbidden["connections"] += 1
        forbidden["bytes"] += len(await reader.read(4096))
        writer.close()
        await writer.wait_closed()

    denied_server = await asyncio.start_server(trap, "127.0.0.1", 0)
    denied_port = denied_server.sockets[0].getsockname()[1]
    tasks = set()

    async def broker(reader, writer):
        tasks.add(asyncio.current_task())
        try:
            while True:
                size = struct.unpack(">i", await reader.readexactly(4))[0]
                request = await reader.readexactly(size)
                api, api_version, correlation = struct.unpack(">hhi", request[:8])
                if api == 18:
                    versions = [(18, 0, 2), (3, 0, 0), (10, 0, 0), (17, 0, 1), (36, 0, 0)]
                    response = (
                        ApiVersionResponse_v0(0, versions)
                        if api_version == 0
                        else ApiVersionResponse_v1(0, versions, 0)
                    )
                elif api == 17:
                    response = SaslHandShakeResponse_v1(0, ["PLAIN"])
                elif api == 36:
                    response = SaslAuthenticateResponse_v0(0, "", b"")
                elif api == 10:
                    response = FindCoordinatorResponse_v0(0, 1, "forbidden.invalid", denied_port)
                else:
                    assert api == 3 and api_version == 0
                    response = MetadataResponse_v0(
                        [(0, "broker.invalid", port), (1, "forbidden.invalid", denied_port)],
                        [(0, "owned", [(0, 0, 1, [1], [1])])],
                    )
                raw = struct.pack(">i", correlation) + response.encode()
                writer.write(struct.pack(">i", len(raw)) + raw)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(broker, "127.0.0.1", 0, ssl=server_tls)
    port = server.sockets[0].getsockname()[1]
    try:
        for coordinator in (False, True):
            owner = BrokerOwner(
                (
                    BrokerRoutePin(
                        connection_revision_id=uuid4(),
                        advertised=f"kafka://broker.invalid:{port}",
                        addresses=("127.0.0.1",),
                        ca_file=str(cert),
                    ),
                ),
                5,
            )
            await owner.open()

            async def probe(coordinator=coordinator):
                from aiokafka.client import AIOKafkaClient, ConnectionGroup

                client = AIOKafkaClient(
                    bootstrap_servers=f"broker.invalid:{port}",
                    security_protocol="SASL_SSL",
                    ssl_context=ssl.create_default_context(cafile=str(cert)),
                    sasl_mechanism="PLAIN",
                    sasl_plain_username="owned",
                    sasl_plain_password="owned-credential-canary",
                )
                try:
                    await client.bootstrap()
                    if coordinator:
                        from aiokafka.protocol.coordination import FindCoordinatorRequest

                        reply = await client.send(0, FindCoordinatorRequest("owned-group", 0))
                        assert reply.coordinator_id == 1
                    await client.ready(
                        1, group=ConnectionGroup.COORDINATION if coordinator else ConnectionGroup.DEFAULT
                    )
                finally:
                    await client.close()

            try:
                from firefly_weave.contracts.connectors import ConnectorFailure

                with pytest.raises(ConnectorFailure):
                    await asyncio.wait_for(owner.call(probe), 8)
                assert owner.loop.denied.is_set()
            finally:
                await owner.close()
        assert forbidden == {"connections": 0, "bytes": 0}
    finally:
        server.close()
        denied_server.close()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()
        await denied_server.wait_closed()
