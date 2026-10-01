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

"""Broker policy boundaries and contiguous accepted-prefix commit ownership."""

import importlib.util
from uuid import uuid4

import pytest


def test_out_of_order_receipts_never_skip_pending_record():
    assert importlib.util.find_spec("firefly_weave.connectors.kafka_transport") is not None, (
        "D2 owned broker transport absent"
    )
    from firefly_weave.connectors.kafka_transport import PartitionFrontier

    frontier = PartitionFrontier()
    frontier.offer(4)
    frontier.offer(7)
    assert frontier.accept(7) is None
    assert frontier.accept(4) == 8
    frontier.offer(9)
    frontier.offer(10)
    assert frontier.accept(10) is None


async def test_unknown_advertised_endpoint_has_no_network_or_dns():
    from firefly_weave.connectors.broker import BrokerRoutePin

    assert importlib.util.find_spec("firefly_weave.connectors.kafka_transport") is not None, (
        "D2 owned broker transport absent"
    )
    from firefly_weave.connectors.kafka_transport import BrokerEventLoop

    loop = BrokerEventLoop(
        (
            BrokerRoutePin(
                connection_revision_id=uuid4(), advertised="kafka://allowed.invalid:9093", addresses=("127.0.0.1",)
            ),
        )
    )
    try:
        with pytest.raises(PermissionError):
            await loop.create_connection(lambda: None, "forbidden.invalid", 9093)
        assert loop.denied.is_set()
        assert loop.delegations == 0
    finally:
        loop.close()


async def test_late_connection_is_aborted_and_not_exposed(monkeypatch):
    import asyncio
    import threading

    from firefly_weave.connectors.broker import BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerOwner

    entered, release = threading.Event(), threading.Event()

    class Transport:
        aborted = False

        def abort(self):
            self.aborted = True

        def is_closing(self):
            return self.aborted

    transport = Transport()

    async def late(self, protocol_factory, host, port, **kwargs):
        entered.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue
        return transport, None

    monkeypatch.setattr(asyncio.SelectorEventLoop, "create_connection", late)
    owner = BrokerOwner(
        (
            BrokerRoutePin(
                connection_revision_id=uuid4(),
                advertised="kafka://broker.invalid:9092",
                addresses=("127.0.0.1",),
                plaintext=True,
            ),
        ),
        1,
    )
    await owner.open()

    async def dial():
        return await owner.loop.create_connection(lambda: None, "broker.invalid", 9092)

    task = asyncio.create_task(owner.call(dial))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        owner.loop.revoked.set()
        owner.loop.call_soon_threadsafe(owner.loop.abort)
        release.set()
        with pytest.raises((PermissionError, asyncio.CancelledError)):
            await task
        assert transport.aborted
    finally:
        release.set()
        await owner.close()
    assert not owner.thread.is_alive()


async def test_owned_logging_debug_exception_context_and_overlapping_lifetimes(caplog):
    import asyncio
    import logging

    from firefly_weave.connectors.kafka_transport import BrokerOwner

    caplog.set_level(logging.DEBUG)
    first, second = BrokerOwner((), 1), BrokerOwner((), 1)
    await first.open()
    await second.open()

    async def unsafe():
        try:
            raise ValueError("secret-canary-exception")
        except ValueError:
            logging.getLogger("aiokafka").error("secret-canary-root")
            logging.getLogger("aiokafka.conn").exception(
                "secret-canary-message %s", "secret-canary-args", stack_info=True
            )
        asyncio.get_running_loop().call_exception_handler(
            {"message": "secret-canary-loop", "exception": ValueError("secret-canary-context")}
        )

    try:
        await first.call(unsafe)
        await second.call(unsafe)
        await first.close()
        await second.call(unsafe)
        logging.getLogger("aiokafka.conn").warning("unrelated-thread-visible")
        assert "secret-canary" not in caplog.text
        assert "unrelated-thread-visible" in caplog.text
        assert "BROKER_DRIVER_EVENT" in caplog.text and "BROKER_LOOP_ERROR" in caplog.text
    finally:
        await first.close()
        await second.close()


async def test_startup_failure_and_cancelled_queued_commands_are_retired(monkeypatch, caplog):
    import asyncio
    import logging
    import threading

    from firefly_weave.connectors import kafka_transport

    class BrokenLoop:
        def __init__(self, routes):
            logging.getLogger("aiokafka").error("startup-secret-canary")
            raise ValueError("constructor-secret-canary")

    original = kafka_transport.BrokerEventLoop
    monkeypatch.setattr(kafka_transport, "BrokerEventLoop", BrokenLoop)
    broken = kafka_transport.BrokerOwner((), 1)
    with pytest.raises(RuntimeError, match="Broker owner startup failed"):
        await broken.open()
    await broken.close()
    broken.thread.join(1)
    assert not broken.thread.is_alive() and "secret-canary" not in caplog.text
    monkeypatch.setattr(kafka_transport, "BrokerEventLoop", original)
    owner = kafka_transport.BrokerOwner((), 1)
    await owner.open()
    entered, cancelled = threading.Event(), threading.Event()

    async def queued():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(owner.call(queued))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await owner.close()
    assert cancelled.is_set() and not owner.thread.is_alive()


async def test_publish_one_byte_over_limit_rejects_before_provider_or_client():
    from datetime import UTC, datetime, timedelta

    from firefly_weave.connectors.broker import BrokerPolicy
    from firefly_weave.connectors.kafka import KafkaConnector
    from firefly_weave.connectors.kafka_transport import BrokerClients
    from firefly_weave.contracts.connectors import (
        ActionContext,
        ConnectionRevision,
        ConnectorFailure,
        ConnectorInvocation,
    )

    revision = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="size",
        connector_version_id=uuid4(),
        connector="weave-kafka@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-kafka",
        config={
            "driver": "kafka",
            "cluster_id": "owned",
            "topics": ["topic"],
            "bootstrap": ["kafka://127.0.0.1:9092"],
            "security_protocol": "PLAINTEXT",
        },
        allowed_destinations=("kafka://127.0.0.1:9092",),
    )

    async def forbidden(*args):
        raise AssertionError("Oversized value reached authority/provider")

    clients = BrokerClients(BrokerPolicy(enabled=True))
    context = ActionContext(
        "size",
        datetime.now(UTC) + timedelta(seconds=30),
        forbidden,
        ConnectorInvocation(revision, {"topic": "topic"}, "publish", {"type": "object"}, {}, 1048576, 1048576),
        forbidden,
    )
    with pytest.raises(ConnectorFailure) as error:
        await KafkaConnector(clients).execute({"v": "x" * (1048576 - 7)}, context)
    assert (error.value.code, error.value.outcome) == ("BROKER_SIZE", "not_started")
    assert not clients.owners


async def test_pending_real_tls_handshake_shutdown_is_bounded():
    import asyncio
    import ssl

    from firefly_weave.connectors.broker import BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerOwner

    entered, disconnected = asyncio.Event(), asyncio.Event()

    async def stalled(reader, writer):
        entered.set()
        try:
            while await reader.read(4096):
                pass
        finally:
            disconnected.set()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(stalled, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    owner = BrokerOwner(
        (
            BrokerRoutePin(
                connection_revision_id=uuid4(), advertised=f"kafka://broker.invalid:{port}", addresses=("127.0.0.1",)
            ),
        ),
        1,
    )
    await owner.open()

    async def dial():
        return await owner.loop.create_connection(
            asyncio.Protocol, "broker.invalid", port, ssl=ssl.create_default_context()
        )

    command = asyncio.create_task(owner.call(dial))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with asyncio.timeout(3):
            await owner.close()
            await disconnected.wait()
        assert not owner.thread.is_alive()
        assert command.done()
        await asyncio.gather(command, return_exceptions=True)
    finally:
        await owner.close()
        server.close()
        await server.wait_closed()


async def test_process_owner_cap_across_apps_and_connection_pin_isolation():
    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerClients
    from firefly_weave.contracts.connectors import ConnectionRevision, ConnectorFailure

    revision = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="capacity",
        connector_version_id=uuid4(),
        connector="weave-kafka@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-kafka",
        config={},
        allowed_destinations=("kafka://127.0.0.1:9092",),
    )
    pin = BrokerRoutePin(
        connection_revision_id=revision.id,
        advertised=revision.allowed_destinations[0],
        addresses=("127.0.0.1",),
        plaintext=True,
    )
    first, second = [BrokerClients(BrokerPolicy(enabled=True, routes=(pin,))) for _ in range(2)]
    try:
        for _ in range(4):
            await first.acquire(revision)
            await second.acquire(revision)
        with pytest.raises(ConnectorFailure) as capacity:
            await second.acquire(revision)
        assert capacity.value.code == "BROKER_PROCESS_CAPACITY"
        await first.close()
        with pytest.raises(ConnectorFailure) as isolation:
            await second.acquire(revision.model_copy(update={"id": uuid4()}))
        assert isolation.value.code == "BROKER_EGRESS"
        await second.acquire(revision)
    finally:
        await first.close()
        await second.close()


async def test_publish_only_minimum_and_unsupported_profile_fail_clearly(monkeypatch):
    import sys

    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerClients, require_driver
    from firefly_weave.settings import Settings
    from firefly_weave.triggers.kafka_loop import KafkaLoop

    policy = BrokerPolicy(enabled=True, max_clients=1)
    loop = KafkaLoop(
        Settings(
            database_url="postgresql+asyncpg://unused:unused@127.0.0.1:65432/unused",
            broker=policy,
            kafka_consumer_enabled=True,
        ),
        None,
        None,
        BrokerClients(policy),
    )
    with pytest.raises(RuntimeError, match="publish-only"):
        await loop.open()
    publish_only = KafkaLoop(
        Settings(database_url="postgresql+asyncpg://unused:unused@127.0.0.1:65432/unused", broker=policy),
        None,
        None,
        BrokerClients(policy),
    )
    await publish_only.open()
    assert publish_only.task is None
    with pytest.raises(ValueError, match="loopback"):
        BrokerRoutePin(
            connection_revision_id=uuid4(),
            advertised="kafka://external.invalid:9092",
            addresses=("192.0.2.10",),
            plaintext=True,
        )
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="validated"):
        require_driver()


@pytest.fixture
def lifetime_clients():
    from firefly_weave.connectors.broker import BrokerPolicy, BrokerRoutePin
    from firefly_weave.connectors.kafka_transport import BrokerClients
    from firefly_weave.contracts.connectors import ConnectionRevision

    revision = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="lifetime",
        connector_version_id=uuid4(),
        connector="weave-kafka@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-kafka",
        config={},
        allowed_destinations=("kafka://127.0.0.1:9092",),
    )
    pin = BrokerRoutePin(
        connection_revision_id=revision.id,
        advertised=revision.allowed_destinations[0],
        addresses=("127.0.0.1",),
        plaintext=True,
    )
    return BrokerClients(BrokerPolicy(enabled=True, routes=(pin,), max_clients=1, cleanup_seconds=0.2)), revision


@pytest.mark.parametrize("cancel", [False, True])
async def test_dead_delayed_startup_reclaimed_without_app_shutdown(lifetime_clients, monkeypatch, cancel):
    import asyncio
    import threading

    from firefly_weave.connectors import kafka_transport
    from firefly_weave.contracts.connectors import ConnectorFailure

    clients, revision = lifetime_clients
    entered, resume = threading.Event(), threading.Event()
    original = kafka_transport.BrokerEventLoop

    def delayed(routes):
        entered.set()
        resume.wait(5)
        return original(routes)

    monkeypatch.setattr(kafka_transport, "BrokerEventLoop", delayed)
    starting = asyncio.create_task(clients.acquire(revision))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        if cancel:
            starting.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await starting
        owner = next(iter(clients.owners))
        assert owner.thread.is_alive()
        with pytest.raises(ConnectorFailure):
            await clients.acquire(revision)
        resume.set()
        await asyncio.to_thread(owner.thread.join, 2)
        assert not owner.thread.is_alive()
        monkeypatch.setattr(kafka_transport, "BrokerEventLoop", original)
        replacement = await clients.acquire(revision)
        assert owner not in clients.owners
        assert owner not in kafka_transport._process_owners
        await clients.release(replacement)
    finally:
        resume.set()
        await clients.close()


async def test_cleanup_exception_reclaims_dead_owner(lifetime_clients, monkeypatch):
    from firefly_weave.connectors import kafka_transport

    clients, revision = lifetime_clients
    owner = await clients.acquire(revision)
    original = owner.close

    async def failed():
        await original()
        raise RuntimeError("controlled cleanup failure")

    monkeypatch.setattr(owner, "close", failed)
    try:
        with pytest.raises(RuntimeError, match="controlled"):
            await clients.release(owner)
        replacement = await clients.acquire(revision)
        assert owner not in clients.owners and owner not in kafka_transport._process_owners
        await clients.release(replacement)
    finally:
        monkeypatch.setattr(owner, "close", original)
        await clients.close()


async def test_cleanup_total_deadline_disposes_queued_coroutines(lifetime_clients, recwarn):
    import asyncio
    import gc
    import threading
    import time

    from firefly_weave.contracts.connectors import ConnectorFailure

    clients, revision = lifetime_clients
    owner = await clients.acquire(revision)
    entered, resume = threading.Event(), threading.Event()

    def stall():
        entered.set()
        resume.wait(3)

    owner.loop.call_soon_threadsafe(stall)
    assert await asyncio.to_thread(entered.wait, 1)
    timer = threading.Timer(0.4, resume.set)
    timer.start()

    async def queued():
        return "queued"

    commands = [asyncio.create_task(owner.call(queued)) for _ in range(3)]
    await asyncio.sleep(0)
    started = time.monotonic()
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            await clients.release(owner)
        assert time.monotonic() - started < 0.3
        assert owner.thread.is_alive()
        with pytest.raises(ConnectorFailure):
            await clients.acquire(revision)
    finally:
        resume.set()
        timer.cancel()
        await asyncio.to_thread(owner.thread.join, 2)
        for task in commands:
            task.cancel()
        await asyncio.gather(*commands, return_exceptions=True)
        gc.collect()
    assert not [w for w in recwarn if issubclass(w.category, RuntimeWarning)]
    replacement = await clients.acquire(revision)
    await clients.release(replacement)
    await clients.close()


async def test_failed_thread_start_reclaims_closed_reservation(lifetime_clients, monkeypatch):
    import threading

    from firefly_weave.connectors import kafka_transport

    clients, revision = lifetime_clients
    failed = []

    def fail_start(thread):
        owner = next(iter(clients.owners))
        assert owner.thread is thread and thread.ident is None and not owner.closed
        clients.reap()
        assert owner in clients.owners and owner in kafka_transport._process_owners
        failed.append(owner)
        raise RuntimeError("controlled thread start failure")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError, match="controlled thread start failure"):
            await clients.acquire(revision)
    owner = failed[0]
    assert owner.closed and owner.thread.ident is None and not owner.thread.is_alive()
    assert owner not in clients.owners and owner not in kafka_transport._process_owners
    replacement = await clients.acquire(revision)
    try:
        assert replacement.thread.is_alive()
    finally:
        await clients.release(replacement)
