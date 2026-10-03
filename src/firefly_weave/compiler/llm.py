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

"""Pure expansion of AI steps into ordinary durable worker actions."""

from typing import cast

from firefly_weave.contracts.agentic import TASK_TYPE, TASK_VERSION, output_schema
from firefly_weave.contracts.values import JsonObject


def llm_input(step: JsonObject, profile: JsonObject) -> JsonObject:
    return {"object": {"profile": {"literal": profile}, "prompt": step["prompt"], "context": step["context"]}}


def llm_action_valid(action: JsonObject, profile: JsonObject) -> bool:
    implementation = cast(JsonObject, action["implementation"])
    retry = cast(JsonObject, action.get("retry", {}))
    connection = cast(JsonObject, action.get("connection", {}))
    return (
        implementation == {"kind": "worker", "taskType": TASK_TYPE, "taskVersion": TASK_VERSION}
        and action["sideEffect"] == "non_idempotent"
        and retry.get("maxAttempts", 1) == 1
        and cast(int, profile["timeoutSeconds"]) <= cast(int, action["timeoutSeconds"]) <= 600
        and bool(connection)
        and connection.get("required", True) is True
    )


def llm_output_schema(profile: JsonObject) -> JsonObject:
    """Require both the declared result and evidence matching the selected model budget."""
    schema = output_schema(cast(JsonObject, profile["outputSchema"]))
    properties = cast(JsonObject, schema["properties"])
    properties["provider"] = {"const": profile["provider"]}
    properties["model"] = {"const": profile["model"]}
    usage = cast(JsonObject, cast(JsonObject, properties["usage"])["properties"])
    cast(JsonObject, usage["requests"])["maximum"] = profile["maxCalls"]
    return schema
