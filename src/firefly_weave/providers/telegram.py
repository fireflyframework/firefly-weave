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

"""Webhook-only Telegram verification and immutable bot/chat resource policy.

Telegram authenticates possession of a shared header secret, not a body HMAC.
All supported and ignored updates share one source-scoped update identity kind.
"""

import hmac
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pyfly.container import service

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.parser import parse_source
from firefly_weave.connections.models import unavailable
from firefly_weave.contracts.connectors import ConnectionRequest, ConnectionRevision
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.providers import (
    ProviderEvent,
    ProviderIngressResponse,
    ProviderSource,
    ProviderSourceRequest,
)
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.providers.credentials import ProviderCredentials
from firefly_weave.providers.service import denied, invalid

ORIGIN = "https://api.telegram.org"
MAX_ID = (1 << 52) - 1
_HEADER = "x-telegram-bot-api-secret-token"
_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}")


def identity(value: object, *, signed: bool = False) -> str:
    if type(value) is not int or value == 0 or abs(value) > MAX_ID or (not signed and value < 0):
        raise ValueError("Invalid provider identity")
    return str(value)


def message_identity(value: object) -> str:
    result = identity(value)
    if int(result) > 2_147_483_647:
        raise ValueError("Invalid message identity")
    return result


def identity_string(value: str, *, signed: bool = False) -> str:
    if not re.fullmatch(r"-?[1-9][0-9]{0,15}" if signed else r"[1-9][0-9]{0,15}", value):
        raise ValueError("Invalid provider identity")
    identity(int(value), signed=signed)
    return value


class TelegramConfig(ContractModel):
    account_id: Annotated[str, Field(min_length=1, max_length=16)]
    mode: Literal["webhook"]
    allowed_chat_ids: list[Annotated[str, Field(min_length=1, max_length=17)]] = Field(min_length=1, max_length=100)

    @field_validator("account_id")
    @classmethod
    def bot_identity(cls, value: str) -> str:
        return identity_string(value)

    @field_validator("allowed_chat_ids")
    @classmethod
    def chat_identities(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate chat identities")
        for item in value:
            identity_string(item, signed=True)
        return value


def validate_telegram_connection(request: ConnectionRequest) -> None:
    try:
        TelegramConfig.model_validate(request.config)
        if (
            request.allowed_destinations != (ORIGIN,)
            or set(request.secret_refs) != {"botToken", "webhookSecret"}
            or len(set(request.secret_refs.values())) != 2
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise unavailable() from None


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("Invalid provider object")
    return value


def _known_chat(kind: str, item: JsonObject) -> JsonObject | None:
    if kind in {
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
        "guest_message",
        "message_reaction",
        "message_reaction_count",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
        "chat_boost",
        "removed_chat_boost",
    }:
        return _object(item.get("chat"))
    if kind == "callback_query" and "message" in item:
        return _object(_object(item["message"]).get("chat"))
    return None


def normalize(document: JsonObject, config: TelegramConfig, webhook_secret: str) -> ProviderEvent:
    update_id = document.get("update_id")
    if type(update_id) is not int or not 0 <= update_id <= 2_147_483_647 or len(document) != 2:
        raise ValueError("Invalid update envelope")
    kind = next(key for key in document if key != "update_id")
    item = _object(document[kind])
    chat = _known_chat(kind, item)
    chat_id = identity(chat.get("id"), signed=True) if chat is not None else None
    if chat_id is not None and chat_id not in config.allowed_chat_ids:
        raise denied()
    payload: JsonObject = {
        "bot_id": config.account_id,
        "update_id": str(update_id),
        "update_type": "unsupported",
        "chat_id": chat_id,
        "message_id": None,
        "sender_id": None,
        "text": None,
        "date": None,
        "facts_digest": canonical_digest(document),
    }
    reason: str | None = "unsupported_update"
    occurred_at = None
    if kind in {
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_message",
        "edited_business_message",
        "guest_message",
    }:
        if "ephemeral_message_id" in item:
            message_identity(item["ephemeral_message_id"])
        if "ephemeral_message_id" not in item or "message_id" in item:
            payload["message_id"] = message_identity(item.get("message_id"))
        date = item.get("date")
        if type(date) is not int or not 0 <= date <= 253402300799:
            raise ValueError("Invalid message date")
        occurred_at = datetime.fromtimestamp(date, UTC)
        payload["date"] = date
        sender = _object(item["from"]) if "from" in item else None
        if sender is not None:
            payload["sender_id"] = identity(sender.get("id"))
            if type(sender.get("is_bot")) is not bool:
                raise ValueError("Invalid sender")
        if kind.startswith("edited_"):
            payload["update_type"], reason = "edited", "edited_message"
        elif kind == "message":
            payload["update_type"], reason = "message", "unsupported_message"
            if sender is not None and (sender["is_bot"] or payload["sender_id"] == config.account_id):
                reason = "bot_message"
            elif (
                sender is not None
                and chat is not None
                and chat.get("type") in {"private", "group", "supergroup"}
                and not any(
                    key in item
                    for key in (
                        "sender_chat",
                        "message_thread_id",
                        "is_topic_message",
                        "ephemeral_message_id",
                        "receiver_user",
                        "business_connection_id",
                        "guest_query_id",
                    )
                )
                and item.get("text") is not None
            ):
                text = item["text"]
                if not isinstance(text, str) or not 1 <= len(text) <= 4096 or webhook_secret in text:
                    raise ValueError("Invalid message text")
                payload["text"], reason = text, None
    return ProviderEvent(
        event_id=f"bot:{config.account_id}:update:{update_id}",
        kind="telegram-update",
        occurred_at=occurred_at,
        account_id=config.account_id,
        conversation_id=chat_id,
        sender_id=payload["sender_id"] if isinstance(payload["sender_id"], str) else None,
        payload=payload,
        disposition="ignore" if reason else "dispatch",
        reason=reason,
    )


@service
class TelegramVerifier:
    def __init__(self, credentials: ProviderCredentials) -> None:
        self.credentials = credentials

    def validate_source(self, request: ProviderSourceRequest, connection: ConnectionRevision) -> None:
        validate_telegram_connection(connection)
        if (
            request.provider != "telegram"
            or request.package != "firefly-weave"
            or connection.adapter != "weave-telegram"
            or request.policy != connection.config
        ):
            raise ValueError("Incompatible provider source")

    async def challenge(self, source: ProviderSource, query: Mapping[str, str]) -> ProviderIngressResponse:
        raise denied()

    async def verify(
        self, source: ProviderSource, raw_body: bytes, headers: Mapping[str, str], received_at: datetime
    ) -> tuple[tuple[ProviderEvent, ...], ProviderIngressResponse]:
        values = [value for key, value in headers.items() if key.lower() == _HEADER]
        if len(values) != 1 or _SECRET.fullmatch(values[0]) is None:
            raise denied()
        connection, secrets = await self.credentials.resolve(source, slots=("webhookSecret",))
        try:
            self.validate_source(source, connection)
            secret = secrets["webhookSecret"].value
            if _SECRET.fullmatch(secret) is None or not hmac.compare_digest(
                secret.encode("ascii"), values[0].encode("ascii")
            ):
                raise ValueError
        except (ValueError, KeyError):
            raise denied() from None
        try:
            event = normalize(
                parse_source(raw_body, format="json").value, TelegramConfig.model_validate(connection.config), secret
            )
        except (ValueError, RecursionError, OverflowError, OSError):
            raise invalid() from None
        return (event,), ProviderIngressResponse()
