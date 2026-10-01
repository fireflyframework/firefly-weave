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
"""Authenticated WhatsApp batches; only E2 admission may commit or acknowledge events."""

import hashlib
import hmac
import re
from collections.abc import Mapping
from datetime import UTC, datetime

from pyfly.container import service

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.providers import (
    ProviderEvent,
    ProviderIngressResponse,
    ProviderSource,
    ProviderSourceRequest,
)
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.whatsapp import STATUSES, WhatsAppConfig, installation_id, validate_connection
from firefly_weave.definitions.models import CatalogError
from firefly_weave.persistence.uow import Transaction
from firefly_weave.providers.credentials import ProviderCredentials
from firefly_weave.providers.service import denied, invalid
from firefly_weave.providers.whatsapp_status import WhatsAppStatusService


def _object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise invalid()
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise invalid()
    return value


def _string(value: JsonValue, maximum: int, pattern: str | None = None) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or (pattern and not re.fullmatch(pattern, value)):
        raise invalid()
    return value


def _event(source: ProviderSource, profile: WhatsAppConfig, item: JsonObject, *, status: bool) -> ProviderEvent:
    identity = _string(item.get("id"), 256, r"[!-~]+")
    timestamp = _string(item.get("timestamp"), 12, r"0|[1-9][0-9]{0,11}")
    try:
        occurred = datetime.fromtimestamp(int(timestamp), UTC)
    except (OverflowError, OSError, ValueError):
        raise invalid() from None
    peer = _string(item.get("recipient_id" if status else "from"), 15, r"[0-9]+")
    provider_type = _string(item.get("status" if status else "type"), 64, r"[a-z][a-z0-9_]*")
    install = installation_id(source)
    known = provider_type in STATUSES if status else provider_type == "text"
    kind = "whatsapp-status" if status else "whatsapp-message"
    semantic: JsonObject = {"domain": "whatsapp-event-v1", "installation": install, "message": identity, "kind": kind}
    if status:
        semantic.update(status=provider_type, timestamp=timestamp)
    if not known:
        kind = "whatsapp-unsupported"
        semantic.update(provider_type=provider_type, timestamp=timestamp)
    event_id = canonical_digest(semantic)
    digest = canonical_digest(item)
    reason = None
    if not known:
        payload: JsonObject = {
            "installation_id": install,
            "message_id": identity,
            "provider_type": provider_type,
            "timestamp": timestamp,
            "facts_digest": digest,
        }
        reason = "unsupported_status" if status else "unsupported_message_type"
    else:
        text = None if status else _string(_object(item.get("text")).get("body"), 4096)
        errors: list[JsonValue] = []
        if status:
            for error in _array(item.get("errors", [])):
                code = _object(error).get("code")
                if type(code) is not int or not 0 <= code <= 2147483647 or len(errors) >= 10:
                    raise invalid()
                errors.append(code)
        payload = {
            "installation_id": install,
            "event_type": "status" if status else "text",
            "message_id": identity,
            "phone_number_id": profile.phone_number_id,
            "peer_id": peer,
            "timestamp": timestamp,
            "text": text,
            "status": provider_type if status else None,
            "error_codes": errors,
            "facts_digest": digest,
        }
        if not status and peer == profile.business_phone_number:
            reason = "self_message"
    return ProviderEvent(
        event_id=event_id,
        kind=kind,
        occurred_at=occurred,
        account_id=profile.account_id,
        conversation_id=peer,
        sender_id=None if status else peer,
        payload=payload,
        disposition="ignore" if reason else "dispatch",
        reason=reason,
    )


def normalize(source: ProviderSource, document: JsonObject) -> tuple[ProviderEvent, ...]:
    profile = WhatsAppConfig.model_validate(source.policy)
    if document.get("object") != "whatsapp_business_account":
        raise invalid()
    events: list[ProviderEvent] = []
    for raw_entry in _array(document.get("entry")):
        entry = _object(raw_entry)
        if entry.get("id") != profile.account_id:
            raise denied()
        changes = _array(entry.get("changes"))
        if not changes:
            raise invalid()
        for raw_change in changes:
            change = _object(raw_change)
            if change.get("field") != "messages":
                raise invalid()
            value = _object(change.get("value"))
            if (
                value.get("messaging_product") != "whatsapp"
                or _object(value.get("metadata")).get("phone_number_id") != profile.phone_number_id
                or ("app_id" in value and value["app_id"] != profile.app_id)
            ):
                raise denied()
            if "errors" in value or not any(key in value for key in ("messages", "statuses")):
                raise invalid()
            before = len(events)
            for key in ("messages", "statuses"):
                for item in _array(value.get(key, [])):
                    if len(events) >= 100:
                        raise CatalogError(413, "WV-PROVIDER-BATCH", "Provider batch exceeds limit")
                    events.append(_event(source, profile, _object(item), status=key == "statuses"))
            if len(events) == before:
                raise invalid()
    if not events:
        raise invalid()
    return tuple(events)


@service
class WhatsAppVerifier:
    def __init__(self, credentials: ProviderCredentials, statuses: WhatsAppStatusService) -> None:
        self.credentials, self.statuses = credentials, statuses

    def validate_source(self, request: ProviderSourceRequest, connection: ConnectionRevision) -> None:
        validate_connection(connection)
        WhatsAppConfig.model_validate(request.policy)
        if (
            request.provider != "whatsapp"
            or request.package != "firefly-weave"
            or connection.adapter != "weave-whatsapp"
            or request.policy != connection.config
        ):
            raise ValueError("Exact WhatsApp source profile required")

    async def _secret(self, source: ProviderSource, slot: str) -> bytes:
        try:
            _, values = await self.credentials.resolve(source, slots=(slot,))
            value = values[slot].value.encode("utf-8")
            if not 1 <= len(value) <= 4096:
                raise denied()
            return value
        except Exception:
            raise denied() from None

    async def challenge(self, source: ProviderSource, query: Mapping[str, str]) -> ProviderIngressResponse:
        if (
            set(query) != {"hub.mode", "hub.verify_token", "hub.challenge"}
            or query["hub.mode"] != "subscribe"
            or not 1 <= len(query["hub.verify_token"].encode()) <= 4096
            or not re.fullmatch(r"[ -~]{1,4096}", query["hub.challenge"])
        ):
            raise denied()
        secret = await self._secret(source, "verifyToken")
        if not hmac.compare_digest(secret, query["hub.verify_token"].encode("utf-8")):
            raise denied()
        return ProviderIngressResponse(media_type="text/plain", body=query["hub.challenge"])

    async def verify(
        self, source: ProviderSource, raw_body: bytes, headers: Mapping[str, str], received_at: datetime
    ) -> tuple[tuple[ProviderEvent, ...], ProviderIngressResponse]:
        if len(raw_body) > 1048576:
            raise CatalogError(413, "WV-PROVIDER-SIZE", "Provider body exceeds limit")
        signatures = [value for key, value in headers.items() if key.lower() == "x-hub-signature-256"]
        if len(signatures) != 1 or not re.fullmatch(r"sha256=[a-fA-F0-9]{64}", signatures[0]):
            raise denied()
        secret = await self._secret(source, "appSecret")
        if not hmac.compare_digest(hmac.digest(secret, raw_body, hashlib.sha256), bytes.fromhex(signatures[0][7:])):
            raise denied()
        try:
            document = parse_source(raw_body, format="json").value
            events = normalize(source, document)
        except CatalogError:
            raise
        except Exception:
            raise invalid() from None
        return events, ProviderIngressResponse(media_type="text/plain", body="OK")

    async def persist(self, tx: Transaction, source: ProviderSource, events: tuple[ProviderEvent, ...]) -> None:
        await self.statuses.persist(tx, source, events)
