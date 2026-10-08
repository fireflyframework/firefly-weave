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
"""Verified Teams activities traverse public SDK protocols before atomic event admission.

SDK clients are lazy and perform no outbound I/O on ingress. Mutable reference
state is consulted only by the transactional hook, never by event normalization.
"""

import asyncio
import json
import math
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from pyfly.client.ports.outbound import BoundedHttpClientPort
from pyfly.container import service
from pyfly.security.oauth2 import JWKSTokenValidator

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.connectors.egress import EgressPolicy, origin
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.providers import (
    ProviderEvent,
    ProviderIngressResponse,
    ProviderSource,
    ProviderSourceRequest,
)
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.providers.teams.policy import ISSUER, JWKS_URL, TeamsProfile, service_url
from firefly_weave.providers.teams.references import TeamsReferences


def denied() -> CatalogError:
    return CatalogError(401, "WV-TEAMS-AUTH", "Teams activity unavailable")


def reference_id(source: ProviderSource, profile: TeamsProfile) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            canonical_digest(
                {
                    "scope": source.scope.model_dump(mode="json"),
                    "app": profile.account_id,
                    "tenant": profile.tenant_id,
                    "conversation": profile.conversation_id,
                }
            ),
        )
    )


class DisabledClient:
    """No-network SDK object; all unimplemented protocol calls fail closed."""

    def __getattr__(self, name: str) -> Any:
        raise denied()

    async def close(self) -> None:
        pass


class LazyFactory:
    async def create_connector_client(
        self,
        context: Any,
        claims_identity: Any,
        service_url: str,
        audience: str,
        scopes: Any = None,
        use_anonymous: bool = False,
    ) -> Any:
        if use_anonymous or claims_identity.allow_anonymous:
            raise denied()
        return DisabledClient()

    async def create_user_token_client(self, context: Any, claims_identity: Any, use_anonymous: bool = False) -> Any:
        if use_anonymous:
            raise denied()
        return DisabledClient()


async def sdk_accept(activity: dict[str, Any], claims: dict[str, Any]) -> None:
    from microsoft_agents.hosting.core import HttpAdapterBase
    from microsoft_agents.hosting.core.authorization import ClaimsIdentity

    class Request:
        method = "POST"
        headers: Mapping[str, str] = {}

        async def json(self) -> dict[str, Any]:
            return activity

        def get_claims_identity(self) -> ClaimsIdentity:
            return ClaimsIdentity(
                claims={key: claims[key] for key in ("iss", "aud", "serviceurl")}, authentication_type="Bearer"
            )

        def get_path_param(self, name: str) -> str:
            return ""

    class Agent:
        seen = False

        async def on_turn(self, context: Any) -> None:
            self.seen = True

    async def failure(context: Any, error: Exception) -> None:
        raise denied() from None

    adapter = HttpAdapterBase(channel_service_client_factory=LazyFactory())
    adapter.on_turn_error = failure
    agent = Agent()
    response = await adapter.process_request(Request(), agent)
    if response.status_code != 202 or not agent.seen:
        raise denied()


@service
class TeamsVerifier:
    def __init__(self, client: BoundedHttpClientPort, references: TeamsReferences) -> None:
        self.client, self.references = client, references
        self.validators: OrderedDict[str, JWKSTokenValidator] = OrderedDict()

    def validate_source(self, request: ProviderSourceRequest, connection: ConnectionRevision) -> None:
        profile = TeamsProfile.model_validate(request.policy)
        expected = TeamsProfile.model_validate(connection.config)
        if (
            profile != expected
            or connection.adapter != "weave-teams"
            or set(connection.secret_refs) != {"client_secret"}
        ):
            raise denied()
        if origin(profile.token_endpoint) not in {origin(v) for v in connection.allowed_destinations}:
            raise denied()

    async def fetch(self, uri: str, *, timeout: float, max_bytes: int) -> bytes:
        if uri != JWKS_URL:
            raise denied()
        async with asyncio.timeout(timeout):
            result = await self.client.request_bounded(
                "GET",
                uri,
                max_response_bytes=min(max_bytes, 262144),
                headers={"Accept-Encoding": "identity"},
                timeout=timeout,
                follow_redirects=False,
                egress_policy=EgressPolicy(("https://login.botframework.com",)),
            )
            if result.status_code != 200 or result.headers.get("content-encoding", "identity").lower() != "identity":
                raise denied()
            return cast(bytes, result.content)

    def validator(self, audience: str) -> JWKSTokenValidator:
        if audience not in self.validators:
            if len(self.validators) == 128:
                self.validators.popitem(last=False)
            self.validators[audience] = JWKSTokenValidator(
                JWKS_URL,
                issuer=ISSUER,
                audiences=[audience],
                algorithms=["RS256"],
                leeway=300,
                async_jwks_fetcher=self.fetch,
                jwks_timeout=5,
                jwks_max_bytes=262144,
                jwks_max_keys=100,
            )
        self.validators.move_to_end(audience)
        return self.validators[audience]

    async def verify(
        self, source: ProviderSource, raw_body: bytes, headers: Mapping[str, str], received_at: datetime
    ) -> tuple[tuple[ProviderEvent, ...], ProviderIngressResponse]:
        try:
            if len(raw_body) > 1048576:
                raise denied()
            profile = TeamsProfile.model_validate(source.policy)
            authorization = headers.get("authorization", "")
            if not authorization.startswith("Bearer ") or authorization.count(" ") != 1:
                raise denied()
            claims = await self.validator(profile.account_id).validate_async(authorization[7:], timeout=5)
            for name in ("iat", "nbf", "exp"):
                value = claims.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise denied()
            if (
                claims["iat"] > claims["exp"]
                or claims["nbf"] > claims["exp"]
                or claims["iat"] > received_at.timestamp() + 300
            ):
                raise denied()
            activity = json.loads(raw_body)
            if not isinstance(activity, dict):
                raise denied()
            url = service_url(activity.get("serviceUrl", ""))
            if claims.get("serviceurl") != url:
                raise denied()
            conversation = activity.get("conversation", {})
            tenant = activity.get("channelData", {}).get("tenant", {}).get("id")
            if (
                activity.get("channelId") != "msteams"
                or conversation.get("conversationType") != "personal"
                or conversation.get("id") != profile.conversation_id
                or tenant != profile.tenant_id
                or conversation.get("tenantId", tenant) != tenant
                or activity.get("recipient", {}).get("id") != profile.bot_id
            ):
                raise denied()
            if (
                activity.get("deliveryMode") not in (None, "normal")
                or activity.get("attachments")
                or activity.get("entities")
            ):
                raise denied()
            identity = activity.get("id")
            if not isinstance(identity, str) or not 1 <= len(identity) <= 256:
                raise denied()
            kind = activity.get("type")
            effect = "add"
            ignore = False
            if kind == "message":
                sender = activity.get("from", {}).get("id")
                if sender == profile.bot_id:
                    ignore = True
                elif sender != profile.user_id:
                    raise denied()
                text = activity.get("text")
                if (
                    not isinstance(text, str)
                    or not 1 <= len(text) <= 16384
                    or activity.get("textFormat", "plain") != "plain"
                ):
                    raise denied()
                effect = "message"
            elif kind == "installationUpdate":
                effect = {"add": "add", "remove": "remove"}.get(str(activity.get("action")), "")
                if not effect:
                    raise denied()
            elif kind == "conversationUpdate":
                added = activity.get("membersAdded", [])
                removed = activity.get("membersRemoved", [])
                adds = any(m.get("id") == profile.bot_id for m in added)
                removes = any(m.get("id") == profile.bot_id for m in removed)
                if adds == removes:
                    raise denied()
                effect = "remove" if removes else "add"
            else:
                raise denied()
            await sdk_accept(activity, claims)
            payload: dict[str, Any] = {
                "reference_id": reference_id(source, profile),
                "generation": profile.installation_generation,
                "activity_id": identity,
                "service_url": url,
                "effect": effect,
                "text": activity.get("text", "") if effect == "message" else "",
            }
            event = ProviderEvent(
                event_id=identity,
                kind="message" if effect == "message" else "lifecycle",
                account_id=profile.account_id,
                conversation_id=profile.conversation_id,
                sender_id=activity.get("from", {}).get("id"),
                payload=payload,
                disposition="ignore" if effect != "message" or ignore else "dispatch",
                reason="self_message" if ignore else "lifecycle" if effect != "message" else None,
            )
            return (event,), ProviderIngressResponse(status_code=202, body="")
        except Exception:
            raise denied() from None

    async def challenge(self, source: ProviderSource, query: Mapping[str, str]) -> ProviderIngressResponse:
        raise denied()

    async def persist(self, tx: Transaction, source: ProviderSource, events: tuple[ProviderEvent, ...]) -> None:
        await self.references.persist(tx, source, events)
