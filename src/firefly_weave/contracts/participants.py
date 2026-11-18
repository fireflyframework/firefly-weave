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


"""Systems that take part in transactions: the checks a step must pass, their report, and the fence's codes.

A step inside a transaction gets its requests at least once and in any order the network allows: a repeat, a
cancel or undo of a step that never arrived, the step itself after its cancel. The participant check sends a step's
own requests in each of those orders and reports, per check, what each request answered. The report names checks,
phases, operation keys, answers and failure codes only, never an input, an output or a setting.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, get_args

from pydantic import Field, model_validator

from firefly_weave.contracts.definitions import ContractModel, ResourceName, VersionedReference
from firefly_weave.contracts.transactions import Effect, PhaseOperation

type ConformanceCheck = Literal[
    "try-then-confirm",
    "try-then-cancel",
    "cancel-without-try",
    "try-after-cancel",
    "try-twice",
    "confirm-twice",
    "cancel-twice",
    "forward-then-compensate",
    "compensate-without-forward",
    "forward-after-compensate",
    "forward-twice",
    "compensate-twice",
]
type CheckReason = Literal["failed", "output", "input", "applied-late", "repeat", "timeout"]
type Answer = Literal["completed", "failed"]
# The pattern of a worker failure code (TaskError.code).
FAILURE_CODE_PATTERN: Final = r"^[A-Z][A-Z0-9_-]{0,63}$"

_CHECKS: Final[tuple[ConformanceCheck, ...]] = get_args(ConformanceCheck.__value__)
# The checks of a step that reserves and is confirmed or cancelled, in the order they run.
RESERVE_CHECKS: Final[tuple[ConformanceCheck, ...]] = _CHECKS[:7]
# The checks of a step that an action undoes, in the order they run.
COMPENSATE_CHECKS: Final[tuple[ConformanceCheck, ...]] = _CHECKS[7:]

# Codes a fence fails a request with; each means nothing was applied. Append-only.
FENCE_CODES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "TXN_ALREADY_UNDONE": "The step arrived after its cancel or undo, so nothing was applied.",
        "TXN_NOT_APPLIED": "The confirm arrived for a step that was never applied.",
        "TXN_OTHER_DIRECTION": "The step was already confirmed, or already cancelled or undone.",
    }
)
# Why a check failed, in fixed words, so a report never carries a value.
CHECK_REASONS: Final[Mapping[CheckReason, str]] = MappingProxyType(
    {
        "failed": "A request that must succeed failed.",
        "output": "An answer doesn't match its action's output schema.",
        "input": "A request's input doesn't match its action's input schema.",
        "applied-late": "The step was applied after its cancel or undo.",
        "repeat": "A repeated request answered differently from the first.",
        "timeout": "A request took longer than its action's time limit.",
    }
)
# Why the check couldn't start; fixed words, never a value. Append-only.
CONFORMANCE_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "WV-CONFORMANCE-STEP": "Choose a step inside a transaction whose confirm and cancel, or undo, run actions.",
        "WV-CONFORMANCE-ACTION": "The catalog doesn't hold an action the step uses.",
        "WV-CONFORMANCE-SCOPE": (
            "The step reads a value the check can't supply. Give earlier steps' outputs with --steps."
        ),
        "WV-CONFORMANCE-INPUT": "The input or a --vary pointer doesn't fit the step.",
        "WV-CONFORMANCE-HANDLER": "No handler runs one of the step's actions. Name them with --handlers.",
        "WV-CONFORMANCE-CONNECTION": (
            "A connection slot of the step has no usable --connection file or --secret variable."
        ),
        "WV-CONFORMANCE-DEPENDENCY": "Checking a PostgreSQL step needs the server extra (firefly-weave[server]).",
    }
)


class Delivery(ContractModel):
    """One request the check sent and what it answered, without its values."""

    phase: PhaseOperation
    operation_key: str = Field(min_length=1, max_length=512)
    answer: Answer
    # The failure's code, when it failed: a worker failure code, so a report never carries free text.
    code: str | None = Field(default=None, pattern=FAILURE_CODE_PATTERN, exclude_if=lambda value: value is None)
    # "none" when the failure said nothing was applied.
    effect: Effect | None = Field(default=None, exclude_if=lambda value: value is None)


class CheckResult(ContractModel):
    check: ConformanceCheck
    passed: bool
    reason: CheckReason | None = Field(default=None, exclude_if=lambda value: value is None)
    deliveries: list[Delivery] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def reason_of_a_failure(self) -> CheckResult:
        if (self.reason is None) != self.passed:
            raise ValueError("A failed check names its reason, and a passed one has none")
        return self


class ConformanceReport(ContractModel):
    workflow: VersionedReference
    step: ResourceName
    mode: Literal["reserve", "compensate"]
    passed: bool
    checks: list[CheckResult]

    @model_validator(mode="after")
    def every_check_of_the_mode(self) -> ConformanceReport:
        expected = RESERVE_CHECKS if self.mode == "reserve" else COMPENSATE_CHECKS
        if tuple(item.check for item in self.checks) != expected:
            raise ValueError("A report lists every check of its mode, in order")
        if self.passed != all(item.passed for item in self.checks):
            raise ValueError("A report passes exactly when every check passes")
        return self
