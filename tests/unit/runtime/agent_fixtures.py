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

"""Compiled AI agent workflows and recorded turn outputs for the runtime tests."""

import copy
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

# The compiler tests own the agent workflow documents and their catalog; the runtime tests compile the same ones.
sys.path.insert(0, str(Path(__file__).parents[1] / "compiler"))

from test_agent_workflows import CANCEL, LOOKUP, agent, catalog, workflow  # noqa: E402

from firefly_weave.compiler.api import CompiledArtifact, compile_source  # noqa: E402
from firefly_weave.compiler.ir import AgentNode, WorkflowIR  # noqa: E402
from firefly_weave.contracts.agent_turns import TurnOutput  # noqa: E402
from firefly_weave.runtime.kernel import transition  # noqa: E402
from firefly_weave.runtime.models import RunState, RuntimeEvent, TaskIntent, Transition  # noqa: E402

__all__ = [
    "CANCEL",
    "INPUT",
    "LOOKUP",
    "NOW",
    "SHIPPED",
    "Run",
    "agent",
    "answering",
    "artifact",
    "compiled",
    "final",
    "node_of_ir",
    "tasks",
    "tool_calls",
    "workflow",
]

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def artifact(document=None, snapshot=None) -> CompiledArtifact:
    result = compile_source(document or workflow(), format="object", catalog=snapshot or catalog())
    assert result.ok, result.to_bytes()
    assert result.artifact is not None
    return result.artifact


def compiled(document=None, snapshot=None) -> WorkflowIR:
    return WorkflowIR.model_validate(artifact(document, snapshot).executable)


def node_of_ir(ir: WorkflowIR, identifier: str = "support") -> AgentNode:
    return next(node for node in ir.graph.nodes if isinstance(node, AgentNode) and node.id == identifier)


def usage(tokens: int = 100) -> dict:
    return {"requests": 1, "inputTokens": tokens - 10, "outputTokens": 10}


def tool_calls(*calls: tuple[str, dict], turn: int = 1, tokens: int = 100, text: str = "") -> TurnOutput:
    return TurnOutput.model_validate(
        {
            "kind": "tool_calls",
            "text": text,
            "toolCalls": [
                {"id": f"t{turn}c{index}", "name": name, "arguments": arguments}
                for index, (name, arguments) in enumerate(calls, start=1)
            ],
            "usage": usage(tokens),
            "provider": "openai-chat",
            "model": "qwen3:4b",
            "finishReason": "tool_calls",
        }
    )


def final(result="Order A-17 has shipped.", tokens: int = 100) -> TurnOutput:
    return TurnOutput.model_validate(
        {
            "kind": "final",
            "text": "",
            "result": result,
            "usage": usage(tokens),
            "provider": "openai-chat",
            "model": "qwen3:4b",
            "finishReason": "stop",
        }
    )


INPUT = {"message": "Where is order A-17?", "region": "eu", "orders": ["A-17"]}
SHIPPED = {"status": "shipped-7731"}


def plain(document):
    # A run's input can't hold secret values, so these runs leave out the secret fields of the compiler's fixture.
    document = copy.deepcopy(document)
    schema = document["spec"]["inputSchema"]
    for name in ("apiKey", "password"):
        del schema["properties"][name]
    schema["required"] = [name for name in schema["required"] if name in schema["properties"]]
    return document


def answering(document):
    document["spec"]["output"] = {"ref": "/steps/support/output/result"}
    return document


class Run:
    def __init__(self, document=None, value=None, snapshot=None) -> None:
        self.flow = artifact(plain(document or answering(workflow(agent(tools=[LOOKUP])))), snapshot)
        self.state = RunState(input=value or INPUT)
        self.at = NOW

    def send(self, kind: str, /, **data) -> Transition:
        event = RuntimeEvent(
            id=uuid4(), type=kind, timestamp=self.at, sequence=self.state.accepted_sequence + 1, data=data
        )
        result = transition(self.state, event, self.flow)
        self.state = result.state
        return result

    def answer(self, key: str, output) -> Transition:
        value = (
            output.model_dump(mode="json", by_alias=True, exclude_none=True)
            if hasattr(output, "model_dump")
            else output
        )
        return self.send("task_completed", node_id=key, output=value)


def tasks(result: Transition) -> dict[str, TaskIntent]:
    return {command.node_id: command for command in result.commands if isinstance(command, TaskIntent)}
