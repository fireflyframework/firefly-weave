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
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from firefly_weave.compiler.api import CompiledArtifact, import_artifact
from firefly_weave.compiler.ir import ActionNode, HumanTaskNode, IRNode, SignalNode, WaitNode
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.operations import (
    DeadlineFact,
    HumanTaskFact,
    RecordedEvidence,
    ReplayDiagnostic,
    ReplayReport,
    TaskFact,
)
from firefly_weave.operations.exports import bounded_size, lock_digest, safe_event, transition_digest
from firefly_weave.operations.redaction import project
from firefly_weave.runtime.kernel import deadline_order, transition, validate, validate_action, workflow
from firefly_weave.runtime.models import Deadline, HumanTaskIntent, RunState, RuntimeEvent, TaskIntent

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
    eligible_at: dict[str, datetime] = {}
    nodes = {n.id: n for n in ir.graph.nodes}
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
                require(event.data.get("node_id") in nodes, "WV-REPLAY-EVENT")
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
            node = nodes.get(node_id)
            receipt_id = UUID(str(event.data["receipt_id"])) if event.type == "incident_resolved" else event.id
            require(receipt_id not in accepted_receipts, "WV-REPLAY-DUPLICATE-RECEIPT")
            if event.type in {"task_completed", "task_failed", "incident_opened", "recovery_scheduled"}:
                require(isinstance(node, ActionNode) and node_id in state.active, "WV-REPLAY-EVENT")
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
                    (recovery and event.type in {"incident_opened", "recovery_scheduled"})
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
                if event.type == "task_completed":
                    require("output" in event.data, "WV-REPLAY-EVENT")
                    assert isinstance(node, ActionNode)
                    definition = load_definition(
                        next(d.document for d in ir.dependencies if d.digest == node.dependency)
                    )
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
                    and kind in {"terminate", "retry_safe", "accept_reconciled_result"},
                    "WV-REPLAY-EVENT",
                )
                if kind != "terminate":
                    require(
                        prior is not None
                        and prior.node_id == node_id
                        and prior.generation == event.data.get("generation"),
                        "WV-REPLAY-EVENT",
                    )
                if kind == "retry_safe":
                    generation = event.data.get("generation")
                    require(
                        type(generation) is int and generation == attempts.get(node_id, (0, ""))[0],
                        "WV-REPLAY-GENERATION",
                    )
                    assert isinstance(generation, int)
                    attempts[node_id] = (generation, "retired")
                    eligible_at[node_id] = event.timestamp
                if kind == "accept_reconciled_result":
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
                require(isinstance(node, HumanTaskNode) and node_id in state.active, "WV-REPLAY-HUMAN-EVENT")
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
            elif event.type in {"wait_elapsed", "timed_out", "signal_received"}:
                _verify_wait(event, evidence, deadlines, nodes)
                if event.type == "signal_received":
                    require(isinstance(node, SignalNode) and node_id in state.active, "WV-REPLAY-EVENT")
                    assert isinstance(node, SignalNode)
                    validate(ir, ir.schemas[node.schema_ref], event.data["output"])
            # Strip metadata so replay doesn't inject new evidence into deferred kernel operands.
            fact = event.model_copy(update={"evidence": None})
            result = transition(state, fact, artifact)
            require(bounded_size(result, MAX_BYTES - 1024) is not None, "WV-REPLAY-STATE-LIMIT", missing=True)
            require(transition_digest(result) == evidence.expected_transition_digest, "WV-REPLAY-TRANSITION")
            human_commands = [c for c in result.commands if isinstance(c, HumanTaskIntent)]
            require(len(human_commands) == len(evidence.issued_human_tasks), "WV-REPLAY-HUMAN-ISSUANCE", missing=True)
            for human_command, issued_human in zip(human_commands, evidence.issued_human_tasks, strict=True):
                require(
                    human_command.node_id == issued_human.node_id and issued_human.node_id not in human_tasks,
                    "WV-REPLAY-HUMAN-ISSUANCE",
                )
                human_tasks[issued_human.node_id] = issued_human
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
                require(
                    (issued_deadline.node_id, issued_deadline.deadline, issued_deadline.name)
                    == (deadline.node_id, deadline.deadline, deadline.name)
                    and issued_deadline.node_id not in deadlines
                    and issued_deadline.wait_id not in issued_wait_ids,
                    "WV-REPLAY-DEADLINE",
                )
                deadlines[issued_deadline.node_id] = issued_deadline
                issued_wait_ids.add(issued_deadline.wait_id)
            if event.type in {"wait_elapsed", "signal_received"}:
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


def _verify_wait(
    event: RuntimeEvent, evidence: RecordedEvidence, deadlines: dict[str, DeadlineFact], nodes: dict[str, IRNode]
) -> None:
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
    duration_nodes = {key for key, node in nodes.items() if isinstance(node, WaitNode)}
    due = sorted(
        (
            d
            for d in deadlines.values()
            if d.deadline <= event.timestamp
            and (d.name is None or not any(s.name == d.name and s.accepted_at < d.deadline for s in pending))
        ),
        key=lambda d: deadline_order(d.node_id, d.deadline, duration_nodes),
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
        require((event.type == "wait_elapsed") == (node_id in duration_nodes), "WV-REPLAY-DEADLINE")
