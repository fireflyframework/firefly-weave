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

"""A new kind of tool needs a target in the runtime's registry and nothing else at run time.

A test-only kind, ``probe``, runs each call as a task of a fake task type. It is registered the way a real kind would
be: one entry in ``TOOL_TARGETS``, and its pinned fields in the IR's pin check (``check_pinned``), which sits next to
the compiler's resolvers. The pinned IR names it, with the IR's closed list of kinds widened for the test. An agent
that calls it then goes through the kernel, replay, exports and the agent log with no other change. A kind the pin
check doesn't know is refused, whatever fields its tool carries.

Not covered here: compiling such a tool, because a workflow definition names a tool's target by a field (``action``,
``workflow`` or ``human``), so a new kind also adds a field there and its resolver; and the simulator, whose agent and
tool mocks are not built yet.
"""

import copy
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "operations"))

from agent_fixtures import (  # noqa: E402
    LOOKUP,
    NOW,
    SHIPPED,
    agent,
    answering,
    artifact,
    final,
    plain,
    tool_calls,
    workflow,
)
from test_agent_log import Recorded  # noqa: E402
from test_agent_replay import recorded, turn  # noqa: E402

import firefly_weave.compiler.agent_tools as compiler_tools  # noqa: E402
import firefly_weave.compiler.ir as ir_module  # noqa: E402
from firefly_weave.compiler.api import CompiledArtifact  # noqa: E402
from firefly_weave.compiler.canonical import canonical_bytes, canonical_digest  # noqa: E402
from firefly_weave.compiler.ir import AgentNode, WorkflowIR  # noqa: E402
from firefly_weave.operations.exports import safe_event  # noqa: E402
from firefly_weave.operations.replay import replay  # noqa: E402
from firefly_weave.runtime import agent_tools  # noqa: E402
from firefly_weave.runtime.agent_tools import RULE_PATTERNS, TOOL_TARGETS, redact  # noqa: E402
from firefly_weave.runtime.kernel import transition  # noqa: E402
from firefly_weave.runtime.models import RunState, RuntimeEvent, TaskIntent  # noqa: E402

PROBE_TASK = "probe.lookup"
PROBE_OUTPUT = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "token": {"type": "string", "x-secret": True}},
}


class ProbeTarget:
    """A read-only tool that runs as a task of a fake task type, with an output schema of its own."""

    target = "probe"
    completion: Literal["task_completed"] = "task_completed"

    def requires_review(self, ir, tool):
        return False

    def callee(self, ir, tool):
        return None

    def side_effect(self, ir, tool):
        return "read_only"

    def input_schema(self, ir, tool):
        return TOOL_TARGETS["action"].input_schema(ir, tool)

    def output_schema(self, ir, tool):
        return PROBE_OUTPUT

    def intent(self, ir, node, tool, key, value, now):
        return TaskIntent(
            node_id=key,
            action_digest=tool.dependency,
            action_reference="probe-lookup@1.0.0",
            input=value,
            connection_slot=None,
            task_type=PROBE_TASK,
            task_version="1.0.0",
            deadline=now + timedelta(seconds=60),
        )

    def result(self, ir, tool, completion, rules):
        if completion.type != "task_completed":
            return {"error": "TOOL_FAILED", "retryable": False}, True
        bundle = {item.reference: item.document for item in ir.dependencies if item.kind == "Schema"}
        return redact(PROBE_OUTPUT, completion.data["output"], bundle, RULE_PATTERNS[rules]), False

    def event_schemas(self, ir, tool, event_type, data):
        return [PROBE_OUTPUT] if event_type in {"task_completed", "incident_resolved"} else []

    def ran(self, ir, tool, evidence):
        return True


@pytest.fixture
def widened(monkeypatch):
    """The probe kind, registered in the runtime's registry and allowed by the IR for this test only."""
    targets = MappingProxyType({**TOOL_TARGETS, "probe": ProbeTarget()})
    for module in (
        "firefly_weave.runtime.agent_tools",
        "firefly_weave.runtime.agents",
        "firefly_weave.runtime.kernel",
        "firefly_weave.operations.agent_log",
    ):
        monkeypatch.setattr(f"{module}.TOOL_TARGETS", targets)
    field = ir_module.AgentToolIR.model_fields["target"]
    original = field.annotation
    field.annotation = Literal["action", "workflow", "human", "probe"]
    models = (
        ir_module.AgentToolIR,
        ir_module.AgentNode,
        ir_module.IRGraph,
        ir_module.WorkflowIR,
        ir_module.ArtifactEnvelope,
    )
    for model in models:
        model.model_rebuild(force=True)
    try:
        yield
    finally:
        field.annotation = original
        for model in models:
            model.model_rebuild(force=True)


@pytest.fixture
def probe(widened, monkeypatch):
    """The probe kind with its pinned fields in the IR's pin check: the probe's pin is an action's."""
    checked = compiler_tools.check_pinned

    def check_pinned(tool, actions, workflows, bind):
        pinned = tool.model_copy(update={"target": "action"}) if tool.target == "probe" else tool
        return checked(pinned, actions, workflows, bind)

    monkeypatch.setattr(compiler_tools, "check_pinned", check_pinned)
    return probe_flow()


def probe_flow(**fields) -> CompiledArtifact:
    """The runtime tests' agent, its lookup tool pinned as a probe: the same artifact with that one change, and any
    other ``fields`` set on the tool."""
    envelope = json.loads(artifact(plain(answering(workflow(agent(tools=[LOOKUP]))))).to_bytes())
    executable = copy.deepcopy(envelope["executable"])
    [node] = [node for node in executable["graph"]["nodes"] if node["kind"] == "agent"]
    node["tools"][0]["target"] = "probe"
    for name, value in fields.items():
        if value is None:
            node["tools"][0].pop(name, None)
        else:
            node["tools"][0][name] = value
    envelope.update(executable=executable, digest=canonical_digest(executable))
    return CompiledArtifact(canonical_bytes(envelope))


SCRIPT = [
    ("started", {"admission_policy": "classified-v1"}),
    turn("support#1", tool_calls(("lookup_order", {"id": "A-17"}))),
    ("task_completed", {"node_id": "support#1.1", "generation": 1, "output": SHIPPED}),
    turn("support#2", final()),
]


def test_a_new_kind_of_tool_runs_end_to_end_with_only_its_registry_entries(probe):
    ir = WorkflowIR.model_validate(probe.executable)
    [node] = [node for node in ir.graph.nodes if isinstance(node, AgentNode)]
    assert [tool.target for tool in node.tools] == ["probe"]

    # The kernel sends the call to the probe's target, as a task of its own type, and the agent answers.
    state = RunState(input={"message": "Where is order A-17?", "region": "eu", "orders": ["A-17"]})
    intents = []
    for sequence, (kind, data) in enumerate(SCRIPT, start=1):
        event = RuntimeEvent(id=uuid4(), type=kind, timestamp=NOW, sequence=sequence, data=data)
        change = transition(state, event, probe)
        intents += [command for command in change.commands if isinstance(command, TaskIntent)]
        state = change.state
    [call] = [intent for intent in intents if intent.node_id == "support#1.1"]
    assert (call.task_type, call.input) == (PROBE_TASK, {"id": "A-17", "region": "eu"})
    assert (state.status, state.output) == ("succeeded", "Order A-17 has shipped.")

    # Replay rebuilds the run, and exports keep the probe's output, screened by the probe's own schema.
    events = recorded(probe, SCRIPT)
    assert replay(probe, events).status == "consistent"
    assert [safe_event(event, probe).data for event in events[1:]] == [event.data for event in events[1:]]
    assert agent_tools.agent_output_schemas(ir, "support#1.1", "task_completed", events[2].data, {}) == [PROBE_OUTPUT]

    # The agent log names the new kind and classifies the output by the probe's schema: its secret field keeps the
    # output out of the log.
    run = Recorded()
    run.flow, run.ir = probe, ir
    run.send("started", NOW, admission_policy="classified-v1")
    run.complete("support#1", NOW + timedelta(seconds=1), _dump(tool_calls(("lookup_order", {"id": "A-17"}))))
    run.complete("support#1.1", NOW + timedelta(seconds=2), {"status": "shipped", "token": "probe-secret-1"})
    run.complete("support#2", NOW + timedelta(seconds=3), _dump(final()))
    [logged] = run.log().turns[0].calls
    assert (logged.target.kind, logged.target.reference, logged.status) == ("probe", "orders-get@1.0.0", "succeeded")
    assert logged.output.available is False
    assert [item.reason for item in logged.output.omissions] == ["classified_secret"]


def test_a_kind_the_pin_check_does_not_know_is_refused(widened):
    # A tool of an unknown kind that carries a person's forms is still not a person tool.
    person = {"questionSchema": {"type": "object"}, "formSchema": {"type": "object"}, "assignment": "support-leads"}
    with pytest.raises(ValueError, match="Unknown AI agent tool kind"):
        probe_flow(human=person, dependency=None, connection=None)


def _dump(output):
    return output.model_dump(mode="json", by_alias=True, exclude_none=True)
