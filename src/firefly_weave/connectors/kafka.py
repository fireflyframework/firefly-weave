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

"""Acknowledged Kafka publish; stable event IDs do not promise exactly-once effects."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import rfc8785
from pyfly.container import service

from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connectors.broker import BrokerConnectionConfig, BrokerPublishConfig
from firefly_weave.connectors.kafka_transport import BrokerClients
from firefly_weave.contracts.connectors import ActionContext, BoundConnection, ConnectionTestResult, ConnectorFailure
from firefly_weave.contracts.values import JsonObject, JsonValue


@service
class KafkaConnector:
    def __init__(self, clients: BrokerClients) -> None:
        self.clients = clients

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        owner = None
        enqueued = False
        try:
            invocation = context.invocation
            config = BrokerConnectionConfig.model_validate_json(json.dumps(invocation.connection.config))
            action = BrokerPublishConfig.model_validate(invocation.config)
            if context.authorize is None or invocation.action != "publish" or action.topic not in config.topics:
                raise ConnectorFailure("BROKER_INPUT", "not_started")
            value = rfc8785.dumps(input)
            if len(value) > min(1048576, invocation.max_request_bytes):
                raise ConnectorFailure("BROKER_SIZE", "not_started")
            if validate_payload(invocation.input_schema, input, invocation.schema_bundle):
                raise ConnectorFailure("BROKER_INPUT", "not_started")
            # The worker must retain authority to record an ambiguous result after cleanup.
            remaining = min(
                30.0,
                (context.attempt_deadline - datetime.now(UTC)).total_seconds()
                - self.clients.policy.cleanup_seconds
                - 1.0,
            )
            if remaining <= 0:
                raise ConnectorFailure("BROKER_DEADLINE", "not_started")
            async with asyncio.timeout(remaining):
                await context.authorize()
                password = None
                versions: tuple[tuple[str, str | None], ...] = ()
                if config.security_protocol == "SASL_SSL":
                    async with asyncio.timeout(5):
                        secret = await context.credentials("password")
                        password = secret.value
                        versions = (("password", secret.provider_version),)
                        del secret
                await context.authorize()
                owner = await self.clients.acquire(invocation.connection, credential_versions=versions)
                await owner.start_producer(invocation.connection, password)
                password = None
                await context.authorize()
                event = str(uuid5(NAMESPACE_URL, "firefly-weave:" + context.operation_key))
                enqueued = True

                async def watch() -> None:
                    assert context.authorize is not None
                    while True:
                        await asyncio.sleep(1)
                        await context.authorize()

                sending = asyncio.create_task(owner.publish(action.topic, value, event))
                watching = asyncio.create_task(watch())
                try:
                    done, _ = await asyncio.wait({sending, watching}, return_when=asyncio.FIRST_COMPLETED)
                    if watching in done:
                        await self.clients.release(owner)
                        watching.result()
                    topic, partition, offset = await sending
                finally:
                    sending.cancel()
                    watching.cancel()
                    await asyncio.gather(sending, watching, return_exceptions=True)
                await context.authorize()
                return {"topic": topic, "partition": partition, "offset": offset, "event_id": event}
        except asyncio.CancelledError:
            raise
        except ConnectorFailure:
            raise
        except Exception:
            raise ConnectorFailure(
                "BROKER_UNKNOWN" if enqueued else "BROKER_FAILED", "unknown" if enqueued else "failed"
            ) from None
        finally:
            if owner is not None:
                await self.clients.release(owner)

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        # Standing and lease authority are mandatory; a metadata-only BoundConnection
        # deliberately cannot create a credential-bearing Kafka lifetime.
        return ConnectionTestResult(ok=False, code="failed")
