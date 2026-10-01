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

"""Owned public asyncio broker dial gate and cross-loop immutable command facade."""

import asyncio
import concurrent.futures
import ipaddress
import json
import logging
import socket
import ssl
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from importlib.metadata import version
from typing import Any, TypeVar

from pyfly.container import service

from firefly_weave.connectors.broker import BrokerConnectionConfig, BrokerPolicy, BrokerRoutePin, destination
from firefly_weave.contracts.broker import BrokerRecord
from firefly_weave.contracts.connectors import ConnectionRevision, ConnectorFailure

T = TypeVar("T")


class PartitionFrontier:
    def __init__(self) -> None:
        self.pending: deque[int] = deque()
        self.accepted: set[int] = set()
        self.last = -1

    def offer(self, offset: int) -> None:
        if offset <= self.last or len(self.pending) >= 32:
            raise ValueError("Invalid broker delivery sequence")
        self.pending.append(offset)
        self.last = offset

    def accept(self, offset: int) -> int | None:
        if offset not in self.pending:
            raise ValueError("Unowned broker delivery")
        self.accepted.add(offset)
        result = None
        while self.pending and self.pending[0] in self.accepted:
            result = self.pending.popleft() + 1
            self.accepted.remove(result - 1)
        return result


class BrokerEventLoop(asyncio.SelectorEventLoop):
    def __init__(self, routes: tuple[BrokerRoutePin, ...]) -> None:
        super().__init__()
        self.set_exception_handler(lambda loop, context: logging.getLogger(__name__).error("BROKER_LOOP_ERROR"))
        self.routes = {destination(route.advertised): route for route in routes}
        self.revoked = threading.Event()
        self.denied = threading.Event()
        self.transports: set[asyncio.Transport] = set()
        self.dials: set[asyncio.Task[Any]] = set()
        self.delegations = 0

    def abort(self) -> None:
        self.revoked.set()
        for task in tuple(self.dials):
            task.cancel()
        for transport in tuple(self.transports):
            transport.abort()
        self.transports.clear()

    def deny(self) -> None:
        self.denied.set()
        self.abort()
        raise PermissionError("Broker route denied")

    async def create_connection(
        self,
        protocol_factory: Any,
        host: Any = None,
        port: Any = None,
        *,
        ssl: Any = None,
        family: int = 0,
        proto: int = 0,
        flags: int = 0,
        sock: Any = None,
        local_addr: Any = None,
        server_hostname: str | None = None,
        ssl_handshake_timeout: float | None = None,
        ssl_shutdown_timeout: float | None = None,
        happy_eyeballs_delay: float | None = None,
        interleave: int | None = None,
        all_errors: bool = False,
    ) -> Any:
        route = self.routes.get((host, port)) if isinstance(host, str) and type(port) is int else None
        if (
            self.revoked.is_set()
            or route is None
            or sock is not None
            or local_addr is not None
            or family
            or proto
            or flags
            or server_hostname is not None
            or ssl_handshake_timeout is not None
            or ssl_shutdown_timeout is not None
            or happy_eyeballs_delay is not None
            or interleave is not None
            or all_errors
        ):
            self.deny()
        assert route is not None
        if ssl is None:
            if not route.plaintext:
                self.deny()
        elif (
            not isinstance(ssl, globals()["ssl"].SSLContext)
            or not ssl.check_hostname
            or ssl.verify_mode != globals()["ssl"].CERT_REQUIRED
        ):
            self.deny()
        task = asyncio.current_task()
        assert task is not None
        self.dials.add(task)
        try:
            async with asyncio.timeout(5):
                for address in route.addresses:
                    try:
                        if self.revoked.is_set():
                            self.deny()
                        self.delegations += 1
                        transport, protocol = await super().create_connection(
                            protocol_factory,
                            address,
                            port,
                            ssl=ssl,
                            family=socket.AF_INET if ipaddress.ip_address(address).version == 4 else socket.AF_INET6,
                            proto=socket.IPPROTO_TCP,
                            flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                            server_hostname=host if ssl is not None else None,
                            ssl_handshake_timeout=4 if ssl is not None else None,
                            ssl_shutdown_timeout=0.5 if ssl is not None else None,
                        )
                    except OSError:
                        if address == route.addresses[-1]:
                            raise
                        continue
                    if self.revoked.is_set():
                        transport.abort()
                        raise PermissionError("Broker authority retired")
                    self.transports = {t for t in self.transports if not t.is_closing()}
                    if len(self.transports) >= 64:
                        transport.abort()
                        self.deny()
                    self.transports.add(transport)
                    return transport, protocol
        finally:
            self.dials.discard(task)
        raise PermissionError("Broker route unavailable")

    async def getaddrinfo(self, *args: Any, **kwargs: Any) -> Any:
        # Numeric delegation is handled without resolution by the standard loop.
        self.deny()


def require_driver() -> None:
    if sys.platform not in {"darwin", "linux"} or sys.version_info[:2] not in {(3, 12), (3, 13)}:
        raise RuntimeError("Kafka requires validated CPython3.12/3.13 on Linux or macOS")
    if version("aiokafka") != "0.14.0":
        raise RuntimeError("Kafka requires firefly-weave[kafka] with aiokafka0.14.0")
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore[import-untyped] # noqa: F401


class BrokerOwner:
    def __init__(
        self,
        routes: tuple[BrokerRoutePin, ...],
        cleanup_seconds: float,
        credential_versions: tuple[tuple[str, str | None], ...] = (),
    ) -> None:
        self.routes = routes
        self.cleanup_seconds = cleanup_seconds
        self.credential_versions = credential_versions
        self.loop: BrokerEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self.closed = False
        self.client: Any = None
        self.consumer = False
        self.generations: dict[tuple[str, int], int] = {}
        self.epoch = 0
        self.frontiers: dict[tuple[str, int], PartitionFrontier] = {}
        self.admitting: set[tuple[str, int]] = set()
        self.commands = 0
        self.cleanup_deadline: float | None = None
        self.bridge_lock = threading.Lock()
        self.bridges: set[concurrent.futures.Future[Any]] = set()

    async def open(self) -> None:
        require_driver()

        def run() -> None:
            from firefly_weave.connectors.broker_logging import register, unregister

            register()
            loop = None
            try:
                loop = BrokerEventLoop(self.routes)
                self.loop = loop
                if self.ready.cancelled() or self.closed:
                    return
                self.ready.set_result(None)
                loop.run_forever()
            except BaseException:
                if not self.ready.done():
                    self.ready.set_exception(RuntimeError("Broker owner startup failed"))
            finally:
                try:
                    with self.bridge_lock:
                        pending_bridges = tuple(self.bridges)
                    for future in pending_bridges:
                        future.cancel()
                    if loop is not None:
                        loop.abort()
                        tasks = asyncio.all_tasks(loop)
                        for task in tasks:
                            task.cancel()
                        if tasks:
                            # Deliver cancellation even when the caller's budget expired.
                            # A noncooperative task may keep this owner alive and counted.
                            remaining = max(0.0, (self.cleanup_deadline or time.monotonic()) - time.monotonic())
                            loop.run_until_complete(asyncio.wait(tasks, timeout=remaining))
                        loop.close()
                finally:
                    unregister()

        self.thread = threading.Thread(target=run, name="weave-kafka-owner", daemon=True)
        self.thread.start()
        await asyncio.wait_for(asyncio.wrap_future(self.ready), 1)

    async def call(self, operation: Callable[[], Awaitable[T]], *, closing: bool = False) -> T:
        if self.loop is None or (self.closed and not closing) or self.commands >= 32:
            raise ConnectorFailure("BROKER_CAPACITY", "not_started")
        if not closing and (self.loop.denied.is_set() or self.loop.revoked.is_set()):
            raise ConnectorFailure("BROKER_AUTHORITY", "unknown")
        self.commands += 1

        async def invoke() -> T:
            assert self.loop is not None
            if not closing and self.loop.revoked.is_set():
                raise ConnectorFailure("BROKER_AUTHORITY", "unknown")
            return await operation()

        loop = self.loop
        future: concurrent.futures.Future[T] = concurrent.futures.Future()
        task: asyncio.Task[T] | None = None

        def completed(done: asyncio.Task[T]) -> None:
            try:
                result = done.result()
            except asyncio.CancelledError:
                future.cancel()
            except BaseException as error:
                with suppress(concurrent.futures.InvalidStateError):
                    future.set_exception(error)
            else:
                with suppress(concurrent.futures.InvalidStateError):
                    future.set_result(result)

        def launch() -> None:
            nonlocal task
            if future.cancelled():
                return
            # Construct the coroutine only when its owner can schedule it. A queued
            # callback discarded during loop closure therefore owns no coroutine.
            task = loop.create_task(invoke())
            task.add_done_callback(completed)

        def cancel(done: concurrent.futures.Future[T]) -> None:
            if done.cancelled():

                def cancel_task() -> None:
                    if task is not None:
                        task.cancel()

                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(cancel_task)

        future.add_done_callback(cancel)
        with self.bridge_lock:
            self.bridges.add(future)
        try:
            loop.call_soon_threadsafe(launch)
            result = await asyncio.wrap_future(future)
            if not closing and loop.denied.is_set():
                raise ConnectorFailure("BROKER_EGRESS", "unknown")
            return result
        finally:
            future.cancel()
            with self.bridge_lock:
                self.bridges.discard(future)
            self.commands -= 1

    def options(self, revision: ConnectionRevision, password: str | None) -> dict[str, Any]:
        config = BrokerConnectionConfig.model_validate_json(json.dumps(revision.config))
        routes = {destination(route.advertised): route for route in self.routes}
        if not all(destination(v) in routes for v in config.bootstrap):
            raise ConnectorFailure("BROKER_EGRESS", "not_started")
        options: dict[str, Any] = {
            "bootstrap_servers": [
                f"{'[' + h + ']' if ':' in h else h}:{p}" for h, p in map(destination, config.bootstrap)
            ],
            "security_protocol": config.security_protocol,
            "request_timeout_ms": 5000,
            "client_id": "firefly-weave",
            "connections_max_idle_ms": 10000,
        }
        if config.security_protocol == "SASL_SSL":
            ca_files = {route.ca_file for route in self.routes}
            if len(ca_files) != 1 or not password:
                raise ConnectorFailure("BROKER_TLS", "not_started")
            options.update(
                ssl_context=ssl.create_default_context(cafile=next(iter(ca_files))),
                sasl_mechanism=config.sasl_mechanism,
                sasl_plain_username=config.username,
                sasl_plain_password=password,
            )
        elif not all(route.plaintext for route in self.routes):
            raise ConnectorFailure("BROKER_TLS", "not_started")
        return options

    async def start_producer(self, revision: ConnectionRevision, password: str | None) -> None:
        async def start() -> None:
            from aiokafka import AIOKafkaProducer

            self.client = AIOKafkaProducer(
                **self.options(revision, password),
                acks="all",
                enable_idempotence=True,
                max_request_size=1048576 + 4096,
                max_batch_size=1048576 + 1024,
                compression_type=None,
            )
            await self.client.start()

        await self.call(start)

    async def publish(self, topic: str, value: bytes, event: str) -> tuple[str, int, int]:
        async def send() -> tuple[str, int, int]:
            metadata = await self.client.send_and_wait(
                topic, value, headers=[("weave-event-id", event.encode("ascii"))]
            )
            return metadata.topic, metadata.partition, metadata.offset

        return await self.call(send)

    async def start_consumer(self, revision: ConnectionRevision, password: str | None, topic: str, group: str) -> None:
        async def start() -> None:
            from aiokafka import AIOKafkaConsumer, ConsumerRebalanceListener

            owner = self

            class Listener(ConsumerRebalanceListener):  # type: ignore[misc]
                async def on_partitions_revoked(self, partitions: Any) -> None:
                    keys = {(p.topic, p.partition) for p in partitions}
                    owner.admitting.difference_update(keys)
                    # Completed receipt commands may finish during this bounded drain.
                    deadline = asyncio.get_running_loop().time() + 5
                    while any(owner.frontiers.get(k) and owner.frontiers[k].pending for k in keys):
                        if asyncio.get_running_loop().time() >= deadline:
                            break
                        await asyncio.sleep(0.01)
                    for key in keys:
                        owner.generations.pop(key, None)
                        owner.frontiers.pop(key, None)

                async def on_partitions_assigned(self, partitions: Any) -> None:
                    if len(partitions) > 32:
                        assert owner.loop is not None
                        owner.loop.deny()
                    owner.epoch += 1
                    for p in partitions:
                        key = (p.topic, p.partition)
                        owner.generations[key] = owner.epoch
                        owner.frontiers[key] = PartitionFrontier()
                        owner.admitting.add(key)

            self.consumer = True
            self.client = AIOKafkaConsumer(
                **self.options(revision, password),
                enable_auto_commit=False,
                auto_offset_reset="earliest",
                group_id=group,
                max_poll_records=32,
                fetch_max_bytes=1048576 + 4096,
                max_partition_fetch_bytes=1048576 + 4096,
                rebalance_timeout_ms=10000,
                max_poll_interval_ms=30000,
            )
            self.client.subscribe([topic], listener=Listener())
            await self.client.start()

        await self.call(start)

    async def fetch(self, cluster: str) -> tuple[tuple[BrokerRecord, int], ...]:
        async def get() -> tuple[tuple[BrokerRecord, int], ...]:
            batches = await self.client.getmany(timeout_ms=500, max_records=32)
            records = []
            for partition, batch in batches.items():
                key = (partition.topic, partition.partition)
                if key not in self.admitting:
                    continue
                for item in batch:
                    headers = item.headers
                    valid = (
                        item.key is None
                        and len(headers) == 1
                        and headers[0][0] == "weave-event-id"
                        and isinstance(headers[0][1], bytes)
                        and len(headers[0][1]) == 36
                    )
                    try:
                        event = headers[0][1].decode("ascii") if valid else None
                    except UnicodeError:
                        event = None
                    self.frontiers[key].offer(item.offset)
                    records.append(
                        (
                            BrokerRecord(cluster, item.topic, item.partition, item.offset, event, item.value, valid),
                            self.generations[key],
                        )
                    )
            return tuple(records)

        return await self.call(get)

    async def commit(self, record: BrokerRecord, generation: int) -> bool:
        async def commit() -> bool:
            from aiokafka import TopicPartition

            key = (record.topic, record.partition)
            if self.generations.get(key) != generation:
                return False
            offset = self.frontiers[key].accept(record.offset)
            if offset is not None:
                await self.client.commit({TopicPartition(*key): offset})
            return True

        return await self.call(commit)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        deadline = time.monotonic() + self.cleanup_seconds
        self.cleanup_deadline = deadline
        if self.loop is None:
            return
        loop = self.loop
        loop.revoked.set()
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(loop.abort)

        async def stop() -> None:
            loop.abort()
            if self.client is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(self.client.stop(), max(0.0, deadline - time.monotonic()) / 2)
            current = asyncio.current_task()
            tasks = [task for task in asyncio.all_tasks() if task is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=max(0.0, deadline - time.monotonic()) / 2)

        try:
            if not loop.is_closed():
                await asyncio.wait_for(self.call(stop, closing=True), max(0.0, deadline - time.monotonic()))
        finally:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
            remaining = max(0.0, deadline - time.monotonic())
            if self.thread is not None and self.thread.is_alive() and remaining:
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.to_thread(self.thread.join, remaining), remaining)
            if self.thread is not None and self.thread.is_alive():
                raise RuntimeError("Broker owner cleanup incomplete")


_process_lock = threading.Lock()
_process_owners: set[BrokerOwner] = set()


@service
class BrokerClients:
    def __init__(self, policy: BrokerPolicy) -> None:
        self.policy = policy
        self.owners: set[BrokerOwner] = set()

    async def acquire(
        self,
        revision: ConnectionRevision,
        *,
        consumer: bool = False,
        credential_versions: tuple[tuple[str, str | None], ...] = (),
    ) -> BrokerOwner:
        self.reap()
        if not self.policy.enabled or len(self.owners) >= self.policy.max_clients:
            raise ConnectorFailure("BROKER_CAPACITY", "not_started")
        if consumer and sum(owner.consumer for owner in self.owners) >= self.policy.max_clients - 1:
            raise ConnectorFailure("BROKER_CONSUMER_CAPACITY", "not_started")
        approved = set(revision.allowed_destinations)
        routes = tuple(
            r for r in self.policy.routes if r.connection_revision_id == revision.id and r.advertised in approved
        )
        if not routes:
            raise ConnectorFailure("BROKER_EGRESS", "not_started")
        owner = BrokerOwner(routes, self.policy.cleanup_seconds, credential_versions)
        owner.consumer = consumer
        with _process_lock:
            if len(_process_owners) >= 8 or (consumer and sum(v.consumer for v in _process_owners) >= 7):
                raise ConnectorFailure("BROKER_PROCESS_CAPACITY", "not_started")
            _process_owners.add(owner)
        self.owners.add(owner)
        try:
            await owner.open()
            return owner
        except BaseException:
            await self.release(owner)
            raise

    def reap(self) -> None:
        def finished(owner: BrokerOwner) -> bool:
            return (owner.thread is None and owner.closed) or (
                owner.thread is not None
                and (owner.closed or owner.thread.ident is not None)
                and not owner.thread.is_alive()
            )

        self.owners.difference_update(owner for owner in tuple(self.owners) if finished(owner))
        with _process_lock:
            _process_owners.difference_update(owner for owner in tuple(_process_owners) if finished(owner))

    async def release(self, owner: BrokerOwner) -> None:
        try:
            await owner.close()
        finally:
            self.reap()

    async def close(self) -> None:
        results = await asyncio.gather(*(self.release(owner) for owner in tuple(self.owners)), return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise RuntimeError("Broker owner cleanup incomplete")
