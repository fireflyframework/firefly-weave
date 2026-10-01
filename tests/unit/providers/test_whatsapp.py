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
"""WhatsApp raw authentication, exact batch normalization and immutable identities."""

import copy
import hashlib
import hmac
import importlib
import importlib.util
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ConnectionRevision, ResolvedSecret
from firefly_weave.contracts.providers import ProviderSource
from firefly_weave.definitions.models import CatalogError


def module(name="providers.whatsapp"):
    assert importlib.util.find_spec("firefly_weave." + name), "WhatsApp implementation is missing"
    return importlib.import_module("firefly_weave." + name)


def config():
    return dict(
        app_id="100",
        account_id="200",
        phone_number_id="300",
        business_phone_number="15550000000",
        graph_version="v26.0",
        recipient_allowlist=["15550000001"],
        approved_templates=[dict(name="appointment", locale="en_US", body_parameters=1)],
    )


def source():
    return ProviderSource(
        name="whatsapp",
        provider="whatsapp",
        package="firefly-weave",
        package_version="0.1.0a1",
        adapter_version="1.0.0",
        schema_digest="a" * 64,
        connection_revision_id=uuid4(),
        policy=config(),
        kind="run",
        activation_id=uuid4(),
        id=uuid4(),
        scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        binding_id=uuid4(),
        principal_id=uuid4(),
    )


def message(identity="wamid.in", sender="15550000001"):
    return dict(id=identity, **{"from": sender}, timestamp="1700000000", type="text", text={"body": "Hello 🌍"})


def status(state="read", timestamp="1700000002"):
    return dict(id="wamid.out", status=state, timestamp=timestamp, recipient_id="15550000001")


def envelope(messages=None, statuses=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "200",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "300", "display_phone_number": "15550000000"},
                            "messages": messages if messages is not None else [message()],
                            "statuses": statuses or [],
                        },
                    }
                ],
            }
        ],
    }


class Credentials:
    def __init__(self):
        self.slots = []

    async def resolve(self, src, *, slots=None):
        self.slots.append(slots)
        assert slots in (("appSecret",), ("verifyToken",))
        return None, {
            slot: ResolvedSecret(value={"appSecret": "app-secret", "verifyToken": "verify-token"}[slot])
            for slot in slots
        }


async def verify(value, src=None, credentials=None, raw=None, signature=None):
    raw = raw if raw is not None else json.dumps(value, ensure_ascii=False).encode()
    signature = (
        signature if signature is not None else "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    )
    verifier = module().WhatsAppVerifier(credentials or Credentials(), None)
    return await verifier.verify(src or source(), raw, {"x-hub-signature-256": signature}, datetime.now(UTC))


async def test_all_entries_changes_messages_statuses_are_normalized():
    value = envelope(
        [message("m1"), message("m2")],
        [status("read"), status("sent", "1700000000"), status("delivered", "1700000001")],
    )
    value["entry"].append(envelope([message("m3")])["entry"][0])
    value["entry"][1]["changes"].append(envelope([message("m4")])["entry"][0]["changes"][0])
    credentials = Credentials()
    events, ack = await verify(value, credentials=credentials)
    assert len(events) == 7 and ack.status_code == 200
    assert [e.payload["status"] for e in events if e.kind == "whatsapp-status"] == ["read", "sent", "delivered"]
    assert len({e.event_id for e in events}) == 7
    assert credentials.slots == [("appSecret",)]


async def test_challenge_uses_only_distinct_verify_token():
    credentials = Credentials()
    verifier = module().WhatsAppVerifier(credentials, None)
    query = {"hub.mode": "subscribe", "hub.verify_token": "verify-token", "hub.challenge": "123456"}
    response = await verifier.challenge(source(), query)
    assert response.body == "123456" and response.media_type == "text/plain"
    assert credentials.slots == [("verifyToken",)]
    for bad in ("app-secret", "access-token", ""):
        with pytest.raises(CatalogError):
            await verifier.challenge(source(), query | {"hub.verify_token": bad})


@pytest.mark.parametrize(
    "signature",
    ["", "sha1=" + "a" * 64, "sha256=" + "a" * 63, "sha256=" + "z" * 64, "sha256=" + "a" * 64 + ",sha256=" + "a" * 64],
)
async def test_malformed_signature_fails_before_json(signature):
    with pytest.raises(CatalogError) as failure:
        await verify(None, raw=b"not-json", signature=signature)
    assert failure.value.status == 401


async def test_signature_is_for_exact_raw_bytes():
    raw = json.dumps(envelope()).encode()
    signature = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    with pytest.raises(CatalogError) as failure:
        await verify(None, raw=raw + b" ", signature=signature)
    assert failure.value.status == 401


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x["entry"][0].update(id="201"),
        lambda x: x["entry"][0]["changes"][0]["value"]["metadata"].update(phone_number_id="301"),
        lambda x: x.update(object="other"),
        lambda x: x["entry"][0]["changes"][0].update(field="other"),
    ],
)
async def test_mixed_installation_or_unsupported_envelope_rejects_whole_batch(mutate):
    value = envelope()
    value["entry"].append(copy.deepcopy(value["entry"][0]))
    mutate(value)
    with pytest.raises(CatalogError):
        await verify(value)


async def test_reordering_batch_and_replacing_source_do_not_change_event_facts():
    src = source()
    value = envelope([message("one"), message("two")])
    first, _ = await verify(value, src)
    value["entry"][0]["changes"][0]["value"]["messages"].reverse()
    replacement = src.model_copy(update={"id": uuid4(), "connection_revision_id": uuid4()})
    second, _ = await verify(value, replacement)
    assert {e.event_id: e.fingerprint for e in first} == {e.event_id: e.fingerprint for e in second}


async def test_unsupported_and_self_messages_are_durably_ignored():
    image = message("image") | {"type": "image", "image": {"id": "media-id"}}
    image.pop("text")
    events, _ = await verify(envelope([image, message("self", "15550000000")]))
    assert [(e.disposition, e.reason) for e in events] == [
        ("ignore", "unsupported_message_type"),
        ("ignore", "self_message"),
    ]
    assert "media-id" not in events[0].model_dump_json()


async def test_limits_and_duplicate_json_keys_are_rejected():
    with pytest.raises(CatalogError) as failure:
        await verify(envelope([message(str(i)) for i in range(101)]))
    assert failure.value.status == 413
    with pytest.raises(CatalogError):
        await verify(None, raw=b'{"object":"whatsapp_business_account","object":"whatsapp_business_account"}')


def test_static_both_dispatch_schemas_map_and_text_only_mapping_is_incompatible():
    from firefly_weave.compiler.expression_types import infer_expression
    from firefly_weave.compiler.typecheck import check_compatibility

    contracts = module("contracts.whatsapp")
    for kind in ("whatsapp-message", "whatsapp-status"):
        schema = contracts.event_schemas()[kind]
        inferred = infer_expression({"ref": "/payload"}, {"payload": schema})
        assert check_compatibility(inferred.schema, contracts.event_target_schema(), {}) == "compatible"
    inferred = infer_expression({"ref": "/payload/text"}, {"payload": contracts.event_schemas()["whatsapp-status"]})
    assert check_compatibility(inferred.schema, {"type": "string"}, {}) == "incompatible"


def test_connection_rejects_shared_secret_handles_and_unknown_keys():
    contracts = module("contracts.whatsapp")
    from firefly_weave.contracts.connectors import ConnectionRequest

    request = ConnectionRequest(
        name="test",
        connector_version_id=uuid4(),
        config=config(),
        secretRef={"accessToken": "access", "appSecret": "app", "verifyToken": "verify"},
        allowed_destinations=("https://graph.facebook.com",),
    )
    contracts.validate_connection(request)
    with pytest.raises(ValueError):
        contracts.validate_connection(
            request.model_copy(
                update={"secret_refs": {"accessToken": "same", "appSecret": "same", "verifyToken": "verify"}}
            )
        )
    with pytest.raises(ValueError):
        contracts.validate_connection(
            request.model_copy(update={"config": config() | {"baseUrl": "https://evil.invalid"}})
        )


@pytest.mark.parametrize("slot,handle", [("verifyToken", "verify"), ("appSecret", "app")])
async def test_selected_slot_resolution_preserves_authority_and_does_not_touch_other_provider(slot, handle):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from firefly_weave.connections.secrets import SecretUnavailable
    from firefly_weave.connections.source_bindings import SourceBindingService

    src = source()
    revision = ConnectionRevision(
        id=src.connection_revision_id,
        revision=1,
        name="wa",
        connector_version_id=uuid4(),
        connector="wa",
        connector_digest="a" * 64,
        adapter="weave-whatsapp",
        config=config(),
        secretRef={"verifyToken": "verify", "appSecret": "app", "accessToken": "access"},
    )
    called = []

    @asynccontextmanager
    async def open_tx(scope, *, mutation):
        assert mutation is False
        yield None

    def resolve(scope, selected_handle):
        called.append(selected_handle)
        if selected_handle != handle:
            raise AssertionError("Unrelated provider was touched")
        return ResolvedSecret(value="verify-token")

    service = SourceBindingService(
        SimpleNamespace(uow=SimpleNamespace(open=open_tx), secrets=SimpleNamespace(resolve=resolve))
    )
    checked = []

    async def check(tx, identifier):
        checked.append(identifier)
        return "binding", revision

    async def source_check(tx):
        return src.binding_id

    service.check = check
    _, values = await service.resolve(src.scope, src.binding_id, source_check, slots=(slot,))
    assert values[slot].value == "verify-token" and called == [handle] and len(checked) == 2
    for slots in ((), ("missing",), ("verifyToken", "verifyToken"), ["verifyToken"], "verifyToken"):
        with pytest.raises(SecretUnavailable):
            await service.resolve(src.scope, src.binding_id, source_check, slots=slots)
    assert called == [handle]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x["entry"].append({"id": "200", "changes": []}),
        lambda x: x["entry"][0]["changes"][0]["value"]["messages"][0].update(timestamp=1700000000),
        lambda x: x["entry"][0]["changes"][0]["value"]["messages"][0].update(timestamp="01700000000"),
        lambda x: x["entry"][0]["changes"][0]["value"]["messages"][0].update(timestamp="999999999999"),
        lambda x: x["entry"][0]["changes"][0]["value"].update(messages="not-an-array"),
        lambda x: x["entry"][0]["changes"][0]["value"].update(errors=[{"code": 123}]),
    ],
)
async def test_malformed_later_branch_or_timestamp_cannot_be_silently_acknowledged(mutation):
    value = envelope()
    mutation(value)
    with pytest.raises(CatalogError):
        await verify(value)


async def test_duplicate_header_casing_is_denied_by_verifier_itself():
    raw = json.dumps(envelope()).encode()
    signature = "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()
    credentials = Credentials()
    with pytest.raises(CatalogError) as failure:
        await (
            module()
            .WhatsAppVerifier(credentials, None)
            .verify(
                source(), raw, {"x-hub-signature-256": signature, "X-Hub-Signature-256": signature}, datetime.now(UTC)
            )
        )
    assert failure.value.status == 401 and credentials.slots == []


async def test_unknown_status_is_ignored_and_recognized_errors_are_redacted():
    raw_status = status("future")
    events, _ = await verify(envelope([], [raw_status]))
    assert events[0].disposition == "ignore" and events[0].reason == "unsupported_status"
    events, _ = await verify(envelope([], [status("failed") | {"errors": [{"code": 123, "message": "SECRET-CANARY"}]}]))
    assert events[0].payload["error_codes"] == [123] and "SECRET-CANARY" not in events[0].model_dump_json()


async def test_status_timestamp_is_identity_and_modified_facts_are_conflicts():
    from firefly_weave.providers.admission import classify

    src = source()
    events, _ = await verify(envelope([], [status("read", "1700000000"), status("read", "1700000001")]), src)
    assert len({e.event_id for e in events}) == 2
    changed, _ = await verify(envelope([], [status("read", "1700000000") | {"errors": [{"code": 123}]}]), src)
    with pytest.raises(CatalogError) as failure:
        classify(changed, {(events[0].kind, events[0].event_id): events[0].fingerprint})
    assert failure.value.status == 409


@pytest.mark.parametrize(
    "patch",
    [
        {"graph_version": "latest"},
        {"graph_version": "v26.0/300"},
        {"recipient_allowlist": ["+15550000001"]},
        {"approved_templates": [{"name": "appointment", "locale": "en_US", "body_parameters": 1, "components": []}]},
        {"phone_number_id": 300},
    ],
)
def test_connection_profile_rejects_dynamic_destinations_or_unbounded_template_shape(patch):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        module("contracts.whatsapp").WhatsAppConfig.model_validate(config() | patch)


def test_disabled_provider_is_not_imported_by_core_app_composition():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """import sys
from firefly_weave.app import make_app
from firefly_weave.settings import Settings
app=make_app(Settings(scheduler_enabled=False, database_url="postgresql+asyncpg://unused:unused@127.0.0.1:65500/unused"))
assert "firefly_weave.providers.whatsapp" not in sys.modules
assert "firefly_weave.connectors.whatsapp" not in sys.modules
assert "firefly_weave.providers.whatsapp_status" not in sys.modules
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
