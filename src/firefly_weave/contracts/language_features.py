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

type LanguageFeature = Literal["ai.agent", "ai.memory", "flow.callWorkflow", "flow.forEach", "text.concat", "text.join"]

# Every feature the language defines, sorted. The values are frozen; adding one is a contract change.
LANGUAGE_FEATURES: Final[tuple[LanguageFeature, ...]] = tuple(sorted(get_args(LanguageFeature.__value__)))

# Features whose runtime support has shipped. Capabilities.language_features and the manifest serve these.
ADVERTISED_FEATURES: Final[tuple[LanguageFeature, ...]] = ("text.concat", "text.join")

# Step kinds, operators and workflow fields that need a feature. AI steps add "agent": "ai.agent" with AgentStep.
KIND_FEATURES: Final[Mapping[str, LanguageFeature]] = MappingProxyType(
    {"forEach": "flow.forEach", "callWorkflow": "flow.callWorkflow"}
)
OPERATOR_FEATURES: Final[Mapping[str, LanguageFeature]] = MappingProxyType(
    {"concat": "text.concat", "join": "text.join"}
)
WORKFLOW_FIELD_FEATURES: Final[Mapping[str, LanguageFeature]] = MappingProxyType({"callable": "flow.callWorkflow"})

# Language limits the manifest publishes; loop and call support enforces them in a later release.
DEFAULT_LOOP_MAX_ITEMS: Final = 1000
MAX_LOOP_ITEMS: Final = 10_000
MAX_LOOP_DEPTH: Final = 3
MAX_RUN_ITERATIONS: Final = 100_000
MAX_CALL_DEPTH: Final = 8
