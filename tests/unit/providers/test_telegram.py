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

"""Telegram authentication, exact resource policy and deterministic update identity."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ConnectionRevision, ResolvedSecret
from firefly_weave.contracts.providers import ProviderSource
from firefly_weave.definitions.models import CatalogError

SECRET = "e5_fixture_webhook_secret"


def setup_source():
    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="telegram",
        connector_version_id=uuid4(),
        connector="weave-telegram@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-telegram",
        config={"account_id": "123456", "mode": "webhook", "allowed_chat_ids": ["-987", "42"]},
        secretRef={"botToken": "bot-token", "webhookSecret": "webhook-secret"},
        allowed_destinations=("https://api.telegram.org",),
    )
    source = ProviderSource(
        id=uuid4(),
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        binding_id=uuid4(),
        principal_id=uuid4(),
        name="telegram",
        provider="telegram",
        package="firefly-weave",
        package_version="0.1.0a1",
        adapter_version="1.0.0",
        schema_digest="a" * 64,
        connection_revision_id=connection.id,
        policy=connection.config,
        kind="run",
        activation_id=uuid4(),
    )
    return source, connection


def update(**changes):
    return {
        "update_id": 73,
        "message": {
            "message_id": 9,
            "date": 1790800000,
            "chat": {"id": -987, "type": "supergroup"},
            "from": {"id": 42, "is_bot": False},
            "text": "hello",
        },
        **changes,
    }


@pytest.fixture
def verifier():
    from firefly_weave.providers.telegram import TelegramVerifier

    source, connection = setup_source()
    resolved = []

    class Credentials:
        async def resolve(self, current, *, slots=None):
            assert current == source
            resolved.append(slots)
            assert slots == ("webhookSecret",)
            return connection, {"webhookSecret": ResolvedSecret(value=SECRET)}

    return TelegramVerifier(Credentials()), source, connection, resolved


async def receive(fixture, document=None, headers=None, raw=None):
    verifier, source, _, _ = fixture
    return await verifier.verify(
        source,
        raw if raw is not None else json.dumps(document or update()).encode(),
        headers if headers is not None else {"X-Telegram-Bot-Api-Secret-Token": SECRET},
        datetime.now(UTC),
    )


async def test_original_text_has_update_identity_and_deterministic_fingerprint(verifier):
    first, ack = await receive(verifier)
    again, _ = await receive(verifier, raw=json.dumps(update(), indent=4).encode())
    assert ack.status_code == 200 and ack.body == "{}"
    assert len(first) == 1 and first[0].event_id == "bot:123456:update:73"
    assert first[0].kind == "telegram-update" and first[0].disposition == "dispatch"
    assert first[0].payload["chat_id"] == "-987" and first[0].payload["text"] == "hello"
    assert first[0].fingerprint == again[0].fingerprint
    assert verifier[3] == [("webhookSecret",), ("webhookSecret",)]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-telegram-bot-api-secret-token": "wrong"},
        {"x-telegram-bot-api-secret-token": SECRET, "X-Telegram-Bot-Api-Secret-Token": SECRET},
        *[
            {"x-telegram-bot-api-secret-token": value}
            for value in ["", SECRET + "," + SECRET, " " + SECRET, SECRET + "\n", "é", "a" * 257]
        ],
    ],
)
async def test_security_header_denial_precedes_invalid_json(verifier, headers):
    with pytest.raises(CatalogError) as error:
        await receive(verifier, headers=headers, raw=b"not-json")
    assert error.value.code == "WV-PROVIDER-AUTH"
    assert SECRET not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"update_id":true,"message":{}}',
        b'{"update_id":1,"update_id":2,"message":{}}',
        b'{"update_id":1,"message":{},"edited_message":{}}',
        b'{"update_id":1,"message":{"date":NaN}}',
        b"[]",
        b"\xff",
        b"[" * 200 + b"]" * 200,
    ],
)
async def test_malformed_envelopes_do_not_ack(verifier, raw):
    with pytest.raises(CatalogError) as error:
        await receive(verifier, raw=raw)
    assert error.value.code == "WV-PROVIDER-PAYLOAD"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"from": {"id": 123456, "is_bot": True}}, "bot_message"),
        ({"from": {"id": 88, "is_bot": True}}, "bot_message"),
        ({"sender_chat": {"id": -987}}, "unsupported_message"),
        ({"message_thread_id": 1}, "unsupported_message"),
        ({"ephemeral_message_id": 1}, "unsupported_message"),
        ({"text": None, "photo": []}, "unsupported_message"),
    ],
)
async def test_bot_and_unsupported_message_dispositions(verifier, change, reason):
    doc = update()
    doc["message"].update(change)
    events, _ = await receive(verifier, doc)
    assert events[0].disposition == "ignore" and events[0].reason == reason
    assert events[0].payload["text"] is None


async def test_edits_and_other_classes_keep_one_kind_and_distinct_update_identity(verifier):
    original, _ = await receive(verifier)
    events, _ = await receive(verifier, {"update_id": 74, "edited_message": update()["message"]})
    assert events[0].reason == "edited_message" and events[0].event_id == "bot:123456:update:74"
    assert events[0].kind == original[0].kind
    changed, _ = await receive(verifier, {"update_id": 73, "edited_message": update()["message"]})
    assert changed[0].event_id == original[0].event_id
    assert changed[0].fingerprint != original[0].fingerprint
    unknown, _ = await receive(verifier, {"update_id": 72, "future_class": {"sensitive": "not-retained"}})
    assert unknown[0].disposition == "ignore"
    assert "not-retained" not in unknown[0].model_dump_json()


async def test_wrong_chat_and_secret_echo_are_denied(verifier):
    for change in [{"chat": {"id": -999, "type": "group"}}, {"text": SECRET}]:
        doc = update()
        doc["message"].update(change)
        with pytest.raises(CatalogError):
            await receive(verifier, doc)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.config.update(mode="poll"),
        lambda c: c.config.update(account_id="123456/evil"),
        lambda c: c.config.update(allowed_chat_ids=["01"]),
        lambda c: c.secret_refs.update(webhookSecret="bot-token"),
        lambda c: c.config.update(baseUrl="https://attacker.test"),
    ],
)
def test_connection_policy_rejects_unsafe_configuration(mutation):
    from firefly_weave.providers.telegram import validate_telegram_connection

    _, connection = setup_source()
    mutation(connection)
    with pytest.raises(CatalogError):
        validate_telegram_connection(connection)


def test_source_policy_exact_values_cannot_narrow_or_change_bot(verifier):
    service, source, connection, _ = verifier
    service.validate_source(source, connection)
    for policy in [{**source.policy, "account_id": "9"}, {**source.policy, "allowed_chat_ids": ["-987"]}]:
        with pytest.raises(ValueError):
            service.validate_source(source.model_copy(update={"policy": policy}), connection)


async def test_challenge_is_denied_without_resolving(verifier):
    with pytest.raises(CatalogError):
        await verifier[0].challenge(verifier[1], {"token": SECRET})
    assert verifier[3] == []


async def test_ephemeral_message_without_ordinary_message_id_is_explicitly_ignored(verifier):
    document = update()
    document["message"].pop("message_id")
    document["message"]["ephemeral_message_id"] = 6
    events, _ = await receive(verifier, document)
    assert events[0].disposition == "ignore" and events[0].reason == "unsupported_message"
    assert events[0].payload["message_id"] is None and events[0].payload["text"] is None
