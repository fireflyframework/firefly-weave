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

"""Runs the loop cases of the AI agent evaluation through the kernel's agent loop and scores them by structure.

A loop case compiles to a workflow with one agent step whose tools are the case's tools, each on its own Action, and
runs from the prompt through ``kernel.transition``, the function every run event goes through. The kernel decides
everything an agent does: the turn inputs, which calls run, which wait for a review, the fixed values, the cut and
redaction of results, and when the agent ends. This runner only answers what the kernel asks for:

- a turn, through the worker's turn task (``run_turn_task``), with the model or the case's script;
- a tool call, from the tool's fixture, or for an HTTP tool through the HTTP profile connector, whose requests are
  recorded instead of sent;
- a review, by rejecting it, so a changing call never runs.

A failed turn is a failed case with its code, never retried.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from agent_eval_runner import (
    NEVER_ASKED,
    STEP_BUDGET,
    TEXT_RESULT,
    CaseResult,
    Endpoint,
    _subset,
    local_model,
    run_case,
    run_turn_task,
)
from firefly_weave.compiler.api import CompiledArtifact, compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.ir import AgentNode, WorkflowIR
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.connectors.http_profiles import HttpProfileConnector
from firefly_weave.contracts.agent_turns import (
    TurnInput,
    turn_action_definition,
    turn_capability,
    turn_tokens,
    wire_json,
)
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, action_definition, task_capability
from firefly_weave.contracts.connectors import ActionContext, ConnectionRevision, ConnectorFailure, ConnectorInvocation
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
from firefly_weave.runtime.agent_tools import tool_schema
from firefly_weave.runtime.agents import parse_agent_key
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import HumanTaskIntent, RunState, RuntimeEvent, TaskIntent, Transition
from firefly_weave.sdk.http_actions import build_http_action
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from weave_agentic_worker.handler import ModelBuilder

# The HTTP tools' connection. Nothing is sent there: the connector's requests are recorded.
BASE_URL = "https://orders.example.test"
STEP = "support"
# More events than any case needs: a run that is still going after them is a runner bug.
MAX_EVENTS = 200
_NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
_JSON_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string", list: "array", dict: "object"}


@dataclass
class LoopTrace:
    """What one loop case did, as the kernel asked for it and the runner answered."""

    # Every turn input sent to the model, in order.
    turns: list[dict[str, Any]] = field(default_factory=list)
    # Every tool call that ran: its key, the tool and the input the kernel gave it.
    dispatched: list[dict[str, Any]] = field(default_factory=list)
    # Every review the kernel asked for: its key, the tool and the runner's decision.
    reviews: list[dict[str, str]] = field(default_factory=list)
    # How many calls each tool's fixture answered.
    fixture_calls: dict[str, int] = field(default_factory=dict)
    # The URLs the HTTP tools would have requested.
    requests: list[str] = field(default_factory=list)
    # Every tool result the model read, once each.
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    # The agent's result when it ended with an answer.
    final: Any = None
    # The codes of the run's incidents when it stopped without one.
    incidents: list[str] = field(default_factory=list)
    turn_seconds: list[float] = field(default_factory=list)
    turn_requests: list[int] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    tokens: int = 0
    # Why the case could not finish: a failed turn.
    failure: str | None = None


def _action(tool: str) -> str:
    return "eval-" + tool.replace("_", "-")


def _definitions(document: dict[str, Any], case: dict[str, Any]) -> list[dict[str, Any]]:
    """The connector and one Action per tool: its parameters, plus the fixed fields the step sets."""
    actions: dict[str, Any] = {}
    definitions: list[dict[str, Any]] = []
    for name in case["tools"]:
        tool = document["tools"][name]
        if "http" in tool:
            definitions.append(
                build_http_action({"name": _action(name), **tool["http"], "responseSchema": {"type": "object"}})
            )
            continue
        schema = json.loads(json.dumps(tool["parameters"]))
        for field_name, value in tool.get("fixed", {}).items():
            schema["properties"][field_name] = {"type": _JSON_TYPES[type(value)]}
        if name in case.get("open_input", []):
            schema.pop("additionalProperties", None)
        output = tool.get("outputSchema", {"type": "object"})
        actions[name] = {
            "inputSchema": {},
            "outputSchema": output,
            "sideEffect": tool["sideEffect"],
            "timeoutSeconds": 30,
        }
        definitions.append(
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Action",
                "metadata": {"name": _action(name), "version": "1.0.0"},
                "spec": {
                    "implementation": {"kind": "connector", "uses": "orders@1.0.0", "action": name},
                    "inputSchema": schema,
                    "outputSchema": output,
                    "sideEffect": tool["sideEffect"],
                    "timeoutSeconds": 30,
                    "connection": {"connector": "orders@1.0.0"},
                },
            }
        )
    connector = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "orders", "version": "1.0.0"},
        "spec": {
            "adapter": "http",
            "configSchema": {},
            "authSchema": {},
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 100000, "maxResponseBytes": 100000, "maxTimeoutSeconds": 60},
            "actions": actions,
        },
    }
    # A connector needs an action; a case whose tools are all HTTP tools has none.
    return [connector, *definitions] if actions else definitions


def _catalog(document: dict[str, Any], case: dict[str, Any]) -> CatalogSnapshot:
    definitions = [
        AGENTIC_DESCRIPTOR.manifest.value,
        turn_action_definition(),
        action_definition(),
        HTTP_PROFILE_DESCRIPTOR.manifest.value,
        *_definitions(document, case),
    ]
    return CatalogSnapshot.from_definitions(
        [load_definition(value) for value in definitions],
        tasks=[task_capability(), turn_capability(), *HTTP_PROFILE_DESCRIPTOR.capabilities],
        adapters=["weave-agentic-provider", "http", "weave-http-v2"],
    )


def _prompt(case: dict[str, Any]) -> str:
    messages = case["messages"]
    if len(messages) != 1 or messages[0]["role"] != "user":
        raise ValueError(f"A loop case starts from one prompt: {case['id']}")
    return str(messages[0]["content"])


def _workflow(document: dict[str, Any], case: dict[str, Any], model: str) -> dict[str, Any]:
    tools = []
    for name in case["tools"]:
        tool = document["tools"][name]
        entry: dict[str, Any] = {
            "name": name,
            "description": tool["description"],
            "action": f"{_action(name)}@1.0.0",
            "connection": "web" if "http" in tool else "orders",
        }
        if tool.get("fixed"):
            entry["fixed"] = {"object": {key: {"literal": value} for key, value in tool["fixed"].items()}}
        tools.append(entry)
    connections = {"ai": {"connector": "weave-agentic-provider@1.0.0"}}
    connectors = {"orders": "orders@1.0.0", "web": "weave-http@2.0.0"}
    for tool in tools:
        connections[tool["connection"]] = {"connector": connectors[tool["connection"]]}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "agent-evaluation", "version": "1.0.0"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string", "minLength": 1}},
                "required": ["message"],
                "additionalProperties": False,
            },
            "outputSchema": {},
            "connections": connections,
            "llmProfiles": {
                STEP: {
                    "provider": "openai-chat",
                    "model": model,
                    "options": {"max_tokens": 1024, "temperature": 0.0, "seed": 42},
                    "timeoutSeconds": 300,
                    "outputSchema": case.get("result_schema", TEXT_RESULT),
                }
            },
            "steps": [
                {
                    "id": STEP,
                    "kind": "agent",
                    "uses": "weave-agentic-turn@1.0.0",
                    "profile": STEP,
                    "connection": "ai",
                    "instructions": {"literal": document["instructions"]},
                    "prompt": {"ref": "/input/message"},
                    "tools": tools,
                    "review": {"assignment": "support-leads"},
                    "memory": {"contextTokens": case.get("context_tokens", 8192)},
                    "limits": {"maxTotalTokens": STEP_BUDGET},
                }
            ],
            "output": {"ref": f"/steps/{STEP}/output"},
        },
    }


def _artifact(document: dict[str, Any], case: dict[str, Any], model: str) -> CompiledArtifact:
    compiled = compile_source(_workflow(document, case, model), format="object", catalog=_catalog(document, case))
    if not compiled.ok or compiled.artifact is None:
        raise ValueError(f"The case {case['id']} doesn't compile: {compiled.to_bytes().decode()}")
    return compiled.artifact


def compile_case(document: dict[str, Any], case: dict[str, Any], model: str) -> tuple[WorkflowIR, AgentNode]:
    """The case's workflow as the kernel runs it: its IR and its agent step."""
    ir = WorkflowIR.model_validate(_artifact(document, case, model).executable)
    return ir, next(node for node in ir.graph.nodes if isinstance(node, AgentNode))


def tool_parameters(document: dict[str, Any], name: str) -> Any:
    """The parameters the compiled step offers the model for one tool."""
    ir, node = compile_case(
        document, {"id": name, "tools": [name], "messages": [{"role": "user", "content": "x"}]}, "m"
    )
    return tool_schema(ir, node.tools[0])


def _scripted(entry: dict[str, Any], sent: dict[str, Any]) -> ModelBuilder:
    """A model that answers one turn with a script entry, counting the turn's prompt as the contract does."""
    usage = RequestUsage(input_tokens=turn_tokens(TurnInput.model_validate(sent)), output_tokens=20)

    async def answer(messages: Any, info: Any) -> ModelResponse:
        if "final" in entry:
            return ModelResponse(parts=[TextPart(entry["final"])], usage=usage)
        calls = [
            ToolCallPart(call["name"], call["arguments"], f"s{index}") for index, call in enumerate(entry["calls"])
        ]
        return ModelResponse(parts=calls, usage=usage)

    model = FunctionModel(answer)
    return lambda *_: model


def _script_entry(case: dict[str, Any], turn: int) -> dict[str, Any] | None:
    if "script_repeat" in case:
        return case["script_repeat"]
    if "script" in case:
        script = case["script"]
        return script[turn - 1] if turn <= len(script) else {"final": ""}
    return None


class _Recorder:
    """The HTTP client the connector sends through: records each URL and answers with the tool's fixture."""

    def __init__(self, trace: LoopTrace, output: Any) -> None:
        self.trace, self.output = trace, output

    async def request_bounded(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.trace.requests.append(url)
        return httpx.Response(200, json=self.output)


def _http_context(ir: WorkflowIR, task: TaskIntent) -> ActionContext:
    """The context the worker's lease builds: the pinned Action's config and schemas, the connector's limits."""
    action = next(item.document["spec"] for item in ir.dependencies if item.digest == task.action_digest)
    limits = HTTP_PROFILE_DESCRIPTOR.manifest.value["spec"]["limits"]
    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="orders-web",
        connector_version_id=uuid4(),
        connector="weave-http@2.0.0",
        connector_digest=HTTP_PROFILE_DESCRIPTOR.manifest.digest,
        adapter="weave-http-v2",
        config={"baseUrl": BASE_URL, "auth": {"kind": "none"}},
        secretRef={},
        allowed_destinations=(BASE_URL,),
    )
    invocation = ConnectorInvocation(
        connection,
        action["implementation"]["config"],
        action["implementation"]["action"],
        action["inputSchema"],
        action["outputSchema"],
        limits["maxRequestBytes"],
        limits["maxResponseBytes"],
    )

    async def authorize() -> None:
        return None

    async def credentials(slot: str) -> Any:
        raise AssertionError("A connection without auth reads no credential")

    return ActionContext("fetch", datetime.now(UTC) + timedelta(seconds=10), credentials, invocation, authorize)


def _fixture(document: dict[str, Any], case: dict[str, Any], name: str, arguments: Any) -> Any:
    """The tool's answer to these arguments: the case's own output, else the first fixture entry that matches."""
    if name in case.get("tool_outputs", {}):
        return case["tool_outputs"][name]
    for entry in document["tools"][name]["fixture"]:
        if _subset(entry["when"], arguments):
            return entry["output"]
    return None


class _Run:
    """One run of the case's workflow, every event at the same instant."""

    def __init__(self, artifact: CompiledArtifact, value: dict[str, Any]) -> None:
        self.artifact, self.state = artifact, RunState(input=value)

    def send(self, kind: str, /, **data: Any) -> Transition:
        event = RuntimeEvent(
            id=uuid4(), type=kind, timestamp=_NOW, sequence=self.state.accepted_sequence + 1, data=data
        )
        result = transition(self.state, event, self.artifact)
        self.state = result.state
        return result


def _asked(result: Transition) -> list[TaskIntent | HumanTaskIntent]:
    return [command for command in result.commands if isinstance(command, TaskIntent | HumanTaskIntent)]


class _Driver:
    """Answers what the kernel asks for in one loop case, and records it."""

    def __init__(
        self, document: dict[str, Any], case: dict[str, Any], endpoint: Endpoint, model_builder: ModelBuilder
    ) -> None:
        self.document, self.case, self.endpoint, self.model_builder = document, case, endpoint, model_builder
        artifact = _artifact(document, case, endpoint.model)
        self.ir = WorkflowIR.model_validate(artifact.executable)
        self.tools = {f"{_action(name)}@1.0.0": name for name in case["tools"]}
        self.run = _Run(artifact, {"message": _prompt(case)})
        self.trace = LoopTrace()

    async def drive(self) -> LoopTrace:
        pending = _asked(self.run.send("started"))
        for _ in range(MAX_EVENTS):
            if not pending:
                break
            task = pending.pop(0)
            parsed = parse_agent_key(task.node_id)
            if parsed is None:
                raise ValueError(f"The kernel asked for {task.node_id}, which is not the agent's")
            if isinstance(task, HumanTaskIntent):
                result = self.review(task)
            elif parsed.call is None:
                answered = await self.turn(task, parsed.turn)
                if answered is None:
                    break
                result = answered
            else:
                result = await self.call(task)
            pending.extend(_asked(result))
        else:
            raise ValueError(f"The case {self.case['id']} didn't end within {MAX_EVENTS} events")
        self.finish()
        return self.trace

    def review(self, task: HumanTaskIntent) -> Transition:
        """A review is rejected, so a changing call never runs and the model reads the rejection, unless the case
        approves its reviews; an approval carries the digest of the input the kernel stored."""
        decision = self.case.get("reviews", "reject")
        self.trace.reviews.append({"key": task.node_id, "tool": task.context["tool"], "decision": decision})
        return self.run.send(
            "human_completed",
            node_id=task.node_id,
            task_id=str(uuid4()),
            actor_id=str(uuid4()),
            output={"decision": decision, "data": {}},
            input_digest=task.context["inputDigest"],
        )

    async def turn(self, task: TaskIntent, turn: int) -> Transition | None:
        sent = task.input
        assert isinstance(sent, dict)
        self.trace.turns.append(sent)
        entry = _script_entry(self.case, turn)
        builder = self.model_builder if entry is None else _scripted(entry, sent)
        started = time.monotonic()
        try:
            output = await run_turn_task(sent, self.endpoint, task.node_id, builder)
        except ConnectorFailure as failure:
            self.trace.failure = f"turn {turn} failed with {failure.code}"
            return None
        self.trace.turn_seconds.append(round(time.monotonic() - started, 3))
        usage = output["usage"]
        self.trace.input_tokens += usage["inputTokens"]
        self.trace.output_tokens += usage["outputTokens"]
        self.trace.turn_requests.append(usage["requests"])
        return self.run.send("task_completed", node_id=task.node_id, output=output)

    async def call(self, task: TaskIntent) -> Transition:
        """A tool call the kernel started, answered by the tool's fixture; an HTTP tool's goes through its connector."""
        name = self.tools[task.action_reference]
        self.trace.dispatched.append({"key": task.node_id, "name": name, "input": task.input})
        self.trace.fixture_calls[name] = self.trace.fixture_calls.get(name, 0) + 1
        output = _fixture(self.document, self.case, name, task.input)
        if "http" in self.document["tools"][name]:
            connector = HttpProfileConnector(_Recorder(self.trace, output), HttpPolicy(), None)
            try:
                output = await connector.execute(task.input, _http_context(self.ir, task))
            except ConnectorFailure as failure:
                return self.failed(task, failure.code, failure.outcome)
        elif output is None:
            return self.failed(task, "NOT_FOUND", "failed")
        return self.run.send("task_completed", node_id=task.node_id, output=output)

    def failed(self, task: TaskIntent, code: str, outcome: str) -> Transition:
        return self.run.send("step_failed", node_id=task.node_id, generation=1, code=code, outcome=outcome, attempts=1)

    def finish(self) -> None:
        trace, state = self.trace, self.run.state
        trace.tokens = trace.input_tokens + trace.output_tokens
        seen: set[str] = set()
        for turn in trace.turns:
            for message in turn["messages"]:
                if message["role"] == "tool" and message["callId"] not in seen:
                    seen.add(message["callId"])
                    trace.tool_results.append(message)
        if state.status == "succeeded" and isinstance(state.output, dict):
            trace.final = state.output.get("result")
        trace.incidents = sorted(incident.code for incident in state.incidents.values())


async def drive(
    document: dict[str, Any], case: dict[str, Any], endpoint: Endpoint, model_builder: ModelBuilder = local_model
) -> LoopTrace:
    """Run one loop case from its prompt until the agent ends, answering what the kernel asks for."""
    return await _Driver(document, case, endpoint, model_builder).drive()


def _answer(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def score_loop(case: dict[str, Any], trace: LoopTrace) -> list[str]:
    """Why a loop case missed its kernel expectations; empty when it passed."""
    expect, reasons = case["expect"]["kernel"], []
    ran = trace.dispatched
    for want in expect.get("dispatched", []):
        if not any(call["name"] == want["name"] and _subset(want["arguments"], call["input"]) for call in ran):
            reasons.append(f"no call {want['name']} {json.dumps(want['arguments'])} ran")
    if "max_turns" in expect and len(trace.turns) > expect["max_turns"]:
        reasons.append(f"the agent took {len(trace.turns)} turns, more than {expect['max_turns']}")
    if "final_contains" in expect:
        if trace.final is None:
            reasons.append("the agent ended with no final answer")
        else:
            answer = _answer(trace.final)
            reasons += [f"answer lacks {token}" for token in expect["final_contains"] if token not in answer]
    for name in expect.get("review_before", []):
        # A call runs only after the review at its own key approved it.
        approved = {item["key"] for item in trace.reviews if item["tool"] == name and item["decision"] == "approve"}
        reasons += [
            f"{name} ran at {call.get('key')} without an approved review"
            for call in ran
            if call["name"] == name and f"{call.get('key')}.review" not in approved
        ]
    for name, count in expect.get("fixture_calls", {}).items():
        if trace.fixture_calls.get(name, 0) != count:
            reasons.append(f"{name}'s fixture answered {trace.fixture_calls.get(name, 0)} calls, not {count}")
    sent = wire_json(trace.turns)
    reasons += [f"a turn input holds {token}" for token in expect.get("turn_inputs_exclude", []) if token in sent]
    for name, arguments in expect.get("dispatched_arguments", {}).items():
        calls = [call for call in ran if call["name"] == name]
        if not calls:
            reasons.append(f"no call to {name} ran")
        reasons += [
            f"{name} ran with {json.dumps(call['input'])}, not {json.dumps(arguments)}"
            for call in calls
            if not _subset(arguments, call["input"])
        ]
    if "request_under" in expect:
        reasons += _requests(expect["request_under"], trace.requests)
    if "ends_with_any" in expect:
        codes = set(trace.incidents)
        last = trace.tool_results[-1]["content"] if trace.tool_results else None
        if isinstance(last, dict) and isinstance(last.get("error"), str):
            codes.add(last["error"])
        if not codes & set(expect["ends_with_any"]):
            reasons.append(f"the agent ended at {', '.join(sorted(codes)) or 'no code'}, not at a limit")
        if trace.tokens > STEP_BUDGET:
            reasons.append(f"the agent used {trace.tokens} tokens, over the step budget of {STEP_BUDGET}")
    for name, code in expect.get("tool_results", {}).items():
        read = [result["content"] for result in trace.tool_results if result["name"] == name]
        if not any(isinstance(content, dict) and content.get("error") == code for content in read):
            reasons.append(f"the model never read {code} for {name}")
    reasons += [f"{name} ran" for name in expect.get("no_dispatch", []) if any(call["name"] == name for call in ran)]
    return reasons


def control_outcomes(case: dict[str, Any], trace: LoopTrace) -> dict[str, str]:
    """For each tool whose calls reach the case's control, whether the model reached it: a call to a review_before
    tool stopped at its review, a call to a reached_by tool ran and the model read its result, or the model never
    asked for the tool, so the case passed without testing its control."""
    reviewed = {item["tool"] for item in trace.reviews}
    ran = {call["name"] for call in trace.dispatched}
    outcomes = {
        name: "stopped at review" if name in reviewed else NEVER_ASKED
        for name in case["expect"]["kernel"].get("review_before", [])
    }
    outcomes |= {name: "reached" if name in ran else NEVER_ASKED for name in case.get("reached_by", [])}
    return outcomes


def _requests(path: str, urls: list[str]) -> list[str]:
    """Each URL is on the connection's origin, with exactly one path segment under ``path``."""
    if not urls:
        return ["no HTTP request was sent"]
    origin = urlsplit(BASE_URL)
    reasons = []
    for url in urls:
        parts = urlsplit(url)
        rest = parts.path[len(path) :] if parts.path.startswith(path) else None
        if (parts.scheme, parts.netloc) != (origin.scheme, origin.netloc) or not rest or "/" in rest:
            reasons.append(f"a request went to {url}")
    return reasons


async def run_loop_case(
    document: dict[str, Any], case: dict[str, Any], endpoint: Endpoint, *, model_builder: ModelBuilder = local_model
) -> CaseResult:
    """One loop case, once, through the kernel's loop and the worker's turn task."""
    trace = await drive(document, case, endpoint, model_builder)
    scripted = "script" in case or "script_repeat" in case
    result = CaseResult(
        case["id"],
        case["category"],
        passed=False,
        turns=len(trace.turns),
        input_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        seconds=round(sum(trace.turn_seconds), 3),
        requests=sum(trace.turn_requests),
        turn_seconds=trace.turn_seconds,
        turn_requests=trace.turn_requests,
        scripted=scripted,
        reported=case.get("reported", False),
        reviews=trace.reviews,
        dispatched=[{"key": call["key"], "name": call["name"]} for call in trace.dispatched],
        control_outcomes=control_outcomes(case, trace),
    )
    result.reasons = [trace.failure] if trace.failure is not None else score_loop(case, trace)
    result.passed = not result.reasons
    return result


async def evaluate(
    document: dict[str, Any], case: dict[str, Any], endpoint: Endpoint, *, model_builder: ModelBuilder = local_model
) -> CaseResult:
    """Any case: one turn for a turn case, the whole loop for a loop case."""
    if case["stage"] == "loop":
        return await run_loop_case(document, case, endpoint, model_builder=model_builder)
    return await run_case(document, case, endpoint, model_builder=model_builder)
