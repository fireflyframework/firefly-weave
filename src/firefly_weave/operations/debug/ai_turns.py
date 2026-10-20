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

"""Simulated AI tasks: a saved final turn becomes the step's mock, so no model is called."""

from collections.abc import Mapping

from firefly_weave.compiler.api import CompiledArtifact
from firefly_weave.compiler.ir import ActionNode
from firefly_weave.contracts.instance_keys import InvalidInstanceKey, node_of
from firefly_weave.contracts.studio_documents import ai_turns_problems
from firefly_weave.contracts.values import JsonValue
from firefly_weave.operations.debug.models import DebugError
from firefly_weave.runtime.kernel import workflow

# Nothing was sent to a model, so a replayed turn reports no usage.
NO_USAGE: dict[str, JsonValue] = {"requests": 0, "inputTokens": 0, "outputTokens": 0}


def ai_task_mocks(artifact: CompiledArtifact, entries: Mapping[str, object]) -> dict[str, JsonValue]:
    """``node:<key>`` mocks for the AI tasks that have saved turns, keyed like the entries.

    A key is a step ID (every instance) or an instance key (one instance). An AI task replays the ``final`` of its
    only turn, with its profile's provider and model and no usage. Entries without turns give no mock, and entries
    for keys that name no AI task (an AI agent's, or a removed step's) are left alone. An entry that breaks the
    recorded turns schema, or an AI task entry that is not one final turn, raises ``WV-DEBUG-MOCK-OUTPUT``.
    """
    tasks = {
        node.id: node
        for node in workflow(artifact).graph.nodes
        if isinstance(node, ActionNode) and node.llm_profile is not None
    }
    mocks: dict[str, JsonValue] = {}
    for key, entry in entries.items():
        try:
            node = tasks.get(node_of(key))
        except InvalidInstanceKey:
            continue
        if node is None or node.llm_profile is None:
            continue
        if ai_turns_problems(entry):
            raise DebugError("WV-DEBUG-MOCK-OUTPUT")
        turns = entry["turns"] if isinstance(entry, dict) else []
        if not turns:
            continue
        if len(turns) != 1 or "final" not in turns[0]:
            raise DebugError("WV-DEBUG-MOCK-OUTPUT")
        mocks["node:" + key] = {
            "result": turns[0]["final"],
            "usage": dict(NO_USAGE),
            "provider": node.llm_profile.provider,
            "model": node.llm_profile.model,
        }
    return mocks


def with_ai_turns(
    artifact: CompiledArtifact, mocks: Mapping[str, JsonValue], entries: Mapping[str, object]
) -> dict[str, JsonValue]:
    """The session's mocks plus the AI tasks' saved turns; a pin on the instance or on its step wins."""
    merged = dict(mocks)
    for key, mock in ai_task_mocks(artifact, entries).items():
        step = "node:" + node_of(key.removeprefix("node:"))
        if key not in merged and step not in merged:
            merged[key] = mock
    return merged
