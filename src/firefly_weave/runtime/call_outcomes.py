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

from typing import cast

from firefly_weave.contracts.values import JsonValue

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
