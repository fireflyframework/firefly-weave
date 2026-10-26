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

"""Compiler rules of step error handling: field shapes reported at their field, and the types a handled step gives.

After a step that continues on error, its output may be null and ``/steps/<id>/error`` may hold the error object.
After a step with an error path, its output is the step's own output or the path's. Inside the error path, the step
has no output and its error is always present.
"""

from copy import deepcopy
from typing import Final

from firefly_weave.compiler.calls import workflow_steps
from firefly_weave.contracts.language_features import ERROR_KINDS
from firefly_weave.contracts.values import JsonObject, JsonValue

ERROR_MESSAGES: Final[dict[str, str]] = {
    "ERROR_PATH": "errorPath goes with onError: errorOutput.",
    "ERROR_CALL_MODE": "onError applies only when the call waits for its result.",
    "ERROR_PATH_OUTPUT": "{step} has no output on its error path; read /steps/{step}/error instead.",
    "ERROR_REFERENCE": (
        "{step} records an error only when its On error setting is Continue or Continue with error output."
    ),
    "ON_ERROR_AMBIGUOUS": (
        "This step changes data and is never retried; if its outcome is unknown, the run stops with an incident "
        "until someone checks it."
    ),
}
# The JSON null a step that continues on error gives as its output.
NULL_SCHEMA: Final[JsonObject] = {"type": "null"}


def error_shape_issues(document: JsonValue) -> list[tuple[str, str]]:
    """Error settings the definition model rejects, as (code, path), reported at their field before validation."""
    found: list[tuple[str, str]] = []
    for step, path in workflow_steps(document):
        if step.get("kind") not in ERROR_KINDS:
            continue
        if (step.get("onError") == "errorOutput") != ("errorPath" in step):
            found.append(("ERROR_PATH", path + "/errorPath"))
        if step.get("kind") == "callWorkflow" and step.get("mode") == "detach" and "onError" in step:
            found.append(("ERROR_CALL_MODE", path + "/onError"))
    return found


def without_error_shape_issues(document: JsonObject) -> JsonObject:
    """A copy of a workflow definition without the error settings ``error_shape_issues`` reports."""
    copy = deepcopy(document)
    flagged = {path.rpartition("/")[0] for _, path in error_shape_issues(copy)}
    for step, path in workflow_steps(copy):
        if path in flagged:
            step.pop("onError", None)
            step.pop("errorPath", None)
    return copy


def continued_output(output: JsonObject) -> JsonObject:
    """The output of a step that continues on error: its own output, or null after a failure."""
    if not output:
        return output
    return {"anyOf": [output, NULL_SCHEMA]}


def error_path_output(output: JsonObject, path: JsonObject, contained: bool) -> JsonObject:
    """The output of a step after its error path: its own output when the path's output fits it, else either."""
    if contained or not output:
        return output
    if not path:
        return {}
    return {"anyOf": [output, path]}
