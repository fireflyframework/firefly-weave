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
"""Bounded single-POST Teams transport implementing the public conversation protocol."""

import asyncio
import json
from typing import Any
from urllib.parse import quote

from pyfly.client.ports.outbound import BoundedHttpClientPort

from firefly_weave.connections.machine_tokens import MachineProfile, MachineTokenService, remaining
from firefly_weave.connectors.egress import EgressPolicy, origin
from firefly_weave.contracts.connectors import ActionContext, ConnectorFailure
from firefly_weave.contracts.teams import TeamsReference
from firefly_weave.providers.teams.policy import SCOPE, TeamsProfile, service_url


def activity_url(base: str, conversation: str, activity: str | None = None) -> str:
    url = service_url(base).rstrip("/") + "/v3/conversations/" + quote(conversation, safe="") + "/activities"
    return url + ("/" + quote(activity, safe="") if activity is not None else "")


class TeamsConversations:
    def __init__(
        self,
        client: BoundedHttpClientPort,
        tokens: MachineTokenService,
        context: ActionContext,
        reference: TeamsReference,
        profile: TeamsProfile,
    ) -> None:
        self.client, self.tokens, self.context, self.reference, self.profile = (
            client,
            tokens,
            context,
            reference,
            profile,
        )

    def __getattr__(self, name: str) -> Any:
        raise ConnectorFailure("TEAMS_UNSUPPORTED", "not_started")

    async def send_to_conversation(self, conversation_id: str, activity: Any) -> Any:
        return await self.send(conversation_id, None, activity)

    async def reply_to_activity(self, conversation_id: str, activity_id: str, activity: Any) -> Any:
        return await self.send(conversation_id, activity_id, activity)

    async def send(self, conversation_id: str, activity_id: str | None, activity: Any) -> Any:
        from microsoft_agents.activity import ResourceResponse

        context = self.context
        started = False
        token = None
        try:
            if (
                conversation_id != self.reference.conversation_id
                or context.reference is None
                or context.authorize is None
            ):
                raise ValueError
            if activity.type != "message" or not isinstance(activity.text, str) or not 1 <= len(activity.text) <= 16384:
                raise ValueError
            url = activity_url(self.reference.service_url, conversation_id, activity_id)
            policy = EgressPolicy(context.invocation.connection.allowed_destinations)
            if origin(url) not in {origin(v) for v in policy.allowed_origins}:
                raise ValueError
            body = json.dumps(
                {
                    "type": "message",
                    "text": activity.text,
                    "textFormat": "plain",
                    "from": {"id": self.reference.bot_id},
                    "recipient": {"id": self.reference.user_id},
                    "conversation": {"id": conversation_id},
                    **({"replyToId": activity_id} if activity_id is not None else {}),
                },
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            if len(body) + len(url.encode()) > context.invocation.max_request_bytes:
                raise ValueError
            async with asyncio.timeout(remaining(context)):
                await context.authorize()
                await context.reference(self.reference.id, self.reference.generation, activity_id)
                token = await self.tokens.acquire(
                    context, MachineProfile(self.profile.account_id, self.profile.token_endpoint, (SCOPE,))
                )
                await context.authorize()
                current = await context.reference(self.reference.id, self.reference.generation, activity_id)
                if TeamsReference.model_validate_json(json.dumps(current)) != self.reference:
                    raise ValueError
                bearer = token.consume()
                started = True
                response = await self.client.request_bounded(
                    "POST",
                    url,
                    max_response_bytes=min(65536, context.invocation.max_response_bytes),
                    content=body,
                    headers={
                        "Authorization": "Bearer " + bearer,
                        "Content-Type": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    timeout=remaining(context),
                    egress_policy=policy,
                    follow_redirects=False,
                )
                if response.status_code not in {200, 201, 202}:
                    raise ConnectorFailure(
                        "TEAMS_REJECTED", "failed" if response.status_code in {400, 401, 403, 404, 429} else "unknown"
                    )
                result = response.json()
                identifier = result.get("id") if isinstance(result, dict) else None
                if (
                    not isinstance(identifier, str)
                    or not 1 <= len(identifier) <= 256
                    or token.contains_sensitive(identifier)
                    or any(ord(c) < 32 for c in identifier)
                ):
                    raise ValueError
                return ResourceResponse(id=identifier)
        except ConnectorFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ConnectorFailure("TEAMS_SEND", "unknown" if started else "not_started") from None
        finally:
            if token is not None:
                token.discard()


class TeamsClient:
    def __init__(self, base_uri: str, conversations: TeamsConversations) -> None:
        self.base_uri, self.conversations = base_uri, conversations

    @property
    def attachments(self) -> Any:
        raise ConnectorFailure("TEAMS_UNSUPPORTED", "not_started")

    async def close(self) -> None:
        pass
