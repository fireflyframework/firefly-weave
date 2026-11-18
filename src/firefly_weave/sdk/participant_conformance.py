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


"""Check that a step inside a transaction handles every request Weave can send it.

``participant_step`` finds one step of a workflow document and the actions that run it, confirm it, cancel it or
undo it. ``check_step`` then sends that step's own requests, built as Weave builds them (the same operation keys,
transaction context and inputs), in every order Weave can deliver them: a step then its confirm or its cancel, a
cancel or undo without the step, the step after its cancel or undo, and each request twice. Each check starts a
fresh run ID, so its keys never meet a real run's. ``judge`` decides each check from the answers alone: the check
never reads the participant's system.

A ``Participant`` delivers the requests: ``HandlerParticipant`` calls SDK worker handlers with a lease, as a worker
would. The report holds check names, phases, operation keys, answers and failure codes, never a value.
"""

import asyncio
import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol, cast
from uuid import UUID, uuid4

from firefly_weave.compiler.calls import BUSINESS_KEY_SCHEMA
from firefly_weave.compiler.canonical import canonical_bytes
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.expressions import ExpressionFailure, evaluate
from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.definitions import (
    ActionDefinition,
    ActionStep,
    ActionUndo,
    Branch,
    ForEachStep,
    ParallelStep,
    SwitchStep,
    TransactionStep,
    WorkerImplementation,
    WorkflowDefinition,
    load_definition,
)
from firefly_weave.contracts.participants import (
    COMPENSATE_CHECKS,
    CONFORMANCE_MESSAGES,
    FAILURE_CODE_PATTERN,
    RESERVE_CHECKS,
    CheckReason,
    CheckResult,
    ConformanceCheck,
    ConformanceReport,
    Delivery,
)
from firefly_weave.contracts.transactions import Effect, PhaseOperation, UndoPhase, phase_key, undo_context
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.contracts.workers import LeaseProof, LeaseTransaction, TaskLease
from firefly_weave.sdk.worker import TaskHandler

type Mode = Literal["reserve", "compensate"]
type Outcome = Literal["not_started", "failed", "unknown"]
# What each check sends, in order: the step's own request, its confirm, its cancel or undo, or the step's own
# request sent after its cancel or undo, which must fail.
type Move = Literal["step", "confirm", "undo", "late"]

_SHAPES: Final[Mapping[ConformanceCheck, tuple[Move, ...]]] = {
    "try-then-confirm": ("step", "confirm"),
    "try-then-cancel": ("step", "undo"),
    "cancel-without-try": ("undo",),
    "try-after-cancel": ("undo", "late"),
    "try-twice": ("step", "step"),
    "confirm-twice": ("step", "confirm", "confirm"),
    "cancel-twice": ("step", "undo", "undo"),
    "forward-then-compensate": ("step", "undo"),
    "compensate-without-forward": ("undo",),
    "forward-after-compensate": ("undo", "late"),
    "forward-twice": ("step", "step"),
    "compensate-twice": ("step", "undo", "undo"),
}
# The two deliveries whose answers must be equal.
_REPEATS: Final[Mapping[ConformanceCheck, tuple[int, int]]] = {
    "try-twice": (0, 1),
    "confirm-twice": (1, 2),
    "cancel-twice": (1, 2),
    "forward-twice": (0, 1),
    "compensate-twice": (1, 2),
}
# Codes the check records for a request it couldn't send or that never answered.
TIMEOUT_CODE: Final = "TIMEOUT"
INPUT_CODE: Final = "INVALID_INPUT"
_FAILURE_CODE: Final = re.compile(FAILURE_CODE_PATTERN)
# Any run ID: the requests built before the checks start, to find setup errors, are never sent.
_SAMPLE_RUN: Final = "00000000-0000-0000-0000-000000000000"


class ConformanceError(ValueError):
    """Why the check couldn't start; ``code`` is a key of ``CONFORMANCE_MESSAGES`` and the text is its message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(CONFORMANCE_MESSAGES[code])


@dataclass(frozen=True)
class PhaseAction:
    """The action that runs one phase of the step, how Weave builds its input, and its connection slot."""

    phase: PhaseOperation
    reference: str
    action: ActionDefinition
    expression: JsonObject
    connection: str | None


@dataclass(frozen=True)
class ParticipantStep:
    workflow: str
    step: str
    # The step ID of the transaction that holds the step.
    transaction: str
    business_key: JsonObject | None
    mode: Mode
    forward: PhaseAction
    confirm: PhaseAction | None
    cancel: PhaseAction | None
    compensate: PhaseAction | None
    input_schema: JsonObject
    schemas: Mapping[str, JsonObject]

    @property
    def actions(self) -> tuple[PhaseAction, ...]:
        return tuple(item for item in (self.forward, self.confirm, self.cancel, self.compensate) if item is not None)


@dataclass(frozen=True)
class Request:
    """One request of the step, as Weave would send it. ``input`` is None when it couldn't be built."""

    phase: PhaseOperation
    reference: str
    action: ActionDefinition
    connection: str | None
    input: JsonValue
    operation_key: str
    transaction: LeaseTransaction
    deadline: datetime


@dataclass(frozen=True)
class Completed:
    output: JsonValue
    # Whether the output matches the action's output schema.
    valid: bool = True


@dataclass(frozen=True)
class Failed:
    code: str
    outcome: Outcome = "unknown"
    effect: Effect = "unknown"


@dataclass(frozen=True)
class Unanswered:
    """A request the check couldn't send (its input couldn't be built or doesn't fit), or that never answered."""

    reason: Literal["input", "timeout"]


type Answer = Completed | Failed | Unanswered


class Participant(Protocol):
    def prepare(self, actions: Sequence[PhaseAction]) -> None:
        """Raise ``ConformanceError`` when it can't run one of these actions; called before any request."""
        ...

    async def deliver(self, request: Request) -> Completed | Failed: ...


class HandlerParticipant:
    """Delivers requests to SDK worker handlers keyed by ``taskType@taskVersion``, as ``Worker(handlers=...)``
    takes them, with the lease a worker would claim, and maps their failures as the worker runner does."""

    def __init__(self, handlers: Mapping[str, TaskHandler]) -> None:
        self.handlers = dict(handlers)

    def prepare(self, actions: Sequence[PhaseAction]) -> None:
        for item in actions:
            implementation = item.action.spec.implementation
            if not isinstance(implementation, WorkerImplementation) or capability(implementation) not in self.handlers:
                raise ConformanceError("WV-CONFORMANCE-HANDLER")

    async def deliver(self, request: Request) -> Completed | Failed:
        implementation = request.action.spec.implementation
        assert isinstance(implementation, WorkerImplementation)
        lease = TaskLease(
            proof=LeaseProof(task_id=uuid4(), generation=1, owner=uuid4(), token="weave-conformance-check"),
            input=request.input,
            operation_key=request.operation_key,
            deadline=request.deadline,
            expires_at=request.deadline,
            capability=capability(implementation),
            worker_release_id=uuid4(),
            transaction=request.transaction,
        )
        try:
            return Completed(await self.handlers[lease.capability](lease))
        except ConnectorFailure as error:
            return failure(error)
        except Exception:
            return Failed("HANDLER_FAILED")


def capability(implementation: WorkerImplementation) -> str:
    return f"{implementation.task_type}@{implementation.task_version}"


def failure(error: ConnectorFailure) -> Failed:
    """A participant's failure as Weave records it; a code that isn't a failure code counts as the handler's."""
    if not isinstance(error.code, str) or _FAILURE_CODE.fullmatch(error.code) is None:
        return Failed("HANDLER_FAILED")
    return Failed(error.code, error.outcome, error.effect)


def participant_step(document: JsonObject, step_id: str, catalog: CatalogSnapshot) -> ParticipantStep:
    """The step ``step_id`` of a workflow document, with the actions that run, confirm, cancel or undo it.

    Raises ``ValueError`` for a document that isn't a valid workflow, and ``ConformanceError`` when the step isn't
    inside a transaction, isn't an action, or isn't confirmed and cancelled, or undone, by actions.
    """
    definition = load_definition(document)
    if not isinstance(definition, WorkflowDefinition):
        raise ValueError("Not a workflow document")
    found = _find(definition, step_id)
    if found is None:
        raise ConformanceError("WV-CONFORMANCE-STEP")
    step, transaction = found
    reserve = isinstance(step.confirm, ActionUndo) and isinstance(step.cancel, ActionUndo)
    if not reserve and not isinstance(step.compensate, ActionUndo):
        raise ConformanceError("WV-CONFORMANCE-STEP")
    mode: Mode = "reserve" if reserve else "compensate"

    def phase(name: PhaseOperation, uses: str, expression: JsonObject, connection: str | None) -> PhaseAction:
        resource = catalog.resolve("Action", uses)
        if resource is None:
            raise ConformanceError("WV-CONFORMANCE-ACTION")
        action = load_definition(resource.definition.value)
        assert isinstance(action, ActionDefinition)
        return PhaseAction(name, uses, action, expression, connection)

    def undo(name: PhaseOperation, operation: object) -> PhaseAction | None:
        if not isinstance(operation, ActionUndo):
            return None
        expression = operation.input.model_dump(by_alias=True)
        return phase(name, operation.uses, expression, operation.connection or step.connection)

    forward = phase("try" if reserve else "forward", step.uses, step.input.model_dump(by_alias=True), step.connection)
    business_key = transaction.business_key
    return ParticipantStep(
        workflow=f"{definition.metadata.name}@{definition.metadata.version}",
        step=step.id,
        transaction=transaction.id,
        business_key=None if business_key is None else business_key.model_dump(by_alias=True),
        mode=mode,
        forward=forward,
        confirm=undo("confirm", step.confirm) if reserve else None,
        cancel=undo("cancel", step.cancel) if reserve else None,
        compensate=None if reserve else undo("compensate", step.compensate),
        input_schema=definition.spec.input_schema,
        schemas={name: document.value for name, document in catalog.schemas.items()},
    )


def _find(definition: WorkflowDefinition, step_id: str) -> tuple[ActionStep, TransactionStep] | None:
    """The action step ``step_id`` and its nearest enclosing transaction; walks the document without recursion."""
    pending: list[tuple[Sequence[object], TransactionStep | None]] = [(definition.spec.steps, None)]
    while pending:
        steps, transaction = pending.pop()
        for item in steps:
            if getattr(item, "id", None) == step_id:
                if isinstance(item, ActionStep) and transaction is not None:
                    return item, transaction
                return None
            branches: list[tuple[Branch, TransactionStep | None]] = []
            if isinstance(item, TransactionStep):
                pending.append((item.steps, item))
            elif isinstance(item, ForEachStep):
                branches.append((item.body, transaction))
            elif isinstance(item, ParallelStep):
                branches.extend((branch, transaction) for branch in item.branches.values())
            elif isinstance(item, SwitchStep):
                branches.extend((case, transaction) for case in item.cases)
                branches.append((item.default, transaction))
            error_path = getattr(item, "error_path", None)
            if isinstance(error_path, Branch):
                # A transaction's error path runs after the transaction, outside it.
                branches.append((error_path, transaction))
            pending.extend((branch.steps, owner) for branch, owner in branches)
    return None


def judge(check: ConformanceCheck, deliveries: Sequence[tuple[Request, Answer]]) -> CheckResult:
    """Decide a check from the requests it sent and their answers, in order."""
    shape = _SHAPES[check]
    if not 1 <= len(deliveries) <= len(shape):
        raise ValueError("A check has one answer per request it sent")
    reason = _first_failure(check, deliveries)
    if reason is None and len(deliveries) < len(shape):
        raise ValueError("A check stops early only on a failed request")
    if reason is None and check in _REPEATS:
        first, again = (deliveries[index][1] for index in _REPEATS[check])
        if not (isinstance(first, Completed) and isinstance(again, Completed) and _same(first.output, again.output)):
            reason = "repeat"
    return CheckResult(
        check=check,
        passed=reason is None,
        reason=reason,
        deliveries=[_delivery(request, answer) for request, answer in deliveries],
    )


async def check_step(
    step: ParticipantStep,
    participant: Participant,
    input: JsonValue,
    *,
    steps: Mapping[str, JsonValue] | None = None,
    vary: Sequence[str] = (),
    new_id: Callable[[], UUID] = uuid4,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ConformanceReport:
    """Run every check of the step's mode against ``participant`` and report each one.

    ``input`` is the run's input and ``steps`` the outputs of earlier steps the step reads, by step ID. Each
    pointer of ``vary`` names a string in the input that gets ``-`` and 12 hexadecimal characters appended in every
    check, so the checks don't share business references. Raises ``ConformanceError`` before any request when the
    check can't run.
    """
    participant.prepare(step.actions)
    bindings: JsonObject = {name: {"output": value} for name, value in (steps or {}).items()}
    pointers = [_pointer(item) for item in vary]
    for candidate in (input, _varied(input, pointers, "0" * 12)):
        if validate_payload(step.input_schema, candidate, dict(step.schemas)):
            raise ConformanceError("WV-CONFORMANCE-INPUT")
    # Build every request that doesn't need an answer once, so a missing value stops the check before it starts.
    sample = _Run(step, bindings, _varied(input, pointers, "0" * 12), _SAMPLE_RUN, now)
    sample.business_key()
    if validate_payload(step.forward.action.spec.input_schema, sample.forward_input(), dict(step.schemas)):
        raise ConformanceError("WV-CONFORMANCE-INPUT")
    sample.undo_input()
    results = []
    for check in RESERVE_CHECKS if step.mode == "reserve" else COMPENSATE_CHECKS:
        run_id = new_id()
        run = _Run(step, bindings, _varied(input, pointers, run_id.hex[-12:]), str(run_id), now)
        results.append(await run.check(check, participant))
    return ConformanceReport(
        workflow=step.workflow,
        step=step.step,
        mode=step.mode,
        passed=all(item.passed for item in results),
        checks=results,
    )


class _Run:
    """The requests of one check: one run ID, one varied input."""

    def __init__(
        self, step: ParticipantStep, bindings: JsonObject, input: JsonValue, run_id: str, now: Callable[[], datetime]
    ) -> None:
        self.step, self.bindings, self.input, self.run_id, self.now = step, bindings, input, run_id, now
        self.branch = f"{run_id}:{step.step}"
        self.transaction = f"{run_id}:{step.transaction}"

    def business_key(self) -> JsonValue:
        if self.step.business_key is None:
            return None
        try:
            value = evaluate(self.step.business_key, {"input": self.input, "steps": self.bindings})
        except ExpressionFailure:
            raise ConformanceError("WV-CONFORMANCE-SCOPE") from None
        if validate_payload(BUSINESS_KEY_SCHEMA, value, {}):
            raise ConformanceError("WV-CONFORMANCE-INPUT")
        return value

    def forward_input(self) -> JsonValue:
        try:
            return evaluate(self.step.forward.expression, {"input": self.input, "steps": self.bindings})
        except ExpressionFailure:
            raise ConformanceError("WV-CONFORMANCE-SCOPE") from None

    def undo_input(self) -> JsonValue:
        """The cancel or undo's input when the step's result is unknown, as Weave builds it after a failure."""
        try:
            return evaluate(self._undo.expression, self._context("unknown", None))
        except ExpressionFailure:
            raise ConformanceError("WV-CONFORMANCE-SCOPE") from None

    @property
    def _undo(self) -> PhaseAction:
        undo = self.step.cancel or self.step.compensate
        assert undo is not None
        return undo

    def _context(self, outcome: str, output: JsonValue) -> JsonObject:
        business_key = self.business_key()
        return undo_context(
            run_id=self.run_id,
            transaction_key=self.step.transaction,
            business_key=business_key if isinstance(business_key, str) else None,
            participant_key=self.step.step,
            outcome=outcome,
            input=self.input,
            scope={"steps": self.bindings, "output": output},
        )

    async def check(self, check: ConformanceCheck, participant: Participant) -> CheckResult:
        deliveries: list[tuple[Request, Answer]] = []
        output: JsonValue = None
        completed = False
        for move in _SHAPES[check]:
            request, built = self._request(move, output, completed)
            answer = await self._send(request, participant) if built else Unanswered("input")
            deliveries.append((request, answer))
            if move == "step" and isinstance(answer, Completed):
                output, completed = answer.output, True
            if _first_failure(check, deliveries) is not None:
                break
        return judge(check, deliveries)

    def _request(self, move: Move, output: JsonValue, completed: bool) -> tuple[Request, bool]:
        """The request a move sends, and whether it could be built: its input evaluates and fits its action."""
        step = self.step
        outcome: Literal["done", "unknown"] | None = None
        value: JsonValue = None
        if move in {"step", "late"}:
            action, key = step.forward, self.branch
            value, evaluated = self.forward_input(), True
        else:
            if move == "confirm":
                assert step.confirm is not None
                action, context = step.confirm, self._context("done", output)
            else:
                action, outcome = self._undo, "done" if completed else "unknown"
                context = self._context(outcome, output)
            assert action.phase in {"confirm", "cancel", "compensate"}
            key = f"{self.run_id}:{phase_key(step.step, cast(UndoPhase, action.phase))}"
            try:
                value, evaluated = evaluate(action.expression, context), True
            except ExpressionFailure:
                evaluated = False
        built = evaluated and not validate_payload(action.action.spec.input_schema, value, dict(step.schemas))
        request = Request(
            phase=action.phase,
            reference=action.reference,
            action=action.action,
            connection=action.connection,
            input=value if built else None,
            operation_key=key,
            transaction=LeaseTransaction(
                id=self.transaction,
                branch_key=self.branch,
                phase=action.phase,
                mode=step.mode,
                forward_outcome=outcome,
            ),
            deadline=self.now() + timedelta(seconds=action.action.spec.timeout_seconds),
        )
        return request, built

    async def _send(self, request: Request, participant: Participant) -> Answer:
        limit = asyncio.timeout(request.action.spec.timeout_seconds)
        try:
            async with limit:
                answer = await participant.deliver(copy.deepcopy(request))
        except TimeoutError:
            if limit.expired():
                return Unanswered("timeout")
            raise
        if isinstance(answer, Completed):
            valid = _json(answer.output) and not validate_payload(
                request.action.spec.output_schema, answer.output, dict(self.step.schemas)
            )
            return Completed(answer.output, valid)
        return answer


def _first_failure(check: ConformanceCheck, deliveries: Sequence[tuple[Request, Answer]]) -> CheckReason | None:
    """Why the check failed at the first answer that breaks its shape, or None while every answer fits."""
    for move, (_, answer) in zip(_SHAPES[check], deliveries, strict=False):
        if isinstance(answer, Unanswered):
            return answer.reason
        if move != "late" and isinstance(answer, Failed):
            return "failed"
        if move == "late" and isinstance(answer, Completed):
            return "applied-late"
        if isinstance(answer, Completed) and not answer.valid:
            return "output"
    return None


def _delivery(request: Request, answer: Answer) -> Delivery:
    if isinstance(answer, Completed):
        return Delivery(phase=request.phase, operation_key=request.operation_key, answer="completed")
    if isinstance(answer, Unanswered):
        # A request that never answered may have applied; one the check couldn't build was never sent.
        effect: Effect = "unknown" if answer.reason == "timeout" else "none"
        code = TIMEOUT_CODE if answer.reason == "timeout" else INPUT_CODE
        return Delivery(
            phase=request.phase, operation_key=request.operation_key, answer="failed", code=code, effect=effect
        )
    return Delivery(
        phase=request.phase,
        operation_key=request.operation_key,
        answer="failed",
        code=answer.code,
        effect=answer.effect,
    )


def _json(value: object) -> bool:
    try:
        canonical_bytes(value)  # type: ignore[arg-type]
    except Exception:
        return False
    return True


def _same(first: JsonValue, second: JsonValue) -> bool:
    return canonical_bytes(first) == canonical_bytes(second)  # type: ignore[arg-type]


def _pointer(text: str) -> tuple[str, ...]:
    if not isinstance(text, str) or not text.startswith("/"):
        raise ConformanceError("WV-CONFORMANCE-INPUT")
    return tuple(part.replace("~1", "/").replace("~0", "~") for part in text[1:].split("/"))


def _varied(input: JsonValue, pointers: Sequence[tuple[str, ...]], suffix: str) -> JsonValue:
    """A copy of ``input`` with ``-{suffix}`` appended to the string each pointer names."""
    if not pointers:
        return input
    varied = copy.deepcopy(input)
    for pointer in pointers:
        parent: object = None
        node: object = varied
        for part in pointer:
            parent = node
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                raise ConformanceError("WV-CONFORMANCE-INPUT")
        if not isinstance(node, str):
            raise ConformanceError("WV-CONFORMANCE-INPUT")
        if isinstance(parent, dict):
            parent[pointer[-1]] = f"{node}-{suffix}"
        elif isinstance(parent, list):
            parent[int(pointer[-1])] = f"{node}-{suffix}"
    return varied
