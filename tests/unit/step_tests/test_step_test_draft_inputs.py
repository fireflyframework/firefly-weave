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
from typing import get_args, get_origin, get_type_hints

import pytest
from pydantic import BaseModel

from firefly_weave.compiler.agents import expression_references
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.calls import workflow_steps
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.platform_catalog import platform_schema_bundle
from firefly_weave.contracts import agentic
from firefly_weave.contracts.agent_turns import turn_action_definition, turn_capability
from firefly_weave.contracts.definitions import Expression, ObjectExpression, Step, load_definition
from firefly_weave.contracts.step_tests import DraftStep
from firefly_weave.step_tests import inputs
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
            "timeoutSeconds": 60,
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


@pytest.fixture
def compiled(monkeypatch):
    """The steps of the part of a draft that its check compiles, by their place in the draft."""
    parts = []

    def recorded(source, **settings):
        parts.append(copy.deepcopy(source))
        return compile_source(source, **settings)

    monkeypatch.setattr(inputs, "compile_source", recorded)

    def steps(document, **settings):
        refusal(document, **settings)
        return {path: step for step, path in workflow_steps(parts.pop())}

    return steps


def emptied(steps):
    """The IDs of the steps that a step which does nothing stands in for."""
    return {step["id"] for step in steps.values() if step == transform(step["id"], NULL)}


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
def test_a_value_marked_secret_is_found_on_each_route(route):
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


@pytest.mark.parametrize(
    "before, text",
    [
        ([], {"context": ref("/input/customer/ssn")}),
        ([transform("copy", ref("/input/customer/ssn"))], {"prompt": ref("/steps/copy/output")}),
    ],
    ids=["directly", "through a transform"],
)
@pytest.mark.parametrize("earlier", [ask, agent], ids=["a task", "an agent"])
def test_an_earlier_ai_step_counts_only_when_the_texts_read_its_output(earlier, before, text):
    reads = earlier(id="read", **text)
    marked = {"mark": MARKS[0]}
    assert refusal(workflow(*before, reads, ask(), **marked)) is None
    refused = refusal(workflow(*before, reads, ask(context=ref("/steps/read/output/result")), **marked))
    assert (f"/spec/steps/{len(before)}/{next(iter(text))}", SECRET_INPUT) in found(refused)


@pytest.mark.parametrize(
    "unread, text",
    [
        ({"tools": [{**agent()["tools"][0], "fixed": {"object": {"id": ref("/steps/copy/output")}}}]}, "tools/0/fixed"),
        ({"memory": {"scope": "conversation", "key": ref("/steps/copy/output")}}, "memory/key"),
    ],
    ids=["a tool's fixed value", "a conversation's key"],
)
def test_whatever_an_agent_that_is_read_reads_keeps_its_mark(unread, text):
    steps = [transform("copy", ref("/input/customer/ssn")), agent(id="read", **unread)]
    document = workflow(*steps, ask(context=ref("/steps/read/output/result")), mark=MARKS[0])
    assert any(
        path.startswith("/spec/steps/1/" + text) and code == SECRET_INPUT for path, code in found(refusal(document))
    )


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
    "a kind that isn't text": lambda: {"id": "x", "kind": ["switch"], "cases": [block(transform("y"))]},
    "tools that aren't": lambda: agent(id="x", tools=[None]),
    "an undo that isn't one": lambda: {**fetch(id="x"), "compensate": 3},
    "a person's task for nobody": lambda: {"id": "x", "kind": "humanTask", **READY["humanTask"], "assignment": ""},
    "a decision without a condition": lambda: {"id": "x", "kind": "switch", "cases": [block()], "default": block()},
    "an unfinished step in an error path": lambda: {
        **fetch(id="x"),
        "onError": "errorOutput",
        "errorPath": block({"id": "y", "kind": "action"}, output=ref("/steps/y/output")),
    },
    "a decision without a list of cases": lambda: {"id": "x", "kind": "switch", "cases": "none", "default": 3},
    "a decision whose case isn't one": lambda: {"id": "x", "kind": "switch", "cases": [None], "default": {}},
    "branches that aren't": lambda: {"id": "x", "kind": "parallel", "branches": [1], "concurrency": 1},
    "a loop without a body": lambda: {"id": "x", "kind": "forEach", "items": NULL, "body": "none"},
    "an error path that isn't one": lambda: {**fetch(id="x"), "errorPath": {"steps": [1]}, "onError": "errorOutput"},
}
# What Studio adds for a new step; those a person still has to finish.
NEW = {
    "action": {"uses": "", "with": {"literal": {}}},
    "callWorkflow": {"uses": "", "with": {"object": {}}, "mode": "wait"},
    "decisionTable": {"uses": "", "with": ref("/input")},
    "forEach": {"concurrency": 1, "maxItems": 1000, "collect": "all", "body": block(output=ref("/item"))},
    "llm": {
        "uses": "weave-agentic-generate@1.0.0",
        "profile": "default",
        "connection": "ai",
        "prompt": {"literal": ""},
        "context": {"literal": {}},
    },
    "signal": {"name": "", "timeoutSeconds": 3600, "payloadSchema": {"type": "object"}},
    "switch": {"cases": [block(output={"literal": {}})], "default": block(output={"literal": {}})},
}
# The same, finished as they are added.
READY = {
    "humanTask": {
        "title": {"literal": "Review request"},
        "context": {"literal": {}},
        "assignment": "reviewers",
        "formSchema": {"type": "object", "properties": {}},
        "decisions": ["approve", "reject"],
    },
    "parallel": {
        "branches": {"first": block(output={"literal": {}}), "second": block(output={"literal": {}})},
        "concurrency": 2,
    },
    "transform": {"value": {"literal": {}}},
    "wait": {"durationSeconds": 60},
}

# What no step can stand in for.
UNNAMED = {
    "a step that isn't one": lambda: None,
    "a step without an ID": lambda: {"kind": "wait", "durationSeconds": 5},
    "a step whose ID isn't text": lambda: {**transform("x"), "id": ["x"]},
    "a step whose ID isn't a name": lambda: transform("Not a name"),
    "a repeated step ID": lambda: transform("first"),
    "a failure without a message": lambda: {"id": "x", "kind": "fail", "code": "business-error", "message": ""},
}


@pytest.mark.parametrize("problem", BROKEN)
def test_a_step_broken_before_the_step_is_left_out_until_a_text_reads_it(problem):
    before = [transform("first"), BROKEN[problem]()]
    assert refusal(workflow(*before, ask(prompt=ref("/input/customer/ssn")))) is None
    marked = refusal(workflow(*before, ask(prompt=ref("/input/customer/ssn")), mark=MARKS[0]))
    assert found(marked) == [("/spec/steps/2/prompt", SECRET_INPUT)]
    assert found(refusal(workflow(*before, ask(context=ref("/steps/x/output")))))
    through = transform("copy", {"object": {"value": ref("/steps/x/output")}})
    assert found(refusal(workflow(*before, through, ask(context=ref("/steps/copy/output")))))


@pytest.mark.parametrize("problem", UNNAMED)
def test_a_step_that_cant_be_left_out_is_refused(problem):
    assert found(refusal(workflow(transform("first"), UNNAMED[problem](), ask())))


@pytest.mark.parametrize("kind", NEW)
def test_a_new_step_is_left_out_until_a_text_reads_it(kind):
    new = {"id": "x", "kind": kind, **NEW[kind]}
    assert refusal(workflow(new, ask())) is None
    assert found(refusal(workflow(new, ask(context=ref("/steps/x/output")))))


@pytest.mark.parametrize("kind", READY)
def test_a_new_step_with_nothing_to_finish_may_be_read(kind):
    new = {"id": "x", "kind": kind, **READY[kind]}
    assert refusal(workflow(new, ask())) is None
    assert refusal(workflow(new, ask(context=ref("/steps/x/output")))) is None


def test_steps_of_one_id_stay_when_a_text_reads_it():
    copy_, text = transform("copy"), ask(context=ref("/steps/copy/output"))
    assert refusal(workflow(decide(copy_), ask())) is None
    for steps in ([decide(copy_), copy_], [copy_, decide(copy_)], [decide(copy_), {**decide(copy_), "id": "again"}]):
        assert "WV-COMP-DUPLICATE_ID" in codes(refusal(workflow(*steps, text)))


def test_a_step_that_stays_in_a_block_keeps_the_block(compiled):
    inside = decide(transform("first"), fetch(), output=ref("/steps/fetch/output"))
    steps = compiled(workflow(transform("before"), inside, ask(context=ref("/steps/fetch/output"))))
    assert emptied(steps) == {"before", "first"}
    assert steps["/spec/steps/1/cases/0/steps/1"] == fetch()


def test_a_failure_stays_where_it_is(compiled):
    stop = {"id": "stop", "kind": "fail", "code": "stopped", "message": "Stopped"}
    # The default never completes, so what it would give is never read.
    route = decide(transform("first"), output=NULL, default=block(stop, output=ref("/input/customer/gone")))
    document = workflow(route, ask(context=ref("/steps/route/output")))
    assert emptied(compiled(document)) == {"first"}
    assert refusal(document) is None


def test_the_blocks_around_the_step_are_cut_to_it():
    # A step that isn't one can't be left out where it stays.
    beside = {"steps": [None, {"id": "x", "kind": "action"}], "output": ref("/steps/x/output")}
    assert refusal(workflow({**split(), "branches": {"left": beside, "right": block(ask())}})) is None
    route = {**decide(), "cases": [{"when": {"literal": True}, **beside}], "default": block(ask())}
    assert refusal(workflow(route)) is None


def test_the_names_only_left_out_steps_use_are_left_out_too():
    unread = [ask(id="read", profile="unfinished"), fetch(connection="unfinished")]
    document = workflow(*unread, ask())
    document["spec"]["llmProfiles"]["unfinished"] = {"provider": "openai-responses", "model": ""}
    document["spec"]["connections"]["unfinished"] = {}
    assert refusal(document) is None
    for read in ("read", "fetch"):
        document["spec"]["steps"][-1] = ask(context=ref(f"/steps/{read}/output"))
        assert found(refusal(document))


@pytest.mark.parametrize("problem", [*BROKEN, *UNNAMED])
def test_a_draft_broken_after_the_step_still_tests_it_and_still_checks_its_texts(problem):
    steps = [transform("first"), ask(prompt=ref("/input/customer/ssn")), {**BROKEN, **UNNAMED}[problem]()]
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


def test_a_compile_that_leaves_errors_out_is_refused():
    def asked(count):
        """The text reads what the action declares; the action's own input reads what isn't there."""
        unfinished = {"object": {f"f{index}": ref(f"/input/customer/no{index}") for index in range(count)}}
        return workflow(fetch(**{"with": unfinished}), ask(context=ref("/steps/fetch/output/name")))

    assert refusal(asked(3)) is None
    assert found(refusal(asked(120)))


def test_a_compile_that_leaves_only_warnings_out_is_admitted():
    # The text reads a hundred and twenty steps that each leave a warning.
    steps = [transform(f"t{index}", ref("/input/note")) for index in range(120)]
    document = workflow(*steps, ask(context={"array": [ref(f"/steps/{step['id']}/output") for step in steps]}))
    document["spec"]["inputSchema"]["properties"]["note"] = TEXT
    whole = compile_source(document, format="object", catalog=catalog())
    assert whole.ok and whole.omitted_count
    assert refusal(document) is None


def test_a_value_marked_secret_counts_where_only_warnings_are_listed():
    def asked(count):
        """The text reads agents that each leave a warning, which is listed before any other finding."""
        steps = [agent(id=f"a{index}", tools=[], limits={"maxTotalTokens": 1}) for index in range(count)]
        texts = [ref(f"/steps/{step['id']}/output/turns") for step in steps]
        return workflow(*steps, ask(context={"array": [*texts, ref("/input/customer/ssn")]}), mark=MARKS[0])

    listed = compile_source(asked(101), format="object", catalog=catalog()).diagnostics
    assert SECRET_INPUT not in {diagnostic.code for diagnostic in listed}
    assert found(refusal(asked(101)))
    assert codes(refusal(asked(3))) == {SECRET_INPUT}


def test_a_refusal_names_at_most_twenty_findings():
    texts = {"array": [ref(f"/input/customer/no{index}") for index in range(30)]}
    assert len(found(refusal(workflow(ask(context=texts))))) == 20
    assert len(found(refusal(workflow(ask(context={"array": texts["array"][:19]}))))) == 19


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
    reads = {"with": {"object": {"id": ref("/steps/first/output/name")}}}
    document = workflow(fetch(id="first", **uses), fetch(**uses, **reads), ask(context=ref("/steps/fetch/output/name")))
    findings = compile_source(document, format="object", catalog=resources).diagnostics
    errors = [finding for finding in findings if finding.severity == "error"]
    assert [error.path for error in errors if error.path.startswith("/spec/steps/0/uses")]
    assert not [error.path for error in errors if error.path.startswith("/spec/steps/1/")]
    assert found(refusal(document, resources=resources))
    assert refusal(workflow(fetch(**uses), ask()), resources=resources) is None


# --- the steps that stay -------------------------------------------------------------------------------------------

READS = {
    **ROUTES,
    **{
        read: (lambda step, steps=steps: [*steps, step], pointer)
        for read, (steps, pointer) in UNRESOLVED.items()
        if steps
    },
}
EVERYTHING = {"every step", "everything"}
# The one step of those drafts that nothing reads, and the one that stays because it fails.
NOT_READ, FAILS = "first", "stop"
UNREAD = {"through a transaction": {NOT_READ}}


def read_by(read):
    """The draft whose text reads that way, and the IDs of the steps it reads or is in."""
    around, pointer = READS[read]
    document = workflow(*around(ask(context=ref(pointer))))
    ids = {step["id"] for step, _ in workflow_steps(document)}
    return document, ids - {"ask"} - UNREAD.get(read, set())


@pytest.mark.parametrize("read", READS)
def test_the_steps_the_texts_read_stay_and_the_others_are_emptied(read, compiled):
    document, stay = read_by(read)
    document["spec"]["steps"].insert(0, transform("beside"))
    steps = compiled(document)
    assert stay <= {step["id"] for step in steps.values()}
    assert emptied(steps) == (set() if read in EVERYTHING else {"beside"} | UNREAD.get(read, set()))


def first_walk_misses(monkeypatch, missed):
    """Make the walk that picks the steps to keep miss every read of one step, so that step is emptied."""
    stays = inputs._stays
    monkeypatch.setattr(
        inputs, "_stays", lambda steps, step_id: {at for at in stays(steps, step_id) if steps[at].get("id") != missed}
    )


@pytest.mark.parametrize(
    "read, missed", [(read, step) for read in READS for step in sorted(read_by(read)[1] - {FAILS})]
)
def test_a_step_that_was_emptied_and_is_read_after_all_is_refused(read, missed, monkeypatch):
    first_walk_misses(monkeypatch, missed)
    for mark in (None, MARKS[0]):
        document, _ = read_by(read)
        document["spec"]["inputSchema"] = workflow(mark=mark)["spec"]["inputSchema"]
        assert refusal(document, resources=catalog(mark)) is not None


@pytest.mark.parametrize(
    "context",
    [
        ref(""),
        ref("/steps"),
        ref("steps/fetch/output"),
        ref("/steps/fe~2tch/output"),
        {"ref": 1},
        {"bogus": 1},
        {"object": [ref("/steps/fetch/output")]},
        {"op": {"name": "coalesce"}},
        {"literal": 1, "ref": "/steps/fetch/output"},
        None,
    ],
    ids=["everything", "every step", "no pointer", "an escape that isn't one", "a pointer that isn't text", "no tag"]
    + ["an object that isn't one", "an operator without operands", "two tags", "nothing"],
)
@pytest.mark.parametrize("where", ["its own text", "a step it reads"])
def test_every_step_stays_where_what_is_read_cant_be_told(where, context, compiled):
    own = where == "its own text"
    read = [] if own else [transform("copy", {"literal": 1}) | {"value": context}]
    text = ask(context=context if own else ref("/steps/copy/output"))
    document = workflow(fetch(), decide(transform("inside")), *read, text)
    assert emptied(compiled(document)) == set()


TEXTS = {"op": {"name": "concat", "args": [ref("/steps/many/output/field")] * 101}}
TOLD = {"op": {"name": "eq", "args": [TEXTS, {"literal": "x"}]}}
FIND = {"id": "find", "kind": "callWorkflow", "uses": "find-customer@1.0.0", "with": {"object": {}}, "mode": "wait"}
PERSON = {"id": "person", "kind": "humanTask", **READY["humanTask"]}
FOUND = ref("/steps/find/output")
UNDO = {"kind": "callWorkflow", "uses": "find-customer@1.0.0", "with": {"object": {"id": TEXTS}}}
# Finished drafts where only this expression of a step that stays reads another step: (the steps, what the text reads).
ALSO_READ = {
    "an action's input": (lambda step: [fetch(**{"with": {"object": {"id": TEXTS}}}), step], "/steps/fetch/output"),
    "a table's input": (
        lambda step: [
            {
                "id": "pick",
                "kind": "decisionTable",
                "uses": "pick-customer@1.0.0",
                "with": {"object": {"name": TEXTS, "ssn": {"literal": "x"}}},
            },
            step,
        ],
        "/steps/pick/output",
    ),
    "a call's input": (lambda step: [{**FIND, "with": {"object": {"id": TEXTS}}}, step], "/steps/find/output"),
    "a call's business key": (lambda step: [{**FIND, "businessKey": TEXTS}, step], "/steps/find/output"),
    "a person's title": (lambda step: [{**PERSON, "title": TEXTS}, step], "/steps/person/output"),
    "a person's context": (
        lambda step: [{**PERSON, "context": {"object": {"id": TEXTS}}}, step],
        "/steps/person/output",
    ),
    "a decision's condition": (
        lambda step: [{**decide(), "cases": [{"when": TOLD, **block()}]}, step],
        "/steps/route/output",
    ),
    "a decision's other path": (
        lambda step: [decide(output={"literal": "x"}, default=block(output=TEXTS)), step],
        "/steps/route/output",
    ),
    "a transaction's business key": (
        lambda step: [transaction(transform("first"), businessKey=TEXTS), step],
        "/steps/move/output",
    ),
    "a confirmation": (
        lambda step: [
            transaction({**FIND, "confirm": UNDO, "cancel": {**UNDO, "with": {"object": {}}}}, output=FOUND),
            step,
        ],
        "/steps/move/output",
    ),
    "a cancellation": (
        lambda step: [
            transaction({**FIND, "confirm": {**UNDO, "with": {"object": {}}}, "cancel": UNDO}, output=FOUND),
            step,
        ],
        "/steps/move/output",
    ),
    "a compensation": (
        lambda step: [transaction({**FIND, "compensate": UNDO}, output=FOUND), step],
        "/steps/move/output",
    ),
    "an earlier AI task's text": (lambda step: [ask(id="read", context=TEXTS), step], "/steps/read/output"),
    "an earlier agent's text": (lambda step: [agent(id="read", instructions=TEXTS), step], "/steps/read/output"),
    "an earlier agent's conversation": (
        lambda step: [agent(id="read", memory={"scope": "conversation", "key": TEXTS}), step],
        "/steps/read/output",
    ),
    "an earlier agent's tool": (
        lambda step: [
            agent(id="read", tools=[{**agent()["tools"][0], "fixed": {"object": {"id": TEXTS}}}]),
            step,
        ],
        "/steps/read/output",
    ),
    "the condition around the step": (
        lambda step: [{**decide(), "cases": [{"when": TOLD, **block(step)}]}],
        "/input/customer/name",
    ),
    "the items around the step": (lambda step: [loop(step, items={"array": [TEXTS]})], "/input/customer/name"),
    "the input of the step whose error path it's in": (
        lambda step: [fetch(**{"with": {"object": {"id": TEXTS}}}, onError="errorOutput", errorPath=block(step))],
        "/input/customer/name",
    ),
}


@pytest.mark.parametrize("read", ALSO_READ)
def test_a_step_that_stays_keeps_every_step_it_reads(read):
    around, pointer = ALSO_READ[read]
    many = transform("many", {"object": {"field": {"literal": "x"}}})
    document = workflow(many, *around(ask(context=ref(pointer))))
    assert compile_source(document, format="object", catalog=catalog()).ok
    # Emptied, the step would leave more findings than a compile lists.
    assert refusal(document) is None


def swapped(node, old, new):
    """The node with every part equal to ``old`` replaced by ``new``."""
    if node == old:
        return copy.deepcopy(new)
    if isinstance(node, dict):
        return {key: swapped(value, old, new) for key, value in node.items()}
    if isinstance(node, list):
        return [swapped(value, old, new) for value in node]
    return node


UNFINISHED = {"id": "many", "kind": "action", **NEW["action"]}


# Where only a block's output reads the step, the check of what the texts read finds it emptied already.
@pytest.mark.parametrize("read", [read for read in ALSO_READ if read != "a decision's other path"])
def test_a_step_emptied_though_a_step_that_stays_reads_it_is_refused(read, monkeypatch):
    around, pointer = ALSO_READ[read]
    document = swapped(workflow(UNFINISHED, *around(ask(context=ref(pointer)))), TEXTS, ref("/steps/many/output"))
    assert found(refusal(document))
    first_walk_misses(monkeypatch, "many")
    assert refusal(document) is not None


@pytest.mark.parametrize(
    "earlier",
    [
        lambda value: ask(id="read", prompt=value),
        lambda value: agent(id="read", tools=[{**agent()["tools"][0], "fixed": {"object": {"id": value}}}]),
    ],
    ids=["an earlier AI task's prompt", "an earlier agent's tool"],
)
def test_a_marked_value_an_earlier_ai_step_reads_is_refused_when_the_first_walk_misses_it(earlier, monkeypatch):
    copied = transform("copy", ref("/input/customer/ssn"))
    document = workflow(copied, earlier(ref("/steps/copy/output")), ask(context=ref("/steps/read/output/result")))
    document["spec"]["inputSchema"] = workflow(mark=MARKS[0])["spec"]["inputSchema"]
    assert codes(refusal(document, resources=catalog(MARKS[0]))) == {SECRET_INPUT}
    first_walk_misses(monkeypatch, "copy")
    assert refusal(document, resources=catalog(MARKS[0])) is not None


@pytest.mark.parametrize(
    "pointer",
    ["/steps/first/output", "", "/steps"],
    ids=["the step", "everything", "every step"],
)
def test_a_read_in_a_field_the_first_walk_doesnt_follow_still_counts(pointer, monkeypatch):
    def reading(at):
        return workflow(
            transform("first"),
            fetch(**{"with": {"object": {"id": ref(at)}}}),
            ask(context=ref("/steps/fetch/output/name")),
        )

    monkeypatch.setattr(inputs, "EXPRESSIONS", tuple(name for name in inputs.EXPRESSIONS if name != "with"))
    assert refusal(reading(pointer)) is not None
    # What reads no step that was emptied is admitted as before.
    assert refusal(reading("/input/customer/name")) is None


def test_every_expression_of_a_step_is_followed():
    def within(annotation):
        """The types an annotation names, through aliases, unions and collections; a step is a step of its own."""
        pending, types = [annotation], []
        while pending:
            part = pending.pop()
            if part in types:
                continue
            types.append(part)
            if part is not Expression and part is not Step:
                pending.extend(get_args(part))
                pending.extend([part.__value__] if hasattr(part, "__value__") else [])
        return types

    marker = ref("/steps/marker/output")

    def holding(model, seen):
        """Each place in the model where an expression may sit, with a step as saved that holds ``marker`` there."""
        # Some fields name the models they hold before those are defined.
        hints = get_type_hints(model, include_extras=True)
        for name, field in model.model_fields.items():
            key, types = field.alias or name, within(hints[name])
            if Expression in types or ObjectExpression in types:
                yield (key,), {key: marker}
            origins = {get_origin(part) for part in types}
            for part in types:
                if isinstance(part, type) and issubclass(part, BaseModel) and part not in seen:
                    for place, inner in holding(part, seen | {part}):
                        held = [inner] if list in origins else {"one": inner} if dict in origins else inner
                        yield (key, *place), {key: held}

    places = set()
    for model in get_args(get_args(Step.__value__)[0]):
        for place, step in holding(model, frozenset({model, ObjectExpression})):
            places.add((model.__name__, *place))
            read = {
                pointer for expression in inputs._expressions(step) for pointer in expression_references(expression)
            }
            assert "/steps/marker/output" in read, (model.__name__, place)
    # The walk resolves the models a field names before they are defined, and goes into lists and maps.
    assert {("ActionStep", "errorPath", "output"), ("SwitchStep", "cases", "when")} <= places
    assert ("ParallelStep", "branches", "output") in places
    # Nothing the first walk follows is gone from the steps; an agent's memory has a name or an expression as its key.
    assert {place[-1] for place in places} == {*inputs.EXPRESSIONS, "key"}
    assert {place[1] for place in places if len(place) > 2} == {*inputs.PARTS, *inputs.LISTS_OF_PARTS, "memory"}


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


@pytest.mark.parametrize("pointer", ["/loops/each/item/ssn", "/loops", ""])
def test_a_text_in_a_loop_in_a_loop_needs_the_items_of_both(pointer):
    def nested(items):
        return workflow(
            fetch(**MISSING),
            loop(loop(ask(context=ref(pointer)), id="inner", items=ref("/input/customers")), items=items),
        )

    assert found(refusal(nested(ref("/steps/fetch/output/all"))))
    if pointer:
        assert refusal(nested(ref("/input/customers"))) is None


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
