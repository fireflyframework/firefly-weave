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

"""Recorded synthetic Telegram sends enforce exact wire and safe result semantics."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from firefly_weave.contracts.connectors import ActionContext, ConnectionRevision, ConnectorInvocation, ResolvedSecret

TOKEN = "123456:synthetic_fixture_only"


def action_context(action="send-text", *, token=TOKEN):
    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="tg",
        connector_version_id=uuid4(),
        connector="weave-telegram@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-telegram",
        config={"account_id": "123456", "mode": "webhook", "allowed_chat_ids": ["-987"]},
        secretRef={"botToken": "bot-token", "webhookSecret": "webhook-secret"},
        allowed_destinations=("https://api.telegram.org",),
    )

    async def credentials(slot):
        assert slot == "botToken"
        return ResolvedSecret(value=token)

    async def authorize():
        pass

    return ActionContext(
        "operation",
        datetime.now(UTC) + timedelta(seconds=5),
        credentials,
        ConnectorInvocation(connection, {}, action, {}, {}, 1048576, 65536),
        authorize,
    )


class RecordedClient:
    def __init__(self, response):
        self.response, self.requests = response, []

    async def request_bounded(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.response


def accepted(**changes):
    return {
        "ok": True,
        "result": {"message_id": 91, "chat": {"id": -987}, "from": {"id": 123456, "is_bot": True}, **changes},
    }


@pytest.mark.parametrize(
    "action,extra,reply",
    [
        ("send-text", {}, {}),
        (
            "reply-text",
            {"message_id": "9"},
            {"reply_parameters": {"message_id": 9, "allow_sending_without_reply": False}},
        ),
    ],
)
async def test_recorded_send_and_same_chat_reply(action, extra, reply):
    from firefly_weave.connectors.telegram import TelegramConnector

    client = RecordedClient(httpx.Response(200, json=accepted()))
    result = await TelegramConnector(client).execute(
        {"chat_id": "-987", "text": "hello", **extra}, action_context(action)
    )
    assert result == {"bot_id": "123456", "chat_id": "-987", "message_id": "91", "acceptance": "accepted"}
    assert len(client.requests) == 1
    method, url, kwargs = client.requests[0]
    assert method == "POST" and url == "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    assert json.loads(kwargs["content"]) == {
        "chat_id": -987,
        "text": "hello",
        "link_preview_options": {"is_disabled": True},
        **reply,
    }
    assert kwargs["max_response_bytes"] == 65536


@pytest.mark.parametrize(
    "status,body,outcome",
    [
        (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 9}}, "failed"),
        (403, {"ok": False, "error_code": 403}, "failed"),
        (500, {"ok": False, "error_code": 500}, "unknown"),
        (200, {}, "unknown"),
        (200, accepted(chat={"id": -999}), "unknown"),
        (200, accepted(text=TOKEN), "unknown"),
        (200, accepted(extra={TOKEN: "safe"}), "unknown"),
    ],
)
async def test_send_failures_never_return_raw_provider_material(status, body, outcome):
    from firefly_weave.connectors.telegram import TelegramConnector
    from firefly_weave.contracts.connectors import ConnectorFailure

    client = RecordedClient(httpx.Response(status, json=body))
    with pytest.raises(ConnectorFailure) as error:
        await TelegramConnector(client).execute({"chat_id": "-987", "text": "hello"}, action_context())
    assert error.value.outcome == outcome and TOKEN not in str(error.value)
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert len(client.requests) == 1


@pytest.mark.parametrize("token", ["999:wrong_bot", TOKEN + "/escape", TOKEN + "%2F", "bad", TOKEN + "\n"])
async def test_invalid_or_wrong_bot_token_cannot_send(token):
    from firefly_weave.connectors.telegram import TelegramConnector
    from firefly_weave.contracts.connectors import ConnectorFailure

    client = RecordedClient(httpx.Response(200, json=accepted()))
    with pytest.raises(ConnectorFailure) as error:
        await TelegramConnector(client).execute({"chat_id": "-987", "text": "hello"}, action_context(token=token))
    assert error.value.outcome == "not_started" and client.requests == []


def test_package_declares_native_catalog_and_one_bounded_event_schema():
    from firefly_weave.compiler.schemas import validate_schema
    from firefly_weave.connectors.telegram import package

    doc = package.metadata.model
    assert doc.manifest.spec.adapter == "weave-telegram"
    assert set(doc.manifest.spec.actions) == {"send-text", "reply-text"}
    assert doc.dispatch_event_kinds == ["telegram-update"]
    assert not validate_schema(doc.event_schemas["telegram-update"], {})


@pytest.mark.parametrize(
    "input",
    [
        {"chat_id": "-999", "text": "no"},
        {"chat_id": "-987", "text": ""},
        {"chat_id": "-987", "text": "a" * 4097},
        {"chat_id": "-987", "text": "hello", "url": "https://evil.test"},
        {"chat_id": "@somebody", "text": "hello"},
        {"chat_id": -987, "text": "hello"},
    ],
)
async def test_unsafe_inputs_never_reach_transport(input):
    from firefly_weave.connectors.telegram import TelegramConnector
    from firefly_weave.contracts.connectors import ConnectorFailure

    client = RecordedClient(httpx.Response(200, json=accepted()))
    with pytest.raises(ConnectorFailure) as error:
        await TelegramConnector(client).execute(input, action_context())
    assert error.value.outcome == "not_started" and client.requests == []


async def test_revocation_during_secret_resolution_stops_send():
    from dataclasses import replace

    from firefly_weave.connectors.telegram import TelegramConnector
    from firefly_weave.contracts.connectors import ConnectorFailure

    checks = []

    async def authorize():
        checks.append(True)
        if len(checks) == 2:
            raise ValueError(TOKEN)

    client = RecordedClient(httpx.Response(200, json=accepted()))
    with pytest.raises(ConnectorFailure) as error:
        await TelegramConnector(client).execute(
            {"chat_id": "-987", "text": "hello"}, replace(action_context(), authorize=authorize)
        )
    assert len(checks) == 2 and error.value.outcome == "not_started" and client.requests == []
    assert error.value.__context__ is None and TOKEN not in str(error.value)


@pytest.fixture
async def tls_wire(tmp_path):
    import asyncio
    import ssl
    from contextlib import asynccontextmanager, suppress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from firefly_weave.connectors.egress import EgressPolicy, SecureHttpClient

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.telegram.org")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.telegram.org")]), False)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "e5-cert.pem", tmp_path / "e5-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(certfile, keyfile)
    client_tls = ssl.create_default_context(cafile=str(certfile))

    @asynccontextmanager
    async def run(response):
        requests, tasks = [], set()
        written, closed = asyncio.Event(), asyncio.Event()

        async def handle(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = int(
                    next(
                        line.split(b":", 1)[1]
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                )
                body = await reader.readexactly(length)
                requests.append((head, body))
                written.set()
                if response is not None:
                    writer.write(response)
                    await writer.drain()
                else:
                    await reader.read()
            finally:
                writer.close()
                with suppress(ConnectionResetError):
                    await writer.wait_closed()
                tasks.discard(task)
                closed.set()

        listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_tls)
        local_origin = f"https://api.telegram.org:{listener.sockets[0].getsockname()[1]}"

        async def resolver(host, port):
            assert host == "api.telegram.org" and port == listener.sockets[0].getsockname()[1]
            return ("127.0.0.1",)

        class FixtureClient:
            async def request_bounded(self, method, url, **kwargs):
                assert url == "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
                assert kwargs["egress_policy"].allowed_origins == ("https://api.telegram.org",)
                kwargs.update(
                    tls=client_tls, resolver=resolver, egress_policy=EgressPolicy((local_origin,), ("127.0.0.0/8",))
                )
                return await SecureHttpClient().request_bounded(
                    method, url.replace("https://api.telegram.org", local_origin, 1), **kwargs
                )

        try:
            yield FixtureClient(), requests, written, closed
        finally:
            listener.close()
            await listener.wait_closed()
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tuple(tasks)), 3)

    return run


def wire_response(body, *, status=200, headers=b""):
    encoded = json.dumps(body).encode() if not isinstance(body, bytes) else body
    return (
        b"HTTP/1.1 "
        + str(status).encode()
        + b" Fixture\r\n"
        + headers
        + b"Content-Length: "
        + str(len(encoded)).encode()
        + b"\r\n\r\n"
        + encoded
    )


async def test_real_tls_send_contains_token_only_on_wire_not_logs_or_spans(tls_wire, caplog):
    import logging

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from firefly_weave.connectors.telegram import TelegramConnector

    caplog.set_level(logging.DEBUG)
    exporter = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    async with tls_wire(wire_response(accepted(), headers=b"X-Token-Echo: " + TOKEN.encode() + b"\r\n")) as (
        client,
        requests,
        _,
        _,
    ):
        with traces.get_tracer("e5-wire").start_as_current_span("send"):
            result = await TelegramConnector(client).execute({"chat_id": "-987", "text": "hello"}, action_context())
    assert result["message_id"] == "91"
    assert len(requests) == 1 and TOKEN.encode() in requests[0][0]
    assert json.loads(requests[0][1])["text"] == "hello"
    assert TOKEN not in caplog.text
    assert TOKEN not in repr([(span.attributes, span.events) for span in exporter.get_finished_spans()])
    traces.shutdown()


@pytest.mark.parametrize(
    "response,code",
    [
        (wire_response(b""), "TELEGRAM_FAILED"),
        (wire_response(b"x" * 65537), "TELEGRAM_RESPONSE_LIMIT"),
        (wire_response(accepted(text=TOKEN)), "TELEGRAM_SENSITIVE_RESPONSE"),
        (wire_response(accepted(), headers=b"Content-Encoding: gzip\r\n"), "TELEGRAM_RESPONSE_LIMIT"),
        (wire_response({}, status=302, headers=b"Location: https://evil.test/\r\n"), "TELEGRAM_RESPONSE"),
    ],
    ids=["empty", "oversized", "echo", "encoding", "redirect"],
)
async def test_real_tls_unknown_send_and_redacted_failure(tls_wire, caplog, response, code):
    import logging
    import traceback

    from firefly_weave.connectors.telegram import TelegramConnector
    from firefly_weave.contracts.connectors import ConnectorFailure

    caplog.set_level(logging.DEBUG)
    async with tls_wire(response) as (client, requests, _, _):
        with pytest.raises(ConnectorFailure) as error:
            await TelegramConnector(client).execute({"chat_id": "-987", "text": "hello"}, action_context())
    assert len(requests) == 1
    assert error.value.outcome == "unknown" and error.value.code == code
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert TOKEN not in caplog.text + "".join(traceback.format_exception(error.value))


async def test_real_tls_cancellation_after_write_preserves_unknown_boundary(tls_wire, caplog):
    import asyncio
    import logging

    from firefly_weave.connectors.telegram import TelegramConnector

    caplog.set_level(logging.DEBUG)
    async with tls_wire(None) as (client, requests, written, closed):
        task = asyncio.create_task(
            TelegramConnector(client).execute({"chat_id": "-987", "text": "hello"}, action_context())
        )
        await asyncio.wait_for(written.wait(), 2)
        task.cancel(TOKEN)
        with pytest.raises(asyncio.CancelledError) as error:
            await task
        await asyncio.wait_for(closed.wait(), 2)
        assert error.value.__context__ is None and TOKEN not in str(error.value)
    assert len(requests) == 1 and TOKEN not in caplog.text


async def test_real_tls_timeout_after_write_is_unknown_not_retried(tls_wire):
    from dataclasses import replace

    from firefly_weave.connectors.telegram import TelegramConnector
    from firefly_weave.contracts.connectors import ConnectorFailure

    async with tls_wire(None) as (client, requests, _, _):
        with pytest.raises(ConnectorFailure) as error:
            await TelegramConnector(client).execute(
                {"chat_id": "-987", "text": "hello"},
                replace(action_context(), attempt_deadline=datetime.now(UTC) + timedelta(seconds=0.2)),
            )
    assert len(requests) == 1 and error.value.outcome == "unknown" and error.value.code == "TELEGRAM_TIMEOUT"


async def test_normalized_message_identity_flows_directly_into_reply():
    from firefly_weave.connectors.telegram import TelegramConnector
    from firefly_weave.providers.telegram import TelegramConfig, normalize

    event = normalize(
        {
            "update_id": 73,
            "message": {
                "message_id": 9,
                "date": 1790800000,
                "chat": {"id": -987, "type": "group"},
                "from": {"id": 42, "is_bot": False},
                "text": "hello",
            },
        },
        TelegramConfig.model_validate(action_context().invocation.connection.config),
        "webhook_secret",
    )
    client = RecordedClient(httpx.Response(200, json=accepted()))
    output = await TelegramConnector(client).execute(
        {"chat_id": event.payload["chat_id"], "message_id": event.payload["message_id"], "text": "Received."},
        action_context("reply-text"),
    )
    assert output["message_id"] == "91"
    assert json.loads(client.requests[0][2]["content"])["reply_parameters"]["message_id"] == 9


def test_public_text_trigger_reply_example_compiles_and_evaluates():
    from runpy import run_path

    from firefly_weave.compiler.expressions import evaluate

    bundle = run_path("examples/telegram_text_reply.py")["documents"]()
    expression = bundle["workflow"]["spec"]["steps"][0]["with"]
    assert evaluate(expression, {"input": {"chat_id": "-987", "message_id": "9", "text": "hello"}}) == {
        "chat_id": "-987",
        "message_id": "9",
        "text": "Received.",
    }
    assert bundle["source"]["mapping"] == {"ref": "/payload"}


async def test_declared_offline_conformance_never_sends():
    from firefly_weave.connectors.telegram import TelegramConnector, package

    client = RecordedClient(httpx.Response(200, json=accepted()))
    await package.conformance(TelegramConnector(client))
    assert client.requests == []


@pytest.mark.parametrize(
    "message_id",
    ["0", "01", "+1", "-1", "2147483648", "1/evil", 9, True, "9\n", "9\r\n", "9 ", " 9", "9\t", "９", "9\u2028"],
)
async def test_reply_message_identity_is_canonical_and_bounded(message_id):
    from dataclasses import replace

    from firefly_weave.connectors.telegram import TelegramConnector
    from firefly_weave.contracts.connectors import ConnectorFailure

    client = RecordedClient(httpx.Response(200, json=accepted()))
    resolved = []

    async def credentials(slot):
        resolved.append(slot)
        return ResolvedSecret(value=TOKEN)

    with pytest.raises(ConnectorFailure) as error:
        await TelegramConnector(client).execute(
            {"chat_id": "-987", "text": "hello", "message_id": message_id},
            replace(action_context("reply-text"), credentials=credentials),
        )
    assert error.value.outcome == "not_started" and client.requests == [] and resolved == []


def test_disabled_provider_is_not_imported_by_native_scan():
    import os
    import subprocess
    import sys

    code = """
import importlib.abc
import sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"firefly_weave.connectors.telegram", "firefly_weave.providers.telegram"}:
            raise AssertionError("disabled Telegram imported")
sys.meta_path.insert(0, Deny())
from pyfly.container import Container
from pyfly.container.scanner import scan_package
from firefly_weave.app import SERVICE_PACKAGES
from firefly_weave.connections.registry import ConnectorRegistry
container = Container()
for module in SERVICE_PACKAGES:
    scan_package(module, container)
assert not ConnectorRegistry().packages
"""
    result = subprocess.run([sys.executable, "-c", code], env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_installed_declaration_is_selected_by_exact_operator_allowlist():
    from firefly_weave.connections.registry import ConnectorRegistry

    identity = "firefly-weave:weave-telegram:firefly_weave.connectors.telegram:package"
    selected = ConnectorRegistry((identity,)).packages
    assert len(selected) == 1 and selected[0].metadata.model.provider == "telegram"


@pytest.mark.parametrize("message_id", ["9\n", "9\r\n", "9 ", " 9", "9\t", "９", "9\u2028", "+9", "09"])
def test_reply_schema_rejects_noncanonical_original_string(message_id):
    from firefly_weave.compiler.schemas import validate_payload
    from firefly_weave.connectors.telegram import INPUT_SCHEMAS

    assert validate_payload(
        INPUT_SCHEMAS["reply-text"], {"chat_id": "-987", "text": "hello", "message_id": message_id}, {}
    )


@pytest.mark.parametrize("message_id", ["1", "2147483647"])
async def test_canonical_reply_bounds_keep_string_contract_and_integer_wire(message_id):
    from firefly_weave.connectors.telegram import TelegramConnector

    client = RecordedClient(httpx.Response(200, json=accepted()))
    output = await TelegramConnector(client).execute(
        {"chat_id": "-987", "text": "hello", "message_id": message_id}, action_context("reply-text")
    )
    assert output["acceptance"] == "accepted" and len(client.requests) == 1
    assert json.loads(client.requests[0][2]["content"])["reply_parameters"]["message_id"] == int(message_id)
