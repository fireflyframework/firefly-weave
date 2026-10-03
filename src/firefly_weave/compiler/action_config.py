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

"""Compile-time Action configuration checks supplied by trusted installed connector descriptors.

The compiler stays pure: callers pass validators explicitly, keyed by the exact digest of the
installed Connector manifest, so tenant data can never select or replace a check.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from firefly_weave.contracts.values import JsonObject, JsonValue

type ActionConfigCode = Literal["CONFIG_CONTRACT", "SIDE_EFFECT_CONTRACT", "INPUT_CONTRACT", "OUTPUT_CONTRACT"]
ACTION_CONFIG_CODES: frozenset[str] = frozenset(
    {"CONFIG_CONTRACT", "SIDE_EFFECT_CONTRACT", "INPUT_CONTRACT", "OUTPUT_CONTRACT"}
)


@dataclass(frozen=True)
class ActionConfigCheck:
    """Owned copies of one Action's connector use; validators must not perform I/O."""

    action: str
    config: JsonValue
    spec: JsonObject
    schemas: Mapping[str, JsonObject]


@dataclass(frozen=True)
class ActionConfigIssue:
    """A plain-language finding; ``path`` is a JSON pointer into the Action document (``/spec/...``)."""

    path: str
    message: str
    code: ActionConfigCode = "CONFIG_CONTRACT"
    severity: Literal["error", "warning"] = "error"


type ActionConfigValidator = Callable[[ActionConfigCheck], Sequence[ActionConfigIssue]]
