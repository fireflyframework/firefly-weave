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

"""Actual native Kafka release pins, worker authority, and built artifact execution."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from native_image_support import write_image_proof
from sqlalchemy import make_url
from test_kafka import ack_loss_proxy as ack_loss_proxy
from test_kafka import docker
from test_kafka import kafka_backend as kafka_backend

from firefly_weave.access.audit import AuditContext

pytestmark = pytest.mark.integration


@pytest.fixture
async def kafka_native(services, access_db, provisioned, kafka_backend, request):
    from firefly_weave.access.models import Grant
    from firefly_weave.connections.service import ConnectionService
    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka import KafkaConnector
    from firefly_weave.connectors.manifest import KAFKA_DESCRIPTOR
    from firefly_weave.contracts.catalog import ActivationRequest
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.contracts.workers import InstanceRequest, ReleaseRequest
    from firefly_weave.definitions.service import DefinitionService
    from firefly_weave.workers.service import WorkerService

    ambiguous = getattr(request, "param", None) == "ambiguous"
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
    graph = services(access_db[0])
    definitions = graph.resolve(DefinitionService)
    # Configure infrastructure before resolving its native consumer service.
    graph.register_instance(BrokerPolicy, BrokerPolicy(enabled=True))
    definitions.registry.register_descriptor(KAFKA_DESCRIPTOR, graph.resolve(KafkaConnector))
    connector = await definitions.publish(
        actor, scope, "Connector", json.dumps(KAFKA_DESCRIPTOR.manifest.value), "json", "broker", context=AuditContext()
    )
    workers = graph.resolve(WorkerService)
    image = os.environ.get("WEAVE_D2_IMAGE_ID", "sha256:" + "d" * 64)
    release = await workers.register_release(
        actor,
        scope,
        ReleaseRequest(
            image_digest=image,
            capabilities=list(KAFKA_DESCRIPTOR.capabilities),
            connector_bindings=list(KAFKA_DESCRIPTOR.bindings),
        ),
        context=AuditContext(),
    )
    topic = "weave-d2-native-" + uuid4().hex
    endpoint = f"kafka://127.0.0.1:{kafka_backend['port']}"
    revision = await graph.resolve(ConnectionService).create_revision(
        actor,
        scope,
        ConnectionRequest(
            name="native-broker",
            connector_version_id=connector.id,
            config={
                "driver": "kafka",
                "cluster_id": "owned-d2",
                "topics": [topic],
                "bootstrap": [endpoint],
                "security_protocol": "PLAINTEXT",
            },
            allowed_destinations=(endpoint,),
        ),
        context=AuditContext(),
    )
    policy = BrokerPolicy(
        enabled=True,
        routes=(
            BrokerRoutePin(
                connection_revision_id=revision.id,
                advertised=endpoint,
                addresses=("::1" if ambiguous else "127.0.0.1",),
                plaintext=True,
            ),
        ),
    )
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "publish", "version": "1.0.0"},
        "spec": {
            "implementation": {
                "kind": "connector",
                "uses": "weave-kafka@1.0.0",
                "action": "publish",
                "config": {"topic": topic},
            },
            "connection": {"connector": "weave-kafka@1.0.0"},
            "sideEffect": "non_idempotent",
            "timeoutSeconds": 10 if ambiguous else 30,
            "inputSchema": KAFKA_DESCRIPTOR.manifest.value["spec"]["actions"]["publish"]["inputSchema"],
            "outputSchema": KAFKA_DESCRIPTOR.manifest.value["spec"]["actions"]["publish"]["outputSchema"],
        },
    }
    await definitions.publish(actor, scope, "Action", json.dumps(action), "json", "action", context=AuditContext())
    flow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "broker-flow", "version": "1.0.0"},
        "spec": {
            "connections": {"broker": {"connector": "weave-kafka@1.0.0"}},
            "inputSchema": {},
            "outputSchema": {},
            "steps": [
                {
                    "id": "send",
                    "kind": "action",
                    "uses": "publish@1.0.0",
                    "connection": "broker",
                    "with": {"ref": "/input"},
                }
            ],
            "output": {"ref": "/steps/send/output"},
        },
    }
    version = await definitions.publish(
        actor, scope, "Workflow", json.dumps(flow), "json", "flow", context=AuditContext()
    )
    activation = await definitions.activate(
        actor,
        scope,
        ActivationRequest(
            version_id=version.id,
            artifact_digest=version.digest,
            scope=scope,
            connection_revision_ids={"broker": revision.id},
            connector_release_ids={connector.id: release.id},
        ),
        "active",
        context=AuditContext(),
    )
    instance = await workers.register_instance(
        actor,
        scope,
        InstanceRequest(release_id=release.id, task_types=["weave-connector-kafka-publish@1.0.0"], capacity=1),
        context=AuditContext(),
    )
    configured = services(access_db[0])
    configured.register_instance(BrokerPolicy, policy)
    configured.resolve(DefinitionService).registry.register_descriptor(
        KAFKA_DESCRIPTOR, configured.resolve(KafkaConnector)
    )
    return configured, actor, scope, activation, instance, policy, topic


async def test_native_lease_publish(kafka_native, kafka_backend):
    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService

    graph, actor, scope, activation, instance, _, topic = kafka_native
    runtime = graph.resolve(RuntimeService)
    payload = {"v": "x" * (1048576 - 8)}
    run = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input=payload), str(uuid4()), context=AuditContext()
    )
    executor = graph.resolve(ConnectorExecutionService)
    transport = ServiceTransport(executor, scope, actor.id, instance.id)
    lease = (await transport.claim(1))[0]
    result = await executor.execute(scope, actor.id, lease)
    assert result["topic"] == topic
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=f"127.0.0.1:{kafka_backend['port']}",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    try:
        await consumer.start()
        record = await asyncio.wait_for(consumer.getone(), 10)
        assert len(record.value) == 1048576
        assert json.loads(record.value) == payload
    finally:
        await consumer.stop()
    await transport.complete(lease.proof, uuid4(), result)
    assert (await runtime.read(actor, scope, run.id, context=AuditContext())).state.status == "succeeded"


async def test_built_native_kafka_executor(kafka_native, access_db, provisioned, tmp_path, scheduler_url):
    from firefly_weave.access.models import Grant
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService

    graph, actor, scope, activation, instance, policy, topic = kafka_native
    image = os.environ.get("WEAVE_D2_IMAGE_ID")
    if not image:
        pytest.fail("Native Kafka artifact acceptance requires WEAVE_D2_IMAGE_ID")
    admin = await access_db[2].load_principal(provisioned[0].id)
    executor_id = await access_db[2].create_principal(admin, "worker")
    await access_db[2].grant(
        admin,
        executor_id,
        Grant(role="worker", scope=scope, resources=(str(instance.release_id), *instance.task_types)),
    )
    config = [
        {
            "scope": scope.model_dump(mode="json"),
            "principal_id": str(executor_id),
            "release_id": str(instance.release_id),
            "task_types": instance.task_types,
            "capacity": 1,
        }
    ]
    env = tmp_path / "native.env"
    env.write_text(
        "\n".join(
            [
                "WEAVE_DATABASE_URL=" + access_db[3].set(host="127.0.0.1").render_as_string(hide_password=False),
                "WEAVE_SCHEDULER_DATABASE_URL="
                + make_url(scheduler_url).set(host="127.0.0.1").render_as_string(hide_password=False),
                "WEAVE_SCHEDULER_ENABLED=false",
                "WEAVE_NATIVE_IMAGE_DIGEST=" + image,
                "WEAVE_NATIVE_EXECUTORS=" + json.dumps(config),
                "WEAVE_BROKER_POLICY=" + policy.model_dump_json(),
            ]
        )
        + "\n"
    )
    env.chmod(0o600)
    http_port = 31000 + int(uuid4().hex[:4], 16) % 20000
    identifier = await docker(
        "run",
        "-d",
        "--network",
        "host",
        "--name",
        "weave-d2-native-" + uuid4().hex[:12],
        "--env-file",
        str(env),
        image,
        "uvicorn",
        "firefly_weave.main:create_application",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(http_port),
    )
    try:
        for _ in range(80):
            result = await docker(
                "exec",
                identifier,
                "python",
                "-c",
                "import urllib.request\ntry:\n print(urllib.request.urlopen("
                f"'http://127.0.0.1:{http_port}/health/ready').status)\n"
                "except Exception:\n print('starting')",
            )
            if result == "200":
                break
            await asyncio.sleep(0.2)
        else:
            pytest.fail("Built Kafka executor did not become ready")
        runtime = graph.resolve(RuntimeService)
        run = await runtime.start(
            actor,
            scope,
            StartRunRequest(activation_id=activation.id, input={"payload": 3}),
            str(uuid4()),
            context=AuditContext(),
        )
        async with asyncio.timeout(30):
            while True:
                view = await runtime.read(actor, scope, run.id, context=AuditContext())
                if view.state.status != "waiting":
                    break
                await asyncio.sleep(0.1)
        assert view.state.status == "succeeded" and view.state.output["topic"] == topic
        assert await docker("inspect", "--format", "{{.Image}}", identifier) == image
        assert await docker("inspect", "--format", "{{json .Mounts}}", identifier) == "[]"
        target = os.environ.get("WEAVE_IMAGE_PROOF_PATH")
        assert target, "Unique image proof path required"
        write_image_proof(
            Path(target),
            "kafka",
            {
                "image_id": image,
                "container": identifier,
                "run_id": str(run.id),
                "output": view.state.output,
                "source_mount": False,
                "pins": [p.model_dump(mode="json") for p in activation.connector_execution_pins],
            },
        )
    finally:
        await docker("stop", "--time", "15", identifier)


@pytest.mark.parametrize("kafka_native", ["ambiguous"], indirect=True)
async def test_native_lost_ack_creates_durable_incident_without_resend(kafka_native, ack_loss_proxy, access_db):
    from sqlalchemy import text

    from firefly_weave.connectors.execution import ConnectorExecutionService, ServiceTransport
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.service import RuntimeService
    from firefly_weave.sdk.worker import Worker

    graph, actor, scope, activation, instance, _, topic = kafka_native
    runtime = graph.resolve(RuntimeService)
    run = await runtime.start(
        actor, scope, StartRunRequest(activation_id=activation.id, input={"v": 3}), str(uuid4()), context=AuditContext()
    )
    executor = graph.resolve(ConnectorExecutionService)
    transport = ServiceTransport(executor, scope, actor.id, instance.id)
    lease = (await transport.claim(1))[0]

    async def execute(lease):
        return await executor.execute(scope, actor.id, lease)

    await Worker(transport, {lease.capability: execute}, 1)._execute(lease)
    assert ack_loss_proxy.is_set()
    async with access_db[1]() as session:
        error = await session.scalar(text("SELECT data->'output' FROM run_events WHERE type='task_failed'"))
        assert error["outcome"] == "unknown"
        assert await session.scalar(text("SELECT count(*) FROM incidents")) == 1
    assert (await runtime.read(actor, scope, run.id, context=AuditContext())).state.status == "suspended"
    assert await transport.claim(1) == []


@pytest.mark.parametrize("kafka_native", ["ambiguous"], indirect=True)
async def test_native_unknown_survives_total_slow_cleanup_budget(kafka_native, ack_loss_proxy, access_db, monkeypatch):
    import threading

    from firefly_weave.connectors.kafka_transport import BrokerOwner

    original = BrokerOwner.close
    owners = []
    timers = []

    async def slow_close(owner):
        if not owner.closed and owner.loop is not None:
            entered, resume = threading.Event(), threading.Event()

            def stall():
                entered.set()
                resume.wait(owner.cleanup_seconds + 2)

            owner.loop.call_soon_threadsafe(stall)
            assert await asyncio.to_thread(entered.wait, 1)
            timer = threading.Timer(owner.cleanup_seconds + 0.4, resume.set)
            timers.append((timer, resume))
            owners.append(owner)
            timer.start()
        await original(owner)

    monkeypatch.setattr(BrokerOwner, "close", slow_close)
    try:
        # This runs actual broker acceptance/ACK loss and the native worker deadline,
        # then verifies unknown + one durable incident and no re-dispatch.
        await test_native_lost_ack_creates_durable_incident_without_resend(kafka_native, ack_loss_proxy, access_db)
        assert owners
    finally:
        for timer, resume in timers:
            resume.set()
            timer.cancel()
        for owner in owners:
            await asyncio.to_thread(owner.thread.join, 2)
            assert not owner.thread.is_alive()
