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

"""What a calling run receives from a child run that ended; pure, so debug sessions share it with real runs."""

from typing import TYPE_CHECKING, cast

from firefly_weave.contracts.values import JsonObject, JsonValue

if TYPE_CHECKING:
    from firefly_weave.compiler.schemas import _Failure

# How a child run that did not finish reports itself to a caller that continues on failure.
STOPPED = {
    "cancelled": ("WV-RUN-CANCELLED", "The called run was cancelled."),
    "timed_out": ("WV-RUN-TIMED_OUT", "The called run timed out."),
}


def call_result(status: str, output: JsonValue, code: str | None) -> tuple[JsonValue, str | None]:
    """What a call row keeps of an ended child: its output, or a code and message."""
    if status == "succeeded":
        return output, None
    if status == "failed":
        failure = output if isinstance(output, dict) and {"code", "message"} <= set(output) else None
        error = failure or {"code": code or "WV-RUN-FAILED", "message": "The called run failed."}
        return {"code": error["code"], "message": error["message"]}, cast(str, error["code"])
    error_code, message = STOPPED[status]
    return {"code": error_code, "message": message}, error_code


def refusal(error: "_Failure") -> str:
    """The incident a caller opens instead of receiving an output its call may not hold."""
    return "WV-SCHEMA-SECRET_VALUE" if error.code == "SECRET_VALUE" else "WV-SCHEMA-CLASSIFICATION"


def screened(schema: JsonObject, output: JsonValue, bundle: dict[str, JsonObject]) -> str | None:
    """Check a succeeded child's output against the call's output schema before it reaches the caller.

    Returns the incident code the caller opens instead (a value the schema classifies as secret, or one it cannot
    classify), or None when the output may be delivered.
    """
    from firefly_weave.compiler.schemas import _Failure
    from firefly_weave.operations.redaction import classify

    try:
        classify(schema, output, bundle)
    except _Failure as error:
        return refusal(error)
    return None
