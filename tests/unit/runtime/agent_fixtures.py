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

import sys
from datetime import UTC, datetime
from pathlib import Path

# The compiler tests own the agent workflow documents and their catalog; the runtime tests compile the same ones.
sys.path.insert(0, str(Path(__file__).parents[1] / "compiler"))

from test_agent_workflows import CANCEL, LOOKUP, agent, catalog, workflow  # noqa: E402

from firefly_weave.compiler.api import compile_source  # noqa: E402
from firefly_weave.compiler.ir import AgentNode, WorkflowIR  # noqa: E402
from firefly_weave.contracts.agent_turns import TurnOutput  # noqa: E402

__all__ = ["CANCEL", "LOOKUP", "NOW", "agent", "compiled", "final", "node_of_ir", "tool_calls", "workflow"]

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def compiled(document=None, snapshot=None) -> WorkflowIR:
    result = compile_source(document or workflow(), format="object", catalog=snapshot or catalog())
    assert result.ok, result.to_bytes()
    assert result.artifact is not None
    return WorkflowIR.model_validate(result.artifact.executable)


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
