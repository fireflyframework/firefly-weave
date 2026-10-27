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

"""Try an input on this computer: a local decision table checked as the platform checks one, with no platform.

The table is compiled against an empty catalog and the compiled rules are evaluated with ``evaluate_decision_table``,
in the order ``DefinitionService.evaluate_decision`` follows. A refusal names its code, a sentence and the place it
points at; it never repeats a value from the input.
"""

from __future__ import annotations

import json
from typing import Any

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.decision_tables import DECISION_MESSAGES, DecisionFailure, evaluate_decision_table
from firefly_weave.compiler.expressions import ExpressionFailure
from firefly_weave.compiler.ir import DecisionTableIR
from firefly_weave.contracts.public import DecisionEvaluation
from firefly_weave.studio.local_contracts import DecisionEvaluateRequest

COMPILE = "WV-DECISION-COMPILE"
REFUSALS: dict[str, str] = {
    "WV-DECISION-MULTIPLE_MATCHES": "Two rules matched but the policy allows exactly one.",
    "WV-DECISION-NO_MATCH": "No rule matched and there's no Otherwise result.",
    "WV-DECISION-INPUT": "The input doesn't fit the table's input fields.",
    "WV-DECISION-OUTPUT": "The result doesn't fit the table's result fields.",
    "WV-DECISION-PREDICATE": "A rule's condition isn't true or false.",
    "WV-DECISION-REFERENCE": "Rules can read only the table's input.",
    "WV-DECISION-CONTRACT": "This isn't a decision table Weave can run.",
    "WV-DECISION-RESOURCE_LIMIT": "This table is too large to check.",
    COMPILE: "Weave can't check this table yet: fix the problems under the grid first.",
    **DECISION_MESSAGES,
}
EXPRESSION = "A rule couldn't be evaluated with this input."


class TableRefused(ValueError):
    """Why the table gave no answer: a code, a sentence and where it points."""

    def __init__(self, code: str, result: dict[str, Any]) -> None:
        self.code = code
        self.message = REFUSALS.get(code, EXPRESSION)
        self.result = result
        super().__init__(code)


def check_table(request: DecisionEvaluateRequest) -> DecisionEvaluation:
    """The table's answer for the input, or ``TableRefused``."""
    document = request.table.model_dump(mode="json", by_alias=True, exclude_none=True)
    compiled = compile_source(document, format="object", catalog=CatalogSnapshot.from_definitions([]))
    if not compiled.ok or compiled.artifact is None:
        report = json.loads(compiled.to_bytes())
        diagnostics = [
            {"code": item["code"], "path": item.get("path", "")}
            for item in report.get("diagnostics", [])
            if item.get("severity") == "error"
        ]
        raise TableRefused(COMPILE, {"diagnostics": diagnostics[:50]})
    table = DecisionTableIR.model_validate(compiled.artifact.executable)
    bundle = {d.reference: d.document for d in table.dependencies if d.kind == "Schema"}
    try:
        decision = evaluate_decision_table(table.spec.model_dump(by_alias=True), request.input, bundle=bundle)
    except (DecisionFailure, ExpressionFailure) as error:
        raise TableRefused(error.code, {"path": error.path}) from None
    return DecisionEvaluation(
        output=decision.output, matched_rule_ids=list(decision.matched_rule_ids), used_default=decision.used_default
    )
