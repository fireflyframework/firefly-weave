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
from firefly_weave.compiler.ir import ActionNode, AgentNode, CallWorkflowNode, Node, WorkflowIR
from firefly_weave.contracts.connectors import BoundConnection
from firefly_weave.contracts.values import JsonObject
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
        # An AI agent runs its turns on its own connection and profile, as an AI task does.
        profile = node.llm_profile if isinstance(node, (ActionNode, AgentNode)) else None
        if profile is None:
            continue
        assert isinstance(node, (ActionNode, AgentNode))
        if node.connection is None or node.connection not in bindings:
            raise CatalogError(422, "WV-CONNECTION", "Activation connection slots do not match requirements")
        config = bindings[node.connection].revision.config
        if profile.provider != config.get("provider"):
            raise CatalogError(422, "WV-ACTIVATION-AI-PROVIDER", "The AI connection does not match the step's provider")
        entry = policy.entry_for(str(config.get("endpoint", "")))
        if entry is None or not entry.approves(profile.provider, profile.model):
            raise CatalogError(422, "WV-ACTIVATION-AI-MODEL", "The model is not approved for this AI connection")


def validate_ai_reserved(ir: WorkflowIR) -> None:
    """Refuse an IR that runs the model turn task anywhere but as an AI agent's model, on the platform's turn action.

    An agent step supplies what keeps a turn safe (the run's token cap, the data notice, the reviewed transcript); any
    other step would leave that to its author. No agent tool runs a model task either, so a model never starts another
    model outside that cap, and no undo action of a transaction step runs one, as an undo has none of an agent's
    guards. This holds with or without an AI policy.
    """
    from firefly_weave.compiler.agent_tools import runs_model_task
    from firefly_weave.compiler.agents import runs_turn_task, turn_action_digest

    platform = turn_action_digest()
    documents = {dependency.digest: dependency.document for dependency in ir.dependencies}

    def spec_of(dependency: object) -> JsonObject:
        """The pinned document's spec; a person's tool pins none, and a workflow's spec runs no task itself."""
        if isinstance(dependency, str) and dependency not in documents:
            # What a pin without its document runs can't be checked, so it may be the model turn.
            raise CatalogError(
                422,
                "WV-ACTIVATION-AI-RESERVED",
                "The workflow pins a document it doesn't carry, so what that runs can't be checked",
            )
        spec = documents.get(dependency, {}).get("spec") if isinstance(dependency, str) else None
        return spec if isinstance(spec, dict) else {}

    for node in ir.graph.nodes:
        dependency = getattr(node, "dependency", None)
        if isinstance(node, AgentNode):
            reserved = dependency != platform or any(runs_model_task(spec_of(tool.dependency)) for tool in node.tools)
        else:
            reserved = runs_turn_task(spec_of(dependency))
        if isinstance(node, (ActionNode, CallWorkflowNode)) and node.txn is not None:
            reserved = reserved or any(runs_model_task(spec_of(phase.dependency)) for phase in node.txn.phases.values())
        if reserved:
            raise CatalogError(422, "WV-ACTIVATION-AI-RESERVED", "Only an AI agent step runs the platform's model turn")
