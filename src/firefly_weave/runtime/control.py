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

"""Pure lexical frames and deterministic direct-branch capacity accounting."""

from firefly_weave.compiler.ir import Node
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.runtime.models import BranchState, JoinState, RunState


def branch_key(owner: str, name: str) -> str:
    # IR author IDs and branch names are resource names; separators cannot collide.
    return f"{owner}/{name}"


def frame(state: RunState, node: Node) -> BranchState | None:
    if not node.scope:
        return None
    return state.branches[branch_key(*node.scope[-2:])]


def bindings(state: RunState, node: Node) -> JsonObject:
    branch = frame(state, node)
    return state.steps if branch is None else {**branch.visible, **branch.steps}


def joined_output(state: RunState, join: JoinState) -> JsonValue:
    branches = [state.branches[key] for key in join.branches]
    if join.mode == "selected":
        return branches[0].output
    return {branch.name: branch.output for branch in sorted(branches, key=lambda branch: branch.name)}


def available(state: RunState, join: JoinState) -> list[BranchState]:
    running = sum(state.branches[key].status == "running" for key in join.branches)
    pending = [state.branches[key] for key in join.branches if state.branches[key].status == "pending"]
    return pending[: max(0, join.concurrency - running)]
