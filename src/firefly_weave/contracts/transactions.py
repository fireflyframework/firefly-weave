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


"""Workflow transactions: the shared vocabulary of the compiler, the runtime, Operations and Studio.

A ``transaction`` step groups steps that change external systems. Each step inside it that changes data declares
how it is undone: it reserves and is later confirmed or cancelled, an action or a called workflow reverses it, or it
is irreversible. This module names the states, modes, phases and codes those parts share; it holds no behavior.

Every phase of a step runs at its own instance key, so it reaches the external system with its own stable operation
key ``"{run_id}:{phase key}"``: ``hold#confirm``, ``hold#cancel`` and ``add#undo`` for the steps ``hold`` and
``add`` (``hold[3]#cancel`` inside a loop). Letter-first ``#`` segments are free in the instance-key grammar, whose
digit-first segments belong to AI agent turns.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, get_args

from firefly_weave.contracts.instance_keys import InvalidInstanceKey, split_instance

type TransactionState = Literal[
    "open",
    "deciding",
    "committing",
    "completed",
    "compensating",
    "compensated",
    "cancelled",
    "in_doubt",
    "reconciled",
    "force_ended",
]
type UndoMode = Literal["reserve", "compensate", "irreversible"]
# The undo phases of a step; ``compensate`` runs at the ``#undo`` key.
type UndoPhase = Literal["confirm", "cancel", "compensate"]
# What a worker or connector is told it is running.
type PhaseOperation = Literal["try", "forward", "confirm", "cancel", "compensate"]
# Whether a failed step may have applied anything: ``none`` only when a transaction-aware release says so.
type Effect = Literal["none", "unknown"]
type ParticipantStatus = Literal[
    "pending",
    "dispatched",
    "done",
    "failed",
    "unknown",
    "skipped",
    "cancelling",
    "cancelled",
    "cancel_stuck",
    "undoing",
    "undone",
    "undo_stuck",
    "confirming",
    "confirmed",
    "confirm_stuck",
]
type PlatformEventType = Literal[
    "transaction.completed",
    "transaction.compensated",
    "transaction.cancelled",
    "transaction.in_doubt",
    "transaction.reconciled",
    "transaction.force_ended",
]

TRANSACTION_STATES: Final[tuple[TransactionState, ...]] = get_args(TransactionState.__value__)
TERMINAL_TRANSACTION_STATES: Final = frozenset({"completed", "compensated", "cancelled", "reconciled", "force_ended"})
# States in which a run's transaction still holds work: purge and archive refuse the run.
UNSETTLED_TRANSACTION_STATES: Final = frozenset({"open", "deciding", "committing", "compensating", "in_doubt"})
# A run's transaction state is its worst transaction's, in this order (worst first).
TRANSACTION_STATE_ORDER: Final[tuple[TransactionState, ...]] = (
    "in_doubt",
    "compensating",
    "deciding",
    "force_ended",
    "reconciled",
    "compensated",
    "cancelled",
    "committing",
    "open",
    "completed",
)
UNDO_MODES: Final[tuple[UndoMode, ...]] = get_args(UndoMode.__value__)
PARTICIPANT_STATUSES: Final[tuple[ParticipantStatus, ...]] = get_args(ParticipantStatus.__value__)
TRANSACTION_EVENT_TYPES: Final[tuple[PlatformEventType, ...]] = get_args(PlatformEventType.__value__)
# The key segment each undo phase runs at.
PHASE_SEGMENTS: Final[Mapping[UndoPhase, str]] = MappingProxyType(
    {"confirm": "confirm", "cancel": "cancel", "compensate": "undo"}
)
_PHASES: Final[Mapping[str, UndoPhase]] = MappingProxyType({value: key for key, value in PHASE_SEGMENTS.items()})

# Policy defaults and bounds, in seconds unless named otherwise.
DEFAULT_HOLD_SECONDS: Final = 900
MIN_HOLD_SECONDS: Final = 30
MAX_HOLD_SECONDS: Final = 86_400
DEFAULT_UNDO_ATTEMPTS: Final = 10
DEFAULT_UNDO_INITIAL_DELAY_SECONDS: Final = 2
DEFAULT_UNDO_MAX_DELAY_SECONDS: Final = 300
DEFAULT_UNDO_TIMEOUT_SECONDS: Final = 86_400
MIN_UNDO_TIMEOUT_SECONDS: Final = 60
MAX_UNDO_TIMEOUT_SECONDS: Final = 604_800
# Reservations must outlive the step that can't be undone by this margin; the hold and settle bounds use it too.
HOLD_MARGIN_SECONDS: Final = 30
# The most participants one transaction can register, loops multiplied out.
MAX_TRANSACTION_PARTICIPANTS: Final = 1000
# The most codes an action can declare as definite failures.
MAX_DEFINITE_FAILURES: Final = 32
# Shared worker codes that never prove nothing was applied.
TRANSIENT_WORKER_CODES: Final = frozenset({"TRANSIENT", "UNAVAILABLE", "RATE_LIMITED", "TIMEOUT"})

# Runtime codes and their product copy. Append-only: transitions and incidents embed these messages.
TRANSACTION_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "WV-TXN-UNDO-STUCK": "A step's undo couldn't finish, so the transaction is in doubt.",
        "WV-TXN-CANCEL-STUCK": "A reservation couldn't be released, so the transaction is in doubt.",
        "WV-TXN-CONFIRM-STUCK": "A reservation couldn't be confirmed after the transaction went ahead.",
        "WV-TXN-AFTER-PIVOT-STUCK": "A step after the point of no return couldn't finish.",
        "WV-TXN-PIVOT-UNKNOWN": "The step that can't be undone may or may not have happened.",
        "WV-TXN-SETTLE-TIMEOUT": "Steps still running didn't finish in time, so nothing can be undone safely yet.",
        "WV-TXN-HOLD-INSUFFICIENT": (
            "Reservations wouldn't outlast the step that can't be undone, so the transaction was undone first."
        ),
        "WV-TXN-HOLD-EXPIRED": "Reservations were about to expire before the transaction could finish.",
        "WV-TXN-ROLLED-BACK": "The transaction was undone.",
        "WV-TXN-RUN-CANCELLED": "The run was canceled, so the transaction was undone.",
        "WV-TXN-RUN-TIMED-OUT": "The run timed out, so the transaction was undone.",
        "WV-TXN-IN-DOUBT": "Resolve the transaction first.",
        "WV-TXN-UNDO-PINNED": "A transaction that is still running may need this activation to undo a step.",
        "WV-TXN-RECONCILE-DENIED": "This run isn't allowed to resolve the transaction.",
        "WV-COMPLETION-EFFECT": "A failure whose outcome is unknown can't also say that nothing was applied.",
        "WV-TRIGGER-EVENT-DEPTH": "Platform events from a run this deep in a chain of reactions start no workflows.",
    }
)
# In-doubt incidents: high priority, and never closed by anything but a resolution.
IN_DOUBT_CODES: Final = frozenset(
    {
        "WV-TXN-UNDO-STUCK",
        "WV-TXN-CANCEL-STUCK",
        "WV-TXN-CONFIRM-STUCK",
        "WV-TXN-AFTER-PIVOT-STUCK",
        "WV-TXN-PIVOT-UNKNOWN",
        "WV-TXN-SETTLE-TIMEOUT",
    }
)


def phase_key(instance_key: str, phase: UndoPhase) -> str:
    """The instance key ``phase`` of the step instance ``instance_key`` runs at: ``hold[3]`` → ``hold[3]#cancel``."""
    parsed = split_instance(instance_key)
    if parsed.segments or parsed.count is not None:
        raise InvalidInstanceKey("Undo phases belong to a step instance, not to a repeated activation or a yield")
    if phase not in PHASE_SEGMENTS:
        raise InvalidInstanceKey("Unknown undo phase")
    return f"{instance_key}#{PHASE_SEGMENTS[phase]}"


def split_phase(key: str) -> tuple[str, UndoPhase] | None:
    """The step instance and the undo phase of a phase key, or ``None`` for any other canonical key."""
    parsed = split_instance(key)
    if len(parsed.segments) != 1 or parsed.count is not None or parsed.segments[0] not in _PHASES:
        return None
    return key.rpartition("#")[0], _PHASES[parsed.segments[0]]
