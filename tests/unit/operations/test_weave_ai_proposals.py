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
"""Weave AI's proposals are checked on the server before Studio shows them."""

import yaml

from firefly_weave.contracts.lumi import LumiAnswer, LumiReply
from firefly_weave.contracts.surface import OPERATIONS
from firefly_weave.operations.lumi import check_proposals

WORKFLOW = yaml.safe_dump(
    {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "greet", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
            "outputSchema": {},
            "steps": [{"id": "build", "kind": "transform", "value": {"object": {"text": {"ref": "/input/name"}}}}],
            "output": {"ref": "/steps/build/output"},
        },
    }
)
BROKEN = WORKFLOW.replace("/steps/build/output", "/steps/missing/output")


def proposal(source, kind="workflow", format="yaml"):
    return {"title": "Greet", "kind": kind, "format": format, "source": source}


def test_each_proposal_carries_its_error_count():
    answer = check_proposals(
        LumiReply(
            answer="Here is a draft.",
            proposals=[proposal(WORKFLOW), proposal(BROKEN), proposal("steps: [unclosed")],
            followUps=["Add a decision?"],
        )
    )
    assert isinstance(answer, LumiAnswer)
    assert [item.diagnostics for item in answer.proposals][0] == 0
    assert all(item.diagnostics >= 1 for item in answer.proposals[1:])
    assert answer.answer == "Here is a draft." and answer.follow_ups == ["Add a decision?"]
    dumped = answer.model_dump(by_alias=True)
    assert dumped["proposals"][0] == {**proposal(WORKFLOW), "diagnostics": 0} and "followUps" in dumped


def test_a_proposal_whose_document_is_another_kind_counts_as_a_problem():
    answer = check_proposals(LumiReply(answer="Table", proposals=[proposal(WORKFLOW, kind="decisionTable")]))
    assert answer.proposals[0].diagnostics == 1


def test_the_ask_operation_answers_with_checked_proposals():
    assert OPERATIONS["lumi.ask"].response is LumiAnswer
