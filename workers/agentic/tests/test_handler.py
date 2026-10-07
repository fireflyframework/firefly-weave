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

"""Real Agentic execution stays inside one fenced, bounded task."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from firefly_weave.contracts.workers import CredentialLease, LeaseProof, TaskLease
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from weave_agentic_worker.handler import AgenticTaskHandler, WorkerPolicy

PATTERNS = [
    "none",
    "react",
    "chain_of_thought",
    "plan_and_execute",
    "reflexion",
    "tree_of_thoughts",
    "goal_decomposition",
]


def lease(pattern="none", **profile):
    now = datetime.now(UTC)
    return TaskLease(
        proof=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="lease-proof"),
        operation_key="operation-1",
        deadline=now + timedelta(seconds=30),
        expires_at=now + timedelta(seconds=30),
        capability="weave-agentic.generate@1.0.0",
        worker_release_id=uuid4(),
        input={
            "profile": {
                "provider": "openai-chat",
                "model": "fixture-model",
                "options": {"max_tokens": 256},
                "reasoning": {"pattern": pattern, "maxSteps": 8},
                "maxCalls": 12,
                "timeoutSeconds": 10,
                "outputSchema": {
                    "type": "object",
                    "properties": {"approved": {"type": "boolean"}},
                    "required": ["approved"],
                    "additionalProperties": False,
                },
                **profile,
            },
            "prompt": "Assess this request",
            "context": {"amount": 10},
        },
    )


class Transport:
    def __init__(self):
        self.requests = []
        self.revision = uuid4()

    async def context(self, proof):
        self.requests.append(("context", proof))
        return SimpleNamespace(
            connection=SimpleNamespace(
                revision_id=self.revision,
                connector="weave-agentic-provider@1.0.0",
                config={"provider": "openai-chat", "endpoint": "https://api.openai.com/v1", "secretSlot": "apiKey"},
                allowed_destinations=("https://api.openai.com",),
                secret_slots=["apiKey"],
            ),
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )

    async def credentials(self, request):
        self.requests.append(("credentials", request))
        return CredentialLease(value="secret-canary", expires_at=datetime.now(UTC) + timedelta(seconds=30))


def fixture_model(calls, *, fail=False, runaway=False):
    async def respond(messages, info):
        calls.append((messages, info))
        if fail:
            raise RuntimeError("secret-canary must never reach logs")
        if info.output_tools:
            tool = info.output_tools[0]
            properties = tool.parameters_json_schema.get("properties", {})
            if "result" in properties:
                value = {"result": {"approved": True}}
            elif "is_final" in properties:
                value = {
                    "content": "private-reasoning-canary",
                    "is_final": not runaway,
                    "final_answer": "decision ready",
                }
            elif "steps" in properties:
                value = {
                    "goal": "assess",
                    "steps": [{"id": str(i), "description": "assess"} for i in range(2 if runaway else 1)],
                }
            elif "is_satisfactory" in properties:
                value = {"is_satisfactory": not runaway, "issues": [], "suggestions": []}
            elif "branches" in properties:
                value = {"branches": ["decision ready"] * (2 if runaway else 1)}
            elif "branch_id" in properties:
                value = {"branch_id": 0, "score": 1.0, "reasoning": "private-reasoning-canary"}
            elif "phases" in properties:
                value = {"goal": "assess", "phases": [{"name": "assess", "tasks": ["assess"] * (2 if runaway else 1)}]}
            else:
                raise AssertionError(properties)
            parts = [ToolCallPart(tool.name, value)]
        else:
            parts = [TextPart('{"approved":true}')]
        return ModelResponse(parts=parts, usage=RequestUsage(input_tokens=5, output_tokens=3))

    return FunctionModel(respond)


def handler(transport, model):
    return AgenticTaskHandler(
        transport,
        WorkerPolicy(
            models=frozenset({("openai-chat", "fixture-model")}), endpoints=frozenset({"https://api.openai.com/v1"})
        ),
        model_builder=lambda *_: model,
    )


@pytest.mark.parametrize("destination", ["https://api.openai.com", "https://API.openai.com:443"])
async def test_approved_endpoint_uses_the_same_canonical_origin_as_the_connection(destination):
    endpoint = "https://API.openai.com:443/v1"

    class CanonicalTransport(Transport):
        async def context(self, proof):
            context = await super().context(proof)
            context.connection.config["endpoint"] = endpoint
            context.connection.allowed_destinations = (destination,)
            return context

    transport = CanonicalTransport()
    worker = AgenticTaskHandler(
        transport,
        WorkerPolicy(models=frozenset({("openai-chat", "fixture-model")}), endpoints=frozenset({endpoint})),
        model_builder=lambda *_: fixture_model([]),
    )
    output = await worker(lease())
    assert output["result"] == {"approved": True}


@pytest.mark.parametrize("pattern", PATTERNS)
async def test_all_patterns_use_real_agentic_and_return_only_validated_final_result(pattern):
    calls = []
    transport = Transport()
    task = lease(pattern)
    output = await handler(transport, fixture_model(calls))(task)
    assert output == {
        "result": {"approved": True},
        "usage": {"requests": len(calls), "inputTokens": len(calls) * 5, "outputTokens": len(calls) * 3},
        "provider": "openai-chat",
        "model": "fixture-model",
    }
    assert "private-reasoning-canary" not in json.dumps(output)
    assert [kind for kind, _ in transport.requests] == ["context", "credentials"]
    request = transport.requests[1][1]
    assert (
        request.lease == task.proof
        and request.connection_revision_id == transport.revision
        and request.slot == "apiKey"
    )


@pytest.mark.parametrize("pattern", PATTERNS[1:])
async def test_reasoning_calls_share_one_request_budget(pattern):
    calls = []
    with pytest.raises(Exception, match="LLM_LIMIT"):
        await handler(Transport(), fixture_model(calls, runaway=True))(lease(pattern, maxCalls=2))
    assert len(calls) == 2


async def test_total_timeout_cancels_model_call():
    cancelled = asyncio.Event()

    async def respond(messages, info):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    with pytest.raises(Exception, match="LLM_TIMEOUT"):
        await handler(Transport(), FunctionModel(respond))(lease(timeoutSeconds=1))
    assert cancelled.is_set()


async def test_provider_failure_does_not_expose_exception_or_partial_output(caplog):
    with pytest.raises(Exception, match="LLM_PROVIDER") as failure:
        await handler(Transport(), fixture_model([], fail=True))(lease("plan_and_execute"))
    assert "secret-canary" not in str(failure.value)
    assert "secret-canary" not in caplog.text


async def test_disallowed_model_does_not_request_credentials_or_call_provider():
    transport = Transport()
    calls = []
    with pytest.raises(Exception, match="LLM_POLICY"):
        await handler(transport, fixture_model(calls))(lease(model="not-approved"))
    assert not calls and not transport.requests


async def test_invalid_result_schema_fails_before_completion():
    with pytest.raises(Exception, match="LLM_OUTPUT"):
        await handler(Transport(), fixture_model([]))(lease(outputSchema={"type": "integer"}))


@pytest.mark.parametrize("pattern", PATTERNS)
async def test_cancellation_propagates_without_completion(pattern):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def respond(messages, info):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    task = asyncio.create_task(handler(Transport(), FunctionModel(respond))(lease(pattern)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_unknown_options_fail_before_any_authority_lookup():
    transport = Transport()
    with pytest.raises(Exception, match="LLM_INPUT"):
        await handler(transport, fixture_model([]))(lease(options={"max_tokens": 10, "base_url": "https://evil.test"}))
    assert transport.requests == []


async def test_unsupported_provider_options_fail_before_credential_request():
    transport = Transport()
    with pytest.raises(Exception, match="LLM_OPTIONS"):
        await handler(transport, fixture_model([]))(lease(options={"max_tokens": 10, "top_k": 3}))
    assert [kind for kind, _ in transport.requests] == ["context"]


@pytest.mark.parametrize("endpoint", ["https://foreign.example/v1", "https://API.openai.com:443/v1"])
async def test_context_with_foreign_endpoint_is_denied_before_credential_request(endpoint):
    transport = Transport()
    original = transport.context

    async def context(proof):
        result = await original(proof)
        result.connection.config["endpoint"] = endpoint
        return result

    transport.context = context
    with pytest.raises(Exception, match="LLM_POLICY"):
        await handler(transport, fixture_model([]))(lease())
    assert [kind for kind, _ in transport.requests] == ["context"]


async def test_real_sdk_worker_claims_context_credentials_and_completes_through_http():
    import httpx
    from firefly_weave.sdk.transport import WorkerTransport
    from firefly_weave.sdk.worker import Worker

    task = lease()
    calls = []
    routes = []
    revision = uuid4()

    async def receive(request):
        routes.append(request.url.path)
        payload = json.loads(request.content)
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json=[task.model_dump(mode="json")])
        if request.url.path.endswith("/context"):
            assert payload == task.proof.model_dump(mode="json")
            return httpx.Response(
                200,
                json={
                    "connection": {
                        "revision_id": str(revision),
                        "connector": "weave-agentic-provider@1.0.0",
                        "config": {
                            "provider": "openai-chat",
                            "endpoint": "https://api.openai.com/v1",
                            "secretSlot": "apiKey",
                        },
                        "allowed_destinations": ["https://api.openai.com"],
                        "secret_slots": ["apiKey"],
                    },
                    "expires_at": task.expires_at.isoformat(),
                },
            )
        if request.url.path.endswith("/credentials"):
            assert payload["connection_revision_id"] == str(revision)
            assert payload["lease"] == task.proof.model_dump(mode="json")
            return httpx.Response(200, json={"value": "secret-canary", "expires_at": task.expires_at.isoformat()})
        assert request.url.path.endswith("/complete")
        assert payload["output"]["result"] == {"approved": True}
        assert "secret-canary" not in request.content.decode()
        await worker.stop()
        return httpx.Response(
            200,
            json={
                "task_id": str(task.proof.task_id),
                "generation": 1,
                "completion_id": payload["completion_id"],
                "accepted_output_hash": "a" * 64,
                "accepted_at": datetime.now(UTC).isoformat(),
                "status": "completed",
            },
        )

    async with httpx.AsyncClient(base_url="https://weave.example", transport=httpx.MockTransport(receive)) as client:
        transport = WorkerTransport(client, "/environment", task.proof.owner)
        worker = Worker(transport, {task.capability: handler(transport, fixture_model(calls))}, 1)
        await asyncio.wait_for(worker.run(), 3)
    assert routes == [
        "/environment/tasks/claim",
        "/environment/tasks/context",
        "/environment/tasks/credentials",
        "/environment/tasks/complete",
    ]
