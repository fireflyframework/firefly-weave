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


"""The participant check's report, the fence's codes, and the undo context shared with the kernel."""

from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.participants import (
    CHECK_REASONS,
    COMPENSATE_CHECKS,
    CONFORMANCE_MESSAGES,
    FENCE_CODES,
    RESERVE_CHECKS,
    CheckResult,
    ConformanceCheck,
    ConformanceReport,
    Delivery,
)
from firefly_weave.contracts.transactions import undo_context
from firefly_weave.contracts.workers import TaskError

RESERVE = [
    "try-then-confirm",
    "try-then-cancel",
    "cancel-without-try",
    "try-after-cancel",
    "try-twice",
    "confirm-twice",
    "cancel-twice",
]
COMPENSATE = [
    "forward-then-compensate",
    "compensate-without-forward",
    "forward-after-compensate",
    "forward-twice",
    "compensate-twice",
]


def passed(check: str) -> CheckResult:
    return CheckResult(
        check=check, passed=True, deliveries=[Delivery(phase="try", operation_key="R:hold", answer="completed")]
    )


def report(mode: str, checks: list[CheckResult], *, ok: bool = True) -> ConformanceReport:
    return ConformanceReport(workflow="move@1.0.0", step="hold", mode=mode, passed=ok, checks=checks)


def test_each_mode_has_its_checks_in_order():
    assert list(RESERVE_CHECKS) == RESERVE
    assert list(COMPENSATE_CHECKS) == COMPENSATE
    every = get_args(ConformanceCheck.__value__)
    assert sorted(RESERVE_CHECKS + COMPENSATE_CHECKS) == sorted(every)
    assert len(set(every)) == len(every) == 12


def test_a_report_lists_exactly_its_modes_checks():
    assert report("reserve", [passed(name) for name in RESERVE]).passed
    assert report("compensate", [passed(name) for name in COMPENSATE]).passed
    for checks in (
        [passed(name) for name in COMPENSATE],
        [passed(name) for name in RESERVE[:-1]],
        [passed(name) for name in [RESERVE[1], RESERVE[0], *RESERVE[2:]]],
        [passed(name) for name in [*RESERVE, RESERVE[0]]],
    ):
        with pytest.raises(ValidationError):
            report("reserve", checks)
    failed = CheckResult(
        check="try-twice",
        passed=False,
        reason="repeat",
        deliveries=[Delivery(phase="try", operation_key="R:hold", answer="completed")] * 2,
    )
    checks = [failed if name == "try-twice" else passed(name) for name in RESERVE]
    assert not report("reserve", checks, ok=False).passed
    with pytest.raises(ValidationError):
        report("reserve", checks, ok=True)
    with pytest.raises(ValidationError):
        report("reserve", [passed(name) for name in RESERVE], ok=False)


def test_a_failed_check_names_its_reason():
    delivery = [Delivery(phase="cancel", operation_key="R:hold#cancel", answer="failed", code="NOPE")]
    failed = CheckResult(check="cancel-without-try", passed=False, reason="failed", deliveries=delivery)
    assert failed.reason == "failed"
    for passed_, reason in ((False, None), (False, "because"), (True, "failed")):
        with pytest.raises(ValidationError):
            CheckResult(check="cancel-without-try", passed=passed_, reason=reason, deliveries=delivery)
    with pytest.raises(ValidationError):
        CheckResult(check="cancel-without-try", passed=True, deliveries=[])
    with pytest.raises(ValidationError):
        CheckResult(check="cancel-twice", passed=True, deliveries=delivery * 4)


def test_a_delivery_carries_no_values():
    completed = Delivery(phase="cancel", operation_key="R:hold#cancel", answer="completed")
    assert completed.model_dump() == {"phase": "cancel", "operation_key": "R:hold#cancel", "answer": "completed"}
    failed = Delivery(phase="try", operation_key="R:hold", answer="failed", code="TXN_ALREADY_UNDONE", effect="none")
    assert failed.model_dump() == {
        "phase": "try",
        "operation_key": "R:hold",
        "answer": "failed",
        "code": "TXN_ALREADY_UNDONE",
        "effect": "none",
    }
    with pytest.raises(ValidationError):
        Delivery(phase="try", operation_key="R:hold", answer="completed", output={"holdId": "h"})


def test_a_delivery_code_is_a_failure_code_never_free_text():
    assert Delivery(phase="try", operation_key="R:hold", answer="failed", code="SQL_ALREADY_UNDONE-2").code
    for code in ("hold 42 is gone", "lower_case", "", "A" * 65, "Ä_CODE"):
        with pytest.raises(ValidationError):
            Delivery(phase="try", operation_key="R:hold", answer="failed", code=code)


def test_fence_codes_are_valid_worker_failure_codes():
    assert set(FENCE_CODES) == {"TXN_ALREADY_UNDONE", "TXN_NOT_APPLIED", "TXN_OTHER_DIRECTION"}
    for code in FENCE_CODES:
        assert TaskError.model_validate({"completion_id": uuid4(), "code": code}).code == code
    assert set(CHECK_REASONS) == {"failed", "output", "input", "applied-late", "repeat", "timeout"}
    assert all(code.startswith("WV-CONFORMANCE-") for code in CONFORMANCE_MESSAGES)
    for message in (*FENCE_CODES.values(), *CHECK_REASONS.values(), *CONFORMANCE_MESSAGES.values()):
        assert message.endswith(".") and "{" not in message


def test_an_undo_reads_the_steps_own_output_and_its_keys():
    scope = {"steps": {"lookup": {"output": 1}}, "output": {"holdId": "h"}}
    context = undo_context(
        run_id="R",
        transaction_key="move",
        business_key="M-1",
        participant_key="hold",
        outcome=None,
        input={"moveId": "M-1"},
        scope=scope,
    )
    assert context == {
        "input": {"moveId": "M-1"},
        "steps": {"lookup": {"output": 1}, "hold": {"output": {"holdId": "h"}}},
        "participant": {"operationKey": "R:hold", "outcome": "unknown"},
        "transaction": {"id": "R:move", "businessKey": "M-1"},
    }
    assert scope == {"steps": {"lookup": {"output": 1}}, "output": {"holdId": "h"}}
    looped = undo_context(
        run_id="R",
        transaction_key="move",
        business_key=None,
        participant_key="hold[2]",
        outcome="done",
        input={},
        scope={"item": 5},
    )
    assert looped == {
        "input": {},
        "steps": {"hold": {"output": None}},
        "participant": {"operationKey": "R:hold[2]", "outcome": "done"},
        "transaction": {"id": "R:move", "businessKey": None},
        "item": 5,
    }
