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

"""Ingress allocation and untrusted-header boundaries run before downstream parsing."""

import json

import pytest


async def invoke(app, chunks, *, path="/compiler/compile", headers=()):
    messages = iter(
        [{"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1} for i, chunk in enumerate(chunks)]
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    await app({"type": "http", "method": "POST", "path": path, "headers": list(headers)}, receive, send)
    return sent


async def test_chunked_overflow_is_rejected_before_downstream_parser():
    from firefly_weave.operations.transport import BodyBoundary, TransportPolicy

    calls = []

    async def downstream(scope, receive, send):
        calls.append(True)

    messages = await invoke(BodyBoundary(downstream, policy=TransportPolicy(json_bytes=8)), [b"12345", b"6789"])
    assert messages[0]["status"] == 413
    assert calls == []
    assert b"WV-REQUEST-LIMIT" in messages[-1]["body"]


@pytest.mark.parametrize(
    "headers",
    [
        ((b"content-length", b"9"),),
        ((b"content-length", b"2"), (b"Content-Length", b"2")),
        ((b"content-encoding", b"gzip"),),
    ],
)
async def test_unacceptable_headers_do_not_read_or_parse_body(headers):
    from firefly_weave.operations.transport import BodyBoundary, TransportPolicy

    async def forbidden(*args):
        pytest.fail("Rejected headers reached downstream or receive")

    sent = []

    async def send(message):
        sent.append(message)

    await BodyBoundary(forbidden, policy=TransportPolicy(json_bytes=8))(
        {"type": "http", "method": "POST", "path": "/compile", "headers": list(headers)}, forbidden, send
    )
    assert sent[0]["status"] in {400, 413, 415}


async def test_sanitized_headers_and_safe_unexpected_error():
    from uuid import UUID

    from firefly_weave.operations.transport import BodyBoundary

    observed = []

    async def downstream(scope, receive, send):
        observed.append(dict(scope["headers"]))
        raise RuntimeError("secret-canary-path-token")

    headers = tuple(
        (key, b"secret-canary-path-token")
        for key in (
            b"x-request-id",
            b"x-correlation-id",
            b"x-transaction-id",
            b"x-tenant-id",
            b"baggage",
            b"traceparent",
        )
    )
    messages = await invoke(BodyBoundary(downstream), [b"{}"], headers=headers)
    assert messages[0]["status"] == 500
    assert b"secret-canary" not in messages[-1]["body"]
    for key in (b"x-request-id", b"x-correlation-id", b"x-transaction-id"):
        UUID(observed[0][key].decode())
    assert all(key not in observed[0] for key in (b"x-tenant-id", b"baggage", b"traceparent"))


async def test_escaped_decoded_source_overflow_reaches_canonical_compiler():
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.operations.transport import BodyBoundary

    observed = []

    async def downstream(scope, receive, send):
        body = (await receive())["body"]
        source = json.loads(body)["source"]
        result = compile_source(source, format="json", catalog=CatalogSnapshot.empty())
        observed.extend(d.code for d in result.diagnostics)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    body = json.dumps({"source": "\0" * 1_048_577}).encode()
    messages = await invoke(BodyBoundary(downstream), [body])
    assert messages[0]["status"] == 200
    assert observed == ["WV-PARSE-SOURCE_LIMIT"]


async def test_native_filters_cannot_reflect_or_log_untrusted_headers_and_auth_errors(caplog, capsys, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from firefly_weave.access.authentication import VerifierSet
    from firefly_weave.app import make_app
    from firefly_weave.settings import Settings

    class BrokenVerifier:
        async def verify(self, credential):
            raise RuntimeError("secret-auth-canary")

    app = make_app(
        Settings(scheduler_enabled=False, database_url="postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"),
        verifiers=VerifierSet((BrokenVerifier(),)),
    )
    from firefly_weave.persistence.resources import DatabaseResources

    async def database_ready(self):
        pass

    monkeypatch.setattr(DatabaseResources, "check_startup", database_ready)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            denied = await client.get("/safe", headers={"X-Transaction-Id": "secret-header-canary"})
            failed = await client.get("/secret-path-canary", headers={"Authorization": "Bearer opaque"})
        captured = capsys.readouterr()
        evidence = str(denied.headers) + denied.text + failed.text + caplog.text + captured.out + captured.err
        assert denied.status_code == 401
        assert failed.status_code == 500
        assert "secret-header-canary" not in evidence
        assert "secret-auth-canary" not in evidence
        assert "secret-path-canary" not in evidence


async def test_structural_prescan_rejects_deep_json_before_downstream_model():
    from firefly_weave.operations.transport import BodyBoundary

    calls = []

    async def downstream(scope, receive, send):
        calls.append(True)

    messages = await invoke(BodyBoundary(downstream), [b"[" * 1000 + b"0" + b"]" * 1000])
    assert messages[0]["status"] == 413
    assert not calls


async def test_early_admission_error_keeps_canonical_wire_and_correlation_contract():
    from uuid import UUID

    from firefly_weave.operations.transport import BodyBoundary, TransportPolicy

    async def forbidden(*args):
        pytest.fail("Early error must not call downstream or receive")

    messages = await invoke(
        BodyBoundary(forbidden, TransportPolicy(json_bytes=8)),
        [],
        headers=((b"content-length", b"9"), (b"x-request-id", b"untrusted-canary")),
    )
    headers = dict(messages[0]["headers"])
    body = json.loads(messages[-1]["body"])
    assert headers[b"x-weave-wire-version"] == b"weave/api-v1"
    assert body["request_id"] == headers[b"x-weave-request-id"].decode()
    assert str(UUID(body["request_id"])) == body["request_id"]
    assert body["diagnostics"] == []
    assert b"untrusted-canary" not in messages[-1]["body"]


async def test_control_body_reservations_survive_ordinary_saturation_and_cancellation(monkeypatch):
    import asyncio

    import firefly_weave.operations.transport as transport

    reservations = transport._Reservations()
    monkeypatch.setattr(transport, "_reservations", reservations)

    async def downstream(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    app = transport.BodyBoundary(downstream)
    held = []

    async def blocked(path):
        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            pass

        await app({"type": "http", "method": "POST", "path": path, "headers": []}, receive, send)

    try:
        held = [asyncio.create_task(blocked("/compiler/compile")) for _ in range(12)]
        async with asyncio.timeout(1):
            while reservations.requests < 12:
                await asyncio.sleep(0)
        assert (await invoke(app, [b"{}"], path="/compiler/compile"))[0]["status"] == 429
        assert (await invoke(app, [b"{}"], path="/runs/id/cancel"))[0]["status"] == 200
        held += [asyncio.create_task(blocked("/runs/id/cancel")) for _ in range(4)]
        async with asyncio.timeout(1):
            while reservations.requests < 16:
                await asyncio.sleep(0)
        assert reservations.bytes <= transport.BODY_BYTES
        assert (await invoke(app, [b"{}"], path="/runs/id/cancel"))[0]["status"] == 429
        held[-1].cancel()
        await asyncio.gather(held[-1], return_exceptions=True)
        assert (await invoke(app, [b"{}"], path="/runs/id/cancel"))[0]["status"] == 200
    finally:
        for task in held:
            task.cancel()
        await asyncio.gather(*held, return_exceptions=True)
    assert reservations.requests == reservations.bytes == reservations.debug == 0
    assert (await invoke(app, [b"{}"], path="/compiler/compile"))[0]["status"] == 200


async def test_compatibility_refusals_tell_clients_when_a_rescan_can_lift_them():
    from types import SimpleNamespace

    from firefly_weave.definitions.models import CatalogError
    from firefly_weave.operations.transport import BodyBoundary

    compatibility = SimpleNamespace(ready=False, retry_after_seconds=lambda: 17)

    async def refused(scope, receive, send):
        await receive()
        raise CatalogError(503, "WV-COMPATIBILITY", "Execution compatibility unavailable", retry_after=9)

    async def call(method):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": method, "path": "/runs", "headers": []}
        scope["app"] = SimpleNamespace(state=SimpleNamespace(compatibility=compatibility))
        await BodyBoundary(refused)(scope, receive, send)
        return sent[0]["status"], dict(sent[0]["headers"]), json.loads(sent[-1]["body"])["code"]

    # Restricted: the boundary refuses before the body and names the next rescan.
    status, headers, code = await call("POST")
    assert (status, code, headers[b"retry-after"]) == (503, "WV-COMPATIBILITY", b"17")
    # Ready at admission, refused inside the request: the refusal keeps its own hint.
    compatibility.ready = True
    status, headers, code = await call("POST")
    assert (status, code, headers[b"retry-after"]) == (503, "WV-COMPATIBILITY", b"9")


@pytest.mark.parametrize(
    "status,code,expected",
    [(501, "WV-UNAVAILABLE", "unavailable"), (501, "WV-OTHER", "failed"), (503, "WV-UNAVAILABLE", "failed")],
)
async def test_unserved_operations_are_telemetry_unavailable(status, code, expected):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from firefly_weave.operations.telemetry import attributes
    from firefly_weave.operations.transport import BodyBoundary

    records = []
    telemetry = SimpleNamespace(
        span=lambda *args, **kwargs: nullcontext(), record=lambda *args, **kwargs: records.append(kwargs)
    )

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": json.dumps({"code": code}).encode()})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    await BodyBoundary(downstream)(
        {
            "type": "http",
            "method": "GET",
            "path": "/runs/id/logs",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(telemetry_service=telemetry)),
        },
        receive,
        send,
    )
    assert records[0]["status"] == expected
    assert attributes(status=expected)["status"] == expected
