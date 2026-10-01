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

"""Independent bounded broker traversal and lifespan-owned consumer turns."""

import asyncio
import logging
from contextlib import suppress

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from firefly_weave.access.scheduler import next_scope
from firefly_weave.connectors.broker import BrokerConnectionConfig
from firefly_weave.connectors.kafka_transport import BrokerClients, BrokerOwner
from firefly_weave.persistence.migrations import check_schema
from firefly_weave.persistence.uow import UnitOfWork
from firefly_weave.settings import Settings
from firefly_weave.triggers.kafka import KafkaSource, KafkaTrigger


class KafkaLoop:
    def __init__(self, settings: Settings, uow: UnitOfWork, triggers: KafkaTrigger, clients: BrokerClients) -> None:
        self.settings, self.uow, self.triggers, self.clients = settings, uow, triggers, clients
        self.engine: AsyncEngine | None = None
        self.task: asyncio.Task[None] | None = None
        self.turns: set[asyncio.Task[None]] = set()

    async def open(self) -> None:
        if not self.settings.broker.enabled or not self.settings.kafka_consumer_enabled:
            return
        if self.settings.broker.max_clients < 2:
            raise RuntimeError("Kafka consumer requires at least2 clients; max_clients=1 is publish-only")
        if self.settings.scheduler_database_url is None:
            raise RuntimeError("Kafka consumer requires execute-only scheduler connection")
        self.engine = create_async_engine(self.settings.scheduler_database_url.get_secret_value(), hide_parameters=True)
        try:
            async with asyncio.timeout(5), self.engine.connect() as connection:
                await check_schema(connection)
                valid = await connection.scalar(
                    text(
                        "SELECT NOT rolsuper AND NOT rolbypassrls AND pg_has_role(current_user,"
                        "'weave_scheduler','MEMBER') "
                        "AND NOT pg_has_role(current_user,'weave_app','MEMBER') AND NOT "
                        "pg_has_role(current_user,'weave_catalog_reader','MEMBER') "
                        "AND NOT has_table_privilege(current_user,'broker_routes','SELECT') AND NOT "
                        "has_table_privilege(current_user,'tenants','SELECT') "
                        "AND has_function_privilege(current_user,'weave_broker_tenants(integer)','EXECUTE') "
                        "FROM pg_roles WHERE rolname=current_user"
                    )
                )
                if not valid:
                    raise RuntimeError("Kafka traversal requires execute-only scheduler authority")
            self.task = asyncio.create_task(self.poll(), name="weave-broker-traversal")
        except BaseException:
            await self.close()
            raise

    async def cycle(self) -> None:
        if (
            len(self.turns) >= self.settings.broker.max_clients - 1
            or len(self.clients.owners) >= self.settings.broker.max_clients - 1
        ):
            return
        assert self.engine is not None
        async with asyncio.timeout(5):
            async with self.engine.begin() as connection:
                await connection.execute(text("SET LOCAL statement_timeout='4000ms'"))
                await connection.execute(text("SET LOCAL lock_timeout='1000ms'"))
                tenant = await connection.scalar(text("SELECT weave_broker_tenants(1)"))
            if tenant is None:
                return
            authority = await next_scope(self.uow, tenant, broker=True)
            if authority is None:
                return
            source = await self.triggers.scheduled_claim(authority)
        if source is not None:
            task = asyncio.create_task(self.turn(source), name="weave-kafka-consumer")
            self.turns.add(task)
            task.add_done_callback(self.turns.discard)

    async def turn(self, source: KafkaSource) -> None:
        owner: BrokerOwner | None = None
        guard: asyncio.Task[None] | None = None
        current = asyncio.current_task()
        assert current is not None
        try:
            async with asyncio.timeout(60):
                revision, secrets = await self.triggers.credentials(source.authority)
                import json

                config = BrokerConnectionConfig.model_validate_json(json.dumps(revision.config))
                async with self.uow.open(source.authority.scope, mutation=False) as tx:
                    route, _ = await self.triggers.checked(tx, source.authority)
                owner = await self.clients.acquire(
                    revision,
                    consumer=True,
                    credential_versions=tuple((slot, value.provider_version) for slot, value in secrets.items()),
                )

                async def monitor() -> None:
                    assert owner is not None
                    try:
                        while True:
                            await asyncio.sleep(1)
                            await self.triggers.authorize(source.authority)
                    except Exception:
                        await self.clients.release(owner)
                        current.cancel()

                guard = asyncio.create_task(monitor())
                await owner.start_consumer(
                    revision,
                    secrets["password"].value if "password" in secrets else None,
                    route.topic,
                    "weave-" + str(route.id),
                )
                del secrets
                while True:
                    await self.triggers.authorize(source.authority)
                    records = await owner.fetch(config.cluster_id)
                    for record, generation in records:
                        receipt = await source.consume(record)
                        if receipt.status == "rejected" and route.dead_letter_policy == "halt":
                            return
                        await self.triggers.authorize(source.authority)
                        await owner.commit(record, generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).error(
                "Broker consumer turn failed; source authority and replay remain required"
            )
        finally:
            if guard is not None:
                guard.cancel()
                with suppress(asyncio.CancelledError):
                    await guard
            if owner is not None:
                await self.clients.release(owner)
            await self.triggers.release(source.authority)

    async def poll(self) -> None:
        while True:
            try:
                await self.cycle()
            except Exception:
                logging.getLogger(__name__).error("Broker traversal failed; retrying bounded turn")
            await asyncio.sleep(1)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        turns = tuple(self.turns)
        for task in turns:
            task.cancel()
        if turns:
            await asyncio.gather(*turns, return_exceptions=True)
        if self.engine:
            await self.engine.dispose()
            self.engine = None
