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
"""A step test checks what an AI step's texts read in the saved draft like a compile, on the draft up to the step."""

import copy
from typing import get_args

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.calls import workflow_steps
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.platform_catalog import platform_schema_bundle
from firefly_weave.contracts import agentic
from firefly_weave.contracts.agent_turns import turn_action_definition, turn_capability
from firefly_weave.contracts.definitions import Step, load_definition
from firefly_weave.contracts.step_tests import DraftStep
from firefly_weave.step_tests.inputs import COMPUTED, DECLARED, draft_refusal

MARKS = [{"x-secret": True}, {"writeOnly": True}]
MARK_IDS = ["x-secret", "writeOnly"]
TEST_METADATA = {"name": "invoice-approval", "version": "1.3.0+test.0123456789ab"}
TEXT = {"type": "string", "minLength": 1}
PROFILE = {
    "provider": "openai-responses",
    "model": "fixture-model",
    "options": {"max_tokens": 500},
    "outputSchema": {"type": "string"},
}
NULL = {"literal": None}


def ref(pointer):
    return {"ref": pointer}


def customer(mark=None):
    """A customer whose ``ssn`` is marked and whose ``name`` never is."""
    return {
        "type": "object",
        "required": ["name", "ssn"],
        "properties": {"name": TEXT, "ssn": {**TEXT, **(mark or {})}},
        "additionalProperties": False,
    }


def catalog(mark=None, published=()):
    """The AI action, the agent's turn action, and an action, a table and a workflow that return a customer."""
    request = {"type": "object", "required": ["id"], "properties": {"id": TEXT}, "additionalProperties": False}
    connector = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Connector",
        "metadata": {"name": "crm-api", "version": "1.0.0"},
        "spec": {
            "adapter": "http",
            "configSchema": {},
            "authSchema": {},
            "compatibility": {"apiVersion": "weave/v1alpha1"},
            "limits": {"maxRequestBytes": 1000, "maxResponseBytes": 1000, "maxTimeoutSeconds": 60},
            "actions": {
                "get-customer": {
                    "inputSchema": request,
                    "outputSchema": customer(mark),
                    "sideEffect": "read_only",
                    "timeoutSeconds": 30,
                }
            },
        },
    }
    fetch = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "fetch-customer", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "connector", "uses": "crm-api@1.0.0", "action": "get-customer"},
            "inputSchema": request,
            "outputSchema": customer(mark),
            "sideEffect": "read_only",
            "timeoutSeconds": 30,
            "connection": {"connector": "crm-api@1.0.0"},
        },
    }
    table = {
        "apiVersion": "weave/v1alpha1",
        "kind": "DecisionTable",
        "metadata": {"name": "pick-customer", "version": "1.0.0"},
        "spec": {
            "inputSchema": customer(mark),
            "outputSchema": customer(mark),
            "hitPolicy": "first",
            "rules": [{"id": "only", "when": {"literal": True}, "output": ref("/input")}],
        },
    }
    callee = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "find-customer", "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": customer(mark),
            "steps": [],
            "output": ref("/input"),
            "callable": {},
        },
    }
    return CatalogSnapshot.from_definitions(
        [
            load_definition(agentic.action_definition()),
            load_definition(turn_action_definition()),
            load_definition(agentic.AGENTIC_DESCRIPTOR.manifest.value),
            load_definition(connector),
            load_definition(fetch),
            load_definition(table),
            load_definition(callee),
            *(load_definition(document) for document in published),
        ],
        tasks=[agentic.task_capability(), turn_capability()],
        adapters=["weave-agentic-provider", "http"],
    )


def ask(**changes):
    return {
        "id": "ask",
        "kind": "llm",
        "uses": "weave-agentic-generate@1.0.0",
        "profile": "assistant",
        "prompt": {"literal": "Summarize the customer"},
        "context": NULL,
        "connection": "model",
        **changes,
    }


def agent(**changes):
    return {
        "id": "ask",
        "kind": "agent",
        "uses": "weave-agentic-turn@1.0.0",
        "profile": "assistant",
        "instructions": {"literal": "Help the customer"},
        "prompt": {"literal": "Who is it?"},
        "connection": "model",
        "tools": [
            {
                "name": "fetch",
                "description": "Reads a customer",
                "action": "fetch-customer@1.0.0",
                "connection": "crm",
                "fixed": {"object": {"id": {"literal": "C-1"}}},
            }
        ],
        "memory": {"scope": "step"},
        "limits": {},
        **changes,
    }


def transform(step_id, value=None):
    return {"id": step_id, "kind": "transform", "value": value or {"literal": step_id}}


def fetch(**changes):
    return {
        "id": "fetch",
        "kind": "action",
        "uses": "fetch-customer@1.0.0",
        "with": {"object": {"id": {"literal": "C-1"}}},
        "connection": "crm",
        **changes,
    }


def block(*steps, output=NULL):
    return {"steps": list(steps), "output": output}


def loop(*steps, items=None, output=NULL, **changes):
    items = items or ref("/input/customers")
    return {"id": "each", "kind": "forEach", "items": items, "body": block(*steps, output=output), **changes}


def decide(*steps, output=NULL, default=None):
    case = {"when": {"literal": True}, **block(*steps, output=output)}
    return {"id": "route", "kind": "switch", "cases": [case], "default": default or block(output=output)}


def split(*steps, output=NULL, **branches):
    return {
        "id": "both",
        "kind": "parallel",
        "branches": {"left": block(*steps, output=output), "right": block(), **branches},
        "concurrency": 2,
    }


def transaction(*steps, output=NULL, **changes):
    return {"id": "move", "kind": "transaction", "steps": list(steps), "output": output, **changes}


def workflow(*steps, mark=None, **spec):
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "invoice-approval", "version": "1.3.0"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "required": ["customer", "customers"],
                "properties": {
                    "customer": customer(mark),
                    "customers": {"type": "array", "items": customer(mark)},
                },
            },
            "outputSchema": {},
            "connections": {
                "model": {"connector": "weave-agentic-provider@1.0.0"},
                "crm": {"connector": "crm-api@1.0.0"},
            },
            "llmProfiles": {"assistant": PROFILE},
            "steps": list(steps),
            "output": NULL,
            **spec,
        },
    }


def refusal(document, *, resources=None):
    step = next(step for step, _ in workflow_steps(document) if step.get("id") == "ask")
    before = copy.deepcopy(document)
    refused = draft_refusal(DraftStep(document, step), TEST_METADATA, resources or catalog())
    assert document == before
    return refused


def found(refused):
    assert refused is not None, "admitted"
    assert (refused.status, refused.code) == (422, "WV-STEPTEST-DRAFT")
    assert set(refused.result) == {"diagnostics"}
    return [(item["path"], item["code"]) for item in refused.result["diagnostics"]]


def codes(refused):
    return {code for _, code in found(refused)}


SECRET_INPUT = "WV-COMP-AI_SECRET_INPUT"


# --- values marked secret -----------------------------------------------------------------------------------------


def through_a_transform(step):
    return [transform("copy", {"object": {"value": ref("/input/customer/ssn")}}), step]


# How a text of the step reads the marked field: (the steps around the AI step, the pointer its text reads).
ROUTES = {
    "directly": (lambda step: [step], "/input/customer/ssn"),
    "through the whole input": (lambda step: [step], "/input/customer"),
    "through a transform": (through_a_transform, "/steps/copy/output/value"),
    "in a loop": (lambda step: [loop(step)], "/item/ssn"),
    "in a loop by its name": (lambda step: [loop(step)], "/loops/each/item/ssn"),
    "through a loop": (lambda step: [loop(output=ref("/item/ssn")), step], "/steps/each/output/0"),
    "through an action": (lambda step: [fetch(), step], "/steps/fetch/output/ssn"),
    "through a decision table": (
        lambda step: [
            {"id": "pick", "kind": "decisionTable", "uses": "pick-customer@1.0.0", "with": ref("/input/customer")},
            step,
        ],
        "/steps/pick/output/ssn",
    ),
    "through a called workflow": (
        lambda step: [
            {"id": "find", "kind": "callWorkflow", "uses": "find-customer@1.0.0", "with": {"object": {}}},
            step,
        ],
        "/steps/find/output/ssn",
    ),
    "through a transaction": (
        lambda step: [transaction(transform("first"), output=ref("/input/customer/ssn")), step],
        "/steps/move/output",
    ),
    "in a decision": (lambda step: [decide(step)], "/input/customer/ssn"),
    "through a decision": (lambda step: [decide(output=ref("/input/customer/ssn")), step], "/steps/route/output"),
    "in a branch": (lambda step: [split(step)], "/input/customer/ssn"),
    "in an error path": (
        lambda step: [fetch(onError="errorOutput", errorPath=block(step))],
        "/input/customer/ssn",
    ),
}


@pytest.mark.parametrize("mark", MARKS, ids=MARK_IDS)
@pytest.mark.parametrize("field", ["prompt", "context"])
@pytest.mark.parametrize("route", ["directly", "through a transform", "in a loop"])
def test_an_ai_task_whose_text_reads_a_value_marked_secret_is_refused(route, field, mark):
    marked_text_is_refused(route, field, mark)


@pytest.mark.parametrize("route", ROUTES)
def test_a_value_marked_secret_is_found_however_the_text_reads_it(route):
    marked_text_is_refused(route, "context", MARKS[0])


def marked_text_is_refused(route, field, mark):
    around, pointer = ROUTES[route]
    document = workflow(*around(ask(**{field: ref(pointer)})), mark=mark)
    refused = refusal(document, resources=catalog(mark))
    at = next(path for step, path in workflow_steps(document) if step["id"] == "ask")
    assert (f"{at}/{field}", SECRET_INPUT) in found(refused)
    assert "marked secret" in refused.message


@pytest.mark.parametrize("route", ROUTES)
def test_an_ai_task_whose_text_reads_the_same_way_without_a_mark_is_admitted(route):
    around, pointer = ROUTES[route]
    assert refusal(workflow(*around(ask(context=ref(pointer))))) is None


@pytest.mark.parametrize("mark", MARKS, ids=MARK_IDS)
@pytest.mark.parametrize(
    "field, value",
    [
        ("instructions", ref("/input/customer/ssn")),
        ("prompt", ref("/input/customer/ssn")),
        ("context", ref("/input/customer")),
        ("fixed", {"object": {"id": ref("/input/customer/ssn")}}),
    ],
    ids=["instructions", "prompt", "context", "fixed"],
)
def test_every_text_of_an_agent_is_checked_the_same_way(field, value, mark):
    step = agent()
    if field == "fixed":
        step["tools"][0]["fixed"] = value
    else:
        step[field] = value
    assert SECRET_INPUT in codes(refusal(workflow(step, mark=mark)))
    assert refusal(workflow(step)) is None


def test_an_agents_conversation_key_is_no_text_of_its_step_test():
    remembers = agent(memory={"scope": "conversation", "key": ref("/steps/fetch/output/name")})
    assert refusal(workflow(fetch(**MISSING), remembers)) is None
    assert found(refusal(workflow(fetch(**MISSING), agent(prompt=ref("/steps/fetch/output/name")))))


def test_an_ai_task_with_a_field_the_language_lacks_is_refused():
    assert found(refusal(workflow(ask(instructions=ref("/input/customer/ssn")), mark=MARKS[0])))


def test_a_marked_result_of_an_earlier_ai_task_is_refused():
    reader = {**PROFILE, "outputSchema": customer(MARKS[0])}
    document = workflow(
        ask(id="read", profile="reader"),
        ask(context=ref("/steps/read/output/result/ssn")),
        llmProfiles={"assistant": PROFILE, "reader": reader},
    )
    assert ("/spec/steps/1/context", SECRET_INPUT) in found(refusal(document))


def test_an_unmarked_field_beside_a_marked_one_is_admitted():
    assert refusal(workflow(ask(prompt=ref("/input/customer/name")), mark=MARKS[0])) is None


def test_the_refusal_names_paths_and_codes_and_no_values():
    document = workflow(ask(prompt=ref("/input/customer/ssn")), mark=MARKS[0])
    refused = refusal(document)
    assert refused.result == {"diagnostics": [{"path": "/spec/steps/0/prompt", "code": SECRET_INPUT}]}


# --- drafts that aren't finished ----------------------------------------------------------------------------------

BROKEN = {
    "an unfinished step": lambda: {"id": "x", "kind": "action"},
    "an unknown field": lambda: {**transform("x"), "bogus": 1},
    "an unknown kind": lambda: {"id": "x", "kind": "nope"},
    "a step that isn't one": lambda: None,
    "a decision without a list of cases": lambda: {"id": "x", "kind": "switch", "cases": "none", "default": 3},
    "a decision whose case isn't one": lambda: {"id": "x", "kind": "switch", "cases": [None], "default": {}},
    "branches that aren't": lambda: {"id": "x", "kind": "parallel", "branches": [1], "concurrency": 1},
    "a loop without a body": lambda: {"id": "x", "kind": "forEach", "items": NULL, "body": "none"},
    "an error path that isn't one": lambda: {**fetch(id="x"), "errorPath": {"steps": [1]}, "onError": "errorOutput"},
    "a repeated step ID": lambda: transform("first"),
}


@pytest.mark.parametrize("problem", BROKEN)
def test_a_draft_broken_before_the_step_is_refused(problem):
    steps = [transform("first"), BROKEN[problem](), ask(prompt=ref("/input/customer/ssn"))]
    assert found(refusal(workflow(*steps, mark=MARKS[0])))


@pytest.mark.parametrize("problem", BROKEN)
def test_a_draft_broken_after_the_step_still_tests_it_and_still_checks_its_texts(problem):
    steps = [transform("first"), ask(prompt=ref("/input/customer/ssn")), BROKEN[problem]()]
    assert refusal(workflow(*steps)) is None
    assert found(refusal(workflow(*steps, mark=MARKS[0]))) == [("/spec/steps/1/prompt", SECRET_INPUT)]


def test_a_draft_without_an_output_still_checks_the_step():
    document = workflow(ask(prompt=ref("/input/customer/ssn")), mark=MARKS[0])
    del document["spec"]["output"]
    assert found(refusal(document)) == [("/spec/steps/0/prompt", SECRET_INPUT)]
    unmarked = workflow(ask(prompt=ref("/input/customer/ssn")))
    del unmarked["spec"]["output"]
    assert refusal(unmarked) is None


def test_an_input_schema_that_cant_be_read_is_refused():
    document = workflow(ask(prompt=ref("/input/customer/ssn")), mark=MARKS[0])
    document["spec"]["inputSchema"]["properties"]["z"] = {"type": "nonsense"}
    assert found(refusal(document))


def test_a_draft_without_an_input_schema_is_refused():
    document = workflow(ask())
    del document["spec"]["inputSchema"]
    assert found(refusal(document))


def test_a_compile_that_leaves_findings_out_is_refused():
    few = [transform(f"t{index}", ref(f"/steps/no{index}/output")) for index in range(3)]
    many = [transform(f"t{index}", ref(f"/steps/no{index}/output")) for index in range(120)]
    # The texts read none of these steps; only what the compile left out refuses the second draft.
    assert refusal(workflow(*few, ask())) is None
    assert found(refusal(workflow(*many, ask())))


def test_a_compile_that_leaves_only_warnings_out_is_refused_too():
    def noted(count):
        document = workflow(*(transform(f"t{index}", ref("/input/note")) for index in range(count)), ask())
        document["spec"]["inputSchema"]["properties"]["note"] = TEXT
        return document

    assert compile_source(noted(120), format="object", catalog=catalog()).ok
    assert refusal(noted(3)) is None
    assert found(refusal(noted(120))) == []


@pytest.mark.parametrize(
    "change",
    [
        {"profile": "writer"},
        {"onError": "errorOutput", "errorPath": block({"id": "x", "kind": "action"})},
        {"onError": "errorOutput", "errorPath": block(transform("x", ref("/steps/later/output")))},
    ],
    ids=["another profile", "its error path isn't finished", "its error path reads a step that isn't there"],
)
def test_what_the_steps_texts_dont_read_may_be_unfinished(change):
    document = workflow(ask(**change), ask(id="after"))
    document["spec"]["llmProfiles"]["writer"] = PROFILE
    document["spec"]["llmProfiles"]["unfinished"] = {"provider": "openai-responses", "model": ""}
    document["spec"]["connections"]["unfinished"] = {}
    document["spec"]["eventTrigger"] = {}
    document["spec"]["callable"] = "unfinished"
    document["spec"]["timeoutSeconds"] = 0
    document["spec"]["outputSchema"] = {"type": "nonsense"}
    assert refusal(document) is None


def test_a_draft_whose_version_is_already_published_still_tests_its_step():
    published = workflow(transform("first"))
    document = workflow(ask())
    resources = catalog(published=[published])
    whole = compile_source(document, format="object", catalog=resources)
    assert "WV-COMP-IMMUTABLE_VERSION" in {diagnostic.code for diagnostic in whole.diagnostics}
    assert refusal(document, resources=resources) is None


# --- what the texts read resolves ---------------------------------------------------------------------------------

MISSING = {"uses": "fetch-customer@9.0.0"}
UNRESOLVED = {
    "an action": ([fetch(**MISSING)], "/steps/fetch/output/ssn"),
    "an action's whole output": ([fetch(**MISSING)], "/steps/fetch/output"),
    "an action behind a transform": (
        [fetch(**MISSING), transform("copy", {"object": {"steps": ref("/steps/fetch/output")}})],
        "/steps/copy/output",
    ),
    "an action behind two transforms": (
        [
            fetch(**MISSING),
            transform("one", {"object": {"literal": ref("/steps/fetch/output/ssn")}}),
            transform("two", {"array": [ref("/steps/one/output")]}),
        ],
        "/steps/two/output/0",
    ),
    "an action behind an operator": (
        [
            fetch(**MISSING),
            transform("copy", {"op": {"name": "coalesce", "args": [ref("/steps/fetch/output/ssn"), NULL]}}),
        ],
        "/steps/copy/output",
    ),
    "an action behind a loop's items": (
        [fetch(**MISSING), loop(items=ref("/steps/fetch/output/all"), output=ref("/item"))],
        "/steps/each/output",
    ),
    "an action in a loop": (
        [loop(fetch(**MISSING), output=ref("/steps/fetch/output"))],
        "/steps/each/output",
    ),
    "an action in a decision": (
        [decide(fetch(**MISSING), output=ref("/steps/fetch/output"), default=block(output=ref("/input/customer")))],
        "/steps/route/output",
    ),
    "an action in a branch": ([split(fetch(**MISSING), output=ref("/steps/fetch/output"))], "/steps/both/output/left"),
    "an action in an error path": (
        [
            fetch(
                id="first",
                onError="errorOutput",
                errorPath=block(fetch(**MISSING), output=ref("/steps/fetch/output")),
            )
        ],
        "/steps/first/output",
    ),
    "an action in a transaction": (
        [transaction(fetch(**MISSING), output=ref("/steps/fetch/output"))],
        "/steps/move/output",
    ),
    "a decision table": (
        [{"id": "pick", "kind": "decisionTable", "uses": "pick-customer@9.0.0", "with": {"object": {}}}],
        "/steps/pick/output",
    ),
    "a called workflow": (
        [{"id": "find", "kind": "callWorkflow", "uses": "find-customer@9.0.0", "with": {"object": {}}, "mode": "wait"}],
        "/steps/find/output",
    ),
    "an earlier AI task's profile": ([ask(id="read", profile="gone")], "/steps/read/output"),
    "a transform that reads a step that isn't there": (
        [transform("copy", ref("/steps/later/output"))],
        "/steps/copy/output",
    ),
    "a transform that reads a field that isn't there": (
        [transform("copy", ref("/input/customer/gone"))],
        "/steps/copy/output",
    ),
    "a decision whose result reads a field that isn't there": (
        [decide(output=ref("/input/customer/gone"))],
        "/steps/route/output",
    ),
    "a loop over what isn't a list": (
        [loop(items=ref("/input/customer/name"), output=ref("/item"))],
        "/steps/each/output",
    ),
    "a loop whose body never completes": (
        [
            loop(
                {"id": "stop", "kind": "fail", "code": "stopped", "message": "Stopped"},
                output=ref("/item"),
            )
        ],
        "/steps/each/output",
    ),
    "a step that isn't there": ([], "/steps/gone/output"),
    "a step after it": ([], "/steps/after/output"),
    "every step": ([fetch(**MISSING)], "/steps"),
    "everything": ([fetch(**MISSING)], ""),
}


@pytest.mark.parametrize("read", UNRESOLVED)
def test_a_text_that_reads_what_doesnt_resolve_is_refused(read):
    steps, pointer = UNRESOLVED[read]
    document = workflow(*steps, ask(context=ref(pointer)), transform("after"))
    refused = refusal(document)
    assert found(refused)
    assert "read" in refused.message and "secret" not in refused.message


@pytest.mark.parametrize("read", [key for key, (steps, _) in UNRESOLVED.items() if steps])
def test_a_step_that_doesnt_resolve_and_no_text_reads_is_left_for_the_compile(read):
    steps, _ = UNRESOLVED[read]
    assert refusal(workflow(*steps, ask(context=ref("/input/customer/name")))) is None


def test_an_action_that_doesnt_resolve_counts_at_every_step_that_runs_it():
    # An action whose schema can't be read is reported once, at the first step that runs it.
    unreadable = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "unreadable", "version": "1.0.0"},
        "spec": {
            "implementation": {"kind": "connector", "uses": "crm-api@1.0.0", "action": "get-customer"},
            "inputSchema": {"type": "object"},
            "outputSchema": {"$ref": "weave:gone"},
            "sideEffect": "read_only",
            "timeoutSeconds": 30,
            "connection": {"connector": "crm-api@1.0.0"},
        },
    }
    resources = catalog(published=[unreadable])
    uses = {"uses": "unreadable@1.0.0"}
    document = workflow(fetch(id="first", **uses), fetch(**uses), ask(context=ref("/steps/fetch/output/name")))
    errors = compile_source(document, format="object", catalog=resources).diagnostics
    assert [error.path for error in errors if error.path.startswith("/spec/steps/0/uses")]
    assert not [error.path for error in errors if error.path.startswith("/spec/steps/1/")]
    assert found(refusal(document, resources=resources))
    assert refusal(workflow(fetch(**uses), ask()), resources=resources) is None


def test_a_text_may_read_a_call_that_doesnt_wait():
    call = {"id": "find", "kind": "callWorkflow", "uses": "find-customer@1.0.0", "with": {"object": {}}}
    assert refusal(workflow({**call, "mode": "detach"}, ask(context=ref("/steps/find/output")))) is None


def test_a_catalog_that_replaces_a_platform_schema_is_refused():
    name = next(iter(platform_schema_bundle()))
    whole = catalog()
    replaced = CatalogSnapshot.from_definitions(
        [load_definition(agentic.action_definition()), load_definition(agentic.AGENTIC_DESCRIPTOR.manifest.value)],
        tasks=[agentic.task_capability()],
        adapters=["weave-agentic-provider"],
        schema_bundle={name: {"type": "object"}},
    )
    assert refusal(workflow(ask()), resources=whole) is None
    assert found(refusal(workflow(ask()), resources=replaced)) == [("", "WV-COMP-RESERVED_SCHEMA")]


@pytest.mark.parametrize("pointer", ["/item/ssn", "/loops/each/item/ssn", "/loops"])
def test_a_text_that_reads_the_loops_item_needs_the_loops_items(pointer):
    items = ref("/steps/fetch/output/all")
    inside = [fetch(**MISSING), loop(ask(context=ref(pointer)), items=items)]
    assert found(refusal(workflow(*inside)))
    beside = [fetch(**MISSING), loop(ask(context=ref("/input/customer/name")), items=items)]
    assert refusal(workflow(*beside)) is None
    copied = loop(transform("copy", ref(pointer)), ask(context=ref("/steps/copy/output")), items=items)
    assert found(refusal(workflow(fetch(**MISSING), copied)))


@pytest.mark.parametrize(
    "step, pointer",
    [
        (fetch(connection="gone"), "/steps/fetch/output/name"),
        (fetch(**{"with": {"object": {}}}), "/steps/fetch/output/name"),
        (
            {"id": "pick", "kind": "decisionTable", "uses": "pick-customer@1.0.0", "with": {"literal": 1}},
            "/steps/pick/output/name",
        ),
    ],
    ids=["no connection yet", "its input isn't finished", "its table's input isn't finished"],
)
def test_a_text_may_read_a_step_whose_output_is_declared_and_whose_rest_isnt_finished(step, pointer):
    document = workflow(step, ask(context=ref(pointer)))
    assert not compile_source(document, format="object", catalog=catalog()).ok
    assert refusal(document) is None
    marked = workflow(step, ask(context=ref(pointer.replace("name", "ssn"))))
    assert SECRET_INPUT in codes(refusal(marked, resources=catalog(MARKS[0])))


def test_a_step_whose_profile_the_draft_lacks_is_refused():
    assert found(refusal(workflow(ask(profile="gone")))) == [("/spec/steps/0/profile", "WV-COMP-LLM_PROFILE")]


def test_a_text_the_compile_refuses_is_refused():
    document = workflow(ask(prompt=ref("/input/customers")))
    assert found(refusal(document)) == [("/spec/steps/0/prompt", "WV-COMP-TYPE_MISMATCH")]


@pytest.mark.parametrize(
    "steps",
    [
        [ask()],
        [transform("first"), fetch(), ask(context=ref("/steps/fetch/output")), transform("after")],
        [loop(ask(context=ref("/item")))],
        [loop(loop(ask(context=ref("/loops/each/item")), id="inner", items=ref("/input/customers")))],
        [decide(transform("first"), ask(context=ref("/steps/first/output")))],
        [split(ask(context=ref("/input/customer")))],
        [transaction(transform("first"), ask(context=ref("/steps/first/output")))],
        [fetch(onError="errorOutput", errorPath=block(ask(context=ref("/steps/fetch/error"))))],
        [transaction(transform("first"), onError="errorOutput", errorPath=block(ask(context=ref("/input/customer"))))],
    ],
    ids=["alone", "in a sequence", "in a loop", "in a loop in a loop", "in a decision", "in a branch"]
    + ["in a transaction", "in an error path", "in a transaction's error path"],
)
def test_a_finished_step_is_admitted_wherever_it_is(steps):
    assert refusal(workflow(*steps)) is None


def test_every_kind_of_step_says_what_its_output_comes_from():
    union = get_args(Step.__value__)[0]
    kinds = {get_args(step.model_fields["kind"].annotation)[0] for step in get_args(union)}
    assert kinds == DECLARED.keys() | COMPUTED.keys() and not DECLARED.keys() & COMPUTED.keys()
