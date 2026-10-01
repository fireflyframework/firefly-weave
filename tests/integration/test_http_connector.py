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

"""Hermetic HTTP acceptance using actual loopback sockets; no Internet endpoints."""

import asyncio
from contextlib import asynccontextmanager

import pytest

pytestmark = pytest.mark.integration


@asynccontextmanager
async def server(response, *, host="127.0.0.1"):
    received = []

    async def handle(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            received.append(request)
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(handle, host, 0)
    try:
        yield f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}", received
    finally:
        listener.close()
        await listener.wait_closed()


async def test_http_requires_explicit_private_destination():
    from firefly_weave.connectors.egress import EgressDenied, EgressPolicy, SecureHttpClient

    async with server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}") as (url, received):
        client = SecureHttpClient()
        with pytest.raises(EgressDenied):
            await client.request_bounded("GET", url, max_response_bytes=20, egress_policy=EgressPolicy((url,)))
        assert received == []
        response = await client.request_bounded(
            "GET", url, max_response_bytes=20, egress_policy=EgressPolicy((url,), ("127.0.0.0/8",))
        )
        assert response.json() == {}
        assert len(received) == 1


async def test_stream_limit_and_compression_fail_closed():
    from pyfly.client.exceptions import ResponseTooLargeException, UnsupportedContentEncodingException

    from firefly_weave.connectors.egress import EgressPolicy, SecureHttpClient

    for headers, body, error in [
        (b"Transfer-Encoding: chunked", b"5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n", ResponseTooLargeException),
        (b"Content-Encoding: gzip\r\nContent-Length: 4", b"bomb", UnsupportedContentEncodingException),
    ]:
        async with server(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\n" + body) as (url, _):
            with pytest.raises(error):
                await SecureHttpClient().request_bounded(
                    "GET", url, max_response_bytes=6, egress_policy=EgressPolicy((url,), ("127.0.0.0/8",))
                )


@pytest.mark.parametrize("url", ["http://example.test:nope", "http://example.test:65536", "http://x:0"])
def test_invalid_ports_are_rejected(url):
    from firefly_weave.connectors.egress import EgressDenied, origin

    with pytest.raises(EgressDenied):
        origin(url)


async def test_declared_http_action_redirect_and_unknown_write():
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from firefly_weave.connectors.egress import SecureHttpClient
    from firefly_weave.connectors.http import HttpConnector, HttpPolicy
    from firefly_weave.contracts.connectors import (
        ActionContext,
        ConnectionRevision,
        ConnectorFailure,
        ConnectorInvocation,
    )

    async def credentials(slot):
        raise AssertionError("Anonymous request must not acquire credentials")

    async with server(b"HTTP/1.1 302 Found\r\nLocation: http://169.254.169.254/\r\nContent-Length: 0\r\n\r\n") as (
        url,
        received,
    ):
        connection = ConnectionRevision(
            id=uuid4(),
            revision=1,
            name="local",
            connector_version_id=uuid4(),
            connector="weave-http@1.0.0",
            connector_digest="a" * 64,
            adapter="weave-http",
            config={"baseUrl": url, "auth": "none"},
            allowed_destinations=(url,),
        )
        context = ActionContext(
            "operation",
            datetime.now(UTC) + timedelta(seconds=10),
            credentials,
            ConnectorInvocation(
                connection=connection,
                config={"method": "GET", "path": "/", "statuses": [200]},
                action="read",
                input_schema={"type": "object"},
                output_schema={},
                max_request_bytes=1024,
                max_response_bytes=1024,
            ),
        )
        connector = HttpConnector(SecureHttpClient(), HttpPolicy(private_networks=("127.0.0.0/8",)))
        with pytest.raises(ConnectorFailure) as error:
            await connector.execute({}, context)
        assert error.value.code == "HTTP_DESTINATION"
        assert len(received) == 1


async def test_dns_pin_keeps_original_host_and_resolves_once():
    from firefly_weave.connectors.egress import EgressPolicy, SecureHttpClient

    async with server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}") as (local, received):
        url = local.replace("127.0.0.1", "rebind.test")
        resolutions = []

        async def rebinding(host, port):
            resolutions.append(host)
            return ("127.0.0.1",) if len(resolutions) == 1 else ("169.254.169.254",)

        response = await SecureHttpClient().request_bounded(
            "GET", url, max_response_bytes=20, egress_policy=EgressPolicy((url,), ("127.0.0.0/8",)), resolver=rebinding
        )
        assert response.status_code == 200
        assert resolutions == ["rebind.test"]
        assert b"Host: rebind.test:" in received[0]


async def test_real_tls_checks_original_hostname(tmp_path):
    import ssl
    from datetime import UTC, datetime, timedelta

    import httpcore
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from firefly_weave.connectors.egress import EgressPolicy, SecureHttpClient

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local.test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("local.test")]), False)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    tls_server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_server.load_cert_chain(certfile, keyfile)
    tls_client = ssl.create_default_context(cafile=str(certfile))
    writes = []

    async def handler(reader, writer):
        try:
            writes.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    listener = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=tls_server)
    port = listener.sockets[0].getsockname()[1]

    async def local_dns(host, port):
        return ("127.0.0.1",)

    try:
        url = f"https://local.test:{port}"
        response = await SecureHttpClient().request_bounded(
            "GET",
            url,
            max_response_bytes=20,
            tls=tls_client,
            egress_policy=EgressPolicy((url,), ("127.0.0.0/8",)),
            resolver=local_dns,
        )
        assert response.status_code == 200
        assert b"Host: local.test:" in writes[0]
        wrong = f"https://wrong.test:{port}"
        with pytest.raises(httpcore.ConnectError):
            await SecureHttpClient().request_bounded(
                "GET",
                wrong,
                max_response_bytes=20,
                tls=tls_client,
                egress_policy=EgressPolicy((wrong,), ("127.0.0.0/8",)),
                resolver=local_dns,
            )
        assert len(writes) == 1
    finally:
        listener.close()
        await listener.wait_closed()


async def test_policy_isolation_and_metadata_deny():
    from firefly_weave.connectors.egress import EgressDenied, EgressPolicy, SecureHttpClient

    async with server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}") as (url, received):
        client = SecureHttpClient()
        await client.request_bounded(
            "GET", url, max_response_bytes=20, egress_policy=EgressPolicy((url,), ("127.0.0.0/8",))
        )
        with pytest.raises(EgressDenied):
            await client.request_bounded("GET", url, max_response_bytes=20, egress_policy=EgressPolicy((url,)))
        assert len(received) == 1
    for address in ("169.254.169.254", "100.100.100.200", "168.63.129.16", "0.0.0.0", "224.0.0.1"):
        with pytest.raises(EgressDenied):
            EgressPolicy((f"http://{address}",), ("0.0.0.0/0",)).validate(f"http://{address}", (address,))


def invocation_context(url, *, method="GET", timeout=2, token=None):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from firefly_weave.contracts.connectors import (
        ActionContext,
        ConnectionRevision,
        ConnectorInvocation,
        ResolvedSecret,
    )

    async def credentials(slot):
        assert slot == "token" and token is not None
        return ResolvedSecret(value=token)

    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="local",
        connector_version_id=uuid4(),
        connector="weave-http@1.0.0",
        connector_digest="a" * 64,
        adapter="weave-http",
        config={"baseUrl": url, "auth": "bearer" if token else "none"},
        allowed_destinations=(url,),
    )
    return ActionContext(
        "operation",
        datetime.now(UTC) + timedelta(seconds=timeout),
        credentials,
        ConnectorInvocation(
            connection,
            {"method": method, "path": "/", "statuses": [200]},
            "read" if method == "GET" else "write",
            {},
            {},
            1024,
            1024,
        ),
    )


async def test_response_timeout_after_write_is_ambiguous_and_not_retried():
    from firefly_weave.connectors.egress import SecureHttpClient
    from firefly_weave.connectors.http import HttpConnector, HttpPolicy
    from firefly_weave.contracts.connectors import ConnectorFailure

    effects = []
    done = asyncio.Event()

    async def handler(reader, writer):
        try:
            effects.append(await reader.readuntil(b"\r\n\r\n"))
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            done.set()

    listener = await asyncio.start_server(handler, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    try:
        connector = HttpConnector(SecureHttpClient(), HttpPolicy(("127.0.0.0/8",)))
        with pytest.raises(ConnectorFailure) as error:
            await connector.execute({"body": {"write": True}}, invocation_context(url, method="POST", timeout=0.15))
        assert error.value.code == "TIMEOUT"
        assert error.value.outcome == "unknown"
        await asyncio.wait_for(done.wait(), 1)
        assert len(effects) == 1
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.parametrize(
    "echo", [b'{"echo":"never-persist-credential"}', b'{"nested":[{"never-persist-credential":"safe"}]}']
)
async def test_secret_echo_and_response_headers_never_escape(echo):
    from firefly_weave.connectors.egress import SecureHttpClient
    from firefly_weave.connectors.http import HttpConnector, HttpPolicy
    from firefly_weave.contracts.connectors import ConnectorFailure

    token = "never-persist-credential"
    body = echo
    response = (
        b"HTTP/1.1 200 OK\r\nAuthorization: canary-header\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    async with server(response) as (url, received):
        connector = HttpConnector(SecureHttpClient(), HttpPolicy(("127.0.0.0/8",)))
        with pytest.raises(ConnectorFailure) as error:
            await connector.execute({}, invocation_context(url, token=token))
        assert token not in str(error.value)
        assert b"Authorization: Bearer never-persist-credential" in received[0]


async def test_connected_peer_checked_before_any_http_write():
    import httpcore

    from firefly_weave.connectors.egress import EgressDenied, EgressPolicy, PinnedBackend

    received = []
    closed = asyncio.Event()

    async def handler(reader, writer):
        received.append(await reader.read())
        writer.close()
        await writer.wait_closed()
        closed.set()

    listener = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]

    class WrongPeer(httpcore.AsyncNetworkStream):
        def __init__(self, stream):
            self.stream = stream

        def get_extra_info(self, info):
            return ("169.254.169.254", port) if info == "server_addr" else self.stream.get_extra_info(info)

        async def aclose(self):
            await self.stream.aclose()

    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            return WrongPeer(await httpcore.AnyIOBackend().connect_tcp(host, port, timeout))

    url = f"http://127.0.0.1:{port}"
    try:
        backend = PinnedBackend(EgressPolicy((url,), ("127.0.0.0/8",)), url, backend=Backend())
        with pytest.raises(EgressDenied):
            await backend.connect_tcp("127.0.0.1", port, 1)
        await asyncio.wait_for(closed.wait(), 1)
        assert received == [b""]
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.parametrize(
    "host", ["metadata.google.internal.", "metadata.", "api.default.svc.", "api.default.svc.cluster.local."]
)
def test_root_dot_cannot_bypass_unconditional_host_denies(host):
    from firefly_weave.connectors.egress import EgressDenied, EgressPolicy

    url = "https://" + host
    with pytest.raises(EgressDenied):
        EgressPolicy((url,), ("10.0.0.0/8",)).validate(url, ("10.1.2.3",))


@pytest.mark.parametrize("token", ['quoted"credential', r"backslash\credential"])
async def test_escaped_bearer_credential_is_rejected_before_network(token):
    import json

    from firefly_weave.connectors.egress import SecureHttpClient
    from firefly_weave.connectors.http import HttpConnector, HttpPolicy
    from firefly_weave.contracts.connectors import ConnectorFailure

    body = json.dumps({"nested": [{token: token}]}).encode()
    response = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    async with server(response) as (url, received):
        connector = HttpConnector(SecureHttpClient(), HttpPolicy(("127.0.0.0/8",)))
        with pytest.raises(ConnectorFailure) as error:
            await connector.execute({}, invocation_context(url, token=token))
        assert error.value.code == "HTTP_AUTH"
        assert error.value.outcome == "not_started"
        assert token not in str(error.value)
        assert received == []


@pytest.mark.parametrize("stage", ["input", "output"])
async def test_named_schema_references_enforce_saved_payload_contract(stage):
    from dataclasses import replace

    from firefly_weave.connectors.egress import SecureHttpClient
    from firefly_weave.connectors.http import HttpConnector, HttpPolicy
    from firefly_weave.contracts.connectors import ConnectorFailure

    async with server(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}") as (url, received):
        context = invocation_context(url)
        invocation = replace(
            context.invocation,
            input_schema={"$ref": "request"},
            output_schema={"$ref": "response"},
            schema_bundle={
                "request": {"type": "object", **({"required": ["query"]} if stage == "input" else {})},
                "response": {"type": "object", "required": ["missing"]},
            },
        )
        connector = HttpConnector(SecureHttpClient(), HttpPolicy(("127.0.0.0/8",)))
        with pytest.raises(ConnectorFailure) as error:
            await connector.execute({}, replace(context, invocation=invocation))
        assert error.value.code == ("HTTP_INPUT" if stage == "input" else "HTTP_OUTPUT")
        assert len(received) == (0 if stage == "input" else 1)
