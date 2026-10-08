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
from datetime import UTC, datetime, timedelta

import httpx
import httpx2
import pytest
from firefly_weave import private_origins as po
from firefly_weave.ai_policy import PolicyFile, render
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from weave_agentic_worker.gateway import create_app
from weave_agentic_worker.handler import WorkerPolicy
from weave_agentic_worker.providers import build_model


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


def ollama_gateway_policy(tmp_path, models="served"):
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


def reply_model(calls):
    def respond(messages, info):
        calls.append(messages)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name, {"result": {"answer": "Ready", "proposals": [], "followUps": []}}
                )
            ]
        )

    return FunctionModel(respond)


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
        names = ["fixture-model", "gpt-4o", "bad name<script>", "y" * 201]
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
