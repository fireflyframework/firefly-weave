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

import pytest
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


def answered(document, *, format="yaml", drafts=()):
    source = yaml.safe_dump(document, sort_keys=False) if format == "yaml" else json.dumps(document)
    proposal = {"title": "Rename", "kind": "workflow", "format": format, "source": source}
    return check_proposals(LumiReply(answer="Renamed.", proposals=[proposal]), drafts)


def proposed(document, *, format="yaml", drafts=()):
    [checked] = answered(document, format=format, drafts=drafts).proposals
    return checked


def errors_of(draft):
    """The error count of the draft proposed back unchanged: the check runs without a catalog."""
    return proposed(copy.deepcopy(draft), drafts=[draft]).diagnostics


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


def test_a_partly_written_guard_keeps_the_settings_it_leaves_out_and_the_drafts_stricter_values():
    draft = guarded()
    edited = copy.deepcopy(draft)
    step = edited["spec"]["steps"][0]
    step["limits"] = {"maxIterations": 8}
    step["tools"][1]["fixed"] = {"object": {}}
    step["tools"][1]["review"] = {"assignment": "order-desk"}

    kept = step_of(proposed(edited, drafts=[draft]))

    assert kept["limits"] == LIMITS
    assert kept["tools"][1]["fixed"] == CANCEL["fixed"]
    assert kept["tools"][1]["review"] == REVIEW


def test_a_guard_weave_ai_tightens_keeps_its_new_value():
    draft = guarded()
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"][0]["tools"][0]["approval"] = "required"
    edited["spec"]["steps"][0]["tools"][0]["review"] = {"assignment": "support-leads"}

    tool = step_of(proposed(edited, format="json", drafts=[draft]))["tools"][0]

    assert tool["approval"] == "required" and tool["review"] == {"assignment": "support-leads"}
    assert tool["fixed"] == LOOKUP["fixed"]


def test_a_renamed_agent_step_keeps_the_guards_of_the_draft_agent_it_replaces():
    draft = guarded()
    renamed = stripped()
    renamed["spec"]["steps"][0]["id"] = "triage"

    checked = proposed(renamed, drafts=[draft])

    step, original = step_of(checked), draft["spec"]["steps"][0]
    assert step["id"] == "triage" and checked.diagnostics == errors_of(draft)
    for name in ("limits", "memory", "onError", "errorPath", "review"):
        assert step[name] == original[name], name
    assert step["tools"] == original["tools"]


def test_a_renamed_tool_keeps_the_guards_of_the_draft_tool_with_the_same_target():
    draft = guarded()
    edited = copy.deepcopy(draft)
    tool = edited["spec"]["steps"][0]["tools"][1]
    tool["name"] = "cancel_orders"
    for name in ("fixed", "approval", "review"):
        del tool[name]

    checked = proposed(edited, drafts=[draft])

    kept = step_of(checked)["tools"][1]
    assert checked.diagnostics == errors_of(draft)
    assert kept == {**draft["spec"]["steps"][0]["tools"][1], "name": "cancel_orders"}


def twin_cancels():
    """A draft agent with two tools on the same action and connection, each fixed to its own region."""
    document = guarded()
    tools = document["spec"]["steps"][0]["tools"]
    tools[1]["name"] = "cancel_eu_order"
    tools.append(
        {**copy.deepcopy(tools[1]), "name": "cancel_us_order", "fixed": {"object": {"region": {"literal": "us"}}}}
    )
    return document


def test_a_renamed_tool_that_could_match_more_than_one_draft_tool_is_reported_rather_than_guessed():
    draft = twin_cancels()
    edited = copy.deepcopy(draft)
    tools = edited["spec"]["steps"][0]["tools"]
    for tool, name in zip(tools[1:], ("cancel_order_eu", "cancel_order_us"), strict=True):
        tool["name"] = name
        del tool["fixed"]

    checked = proposed(edited, drafts=[draft])

    assert checked.diagnostics == errors_of(draft) + 1
    assert [tool.get("fixed") for tool in step_of(checked)["tools"][1:]] == [None, None]


def test_a_renamed_agent_step_that_could_match_more_than_one_draft_agent_is_reported_rather_than_guessed():
    draft = guarded()
    second = {**copy.deepcopy(draft["spec"]["steps"][0]), "id": "escalation"}
    draft["spec"]["steps"].append(second)
    edited = copy.deepcopy(draft)
    for step, name in zip(edited["spec"]["steps"], ("triage", "handover"), strict=True):
        step["id"] = name
        del step["limits"]

    checked = proposed(edited, drafts=[draft])

    # The step's own guards can't be placed; its tools still match by target, and bring their agent's limits.
    assert checked.diagnostics == errors_of(draft) + 1
    assert step_of(checked)["limits"] == LIMITS


def test_a_guard_weave_ai_writes_looser_keeps_the_drafts_value_and_the_answer_says_so():
    draft = guarded()
    edited = copy.deepcopy(draft)
    step = edited["spec"]["steps"][0]
    step["onError"] = "continue"
    del step["errorPath"]
    step["limits"] = {"maxIterations": 2, "maxTotalTokens": 2000000}
    step["review"] = {"assignment": "order-desk", "expirySeconds": 7200, "distinctReviewer": False}
    tool = step["tools"][1]
    tool["approval"] = "auto"
    tool["fixed"] = {"object": {"region": {"ref": "/input/region"}, "reason": {"literal": "duplicate"}}}

    answer = answered(edited, drafts=[draft])

    kept, original = step_of(answer.proposals[0]), draft["spec"]["steps"][0]
    assert (kept["onError"], kept["errorPath"], kept["review"]) == ("errorOutput", ERROR_PATH, REVIEW)
    # A lower limit and a new fixed argument tighten, so they stay.
    assert kept["limits"] == {"maxIterations": 2, "maxTotalTokens": 20000}
    assert kept["tools"][1]["approval"] == "required"
    assert kept["tools"][1]["fixed"] == {"object": {"region": {"literal": "eu"}, "reason": {"literal": "duplicate"}}}
    assert kept["tools"][0] == original["tools"][0]
    notes = answer.answer.splitlines()[2:]
    assert answer.answer.startswith("Renamed.\n\n") and len(notes) == 7
    for field in ("onError", "limits.maxTotalTokens", "review.assignment", "review.expirySeconds"):
        assert any(f"{field} of agent support" in line for line in notes), field
    for field in ("review.distinctReviewer", "approval", "fixed.region"):
        assert any(f"{field} of " in line for line in notes), field


def test_a_tool_split_in_two_gives_both_its_guards():
    draft = guarded()
    edited = copy.deepcopy(draft)
    tools = edited["spec"]["steps"][0]["tools"]
    bare = {key: value for key, value in tools[1].items() if key not in ("fixed", "approval", "review")}
    tools[1:] = [{**bare, "name": "cancel_open_order"}, {**bare, "name": "cancel_shipped_order"}]

    checked = proposed(edited, drafts=[draft])

    original = draft["spec"]["steps"][0]["tools"][1]
    assert checked.diagnostics == errors_of(draft)
    for tool in step_of(checked)["tools"][1:]:
        assert {key: tool.get(key) for key in ("fixed", "approval", "review")} == {
            key: original[key] for key in ("fixed", "approval", "review")
        }


def test_a_tool_moved_to_a_new_agent_keeps_its_guards_and_brings_its_agents_limits():
    draft = guarded()
    edited = copy.deepcopy(draft)
    first = edited["spec"]["steps"][0]
    second = {**copy.deepcopy(first), "id": "support_cancel", "tools": [copy.deepcopy(first["tools"][1])]}
    first["tools"] = first["tools"][:1]
    for name in ("limits", "onError", "errorPath"):
        del second[name]
    for name in ("fixed", "approval", "review"):
        del second["tools"][0][name]
    edited["spec"]["steps"].append(second)

    checked = proposed(edited, drafts=[draft])

    moved = yaml.safe_load(checked.source)["spec"]["steps"][1]
    assert moved["tools"] == [draft["spec"]["steps"][0]["tools"][1]] and moved["limits"] == LIMITS
    # The new agent's own failure handling is its author's to choose.
    assert "onError" not in moved


def test_an_agent_shaped_value_never_takes_the_guards_of_the_real_agent():
    draft = guarded()
    edited = stripped()
    decoy = {"id": "support", "kind": "agent"}
    edited["spec"]["steps"].insert(0, {"id": "note", "kind": "transform", "value": {"literal": decoy}})

    checked = proposed(edited, drafts=[draft])

    steps = yaml.safe_load(checked.source)["spec"]["steps"]
    assert steps[0]["value"] == {"literal": decoy}
    assert steps[1]["limits"] == LIMITS and steps[1]["tools"] == draft["spec"]["steps"][0]["tools"]


@pytest.mark.parametrize("branches", ["map", "list"])
def test_an_agent_moved_into_a_parallel_branch_keeps_its_guards(branches):
    draft = guarded()
    edited = stripped()
    branch = {"steps": edited["spec"]["steps"], "output": {"literal": None}}
    # A list of branches doesn't fit the schema, but its proposal can come back to be fixed: it keeps the guards too.
    edited["spec"]["steps"] = [
        {
            "id": "fan",
            "kind": "parallel",
            "concurrency": 1,
            "branches": {"only": branch} if branches == "map" else [branch],
        }
    ]

    document = yaml.safe_load(proposed(edited, drafts=[draft]).source)

    fan = document["spec"]["steps"][0]["branches"]
    moved = (fan["only"] if branches == "map" else fan[0])["steps"][0]
    assert moved["limits"] == LIMITS and moved["tools"] == draft["spec"]["steps"][0]["tools"]


def test_a_proposal_that_repeats_a_step_id_is_withheld():
    draft = guarded()
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"].insert(0, stripped()["spec"]["steps"][0])

    assert answered(edited, drafts=[draft]).proposals == []


def test_a_workflow_renamed_to_another_drafts_name_keeps_the_strictest_guards_of_all_drafts():
    draft, other = guarded(), stripped()
    other["metadata"]["name"] = "returns-desk"
    edited = stripped()
    edited["metadata"]["name"] = "returns-desk"

    step = step_of(proposed(edited, drafts=[draft, other]))

    lookup, cancel = draft["spec"]["steps"][0]["tools"]
    assert step["limits"] == LIMITS and step["tools"][1] == cancel
    # The other draft leaves the lookup's approval to its default, which is stricter than auto.
    assert step["tools"][0] == {key: value for key, value in lookup.items() if key != "approval"}


def test_strings_that_look_like_numbers_stay_strings_when_guards_are_carried():
    from firefly_weave.compiler.parser import parse_source

    draft = guarded()
    draft["spec"]["steps"][0]["tools"][1]["fixed"]["object"]["code"] = {"literal": "1e3"}
    edited = stripped()
    edited["spec"]["steps"][0]["instructions"] = {"literal": "2E5"}
    # As a model writes it: the string quoted, so the platform reads a string.
    source = yaml.safe_dump(edited, sort_keys=False).replace("literal: 2E5", "literal: '2E5'")
    reply = LumiReply(
        answer="Done.", proposals=[{"title": "T", "kind": "workflow", "format": "yaml", "source": source}]
    )

    [checked] = check_proposals(reply, [draft]).proposals

    step = parse_source(checked.source, format="yaml").value["spec"]["steps"][0]
    assert step["instructions"] == {"literal": "2E5"}
    assert step["tools"][1]["fixed"]["object"]["code"] == {"literal": "1e3"}


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


async def test_asking_keeps_the_guards_of_a_draft_attached_without_an_inline_one(monkeypatch):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    sys.path.insert(0, str(Path(__file__).parent))
    from test_weave_ai_context import ask_through_the_service

    from firefly_weave.contracts.lumi import LumiAskRequest
    from firefly_weave.operations.lumi import LumiService

    attached = {"kind": "draft", "id": "d", "data": {"document": guarded(), "revision": 3}}
    monkeypatch.setattr(LumiService, "_attachments", AsyncMock(return_value=[attached]))
    proposal = {"title": "Rename", "kind": "workflow", "format": "yaml", "source": yaml.safe_dump(stripped())}
    request = LumiAskRequest(message="Be more polite", attachments=[{"kind": "draft", "id": str(uuid4())}])

    answer, _ = await ask_through_the_service(monkeypatch, request, LumiReply(answer="Done.", proposals=[proposal]))

    [checked] = answer.proposals
    assert step_of(checked)["limits"] == LIMITS and step_of(checked)["tools"][1]["fixed"] == CANCEL["fixed"]


def broken_draft():
    return yaml.safe_dump(guarded(), sort_keys=False) + "spec: [unclosed\n"


async def test_a_question_whose_draft_does_not_parse_reports_that_agent_guards_could_not_be_checked(monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent))
    from test_weave_ai_context import ask_through_the_service

    from firefly_weave.contracts.lumi import LumiAskRequest

    proposal = {"title": "Fix", "kind": "workflow", "format": "yaml", "source": yaml.safe_dump(stripped())}
    request = LumiAskRequest(message="Fix it", fix=True, draft={"format": "yaml", "source": broken_draft()})

    answer, _ = await ask_through_the_service(monkeypatch, request, LumiReply(answer="Fixed.", proposals=[proposal]))

    [checked] = answer.proposals
    assert checked.diagnostics == errors_of(stripped()) + 1
    assert "limits" not in step_of(checked) and "could not be checked" in answer.answer


async def test_a_question_whose_draft_does_not_parse_keeps_the_guards_of_an_attached_draft(monkeypatch):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    sys.path.insert(0, str(Path(__file__).parent))
    from test_weave_ai_context import ask_through_the_service

    from firefly_weave.contracts.lumi import LumiAskRequest
    from firefly_weave.operations.lumi import LumiService

    attached = {"kind": "draft", "id": "d", "data": {"document": guarded(), "revision": 3}}
    monkeypatch.setattr(LumiService, "_attachments", AsyncMock(return_value=[attached]))
    proposal = {"title": "Fix", "kind": "workflow", "format": "yaml", "source": yaml.safe_dump(stripped())}
    request = LumiAskRequest(
        message="Fix it",
        fix=True,
        draft={"format": "yaml", "source": broken_draft()},
        attachments=[{"kind": "draft", "id": str(uuid4())}],
    )

    answer, _ = await ask_through_the_service(monkeypatch, request, LumiReply(answer="Fixed.", proposals=[proposal]))

    [checked] = answer.proposals
    assert step_of(checked)["limits"] == LIMITS and step_of(checked)["tools"][1]["fixed"] == CANCEL["fixed"]
    assert checked.diagnostics == errors_of(guarded()) and answer.answer == "Fixed."
