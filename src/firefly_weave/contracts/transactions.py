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

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, cast, get_args

from firefly_weave.contracts.instance_keys import InvalidInstanceKey, instance_view, node_of, split_instance
from firefly_weave.contracts.values import JsonObject, JsonValue

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
        "WV-TXN-RUN-OVERSIZED": (
            "The run grew too large to undo its transactions, so they're held until someone force cancels the run."
        ),
        "WV-TXN-UNDO-PINNED": "A transaction that is still running may need this activation to undo a step.",
        "WV-TXN-RECONCILE-DENIED": "This run isn't allowed to resolve the transaction.",
        "WV-TXN-RECONCILE-INCOMPLETE": "Say what happened to every step whose end isn't known.",
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
        "WV-TXN-RUN-OVERSIZED",
    }
)


# The statuses of a step whose end a person must state: Weave doesn't know it, or it contradicts the stated ending.
_UNKNOWN_END: Final = frozenset(
    {
        "dispatched",
        "unknown",
        "confirming",
        "confirm_stuck",
        "cancelling",
        "cancel_stuck",
        "undoing",
        "undo_stuck",
    }
)


def must_say(
    ending: Literal["completed", "compensated"], mode: UndoMode, status: ParticipantStatus, doubted: bool
) -> bool:
    """Whether marking a transaction as reconciled with ``ending`` must name a step that stands at ``status``.

    ``doubted`` is true for a step that holds an open in-doubt incident of the transaction. A step that failed for
    good applied nothing, so it is named only when it is why the transaction is in doubt.
    """
    if status in _UNKNOWN_END:
        return True
    if ending == "compensated":
        return status in {"done", "confirmed"}
    return (
        status in {"cancelled", "undone"}
        or (status == "done" and mode == "reserve")
        or (status == "failed" and doubted)
    )


# The most steps a refused reconciliation names; the rest are counted.
UNSAID_STEPS: Final = 20


def unsaid_message(keys: Sequence[str]) -> str:
    """What a reconciliation that leaves steps out answers: the steps a person must still say something about."""
    more = len(keys) - UNSAID_STEPS
    return f"Say what happened to: {', '.join(keys[:UNSAID_STEPS])}{f', and {more} more' if more > 0 else ''}."


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


def undo_context(
    *,
    run_id: str,
    transaction_key: str,
    business_key: str | None,
    participant_key: str,
    outcome: str | None,
    input: JsonValue,
    scope: Mapping[str, JsonValue],
) -> JsonObject:
    """What a confirm, cancel or undo's ``with`` is evaluated over: the run's input, ``steps`` with the step's own
    output, the ``participant`` (its operation key and what is known of its outcome), the ``transaction`` (its ID and
    business key) and the loop roots the step recorded. ``scope`` is what the step recorded when it was dispatched,
    plus its ``output``. The runtime and the participant check both build phase inputs from this."""
    roots = dict(scope)
    steps = dict(cast(JsonObject, roots.pop("steps", {})))
    steps[node_of(participant_key)] = {"output": roots.pop("output", None)}
    return {
        "input": input,
        "steps": steps,
        "participant": {"operationKey": f"{run_id}:{participant_key}", "outcome": outcome or "unknown"},
        "transaction": {"id": f"{run_id}:{transaction_key}", "businessKey": business_key},
        **roots,
    }


def dispatch_effect(calls: bool, action_digests: Iterable[str], dependencies: Sequence[Mapping[str, Any]]) -> Effect:
    """What a run may have applied from what it dispatched: ``none`` only with no call and no task of an action
    that changes data (every task's action is ``read_only``), otherwise ``unknown``. Platform runs and simulated
    runs share this rule."""
    if calls:
        return "unknown"
    digests = list(action_digests)
    if not digests:
        return "none"
    effects = {
        dependency["digest"]: dependency["document"]["spec"].get("sideEffect")
        for dependency in dependencies
        if dependency.get("kind") == "Action"
    }
    return "none" if all(effects.get(digest) == "read_only" for digest in digests) else "unknown"


class Participant(Protocol):
    """What a view of one step of a transaction reads from its record."""

    @property
    def key(self) -> str: ...

    @property
    def mode(self) -> UndoMode: ...

    @property
    def status(self) -> ParticipantStatus: ...

    @property
    def effect(self) -> Effect: ...

    @property
    def attempts(self) -> int: ...


def participants_view(participants: Iterable[Participant]) -> list[JsonObject]:
    """Keys, modes, statuses and attempts of each participant; never a value. Platform runs and simulated runs
    share this view."""
    views: list[JsonObject] = []
    for item in participants:
        identity = instance_view(item.key)
        view: JsonObject = {
            "node_id": identity.node_id,
            "instance_key": item.key,
            "mode": item.mode,
            "status": item.status,
            "effect": item.effect,
        }
        if item.attempts:
            view["attempts"] = item.attempts
        views.append(view)
    return views
