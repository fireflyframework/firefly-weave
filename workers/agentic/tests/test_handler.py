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
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from firefly_weave import private_origins as po
from firefly_weave.ai_policy import PolicyFile, render
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.workers import CredentialLease, LeaseProof, TaskLease
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from weave_agentic_worker.handler import AgenticTaskHandler, WorkerPolicy
from weave_agentic_worker.providers import build_model

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
            parts = [TextPart('{"result":{"approved":true}}')]
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

    with pytest.raises(Exception, match="LLM_TIMEOUT") as failure:
        await handler(Transport(), FunctionModel(respond))(lease(timeoutSeconds=1))
    assert failure.value.outcome == "unknown" and cancelled.is_set()


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


@pytest.mark.parametrize("operation", ["context", "credentials"])
@pytest.mark.parametrize("code", ["WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"])
async def test_worker_recovers_preprovider_admission_without_reexecuting_model(operation, code):
    import httpx
    from firefly_weave.sdk.transport import WorkerTransport
    from firefly_weave.sdk.worker import Worker

    value = lease("none")
    requests, completions, failures, model_calls = [], [], [], []
    revision = uuid4()

    async def endpoint(request):
        body = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        if path == operation:
            requests.append(request.content)
            if len(requests) <= 4:
                assert not model_calls
                return httpx.Response(429, json={"code": code})
        if path == "context":
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
                    "expires_at": value.deadline.isoformat(),
                },
            )
        if path == "credentials":
            return httpx.Response(200, json={"value": "secret-canary", "expires_at": value.deadline.isoformat()})
        if path == "heartbeat":
            return httpx.Response(200, json=value.model_dump(mode="json"))
        (completions if path == "complete" else failures).append(body)
        return httpx.Response(
            200,
            json={
                "task_id": str(value.proof.task_id),
                "generation": 1,
                "completion_id": body.get("completion_id") or body["error"]["completion_id"],
                "accepted_output_hash": "sha256:accepted",
                "accepted_at": datetime.now(UTC).isoformat(),
                "status": "completed" if path == "complete" else "failed",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", value.proof.owner)
        handler = AgenticTaskHandler(
            transport,
            WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
            model_builder=lambda *_: fixture_model(model_calls),
        )
        await Worker(transport, {value.capability: handler}, 1)._execute(value)
    assert len(requests) == 5 and len(set(requests)) == 1
    assert not failures and len(completions) == len(model_calls) == 1
    assert completions[0]["output"]["result"] == {"approved": True}


@pytest.mark.parametrize("operation", ["context", "credentials"])
@pytest.mark.parametrize("code", ["WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"])
async def test_exhausted_preprovider_capacity_is_not_started(operation, code):
    import httpx
    from firefly_weave.contracts.connectors import ConnectorFailure
    from firefly_weave.sdk.transport import WorkerTransport

    requests, model_calls = [], []
    value = lease("none")

    async def endpoint(request):
        requests.append(request.content)
        return httpx.Response(429, json={"code": code})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        real = WorkerTransport(client, "/scope", value.proof.owner)
        transport = Transport()
        if operation == "context":
            transport.context = real.context
        else:
            transport.credentials = real.credentials
        handler = AgenticTaskHandler(
            transport,
            WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
            model_builder=lambda *_: fixture_model(model_calls),
        )
        with pytest.raises(ConnectorFailure) as failure:
            await handler(value)
    assert failure.value.code == "LLM_CAPACITY" and failure.value.outcome == "not_started"
    assert len(requests) == 3 and len(set(requests)) == 1 and not model_calls


@pytest.mark.parametrize("kind", ["unknown429", "malformed429", "invalid_code", "timeout"])
async def test_unclassified_credential_failure_remains_ambiguous_and_never_replayed(kind):
    import httpx
    from firefly_weave.sdk.transport import WorkerTransport

    value = lease("none")
    calls, model_calls = [], []

    async def endpoint(request):
        calls.append(True)
        if kind == "timeout":
            raise httpx.ReadTimeout("private wire outcome")
        if kind == "invalid_code":
            return httpx.Response(429, json={"code": ["WV-REQUEST-CAPACITY"]})
        return (
            httpx.Response(429, text="private malformed body")
            if kind == "malformed429"
            else httpx.Response(429, json={"code": "OTHER"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = Transport()
        transport.credentials = WorkerTransport(client, "/scope", value.proof.owner).credentials
        handler = AgenticTaskHandler(
            transport,
            WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"})),
            model_builder=lambda *_: fixture_model(model_calls),
        )
        with pytest.raises(httpx.HTTPError):
            await handler(value)
    assert calls == [True] and not model_calls


@pytest.mark.parametrize("operation", ["context", "credentials"])
async def test_owned_preprovider_profile_timeout_is_not_started(operation):
    import httpx
    from firefly_weave.sdk.transport import WorkerTransport
    from firefly_weave.sdk.worker import Worker

    value = lease(timeoutSeconds=1)
    calls, failures, model_calls = [], [], []

    async def endpoint(request):
        if request.url.path.endswith(operation):
            calls.append(True)
            return httpx.Response(429, json={"code": "WV-REQUEST-CAPACITY"})
        assert request.url.path.endswith("fail")
        body = json.loads(request.content)
        failures.append(body["error"])
        return httpx.Response(
            200,
            json={
                "task_id": str(value.proof.task_id),
                "generation": 1,
                "completion_id": body["error"]["completion_id"],
                "accepted_output_hash": "sha256:accepted",
                "accepted_at": datetime.now(UTC).isoformat(),
                "status": "failed",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        real = WorkerTransport(client, "/scope", value.proof.owner)
        preparation = Transport()
        if operation == "context":
            preparation.context = real.context
        else:
            preparation.credentials = real.credentials
        instance = handler(preparation, fixture_model(model_calls))
        await Worker(real, {value.capability: instance}, 1)._execute(value)
    assert len(calls) > 3 and not model_calls
    assert len(failures) == 1
    assert failures[0]["code"] == "LLM_TIMEOUT" and failures[0]["outcome"] == "not_started"


@pytest.mark.parametrize("end", ["cancel", "expiry"])
async def test_owned_preprovider_timeout_never_settles_after_authority_ends(end):
    import httpx
    from firefly_weave.sdk.transport import WorkerTransport
    from firefly_weave.sdk.worker import Worker

    value = lease(timeoutSeconds=1).model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=0.25)})
    retrying = asyncio.Event()
    calls, model_calls, settlements = [], [], []

    async def endpoint(request):
        if request.url.path.endswith("context"):
            calls.append(True)
            if len(calls) == 2:
                retrying.set()
            return httpx.Response(429, json={"code": "WV-REQUEST-CAPACITY"})
        if request.url.path.endswith("heartbeat"):
            await asyncio.Event().wait()
        settlements.append(True)
        raise AssertionError("Ended authority cannot settle")

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint), base_url="https://weave.test") as client:
        transport = WorkerTransport(client, "/scope", value.proof.owner)
        instance = handler(transport, fixture_model(model_calls))
        running = asyncio.create_task(Worker(transport, {value.capability: instance}, 1)._execute(value))
        await asyncio.wait_for(retrying.wait(), 1)
        if end == "cancel":
            running.cancel()
        with pytest.raises(asyncio.CancelledError if end == "cancel" else TimeoutError):
            await asyncio.wait_for(running, 1)
    assert not settlements and not model_calls


async def test_preprovider_nonbudget_timeout_remains_unknown():
    from firefly_weave.contracts.connectors import ConnectorFailure

    transport = Transport()

    async def timeout(proof):
        raise TimeoutError("private transport detail")

    transport.context = timeout
    with pytest.raises(ConnectorFailure) as failure:
        await handler(transport, fixture_model([]))(lease())
    assert failure.value.code == "LLM_TIMEOUT" and failure.value.outcome == "unknown"


OLLAMA_ORIGINS = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
    [po.PrivateOrigin(origin="http://ollama:11434", purpose="model", networks=("10.246.21.0/24",), credentials="none")]
)


def ollama_endpoint(**changes):
    return {
        "id": "ollama-local",
        "label": "Ollama (Weave-managed)",
        "url": "http://ollama:11434/v1",
        "providers": ["openai-chat"],
        "compat": "ollama",
        "credential": "none",
        "models": "served",
        "contextTokens": 8192,
        "structuredOutput": "native",
        "maxOutputTokens": 4096,
        **changes,
    }


def ollama_policy(tmp_path, **changes):
    path = tmp_path / f"ai-policy-{uuid4().hex}.json"
    path.write_bytes(render([ollama_endpoint(**changes)]))
    path.chmod(0o444)
    return WorkerPolicy(source=PolicyFile(path, OLLAMA_ORIGINS), origins=OLLAMA_ORIGINS)


class OllamaTransport(Transport):
    async def context(self, proof):
        context = await super().context(proof)
        context.connection.config = {
            "provider": "openai-chat",
            "endpoint": "http://ollama:11434/v1",
            "secretSlot": "apiKey",
        }
        context.connection.allowed_destinations = ("http://ollama:11434",)
        return context


def ollama_lease(**profile):
    return lease(model="qwen2.5:1.5b", **profile)


@pytest.mark.parametrize("mode", ["native", "tool", "prompted", "cloud-default"])
async def test_policy_selects_the_result_format_on_the_provider_request(tmp_path, mode):
    requests = []

    async def receive(request):
        body = json.loads(request.content)
        requests.append(body)
        message = {"role": "assistant", "content": '{"result":"Summary"}'}
        if mode in {"tool", "cloud-default"}:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": body["tools"][0]["function"]["name"], "arguments": '{"result":"Summary"}'},
                    }
                ],
            }
        return httpx2.Response(
            200,
            json={
                "id": "chat-1",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen2.5:1.5b",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    def builder(spec, secret, timeout, options):
        return build_model(spec, secret, timeout, options, transport=httpx2.MockTransport(receive))

    policy = (
        ollama_policy(tmp_path, structuredOutput=mode)
        if mode != "cloud-default"
        else WorkerPolicy(frozenset({("openai-chat", "qwen2.5:1.5b")}), frozenset({"https://api.openai.com/v1"}))
    )
    transport = OllamaTransport() if mode != "cloud-default" else Transport()
    with po.installed(OLLAMA_ORIGINS):
        output = await AgenticTaskHandler(transport, policy, model_builder=builder)(
            ollama_lease(outputSchema={"type": "string", "minLength": 1})
        )
    assert output["result"] == "Summary"
    assert output["usage"]["requests"] == 1 and len(requests) == 1
    body = requests[0]
    assert body.get("max_tokens", body.get("max_completion_tokens")) == 256
    if mode == "native":
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"]["properties"]["result"]["type"] == "string"
        assert not body.get("tools") and "tool_choice" not in body
    elif mode == "prompted":
        assert body["response_format"]["type"] == "json_object"
        assert not body.get("tools") and "tool_choice" not in body
    else:
        assert body["tools"][0]["function"]["parameters"]["properties"]["result"]["type"] == "string"
        assert "response_format" not in body


async def test_native_policy_uses_prompted_output_before_calling_an_unsupported_model(tmp_path):
    from pydantic_ai.profiles import ModelProfile

    calls = []

    def respond(messages, info):
        calls.append(info)
        return ModelResponse(parts=[TextPart('{"result":{"approved":true}}')])

    model = FunctionModel(respond, profile=ModelProfile(supports_json_schema_output=False))
    with po.installed(OLLAMA_ORIGINS):
        output = await AgenticTaskHandler(OllamaTransport(), ollama_policy(tmp_path), model_builder=lambda *_: model)(
            ollama_lease()
        )
    assert output["result"] == {"approved": True}
    assert len(calls) == 1 and not calls[0].output_tools


@pytest.mark.parametrize("answer,code", [("rejected-format", "LLM_PROVIDER"), ("invalid-result", "LLM_OUTPUT")])
async def test_a_native_result_failure_does_not_repeat_the_request(tmp_path, answer, code):
    requests = []

    async def receive(request):
        requests.append(json.loads(request.content))
        if answer == "rejected-format":
            return httpx2.Response(400, json={"error": {"message": "response_format is unsupported"}})
        return httpx2.Response(
            200,
            json={
                "id": "chat-1",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen2.5:1.5b",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": '{"result":123}'}, "finish_reason": "stop"}
                ],
            },
        )

    def builder(spec, secret, timeout, options):
        return build_model(spec, secret, timeout, options, transport=httpx2.MockTransport(receive))

    with po.installed(OLLAMA_ORIGINS), pytest.raises(ConnectorFailure) as failed:
        await AgenticTaskHandler(OllamaTransport(), ollama_policy(tmp_path), model_builder=builder)(
            ollama_lease(outputSchema={"type": "string", "minLength": 1})
        )
    assert failed.value.code == code and len(requests) == 1
    assert requests[0]["response_format"]["type"] == "json_schema"


async def test_keyless_ollama_calls_never_request_a_credential(tmp_path):
    transport, built = OllamaTransport(), []

    def builder(spec, secret, timeout, options):
        built.append((spec, secret, options))
        return fixture_model([])

    with po.installed(OLLAMA_ORIGINS):
        output = await AgenticTaskHandler(transport, ollama_policy(tmp_path), model_builder=builder)(ollama_lease())
    assert output["result"] == {"approved": True} and output["model"] == "qwen2.5:1.5b"
    assert [kind for kind, _ in transport.requests] == ["context"]
    spec, secret, options = built[0]
    assert secret == "" and spec.base_url == "http://ollama:11434/v1"
    assert options.endpoint.compat == "ollama" and options.origins is OLLAMA_ORIGINS


async def test_an_exact_list_without_the_model_refuses_before_the_context(tmp_path):
    transport = OllamaTransport()
    policy = ollama_policy(tmp_path, models=["qwen3:4b"])
    with po.installed(OLLAMA_ORIGINS), pytest.raises(ConnectorFailure, match="LLM_POLICY"):
        await AgenticTaskHandler(transport, policy, model_builder=lambda *_: fixture_model([]))(ollama_lease())
    assert transport.requests == []


async def test_a_missing_served_model_reports_model_not_found(tmp_path):
    def missing(messages, info):
        raise ModelHTTPError(404, "qwen2.5:1.5b", body={"message": "model 'qwen2.5:1.5b' not found"})

    handler_ = AgenticTaskHandler(
        OllamaTransport(), ollama_policy(tmp_path), model_builder=lambda *_: FunctionModel(missing)
    )
    with po.installed(OLLAMA_ORIGINS), pytest.raises(ConnectorFailure) as failed:
        await handler_(ollama_lease())
    assert (failed.value.code, failed.value.outcome) == ("LLM_MODEL_NOT_FOUND", "not_started")


async def test_the_policy_context_and_output_limits_apply(tmp_path):
    calls = []
    with po.installed(OLLAMA_ORIGINS):
        small = AgenticTaskHandler(
            OllamaTransport(),
            ollama_policy(tmp_path, contextTokens=512),
            model_builder=lambda *_: fixture_model(calls),
        )
        with pytest.raises(ConnectorFailure, match="LLM_CONTEXT_LIMIT"):
            await small(ollama_lease(options={"max_tokens": 1024}))
        capped = AgenticTaskHandler(
            OllamaTransport(),
            ollama_policy(tmp_path, maxOutputTokens=128),
            model_builder=lambda *_: fixture_model(calls),
        )
        with pytest.raises(ConnectorFailure, match="LLM_OPTIONS"):
            await capped(ollama_lease())
    assert calls == []


async def test_plain_http_without_this_process_entry_is_refused(tmp_path):
    policy = ollama_policy(tmp_path)
    with po.installed(po.PrivateOrigins.empty()), pytest.raises(ConnectorFailure, match="LLM_POLICY"):
        await AgenticTaskHandler(OllamaTransport(), policy, model_builder=lambda *_: fixture_model([]))(ollama_lease())


async def test_an_invalid_policy_edit_fails_closed_until_fixed(tmp_path):
    path = tmp_path / "ai-policy.json"
    path.write_bytes(render([ollama_endpoint()]))
    path.chmod(0o644)
    policy = WorkerPolicy(source=PolicyFile(path, OLLAMA_ORIGINS), origins=OLLAMA_ORIGINS)
    worker = AgenticTaskHandler(OllamaTransport(), policy, model_builder=lambda *_: fixture_model([]))
    path.write_text("{")
    os.utime(path, ns=(1, 1))
    with po.installed(OLLAMA_ORIGINS):
        with pytest.raises(ConnectorFailure, match="LLM_POLICY"):
            await worker(ollama_lease())
        path.write_bytes(render([ollama_endpoint()]))
        os.utime(path, ns=(2, 2))
        assert (await worker(ollama_lease()))["result"] == {"approved": True}


async def test_a_credentialed_endpoint_requests_its_credential_and_passes_it_on():
    transport, built = Transport(), []

    def builder(spec, secret, timeout, options):
        built.append((secret, options))
        return fixture_model([])

    policy = WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({"https://api.openai.com/v1"}))
    await AgenticTaskHandler(transport, policy, model_builder=builder)(lease())
    assert [kind for kind, _ in transport.requests] == ["context", "credentials"]
    secret, options = built[0]
    assert secret == "secret-canary" and options.endpoint.credential == "required"


async def test_a_refused_destination_is_a_policy_failure_before_any_credential():
    endpoint = "https://10.0.0.5/v1"

    class PrivateLiteralTransport(Transport):
        async def context(self, proof):
            context = await super().context(proof)
            context.connection.config["endpoint"] = endpoint
            context.connection.allowed_destinations = ("https://10.0.0.5",)
            return context

    transport, built = PrivateLiteralTransport(), []
    policy = WorkerPolicy(frozenset({("openai-chat", "fixture-model")}), frozenset({endpoint}))
    worker = AgenticTaskHandler(transport, policy, model_builder=lambda *args: built.append(args))
    with po.installed(po.PrivateOrigins.empty()), pytest.raises(ConnectorFailure) as failed:
        await worker(lease())
    assert (failed.value.code, failed.value.outcome) == ("LLM_POLICY", "not_started")
    assert [kind for kind, _ in transport.requests] == ["context"] and built == []


async def test_a_refused_model_address_reports_policy_through_the_real_transport(tmp_path):
    from dataclasses import replace

    resolved = []

    async def outside_the_entry(host, port):
        resolved.append((host, port))
        return ("10.99.0.5",)

    policy = replace(ollama_policy(tmp_path), resolver=outside_the_entry)
    with po.installed(OLLAMA_ORIGINS), pytest.raises(ConnectorFailure) as failed:
        await AgenticTaskHandler(OllamaTransport(), policy)(ollama_lease())
    assert (failed.value.code, failed.value.outcome) == ("LLM_POLICY", "not_started")
    assert resolved == [("ollama", 11434)]
