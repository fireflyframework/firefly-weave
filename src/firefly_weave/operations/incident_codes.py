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

"""What each incident code means, and the resolutions that can apply to it, in plain words for Weave AI.

The entries follow the incident guide (``docs/reference/incident-operations.md``), and a test keeps the two in step
both ways. ``decisions`` lists ``RecordedResolution.kind`` values only: the ones that can apply to the code, never a
recommendation for one incident, since the safe choice depends on the other system's state.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

type ResolutionKind = Literal["retry_safe", "accept_reconciled_result", "accept_reconciled_failure", "terminate"]


@dataclass(frozen=True, slots=True)
class IncidentCode:
    meaning: str
    decisions: tuple[ResolutionKind, ...]
    note: str | None = None


_AGENT_STOPPED = "Change the workflow or its limits, then start a linked retry of the run."
_STUCK = IncidentCode(
    "A transaction step couldn't finish, so the transaction is in doubt and waits for a person.",
    ("retry_safe", "accept_reconciled_result", "terminate"),
    "Check the other system first. Mark as reconciled and force end need the transaction reconcile capability.",
)

INCIDENT_CODES: Final = MappingProxyType(
    {
        "WV-TASK-AMBIGUOUS": IncidentCode(
            "An attempt of an action that is not safe to repeat ended without a usable result, so whether it applied "
            "is unknown.",
            ("accept_reconciled_result", "accept_reconciled_failure", "terminate"),
            "Check the other system before deciding. Retrying is refused because the action may already have applied.",
        ),
        "WV-TASK-RETRIES-EXHAUSTED": IncidentCode(
            "A safe-to-repeat action used every automatic attempt, or no time was left for another.",
            ("retry_safe", "accept_reconciled_result", "terminate"),
            "Fix the cause first; a retry must start before the action's deadline.",
        ),
        "WV-TASK-DEADLINE": IncidentCode(
            "Nobody claimed the task before its deadline, so its time budget is spent.",
            ("terminate",),
            "Check that an executor or worker is running, then start a linked retry of the run.",
        ),
        "WV-TASK-FAILED": IncidentCode(
            "The executor or worker reported a failure for this step.",
            ("retry_safe", "accept_reconciled_result", "accept_reconciled_failure", "terminate"),
            "Retry only when the step's side effect is safe to repeat; otherwise reconcile or terminate.",
        ),
        "WV-AGENT-BUDGET": IncidentCode(
            "An AI agent turn crossed the step's token budget or the run's token limit, and the tool calls it asked "
            "for didn't run.",
            ("retry_safe", "terminate"),
            "A retry grants one final turn in which the model must answer without tools.",
        ),
        "WV-AGENT-ITERATIONS": IncidentCode(
            "The AI agent used all its turns without a final answer.", ("terminate",), _AGENT_STOPPED
        ),
        "WV-AGENT-TIMEOUT": IncidentCode(
            "The AI agent passed its time limit without a final answer.", ("terminate",), _AGENT_STOPPED
        ),
        "WV-AGENT-TOOL-FAILED": IncidentCode(
            "A tool of the AI agent failed, and the step stops on tool failures.", ("terminate",), _AGENT_STOPPED
        ),
        "WV-RUNTIME-LOOP-ITEMS": IncidentCode(
            "A loop's list was not a list.", ("terminate",), "Fix the workflow and start a new run."
        ),
        "WV-RUNTIME-LOOP-LIMIT": IncidentCode(
            "A loop's list was longer than its item limit.", ("terminate",), "Fix the workflow and start a new run."
        ),
        "WV-RUNTIME-LOOP-OUTPUT": IncidentCode(
            "A loop's results exceeded the payload limit.", ("terminate",), "Fix the workflow and start a new run."
        ),
        "WV-CALL-START": IncidentCode(
            "A call step could not start its child run.",
            ("retry_safe", "accept_reconciled_result", "terminate"),
            "A retry starts a new child run.",
        ),
        "WV-CALL-DEPTH": IncidentCode(
            "A call step could not start its child run because the chain of calls is too deep.",
            ("retry_safe", "accept_reconciled_result", "terminate"),
            "A retry starts a new child run.",
        ),
        "WV-CALL-INTERRUPTED": IncidentCode(
            "A call step's child run was cancelled or timed out, and the call stops on failure.",
            ("retry_safe", "accept_reconciled_result", "terminate"),
            "A retry starts a new child run.",
        ),
        "WV-TXN-UNDO-STUCK": _STUCK,
        "WV-TXN-CANCEL-STUCK": IncidentCode(
            "A reservation couldn't be released, so the transaction is in doubt.", _STUCK.decisions, _STUCK.note
        ),
        "WV-TXN-CONFIRM-STUCK": IncidentCode(
            "A reservation couldn't be confirmed after the transaction went ahead.", _STUCK.decisions, _STUCK.note
        ),
        "WV-TXN-AFTER-PIVOT-STUCK": IncidentCode(
            "A step after the transaction's point of no return couldn't finish.", _STUCK.decisions, _STUCK.note
        ),
        "WV-TXN-PIVOT-UNKNOWN": IncidentCode(
            "The transaction step that can't be undone may or may not have happened.",
            ("accept_reconciled_result", "accept_reconciled_failure", "terminate"),
            "Say whether it happened after checking the other system, or force end the run. Retrying is refused.",
        ),
        "WV-TXN-SETTLE-TIMEOUT": IncidentCode(
            "Transaction steps still running didn't finish in time, so nothing can be undone safely yet.",
            _STUCK.decisions,
            _STUCK.note,
        ),
        "WV-TXN-RUN-OVERSIZED": IncidentCode(
            "The run grew too large to undo its transactions, so they are held until someone force ends the run.",
            ("terminate",),
            "Force end ends every open transaction of the run; work already sent elsewhere may continue.",
        ),
    }
)
