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

"""Offline replay of supplied recorded facts through the production pure kernel."""

import json
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError

from firefly_weave.compiler.api import CompiledArtifact, import_artifact
from firefly_weave.compiler.calls import call_output_schema
from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.compiler.ir import (
    ActionNode,
    CallWorkflowNode,
    HandlesErrors,
    HumanTaskNode,
    IRNode,
    SignalNode,
    WorkflowIR,
)
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.instance_keys import node_of
from firefly_weave.contracts.operations import (
    CallFact,
    DeadlineFact,
    HumanTaskFact,
    RecordedEvidence,
    ReplayDiagnostic,
    ReplayReport,
    TaskFact,
)
from firefly_weave.contracts.step_errors import OUTCOME_ORDER, StepOutcome, confirmable, strongest
from firefly_weave.contracts.transactions import IN_DOUBT_CODES, split_phase
from firefly_weave.operations.exports import (
    bounded_size,
    event_node,
    lock_digest,
    phase_step,
    safe_event,
    transition_digest,
)
from firefly_weave.operations.redaction import project
from firefly_weave.runtime.agent_tools import agent_key, agent_output_schemas
from firefly_weave.runtime.kernel import (
    called_workflow,
    deadline_order,
    duration_steps,
    error_policy,
    transition,
    validate,
    validate_action,
    workflow,
)
from firefly_weave.runtime.models import (
    CallIntent,
    Deadline,
    HumanTaskIntent,
    RunState,
    RuntimeEvent,
    TaskIntent,
    Transition,
)
from firefly_weave.runtime.step_failures import continuation_steps
from firefly_weave.runtime.transactions import HELD_CODE, HELD_NODE, in_transaction, shapes, split_deadline

MAX_EVENTS = 10_000
MAX_BYTES = 64 * 1024 * 1024
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


class EvidenceFailure(ValueError):
    def __init__(self, code: str, *, missing: bool = False) -> None:
        self.code, self.missing = code, missing


def require(condition: bool, code: str, *, missing: bool = False) -> None:
    if not condition:
        raise EvidenceFailure(code, missing=missing)


def replay(artifact: CompiledArtifact, events: tuple[RuntimeEvent, ...]) -> ReplayReport:
    state: RunState | None = None
    verified = 0

    def report(status: str, code: str, sequence: int) -> ReplayReport:
        return ReplayReport.model_validate(
            {
                "status": status,
                "diagnostics": [{"code": code, "sequence": sequence}],
                "final_state": state if verified else None,
                "last_verified_sequence": verified,
            }
        )

    try:
        artifact = import_artifact(artifact.to_bytes())
        ir = workflow(artifact)
    except (ValueError, RecursionError):
        return report("inconsistent", "WV-REPLAY-ARTIFACT", 0)
    size = len(artifact.to_bytes()) + 2
    identity = None
    human_tasks: dict[str, HumanTaskFact] = {}
    seen: set[UUID] = set()
    accepted_receipts: set[UUID] = set()
    issued_wait_ids: set[UUID] = set()
    tasks: dict[str, TaskFact] = {}
    deadlines: dict[str, DeadlineFact] = {}
    attempts: dict[str, tuple[int, str]] = {}
    # Each task's started attempts (generations), and the outcome each failed attempt reported.
    started: dict[str, set[int]] = {}
    reported: dict[str, dict[int, StepOutcome]] = {}

    def outcome_of(key: str) -> StepOutcome:
        """A task's outcome over its started attempts, a lost lease counting as unknown (as recovery decides it)."""
        return strongest(reported.get(key, {}).get(generation, "unknown") for generation in started.get(key, set()))

    eligible_at: dict[str, datetime] = {}
    # The latest call row issued for each call step instance, and every call row seen.
    calls: dict[str, CallFact] = {}
    call_ids: set[UUID] = set()
    nodes = {n.id: n for n in ir.graph.nodes}
    duration_nodes = duration_steps(ir)
    transactions = shapes(ir)
    continuations = continuation_steps(ir)
    expected_lock = lock_digest(artifact)
    for index, supplied in enumerate(events, 1):
        if index > MAX_EVENTS:
            return report("incomplete", "WV-REPLAY-LIMIT", index)
        try:
            size += len(supplied.model_dump_json().encode()) + (1 if index > 1 else 0)
            require(size <= MAX_BYTES, "WV-REPLAY-LIMIT", missing=True)
            event = RuntimeEvent.model_validate_json(supplied.model_dump_json())
            require(event.id not in seen, "WV-REPLAY-DUPLICATE")
            require(event.sequence >= index, "WV-REPLAY-SEQUENCE")
            require(event.sequence == index, "WV-REPLAY-GAP", missing=True)
            require(event.evidence is not None, "WV-REPLAY-PROVENANCE", missing=True)
            evidence = RecordedEvidence.model_validate_json(json.dumps(event.evidence))
            require(
                evidence.artifact_digest == artifact.digest and evidence.lock_digest == expected_lock,
                "WV-REPLAY-IDENTITY",
            )
            require(
                evidence.scope.project_id is not None and evidence.scope.environment_id is not None, "WV-REPLAY-SCOPE"
            )
            current_identity = (evidence.run_id, evidence.scope, evidence.source, evidence.provenance)
            if identity is None:
                identity = current_identity
            require(identity == current_identity, "WV-REPLAY-IDENTITY")
            require(evidence.source is not None, "WV-REPLAY-SOURCE", missing=True)
            assert evidence.source is not None
            require(
                evidence.source.scope == evidence.scope and evidence.source.source_hash == artifact.source_hash,
                "WV-REPLAY-SOURCE",
            )
            require(evidence.expected_transition_digest is not None, "WV-REPLAY-EXPECTATION", missing=True)
            require(
                not any(o.path not in {"/data/reason", "/data/evidence_reference"} for o in evidence.omissions),
                "WV-REPLAY-REDACTED",
                missing=True,
            )
            if event.type not in {"started", "cancelled", "timed_out", "incident_resolved", "paused", "resumed"}:
                # A run holding its transactions records its hold at a key of its own, not at a step.
                held = event.type == "incident_opened" and event.data.get("node_id") == HELD_NODE
                require(held or event_node(nodes, event.data.get("node_id")) is not None, "WV-REPLAY-EVENT")
            projected = safe_event(event, artifact)
            changes = {key for key in event.data if key not in projected.data}
            require(changes <= {"reason", "evidence_reference"}, "WV-REPLAY-REDACTED", missing=True)
            event = projected
            if index == 1:
                require(event.type == "started", "WV-REPLAY-START")
                require(
                    evidence.initial_input is not None
                    and evidence.initial_input.available
                    and not evidence.initial_input.omissions,
                    "WV-REPLAY-INPUT",
                    missing=True,
                )
                assert evidence.initial_input is not None
                projection = project(
                    evidence.initial_input.value,
                    ir.schemas[ir.input_schema],
                    {d.reference: d.document for d in ir.dependencies if d.kind == "Schema"},
                )
                require(projection.available, "WV-REPLAY-REDACTED", missing=True)
                validate(ir, ir.schemas[ir.input_schema], evidence.initial_input.value)
                state = RunState(input=evidence.initial_input.value)
            else:
                require(event.type != "started" and evidence.initial_input is None, "WV-REPLAY-START")
            assert state is not None
            require(state.status not in TERMINAL, "WV-REPLAY-TERMINAL")
            node_id = str(event.data.get("node_id", ""))
            node = event_node(nodes, node_id)
            phase = phase_step(node, node_id)
            # An AI agent's turn runs a task, and each tool call runs what its target completes with.
            agent = agent_key(ir, node_id, state.agents)
            completes = (
                None
                if agent is None or agent.key.review
                else "task_completed"
                if agent.key.call is None
                else agent.target.completion
                if agent.target is not None
                else None
            )
            # A confirm, cancel or undo runs its own action or called workflow at its phase key.
            runs_call = (
                phase.kind == "callWorkflow"
                if phase is not None
                else isinstance(node, CallWorkflowNode) or completes == "call_completed"
            )
            runs_task = (
                phase.kind == "action"
                if phase is not None
                else isinstance(node, ActionNode) or completes == "task_completed"
            )
            receipt_id = UUID(str(event.data["receipt_id"])) if event.type == "incident_resolved" else event.id
            require(receipt_id not in accepted_receipts, "WV-REPLAY-DUPLICATE-RECEIPT")
            # An agent's turn and tool failures go to the agent, as the kernel's error policy says.
            handles = (isinstance(node, HandlesErrors) and node.on_error is not None) or (
                agent is not None and error_policy(ir, node_id) != "stop"
            )
            inside = event.type == "step_failed" and in_transaction(transactions, node_id)
            if event.type == "step_failed":
                # A step's own setting takes its failure, or its transaction does.
                require((handles or inside) and node_id in state.active, "WV-REPLAY-EVENT")
            if event.type == "incident_opened" and node_id == HELD_NODE:
                require(event.data.get("code") == HELD_CODE, "WV-REPLAY-EVENT")
            elif event.type == "step_failed" and "wait_id" in event.data:
                # A wait's deadline the step handles: the same deadline evidence as a timeout.
                _verify_wait(event, evidence, deadlines, nodes, duration_nodes, continuations)
            elif event.type == "step_failed" and inside:
                # A final failure the transaction handles: the kernel checks it; its attempts are done with.
                generation = event.data.get("generation")
                require(type(generation) is int and generation >= 0, "WV-REPLAY-EVENT")
                assert isinstance(generation, int)
                if generation:
                    attempts[node_id] = (generation, "retired")
            elif (
                event.type == "step_failed"
                and isinstance(node, ActionNode)
                and event.data.get("code") == "WV-TASK-DEADLINE"
            ):
                # A task nobody claimed before its deadline: its latest attempt never ran, earlier ones may have.
                ran = started.get(node_id, set())
                require(
                    node_id in tasks
                    and event.timestamp >= tasks[node_id].deadline
                    and event.data.get("generation") == max(ran, default=0)
                    and event.data.get("attempts") == len(ran)
                    and event.data.get("outcome") == outcome_of(node_id),
                    "WV-REPLAY-EVENT",
                )
            elif event.type in {"call_completed", "incident_opened", "step_failed"} and runs_call:
                require(node_id in state.active, "WV-REPLAY-EVENT")
                call_receipt = evidence.call_receipt
                require(call_receipt is not None, "WV-REPLAY-CALL-PROVENANCE", missing=True)
                assert call_receipt is not None
                require(
                    call_receipt.call == calls.get(node_id)
                    and call_receipt.call.generation == event.data.get("generation"),
                    "WV-REPLAY-CALL",
                )
                if event.type == "step_failed":
                    # A call that could not start fails its step with the code its row recorded.
                    require(
                        call_receipt.status == "rejected" and call_receipt.code == event.data.get("code"),
                        "WV-REPLAY-CALL",
                    )
                elif event.type == "call_completed":
                    require(
                        call_receipt.status == event.data.get("status")
                        and call_receipt.child_run_id is not None
                        and str(call_receipt.child_run_id) == event.data.get("child_run_id"),
                        "WV-REPLAY-CALL",
                    )
                else:
                    require(isinstance(event.data.get("code"), str) and bool(event.data["code"]), "WV-REPLAY-EVENT")
            elif event.type in {
                "task_completed",
                "task_failed",
                "incident_opened",
                "recovery_scheduled",
                "step_failed",
            }:
                require(runs_task and node_id in state.active, "WV-REPLAY-EVENT")
                receipt = evidence.task_receipt
                require(receipt is not None, "WV-REPLAY-TASK-PROVENANCE", missing=True)
                assert receipt is not None
                require(
                    receipt.task == tasks.get(node_id)
                    and receipt.receipt_id == event.id
                    and type(event.data.get("generation")) is int
                    and receipt.generation == event.data.get("generation"),
                    "WV-REPLAY-TASK",
                )
                timing = receipt.timing
                require(timing is not None, "WV-REPLAY-TASK-TIMING", missing=True)
                assert timing is not None
                require(
                    eligible_at[node_id] <= timing.checked_at <= event.timestamp
                    and timing.effective_deadline <= receipt.task.deadline
                    and ("@run" not in deadlines or timing.effective_deadline <= deadlines["@run"].deadline),
                    "WV-REPLAY-TASK-TIMING",
                )
                recovery = timing.kind == "recovery_decision"
                require(
                    (recovery and event.type in {"incident_opened", "recovery_scheduled", "step_failed"})
                    or (
                        not recovery
                        and event.type in {"task_completed", "task_failed", "incident_opened"}
                        and timing.checked_at < timing.effective_deadline
                    ),
                    "WV-REPLAY-TASK-TIMING",
                )
                prior_generation, disposition = attempts.get(node_id, (0, "retired"))
                require(
                    (disposition == "retired" and receipt.generation == prior_generation + 1)
                    or (disposition == "task_failed" and recovery and receipt.generation == prior_generation),
                    "WV-REPLAY-GENERATION",
                )
                attempts[node_id] = (receipt.generation, event.type)
                started.setdefault(node_id, set()).add(receipt.generation)
                if event.type == "task_failed":
                    output = event.data.get("output")
                    said = output.get("outcome") if isinstance(output, dict) else None
                    reported.setdefault(node_id, {})[receipt.generation] = (
                        cast(StepOutcome, said) if said in OUTCOME_ORDER else "unknown"
                    )
                if event.type == "step_failed" and not in_transaction(transactions, node_id):
                    # A final failure handed to the step reports the outcome and count of every attempt.
                    require(
                        event.data.get("outcome") == outcome_of(node_id)
                        and event.data.get("attempts") == len(started[node_id]),
                        "WV-REPLAY-EVENT",
                    )
                if event.type == "task_completed" and agent is not None:
                    require("output" in event.data, "WV-REPLAY-EVENT")
                    agent_output(ir, event, evidence, state)
                elif event.type == "task_completed":
                    require("output" in event.data, "WV-REPLAY-EVENT")
                    assert isinstance(node, ActionNode | CallWorkflowNode)
                    digest = phase.dependency if phase is not None else node.dependency
                    definition = load_definition(next(d.document for d in ir.dependencies if d.digest == digest))
                    assert isinstance(definition, ActionDefinition)
                    validate_action(ir, definition, "output", event.data["output"])
                if event.type == "incident_opened":
                    require(isinstance(event.data.get("code"), str) and bool(event.data["code"]), "WV-REPLAY-EVENT")
                if event.type == "recovery_scheduled":
                    scheduled = datetime.fromisoformat(str(event.data.get("next_attempt_at")))
                    require(
                        scheduled.tzinfo is not None and timing.checked_at <= scheduled < timing.effective_deadline,
                        "WV-REPLAY-TASK-TIMING",
                    )
                    attempts[node_id] = (receipt.generation, "retired")
                    eligible_at[node_id] = scheduled
            elif event.type == "incident_resolved":
                key = str(event.data.get("incident_key"))
                prior = state.incidents.get(key)
                kind = event.data.get("kind")
                require(
                    state.status == "suspended"
                    and (prior is not None or key == "@legacy")
                    and kind in {"terminate", "retry_safe", "accept_reconciled_result", "accept_reconciled_failure"},
                    "WV-REPLAY-EVENT",
                )
                # Dispatched on the incident's code first, as the service does: in doubt, accept_reconciled_failure is
                # "It didn't happen" and the kernel decides it; otherwise it confirms a step's unknown outcome.
                doubt = prior is not None and prior.code in IN_DOUBT_CODES
                if kind == "accept_reconciled_failure" and not doubt:
                    # Only a step that handles its failures goes on after its effect was confirmed not to happen.
                    attempted = event.data.get("attempts")
                    require(
                        handles
                        and prior is not None
                        and confirmable(node_id, prior.code)
                        and runs_task
                        and type(attempted) is int
                        and attempted >= 0
                        and "output" in event.data
                        and event.data["output"] is None,
                        "WV-REPLAY-EVENT",
                    )
                if kind != "terminate":
                    require(
                        prior is not None
                        and prior.node_id == node_id
                        and prior.generation == event.data.get("generation"),
                        "WV-REPLAY-EVENT",
                    )
                require(
                    (event.data.get("code") is None and not doubt)
                    or (prior is not None and event.data.get("code") == prior.code),
                    "WV-REPLAY-EVENT",
                )
                if doubt:
                    # The kernel decides a resolution of a transaction in doubt; replay compares its transition.
                    assert prior is not None
                    if kind == "retry_safe" and runs_call:
                        require(node_id in calls, "WV-REPLAY-CALL")
                    elif kind == "retry_safe" and runs_task:
                        generation = event.data.get("generation")
                        require(
                            type(generation) is int and generation == attempts.get(node_id, (0, ""))[0],
                            "WV-REPLAY-GENERATION",
                        )
                        assert isinstance(generation, int) and isinstance(node, ActionNode | CallWorkflowNode)
                        attempts[node_id] = (generation, "retired")
                        eligible_at[node_id] = event.timestamp
                        # The stuck attempt is made ready again with a fresh time limit from the resolution.
                        seconds = retry_seconds(ir, node, node_id)
                        tasks[node_id] = tasks[node_id].model_copy(
                            update={"deadline": event.timestamp + timedelta(seconds=seconds)}
                        )
                elif kind == "retry_safe" and isinstance(node, CallWorkflowNode):
                    require(node_id in calls, "WV-REPLAY-CALL")
                elif kind == "retry_safe":
                    generation = event.data.get("generation")
                    require(
                        type(generation) is int and generation == attempts.get(node_id, (0, ""))[0],
                        "WV-REPLAY-GENERATION",
                    )
                    assert isinstance(generation, int)
                    attempts[node_id] = (generation, "retired")
                    eligible_at[node_id] = event.timestamp
                if doubt:
                    pass
                elif kind == "accept_reconciled_result" and isinstance(node, CallWorkflowNode):
                    require("output" in event.data, "WV-REPLAY-EVENT")
                    output = called_workflow(ir, node)[1]["outputSchema"]
                    schema = call_output_schema(node.mode, node.on_failure, output)  # type: ignore[arg-type]
                    validate(ir, schema, event.data["output"])
                elif kind == "accept_reconciled_result" and agent is not None:
                    require("output" in event.data, "WV-REPLAY-EVENT")
                    agent_output(ir, event, evidence, state)
                elif kind == "accept_reconciled_result":
                    require("output" in event.data and isinstance(node, ActionNode), "WV-REPLAY-EVENT")
                    assert isinstance(node, ActionNode)
                    definition = load_definition(
                        next(d.document for d in ir.dependencies if d.digest == node.dependency)
                    )
                    assert isinstance(definition, ActionDefinition)
                    validate_action(ir, definition, "output", event.data["output"])
            elif event.type == "human_completed":
                human_receipt = evidence.human_receipt
                require(human_receipt is not None, "WV-REPLAY-HUMAN-RECEIPT", missing=True)
                assert human_receipt is not None
                person = agent is not None and (agent.key.review or completes == "human_completed")
                require(
                    (isinstance(node, HumanTaskNode) or person) and node_id in state.active, "WV-REPLAY-HUMAN-EVENT"
                )
                require(
                    human_tasks.get(node_id) == human_receipt.task
                    and str(human_receipt.task.task_id) == event.data.get("task_id")
                    and str(human_receipt.actor_id) == event.data.get("actor_id")
                    and human_receipt.accepted_at == event.timestamp,
                    "WV-REPLAY-HUMAN-RECEIPT",
                )
                require(
                    not any(e.data.get("node_id") == node_id for e in state.deferred_results),
                    "WV-REPLAY-HUMAN-DUPLICATE",
                )
                if node_id in deadlines:
                    require(event.timestamp < deadlines[node_id].deadline, "WV-REPLAY-HUMAN-EXPIRED")
                    deadlines.pop(node_id)
                if person:
                    # A review's decision or a person's answer to an agent fits the form the task offered.
                    require("output" in event.data, "WV-REPLAY-EVENT")
                    agent_output(ir, event, evidence, state)
            elif event.type in {"wait_elapsed", "timed_out", "signal_received"}:
                _verify_wait(event, evidence, deadlines, nodes, duration_nodes, continuations)
                if event.type == "signal_received":
                    require(isinstance(node, SignalNode) and node_id in state.active, "WV-REPLAY-EVENT")
                    assert isinstance(node, SignalNode)
                    validate(ir, ir.schemas[node.schema_ref], event.data["output"])
            # Strip metadata so replay doesn't inject new evidence into deferred kernel operands.
            fact = event.model_copy(update={"evidence": None})
            result = transition(state, fact, artifact)
            require(bounded_size(result, MAX_BYTES - 1024) is not None, "WV-REPLAY-STATE-LIMIT", missing=True)
            turn_inputs(ir, result, evidence)
            require(transition_digest(result) == evidence.expected_transition_digest, "WV-REPLAY-TRANSITION")
            human_commands = [c for c in result.commands if isinstance(c, HumanTaskIntent)]
            require(len(human_commands) == len(evidence.issued_human_tasks), "WV-REPLAY-HUMAN-ISSUANCE", missing=True)
            for human_command, issued_human in zip(human_commands, evidence.issued_human_tasks, strict=True):
                require(
                    human_command.node_id == issued_human.node_id and issued_human.node_id not in human_tasks,
                    "WV-REPLAY-HUMAN-ISSUANCE",
                )
                human_tasks[issued_human.node_id] = issued_human
            expected = [
                (c.node_id, 0, c.mode, c.workflow_reference) for c in result.commands if isinstance(c, CallIntent)
            ]
            if event.type == "incident_resolved" and event.data.get("kind") == "retry_safe" and runs_call:
                prior_call = calls[node_id]
                expected.append((node_id, prior_call.generation + 1, prior_call.mode, prior_call.workflow_reference))
            issued_calls = evidence.issued_calls
            require(len(issued_calls) <= len(expected), "WV-REPLAY-CALL-ISSUANCE")
            require(len(issued_calls) == len(expected), "WV-REPLAY-CALL-ISSUANCE", missing=True)
            for issued_call, (call_node, call_generation, mode, reference) in zip(issued_calls, expected, strict=True):
                require(
                    (issued_call.node_id, issued_call.generation, issued_call.mode, issued_call.workflow_reference)
                    == (call_node, call_generation, mode, reference)
                    and issued_call.call_id not in call_ids,
                    "WV-REPLAY-CALL-ISSUANCE",
                )
                calls[call_node] = issued_call
                call_ids.add(issued_call.call_id)
            task_commands = [c for c in result.commands if isinstance(c, TaskIntent)]
            deadline_commands = [c for c in result.commands if isinstance(c, Deadline)]
            require(
                len(task_commands) >= len(evidence.issued_tasks)
                and len(deadline_commands) >= len(evidence.issued_deadlines),
                "WV-REPLAY-ISSUANCE",
            )
            require(
                len(task_commands) == len(evidence.issued_tasks)
                and len(deadline_commands) == len(evidence.issued_deadlines),
                "WV-REPLAY-ISSUANCE",
                missing=True,
            )
            for command, issued in zip(task_commands, evidence.issued_tasks, strict=True):
                require(
                    (issued.node_id, issued.action_digest, issued.deadline)
                    == (command.node_id, command.action_digest, command.deadline)
                    and issued.node_id not in tasks
                    and issued.task_id not in {t.task_id for t in tasks.values()},
                    "WV-REPLAY-TASK",
                )
                tasks[issued.node_id] = issued
                eligible_at[issued.node_id] = event.timestamp
            for deadline, issued_deadline in zip(deadline_commands, evidence.issued_deadlines, strict=True):
                # A transaction's hold deadline moves on its one row: armed again, it keeps its wait.
                rearmed = (
                    split_deadline(issued_deadline.node_id) is not None
                    and issued_deadline.node_id in deadlines
                    and deadlines[issued_deadline.node_id].wait_id == issued_deadline.wait_id
                )
                require(
                    (issued_deadline.node_id, issued_deadline.deadline, issued_deadline.name)
                    == (deadline.node_id, deadline.deadline, deadline.name)
                    and (
                        rearmed
                        or (issued_deadline.node_id not in deadlines and issued_deadline.wait_id not in issued_wait_ids)
                    ),
                    "WV-REPLAY-DEADLINE",
                )
                deadlines[issued_deadline.node_id] = issued_deadline
                issued_wait_ids.add(issued_deadline.wait_id)
            if event.type in {"wait_elapsed", "signal_received"} or (
                event.type == "step_failed" and "wait_id" in event.data
            ):
                deadlines.pop(node_id)
            seen.add(event.id)
            accepted_receipts.add(receipt_id)
            state, verified = result.state, event.sequence
        except EvidenceFailure as error:
            return report("incomplete" if error.missing else "inconsistent", error.code, index)
        except (ValueError, KeyError, TypeError, RecursionError, ValidationError):
            return report("inconsistent", "WV-REPLAY-EVENT", index)
    if state is not None and state.status in TERMINAL:
        return ReplayReport(status="consistent", final_state=state, last_verified_sequence=verified)
    return ReplayReport(
        status="incomplete",
        final_state=state,
        last_verified_sequence=verified,
        diagnostics=[ReplayDiagnostic(code="WV-REPLAY-OPEN-PREFIX", sequence=verified)],
    )


def agent_output(ir: WorkflowIR, event: RuntimeEvent, evidence: RecordedEvidence, state: RunState) -> None:
    """A recorded output at an agent's key fits what it ran: the turn's pinned guard, or the tool the run's agent
    recorded for the call (a person's resolution carries no task receipt to name it)."""
    schemas = agent_output_schemas(
        ir, event.data.get("node_id"), event.type, event.data, evidence.model_dump(mode="json"), state.agents
    )
    require(bool(schemas), "WV-REPLAY-EVENT")
    for schema in schemas or []:
        validate(ir, schema, event.data["output"])


def turn_inputs(ir: WorkflowIR, result: Transition, evidence: RecordedEvidence) -> None:
    """Each turn a transition sends gives the model exactly the input its recorded turn task was given: a transcript
    rebuilt differently (rules, trimming, the order of results) is reported as such, before the transition's digest."""
    issued = {fact.node_id: fact for fact in evidence.issued_tasks}
    for command in result.commands:
        if not isinstance(command, TaskIntent):
            continue
        reference = agent_key(ir, command.node_id)
        fact = issued.get(command.node_id)
        if reference is None or reference.key.call is not None or fact is None:
            continue
        require(fact.input_digest is not None, "WV-REPLAY-AGENT-INPUT", missing=True)
        require(fact.input_digest == canonical_digest(cast(dict[str, Any], command.input)), "WV-REPLAY-AGENT-INPUT")


def retry_seconds(ir: Any, node: ActionNode | CallWorkflowNode, key: str) -> int:
    """The time limit a retried stuck attempt gets: the transaction's undo limit for a phase, the action's own for
    a step after the point of no return."""
    from firefly_weave.compiler.ir import TransactionNode
    from firefly_weave.runtime.transactions import shapes

    if split_phase(key) is not None:
        facts = shapes(ir)
        owner = facts.owner_key(key.partition("#")[0]) if facts is not None else None
        transaction = next(n for n in ir.graph.nodes if owner is not None and n.id == node_of(owner))
        assert isinstance(transaction, TransactionNode)
        return transaction.policy.undo_timeout_seconds
    definition = load_definition(next(d.document for d in ir.dependencies if d.digest == node.dependency))
    assert isinstance(definition, ActionDefinition)
    return definition.spec.timeout_seconds


def _verify_wait(
    event: RuntimeEvent,
    evidence: RecordedEvidence,
    deadlines: dict[str, DeadlineFact],
    nodes: dict[str, IRNode],
    duration_nodes: set[str],
    continuations: set[str] | None = None,
) -> None:
    # Waits whose step handles the timeout continue the run like duration waits.
    continuations = duration_nodes if continuations is None else continuations
    node_id = str(event.data.get("node_id", ""))
    issued = deadlines.get(node_id)
    require(issued is not None, "WV-REPLAY-DEADLINE")
    assert issued is not None
    receipt = evidence.wait_receipt
    require(receipt is not None and receipt.complete, "WV-REPLAY-WAIT-PROVENANCE", missing=True)
    assert receipt is not None
    require(
        receipt.wait_id == issued.wait_id and event.data.get("deadline") == issued.deadline.isoformat(),
        "WV-REPLAY-DEADLINE",
    )
    pending = receipt.pending_signals
    require(
        len({s.receipt_id for s in pending}) == len(pending)
        and pending == sorted(pending, key=lambda s: (s.accepted_at, s.receipt_id))
        and all(s.accepted_at <= event.timestamp for s in pending),
        "WV-REPLAY-SIGNAL",
    )
    due = sorted(
        (
            d
            for d in deadlines.values()
            if d.deadline <= event.timestamp
            and (d.name is None or not any(s.name == d.name and s.accepted_at < d.deadline for s in pending))
        ),
        key=lambda d: deadline_order(d.node_id, d.deadline, continuations),
    )
    if event.type == "signal_received":
        selected_node = next(
            (
                key
                for key, node in nodes.items()
                if isinstance(node, SignalNode)
                and key in deadlines
                and any(s.name == node.name and s.accepted_at < deadlines[key].deadline for s in pending)
            ),
            None,
        )
        require(selected_node == node_id, "WV-REPLAY-SIGNAL-ORDER")
        require(isinstance(nodes.get(node_id), SignalNode) and "output" in event.data and not due, "WV-REPLAY-SIGNAL")
        matches = [s for s in pending if s.name == issued.name and s.accepted_at < issued.deadline]
        require(
            bool(matches)
            and matches[0].receipt_id == event.id
            and matches[0].accepted_at.isoformat() == event.data.get("accepted_at"),
            "WV-REPLAY-SIGNAL",
        )
    else:
        require(
            event.data.get("wait_id") == str(issued.wait_id) and bool(due) and due[0] == issued, "WV-REPLAY-DEADLINE"
        )
        require((event.type == "wait_elapsed") == (node_of(node_id) in duration_nodes), "WV-REPLAY-DEADLINE")
        handled = node_of(node_id) in continuations - duration_nodes
        require((event.type == "step_failed") == handled, "WV-REPLAY-DEADLINE")
