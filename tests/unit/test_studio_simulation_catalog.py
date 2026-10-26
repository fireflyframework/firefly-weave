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

"""The catalog a simulation compiles against: the project's, what ships with Weave, local definitions and stubs."""

from pathlib import Path

import pytest
import yaml

from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts import agentic
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.studio.simulation import CatalogConflict, bundled_catalog, simulation_catalog

ACTION = yaml.safe_load(Path("examples/definitions/check-customer.action.yaml").read_text("utf-8"))
WORKFLOW = {
    "spec": {
        "steps": [
            {"id": "check", "kind": "action", "uses": "onboarding.check-customer@1.0.0", "with": {"literal": {}}},
            {
                "id": "route",
                "kind": "switch",
                "cases": [
                    {
                        "when": {"literal": True},
                        "steps": [{"id": "lookup", "kind": "action", "uses": "crm.lookup@1.0.0", "with": {}}],
                        "output": {"literal": None},
                    }
                ],
                "default": {"steps": [], "output": {"literal": None}},
            },
        ]
    }
}


def test_what_ships_with_weave_compiles_offline():
    catalog = bundled_catalog()
    for kind, reference in [
        ("Connector", "weave-http@2.0.0"),
        ("Connector", "weave-email@1.0.0"),
        ("Connector", "weave-sftp@1.0.0"),
        ("Action", "weave-agentic-generate@1.0.0"),
        ("Adapter", "weave-agentic-provider"),
    ]:
        assert (kind, reference) in catalog.resources, reference


def test_a_local_worker_action_brings_the_task_it_implies():
    catalog, stubs = simulation_catalog(WORKFLOW, [ACTION], None, stub_unresolved=False)
    assert ("Action", "onboarding.check-customer@1.0.0") in catalog.resources
    assert ("TaskCapability", "onboarding.check-customer@1.0.0") in catalog.resources
    assert stubs == []


def test_actions_nobody_has_are_stubbed_only_when_asked():
    catalog, stubs = simulation_catalog(WORKFLOW, [], None, stub_unresolved=True)
    assert stubs == ["crm.lookup@1.0.0", "onboarding.check-customer@1.0.0"]
    stub = catalog.resources[("Action", "crm.lookup@1.0.0")].definition.value
    assert stub["spec"]["sideEffect"] == "read_only"
    assert stub["spec"]["implementation"] == {"kind": "worker", "taskType": "studio.stub", "taskVersion": "0.0.0"}
    assert ("Action", "crm.lookup@1.0.0") not in simulation_catalog(WORKFLOW, [], None, stub_unresolved=False)[
        0
    ].resources


def test_the_project_catalog_counts_and_a_different_copy_of_one_of_its_versions_is_a_conflict():
    project = CatalogSnapshot.from_definitions([load_definition(ACTION)])
    catalog, stubs = simulation_catalog(WORKFLOW, [], project, stub_unresolved=True)
    assert stubs == ["crm.lookup@1.0.0"]
    changed = ACTION | {"spec": ACTION["spec"] | {"timeoutSeconds": 61}}
    with pytest.raises(CatalogConflict):
        simulation_catalog(WORKFLOW, [changed], project, stub_unresolved=False)
    shipped = agentic.action_definition()
    with pytest.raises(CatalogConflict):
        simulation_catalog(
            WORKFLOW, [shipped | {"spec": shipped["spec"] | {"timeoutSeconds": 7}}], None, stub_unresolved=False
        )


def test_a_definition_that_does_not_load_is_left_out_never_guessed():
    catalog, stubs = simulation_catalog(WORKFLOW, [{"kind": "Action", "spec": "nonsense"}], None, stub_unresolved=True)
    assert "onboarding.check-customer@1.0.0" in stubs


def test_an_unknown_called_workflow_gets_an_interface_stub_whose_output_must_be_pinned():
    from firefly_weave.compiler.api import compile_source

    call = {"id": "notify", "kind": "callWorkflow", "uses": "notify@1.0.0", "with": {"literal": {}}}
    caller = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "orders", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": [call], "output": {"literal": None}},
    }
    catalog, stubs = simulation_catalog(caller, [], None, stub_unresolved=True)
    assert stubs == ["notify@1.0.0"]
    stub = catalog.resources[("Workflow", "notify@1.0.0")].definition.value["spec"]
    assert stub["inputSchema"] == {} and stub["outputSchema"] == {}
    assert stub["steps"] == [] and stub["output"] == {"literal": None} and stub["callable"] == {}
    result = compile_source(caller, format="object", catalog=catalog)
    assert result.ok, [d.code for d in result.diagnostics if d.severity == "error"]
    assert ("Workflow", "notify@1.0.0") not in simulation_catalog(caller, [], None, stub_unresolved=False)[0].resources


def test_a_decision_table_is_never_stubbed():
    table = {"id": "policy", "kind": "decisionTable", "uses": "payment-policy@1.2.0", "with": {"literal": {}}}
    document = {"spec": {"steps": [table]}}
    catalog, stubs = simulation_catalog(document, [], None, stub_unresolved=True)
    assert stubs == []
    assert not any(reference == "payment-policy@1.2.0" for _, reference in catalog.resources)
