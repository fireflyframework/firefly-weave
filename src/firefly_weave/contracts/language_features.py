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

"""Language feature names and the definition constructs that need them.

A workflow IR lists the features it uses in ``features``; a platform runs it only when it advertises each one.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, get_args

type LanguageFeature = Literal[
    "ai.agent",
    "ai.memory",
    "flow.callWorkflow",
    "flow.eventTrigger",
    "flow.forEach",
    "flow.onError",
    "flow.transaction",
    "text.concat",
    "text.join",
]

# Every feature the language defines, sorted. The values are frozen; adding one is a contract change.
LANGUAGE_FEATURES: Final[tuple[LanguageFeature, ...]] = tuple(sorted(get_args(LanguageFeature.__value__)))

# Features whose runtime support has shipped. Capabilities.language_features and the manifest serve these.
ADVERTISED_FEATURES: Final[tuple[LanguageFeature, ...]] = (
    "ai.agent",
    "flow.callWorkflow",
    "flow.forEach",
    "flow.onError",
    "flow.transaction",
    "text.concat",
    "text.join",
)

# Step kinds, operators and workflow fields that need a feature.
KIND_FEATURES: Final[Mapping[str, LanguageFeature]] = MappingProxyType(
    {
        "agent": "ai.agent",
        "forEach": "flow.forEach",
        "callWorkflow": "flow.callWorkflow",
        "transaction": "flow.transaction",
    }
)
# AI agent tool targets that need a feature beyond ai.agent: a sub-workflow tool is a workflow call.
TOOL_TARGET_FEATURES: Final[Mapping[str, LanguageFeature]] = MappingProxyType({"workflow": "flow.callWorkflow"})
OPERATOR_FEATURES: Final[Mapping[str, LanguageFeature]] = MappingProxyType(
    {"concat": "text.concat", "join": "text.join"}
)
WORKFLOW_FIELD_FEATURES: Final[Mapping[str, LanguageFeature]] = MappingProxyType(
    {"callable": "flow.callWorkflow", "eventTrigger": "flow.eventTrigger"}
)
# Step fields that need a feature, by the kinds that accept them: how a step inside a transaction is undone.
UNDO_FIELDS: Final[tuple[str, ...]] = ("confirm", "cancel", "compensate", "irreversible", "holdSeconds")
UNDO_KINDS: Final[tuple[str, ...]] = ("action", "callWorkflow")
# What a step's final failure does, by the kinds that accept it.
ERROR_FIELDS: Final[tuple[str, ...]] = ("onError", "errorPath")
ERROR_KINDS: Final[tuple[str, ...]] = (
    "action",
    "llm",
    "transform",
    "decisionTable",
    "callWorkflow",
    "humanTask",
    "signal",
    "transaction",
    "agent",
)
# Every step field that needs a feature.
_UNDO_FEATURE: Final[LanguageFeature] = "flow.transaction"
_ERROR_FEATURE: Final[LanguageFeature] = "flow.onError"
STEP_FIELD_FEATURES: Final[Mapping[str, LanguageFeature]] = MappingProxyType(
    {name: _UNDO_FEATURE for name in UNDO_FIELDS} | {name: _ERROR_FEATURE for name in ERROR_FIELDS}
)

# Language limits the manifest publishes. Limits uses them as defaults; the compiler enforces the loop limits and the
# call depth, and activation enforces the call depth again through the pinned workflows.
DEFAULT_LOOP_MAX_ITEMS: Final = 1000
MAX_LOOP_ITEMS: Final = 10_000
MAX_LOOP_DEPTH: Final = 3
MAX_RUN_ITERATIONS: Final = 100_000
MAX_CALL_DEPTH: Final = 8
# Tokens all AI agents of one run may use; the kernel stops the agent that crosses it.
MAX_RUN_AGENT_TOKENS: Final = 2_000_000
