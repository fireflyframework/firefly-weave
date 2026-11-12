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
    # The attached draft may be another workflow: the guards of the draft that doesn't parse are still unknown.
    assert checked.diagnostics == errors_of(guarded()) + 1
    assert answer.answer.startswith("Fixed.\n\nSafeguards could not be checked")


def leaning():
    """The guarded draft with a cancel tool that sets no review of its own: it gets its step's review."""
    draft = guarded()
    del draft["spec"]["steps"][0]["tools"][1]["review"]
    return draft


def test_a_review_written_on_a_tool_is_held_to_the_step_review_the_tool_had():
    draft = leaning()
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"][0]["tools"][1]["review"] = {"assignment": "anyone", "expirySeconds": 604800}

    answer = answered(edited, drafts=[draft])

    assert step_of(answer.proposals[0])["tools"][1]["review"] == REVIEW
    assert "review.assignment of tool cancel_order" in answer.answer
    assert "review.expirySeconds of tool cancel_order" in answer.answer


def test_a_tool_moved_to_a_new_agent_keeps_the_step_review_it_had():
    draft = leaning()
    edited = copy.deepcopy(draft)
    first = edited["spec"]["steps"][0]
    second = {
        **copy.deepcopy(first),
        "id": "support_cancel",
        "tools": [copy.deepcopy(first["tools"][1])],
        "review": {"assignment": "support-leads"},
    }
    first["tools"] = first["tools"][:1]
    edited["spec"]["steps"].append(second)

    moved = yaml.safe_load(proposed(edited, drafts=[draft]).source)["spec"]["steps"][1]

    assert moved["tools"][0].get("review", moved["review"]) == REVIEW


def test_a_tool_moved_to_a_stricter_agent_inherits_that_agents_review_rather_than_a_copy():
    draft = leaning()
    edited = copy.deepcopy(draft)
    first = edited["spec"]["steps"][0]
    stricter = {**REVIEW, "expirySeconds": 600}
    second = {**copy.deepcopy(first), "id": "support_cancel", "tools": [first["tools"][1]], "review": stricter}
    first["tools"] = first["tools"][:1]
    edited["spec"]["steps"].append(second)

    moved = yaml.safe_load(proposed(edited, drafts=[draft]).source)["spec"]["steps"][1]

    assert "review" not in moved["tools"][0] and moved["review"] == stricter


def test_a_moved_tool_that_two_agents_reviewed_differently_is_reported_rather_than_guessed():
    draft = leaning()
    escalation = {**copy.deepcopy(draft["spec"]["steps"][0]), "id": "escalation"}
    escalation["review"] = {**REVIEW, "assignment": "order-desk"}
    draft["spec"]["steps"].append(escalation)
    edited = copy.deepcopy(draft)
    agents = edited["spec"]["steps"]
    # Both draft tools set the same guards of their own; only the review each gets from its agent differs.
    moved = {**agents[0], "id": "handover", "tools": [{**agents[0]["tools"][1], "name": "cancel_orders"}]}
    for step in agents:
        step["tools"] = step["tools"][:1]
    agents.append(moved)
    for name in ("fixed", "approval"):
        del moved["tools"][0][name]
    edited = json.loads(json.dumps(edited))

    checked = proposed(edited, drafts=[draft])

    tool = yaml.safe_load(checked.source)["spec"]["steps"][2]["tools"][0]
    assert "fixed" not in tool and "approval" not in tool
    assert checked.diagnostics == proposed(yaml.safe_load(checked.source)).diagnostics + 1


def test_a_tool_whose_action_moves_to_another_version_keeps_its_guards():
    draft = guarded()
    edited = stripped()
    edited["spec"]["steps"][0]["tools"][1]["action"] = "orders-cancel@1.1.0"

    tool = step_of(proposed(edited, drafts=[draft]))["tools"][1]

    assert (tool["fixed"], tool["approval"], tool["review"]) == (CANCEL["fixed"], "required", REVIEW)


def other_slot(document, slot="orders_admin", connector="orders@1.0.0"):
    document["spec"]["connections"][slot] = {"connector": connector}
    document["spec"]["steps"][0]["tools"][1]["connection"] = slot
    return document


@pytest.mark.parametrize("name", ["cancel_order", "cancel_orders"], ids=["same-name", "renamed"])
def test_a_tool_on_another_slot_of_the_same_connector_keeps_its_guards(name):
    draft = guarded()
    edited = other_slot(stripped())
    edited["spec"]["steps"][0]["tools"][1]["name"] = name

    tool = step_of(proposed(edited, drafts=[draft]))["tools"][1]

    assert (tool["fixed"], tool["approval"], tool["review"]) == (CANCEL["fixed"], "required", REVIEW)


def test_a_renamed_tool_whose_action_moves_to_another_version_keeps_its_guards():
    draft = guarded()
    edited = stripped()
    tool = edited["spec"]["steps"][0]["tools"][1]
    tool["name"], tool["action"] = "cancel_orders", "orders-cancel@2.0.0"

    kept = step_of(proposed(edited, drafts=[draft]))["tools"][1]

    assert (kept["fixed"], kept["approval"], kept["review"]) == (CANCEL["fixed"], "required", REVIEW)


def test_a_tool_that_keeps_its_name_keeps_its_guards_whatever_action_it_runs():
    draft = guarded()
    edited = stripped()
    # No draft tool runs the refund action, so only the name says which tool this was.
    edited["spec"]["steps"][0]["tools"][1]["action"] = "orders-refund@1.0.0"

    tool = step_of(proposed(edited, drafts=[draft]))["tools"][1]

    assert (tool["fixed"], tool["approval"], tool["review"]) == (CANCEL["fixed"], "required", REVIEW)


def cancel_tool(step):
    [tool] = [tool for tool in step["tools"] if tool["action"].startswith("orders-cancel")]
    return tool


@pytest.mark.parametrize("written", ["left-out", "looser"])
def test_swapping_two_tools_names_never_moves_a_read_tools_safeguards_onto_a_changing_one(written):
    draft = guarded()
    edited = copy.deepcopy(draft)
    lookup, cancel = edited["spec"]["steps"][0]["tools"]
    lookup["name"], cancel["name"] = cancel["name"], lookup["name"]
    for tool in (lookup, cancel):
        for name in ("fixed", "approval", "review"):
            tool.pop(name, None)
    if written == "looser":
        cancel.update(approval="auto", fixed=copy.deepcopy(LOOKUP["fixed"]))

    tool = cancel_tool(step_of(proposed(edited, drafts=[draft])))

    assert (tool["name"], tool["approval"], tool["fixed"], tool["review"]) == (
        "lookup_order",
        "required",
        CANCEL["fixed"],
        REVIEW,
    )


def test_swapping_names_while_upgrading_the_action_and_its_connector_keeps_the_changing_tools_safeguards():
    draft = guarded()
    edited = copy.deepcopy(draft)
    lookup, cancel = edited["spec"]["steps"][0]["tools"]
    lookup["name"], cancel["name"] = cancel["name"], lookup["name"]
    for tool in (lookup, cancel):
        for name in ("fixed", "approval", "review"):
            tool.pop(name, None)
    cancel["action"] = "orders-cancel@1.1.0"
    edited["spec"]["connections"]["orders"]["connector"] = "orders@1.1.0"

    tool = cancel_tool(step_of(proposed(edited, drafts=[draft])))

    assert (tool["name"], tool["approval"], tool["fixed"], tool["review"]) == (
        "lookup_order",
        "required",
        CANCEL["fixed"],
        REVIEW,
    )


def test_a_name_match_with_two_differently_guarded_tools_of_its_new_target_takes_the_strictest():
    draft = guarded()
    tools = draft["spec"]["steps"][0]["tools"]
    tools.append(
        {**copy.deepcopy(tools[1]), "name": "cancel_us_order", "fixed": {"object": {"region": {"literal": "us"}}}}
    )
    edited = copy.deepcopy(draft)
    lookup = edited["spec"]["steps"][0]["tools"][0]
    for name in ("fixed", "approval"):
        del lookup[name]
    lookup["action"] = CANCEL["action"]

    tool = step_of(proposed(edited, drafts=[draft]))["tools"][0]

    # Its own target's first tool decides the fixed arguments; the strictest approval and review win.
    assert (tool["name"], tool["approval"], tool["fixed"], tool["review"]) == (
        "lookup_order",
        "required",
        CANCEL["fixed"],
        REVIEW,
    )


def test_a_read_tools_name_pointed_at_a_changing_action_takes_the_changing_tools_safeguards():
    draft = guarded()
    edited = stripped()
    tools = edited["spec"]["steps"][0]["tools"]
    tools[0]["action"] = CANCEL["action"]
    del tools[1]

    tool = cancel_tool(step_of(proposed(edited, drafts=[draft])))

    assert (tool["name"], tool["approval"], tool["fixed"], tool["review"]) == (
        "lookup_order",
        "required",
        CANCEL["fixed"],
        REVIEW,
    )


def test_a_tool_failure_that_stops_the_agent_is_never_loosened():
    draft = guarded()
    draft["spec"]["steps"][0]["limits"]["onToolError"] = "incident"
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"][0]["limits"]["onToolError"] = "return_to_model"

    answer = answered(edited, drafts=[draft])

    assert step_of(answer.proposals[0])["limits"]["onToolError"] == "incident"
    assert "limits.onToolError of agent support" in answer.answer


RUN_MEMORY = {"scope": "run", "key": "support-desk", "window": 12, "contextTokens": 4096}


def remembering():
    document = guarded()
    document["spec"]["steps"][0]["memory"] = copy.deepcopy(RUN_MEMORY)
    return document


@pytest.mark.parametrize(
    ("written", "fields"),
    [
        ({"scope": "conversation", "key": {"literal": "everyone"}}, ("scope", "key")),
        ({**RUN_MEMORY, "key": "all-desks"}, ("key",)),
        ({**RUN_MEMORY, "window": 200, "contextTokens": 262144}, ("window", "contextTokens")),
    ],
    ids=["wider-scope", "other-key", "larger"],
)
def test_memory_written_wider_keeps_the_drafts_and_the_answer_says_so(written, fields):
    draft = remembering()
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"][0]["memory"] = written

    answer = answered(edited, drafts=[draft])

    assert step_of(answer.proposals[0])["memory"] == RUN_MEMORY
    for name in fields:
        assert f"memory.{name} of agent support" in answer.answer, name
    assert len(answer.answer.splitlines()[2:]) == len(fields)


def test_memory_added_to_an_agent_without_it_stays_with_the_step():
    draft = guarded()
    del draft["spec"]["steps"][0]["memory"]
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"][0]["memory"] = {"scope": "run", "key": "everyone", "window": 8}

    answer = answered(edited, drafts=[draft])

    # Step memory is the default; a smaller window narrows it, so it stays.
    assert step_of(answer.proposals[0])["memory"] == {"window": 8}
    assert "memory.scope of agent support" in answer.answer and "memory.key of agent support" in answer.answer


def test_memory_narrowed_to_the_run_keeps_the_drafts_key():
    draft = guarded()
    draft["spec"]["steps"][0]["memory"] = {"scope": "conversation", "key": {"ref": "/input/message"}, "window": 12}
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"][0]["memory"] = {"scope": "run", "key": "all-desks", "window": 12}

    answer = answered(edited, drafts=[draft])

    memory = step_of(answer.proposals[0])["memory"]
    assert memory == {"scope": "run", "key": {"ref": "/input/message"}, "window": 12}
    assert answer.answer.splitlines()[2:] == [
        "Kept the draft's memory.key of agent support: only the editor can loosen a safeguard."
    ]


@pytest.mark.parametrize("written", [None, 604800], ids=["left-out", "longer"])
def test_a_conversations_memory_lifetime_keeps_the_drafts(written):
    draft = guarded()
    draft["spec"]["steps"][0]["memory"] = {
        "scope": "conversation",
        "key": {"ref": "/input/message"},
        "window": 12,
        "ttlSeconds": 3600,
    }
    edited = copy.deepcopy(draft)
    memory = edited["spec"]["steps"][0]["memory"]
    del memory["ttlSeconds"]
    if written is not None:
        memory["ttlSeconds"] = written

    answer = answered(edited, drafts=[draft])

    assert step_of(answer.proposals[0])["memory"]["ttlSeconds"] == 3600


def test_memory_narrowed_to_the_step_keeps_the_proposals_scope_and_the_lower_window():
    draft = remembering()
    edited = copy.deepcopy(draft)
    edited["spec"]["steps"][0]["memory"] = {"scope": "step", "window": 30}

    answer = answered(edited, drafts=[draft])

    assert step_of(answer.proposals[0])["memory"] == {"scope": "step", "window": 12, "contextTokens": 4096}
    assert answer.answer.splitlines()[2:] == [
        "Kept the draft's memory.window of agent support: only the editor can loosen a safeguard."
    ]


def many_loose_guards():
    """The guarded draft rewritten with more loosened guards than the answer names one by one."""
    loose = guarded()
    step = loose["spec"]["steps"][0]
    step["onError"] = "continue"
    step["limits"] = {
        "maxIterations": 32,
        "maxTotalTokens": 2000000,
        "maxToolCalls": 64,
        "parallelToolCalls": 8,
        "timeoutSeconds": 604800,
    }
    step["review"] = {"assignment": "x", "expirySeconds": 604800, "distinctReviewer": False}
    for tool in step["tools"]:
        tool["approval"] = "auto"
        tool["review"] = {"assignment": "y", "expirySeconds": 604800, "distinctReviewer": False}
        tool["fixed"] = {"object": {"region": {"literal": "us"}}}
    return loose


def test_a_withheld_proposal_is_named_first_however_many_safeguards_were_kept():
    draft = guarded()
    repeated = copy.deepcopy(draft)
    repeated["spec"]["steps"].append(copy.deepcopy(repeated["spec"]["steps"][0]))
    proposals = [
        {"title": title, "kind": "workflow", "format": "yaml", "source": yaml.safe_dump(document)}
        for title, document in (("Loose", many_loose_guards()), ("Twice", repeated))
    ]

    answer = check_proposals(LumiReply(answer="Done.", proposals=proposals), [draft])

    lines = answer.answer.splitlines()[2:]
    assert lines[0] == "Withheld the proposal Twice: it gives two steps the same id."
    folded = lines[-1]
    assert folded.startswith("Kept ") and folded.endswith(" more of the draft's safeguards.")
    assert len(lines) == 1 + 12 + 1


def test_safeguards_that_could_not_be_checked_are_named_first():
    draft = guarded()

    answer = check_proposals(
        LumiReply(
            answer="Done.",
            proposals=[
                {"title": "Loose", "kind": "workflow", "format": "yaml", "source": yaml.safe_dump(many_loose_guards())}
            ],
        ),
        [draft],
        unchecked=True,
    )

    lines = answer.answer.splitlines()[2:]
    assert lines[0].startswith("Safeguards could not be checked") and len(lines) == 1 + 12 + 1


@pytest.mark.parametrize("same_first", [False, True], ids=["other-first", "same-first"])
def test_the_same_workflows_draft_decides_what_only_the_editor_changes_whatever_the_order(same_first):
    draft = guarded()
    other = guarded()
    other["metadata"]["name"] = "returns-desk"
    other["spec"]["steps"][0]["onError"] = "continue"
    other["spec"]["steps"][0]["errorPath"] = {"steps": [], "output": {"literal": "other"}}

    step = step_of(proposed(stripped(), drafts=[draft, other] if same_first else [other, draft]))

    assert (step["onError"], step["errorPath"]) == ("errorOutput", ERROR_PATH)


def test_a_limit_the_draft_leaves_out_holds_the_proposal_to_the_contracts_default():
    draft = guarded()
    edited = copy.deepcopy(draft)
    # The draft leaves maxToolCalls out, so its agent makes at most the default 12 calls.
    edited["spec"]["steps"][0]["limits"]["maxToolCalls"] = 40

    answer = answered(edited, drafts=[draft])

    assert step_of(answer.proposals[0])["limits"] == LIMITS
    assert "limits.maxToolCalls of agent support" in answer.answer
