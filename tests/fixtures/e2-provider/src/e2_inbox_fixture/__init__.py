# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""A bounded local echo example; no external system is contacted."""

import json
from datetime import UTC, datetime
from importlib.resources import files

from pyfly.container import service

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.connectors.packages import ConnectorPackage, PackageMetadata
from firefly_weave.contracts.connectors import (
    ActionContext,
    BoundConnection,
    ConnectionTestResult,
    ConnectorFailure,
)
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.providers.credentials import ProviderCredentials

from .conformance import check


@service
class Echo:
    """Read-only example implementing the existing leased adapter contract."""

    async def execute(self, input: JsonObject, context: ActionContext) -> JsonValue:
        if context.attempt_deadline <= datetime.now(UTC):
            raise ConnectorFailure("DEADLINE_EXCEEDED", "not_started")
        if context.authorize is not None:
            await context.authorize()
        if context.invocation.action != "echo":
            raise ConnectorFailure("INVALID_ACTION", "not_started")
        if validate_payload(context.invocation.input_schema, input, {}):
            raise ConnectorFailure("INVALID_INPUT", "not_started")
        canonical = FrozenDocument.from_value(input)
        if len(canonical.canonical) > min(context.invocation.max_request_bytes, context.invocation.max_response_bytes):
            raise ConnectorFailure("PAYLOAD_TOO_LARGE", "not_started")
        return canonical.value

    async def test_connection(self, connection: BoundConnection) -> ConnectionTestResult:
        return ConnectionTestResult(ok=True)


@service
class FixtureVerifier:
    """Test-only HMAC protocol; never a claimed vendor implementation."""

    def __init__(self, credentials: ProviderCredentials) -> None:
        self.credentials = credentials
        self.fail_hook = False
        self.fail_commit = False
        self.reject_source = False
        self.mutate_validation = False

    def validate_source(self, request, connection):
        if self.reject_source or request.policy.get("account_id") != connection.config["account_id"]:
            raise ValueError("fixture-secret-that-must-never-escape")
        if self.mutate_validation:
            request.policy["account_id"] = "mutated"
            connection.config["account_id"] = "mutated"

    async def verify(self, source, raw_body, headers, received_at):
        import hashlib
        import hmac

        from firefly_weave.compiler.parser import parse_source
        from firefly_weave.contracts.providers import ProviderEvent, ProviderIngressResponse
        from firefly_weave.providers.service import denied

        _, secrets = await self.credentials.resolve(source)
        expected = hmac.new(secrets["signature"].value.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(headers.get("x-fixture-signature", ""), expected):
            raise denied()
        document = parse_source(raw_body, format="json").value
        if document.get("account") != source.policy["account_id"]:
            raise denied()
        return tuple(
            ProviderEvent.model_validate_json(json.dumps(event)) for event in document["events"]
        ), ProviderIngressResponse(body='{"ok":true}')

    async def challenge(self, source, query):
        from firefly_weave.contracts.providers import ProviderIngressResponse
        from firefly_weave.providers.service import denied

        if query.get("token") != "fixture-challenge":
            raise denied()
        return ProviderIngressResponse(media_type="text/plain", body=query.get("challenge", ""))

    async def persist(self, tx, source, events):
        from sqlalchemy import text

        for event in events:
            await tx.session.execute(
                text("INSERT INTO provider_fixture_effects(source_id,event_id) VALUES(:source,:event)"),
                {"source": source.id, "event": event.event_id},
            )
        if self.fail_hook:
            raise RuntimeError("fixture hook failure")
        if self.fail_commit:
            tx.session.info["e2_fail_commit"] = True


package = ConnectorPackage(
    PackageMetadata(FrozenDocument.from_value(json.loads(files(__package__).joinpath("connector.json").read_text()))),
    Echo,
    conformance=check,
    verifier_service_type=FixtureVerifier,
)
