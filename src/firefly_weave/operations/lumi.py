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

"""Private ephemeral assistant: current caller reads and explicit connection delegation."""

import asyncio
import contextlib
import copy
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, NamedTuple, cast
from uuid import UUID

import yaml
from pyfly.container import service
from sqlalchemy import text

from firefly_weave.access.audit import AuditContext
from firefly_weave.access.models import Principal
from firefly_weave.access.repository import load_principal
from firefly_weave.access.service import audit
from firefly_weave.ai_policy import DEFAULT_CONTEXT_TOKENS
from firefly_weave.compiler.api import validate_authoring
from firefly_weave.compiler.expressions import measure_value
from firefly_weave.compiler.parser import parse_source
from firefly_weave.connections.diagnostics import keyless_connection
from firefly_weave.connections.repository import ConnectionRepository
from firefly_weave.connections.secret_execution import resolve_secret
from firefly_weave.connections.secrets import SecretUnavailable
from firefly_weave.connections.service import ConnectionService
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR, CONNECTOR_REFERENCE, validate_connection
from firefly_weave.contracts.connectors import ConnectionRevision
from firefly_weave.contracts.definitions import AgentLimits, AgentMemory, AgentReview
from firefly_weave.contracts.lumi import (
    LumiAnswer,
    LumiAskRequest,
    LumiCheckedProposal,
    LumiConfiguration,
    LumiConfigurationRequest,
    LumiProposal,
    LumiReply,
    LumiStatus,
)
from firefly_weave.contracts.run_views import RunStepQuery, RunSummary, StepFact
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.values import JsonObject, JsonValue
from firefly_weave.definitions.models import CatalogError
from firefly_weave.deployments.service import DeploymentService
from firefly_weave.operations.ai_policy import APIAIPolicy
from firefly_weave.operations.debug.store import DebugService
from firefly_weave.operations.history import HistoryService
from firefly_weave.operations.lumi_context import (
    Profile,
    Shaped,
    choose_profile,
    input_budget,
    output_tokens,
    run_context,
    shape,
)
from firefly_weave.operations.lumi_deployments import COLLECTIONS, deployment_context
from firefly_weave.operations.lumi_gateway import LumiGatewayClient
from firefly_weave.operations.run_views import RunViewService
from firefly_weave.persistence.uow import Transaction

# The document kind each proposal kind must hold.
PROPOSAL_KINDS = {
    "workflow": "Workflow",
    "decisionTable": "DecisionTable",
    "action": "Action",
    "connector": "Connector",
}


# An AI agent's guards. Weave AI never loosens one, whether a proposal leaves the guard out or writes a looser value:
# the compact schema small models write from doesn't show them all, and what a model writes can be steered. Loosening
# is done in the editor. Against every draft the question carried, the strictest value wins: a required approval, a
# distinct reviewer, the lower review expiry, the lower of each limit and memory size, and a tool failure that stops
# the agent. The draft's fixed arguments, reviewers, memory scope and key, onError and errorPath stay as the draft has
# them; a proposal may add fixed arguments, limits and reviews, and narrow the memory to the step. A guard left out
# keeps the draft's value, settings key by key, and a value the proposal changed back is named in the answer.
STEP_GUARDS: Final = ("onError", "errorPath")
TOOL_GUARDS: Final = ("fixed", "approval", "review", "human")
# An omitted approval is the target's default, which reviews a tool that changes data.
APPROVALS: Final = ("auto", None, "required")
LIMITS: Final = {
    limit.alias: limit.default
    for limit in AgentLimits.model_fields.values()
    if limit.alias is not None and type(limit.default) is int
}
# What a tool's final failure does, from the loosest: the model reads the error, or the agent stops.
TOOL_ERRORS: Final = ("return_to_model", "incident")
TOOL_ERROR: Final = cast(str, AgentLimits.model_fields["on_tool_error"].default)
REVIEW_EXPIRY: Final = cast(int, AgentReview.model_fields["expiry_seconds"].default)
# Memory scopes from the narrowest: one activation, the run, then a conversation kept across runs.
MEMORY_SCOPES: Final = ("step", "run", "conversation")
MEMORY_SIZES: Final = {
    size.alias or name: size.default for name, size in AgentMemory.model_fields.items() if type(size.default) is int
}
# What the memory scope decides with it: the name it is kept under, and how long a conversation lasts.
MEMORY_SCOPED: Final = ("scope", "key", "ttlSeconds")
MAX_PROPOSAL_CHARACTERS: Final = 100000
# The answer's limit in the reply contract, and how many kept guards it names before it counts the rest.
MAX_ANSWER_CHARACTERS: Final = 20000
MAX_NOTES: Final = 12
UNCHECKED: Final = (
    "Safeguards could not be checked: the draft sent with the question does not parse, so check the agents' "
    "approvals, reviews, fixed arguments and limits before you apply this."
)
# Plain YAML scalars the platform reads as something other than a string; a string that looks like one is quoted.
NOT_PLAIN: Final = re.compile(r"[-+]?\.?[0-9][0-9._]*(?:[eE][-+]?[0-9]+)?|[-+]?\.(?:inf|nan)|null|true|false|~|", re.I)


class _ProposalDumper(yaml.SafeDumper):
    """YAML that reads back as the same JSON values on the platform: "1e3" stays a string."""


def _string(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"' if NOT_PLAIN.fullmatch(value) else None)


_ProposalDumper.add_representer(str, _string)


def _steps(document: JsonValue) -> list[JsonObject]:
    """A workflow document's steps, nested ones included, read only where steps sit: a value shaped like a step is
    never one."""
    spec = document.get("spec") if isinstance(document, dict) else None
    top = spec.get("steps") if isinstance(spec, dict) else None
    found: list[JsonObject] = []
    pending: list[JsonValue] = list(reversed(top)) if isinstance(top, list) else []
    while pending:
        step = pending.pop()
        if not isinstance(step, dict):
            continue
        found.append(step)
        kind, branches = step.get("kind"), step.get("branches")
        blocks: list[JsonValue] = [step.get("errorPath")]
        if kind == "switch":
            cases = step.get("cases")
            blocks += [*(cases if isinstance(cases, list) else []), step.get("default")]
        elif kind == "parallel" and isinstance(branches, dict):
            blocks += list(branches.values())
        elif kind == "parallel" and isinstance(branches, list):
            # A list of branches doesn't fit the schema, yet a proposal that has one can come back as a draft to fix.
            blocks += branches
        elif kind == "forEach":
            blocks.append(step.get("body"))
        elif kind == "transaction":
            blocks.append(step)
        nested = [block.get("steps") for block in blocks if isinstance(block, dict)]
        pending.extend(reversed([child for steps in nested if isinstance(steps, list) for child in steps]))
    return found


def _agents(document: JsonValue) -> dict[str, JsonObject]:
    """The agent steps of a workflow document by id; the first one when an id repeats."""
    found: dict[str, JsonObject] = {}
    for step in _steps(document):
        identifier = step.get("id")
        if step.get("kind") == "agent" and isinstance(identifier, str):
            found.setdefault(identifier, step)
    return found


def _repeats_an_id(document: JsonValue) -> bool:
    identifiers = [step["id"] for step in _steps(document) if isinstance(step.get("id"), str)]
    return len(identifiers) != len(set(identifiers))


def _tools(step: JsonObject) -> list[JsonObject]:
    tools = step.get("tools")
    return [tool for tool in (tools if isinstance(tools, list) else []) if isinstance(tool, dict)]


def _guarded_step(step: JsonObject) -> bool:
    """Whether an agent step holds a guard of its own or on one of its tools."""
    names = (*STEP_GUARDS, "limits", "memory", "review")
    return any(name in step for name in names) or any(_guarded_tool(tool) for tool in _tools(step))


def _guarded_tool(tool: JsonObject) -> bool:
    return any(name in tool for name in TOOL_GUARDS)


def _agent_target(step: JsonObject) -> str:
    """What a renamed agent step keeps: the turn it uses and its model profile."""
    return json.dumps([step.get("uses"), step.get("profile")], sort_keys=True)


def _connectors(document: JsonValue) -> dict[str, JsonValue]:
    """The connector each connection slot of a workflow document declares."""
    spec = document.get("spec") if isinstance(document, dict) else None
    slots = spec.get("connections") if isinstance(spec, dict) else None
    return {
        slot: value["connector"]
        for slot, value in (slots.items() if isinstance(slots, dict) else ())
        if isinstance(value, dict) and "connector" in value
    }


def _tool_target(tool: JsonObject, connectors: dict[str, JsonValue]) -> str | None:
    """What a renamed tool keeps: its action, whatever its version, on its slot's connector, whatever its version (the
    slot when its document declares none); its workflow; or who it asks. None when it names none."""
    human = tool.get("human")
    if "action" in tool:
        action, slot = tool["action"], tool.get("connection")
        name = action.rpartition("@")[0] or action if isinstance(action, str) else action
        connection = connectors.get(slot, slot) if isinstance(slot, str) else slot
        if isinstance(connection, str):
            connection = connection.rpartition("@")[0] or connection
        target: list[JsonValue] = ["action", name, connection]
    elif "workflow" in tool:
        target = ["workflow", tool["workflow"]]
    elif isinstance(human, dict):
        target = ["human", human.get("assignment")]
    else:
        return None
    return json.dumps(target, sort_keys=True)


def _renamed_agents(edited: dict[str, JsonObject], previous: dict[str, list[JsonObject]]) -> tuple[dict[str, str], int]:
    """Each agent the edit renamed, by the draft id it replaces; and how many renames could match more than one guarded
    agent. An agent whose id no draft holds is matched to the one draft agent the edit no longer names with the same
    uses and profile. Several on either side can't be told apart, so their guards are reported, never guessed."""
    added: dict[str, list[str]] = {}
    for identifier, step in edited.items():
        if identifier not in previous:
            added.setdefault(_agent_target(step), []).append(identifier)
    gone: dict[str, list[str]] = {}
    for identifier, steps in previous.items():
        if identifier not in edited:
            gone.setdefault(_agent_target(steps[0]), []).append(identifier)
    renamed: dict[str, str] = {}
    ambiguous = 0
    for key, identifiers in added.items():
        replaced = gone.get(key, [])
        if len(identifiers) == len(replaced) == 1:
            renamed[identifiers[0]] = replaced[0]
        elif any(_guarded_step(step) for identifier in replaced for step in previous[identifier]):
            ambiguous += 1
    return renamed, ambiguous


@dataclass
class _Kept:
    """What keeping the drafts' guards in one proposal changed back, and what it couldn't place."""

    notes: list[str] = field(default_factory=list)
    unresolved: int = 0

    def changed_back(self, name: str, where: str) -> None:
        line = f"Kept the draft's {name} of {where}: only the editor can loosen a safeguard."
        if line not in self.notes:
            self.notes.append(line)


def _lowest(edited: JsonObject, kept: Sequence[JsonObject], name: str, default: int) -> bool:
    """Hold a number to the lowest the drafts allow, an omitted one being the default; whether a written one changed."""
    allowed = [
        value
        for value in (draft.get(name, default) for draft in kept)
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    written, stated = edited.get(name), name in edited
    if not allowed or (isinstance(written, int) and not isinstance(written, bool) and written <= min(allowed)):
        return False
    lowest = min(allowed)
    # A draft that states the lowest value keeps it written; the default stays omitted.
    if any(draft.get(name) == lowest for draft in kept):
        edited[name] = lowest
    else:
        edited.pop(name, None)
    return stated


def _review(
    edited: JsonObject, kept: Sequence[JsonObject], notes: _Kept, where: str, inherited: JsonValue = None
) -> None:
    """Hold a review to the drafts' reviews. ``inherited`` is the review the edited tool gets from its step when it sets
    none: the tool keeps it unless a draft's review is stricter."""
    reviews = [draft["review"] for draft in kept if isinstance(draft.get("review"), dict)]
    if not reviews:
        return
    value = edited.get("review")
    seeded = "review" not in edited and isinstance(inherited, dict)
    if seeded:
        value = copy.deepcopy(cast(JsonObject, inherited))
    elif not isinstance(value, dict):
        if "review" in edited:
            notes.changed_back("review", where)
        value = {}
    first = cast(JsonObject, reviews[0])
    if "assignment" in first and value.get("assignment") != first["assignment"]:
        if "assignment" in value:
            notes.changed_back("review.assignment", where)
        value["assignment"] = copy.deepcopy(first["assignment"])
    distinct = any(cast(JsonObject, review).get("distinctReviewer") is True for review in reviews)
    if distinct and value.get("distinctReviewer") is not True:
        if "distinctReviewer" in value:
            notes.changed_back("review.distinctReviewer", where)
        value["distinctReviewer"] = True
    if _lowest(value, cast(list[JsonObject], reviews), "expirySeconds", REVIEW_EXPIRY):
        notes.changed_back("review.expirySeconds", where)
    for key, setting in first.items():
        if key not in value:
            value[key] = copy.deepcopy(setting)
    if not (seeded and value == inherited):
        edited["review"] = value


def _limits(step: JsonObject, kept: Sequence[JsonObject], notes: _Kept, where: str) -> None:
    drafts = [cast(JsonObject, agent["limits"]) if isinstance(agent.get("limits"), dict) else {} for agent in kept]
    value = step.get("limits")
    if not isinstance(value, dict):
        if "limits" in step:
            notes.changed_back("limits", where)
        value = {}
    for name, default in LIMITS.items():
        if _lowest(value, drafts, name, default):
            notes.changed_back(f"limits.{name}", where)
    stops = [draft.get("onToolError", TOOL_ERROR) for draft in drafts]
    if TOOL_ERRORS[-1] in stops and value.get("onToolError") != TOOL_ERRORS[-1]:
        if "onToolError" in value:
            notes.changed_back("limits.onToolError", where)
        value["onToolError"] = TOOL_ERRORS[-1]
    for draft in drafts:
        for key, setting in draft.items():
            if key not in value:
                value[key] = copy.deepcopy(setting)
    if value or "limits" in step:
        step["limits"] = value


def _fixed(tool: JsonObject, kept: Sequence[JsonObject], notes: _Kept, where: str) -> None:
    """The drafts' fixed arguments stay fixed to their values; the first draft that fixes an argument decides it."""
    drafts = [draft["fixed"] for draft in kept if "fixed" in draft]
    objects = [fixed.get("object") if isinstance(fixed, dict) else None for fixed in drafts]
    whole = next(
        (fixed for fixed, arguments in zip(drafts, objects, strict=True) if not isinstance(arguments, dict)), None
    )
    if not drafts or whole is not None:
        if whole is not None and tool.get("fixed") != whole:
            if "fixed" in tool:
                notes.changed_back("fixed", where)
            tool["fixed"] = copy.deepcopy(whole)
        return
    value = tool.get("fixed")
    arguments = value.get("object") if isinstance(value, dict) else None
    if not isinstance(arguments, dict):
        if "fixed" in tool:
            notes.changed_back("fixed", where)
        arguments = {}
    decided: set[str] = set()
    for fixed in cast(list[JsonObject], objects):
        for key, argument in fixed.items():
            if key in decided:
                continue
            decided.add(key)
            if key not in arguments or arguments[key] != argument:
                if key in arguments:
                    notes.changed_back(f"fixed.{key}", where)
                arguments[key] = copy.deepcopy(argument)
    tool["fixed"] = {"object": arguments}


def _keep_tool(tool: JsonObject, kept: Sequence[JsonObject], notes: _Kept, where: str, inherited: JsonValue) -> None:
    def approval(value: JsonValue) -> str | None:
        return next((known for known in APPROVALS if known is not None and known == value), None)

    written = tool.get("approval")
    allowed = [approval(draft.get("approval")) for draft in kept]
    if approval(written) is not None:
        allowed.append(approval(written))
    strictest = max(allowed, key=APPROVALS.index)
    if strictest != written or ("approval" in tool) != (strictest is not None):
        if "approval" in tool:
            notes.changed_back("approval", where)
        if strictest is None:
            tool.pop("approval", None)
        else:
            tool["approval"] = strictest
    _review(tool, kept, notes, where, inherited)
    _fixed(tool, kept, notes, where)
    human = next((draft["human"] for draft in kept if "human" in draft), None)
    if "human" not in tool and human is not None:
        tool["human"] = copy.deepcopy(human)


def _keep_step(step: JsonObject, kept: Sequence[JsonObject], notes: _Kept, where: str) -> None:
    first = kept[0]
    for name in STEP_GUARDS:
        if (name in step) != (name in first) or step.get(name) != first.get(name):
            if name in step:
                notes.changed_back(name, where)
            if name in first:
                step[name] = copy.deepcopy(first[name])
            else:
                del step[name]
    _review(step, kept, notes, where)
    _memory(step, kept, notes, where)


def _memory(step: JsonObject, kept: Sequence[JsonObject], notes: _Kept, where: str) -> None:
    """Memory decides what an agent shares across activations and runs: its scope stays the first draft's unless the
    proposal narrows it, its key stays the first draft's unless the scope becomes step, which takes none, and each size
    is held to the lowest the drafts allow."""
    drafts = [cast(JsonObject, agent["memory"]) if isinstance(agent.get("memory"), dict) else {} for agent in kept]
    first = drafts[0]
    value = step.get("memory")
    if not isinstance(value, dict):
        if "memory" in step:
            notes.changed_back("memory", where)
        value = {}

    def rank(scope: JsonValue) -> int:
        return MEMORY_SCOPES.index(scope) if isinstance(scope, str) and scope in MEMORY_SCOPES else len(MEMORY_SCOPES)

    # A scope left out keeps the draft's, like any guard; only a narrower one written replaces it. A conversation's
    # lifetime goes with its scope.
    narrowed = "scope" in value and rank(value["scope"]) < rank(first.get("scope", MEMORY_SCOPES[0]))
    kept_names: list[str] = [] if narrowed else ["scope", "ttlSeconds"]
    if not narrowed or value["scope"] != MEMORY_SCOPES[0]:
        kept_names.append("key")
    for name in kept_names:
        if (name in value) != (name in first) or value.get(name) != first.get(name):
            if name in value:
                notes.changed_back(f"memory.{name}", where)
            if name in first:
                value[name] = copy.deepcopy(first[name])
            else:
                del value[name]
    for name, default in MEMORY_SIZES.items():
        if _lowest(value, drafts, name, default):
            notes.changed_back(f"memory.{name}", where)
    for key, setting in first.items():
        if key not in value and key not in MEMORY_SCOPED:
            value[key] = copy.deepcopy(setting)
    if value or "memory" in step:
        step["memory"] = value


class KeptGuards(NamedTuple):
    changed: bool
    # Renames whose guards can't be placed because more than one agent or tool could be the one replaced: each is an
    # error, as the guards may be lost.
    unresolved: int
    # One line per guard a proposal wrote looser and the draft's value replaced.
    notes: tuple[str, ...]


def keep_agent_guards(document: JsonValue, drafts: Sequence[JsonValue]) -> KeptGuards:
    """Hold each agent a proposal writes to the strictest guards of the drafts it may replace; in place.

    An agent is matched by its step id, or when renamed, by its uses and profile; a tool by its name in its agent,
    whatever its target, then by its target (action and connector, workflow, or who it asks) in that agent, then in
    any agent of any draft. A matched tool keeps the review it really had: its own, else its agent's. An agent holding
    a matched tool keeps that tool's agent's limits. A new agent and a new tool are left as written.
    """
    if not isinstance(document, dict):
        return KeptGuards(changed=False, unresolved=0, notes=())

    def name_of(value: JsonValue) -> object:
        metadata = value.get("metadata") if isinstance(value, dict) else None
        return metadata.get("name") if isinstance(metadata, dict) else None

    # Every draft counts; a draft of the same workflow decides first what only the editor changes.
    ordered = sorted(drafts, key=lambda draft: name_of(draft) != name_of(document))
    previous: dict[str, list[JsonObject]] = {}
    targets: dict[str, list[tuple[JsonObject, JsonObject]]] = {}
    # Each draft tool's target, by the tool: a slot's connector is read from the tool's own draft.
    aims: dict[int, str | None] = {}
    for draft in ordered:
        connectors = _connectors(draft)
        for identifier, step in _agents(draft).items():
            previous.setdefault(identifier, []).append(step)
            for tool in _tools(step):
                aims[id(tool)] = target = _tool_target(tool, connectors)
                if target is not None:
                    targets.setdefault(target, []).append((step, tool))
    before = json.dumps(document, sort_keys=True)
    connectors = _connectors(document)
    agents = _agents(document)
    renamed, ambiguous = _renamed_agents(agents, previous)
    kept = _Kept(unresolved=ambiguous)
    unclear: set[str] = set()

    def really(agent: JsonObject, old: JsonObject, inherits: bool) -> JsonObject:
        """The draft tool with the review it really gets: its agent's when it sets none, unless its calls run
        unreviewed. Only when the edited tool doesn't simply inherit its own step's review."""
        review = agent.get("review")
        if not inherits or "review" in old or old.get("approval") == "auto" or not isinstance(review, dict):
            return old
        return {**old, "review": review}

    def guards(agent: JsonObject, old: JsonObject) -> JsonObject:
        return {key: really(agent, old, True).get(key) for key in TOOL_GUARDS}

    for identifier, step in agents.items():
        matched = previous.get(identifier) or previous.get(renamed.get(identifier, ""), [])
        if matched:
            _keep_step(step, matched, kept, f"agent {identifier}")
        holders = list(matched)
        for tool in _tools(step):
            name, target = tool.get("name"), _tool_target(tool, connectors)
            named = [(agent, old) for agent in matched for old in _tools(agent) if old.get("name") == name]
            aimed: list[tuple[JsonObject, JsonObject]] = []
            if target is not None and (not named or any(aims.get(id(old)) != target for _, old in named)):
                # A tool that keeps a name but runs another target also answers to the draft tools of its target, so
                # swapping names never moves a read tool's looser guards onto a tool that changes data.
                found = [(agent, old) for agent in matched for old in _tools(agent) if aims.get(id(old)) == target]
                aimed = found or targets.get(target, [])
                holders += [agent for agent, _ in aimed]
                if not named and len({json.dumps(guards(agent, old), sort_keys=True) for agent, old in aimed}) > 1:
                    unclear.add(target)
                    continue
            # The tools of the same target come first: their fixed arguments belong to the action.
            same = list({id(old): (agent, old) for agent, old in [*aimed, *named]}.values())
            if same:
                own = "review" in tool
                had = [really(agent, old, own or all(agent is not held for held in matched)) for agent, old in same]
                # A tool that leaves its review out gets its step's: that one stays unless a draft's is stricter.
                inherited = None if own or any("review" in old for _, old in same) else step.get("review")
                _keep_tool(tool, had, kept, f"tool {name}", inherited)
        if holders:
            _limits(step, list({id(agent): agent for agent in holders}.values()), kept, f"agent {identifier}")
    return KeptGuards(
        changed=json.dumps(document, sort_keys=True) != before,
        unresolved=kept.unresolved + len(unclear),
        notes=tuple(kept.notes),
    )


def _source(document: JsonValue, format: str) -> str:
    if format == "json":
        return json.dumps(document, ensure_ascii=False)
    return yaml.dump(document, Dumper=_ProposalDumper, sort_keys=False, allow_unicode=True)


class _Guarded(NamedTuple):
    # The proposal held to the drafts' guards; None when it must be withheld.
    proposal: LumiProposal | None
    # Errors the guards add: unresolved renames, and agents whose guards could not be checked.
    errors: int
    # What the answer must say whatever else it says: a proposal withheld, or guards that could not be checked.
    alerts: tuple[str, ...]
    # One line per guard the proposal wrote looser and the draft's value replaced.
    notes: tuple[str, ...]


def _guarded(proposal: LumiProposal, drafts: Sequence[JsonValue], unchecked: bool) -> _Guarded:
    if proposal.kind != "workflow" or not (drafts or unchecked):
        return _Guarded(proposal, 0, (), ())
    try:
        document = parse_source(proposal.source, format=proposal.format).value
    except Exception:
        return _Guarded(proposal, 0, (), ())
    title = proposal.title
    if _repeats_an_id(document):
        # Two steps with one id: which of them the draft's guards belong to can't be told.
        return _Guarded(None, 0, (f"Withheld the proposal {title}: it gives two steps the same id.",), ())
    # The draft that didn't parse may hold agents no other draft has, so their guards stay unknown.
    alerts, errors = ((UNCHECKED,), 1) if unchecked and _agents(document) else ((), 0)
    if not drafts:
        return _Guarded(proposal, errors, alerts, ())
    kept = keep_agent_guards(document, drafts)
    if not kept.changed:
        return _Guarded(proposal, errors + kept.unresolved, alerts, kept.notes)
    source = _source(document, proposal.format)
    if len(source) > MAX_PROPOSAL_CHARACTERS:
        # A proposal that can't keep its guards within the size limit is withheld rather than shown loosened.
        withheld = f"Withheld the proposal {title}: it is too large once its agents keep their safeguards."
        return _Guarded(None, 0, (withheld,), ())
    return _Guarded(proposal.model_copy(update={"source": source}), errors + kept.unresolved, alerts, kept.notes)


def _with_notes(answer: str, alerts: Sequence[str], notes: Sequence[str]) -> str:
    """The answer, then the alerts, then the kept guards: the first ones by name and a count of the rest."""
    if not (alerts or notes):
        return answer
    shown = [*alerts, *notes[:MAX_NOTES]]
    if len(notes) > MAX_NOTES:
        shown.append(f"Kept {len(notes) - MAX_NOTES} more of the draft's safeguards.")
    text = "\n".join(shown)
    return f"{answer[: max(0, MAX_ANSWER_CHARACTERS - len(text) - 2)]}\n\n{text}"


def check_proposals(reply: LumiReply, drafts: Sequence[JsonValue] = (), unchecked: bool = False) -> LumiAnswer:
    """The reply with each proposal's error count from the platform's own authoring check (no catalog needed).

    ``drafts`` are the workflow documents the question carried: each agent a proposal writes is held to their guards
    first, and a rename that could have lost guards counts as an error. ``unchecked`` says the question's own draft
    didn't parse: a proposal with an agent then counts an error, as that draft's guards can't be checked, even when
    other drafts came with the question.
    """
    checked = []
    alerts: list[str] = []
    notes: list[str] = []
    for given in reply.proposals:
        proposal, unresolved, raised, lines = _guarded(given, drafts, unchecked)
        alerts += [line for line in raised if line not in alerts]
        notes += [line for line in lines if line not in notes]
        if proposal is None:
            continue
        result = validate_authoring(proposal.source, format=proposal.format)
        errors = sum(1 for item in result.diagnostics if item.severity == "error")
        try:
            document = parse_source(proposal.source, format=proposal.format).value
        except Exception:
            document = None
        if errors == 0 and (not isinstance(document, dict) or document.get("kind") != PROPOSAL_KINDS[proposal.kind]):
            errors = 1
        checked.append(LumiCheckedProposal(**proposal.model_dump(), diagnostics=errors + unresolved))
    answer = _with_notes(reply.answer, alerts, notes)
    return LumiAnswer.model_validate({"answer": answer, "proposals": checked, "followUps": reply.follow_ups})


def _drafts(request: LumiAskRequest, resources: Sequence[JsonValue]) -> tuple[list[JsonValue], bool]:
    """The workflow documents a question carried, its inline draft then each attached draft; and whether its inline
    draft didn't parse."""
    found: list[JsonValue] = []
    parsed = request.draft is None
    if request.draft is not None:
        # A draft that doesn't parse can't say which guards its agents have.
        with contextlib.suppress(Exception):
            found.append(parse_source(request.draft.source, format=request.draft.format).value)
            parsed = True
    for resource in resources:
        data = resource.get("data") if isinstance(resource, dict) and resource.get("kind") == "draft" else None
        if isinstance(data, dict) and "document" in data:
            found.append(data["document"])
    return found, not parsed


def _answer_limit(config: LumiConfiguration, profile: Profile) -> LumiConfiguration:
    """The configuration the gateway receives: the profile's answer limit replaces a larger configured one."""
    limit = output_tokens(profile, config.profile.options.max_tokens)
    if limit == config.profile.options.max_tokens:
        return config
    options = config.profile.options.model_copy(update={"max_tokens": limit})
    return config.model_copy(update={"profile": config.profile.model_copy(update={"options": options})})


# Pages of 200 step facts read for a run attachment: the latest steps of runs up to 10,000 instances.
RUN_PAGES = 50


@service
class LumiService:
    def __init__(
        self,
        connections: ConnectionService,
        history: HistoryService,
        debug: DebugService,
        gateway: LumiGatewayClient,
        deployments: DeploymentService,
        policy: APIAIPolicy,
        runs: RunViewService,
    ) -> None:
        self.connections, self.history, self.debug, self.gateway = connections, history, debug, gateway
        self.definitions = connections.definitions
        self.deployments = deployments
        self.policy, self.runs = policy, runs

    async def _require(self, tx: Transaction, actor: Principal, capability: str, context: AuditContext) -> Principal:
        if tx.scope.environment_id is None:
            raise CatalogError(422, "WV-SCOPE", "Weave AI requires an environment")
        current = await load_principal(tx.session, actor.id)
        self.definitions.require(current, tx.scope, capability, context)
        return current

    @staticmethod
    async def _read(tx: Transaction) -> LumiConfiguration | None:
        payload = await tx.session.scalar(
            text("SELECT payload FROM lumi_configurations WHERE tenant_id=:t AND project_id=:p AND environment_id=:e"),
            {"t": tx.scope.tenant_id, "p": tx.scope.project_id, "e": tx.scope.environment_id},
        )
        return LumiConfiguration.model_validate_json(json.dumps(payload)) if payload is not None else None

    async def configuration(self, actor: Principal, scope: Scope, *, context: AuditContext) -> LumiConfiguration:
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            await self._require(tx, actor, "lumi.manage", context)
            result = await self._read(tx)
            if result is None:
                raise CatalogError(404, "WV-NOT-FOUND", "Weave AI is not configured")
            return result

    async def status(self, actor: Principal, scope: Scope, *, context: AuditContext) -> LumiStatus:
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            await self._require(tx, actor, "lumi.use", context)
            config = await self._read(tx)
            if config is None or not config.enabled or not self.gateway.configured:
                return LumiStatus(configured=False)
            return LumiStatus(
                configured=True, provider=config.profile.provider, model=config.profile.model, revision=config.revision
            )

    async def configure(
        self,
        actor: Principal,
        scope: Scope,
        request: LumiConfigurationRequest,
        expected: int | None,
        *,
        context: AuditContext,
    ) -> LumiConfiguration:
        async with self.definitions.transaction(scope, None) as tx:
            actor = await self._require(tx, actor, "lumi.manage", context)
            prior = await self._read(tx)
            if (prior.revision if prior else None) != expected:
                raise CatalogError(409, "WV-LUMI-REVISION", "Weave AI configuration changed; reload it")
            connection = await self.connections.read(
                actor, scope, request.connection_revision_id, context=context, tx=tx
            )
            self._connection(scope, request, connection)
            result = LumiConfiguration(**request.model_dump(), revision=1 if prior is None else prior.revision + 1)
            await tx.session.execute(
                text(
                    "INSERT INTO lumi_configurations(tenant_id,project_id,environment_id,revision,payload) "
                    "VALUES(:t,:p,:e,:r,cast(:v AS jsonb)) ON CONFLICT(tenant_id,project_id,environment_id) "
                    "DO UPDATE SET revision=excluded.revision,payload=excluded.payload"
                ),
                {
                    "t": scope.tenant_id,
                    "p": scope.project_id,
                    "e": scope.environment_id,
                    "r": result.revision,
                    "v": result.model_dump_json(by_alias=True),
                },
            )
            await audit(
                tx.session,
                actor,
                "lumi.configure",
                str(scope.environment_id),
                scope=scope,
                capability="lumi.manage",
                context=context,
                details={"revision": result.revision},
            )
            return result

    def _connection(
        self, scope: Scope, configuration: LumiConfigurationRequest, connection: ConnectionRevision
    ) -> str | None:
        """The connection's secret handle, or None for a keyless local model endpoint, which sends no credential."""
        if (
            connection.connector != CONNECTOR_REFERENCE
            or connection.connector_digest != AGENTIC_DESCRIPTOR.manifest.digest
            or connection.config.get("provider") != configuration.profile.provider
        ):
            raise CatalogError(422, "WV-LUMI-CONNECTION", "Weave AI needs its configured provider connection")
        validate_connection(connection)
        if keyless_connection(connection.adapter, connection.config, connection.secret_refs):
            # The reserved no-credential handle is never checked, resolved or sent.
            return None
        handle = connection.secret_refs["apiKey"]
        self.connections.secrets.check(scope, handle)
        return handle

    async def _attachments(
        self, actor: Principal, scope: Scope, request: LumiAskRequest, context: AuditContext
    ) -> list[JsonValue]:
        """The attached resources, read with the caller's own access; the inline draft is shaped separately."""
        result: list[JsonValue] = []
        project = scope.model_copy(update={"environment_id": None})
        for attachment in request.attachments:
            data: JsonValue
            if attachment.kind in COLLECTIONS:
                record = await self.deployments.read(
                    COLLECTIONS[attachment.kind], attachment.id, actor=actor, scope=scope, context=context
                )
                data = deployment_context(attachment.kind, record)
            elif attachment.kind == "draft":
                value = await self.definitions.read(actor, project, "drafts", attachment.id, context=context)
                data = cast(JsonValue, {"document": value["document"], "revision": value["revision"]})
            elif attachment.kind == "run":
                data = await self._run(actor, scope, attachment.id, context)
            else:
                async with self.definitions.transaction(project, None, mutation=False) as tx:
                    session = await self.debug.inspect(tx, attachment.id, actor=actor, scope=project, context=context)
                    data = {
                        "status": session.view.status,
                        "current_nodes": list(session.view.current_nodes),
                        "diagnostics": [item.model_dump(mode="json") for item in session.view.diagnostics],
                    }
            result.append({"kind": attachment.kind, "id": str(attachment.id), "data": data})
        measure_value(result)
        size = len(json.dumps(result, ensure_ascii=False).encode())
        if size + (len(request.draft.source.encode()) if request.draft else 0) > 262144:
            raise CatalogError(413, "WV-LUMI-LIMIT", "Selected context is too large")
        return result

    async def _run(self, actor: Principal, scope: Scope, run_id: UUID, context: AuditContext) -> JsonValue:
        """A run's facts through the allowlisted builder; the run's own read checks `run.read` each time."""
        summary = await self.runs.summary(actor, scope, run_id, context=context)
        if summary is None:
            return {"status": "not_recorded_yet"}
        if not isinstance(summary, RunSummary):
            # Classified or blocked by policy for this caller: say so, and read none of its steps.
            return {"status": "unavailable"}
        facts: list[StepFact] = []
        position: tuple[JsonValue, str] | None = None
        for _ in range(RUN_PAGES):
            page = await self.runs.steps(actor, scope, run_id, RunStepQuery(limit=200), position, context=context)
            facts.extend(item for item in page.items if isinstance(item, StepFact))
            if page.next_cursor is None or not page.items:
                return run_context(summary, facts)
            # The next page starts after the last item, as the step cursor does: its scheduled time and key.
            last = page.items[-1]
            position = (last.scheduled_at.isoformat() if last.scheduled_at else None, last.instance_key or last.node_id)
        return run_context(summary, facts, partial=True)

    def _context_tokens(self, connection: ConnectionRevision) -> int | None:
        """The context size the AI policy records for the connection's endpoint, or None when it records none."""
        policy = self.policy.current()
        endpoint = connection.config.get("endpoint")
        entry = policy.entry_for(endpoint) if policy is not None and isinstance(endpoint, str) else None
        if entry is None:
            return None
        if entry.context_tokens is None and entry.compat == "ollama":
            # Ollama runs a model with a small context unless told otherwise; never size for an unbounded one.
            return DEFAULT_CONTEXT_TOKENS
        return entry.context_tokens

    def _shape(
        self, config: LumiConfiguration, request: LumiAskRequest, resources: list[JsonValue], tokens: int | None
    ) -> Shaped:
        profile = choose_profile(tokens, config.context_profile)
        schemas = cast(dict[str, JsonValue], export_schemas())
        return shape(
            profile,
            input_budget(profile, tokens, output_tokens(profile, config.profile.options.max_tokens)),
            message=request.message,
            history=request.history,
            resources=resources,
            draft=(request.draft.format, request.draft.source) if request.draft else None,
            selected_step=request.selected_step,
            schemas=schemas,
            explanation_only=request.explanation_only,
            fix=request.fix,
        )

    async def ask(
        self, actor: Principal, scope: Scope, request: LumiAskRequest, *, context: AuditContext
    ) -> LumiAnswer:
        async with self.definitions.transaction(scope, None, mutation=False) as tx:
            actor = await self._require(tx, actor, "lumi.use", context)
            config = await self._read(tx)
            if config is None or not config.enabled or not self.gateway.configured:
                raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is not configured")
            connection = await ConnectionRepository(tx).revision(config.connection_revision_id)
            handle = self._connection(scope, config, connection)
        # No database transaction is held across secret resolution or model I/O.
        try:
            async with asyncio.timeout(config.profile.timeout_seconds):
                tokens = self._context_tokens(connection)
                shaped = self._shape(config, request, await self._attachments(actor, scope, request, context), tokens)
                credential = None
                if handle is not None:
                    secret = await resolve_secret(lambda: self.connections.secrets.resolve(scope, handle))
                    credential = secret.value
                reply = await self.gateway.ask(
                    _answer_limit(config, shaped.profile),
                    request.model_copy(update={"history": shaped.history}),
                    shaped.context,
                    dict(connection.config),
                    credential,
                    context_tokens=tokens,
                )
                async with self.definitions.transaction(scope, None, mutation=False) as tx:
                    actor = await self._require(tx, actor, "lumi.use", context)
                    current = await self._read(tx)
                    if current is None or current.revision != config.revision or not current.enabled:
                        raise CatalogError(409, "WV-LUMI-REVISION", "Weave AI configuration changed; reply discarded")
                # Recheck attachment permissions and simulation ownership before releasing output.
                resources = await self._attachments(actor, scope, request, context)
                if request.explanation_only:
                    reply = reply.model_copy(update={"proposals": []})
                drafts, unchecked = _drafts(request, resources)
                return await asyncio.to_thread(check_proposals, reply, drafts, unchecked)
        except TimeoutError:
            raise CatalogError(504, "WV-LUMI-TIMEOUT", "Weave AI did not respond in time") from None
        except SecretUnavailable:
            raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is unavailable") from None
