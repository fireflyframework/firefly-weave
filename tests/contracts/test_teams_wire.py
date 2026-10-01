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
"""Independent expected Teams REST path shapes and native administrative parity."""

from click.testing import CliRunner


def test_teams_send_url_encodes_ids_and_preserves_base_path():
    from firefly_weave.providers.teams.transport import activity_url

    assert (
        activity_url("https://smba.trafficmanager.net/emea/", "a/b", "c?d")
        == "https://smba.trafficmanager.net/emea/v3/conversations/a%2Fb/activities/c%3Fd"
    )
    assert (
        activity_url("https://smba.trafficmanager.net/emea/", "a/b")
        == "https://smba.trafficmanager.net/emea/v3/conversations/a%2Fb/activities"
    )


def test_teams_reference_surface():
    from firefly_weave.cli.main import cli
    from firefly_weave.contracts.surface import OPERATIONS
    from firefly_weave.sdk.client import WeaveClient

    for action in ("read", "list", "revoke", "reactivate"):
        assert "teams_references." + action in OPERATIONS
    assert callable(getattr(WeaveClient, "reactivate_teams_reference", None))
    assert CliRunner().invoke(cli, ["teams-references", "--help"]).exit_code == 0


async def test_reply_and_proactive_real_local_wire_and_unknown_timeout(monkeypatch, signed_activity, caplog):
    import asyncio
    import json
    import logging
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    import httpx
    import pytest

    from firefly_weave.connections.machine_tokens import MachineTokenService
    from firefly_weave.connectors.egress import EgressPolicy, SecureHttpClient
    from firefly_weave.connectors.http import HttpPolicy
    from firefly_weave.connectors.teams import TeamsConnector
    from firefly_weave.contracts.connectors import (
        ActionContext,
        ConnectionRevision,
        ConnectorFailure,
        ConnectorInvocation,
        ResolvedSecret,
    )
    from firefly_weave.contracts.teams import TeamsReference

    caplog.set_level(logging.DEBUG)
    records = []
    mode = "success"
    handlers = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            first, *lines = header.decode().split("\r\n")
            headers = dict(line.split(": ", 1) for line in lines if ": " in line)
            size = int(next((v for k, v in headers.items() if k.lower() == "content-length"), "0"))
            body = await reader.readexactly(size)
            records.append((first, headers, body))
            if "/oauth2/v2.0/token" in first:
                response = {"access_token": "fixture-bearer", "token_type": "Bearer", "expires_in": 3600}
            elif mode == "disconnect":
                return
            else:
                response = {"id": "provider-acceptance"}
            raw = json.dumps(response).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nX-Echo: fixture-bearer fixture-client-secret\r\n"
                b"Content-Type: application/json\r\nContent-Length: "
                + str(len(raw)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + raw
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    closed = []

    class LocalTokenTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            assert request.url.host == "login.microsoftonline.com"
            mapped = httpx.Request(
                request.method,
                base + request.url.raw_path.decode(),
                headers=request.headers,
                content=request.content,
                extensions=request.extensions,
            )
            return await self.inner.handle_async_request(mapped)

        async def aclose(self):
            await self.inner.aclose()
            closed.append(True)

    monkeypatch.setattr(
        "firefly_weave.connections.machine_tokens.PinnedTransport", lambda *a, **k: LocalTokenTransport()
    )

    class LocalSendClient:
        async def request_bounded(self, method, url, **kwargs):
            assert url.startswith("https://smba.trafficmanager.net/emea/")
            kwargs["egress_policy"] = EgressPolicy((base,), ("127.0.0.0/8",))
            return await SecureHttpClient().request_bounded(method, base + httpx.URL(url).raw_path.decode(), **kwargs)

    data, _, _, _ = signed_activity
    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="teams",
        connector_version_id=uuid4(),
        connector="teams",
        connector_digest="a" * 64,
        adapter="weave-teams",
        config=data,
        secretRef={"client_secret": "handle"},
        allowed_destinations=("https://login.microsoftonline.com", "https://smba.trafficmanager.net"),
    )
    reference = TeamsReference(
        id=uuid4(),
        source_id=uuid4(),
        connection_revision_id=connection.id,
        generation=1,
        state="active",
        service_url="https://smba.trafficmanager.net/emea/",
        conversation_id=data["conversation_id"],
        bot_id=data["bot_id"],
        user_id=data["user_id"],
        account_id=data["account_id"],
        tenant_id=data["tenant_id"],
    )
    authorized = []

    async def authority():
        authorized.append(True)

    async def resolve(identifier, generation, activity_id):
        assert (identifier, generation) == (reference.id, 1)
        return reference.model_dump(mode="json")

    async def credentials(slot):
        assert slot == "client_secret"
        return ResolvedSecret(value="fixture-client-secret")

    def context(action):
        return ActionContext(
            "fixture-operation",
            datetime.now(UTC) + timedelta(seconds=5),
            credentials,
            ConnectorInvocation(connection, {}, action, {}, {}, 1048576, 65536),
            authority,
            resolve,
        )

    connector = TeamsConnector(LocalSendClient(), MachineTokenService(HttpPolicy()))
    inputs = {"reference_id": str(reference.id), "generation": 1, "text": "hello"}
    try:
        assert await connector.execute({**inputs, "activity_id": "activity?1"}, context("reply")) == {
            "id": "provider-acceptance",
            "status": "accepted",
        }
        assert await connector.execute(inputs, context("send")) == {"id": "provider-acceptance", "status": "accepted"}
        sends = [r for r in records if "/activities" in r[0]]
        assert sends[0][0] == "POST /emea/v3/conversations/conv%2F1/activities/activity%3F1 HTTP/1.1"
        assert sends[1][0] == "POST /emea/v3/conversations/conv%2F1/activities HTTP/1.1"
        assert json.loads(sends[0][2]) == {
            "type": "message",
            "text": "hello",
            "textFormat": "plain",
            "from": {"id": "28:bot"},
            "recipient": {"id": "29:user"},
            "conversation": {"id": "conv/1"},
            "replyToId": "activity?1",
        }
        assert any(k.lower() == "authorization" and v == "Bearer fixture-bearer" for k, v in sends[0][1].items())
        assert len(closed) == 2
        mode = "disconnect"
        with pytest.raises(ConnectorFailure) as failure:
            await connector.execute(inputs, context("send"))
        assert failure.value.outcome == "unknown"
        assert len([r for r in records if "/activities" in r[0]]) == 3
        assert len(closed) == 3
        assert "fixture-bearer" not in caplog.text and "fixture-client-secret" not in caplog.text
        logging.getLogger("httpx").info("unrelated-after-teams")
        assert "unrelated-after-teams" in caplog.text
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)
