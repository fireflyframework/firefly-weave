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

"""Weave AI may tighten an agent's guards in a proposal, but never loosen one by leaving it out."""

import copy
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parents[1] / "compiler"))

from test_agent_workflows import CANCEL, LOOKUP, agent, workflow  # noqa: E402

from firefly_weave.contracts.lumi import LumiReply  # noqa: E402
from firefly_weave.operations.lumi import check_proposals  # noqa: E402

REVIEW = {"assignment": "support-leads", "expirySeconds": 3600, "distinctReviewer": True}
LIMITS = {"maxIterations": 4, "maxTotalTokens": 20000}
MEMORY = {"scope": "run", "window": 12}
ERROR_PATH = {"steps": [], "output": {"literal": "unavailable"}}


def guarded():
    """A draft whose agent sets every guard: fixed arguments, approvals, reviews, limits, memory and its error path."""
    lookup = {**LOOKUP, "approval": "auto"}
    cancel = {**CANCEL, "approval": "required", "review": REVIEW}
    step = agent(
        tools=[lookup, cancel],
        review=REVIEW,
        limits=LIMITS,
        memory=MEMORY,
        onError="errorOutput",
        errorPath=ERROR_PATH,
    )
    # A plain copy: the platform parses no YAML aliases, so no two places may share one object.
    return json.loads(json.dumps(workflow(step)))


def proposed(document, *, format="yaml", drafts=()):
    source = yaml.safe_dump(document, sort_keys=False) if format == "yaml" else json.dumps(document)
    proposal = {"title": "Rename", "kind": "workflow", "format": format, "source": source}
    [checked] = check_proposals(LumiReply(answer="Renamed.", proposals=[proposal]), drafts).proposals
    return checked


def step_of(checked):
    document = yaml.safe_load(checked.source) if checked.format == "yaml" else json.loads(checked.source)
    return document["spec"]["steps"][0]


def stripped():
    """The draft as a small model writes it back from the compact schema: every guard it doesn't show is gone."""
    document = guarded()
    step = document["spec"]["steps"][0]
    step["instructions"] = {"literal": "You answer order questions politely."}
    for name in ("limits", "memory", "onError", "errorPath"):
        del step[name]
    del step["review"]["expirySeconds"], step["review"]["distinctReviewer"]
    for tool in step["tools"]:
        for name in ("fixed", "approval", "review"):
            tool.pop(name, None)
    return document


def test_a_proposal_that_leaves_out_an_agents_guards_keeps_them_from_the_draft():
    draft = guarded()

    step = step_of(proposed(stripped(), drafts=[draft]))

    assert step["instructions"] == {"literal": "You answer order questions politely."}
    original = draft["spec"]["steps"][0]
    for name in ("limits", "memory", "onError", "errorPath", "review"):
        assert step[name] == original[name], name
    assert step["tools"] == original["tools"]


def test_a_partly_written_guard_keeps_the_settings_it_leaves_out():
    draft = guarded()
    edited = copy.deepcopy(draft)
    step = edited["spec"]["steps"][0]
    step["limits"] = {"maxIterations": 8}
    step["tools"][1]["fixed"] = {"object": {}}
    step["tools"][1]["review"] = {"assignment": "order-desk"}

    kept = step_of(proposed(edited, drafts=[draft]))

    assert kept["limits"] == {"maxIterations": 8, "maxTotalTokens": 20000}
    assert kept["tools"][1]["fixed"] == CANCEL["fixed"]
    assert kept["tools"][1]["review"] == {**REVIEW, "assignment": "order-desk"}


def test_a_guard_weave_ai_tightens_keeps_its_new_value():
    draft = guarded()
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"][0]["tools"][0]["approval"] = "required"
    edited["spec"]["steps"][0]["tools"][0]["review"] = {"assignment": "support-leads"}

    tool = step_of(proposed(edited, format="json", drafts=[draft]))["tools"][0]

    assert tool["approval"] == "required" and tool["review"] == {"assignment": "support-leads"}
    assert tool["fixed"] == LOOKUP["fixed"]


def test_a_new_agent_or_a_proposal_without_a_draft_is_returned_as_written():
    document = stripped()
    source = yaml.safe_dump(document, sort_keys=False)

    assert proposed(document).source == source
    renamed = copy.deepcopy(document)
    renamed["spec"]["steps"][0]["id"] = "triage"
    assert proposed(renamed, drafts=[guarded()]).source == yaml.safe_dump(renamed, sort_keys=False)


def test_a_proposal_too_large_to_keep_its_guards_is_withheld_rather_than_shown_loosened():
    draft = guarded()
    # The draft's error path holds most of a proposal's size; the edit leaves it out.
    draft["spec"]["steps"][0]["errorPath"]["output"] = {"literal": "x" * 99000}
    edited = copy.deepcopy(draft)
    del edited["spec"]["steps"][0]["errorPath"]
    source = yaml.safe_dump(edited, sort_keys=False)
    reply = LumiReply(
        answer="Renamed.", proposals=[{"title": "Rename", "kind": "workflow", "format": "yaml", "source": source}]
    )

    assert check_proposals(reply, [draft]).proposals == []
    assert len(check_proposals(reply).proposals) == 1


async def test_asking_keeps_the_guards_of_the_draft_the_question_carries(monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent))
    from test_weave_ai_context import ask_through_the_service

    from firefly_weave.contracts.lumi import LumiAskRequest

    source = yaml.safe_dump(guarded(), sort_keys=False)
    proposal = {"title": "Rename", "kind": "workflow", "format": "yaml", "source": yaml.safe_dump(stripped())}
    request = LumiAskRequest(message="Be more polite", draft={"format": "yaml", "source": source})

    answer, _ = await ask_through_the_service(monkeypatch, request, LumiReply(answer="Done.", proposals=[proposal]))

    [checked] = answer.proposals
    assert step_of(checked)["limits"] == LIMITS and step_of(checked)["tools"][1]["fixed"] == CANCEL["fixed"]
