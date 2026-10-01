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
"""Fixed WhatsApp POST payloads and explicit unsafe-send outcomes."""

import importlib
import importlib.util
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from firefly_weave.contracts.connectors import (
    ActionContext,
    ConnectionRevision,
    ConnectorFailure,
    ConnectorInvocation,
    ResolvedSecret,
)


def connector():
    assert importlib.util.find_spec("firefly_weave.connectors.whatsapp"), "WhatsApp sends are missing"
    return importlib.import_module("firefly_weave.connectors.whatsapp")


class Transport:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response or httpx.Response(
            200, json={"messaging_product": "whatsapp", "messages": [{"id": "wamid.accepted"}]}
        )
        self.error = error

    async def request_bounded(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


def context(action="send-text", authorize=None):
    config = dict(
        app_id="100",
        account_id="200",
        phone_number_id="300",
        business_phone_number="15550000000",
        graph_version="v26.0",
        recipient_allowlist=["15550000001"],
        approved_templates=[dict(name="appointment", locale="en_US", body_parameters=1)],
    )
    revision = ConnectionRevision(
        id=uuid4(),
        revision=1,
        connector="whatsapp",
        connector_digest="a" * 64,
        adapter="weave-whatsapp",
        name="whatsapp",
        connector_version_id=uuid4(),
        config=config,
        secretRef={"accessToken": "access", "appSecret": "app", "verifyToken": "verify"},
        allowed_destinations=("https://graph.facebook.com",),
    )

    async def credentials(slot):
        assert slot == "accessToken"
        return ResolvedSecret(value="access-token")

    async def allowed():
        pass

    return ActionContext(
        "operation",
        datetime.now(UTC) + timedelta(seconds=10),
        credentials,
        ConnectorInvocation(revision, {}, action, {}, {}, 1048576, 65536),
        authorize or allowed,
    )


async def test_text_has_exact_fixed_path_and_safe_acceptance_projection():
    import json

    transport = Transport()
    result = (
        await connector().WhatsAppConnector(transport).execute({"recipient": "15550000001", "text": "hello"}, context())
    )
    assert result == {"message_id": "wamid.accepted", "acceptance": "accepted"}
    method, url, options = transport.calls[0]
    assert method == "POST" and url == "https://graph.facebook.com/v26.0/300/messages"
    assert json.loads(options["content"]) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15550000001",
        "type": "text",
        "text": {"preview_url": False, "body": "hello"},
    }
    assert len(transport.calls) == 1


async def test_template_is_configured_body_text_only():
    import json

    transport = Transport()
    await (
        connector()
        .WhatsAppConnector(transport)
        .execute(
            {"recipient": "15550000001", "name": "appointment", "locale": "en_US", "parameters": ["Tuesday"]},
            context("send-template"),
        )
    )
    body = json.loads(transport.calls[0][2]["content"])
    assert body["template"] == {
        "name": "appointment",
        "language": {"code": "en_US"},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": "Tuesday"}]}],
    }


@pytest.mark.parametrize(
    "input",
    [
        {"recipient": "15550000002", "text": "hello"},
        {"recipient": "15550000001", "text": "hello", "url": "https://evil.invalid"},
    ],
)
async def test_unapproved_recipient_or_destination_override_never_sends(input):
    transport = Transport()
    with pytest.raises(ConnectorFailure) as failure:
        await connector().WhatsAppConnector(transport).execute(input, context())
    assert failure.value.outcome == "not_started" and transport.calls == []


@pytest.mark.parametrize("status,outcome", [(429, "failed"), (500, "unknown"), (302, "unknown"), (408, "unknown")])
async def test_rejection_and_unknown_post_never_retry(status, outcome):
    transport = Transport(httpx.Response(status, json={"error": {"message": "access-token"}}))
    with pytest.raises(ConnectorFailure) as failure:
        await connector().WhatsAppConnector(transport).execute({"recipient": "15550000001", "text": "hello"}, context())
    assert failure.value.outcome == outcome and len(transport.calls) == 1
    assert "access-token" not in str(failure.value)


async def test_timeout_after_post_is_unknown_and_does_not_retry():
    transport = Transport(error=TimeoutError())
    with pytest.raises(ConnectorFailure) as failure:
        await connector().WhatsAppConnector(transport).execute({"recipient": "15550000001", "text": "hello"}, context())
    assert failure.value.outcome == "unknown" and len(transport.calls) == 1


async def test_credential_echo_acceptance_is_not_exposed():
    transport = Transport(
        httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": "access-token"}]})
    )
    with pytest.raises(ConnectorFailure) as failure:
        await connector().WhatsAppConnector(transport).execute({"recipient": "15550000001", "text": "hello"}, context())
    assert failure.value.outcome == "unknown" and "access-token" not in str(failure.value)


async def test_sdk_status_reads_use_scoped_identifiers_and_bounded_facts():
    from firefly_weave.contracts.access import Scope
    from firefly_weave.sdk.client import WeaveClient

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    source_id, state_id = uuid4(), uuid4()
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/facts"):
            return httpx.Response(200, json={"items": [], "next_cursor": None})
        return httpx.Response(
            200,
            json={
                "id": str(state_id),
                "installation_id": "a" * 64,
                "message_id": "wamid.out",
                "recipient": "15550000001",
                "progress": "read",
                "failed_seen": True,
                "deleted_seen": False,
                "fact_count": 2,
            },
        )

    async with WeaveClient(
        "https://weave.example", lambda: "token", scope, transport=httpx.MockTransport(handle)
    ) as sdk:
        assert hasattr(sdk, "whatsapp_delivery_state"), "WhatsApp typed reads are missing"
        result = await sdk.whatsapp_delivery_state(source_id, "wamid.out")
        assert result.progress == "read" and result.failed_seen
        page = await sdk.list_whatsapp_status_facts(source_id, state_id, limit=1)
        assert page.items == []
    assert seen[0].url.params["message_id"] == "wamid.out"
    assert f"/provider-sources/{source_id}/whatsapp-statuses/{state_id}/facts" in seen[1].url.path
    assert seen[1].url.params["limit"] == "1"


def test_native_status_openapi_and_cli_include_exact_read_parameters():
    from click.testing import CliRunner

    from firefly_weave.cli.main import cli
    from firefly_weave.contracts.openapi import export_openapi

    spec = export_openapi()
    operations = {
        operation["operationId"]: operation
        for path in spec["paths"].values()
        for method, operation in path.items()
        if method == "get"
    }
    assert "whatsapp_statuses.read" in operations, "WhatsApp native surface is missing"
    assert any(
        p["name"] == "message_id" and p["in"] == "query" and p["required"]
        for p in operations["whatsapp_statuses.read"]["parameters"]
    )
    result = CliRunner().invoke(cli, ["whatsapp-statuses", "facts", "--help"])
    assert result.exit_code == 0 and "--source" in result.output and "--state-id" in result.output


async def test_authority_revoked_during_credential_resolution_never_sends():
    transport = Transport()
    checks = []

    async def authorize():
        checks.append(True)
        if len(checks) > 1:
            raise ValueError("REVOKED-CANARY")

    with pytest.raises(ConnectorFailure) as failure:
        await (
            connector()
            .WhatsAppConnector(transport)
            .execute({"recipient": "15550000001", "text": "hello"}, context(authorize=authorize))
        )
    assert failure.value.outcome == "not_started" and transport.calls == []
    assert "REVOKED-CANARY" not in str(failure.value)


@pytest.mark.parametrize(
    "patch",
    [{"name": "unapproved"}, {"locale": "fr_FR"}, {"parameters": []}, {"components": []}, {"parameters": ["x" * 1025]}],
)
async def test_template_selection_must_match_exact_approved_name_locale_count(patch):
    transport = Transport()
    with pytest.raises(ConnectorFailure) as failure:
        await (
            connector()
            .WhatsAppConnector(transport)
            .execute(
                {"recipient": "15550000001", "name": "appointment", "locale": "en_US", "parameters": ["Tuesday"]}
                | patch,
                context("send-template"),
            )
        )
    assert failure.value.outcome == "not_started" and transport.calls == []


@pytest.mark.parametrize(
    "response",
    [
        {"messages": []},
        {"messaging_product": "whatsapp", "messages": [{"id": "one"}, {"id": "two"}]},
        {"messaging_product": "whatsapp", "messages": [{"id": "good"}], "unmodeled": {"access-token": "echo"}},
    ],
)
async def test_ambiguous_acceptance_or_nested_credential_echo_remains_unknown(response):
    transport = Transport(httpx.Response(200, json=response))
    with pytest.raises(ConnectorFailure) as failure:
        await connector().WhatsAppConnector(transport).execute({"recipient": "15550000001", "text": "hello"}, context())
    assert failure.value.outcome == "unknown" and len(transport.calls) == 1


async def test_utf8_encoded_request_budget_is_checked_before_auth_and_send():
    from dataclasses import replace

    transport = Transport()
    ctx = context()
    ctx = replace(ctx, invocation=replace(ctx.invocation, max_request_bytes=200))
    with pytest.raises(ConnectorFailure) as failure:
        await connector().WhatsAppConnector(transport).execute({"recipient": "15550000001", "text": "🌍" * 100}, ctx)
    assert failure.value.outcome == "not_started" and transport.calls == []


async def test_cancellation_after_post_starts_is_propagated_without_replay():
    import asyncio

    started = asyncio.Event()

    class HeldTransport(Transport):
        async def request_bounded(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            started.set()
            await asyncio.Event().wait()

    transport = HeldTransport()
    task = asyncio.create_task(
        connector().WhatsAppConnector(transport).execute({"recipient": "15550000001", "text": "hello"}, context())
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(transport.calls) == 1


async def test_missing_authority_or_remote_connection_test_does_not_send():
    from dataclasses import replace

    from firefly_weave.contracts.connectors import BoundConnection

    transport = Transport()
    ctx = replace(context(), authorize=None)
    with pytest.raises(ConnectorFailure) as failure:
        await connector().WhatsAppConnector(transport).execute({"recipient": "15550000001", "text": "hello"}, ctx)
    assert failure.value.outcome == "not_started"
    tested = (
        await connector()
        .WhatsAppConnector(transport)
        .test_connection(BoundConnection("test", ctx.invocation.connection, lambda name: None))
    )
    assert tested.ok is False and transport.calls == []


def test_cli_status_commands_preserve_ids_and_standard_error_exits(monkeypatch):
    from click.testing import CliRunner

    from firefly_weave.cli.main import cli
    from firefly_weave.contracts.public import Problem
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.errors import WeaveError

    source, state = uuid4(), uuid4()
    calls = []

    async def invoke(self, operation, **kwargs):
        calls.append((operation, kwargs))
        if len(calls) > 1:
            raise WeaveError(Problem(status=503, code="WV-UNAVAILABLE", message="Unavailable"))
        return {"items": [], "next_cursor": None}

    monkeypatch.setattr(WeaveClient, "invoke", invoke)
    args = [
        "whatsapp-statuses",
        "facts",
        "--source",
        str(source),
        "--state-id",
        str(state),
        "--limit",
        "1",
        "--base-url",
        "https://weave.example",
        "--tenant",
        str(uuid4()),
        "--project",
        str(uuid4()),
        "--environment",
        str(uuid4()),
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0 and '"items": []' in result.output
    assert calls[0][0] == "whatsapp_statuses.facts"
    assert (
        calls[0][1]["identifier"] == source
        and calls[0][1]["state_id"] == state
        and calls[0][1]["query"] == {"limit": 1}
    )
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 3 and "WV-UNAVAILABLE" in result.output


def test_checked_in_offline_examples_match_real_contracts():
    import json
    from pathlib import Path

    from firefly_weave.compiler.schemas import validate_schema
    from firefly_weave.contracts.connectors import ConnectionRequest
    from firefly_weave.contracts.whatsapp import TemplateInput, TextInput, validate_connection

    folder = Path("examples/connectors/whatsapp")
    validate_connection(ConnectionRequest.model_validate_json((folder / "connection.json").read_text()))
    TextInput.model_validate_json((folder / "text.input.json").read_text())
    TemplateInput.model_validate_json((folder / "template.input.json").read_text())
    assert not validate_schema(json.loads((folder / "event-target.schema.json").read_text()), {})
