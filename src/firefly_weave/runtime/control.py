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

"""Pure lexical frames keyed by instance keys, and deterministic direct-branch capacity accounting."""

from firefly_weave.compiler.ir import Node
from firefly_weave.contracts.instance_keys import node_of, split_instance
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.runtime.models import BranchState, JoinState, RunState


def loop_suffix(key: str) -> str:
    """The loop iterations of an instance key as key text, outer loop first; "" outside loops."""
    if node_of(key) == key:
        return ""
    return "".join(f"[{index}]" for index in split_instance(key).indexes)


def branch_key(owner: str, name: str, suffix: str = "") -> str:
    """A frame key: the owner step ID, its block name and the loop suffix of the steps inside it."""
    # IR author IDs and branch names are resource names; separators cannot collide.
    return f"{owner}/{name}{suffix}"


def frame_key(node: Node, key: str) -> str | None:
    """The frame that holds the instance ``key`` of ``node``; None at the top level."""
    if not node.scope:
        return None
    owner, name = node.scope[-2:]
    return branch_key(owner, name, loop_suffix(key))


def frame(state: RunState, node: Node, key: str) -> BranchState | None:
    name = frame_key(node, key)
    return None if name is None else state.branches[name]


def iteration_suffix(suffix: str, index: int) -> str:
    """The loop suffix of iteration ``index`` of a loop instance whose own suffix is ``suffix``."""
    return f"{suffix}[{index}]"


def enclosing_suffix(key: str) -> str:
    """The loop suffix of ``key`` without its innermost index: the suffix of the innermost loop instance."""
    suffix = loop_suffix(key)
    return suffix[: suffix.rindex("[")] if suffix else ""


def frame_suffix(key: str, branch: BranchState) -> str:
    """The loop suffix of a frame key: the text after ``owner/name``."""
    return key[len(branch.owner) + 1 + len(branch.name) :]


def bindings(state: RunState, node: Node, key: str) -> JsonObject:
    branch = frame(state, node, key)
    if branch is None:
        return state.steps
    if branch.index is not None:
        # An iteration frame shares the bindings visible at its loop, stored once on the loop instance.
        return {**state.loops[branch.owner + enclosing_suffix(key)].visible, **branch.steps}
    return {**branch.visible, **branch.steps}


def joined_output(state: RunState, join: JoinState) -> JsonValue:
    branches = [state.branches[key] for key in join.branches]
    if join.mode == "selected":
        return branches[0].output
    return {branch.name: branch.output for branch in sorted(branches, key=lambda branch: branch.name)}


def available(state: RunState, join: JoinState) -> list[str]:
    """Keys of the pending branches of ``join`` that may start now, in declaration order."""
    running = sum(state.branches[key].status == "running" for key in join.branches)
    pending = [key for key in join.branches if state.branches[key].status == "pending"]
    return pending[: max(0, join.concurrency - running)]
