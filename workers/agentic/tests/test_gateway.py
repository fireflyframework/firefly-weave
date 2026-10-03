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
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from weave_agentic_worker.gateway import create_app
from weave_agentic_worker.handler import WorkerPolicy


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
