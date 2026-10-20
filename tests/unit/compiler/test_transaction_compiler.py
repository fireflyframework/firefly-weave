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


"""Compiling transactions: every publish-time rule at its field, the typing of undo phases, and the lowered IR."""

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from firefly_weave.compiler.api import compile_source, import_artifact, validate_authoring
from firefly_weave.compiler.catalog import CatalogResource, CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.ir import UnsupportedIR, WorkflowIR
from firefly_weave.compiler.language_support import COMPILED_FEATURES
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.language_features import ADVERTISED_FEATURES

FIXTURE = yaml.safe_load(Path("tests/fixtures/transactions/catalog.yaml").read_text())
CATALOG = CatalogSnapshot.from_definitions(
    [load_definition(item) for item in FIXTURE["definitions"]], tasks=FIXTURE["tasks"]
)
EXAMPLE = parse_source(Path("examples/language/move-between-systems.workflow.yaml").read_text(), format="yaml").value
BODY = "/spec/steps/0/steps"
INPUT = {"object": {"reference": {"ref": "/input/moveId"}}}
QUANTITY = {"object": {"quantity": {"ref": "/input/quantity"}, "reference": {"ref": "/input/moveId"}}}


def ref(pointer):
    return {"ref": pointer}


def action(identifier, uses, value=None, **fields):
    """An action step; a field set to None is left out."""
    step = {"id": identifier, "kind": "action", "uses": uses, "with": value or INPUT, **fields}
    return {name: field for name, field in step.items() if field is not None}


def undo(uses, value=None, kind="action", **fields):
    return {"kind": kind, "uses": uses, "with": value or INPUT, **fields}


def hold(identifier="hold", **fields):
    release = undo(
        "source-system.release-hold@1.0.0",
        {"object": {"reference": ref("/input/moveId"), "holdOperation": ref("/participant/operationKey")}},
    )
    confirm = undo(
        "source-system.confirm-hold@1.0.0", {"object": {"holdId": ref(f"/steps/{identifier}/output/holdId")}}
    )
    return action(
        identifier, "source-system.hold-quantity@1.0.0", QUANTITY, **({"confirm": confirm, "cancel": release} | fields)
    )


def add(identifier="add", **fields):
    reverse = undo(
        "target-system.remove-quantity@1.0.0",
        {"object": {"reference": ref("/input/moveId"), "addOperation": ref("/participant/operationKey")}},
    )
    return action(identifier, "target-system.add-quantity@1.0.0", QUANTITY, **({"compensate": reverse} | fields))


def submit(identifier="submit", **fields):
    return action(identifier, "orders.submit-order@1.0.0", irreversible=True, **fields)


def transaction(*steps, identifier="move", output=None, **fields):
    return {
        "id": identifier,
        "kind": "transaction",
        "steps": list(steps),
        "output": output or {"literal": None},
        **fields,
    }


def workflow(*steps, output=None):
    document = copy.deepcopy(EXAMPLE)
    document["spec"]["steps"] = list(steps)
    document["spec"]["output"] = output or {"literal": None}
    return document


def compiled(document):
    return compile_source(document, format="object", catalog=CATALOG)


def findings(result, *codes):
    return [
        (d.code.removeprefix("WV-COMP-"), d.path, d.severity)
        for d in result.diagnostics
        if not codes or d.code.removeprefix("WV-COMP-") in codes
    ]


def executable(result) -> dict:
    return json.loads(result.artifact.to_bytes())["executable"]


def test_the_transaction_feature_compiles_and_this_platform_does_not_run_it_yet():
    assert "flow.transaction" in COMPILED_FEATURES
    assert "flow.eventTrigger" not in COMPILED_FEATURES
    assert not {"flow.transaction", "flow.eventTrigger"} & set(ADVERTISED_FEATURES)


def test_the_example_compiles_to_a_transaction_block_with_its_participants():
    result = compiled(EXAMPLE)
    assert result.ok and findings(result) == []
    value = executable(result)
    assert (value["irVersion"], value["features"]) == ("weave/ir-v1alpha4", ["flow.transaction"])
    nodes = {node["id"]: node for node in value["graph"]["nodes"]}
    assert nodes["move"]["kind"] == "transaction" and nodes["move"]["target"] == "hold"
    assert nodes["move"]["policy"] == {
        "holdSeconds": 900,
        "undoRetry": {"maxAttempts": 10, "initialDelaySeconds": 2, "maxDelaySeconds": 300},
        "undoTimeoutSeconds": 86400,
    }
    assert nodes["@join:move"]["mode"] == "transaction"
    assert {(node, nodes[node]["txn"]["mode"]) for node in ("hold", "add", "submit")} == {
        ("hold", "reserve"),
        ("add", "compensate"),
        ("submit", "irreversible"),
    }
    assert nodes["hold"]["txn"]["holdSeconds"] == 600 and set(nodes["hold"]["txn"]["phases"]) == {"confirm", "cancel"}
    assert nodes["add"]["txn"]["phases"]["compensate"]["kind"] == "callWorkflow"
    assert nodes["submit"]["txn"] == {"owner": "move", "mode": "irreversible", "pivot": True}
    assert {"kind": "transaction", "source": "move", "target": "hold", "branch": "body"} in value["graph"]["edges"]
    references = {(item["kind"], item["reference"]) for item in value["dependencies"]}
    assert {("Action", "source-system.release-hold@1.0.0"), ("Workflow", "reverse-target-addition@1.0.0")} <= references
    guards = {(guard["path"], guard["purpose"]) for guard in value["guards"]}
    assert {(f"{BODY}/0/cancel/with", "undo_input"), (f"{BODY}/1/compensate/with", "call_input")} <= guards
    assert compiled(EXAMPLE).artifact.digest == result.artifact.digest


def test_a_platform_that_does_not_run_transactions_refuses_the_artifact():
    artifact = compiled(EXAMPLE).artifact
    with pytest.raises(UnsupportedIR) as refused:
        import_artifact(artifact.to_bytes())
    assert refused.value.missing == ("flow.transaction",)
    assert import_artifact(artifact.to_bytes(), features=["flow.transaction"]).digest == artifact.digest


def test_steps_after_a_transaction_read_its_output_never_its_steps():
    after = {"id": "report", "kind": "transform", "value": ref("/steps/move/output/orderId")}
    inside = {"id": "peek", "kind": "transform", "value": ref("/steps/add/output")}
    result = compiled(workflow(transaction(hold(), add(), submit(), output=ref("/steps/submit/output")), after, inside))
    assert findings(result) == [("UNAVAILABLE_REFERENCE", "/spec/steps/2/value/ref", "error")]


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        pytest.param(
            [hold(), action("add", "target-system.add-quantity@1.0.0", QUANTITY), submit()],
            [("TXN_UNDECLARED", f"{BODY}/1", "error")],
            id="undeclared",
        ),
        pytest.param(
            [
                hold(),
                add(compensate=undo("reverse-without-limit@1.0.0", {"object": {}}, kind="callWorkflow")),
                submit(),
            ],
            [("TXN_CALL_TIMEOUT", f"{BODY}/1/compensate/uses", "error")],
            id="call-timeout",
        ),
        pytest.param(
            [hold(holdSeconds=30), add(), submit()],
            [("TXN_HOLD_PIVOT", f"{BODY}/0/holdSeconds", "error")],
            id="hold-pivot",
        ),
        pytest.param([hold(), submit(), add()], [("TXN_ORDER", f"{BODY}/2/compensate", "error")], id="order"),
        pytest.param(
            [
                hold(),
                {
                    "id": "each",
                    "kind": "forEach",
                    "items": {"literal": [1, 2]},
                    "maxItems": 2,
                    "body": {"steps": [submit()], "output": {"literal": None}},
                },
            ],
            [("TXN_PIVOT_PLACEMENT", f"{BODY}/1/body/steps/0", "error")],
            id="pivot-in-loop",
        ),
        pytest.param(
            [hold(), add(compensate=undo("orders.submit-order@1.0.0")), submit()],
            [("TXN_UNDO_SIDE_EFFECT", f"{BODY}/1/compensate/uses", "error")],
            id="undo-side-effect",
        ),
        pytest.param(
            [
                hold(
                    cancel=undo(
                        "source-system.release-hold@1.0.0",
                        {
                            "object": {
                                "reference": ref("/steps/hold/output/holdId"),
                                "holdOperation": ref("/participant/operationKey"),
                            }
                        },
                    )
                ),
                add(),
                submit(),
            ],
            [("TXN_CANCEL_OUTPUT", f"{BODY}/0/cancel/with/object/reference/ref", "error")],
            id="cancel-output",
        ),
        pytest.param(
            [hold(), add(), submit(), action("notify", "orders.submit-order@1.0.0", irreversible=True)],
            [("TXN_AFTER_PIVOT_UNSAFE", f"{BODY}/3", "error")],
            id="after-pivot-unsafe",
        ),
        pytest.param(
            [hold(), add(), submit(), {"id": "note", "kind": "transform", "value": {"literal": 1}}],
            [("TXN_AFTER_PIVOT_PURE", f"{BODY}/3", "info")],
            id="after-pivot-pure",
        ),
        pytest.param(
            [hold(), {"id": "pause", "kind": "wait", "durationSeconds": 3600}, add(), submit()],
            [("TXN_HOLD_WAIT", f"{BODY}/1", "warning")],
            id="hold-wait",
        ),
        pytest.param([submit()], [("TXN_NOTHING_TO_UNDO", "/spec/steps/0", "warning")], id="nothing-to-undo"),
        pytest.param(
            [hold(), transaction(add(), identifier="inner"), submit()],
            [("TXN_NESTED", f"{BODY}/1/kind", "error")],
            id="nested",
        ),
        pytest.param(
            [
                hold(),
                {
                    "id": "later",
                    "kind": "callWorkflow",
                    "uses": "reverse-target-addition@1.0.0",
                    "with": INPUT,
                    "mode": "detach",
                },
                submit(),
            ],
            [("TXN_DETACH", f"{BODY}/1/mode", "error")],
            id="detach",
        ),
        pytest.param(
            [hold(), {"id": "note", "kind": "transform", "value": ref("/participant/operationKey")}, submit()],
            [("TXN_SCOPE", f"{BODY}/1/value/ref", "error")],
            id="scope",
        ),
    ],
)
def test_each_rule_is_reported_at_its_field(steps, expected):
    result = compiled(workflow(transaction(*steps)))
    codes = {code for code, _, _ in expected}
    assert findings(result, *codes) == expected
    assert result.ok is all(severity != "error" for _, _, severity in expected)


def test_the_ordering_edit_moves_the_step_that_cant_be_undone_last_and_then_compiles_clean():
    result = compiled(workflow(transaction(hold(), submit(), add())))
    [order] = [d for d in result.diagnostics if d.code == "WV-COMP-TXN_ORDER"]
    assert (
        order.message == "add can be undone but runs after submit, which can't. "
        "Steps that can't be undone must come last in a transaction."
    )
    assert order.related[0].path == f"{BODY}/1"
    assert order.suggested_edit is not None and order.suggested_edit.path == BODY
    fixed = workflow(transaction(hold(), submit(), add()))
    fixed["spec"]["steps"][0]["steps"] = order.suggested_edit.value
    assert compiled(fixed).ok and findings(compiled(fixed)) == []


def test_the_hold_edit_names_the_hold_that_outlasts_the_decision():
    result = compiled(workflow(transaction(hold(holdSeconds=60), add(), submit())))
    [finding] = [d for d in result.diagnostics if d.code == "WV-COMP-TXN_HOLD_PIVOT"]
    assert (
        finding.message == "Reservations are held for 1 minute, but submit may take up to 1 minute. "
        "Hold them for at least 1 minute 30 seconds."
    )
    assert (finding.suggested_edit.path, finding.suggested_edit.value) == (f"{BODY}/0/holdSeconds", 90)


@pytest.mark.parametrize(
    ("step", "key", "path"),
    [
        (hold(cancel=None), "TXN_RESERVE_PAIR", "/confirm"),
        (add(irreversible=True), "TXN_UNDO_FORMS", "/irreversible"),
        (add(holdSeconds=600), "TXN_RESERVE_PAIR", "/holdSeconds"),
        ({**hold(), "compensate": undo("target-system.remove-quantity@1.0.0")}, "TXN_UNDO_FORMS", "/compensate"),
        (
            {
                "id": "later",
                "kind": "callWorkflow",
                "uses": "reverse-target-addition@1.0.0",
                "with": INPUT,
                "mode": "detach",
                "irreversible": True,
            },
            "TXN_DETACH",
            "/mode",
        ),
    ],
)
def test_undo_declarations_the_model_rejects_are_reported_at_their_field(step, key, path):
    result = compiled(workflow(transaction(step, submit())))
    assert findings(result) == [(key, f"{BODY}/0{path}", "error")]


def test_undo_fields_outside_a_transaction_are_refused():
    result = compiled(workflow(add(), submit()))
    assert findings(result) == [
        ("TXN_OUTSIDE", "/spec/steps/0/compensate", "error"),
        ("TXN_OUTSIDE", "/spec/steps/1/irreversible", "error"),
    ]


def test_read_only_actions_and_steps_that_change_nothing_need_no_declaration():
    lookup = action("lookup", "reports.lookup@1.0.0")
    result = compiled(workflow(transaction(hold(), lookup, add(), submit())))
    assert result.ok and findings(result) == []


def test_undo_phases_read_the_steps_own_output_as_maybe_missing_and_their_own_roots():
    reverse = undo(
        "target-system.remove-quantity@1.0.0",
        {"object": {"reference": ref("/transaction/businessKey"), "addOperation": ref("/steps/add/output/id")}},
    )
    result = compiled(
        workflow(transaction(hold(), add(compensate=reverse), submit(), businessKey=ref("/input/moveId")))
    )
    # The business key may be null, and the step's own output is missing when its outcome is unknown.
    assert findings(result) == [
        ("REFERENCE_PRESENCE", f"{BODY}/1/compensate/with/object/addOperation", "warning"),
        ("UNKNOWN_COMPATIBILITY", f"{BODY}/1/compensate/with", "warning"),
    ]


def test_an_action_cannot_call_a_temporary_failure_definite():
    document = next(item for item in FIXTURE["definitions"] if item["metadata"]["name"] == "target-system.add-quantity")
    changed = copy.deepcopy(document)
    changed["metadata"]["version"] = "1.0.1"
    changed["spec"]["definiteFailures"] = ["VALIDATION", "TIMEOUT", "LLM_TIMEOUT"]
    result = compile_source(changed, format="object", catalog=CATALOG)
    assert findings(result) == [
        ("ACTION_DEFINITE_TRANSIENT", "/spec/definiteFailures/1", "error"),
        ("ACTION_DEFINITE_TRANSIENT", "/spec/definiteFailures/2", "error"),
    ]
    assert result.diagnostics[0].message == "TIMEOUT is a temporary failure, so it can't mean that nothing was applied."


def test_offline_authoring_reports_calls_that_change_data_but_waits_for_the_catalog_for_actions():
    lookup = action("lookup", "reports.lookup@1.0.0")
    unknown_call = {"id": "fetch", "kind": "callWorkflow", "uses": "reverse-target-addition@1.0.0", "with": INPUT}
    result = validate_authoring(workflow(transaction(hold(), lookup, unknown_call, submit())), format="object")
    assert [d.code for d in result.diagnostics if d.severity == "error"] == ["WV-COMP-TXN_UNDECLARED"]
    assert validate_authoring(EXAMPLE, format="object").validation_ok


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda nodes: nodes["submit"]["txn"].pop("pivot"), id="pivot-cleared"),
        pytest.param(lambda nodes: nodes["add"].pop("txn"), id="participant-undeclared"),
        pytest.param(lambda nodes: nodes["add"]["txn"].update(owner="other"), id="foreign-owner"),
        pytest.param(
            lambda nodes: nodes["add"]["txn"]["phases"]["compensate"].update(dependency="0" * 64), id="unknown-phase"
        ),
    ],
)
def test_an_artifact_that_breaks_the_rules_is_refused_at_import(mutation):
    value = executable(compiled(EXAMPLE))
    mutation({node["id"]: node for node in value["graph"]["nodes"]})
    with pytest.raises(ValidationError):
        WorkflowIR.model_validate(value)


def _redigest(value: dict, reference: str, kind: str, change) -> None:
    """Change a locked dependency document and every digest that points at it, as a forger would."""
    dependency = next(item for item in value["dependencies"] if item["reference"] == reference)
    change(dependency["document"])
    digest = CatalogResource(kind, reference, FrozenDocument.from_value(dependency["document"])).digest
    for node in value["graph"]["nodes"]:
        if node.get("dependency") == dependency["digest"]:
            node["dependency"] = digest
        for phase in node.get("txn", {}).get("phases", {}).values():
            if phase["dependency"] == dependency["digest"]:
                phase["dependency"] = digest
    dependency["digest"] = digest


def _example_nodes() -> tuple[dict, dict]:
    value = executable(compiled(EXAMPLE))
    return value, {node["id"]: node for node in value["graph"]["nodes"]}


def test_import_rechecks_that_reservations_outlast_the_step_that_cant_be_undone():
    value, nodes = _example_nodes()
    nodes["hold"]["txn"]["holdSeconds"] = 31
    with pytest.raises(ValidationError, match="publish-time rule"):
        WorkflowIR.model_validate(value)


def test_import_rechecks_that_undo_actions_are_safe_to_retry():
    value, _ = _example_nodes()
    _redigest(
        value,
        "source-system.release-hold@1.0.0",
        "Action",
        lambda document: document["spec"].update(sideEffect="non_idempotent"),
    )
    with pytest.raises(ValidationError, match="publish-time rule"):
        WorkflowIR.model_validate(value)


def test_import_refuses_an_undo_workflow_that_does_not_allow_this_caller():
    value, _ = _example_nodes()
    _redigest(
        value,
        "reverse-target-addition@1.0.0",
        "Workflow",
        lambda document: document["spec"].update(callable={"allowedCallers": ["someone-else"]}),
    )
    with pytest.raises(ValidationError, match="does not allow calls from this workflow"):
        WorkflowIR.model_validate(value)


def test_import_refuses_an_undo_action_bound_to_an_unknown_connection():
    value, nodes = _example_nodes()
    nodes["hold"]["txn"]["phases"]["cancel"]["connection"] = "nowhere"
    with pytest.raises(ValidationError, match="Unresolved or incompatible action connection"):
        WorkflowIR.model_validate(value)


def test_import_refuses_a_call_inside_a_transaction_that_says_nothing():
    reverse = undo(
        "target-system.remove-quantity@1.0.0",
        {"object": {"reference": ref("/input/moveId"), "addOperation": ref("/participant/operationKey")}},
    )
    call = {
        "id": "fetch",
        "kind": "callWorkflow",
        "uses": "reverse-target-addition@1.0.0",
        "with": {"object": {"reference": ref("/input/moveId"), "addOperation": ref("/input/moveId")}},
        "compensate": reverse,
    }
    result = compiled(workflow(transaction(hold(), call, add(), submit())))
    assert result.ok, findings(result)
    value = executable(result)
    next(node for node in value["graph"]["nodes"] if node["id"] == "fetch").pop("txn")
    with pytest.raises(ValidationError, match="must say how it is undone"):
        WorkflowIR.model_validate(value)


def test_an_artifact_with_the_step_that_cant_be_undone_moved_first_is_refused_at_import():
    value = executable(compiled(EXAMPLE))
    nodes = {node["id"]: node for node in value["graph"]["nodes"]}
    nodes["add"]["txn"] = {"owner": "move", "mode": "irreversible", "pivot": True}
    nodes["submit"]["txn"] = {"owner": "move", "mode": "compensate", "phases": nodes["hold"]["txn"]["phases"] | {}}
    with pytest.raises(ValidationError):
        WorkflowIR.model_validate(value)


def test_workflows_without_transactions_keep_their_artifacts():
    for path in sorted(Path("examples/language").glob("*.yaml")):
        if path.name == "move-between-systems.workflow.yaml":
            continue
        text = path.read_text()
        assert "transaction" not in json.dumps(validate_authoring(text, format="yaml").to_bytes().decode()), path


def _with_secret_release_input(catalog_definitions):
    """The catalog with the release action's ``reference`` input marked secret."""
    changed = copy.deepcopy(catalog_definitions)
    release = next(item for item in changed if item["metadata"]["name"] == "source-system.release-hold")
    release["spec"]["inputSchema"]["properties"]["reference"]["x-secret"] = True
    return changed


def test_an_undo_step_never_binds_a_literal_to_a_secret_input():
    definitions = _with_secret_release_input(FIXTURE["definitions"])
    catalog = CatalogSnapshot.from_definitions([load_definition(item) for item in definitions], tasks=FIXTURE["tasks"])
    literal = undo(
        "source-system.release-hold@1.0.0",
        {"object": {"reference": {"literal": "M-1"}, "holdOperation": ref("/participant/operationKey")}},
    )
    result = compile_source(
        workflow(transaction(hold(cancel=literal), add(), submit())), format="object", catalog=catalog
    )
    assert not result.ok and [d.code for d in result.diagnostics] == ["WV-SCHEMA-SECRET_VALUE"]


def test_steps_after_a_failing_path_still_say_how_they_are_undone():
    stop = {"id": "stop", "kind": "fail", "code": "move-refused", "message": "The move was refused."}
    undeclared = action("add", "target-system.add-quantity@1.0.0", QUANTITY)
    result = compiled(workflow(transaction(hold(), stop, undeclared)))
    assert findings(result, "TXN_UNDECLARED") == [("TXN_UNDECLARED", f"{BODY}/2", "error")]
    assert not result.ok and result.artifact is None
    declared = compiled(workflow(transaction(hold(), stop, add(), submit())))
    assert declared.ok, findings(declared)
    assert WorkflowIR.model_validate(executable(declared)).features == ["flow.transaction"]


def test_a_cancel_that_reads_every_step_reads_its_own_step_too():
    whole = undo(
        "source-system.release-hold@1.0.0",
        {"object": {"reference": ref("/steps"), "holdOperation": ref("/participant/operationKey")}},
    )
    result = compiled(workflow(transaction(hold(cancel=whole), add(), submit())))
    assert findings(result, "TXN_CANCEL_OUTPUT") == [
        ("TXN_CANCEL_OUTPUT", f"{BODY}/0/cancel/with/object/reference/ref", "error")
    ]


def test_the_participant_budget_is_the_compiling_platforms_and_import_does_not_apply_it_again():
    from firefly_weave.contracts.limits import Limits

    each = {
        "id": "each",
        "kind": "forEach",
        "items": {"literal": [1]},
        "maxItems": 2000,
        "body": {"steps": [add()], "output": {"literal": None}},
    }
    document = workflow(transaction(hold(), each, submit()))
    assert findings(compiled(document), "TXN_SIZE") == [("TXN_SIZE", "/spec/steps/0", "error")]
    raised = compile_source(
        document, format="object", catalog=CATALOG, limits=Limits(max_transaction_participants=5000)
    )
    assert raised.ok, findings(raised)
    features = [*ADVERTISED_FEATURES, "flow.transaction"]
    assert import_artifact(raised.artifact.to_bytes(), features=features).digest == raised.artifact.digest


def test_workflows_called_to_undo_a_step_count_as_calls_for_depth_pins_and_side_effects():
    import asyncio
    from uuid import uuid4

    from firefly_weave.compiler.calls import CallGraph, called_references
    from firefly_weave.contracts.access import Scope
    from firefly_weave.definitions.calls import call_pins, call_tree_side_effect, called_dependencies, side_effect
    from firefly_weave.definitions.models import CatalogError

    assert called_references(EXAMPLE) == ["reverse-target-addition@1.0.0"]
    graph = CallGraph(CatalogSnapshot.from_definitions([load_definition(EXAMPLE)]))
    assert graph.calls("move-between-systems@1.0.0") == ("reverse-target-addition@1.0.0",)
    value = executable(compiled(EXAMPLE))
    assert set(called_dependencies(value)) == {"reverse-target-addition@1.0.0"}
    # Activation must pin the workflow an undo calls, like any called workflow.
    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())

    async def nothing(identifier):
        raise AssertionError("pins are compared with the calls before anything is loaded")

    with pytest.raises(CatalogError) as refused:
        asyncio.run(call_pins("move-between-systems@1.0.0", scope, value, {}, nothing))
    assert refused.value.code == "WV-READINESS-CALL"
    # Undo actions count toward the workflow's side effect, and so does what its undo workflows do.
    assert side_effect(value) == "non_idempotent"
    reverse = next(item for item in FIXTURE["definitions"] if item["metadata"]["name"] == "reverse-target-addition")
    callee = executable(compile_source(reverse, format="object", catalog=CATALOG))
    pin = uuid4()

    async def resolve(identifier):
        assert identifier == pin
        return callee, {}

    pins = {"reverse-target-addition@1.0.0": pin}
    assert asyncio.run(call_tree_side_effect(value, pins, resolve)) == "non_idempotent"


def test_import_refuses_a_forged_artifact_whose_undo_binds_a_literal_to_a_secret_input():
    from firefly_weave.compiler.canonical import canonical_digest
    from firefly_weave.compiler.catalog import CatalogResource, FrozenDocument

    literal = undo(
        "source-system.release-hold@1.0.0",
        {"object": {"reference": {"literal": "M-1"}, "holdOperation": ref("/participant/operationKey")}},
    )
    artifact = compiled(workflow(transaction(hold(cancel=literal), add(), submit()))).artifact
    assert import_artifact(artifact.to_bytes(), features=["flow.transaction"]).digest == artifact.digest
    value = json.loads(artifact.to_bytes())
    executable_value = value["executable"]
    dependency = next(
        d for d in executable_value["dependencies"] if d["reference"] == "source-system.release-hold@1.0.0"
    )
    dependency["document"]["spec"]["inputSchema"]["properties"]["reference"]["x-secret"] = True
    digest = CatalogResource(
        "Action", dependency["reference"], FrozenDocument.from_value(dependency["document"])
    ).digest
    for node in executable_value["graph"]["nodes"]:
        for phase in node.get("txn", {}).get("phases", {}).values():
            if phase["dependency"] == dependency["digest"]:
                phase["dependency"] = digest
    dependency["digest"] = digest
    value["digest"] = canonical_digest(executable_value)
    with pytest.raises(ValueError, match="classified or unclassifiable"):
        import_artifact(json.dumps(value), features=["flow.transaction"])
