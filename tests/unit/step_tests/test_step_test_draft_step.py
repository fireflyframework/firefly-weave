# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""A step test finds its step anywhere in the saved draft, and only when its ID names exactly one step."""

import pytest

from firefly_weave.step_tests.admission import draft_step


def action(identifier):
    return {"id": identifier, "kind": "action", "uses": "fetch-customer@1.0.0", "with": {"ref": "/input"}}


def workflow(*steps):
    return {"kind": "Workflow", "spec": {"steps": list(steps)}}


NESTED = workflow(
    action("first"),
    {"id": "each", "kind": "forEach", "body": {"steps": [action("in-loop")]}},
    {
        "id": "choose",
        "kind": "switch",
        "cases": [{"when": {"literal": True}, "steps": [action("in-case")]}],
        "default": {"steps": [action("by-default")]},
    },
    {"id": "both", "kind": "parallel", "branches": {"left": {"steps": [action("in-branch")]}}},
    {"id": "move", "kind": "transaction", "steps": [action("in-transaction")]},
)


@pytest.mark.parametrize("identifier", ["first", "in-loop", "in-case", "by-default", "in-branch", "in-transaction"])
def test_a_step_is_found_inside_any_block(identifier):
    assert draft_step(NESTED, identifier) == action(identifier)


@pytest.mark.parametrize(
    "document, identifier",
    [
        (workflow(action("first")), "missing"),
        (workflow(action("twice"), action("twice")), "twice"),
        ({"kind": "Workflow"}, "first"),
        ({"spec": []}, "first"),
    ],
    ids=["missing", "repeated", "no-spec", "malformed"],
)
def test_a_missing_or_repeated_step_id_finds_nothing(document, identifier):
    assert draft_step(document, identifier) is None
