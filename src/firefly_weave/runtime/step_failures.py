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

"""Pure rules for final step failures: whether a step's onError setting may take one, and what it reports.

Recovery, the deadline scan and call dispatch decide here, so every producer of ``step_failed`` applies the same
barrier: an effect that may have happened on a step that changes data and is never retried keeps its incident.
"""

from collections.abc import Iterable, Mapping
from typing import Literal

from firefly_weave.compiler.ir import HandlesErrors, HumanTaskNode, IRNode, SignalNode, WorkflowIR
from firefly_weave.contracts.instance_keys import node_of
from firefly_weave.contracts.step_errors import StepOutcome, strongest
from firefly_weave.contracts.values import JsonObject
from firefly_weave.runtime.kernel import duration_steps
from firefly_weave.runtime.models import ControlCommand, Transition

type Verdict = Literal["handled", "ambiguous", "exhausted"]
# The code a lease lost on a task's last attempt reports.
LEASE_EXPIRED = "WV-TASK-LEASE-EXPIRED"


def final_failure(policy: str, side_effect: str, outcome: StepOutcome) -> Verdict:
    """What a task's final failure becomes: handled by the step, or the incident a step that stops would open.

    A step that stops the run keeps today's codes: every failure of an action that changes data and is never retried
    is ambiguous. A step that handles its failures takes every failure except an unknown outcome of such an action.
    """
    unsafe = side_effect == "non_idempotent"
    if policy == "stop":
        return "ambiguous" if unsafe else "exhausted"
    return "ambiguous" if unsafe and outcome == "unknown" else "handled"


def attempt_outcomes(generations: Iterable[int], reported: Mapping[int, StepOutcome]) -> StepOutcome:
    """A task's outcome over its attempts: what each failed attempt reported, and ``unknown`` for a lost lease."""
    return strongest(reported.get(generation, "unknown") for generation in generations)


# What an earlier child of a call says about the call's effect: a stopped child may have applied anything.
CHILD_OUTCOMES: dict[str, StepOutcome] = {"rejected": "not_started", "failed": "failed"}


def call_outcome(statuses: Iterable[str]) -> StepOutcome:
    """A call's outcome over its earlier generations' children: ``failed`` for a child that failed, ``unknown`` for
    one cancelled, timed out or still unaccounted for, ``not_started`` when none started."""
    return strongest(CHILD_OUTCOMES.get(status, "unknown") for status in statuses)


def continuation_steps(ir: WorkflowIR) -> set[str]:
    """Steps whose due deadline continues the run: duration waits, loop yields, and waits that handle a timeout."""
    return duration_steps(ir) | {
        node.id
        for node in ir.graph.nodes
        if isinstance(node, (SignalNode, HumanTaskNode)) and node.on_error is not None
    }


def wait_failure(node: IRNode) -> str:
    """The code a wait's deadline reports when its step handles the timeout."""
    return "WV-HUMAN-EXPIRED" if isinstance(node, HumanTaskNode) else "WV-SIGNAL-TIMEOUT"


def failure_data(
    key: str,
    generation: int,
    code: str,
    outcome: StepOutcome,
    attempts: int,
    **extra: str,
) -> JsonObject:
    """The data of a ``step_failed`` event; ``extra`` carries ``child_run_id``, ``deadline`` and ``wait_id``."""
    data: JsonObject = {
        "node_id": key,
        "generation": generation,
        "code": code,
        "outcome": outcome,
        "attempts": attempts,
    }
    data.update(extra)
    return data


def first_failures(result: Transition) -> list[tuple[str, str, str]]:
    """The failures a transition handled for the first time, as (instance key, handling, code).

    A step whose error path finished is reported again with the path's output, directly before its error join's own
    change; that final change is not a new failure.
    """
    found: list[tuple[str, str, str]] = []
    for index, change in enumerate(result.steps):
        if change.status != "failed" or change.error is None:
            continue
        following = result.steps[index + 1] if index + 1 < len(result.steps) else None
        if following is not None and node_of(following.node_id).startswith("@join:"):
            continue
        handling = (
            "errorOutput"
            if any(
                isinstance(command, ControlCommand)
                and command.kind == "spawn_branch"
                and command.node_id == change.node_id
                and command.branch == "errorPath"
                for command in result.commands
            )
            else "continue"
        )
        found.append((change.node_id, handling, str(change.error["code"])))
    return found


def handles(node: IRNode) -> bool:
    """Whether a step's onError setting takes its final failures."""
    return isinstance(node, HandlesErrors) and node.on_error is not None
