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

"""Activation admission for AI steps against the operator's AI policy.

A pure check over the compiled graph and the connections an activation already resolved under
current authority: it reads no database, cache, gateway, network or secret.
"""

from collections.abc import Mapping, Sequence

from firefly_weave.ai_policy import AIPolicy
from firefly_weave.compiler.ir import ActionNode, Node
from firefly_weave.contracts.connectors import BoundConnection
from firefly_weave.definitions.models import CatalogError


def validate_ai_bindings(
    nodes: Sequence[Node], bindings: Mapping[str, BoundConnection], policy: AIPolicy | None
) -> None:
    """Refuse an AI step whose bound connection's provider or model the loaded policy does not approve.

    Without a policy the worker remains the only judge, as before. Approval is the policy's own
    answer for the connection's exact endpoint, never whether a model is installed or reachable.
    """
    if policy is None:
        return
    for node in nodes:
        if not isinstance(node, ActionNode) or node.llm_profile is None:
            continue
        if node.connection is None or node.connection not in bindings:
            raise CatalogError(422, "WV-CONNECTION", "Activation connection slots do not match requirements")
        config = bindings[node.connection].revision.config
        profile = node.llm_profile
        if profile.provider != config.get("provider"):
            raise CatalogError(422, "WV-ACTIVATION-AI-PROVIDER", "The AI connection does not match the step's provider")
        entry = policy.entry_for(str(config.get("endpoint", "")))
        if entry is None or not entry.approves(profile.provider, profile.model):
            raise CatalogError(422, "WV-ACTIVATION-AI-MODEL", "The model is not approved for this AI connection")
