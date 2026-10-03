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


"""Reusable decision policies share strict contracts and one bounded evaluator."""

import copy
import json

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.limits import Limits


def decision_table(policy="first", rules=None, **changes):
    spec = {
        "inputSchema": {"type": "object", "properties": {"amount": {"type": "number"}}},
        "outputSchema": {"type": "array", "items": {"type": "string"}} if policy == "collect" else {"type": "string"},
        "hitPolicy": policy,
        "rules": rules
        if rules is not None
        else [
            {
                "id": "large",
                "when": {"op": {"name": "gte", "args": [{"ref": "/input/amount"}, {"literal": 100}]}},
                "output": {"literal": "review"},
            },
            {"id": "small", "when": {"literal": True}, "output": {"literal": "approve"}},
        ],
    }
    spec.update(changes)
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "DecisionTable",
        "metadata": {"name": "payment-policy", "version": "1.0.0"},
        "spec": spec,
    }


def evaluate(document, value, **options):
    from firefly_weave.compiler.decision_tables import evaluate_decision_table

    return evaluate_decision_table(document["spec"], value, **options)


def test_definition_contract_accepts_versioned_reusable_decision_table():
    document = decision_table()
    definition = load_definition(document)
    assert definition.kind == "DecisionTable"
    assert definition.model_dump(by_alias=True) == document


def test_first_returns_first_match_without_evaluating_later_predicates():
    document = decision_table(
        rules=[
            {"id": "first", "when": {"literal": True}, "output": {"ref": "/input/result"}},
            {"id": "later", "when": {"ref": "/input/missing"}, "output": {"literal": "unused"}},
        ]
    )
    original = copy.deepcopy(document)
    result = evaluate(document, {"result": "approve"})
    assert result.output == "approve"
    assert result.matched_rule_ids == ("first",)
    assert not result.used_default
    assert document == original


def test_unique_reports_multiple_matches_without_exposing_input_values():
    with pytest.raises(ValueError, match="WV-DECISION-MULTIPLE_MATCHES") as error:
        evaluate(decision_table("unique"), {"amount": 500, "private": "never-in-errors"})
    assert "never-in-errors" not in str(error.value)
    assert error.value.path == "/spec/rules/1/when"
    assert evaluate(decision_table("unique"), {"amount": 50}).matched_rule_ids == ("small",)


def test_collect_returns_rows_in_source_order_and_empty_list_for_no_matches():
    result = evaluate(decision_table("collect"), {"amount": 200})
    assert result.output == ["review", "approve"]
    assert result.matched_rule_ids == ("large", "small")
    none = decision_table(
        "collect", rules=[{"id": "never", "when": {"literal": False}, "output": {"literal": "unused"}}]
    )
    assert evaluate(none, {}).output == []


@pytest.mark.parametrize("policy", ["first", "unique"])
def test_no_match_requires_explicit_default_or_reports_a_controlled_failure(policy):
    document = decision_table(
        policy, rules=[{"id": "never", "when": {"literal": False}, "output": {"literal": "unused"}}]
    )
    with pytest.raises(ValueError, match="WV-DECISION-NO_MATCH"):
        evaluate(document, {})
    document["spec"]["defaultOutput"] = {"literal": "manual"}
    result = evaluate(document, {})
    assert result.output == "manual"
    assert result.matched_rule_ids == ()
    assert result.used_default


@pytest.mark.parametrize(
    "change",
    [
        {"hitPolicy": "priority"},
        {"defaultOutput": None},
        {"rules": []},
        {"rules": [{"id": "same", "when": {"literal": True}, "output": {"literal": "x"}}] * 2},
    ],
)
def test_table_rejects_invalid_policy_null_default_and_duplicate_rule_ids(change):
    with pytest.raises(ValidationError):
        load_definition(decision_table(**change))


def test_collect_rejects_default_and_requires_a_homogeneous_array_output():
    for change in [
        {"defaultOutput": {"literal": []}},
        {"outputSchema": {"type": "string"}},
        {"outputSchema": {"type": "array", "items": {}, "prefixItems": [{}]}},
    ]:
        with pytest.raises(ValidationError):
            load_definition(decision_table("collect", **change))


def test_rule_bindings_only_read_input_and_literal_payloads_are_not_syntax():
    for ref in ["/steps/previous/output", "", "/inputs/amount"]:
        document = decision_table(rules=[{"id": "bad", "when": {"ref": ref}, "output": {"literal": "x"}}])
        with pytest.raises(ValueError, match="WV-DECISION-REFERENCE"):
            evaluate(document, {})
    document = decision_table(
        rules=[{"id": "ok", "when": {"literal": True}, "output": {"literal": {"ref": "/steps/ordinary-data/output"}}}],
        outputSchema={"type": "object"},
    )
    assert evaluate(document, {}).output == {"ref": "/steps/ordinary-data/output"}


def test_input_predicate_and_output_guards_fail_before_returning_untyped_values():
    with pytest.raises(ValueError, match="WV-DECISION-INPUT"):
        evaluate(decision_table(), {"amount": "a lot"})
    for expression in [{"literal": "yes"}, {"literal": 1}, {"literal": None}]:
        with pytest.raises(ValueError, match="WV-DECISION-PREDICATE"):
            evaluate(decision_table(rules=[{"id": "bad", "when": expression, "output": {"literal": "x"}}]), {})
    document = decision_table(rules=[{"id": "bad", "when": {"literal": True}, "output": {"literal": 3}}])
    with pytest.raises(ValueError, match="WV-DECISION-OUTPUT"):
        evaluate(document, {})


def test_expression_and_comparison_budgets_are_shared_across_the_whole_table():
    rules = [{"id": f"row-{index}", "when": {"literal": False}, "output": {"literal": "unused"}} for index in range(10)]
    with pytest.raises(ValueError, match="RESOURCE_LIMIT"):
        evaluate(decision_table("collect", rules), {}, limits=Limits(max_expression_nodes=12))
    rules = [
        {
            "id": f"row-{index}",
            "when": {"op": {"name": "contains", "args": [{"ref": "/input/text"}, {"literal": "x"}]}},
            "output": {"literal": "matched"},
        }
        for index in range(10)
    ]
    with pytest.raises(ValueError, match="RESOURCE_LIMIT"):
        evaluate(decision_table("collect", rules), {"text": "x" * 400}, limits=Limits(max_payload_bytes=1000))


def test_collect_charges_aggregate_output_and_never_leaks_classified_values():
    rules = [{"id": f"row-{index}", "when": {"literal": True}, "output": {"ref": "/input/text"}} for index in range(4)]
    with pytest.raises(ValueError, match="RESOURCE_LIMIT"):
        evaluate(decision_table("collect", rules), {"text": "x" * 250}, limits=Limits(max_payload_bytes=1000))
    document = decision_table(
        inputSchema={"type": "object", "properties": {"token": {"type": "string", "x-secret": True}}}
    )
    with pytest.raises(ValueError, match="WV-DECISION-INPUT") as error:
        evaluate(document, {"token": "secret-never-logged"})
    assert "secret-never-logged" not in str(error.value)


def compile_decision_workflow(table):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot

    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "payment", "version": "1.0.0"},
        "spec": {
            "inputSchema": table["spec"]["inputSchema"],
            "outputSchema": table["spec"]["outputSchema"],
            "steps": [
                {"id": "decide", "kind": "decisionTable", "uses": "payment-policy@1.0.0", "with": {"ref": "/input"}}
            ],
            "output": {"ref": "/steps/decide/output"},
        },
    }
    return compile_source(
        json.dumps(workflow), format="json", catalog=CatalogSnapshot.from_definitions([load_definition(table)])
    )


def test_compile_table_and_workflow_lock_the_table_and_require_feature_ir():
    from firefly_weave.compiler.api import compile_source, import_artifact
    from firefly_weave.compiler.canonical import canonical_digest
    from firefly_weave.compiler.catalog import CatalogSnapshot

    table = decision_table()
    result = compile_source(json.dumps(table), format="json", catalog=CatalogSnapshot.empty())
    assert result.ok, result.to_bytes()
    assert result.artifact.executable["irVersion"] == "weave/ir-v1alpha3"
    flow = compile_decision_workflow(table)
    assert flow.ok, flow.to_bytes()
    executable = flow.artifact.executable
    assert executable["irVersion"] == "weave/ir-v1alpha3"
    dependency = next(d for d in executable["dependencies"] if d["kind"] == "DecisionTable")
    assert dependency["digest"] == canonical_digest(table)
    node = next(n for n in executable["graph"]["nodes"] if n["kind"] == "decisionTable")
    assert node["dependency"] == dependency["digest"]
    assert import_artifact(flow.artifact.to_bytes()).digest == flow.artifact.digest


@pytest.mark.parametrize(
    "change,path",
    [
        ({"rules": [{"id": "bad", "when": {"literal": 1}, "output": {"literal": "x"}}]}, "/spec/rules/0/when"),
        ({"rules": [{"id": "bad", "when": {"literal": True}, "output": {"literal": 1}}]}, "/spec/rules/0/output"),
        (
            {"rules": [{"id": "bad", "when": {"ref": "/steps/previous/output"}, "output": {"literal": "x"}}]},
            "/spec/rules/0/when/ref",
        ),
    ],
)
def test_compiler_reports_invalid_rule_at_author_location(change, path):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot

    result = compile_source(json.dumps(decision_table(**change)), format="json", catalog=CatalogSnapshot.empty())
    assert not result.ok
    assert any(d.path == path and d.source is not None for d in result.diagnostics)


def test_compiler_rejects_collect_schema_that_cannot_return_empty_list():
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot

    result = compile_source(
        json.dumps(
            decision_table("collect", outputSchema={"type": "array", "items": {"type": "string"}, "minItems": 1})
        ),
        format="json",
        catalog=CatalogSnapshot.empty(),
    )
    assert not result.ok
    assert any(d.path == "/spec/outputSchema" for d in result.diagnostics)


def test_kernel_decides_without_worker_commands_and_persists_only_row_ids_as_evidence():
    from datetime import UTC, datetime
    from uuid import uuid4

    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState, RuntimeEvent

    flow = compile_decision_workflow(decision_table())
    assert flow.ok, flow.to_bytes()
    event = RuntimeEvent(id=uuid4(), type="started", data={}, timestamp=datetime(2026, 10, 2, tzinfo=UTC), sequence=1)
    result = transition(RunState(input={"amount": 500}), event, flow.artifact)
    assert result.state.status == "succeeded"
    assert result.state.output == "review"
    assert not result.commands
    step = next(s for s in result.steps if s.node_id == "decide")
    assert step.decision == {"matched_rule_ids": ["large"], "used_default": False}
    assert "500" not in json.dumps(step.decision)
    failed = compile_decision_workflow(decision_table("unique"))
    incident = transition(RunState(input={"amount": 500}), event, failed.artifact)
    assert incident.state.status == "suspended"
    assert incident.state.incident == "WV-DECISION-MULTIPLE_MATCHES"
    assert not incident.commands
    assert not incident.state.steps


def test_import_rejects_feature_downgrade_and_forged_table_scope_even_with_new_hashes():
    from firefly_weave.compiler.api import import_artifact
    from firefly_weave.compiler.canonical import canonical_digest

    artifact = compile_decision_workflow(decision_table()).artifact
    for version in ["weave/ir-v1alpha1", "weave/ir-v1alpha2"]:
        envelope = json.loads(artifact.to_bytes())
        envelope["executable"]["irVersion"] = version
        envelope["digest"] = canonical_digest(envelope["executable"])
        with pytest.raises(ValueError):
            import_artifact(envelope)
    envelope = json.loads(artifact.to_bytes())
    dependency = next(d for d in envelope["executable"]["dependencies"] if d["kind"] == "DecisionTable")
    old_digest = dependency["digest"]
    dependency["document"]["spec"]["rules"][0]["when"] = {"ref": "/steps/previous/output"}
    dependency["digest"] = canonical_digest(dependency["document"])
    for node in envelope["executable"]["graph"]["nodes"]:
        if node.get("dependency") == old_digest:
            node["dependency"] = dependency["digest"]
    envelope["digest"] = canonical_digest(envelope["executable"])
    with pytest.raises(ValueError):
        import_artifact(envelope)


def test_simulator_restores_before_decision_and_matches_production_without_mocks():
    from datetime import UTC, datetime

    from firefly_weave.operations.debug.simulator import Simulator
    from firefly_weave.runtime.kernel import transition
    from firefly_weave.runtime.models import RunState

    artifact = compile_decision_workflow(decision_table("collect")).artifact
    simulator = Simulator(artifact, mocks={}, input={"amount": 200}, now=datetime(2026, 10, 2, tzinfo=UTC))
    simulator.set_breakpoints({"decide"})
    assert simulator.continue_until_breakpoint().selected_node == "decide"
    restored = Simulator.restore(simulator.serialize())
    view = restored.continue_until_breakpoint()
    assert view.status == "succeeded"
    production = transition(RunState(input={"amount": 200}), view.events[0], artifact)
    assert view.variables == production.state.model_dump(mode="json")


def test_classified_table_outputs_cannot_be_published_or_imported_as_literals():
    from firefly_weave.compiler.api import compile_source, import_artifact
    from firefly_weave.compiler.canonical import canonical_digest
    from firefly_weave.compiler.catalog import CatalogSnapshot

    table = decision_table(outputSchema={"type": "string", "x-secret": True})
    result = compile_source(json.dumps(table), format="json", catalog=CatalogSnapshot.empty())
    assert not result.ok
    clean = compile_source(json.dumps(decision_table()), format="json", catalog=CatalogSnapshot.empty()).artifact
    envelope = json.loads(clean.to_bytes())
    envelope["executable"]["spec"]["outputSchema"]["x-secret"] = True
    envelope["digest"] = canonical_digest(envelope["executable"])
    with pytest.raises(ValueError):
        import_artifact(envelope)


@pytest.mark.parametrize("rules", [None, 1, "rows", {}, [None], [1], ["row"]])
def test_malformed_rules_are_diagnostics_not_admission_exceptions(rules):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot

    table = decision_table()
    table["spec"]["rules"] = rules
    result = compile_source(json.dumps(table), format="json", catalog=CatalogSnapshot.empty())
    assert not result.ok and result.diagnostics
