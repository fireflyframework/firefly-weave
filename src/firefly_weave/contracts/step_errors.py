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

"""The error object a handled step failure records, the copy it carries, and the failures a step may handle.

A step whose ``onError`` is ``continue`` or ``errorOutput`` turns its final failure into data: later steps read the
error object at ``/steps/<id>/error``. Transitions embed the message, so ``STEP_ERROR_MESSAGES`` is append-only:
replaying an old history must find the same words.
"""

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import Field

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.instance_keys import InstanceKeyText
from firefly_weave.contracts.language_features import ERROR_KINDS
from firefly_weave.contracts.values import JsonObject, UnicodeString

# Whether a failed step's external effect may have happened: the worker contract's vocabulary.
type StepOutcome = Literal["not_started", "failed", "unknown"]
# How a handled failure was handled; "stop" never records an error object.
type HandledPolicy = Literal["continue", "errorOutput"]

__all__ = [
    "ERROR_KINDS",
    "HANDLED_KERNEL_CODES",
    "OUTCOME_ORDER",
    "RECONCILED_FAILURE_CODES",
    "STEP_ERROR_MESSAGES",
    "STEP_ERROR_SCHEMA",
    "HandledPolicy",
    "StepError",
    "StepOutcome",
    "confirmable",
    "step_error",
    "step_error_message",
    "strongest",
]

# Strength of an outcome over several attempts: unknown beats failed beats not_started.
OUTCOME_ORDER: Final[Mapping[StepOutcome, int]] = MappingProxyType({"not_started": 0, "failed": 1, "unknown": 2})


def strongest(outcomes: Iterable[StepOutcome]) -> StepOutcome:
    """The outcome of a step over all its attempts; a step without attempts reports ``not_started``."""
    result: StepOutcome = "not_started"
    for outcome in outcomes:
        if OUTCOME_ORDER[outcome] > OUTCOME_ORDER[result]:
            result = outcome
    return result


class StepError(ContractModel):
    """A handled failure, as later steps read it. Fields are camelCase because it is a language value."""

    code: Annotated[UnicodeString, Field(min_length=1, max_length=128)]
    message: Annotated[UnicodeString, Field(min_length=1, max_length=512)]
    outcome: StepOutcome
    attempts: Annotated[int, Field(ge=0)]
    instance_key: InstanceKeyText = Field(alias="instanceKey")
    child_run_id: UUID | None = Field(default=None, alias="childRunId", exclude_if=lambda value: value is None)


# The closed schema of the error object, as the compiler types /steps/<id>/error.
STEP_ERROR_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1, "maxLength": 128},
        "message": {"type": "string", "minLength": 1, "maxLength": 512},
        "outcome": {"type": "string", "enum": ["not_started", "failed", "unknown"]},
        "attempts": {"type": "integer", "minimum": 0},
        "instanceKey": {"type": "string", "minLength": 1},
        "childRunId": {"type": "string", "format": "uuid"},
    },
    "required": ["code", "message", "outcome", "attempts", "instanceKey"],
    "additionalProperties": False,
}

# Failures of evaluating or validating a step that its onError setting handles, before any effect.
HANDLED_KERNEL_CODES: Final[frozenset[str]] = frozenset(
    {
        "WV-EXPR-MISSING",
        "WV-EXPR-TYPE",
        "WV-EXPR-RESOURCE_LIMIT",
        "WV-DECISION-NO_MATCH",
        "WV-DECISION-MULTIPLE_MATCHES",
        "WV-DECISION-INPUT",
        "WV-DECISION-OUTPUT",
        "WV-DECISION-PREDICATE",
        "WV-DECISION-RESOURCE_LIMIT",
        "WV-SCHEMA-INVALID_INSTANCE",
        "WV-SCHEMA-INVALID_FORMAT",
        "WV-SCHEMA-SECRET_VALUE",
        "WV-SCHEMA-RESOURCE_LIMIT",
    }
)

# Incidents that Confirm it had no effect (accept_reconciled_failure) resolves: the unknown outcome of a step that
# changes data and is never retried. An interim WV-TASK-FAILED is still recovery's to decide.
RECONCILED_FAILURE_CODES: Final[frozenset[str]] = frozenset({"WV-TASK-AMBIGUOUS"})


def confirmable(key: str, code: str) -> bool:
    """Whether Confirm it had no effect may resolve an incident: a forward step's unknown outcome, never the
    confirm, cancel or undo of a transaction's step."""
    from firefly_weave.contracts.transactions import split_phase

    return code in RECONCILED_FAILURE_CODES and split_phase(key) is None


# Product copy for each code a handled failure may carry. Append only: transitions embed these words.
STEP_ERROR_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "WV-EXPR-MISSING": "A value the step reads is missing.",
        "WV-EXPR-TYPE": "A value the step reads has the wrong type.",
        "WV-EXPR-RESOURCE_LIMIT": "The step's expression exceeded its size or work limit.",
        "WV-DECISION-NO_MATCH": "No rule of the decision table matched.",
        "WV-DECISION-MULTIPLE_MATCHES": "More than one rule of the decision table matched.",
        "WV-DECISION-INPUT": "The decision table's input does not match its schema.",
        "WV-DECISION-OUTPUT": "The decision table's result does not match its output schema.",
        "WV-DECISION-PREDICATE": "A rule of the decision table did not evaluate to true or false.",
        "WV-DECISION-RESOURCE_LIMIT": "The decision table exceeded its work limit.",
        "WV-SCHEMA-INVALID_INSTANCE": "The step's input does not match its schema.",
        "WV-SCHEMA-INVALID_FORMAT": "The step's input has a value in the wrong format.",
        "WV-SCHEMA-SECRET_VALUE": "The step's input contains a secret value.",
        "WV-SCHEMA-RESOURCE_LIMIT": "The step's input exceeded its size limit.",
        "WV-TASK-LEASE-EXPIRED": "The worker stopped responding before the step finished.",
        "WV-TASK-DEADLINE": "No worker picked up the step before its deadline.",
        "WV-TASK-RECONCILED-FAILURE": "Someone confirmed that the step had no effect.",
        "WV-CALL-START": "The called workflow could not start.",
        "WV-CALL-DEPTH": "The chain of called workflows is too deep.",
        "WV-RUN-CANCELLED": "The called workflow was cancelled.",
        "WV-RUN-TIMED_OUT": "The called workflow ran out of time.",
        "WV-HUMAN-EXPIRED": "Nobody completed the human task before it expired.",
        "WV-SIGNAL-TIMEOUT": "The signal did not arrive in time.",
        "WV-AGENT-ITERATIONS": "The agent reached its limit of turns.",
        "WV-AGENT-BUDGET": "The agent reached its budget.",
        "WV-AGENT-TIMEOUT": "The agent ran out of time.",
        "WV-AGENT-TOOL-FAILED": "A tool the agent called failed.",
        "WV-TXN-ROLLED-BACK": "The transaction was undone.",
        "TRANSIENT": "The step failed with a temporary error.",
        "UNAVAILABLE": "The endpoint was unavailable.",
        "RATE_LIMITED": "The endpoint refused the request because of its rate limit.",
        "TIMEOUT": "The step timed out.",
        "LLM_POLICY": "This model is not approved for the endpoint.",
        "LLM_CONNECTION": "The AI connection does not match this step.",
        "LLM_MODEL_NOT_FOUND": "The model is not installed on the endpoint.",
        "LLM_NO_TOOL_SUPPORT": "The model cannot call tools.",
        "LLM_UNREACHABLE": "Weave could not reach the endpoint.",
        "LLM_TIMEOUT": "The model did not answer in time.",
        "LLM_CONTEXT_LIMIT": "The prompt is too long for the model's context.",
        "LLM_AUTH": "The provider refused the credential.",
        "LLM_RATE_LIMITED": "The provider is rate limiting requests.",
        "LLM_CAPACITY": "Weave was too busy to run the step.",
        "LLM_OUTPUT": "The model's answer did not match the result fields.",
        "LLM_LIMIT": "The step reached its token or request limit.",
        "LLM_INPUT": "The AI step input is not valid.",
        "LLM_OPTIONS": "The provider does not accept these model options.",
        "LLM_REASONING": "The reasoning pattern did not reach a final answer.",
        "LLM_PROVIDER": "The model provider returned an unexpected error.",
    }
)


def step_error_message(code: str) -> str:
    """The frozen copy for ``code``; any other code reads as a generic failure that names it."""
    return STEP_ERROR_MESSAGES.get(code, f"The step failed with code {code}.")[:512]


def step_error(
    code: str,
    outcome: StepOutcome,
    attempts: int,
    instance_key: str,
    *,
    message: str | None = None,
    child_run_id: str | None = None,
) -> JsonObject:
    """The error object of a handled failure as a JSON value; ``message`` overrides the copy for a child's fail step."""
    error = StepError.model_validate(
        {
            "code": code[:128],
            "message": (message or step_error_message(code))[:512],
            "outcome": outcome,
            "attempts": attempts,
            "instanceKey": instance_key,
            **({"childRunId": child_run_id} if child_run_id is not None else {}),
        },
        strict=False,
    )
    return error.model_dump(mode="json", by_alias=True)
