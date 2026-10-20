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

"""The private gateway has no caller-selected provider authority or durable state."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from firefly_weave import private_origins as po
from firefly_weave.ai_policy import PolicyFile, render
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import RequestUsage

from weave_agentic_worker.gateway import (
    LOOP_MIN_CHARS,
    OLLAMA_FREQUENCY_PENALTY,
    conservative_tokens,
    create_app,
    fewest_tokens,
    repeating,
)
from weave_agentic_worker.handler import WorkerPolicy
from weave_agentic_worker.providers import ProviderModel, build_model


def payload():
    return {
        "profile": {
            "provider": "openai-chat",
            "model": "fixture-model",
            "options": {"max_tokens": 1024},
            "outputSchema": LUMI_REPLY_SCHEMA,
            "timeoutSeconds": 2,
        },
        "request": {"message": "Help"},
        "attachments": [],
        "endpoint": "https://api.openai.com/v1",
        "credential": "private-key",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
    }


@pytest.mark.parametrize("mode", ["native", "prompted", "tool"])
async def test_gateway_uses_the_policy_result_format(tmp_path, mode):
    token = tmp_path / "token"
    token.write_text("service-token")
    source = tmp_path / "policy.json"
    source.write_bytes(
        render(
            [
                {
                    "id": "fixture",
                    "label": "Fixture",
                    "url": "https://api.openai.com/v1",
                    "providers": ["openai-chat"],
                    "models": ["fixture-model"],
                    "structuredOutput": mode,
                }
            ]
        )
    )
    source.chmod(0o444)
    policy = WorkerPolicy(source=PolicyFile(source, po.PrivateOrigins.empty()))
    requests = []
    result = {"result": {"answer": "Review this", "proposals": [], "followUps": []}}

    async def receive(request):
        body = json.loads(request.content)
        requests.append(body)
        message = {"role": "assistant", "content": json.dumps(result)}
        if mode == "tool":
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": body["tools"][0]["function"]["name"], "arguments": json.dumps(result)},
                    }
                ],
            }
        return httpx2.Response(
            200,
            json={
                "id": "chat-1",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture-model",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )

    def builder(spec, secret, timeout, options):
        return build_model(spec, secret, timeout, options, transport=httpx2.MockTransport(receive))

    app = create_app(policy, token, model_builder=builder)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
        response = await client.post("/v1/lumi", json=payload(), headers={"Authorization": "Bearer service-token"})
    assert response.status_code == 200 and response.json() == result["result"]
    assert len(requests) == 1
    if mode == "tool":
        assert requests[0].get("tools") and "response_format" not in requests[0]
    else:
        assert not requests[0].get("tools")
        assert requests[0]["response_format"]["type"] == ("json_schema" if mode == "native" else "json_object")


async def test_gateway_requires_service_auth_and_returns_only_validated_reply(tmp_path):
    token = tmp_path / "token"
    token.write_text("service-token")
    calls = []

    def respond(messages, info):
        calls.append(messages)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name, {"result": {"answer": "Review this", "proposals": [], "followUps": []}}
                )
            ]
        )

    app = create_app(
        WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
        token,
        model_builder=lambda *_: FunctionModel(respond),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
        denied = await client.post("/v1/lumi", json=payload())
        assert denied.status_code == 401 and not calls
        good = await client.post("/v1/lumi", json=payload(), headers={"Authorization": "Bearer service-token"})
        assert good.status_code == 200, good.text
        assert good.json() == {"answer": "Review this", "proposals": [], "followUps": []}
        assert "private-key" not in good.text
        bad = payload()
        bad["endpoint"] = "https://foreign.invalid/v1"
        assert (
            await client.post("/v1/lumi", json=bad, headers={"Authorization": "Bearer service-token"})
        ).status_code == 422
        assert len(calls) == 1


async def test_gateway_cancels_owned_model_on_disconnect(tmp_path):
    token = tmp_path / "token"
    token.write_text("service-token")
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def respond(messages, info):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    app = create_app(
        WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
        token,
        model_builder=lambda *_: FunctionModel(respond),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
        task = asyncio.create_task(
            client.post("/v1/lumi", json=payload(), headers={"Authorization": "Bearer service-token"})
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()


@pytest.mark.parametrize("route", ["test", "models"])
async def test_disconnect_joins_probe_or_discovery_closes_client_and_releases_capacity(tmp_path, route):
    started, cancelled = asyncio.Event(), asyncio.Event()
    clients = []

    async def blocked():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class Client:
        def __init__(self, blocking):
            self.blocking, self.closed = blocking, False
            self.models = self

        async def list(self):
            if self.blocking:
                await blocked()
            yield SimpleNamespace(id="fixture-model")

        async def close(self):
            if self.blocking:
                assert cancelled.is_set()
            self.closed = True

    def builder(*args):
        client = Client(not clients)
        clients.append(client)

        async def respond(messages, info):
            if client.blocking:
                await blocked()
            return ModelResponse(parts=[TextPart("ready")])

        return ProviderModel(FunctionModel(respond), client)

    token = tmp_path / "token"
    token.write_text("service-token")
    app = create_app(
        WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
        token,
        model_builder=builder,
        max_concurrency=1,
    )
    body = {
        "provider": "openai-chat",
        "endpoint": "https://api.openai.com/v1",
        "credential": "private-key",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
    }
    if route == "test":
        body["model"] = "fixture-model"
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": json.dumps(body).encode(), "more_body": False})

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/" + route,
        "raw_path": ("/v1/" + route).encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"authorization", b"Bearer service-token")],
        "server": ("localhost", 8090),
        "client": ("localhost", 1),
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    try:
        await asyncio.wait_for(started.wait(), 1)
        incoming.put_nowait({"type": "http.disconnect"})
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        assert cancelled.is_set() and clients[0].closed
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            response = await client.post("/v1/" + route, json=body, headers={"Authorization": "Bearer service-token"})
        assert response.status_code == 200 and clients[1].closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_provider_schema_hoists_arbitrary_local_refs_without_rewriting_literal_data():
    from firefly_weave.compiler.schemas import validate_payload

    from weave_agentic_worker.execution import provider_schema

    schema = {
        "type": "object",
        "properties": {
            "literal": {"const": {"$ref": "literal-data"}},
            "value": {"type": "boolean"},
            "copy": {"$ref": "#/properties/value"},
        },
        "required": ["literal", "value", "copy"],
        "additionalProperties": False,
    }
    prepared = provider_schema(schema)
    assert not validate_payload(
        prepared, {"result": {"literal": {"$ref": "literal-data"}, "value": True, "copy": False}}, {}
    )
    assert validate_payload(
        prepared, {"result": {"literal": {"$ref": "literal-data"}, "value": True, "copy": "false"}}, {}
    )


async def test_gateway_bounds_concurrency_and_releases_slot_after_cancellation(tmp_path):
    token = tmp_path / "token"
    token.write_text("service-token")
    entered = asyncio.Event()

    async def respond(messages, info):
        entered.set()
        await asyncio.Event().wait()

    app = create_app(
        WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
        token,
        model_builder=lambda *_: FunctionModel(respond),
        max_concurrency=1,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
        first = asyncio.create_task(
            client.post("/v1/lumi", json=payload(), headers={"Authorization": "Bearer service-token"})
        )
        await asyncio.wait_for(entered.wait(), 2)
        assert (
            await client.post("/v1/lumi", json=payload(), headers={"Authorization": "Bearer service-token"})
        ).status_code == 429
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        expired = payload()
        expired["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        assert (
            await client.post("/v1/lumi", json=expired, headers={"Authorization": "Bearer service-token"})
        ).status_code == 422


async def test_gateway_total_timeout_cancels_provider_and_returns_safe_code(tmp_path):
    token = tmp_path / "token"
    token.write_text("service-token")
    cancelled = asyncio.Event()

    async def respond(messages, info):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    app = create_app(
        WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
        token,
        model_builder=lambda *_: FunctionModel(respond),
    )
    body = payload()
    body["profile"]["timeoutSeconds"] = 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
        response = await client.post("/v1/lumi", json=body, headers={"Authorization": "Bearer service-token"})
    assert response.status_code == 504, response.text
    assert cancelled.is_set()
    assert response.json() == {"code": "LUMI_TIMEOUT"}


async def test_actual_agentic_executor_accepts_local_result_schema_references():
    from firefly_weave.contracts.llm import LLMProfile

    from weave_agentic_worker.execution import run_model

    profile = LLMProfile(
        provider="openai-chat",
        model="fixture",
        options={"max_tokens": 256},
        outputSchema={"$defs": {"answer": {"type": "boolean"}}, "$ref": "#/$defs/answer"},
    )

    def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"result": True})])

    result = await run_model(profile, "Decide", {}, FunctionModel(respond), {})
    assert result["result"] is True and result["usage"]["requests"] == 1


OLLAMA_ORIGINS = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
    [po.PrivateOrigin(origin="http://ollama:11434", purpose="model", networks=("10.246.21.0/24",), credentials="none")]
)
TAGS = {
    "models": [
        {"name": "qwen2.5:1.5b", "size": 986061892, "details": {"family": "qwen2"}},
        {"name": "gemma3:270m", "size": 291554930, "details": {"family": "gemma3"}},
    ]
}
SHOWS = {
    "qwen2.5:1.5b": {
        "capabilities": ["completion", "tools"],
        "model_info": {"general.architecture": "qwen2", "qwen2.context_length": 32768},
    },
    "gemma3:270m": {
        "capabilities": ["completion"],
        "model_info": {"general.architecture": "gemma3", "gemma3.context_length": 32768},
    },
}
AUTH = {"Authorization": "Bearer service-token"}


def ollama_gateway_policy(tmp_path, models="served", **changes):
    path = tmp_path / "ai-policy.json"
    endpoint = {
        "id": "ollama-local",
        "label": "Ollama (Weave-managed)",
        "url": "http://ollama:11434/v1",
        "providers": ["openai-chat"],
        "compat": "ollama",
        "credential": "none",
        "models": models,
        "contextTokens": 8192,
        "maxOutputTokens": 4096,
        **changes,
    }
    path.write_bytes(render([endpoint]))
    path.chmod(0o444)
    return WorkerPolicy(source=PolicyFile(path, OLLAMA_ORIGINS), origins=OLLAMA_ORIGINS)


def fake_ollama(paths):
    async def handle(request):
        paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json=TAGS)
        if request.url.path == "/api/show":
            return httpx2.Response(200, json=SHOWS[json.loads(request.content)["model"]])
        return httpx2.Response(404)

    return lambda entry: httpx2.MockTransport(handle)


def probe_payload(**changes):
    value = {
        "provider": "openai-chat",
        "model": "qwen2.5:1.5b",
        "endpoint": "http://ollama:11434/v1",
        "probe_tools": True,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }
    value.update(changes)
    return {key: item for key, item in value.items() if item is not None}


def chatty(tools=True):
    def respond(messages, info):
        if info.function_tools and tools:
            return ModelResponse(parts=[ToolCallPart("ping", {"value": "weave"})])
        return ModelResponse(parts=[TextPart("ready-canary")])

    return FunctionModel(respond)


def gateway(tmp_path, *, builder, paths=None, policy=None):
    token = tmp_path / "token"
    token.write_text("service-token")
    app = create_app(
        policy or ollama_gateway_policy(tmp_path),
        token,
        model_builder=builder,
        transport_factory=fake_ollama([] if paths is None else paths),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway")


async def test_connection_test_reports_latency_and_tools_but_never_the_answer(tmp_path):
    async with gateway(tmp_path, builder=lambda *_: chatty()) as client:
        assert (await client.post("/v1/test", json=probe_payload())).status_code == 401
        response = await client.post("/v1/test", json=probe_payload(), headers=AUTH)
    body = response.json()
    assert response.status_code == 200 and body["ok"] is True and body["code"] == "ok"
    assert body["tool_calling"] == "supported" and body["model"] == "qwen2.5:1.5b"
    assert body["context_tokens"] == 8192 and isinstance(body["latency_ms"], int)
    assert "ready-canary" not in response.text


async def test_a_missing_model_fails_the_test_with_its_code(tmp_path):
    def missing(messages, info):
        raise ModelHTTPError(404, "qwen3:4b", body={"message": "model 'qwen3:4b' not found"})

    async with gateway(tmp_path, builder=lambda *_: FunctionModel(missing)) as client:
        body = (await client.post("/v1/test", json=probe_payload(model="qwen3:4b"), headers=AUTH)).json()
    assert body == {
        "ok": False,
        "code": "LLM_MODEL_NOT_FOUND",
        "latency_ms": None,
        "model": "qwen3:4b",
        "tool_calling": "unknown",
    }


async def test_a_model_without_tools_is_reported_unsupported(tmp_path):
    def respond(messages, info):
        if info.function_tools:
            raise ModelHTTPError(400, "gemma3:270m", body={"error": {"message": "gemma3:270m does not support tools"}})
        return ModelResponse(parts=[TextPart("ready")])

    async with gateway(tmp_path, builder=lambda *_: FunctionModel(respond)) as client:
        body = (await client.post("/v1/test", json=probe_payload(model="gemma3:270m"), headers=AUTH)).json()
    assert body["ok"] is True and body["tool_calling"] == "unsupported"


async def test_an_unapproved_model_is_refused_without_calling_it(tmp_path):
    calls = []

    def builder(*args):
        calls.append(args)
        return chatty()

    policy = ollama_gateway_policy(tmp_path, models=["qwen3:4b"])
    async with gateway(tmp_path, builder=builder, policy=policy) as client:
        body = (await client.post("/v1/test", json=probe_payload(), headers=AUTH)).json()
    assert body["ok"] is False and body["code"] == "LLM_POLICY" and calls == []


async def test_without_a_model_the_test_uses_the_first_served_model(tmp_path):
    async with gateway(tmp_path, builder=lambda *_: chatty()) as client:
        body = (await client.post("/v1/test", json=probe_payload(model=None, probe_tools=False), headers=AUTH)).json()
    assert body["ok"] is True and body["model"] == "gemma3:270m" and body["tool_calling"] == "unknown"


async def test_discovery_lists_served_models_with_their_facts_and_never_pulls(tmp_path):
    paths = []
    request = {
        "provider": "openai-chat",
        "endpoint": "http://ollama:11434/v1",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }
    async with gateway(tmp_path, builder=lambda *_: chatty(), paths=paths) as client:
        response = await client.post("/v1/models", json=request, headers=AUTH)
    assert response.json() == {
        "discovery": "ok",
        "models": [
            {
                "name": "gemma3:270m",
                "size_bytes": 291554930,
                "family": "gemma3",
                "context_tokens": 32768,
                "tools": "no",
                "approved": True,
                "available": True,
            },
            {
                "name": "qwen2.5:1.5b",
                "size_bytes": 986061892,
                "family": "qwen2",
                "context_tokens": 32768,
                "tools": "yes",
                "approved": True,
                "available": True,
            },
        ],
    }
    assert set(paths) == {"/api/tags", "/api/show"}


async def test_credentialed_endpoints_need_the_credential(tmp_path):
    policy = WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"}))
    payload_ = probe_payload(model="fixture-model", endpoint="https://api.openai.com/v1")
    async with gateway(tmp_path, builder=lambda *_: chatty(), policy=policy) as client:
        body = (await client.post("/v1/test", json=payload_, headers=AUTH)).json()
    assert body["ok"] is False and body["code"] == "LLM_AUTH"


REPLY = {"result": {"answer": "Ready", "proposals": [], "followUps": []}}


class Counted(WrapperModel):
    """Reports the prompt tokens a real endpoint counts: three characters each, or a fixed number."""

    def __init__(self, wrapped, tokens=None):
        super().__init__(wrapped)
        self.tokens = tokens

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        sent = sum(len(str(part.content)) for message in messages for part in getattr(message, "parts", ()))
        sent += sum(len(message.instructions or "") for message in messages if hasattr(message, "instructions"))
        async with self.wrapped.request_stream(messages, model_settings, model_request_parameters) as stream:
            stream._usage = RequestUsage(input_tokens=self.tokens if self.tokens is not None else -(-sent // 3))
            yield stream


def reply_model(calls):
    def respond(messages, info):
        calls.append(messages)
        part = ToolCallPart(info.output_tools[0].name, REPLY) if info.output_tools else TextPart(json.dumps(REPLY))
        return ModelResponse(parts=[part])

    async def stream(messages, info):
        calls.append(messages)
        for chunk in streamed(info, [json.dumps(REPLY)]):
            yield chunk

    return Counted(FunctionModel(respond, stream_function=stream))


def streamed(info, chunks):
    """Stream text chunks as the reply format the request asks for: an output tool's arguments, or plain text."""
    for chunk in chunks:
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=chunk)} if info.output_tools else chunk


async def test_the_assistant_route_serves_a_file_policy_through_the_pinned_options(tmp_path):
    built, calls = [], []

    def builder(spec, secret, timeout, options):
        built.append((spec, secret, options))
        return reply_model(calls)

    ask = payload()
    ask["profile"]["model"] = "qwen2.5:1.5b"
    ask["endpoint"] = "http://ollama:11434/v1"
    del ask["credential"]
    async with gateway(tmp_path, builder=builder) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json() == {"answer": "Ready", "proposals": [], "followUps": []} and len(calls) == 1
    spec, secret, options = built[0]
    assert spec.base_url == "http://ollama:11434/v1" and secret == ""
    assert options.endpoint.id == "ollama-local" and options.origins is OLLAMA_ORIGINS


async def test_the_assistant_route_refuses_what_the_file_policy_does_not_approve(tmp_path):
    built = []
    ask = payload()
    ask["profile"]["model"] = "qwen2.5:1.5b"
    ask["endpoint"] = "http://ollama:11434/v1"
    del ask["credential"]
    policy = ollama_gateway_policy(tmp_path, models=["qwen3:4b"])
    async with gateway(tmp_path, builder=lambda *args: built.append(args), policy=policy) as client:
        unapproved = await client.post("/v1/lumi", json=ask, headers=AUTH)
        ask["endpoint"] = "http://ollama:11434/other"
        unlisted = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert unapproved.status_code == unlisted.status_code == 422
    assert unapproved.json() == unlisted.json() == {"code": "LLM_POLICY"} and built == []


def models_request(**changes):
    value = {
        "provider": "openai-chat",
        "endpoint": "http://ollama:11434/v1",
        "expires_at": (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
    }
    value.update(changes)
    return value


async def test_discovery_never_follows_a_redirect_and_reports_only_a_code(tmp_path):
    paths = []

    async def moved(request):
        paths.append(request.url.path)
        return httpx2.Response(302, headers={"location": "http://foreign.example/collect"}, text="moved-canary")

    token = tmp_path / "token"
    token.write_text("service-token")
    app = create_app(
        ollama_gateway_policy(tmp_path),
        token,
        model_builder=lambda *_: chatty(),
        transport_factory=lambda entry: httpx2.MockTransport(moved),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        listed = await client.post("/v1/models", json=models_request(), headers=AUTH)
        tested = await client.post("/v1/test", json=probe_payload(model=None), headers=AUTH)
    assert listed.status_code == 200 and listed.json() == {"discovery": "LLM_PROVIDER", "models": []}
    assert tested.json()["ok"] is False and tested.json()["code"] == "LLM_PROVIDER"
    assert paths == ["/api/tags", "/api/tags"]
    assert "foreign" not in listed.text + tested.text and "canary" not in listed.text + tested.text


async def test_discovery_skips_unusable_tags_and_keeps_models_whose_details_fail(tmp_path):
    async def handle(request):
        if request.url.path == "/api/tags":
            return httpx2.Response(
                200,
                json={
                    "models": [
                        {"name": "x" * 201},
                        {"name": "bad name<script>"},
                        {"name": 7},
                        "gemma3:270m",
                        {"name": "qwen2.5:1.5b", "size": 986061892},
                    ]
                },
            )
        return httpx2.Response(500, text="show-canary")

    token = tmp_path / "token"
    token.write_text("service-token")
    app = create_app(
        ollama_gateway_policy(tmp_path),
        token,
        model_builder=lambda *_: chatty(),
        transport_factory=lambda entry: httpx2.MockTransport(handle),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/v1/models", json=models_request(), headers=AUTH)
    assert response.json() == {
        "discovery": "ok",
        "models": [
            {"name": "qwen2.5:1.5b", "size_bytes": 986061892, "tools": "unknown", "approved": True, "available": True}
        ],
    }


async def test_an_unreachable_ollama_reports_unreachable(tmp_path):
    async def down(request):
        raise httpx2.ConnectError("Connection refused", request=request)

    token = tmp_path / "token"
    token.write_text("service-token")
    app = create_app(
        ollama_gateway_policy(tmp_path),
        token,
        model_builder=lambda *_: chatty(),
        transport_factory=lambda entry: httpx2.MockTransport(down),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        listed = await client.post("/v1/models", json=models_request(), headers=AUTH)
        tested = await client.post("/v1/test", json=probe_payload(model=None), headers=AUTH)
    assert listed.json() == {"discovery": "LLM_UNREACHABLE", "models": []}
    assert tested.json()["code"] == "LLM_UNREACHABLE"


async def test_discovery_goes_through_the_pinned_model_transport_by_default(tmp_path):
    from dataclasses import replace

    resolved = []

    async def outside_the_entry(host, port):
        resolved.append((host, port))
        return ("10.99.0.5",)

    token = tmp_path / "token"
    token.write_text("service-token")
    policy = replace(ollama_gateway_policy(tmp_path), resolver=outside_the_entry)
    app = create_app(policy, token, model_builder=lambda *_: chatty())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/v1/models", json=models_request(), headers=AUTH)
    assert response.json() == {"discovery": "LLM_POLICY", "models": []}
    assert resolved == [("ollama", 11434)]


async def test_provider_discovery_lists_bounded_names_and_the_approved_models_it_misses(tmp_path):
    seen = []

    async def provider(request):
        seen.append(request)
        names = ["fixture-model", "gpt-4o", "fixture-model", "gpt-4o", "bad name<script>", "y" * 201]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": n, "object": "model", "created": 0, "owned_by": "o"} for n in names],
            },
        )

    def builder(spec, secret, timeout, options):
        return build_model(spec, secret, timeout, options, transport=httpx2.MockTransport(provider))

    policy = WorkerPolicy(
        frozenset({("openai-chat", "fixture-model"), ("openai-chat", "gone-model"), ("anthropic", "claude-x")}),
        frozenset({"https://api.openai.com/v1"}),
    )
    request = models_request(endpoint="https://api.openai.com/v1")
    async with gateway(tmp_path, builder=builder, policy=policy) as client:
        missing = await client.post("/v1/models", json=request, headers=AUTH)
        response = await client.post("/v1/models", json={**request, "credential": "private-key"}, headers=AUTH)
    assert missing.json() == {"discovery": "LLM_AUTH", "models": []}
    assert response.json() == {
        "discovery": "ok",
        "models": [
            {"name": "fixture-model", "approved": True, "available": True, "tools": "unknown"},
            {"name": "gpt-4o", "approved": False, "available": True, "tools": "unknown"},
            {"name": "gone-model", "approved": True, "available": False, "tools": "unknown"},
        ],
    }
    assert [request.url.path for request in seen] == ["/v1/models"]
    assert seen[0].headers["authorization"] == "Bearer private-key" and "private-key" not in response.text


async def test_the_connection_test_builds_its_client_for_the_approved_endpoint(tmp_path):
    built = []

    def builder(spec, secret, timeout, options):
        built.append((spec, secret, options))
        return chatty()

    async with gateway(tmp_path, builder=builder) as client:
        body = (await client.post("/v1/test", json=probe_payload(credential="unused"), headers=AUTH)).json()
    assert body["ok"] is True
    spec, secret, options = built[0]
    assert (spec.model, spec.base_url) == ("qwen2.5:1.5b", "http://ollama:11434/v1")
    assert options.endpoint.id == "ollama-local" and options.origins is OLLAMA_ORIGINS


async def test_azure_discovery_needs_an_api_version_before_any_client(tmp_path):
    built = []
    endpoint = "https://resource.openai.azure.com"
    policy = WorkerPolicy(frozenset({("azure-chat", "deployment")}), frozenset({endpoint}))
    request = models_request(provider="azure-chat", endpoint=endpoint, credential="private-key")
    async with gateway(tmp_path, builder=lambda *args: built.append(args), policy=policy) as client:
        response = await client.post("/v1/models", json=request, headers=AUTH)
    assert response.json() == {"discovery": "LLM_CONNECTION", "models": []} and built == []


async def test_a_model_that_answers_passes_even_when_its_served_details_do_not(tmp_path):
    async def no_details(request):
        return httpx2.Response(500, text="details-canary")

    token = tmp_path / "token"
    token.write_text("service-token")
    app = create_app(
        ollama_gateway_policy(tmp_path),
        token,
        model_builder=lambda *_: chatty(),
        transport_factory=lambda entry: httpx2.MockTransport(no_details),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/v1/test", json=probe_payload(), headers=AUTH)
    body = response.json()
    assert body["ok"] is True and body["code"] == "ok" and body["context_tokens"] == 8192
    assert "canary" not in response.text


async def test_an_oversized_listing_is_refused_and_unknown_codes_never_leave(tmp_path, monkeypatch):
    from weave_agentic_worker import discovery

    monkeypatch.setattr(discovery, "MAX_BODY", 64)
    policy = ollama_gateway_policy(tmp_path)
    async with gateway(tmp_path, builder=lambda *_: chatty(), policy=policy) as client:
        listed = await client.post("/v1/models", json=models_request(), headers=AUTH)

    def foreign(*_):
        raise ConnectorFailure("NOT_A_PRODUCT_CODE", "not_started")

    async with gateway(tmp_path, builder=foreign, policy=policy) as client:
        tested = await client.post("/v1/test", json=probe_payload(), headers=AUTH)
    assert listed.json() == {"discovery": "LLM_PROVIDER", "models": []}
    assert tested.status_code == 200 and tested.json()["code"] == "LLM_PROVIDER"


async def test_a_malformed_provider_answer_is_a_provider_code_not_a_bad_request(tmp_path):
    async def malformed(request):
        return httpx2.Response(200, headers={"content-type": "application/json"}, content=b"{not json")

    def builder(spec, secret, timeout, options):
        return build_model(spec, secret, timeout, options, transport=httpx2.MockTransport(malformed))

    policy = WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"}))
    endpoint = "https://api.openai.com/v1"
    async with gateway(tmp_path, builder=builder, policy=policy) as client:
        listed = await client.post(
            "/v1/models", json=models_request(endpoint=endpoint, credential="private-key"), headers=AUTH
        )
        tested = await client.post(
            "/v1/test",
            json=probe_payload(model="fixture-model", endpoint=endpoint, credential="private-key"),
            headers=AUTH,
        )
    assert listed.status_code == 200 and listed.json() == {"discovery": "LLM_PROVIDER", "models": []}
    assert tested.status_code == 200 and tested.json()["code"] == "LLM_PROVIDER"
    assert "not json" not in listed.text + tested.text


async def test_the_assistant_route_refuses_more_output_than_the_endpoint_allows(tmp_path):
    built = []
    ask = payload()
    ask["profile"]["model"] = "qwen2.5:1.5b"
    ask["endpoint"] = "http://ollama:11434/v1"
    del ask["credential"]
    policy = ollama_gateway_policy(tmp_path, maxOutputTokens=128)
    async with gateway(tmp_path, builder=lambda *args: built.append(args), policy=policy) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert response.status_code == 422 and response.json() == {"code": "LLM_OPTIONS"} and built == []


async def test_discovery_reports_only_product_codes(tmp_path):
    def foreign(*_):
        raise ConnectorFailure("NOT_A_PRODUCT_CODE", "not_started")

    policy = WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"}))
    request = models_request(endpoint="https://api.openai.com/v1", credential="private-key")
    async with gateway(tmp_path, builder=foreign, policy=policy) as client:
        response = await client.post("/v1/models", json=request, headers=AUTH)
    assert response.json() == {"discovery": "LLM_PROVIDER", "models": []}


async def test_a_model_side_value_error_on_the_assistant_route_is_not_a_bad_request(tmp_path):
    def faulty(*_):
        raise ValueError("provider-canary")

    ask = payload()
    ask["profile"]["model"] = "qwen2.5:1.5b"
    ask["endpoint"] = "http://ollama:11434/v1"
    del ask["credential"]
    async with gateway(tmp_path, builder=faulty) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert response.status_code == 503 and response.json() == {"code": "LUMI_UNAVAILABLE"}


async def test_duplicate_normalization_does_not_scan_for_fifty_unique_names():
    from weave_agentic_worker.discovery import provider_models, unique_model_items

    inspected = []

    async def listing():
        for index in range(51):
            inspected.append(index)
            yield SimpleNamespace(id="first" if index < 50 else "outside-the-bound")

    owned = SimpleNamespace(client=SimpleNamespace(models=SimpleNamespace(list=listing)))
    names = await provider_models(owned, "openai-chat")
    rows = [{"name": name, "available": True} for name in names]
    assert unique_model_items(rows) == [{"name": "first", "available": True}]
    assert inspected == list(range(50))


def test_duplicate_normalization_preserves_first_metadata_and_order():
    from weave_agentic_worker.discovery import unique_model_items

    rows = [{"name": "b", "available": True}, {"name": "a", "available": True}, {"name": "b", "available": False}]
    assert unique_model_items(rows) == rows[:2]


async def test_ollama_discovery_keeps_the_first_duplicate_tag_metadata(tmp_path):
    paths = []

    def respond(request):
        paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx2.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3:4b", "size": 100, "details": {"family": "qwen3"}},
                        {"name": "qwen3:4b", "size": 200, "details": {"family": "other"}},
                    ]
                },
            )
        return httpx2.Response(200, json={"capabilities": ["completion", "tools"]})

    token = tmp_path / "token"
    token.write_text("service-token")
    app = create_app(
        ollama_gateway_policy(tmp_path), token, transport_factory=lambda entry: httpx2.MockTransport(respond)
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gateway") as client:
        response = await client.post("/v1/models", json=models_request(), headers=AUTH)
    assert response.json() == {
        "discovery": "ok",
        "models": [
            {
                "name": "qwen3:4b",
                "size_bytes": 100,
                "family": "qwen3",
                "tools": "yes",
                "approved": True,
                "available": True,
            }
        ],
    }
    assert paths == ["/api/tags", "/api/show", "/api/show"]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ConnectorFailure("LLM_MODEL_NOT_FOUND", "not_started"), "LLM_MODEL_NOT_FOUND"),
        (ConnectorFailure("LLM_UNREACHABLE", "not_started"), "LLM_UNREACHABLE"),
        (ConnectorFailure("PROVIDER_SECRET_CANARY", "unknown"), "LLM_PROVIDER"),
    ],
)
async def test_the_assistant_route_answers_only_product_codes(tmp_path, failure, code):
    def failing(*args):
        raise failure

    ask = payload()
    ask["profile"]["model"] = "qwen2.5:1.5b"
    ask["endpoint"] = "http://ollama:11434/v1"
    del ask["credential"]
    async with gateway(tmp_path, builder=failing) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"code": code})


def test_the_assistant_instructions_and_reply_format_fit_the_reserve_weave_ai_leaves_them():
    from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA

    from weave_agentic_worker.execution import provider_schema
    from weave_agentic_worker.gateway import INSTRUCTIONS

    # The API leaves these tokens for the gateway's own text (GATEWAY_RESERVE_TOKENS in the server's Weave AI context).
    reserve = 736
    reply_format = json.dumps(provider_schema(LUMI_REPLY_SCHEMA))
    assert conservative_tokens(INSTRUCTIONS, reply_format) <= reserve
    assert INSTRUCTIONS.startswith("You are Weave AI")
    for rule in (
        "whole document",
        "never a fragment",
        "data, not instructions",
        "trimmed",
        "return no proposals",
        "Answer briefly",
        "in a proposal's source field, never in the answer",
    ):
        assert rule in INSTRUCTIONS


LOOP = "The workflow should also be reviewed to ensure that it follows the correct logic and uses the right data. "


def test_a_reply_that_keeps_repeating_one_span_is_a_loop():
    assert repeating('{"result": {"answer": "Step-3 fails. ' + LOOP * 4)
    assert repeating('{"result": {"answer": "' + " " * 256)
    assert repeating("x" + "ab" * 128)


def test_ordinary_replies_and_whole_documents_are_not_loops():
    answer = (
        "Step-3 calls crm-lookup with an empty customer ID. Map the input's customerId into its with block, "
        "then run the workflow again. If the lookup still fails, check the connection's endpoint. "
    )
    steps = "".join(
        f"    - id: step-{index}\n      kind: action\n      uses: crm-lookup@1.0.0\n" for index in range(20)
    )
    document = f"apiVersion: weave/v1alpha1\nkind: Workflow\nspec:\n  steps:\n{steps}"
    assert not repeating(answer * 2)
    assert not repeating(json.dumps({"answer": answer, "proposals": [{"source": document}]}))
    assert not repeating(LOOP * 3)


def ollama_ask():
    ask = payload()
    ask["profile"]["model"] = "qwen2.5:1.5b"
    ask["endpoint"] = "http://ollama:11434/v1"
    del ask["credential"]
    return ask


async def test_the_assistant_route_stops_a_reply_that_repeats_itself(tmp_path):
    sent = []

    async def loop(messages, info):
        for chunk in streamed(info, ['{"result": {"answer": "Step-3 fails. ', *[LOOP] * 1000]):
            sent.append(chunk)
            yield chunk

    async with gateway(tmp_path, builder=lambda *_: FunctionModel(stream_function=loop)) as client:
        response = await client.post("/v1/lumi", json=ollama_ask(), headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"code": "LUMI_REPETITION"})
    assert len(sent) < 10


async def test_ollama_replies_carry_a_frequency_penalty_and_cloud_replies_do_not(tmp_path):
    settings = []

    def respond(messages, info):
        settings.append(info.model_settings or {})
        part = ToolCallPart(info.output_tools[0].name, REPLY) if info.output_tools else TextPart(json.dumps(REPLY))
        return ModelResponse(parts=[part])

    async def stream(messages, info):
        settings.append(info.model_settings or {})
        for chunk in streamed(info, [json.dumps(REPLY)]):
            yield chunk

    model = Counted(FunctionModel(respond, stream_function=stream))
    async with gateway(tmp_path, builder=lambda *_: model) as client:
        local = await client.post("/v1/lumi", json=ollama_ask(), headers=AUTH)
    token = tmp_path / "cloud-token"
    token.write_text("service-token")
    app = create_app(
        WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
        token,
        model_builder=lambda *_: model,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        cloud = await client.post("/v1/lumi", json=payload(), headers=AUTH)
    assert local.status_code == cloud.status_code == 200, (local.text, cloud.text)
    assert settings[0]["frequency_penalty"] == OLLAMA_FREQUENCY_PENALTY == 0.2
    # Thinking models (qwen3) spend the whole answer limit thinking; Weave AI asks for none on Ollama.
    assert settings[0]["openai_reasoning_effort"] == "none"
    assert "frequency_penalty" not in settings[1] and "openai_reasoning_effort" not in settings[1]


def test_the_gateway_counts_two_and_a_half_characters_per_token_like_the_api():
    assert conservative_tokens("x" * 1000) == 400 and conservative_tokens("ab", "c") == 2
    # Outside ASCII each character counts its UTF-8 bytes, like the API: measured 1.9 characters per token for
    # Chinese and 0.33 for emoji on llama3.2:3b.
    assert conservative_tokens("ab" * 5 + "é" + "中") == 4 + 2 + 3
    assert conservative_tokens("中" * 5000) == 15000 >= 4838 and conservative_tokens("😀" * 3000) == 12000 >= 9206


def test_the_fewest_tokens_a_whole_prompt_can_count_depends_on_its_script():
    # No text measured took more than 4.6 ASCII characters per token or 2 other characters per token.
    assert fewest_tokens("x" * 600) == 100 and fewest_tokens("中" * 100) == 50
    assert fewest_tokens("x" * 600 + "中" * 100) == 150


def test_a_short_repeated_tail_is_not_yet_a_loop():
    assert LOOP_MIN_CHARS == 256
    assert not repeating("x" + "ab" * 100) and repeating("x" + "ab" * 128)


async def test_an_emoji_question_that_may_not_fit_is_refused_before_the_model(tmp_path):
    calls = []
    ask = ollama_ask()
    # 3,000 emoji counted 9,206 tokens on llama3.2:3b; characters / 2.5 would have let it through.
    ask["request"]["message"] = "😀" * 3000
    async with gateway(tmp_path, builder=lambda *_: reply_model(calls)) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"code": "LLM_CONTEXT_LIMIT"})
    assert calls == []


@pytest.mark.parametrize(
    ("message", "counted"),
    [
        # Ollama 0.40.2 keeps about half the context when it cuts a prompt: 4,098 of 8,192, measured twice.
        ("word " * 2400, 4098),
        # Fewer tokens than 800 emoji (at least one per two characters) and the ASCII around them can count.
        ("😀" * 800, 400),
    ],
    ids=["half-the-context", "fewer-than-the-script-allows"],
)
async def test_an_answer_to_a_prompt_ollama_cut_is_not_used(tmp_path, message, counted):
    calls = []
    ask = ollama_ask()
    ask["request"]["message"] = message

    def cut(*_):
        model = reply_model(calls)
        model.tokens = counted
        return model

    async with gateway(tmp_path, builder=cut) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"code": "LUMI_TRUNCATED"})
    assert len(calls) == 1


async def test_an_ollama_entry_without_a_context_size_is_held_to_the_default_context(tmp_path):
    calls = []
    ask = ollama_ask()
    ask["request"]["message"] = "word " * 4000
    policy = ollama_gateway_policy(tmp_path, contextTokens=None)
    async with gateway(tmp_path, builder=lambda *_: reply_model(calls), policy=policy) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"code": "LLM_CONTEXT_LIMIT"})
    assert calls == []


async def test_a_request_that_may_not_fit_the_context_is_refused_before_the_model(tmp_path):
    calls = []
    ask = ollama_ask()
    # Characters / 4 would fit 8,192 tokens with the answer; the conservative count does not.
    ask["request"]["message"] = "word " * 3600
    async with gateway(tmp_path, builder=lambda *_: reply_model(calls)) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"code": "LLM_CONTEXT_LIMIT"})
    assert calls == []


async def test_an_answer_to_a_prompt_the_model_saw_only_part_of_is_not_used(tmp_path):
    calls = []

    def truncating(*_):
        model = reply_model(calls)
        model.tokens = 100
        return model

    async with gateway(tmp_path, builder=truncating) as client:
        response = await client.post("/v1/lumi", json=ollama_ask(), headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"code": "LUMI_TRUNCATED"})
    assert len(calls) == 1


async def test_a_whole_prompt_the_estimate_overcounts_is_answered(tmp_path):
    # Live on llama3.2:3b: a 1,000-character Chinese question counted 1,516 tokens, whole (the instructions' code word
    # was in the answer), while the conservative count was 4,372, above half the 8,192-token context.
    calls = []
    ask = ollama_ask()
    # With no guide in this request, 1,300 characters bring the conservative count above half the context, as live.
    ask["request"]["message"] = "Explain this, then name the code word. " + ("请解释工作流" * 217)[:1300]

    def whole(*_):
        model = reply_model(calls)
        model.tokens = 1516
        return model

    async with gateway(tmp_path, builder=whole) as client:
        response = await client.post("/v1/lumi", json=ask, headers=AUTH)
    assert response.status_code == 200, response.text
