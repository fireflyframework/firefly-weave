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

"""Test data of a transaction's steps: a pin of a confirm, release or undo is checked against its own operation."""

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "operations" / "debug"))

from simulation_support import callee, each, fixture, snapshot_with, strict_catalog  # noqa: E402
from transaction_support import add, catalog, hold, reverse_by_call, submit, transaction  # noqa: E402

from firefly_weave.compiler import test_data  # noqa: E402
from firefly_weave.compiler.catalog import CatalogSnapshot  # noqa: E402
from firefly_weave.contracts.studio_documents import StudioTestData  # noqa: E402

UNRESOLVED = "WV-STUDIO-TESTDATA-UNRESOLVED"
PIN = {
    "source": "manual",
    "pinnedAt": "2026-11-24T10:00:00Z",
    "configHash": "fnv1a64:0123456789abcdef",
    "contractHash": "fnv1a64:0123456789abcdef",
    "upstreamHash": "fnv1a64:0123456789abcdef",
}
UNDONE = {
    "type": "object",
    "properties": {"undone": {"type": "boolean"}},
    "required": ["undone"],
    "additionalProperties": False,
}


def document(*steps: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "move-between-systems", "version": "1.0.0"},
        "spec": {"inputSchema": {}, "outputSchema": {}, "steps": list(steps), "output": {"literal": None}},
    }


def output_of(catalog: CatalogSnapshot, kind: str, reference: str) -> Any:
    return catalog.resources[(kind, reference)].definition.value["spec"]["outputSchema"]  # type: ignore[index]


def undo_by_call(output_schema: dict[str, Any]) -> CatalogSnapshot:
    """The fixture catalog, where the undo workflow returns ``output_schema``."""
    definitions = fixture()["definitions"]
    undo = next(item for item in definitions if item["metadata"]["name"] == "reverse-target-addition")
    return snapshot_with({**undo, "spec": {**undo["spec"], "outputSchema": output_schema}})


def saved(workflow: dict[str, Any], catalog: CatalogSnapshot, **sections: Any) -> dict[str, str | None]:
    """Why each entry of a saved test data document is refused, or None, by pointer."""
    data = {"schemaVersion": 1, "kind": "weave.studio/testData", "updatedAt": "2026-11-24T10:00:00Z", **sections}
    return dict(test_data.check_test_data(workflow, StudioTestData.model_validate(data, strict=False), catalog))


def pins(**outputs: Any) -> dict[str, Any]:
    return {key: {**PIN, "output": output} for key, output in outputs.items()}


def test_a_confirm_pin_is_checked_against_the_confirm_actions_output():
    strict = strict_catalog("source-system.confirm-hold", ["confirmedAt"])
    move = document(transaction([hold(), add(), submit()]))
    confirmed = output_of(strict, "Action", "source-system.confirm-hold@1.0.0")
    assert test_data.pin_target(move, "hold#confirm", strict) == confirmed
    assert test_data.pin_target(move, "hold[2]#confirm", strict) == confirmed
    assert confirmed != output_of(strict, "Action", "source-system.hold-quantity@1.0.0")


def test_an_undo_pin_of_a_called_undo_is_checked_against_its_workflows_output():
    move = document(transaction([hold(), reverse_by_call(), submit()]))
    assert test_data.pin_target(move, "add#undo", undo_by_call(UNDONE)) == UNDONE


@pytest.mark.parametrize("key", ["add#confirm", "hold#undo", "submit#cancel", "add#1.undo", "nowhere#undo"])
def test_a_phase_its_step_does_not_declare_has_no_schema(key):
    move = document(transaction([hold(), add(), submit()]))
    assert test_data.pin_target(move, key, catalog()) == UNRESOLVED


def test_a_pin_of_a_step_in_a_transaction_is_checked_against_the_steps_output():
    # The transaction's own steps are found wherever they are: in its body, in a loop of its body, in its error path.
    recover = {"steps": [add("again")], "output": {"literal": None}}
    move = document(transaction([hold(), each([add()]), submit()], onError="errorOutput", errorPath=recover))
    found = saved(move, catalog(), pins=pins(**{"hold": {"holdId": 7}, "add[1]": {"id": "a-1"}, "again": {"id": 1}}))
    assert found == {
        "/pins/hold": "WV-STUDIO-TESTDATA-SCHEMA",
        "/pins/add[1]": None,
        "/pins/again": "WV-STUDIO-TESTDATA-SCHEMA",
    }


def test_saved_phase_pins_are_checked_against_the_operation_that_runs_the_phase():
    strict = strict_catalog("source-system.confirm-hold", ["confirmedAt"])
    move = document(transaction([hold(), add(), submit()]))
    # The confirm's own output: a value that fits the step but not its confirm is out of date, and the other way round
    # a value that fits the confirm is kept.
    assert saved(move, strict, pins=pins(**{"hold#confirm": {"holdId": "h-1"}})) == {
        "/pins/hold#confirm": "WV-STUDIO-TESTDATA-SCHEMA"
    }
    assert saved(move, strict, pins=pins(**{"hold#confirm": {"confirmedAt": "2026-11-24"}})) == {
        "/pins/hold#confirm": None
    }


def test_a_secret_only_a_called_undo_marks_is_refused_in_saved_test_data():
    marked = undo_by_call({**UNDONE, "properties": {"undone": {"type": "boolean", "x-secret": True}}})
    move = document(transaction([hold(), reverse_by_call(), submit()]))
    assert saved(move, marked, pins=pins(**{"add#undo": {"undone": True}})) == {
        "/pins/add#undo": "WV-SCHEMA-SECRET_VALUE"
    }


def test_a_called_workflows_phase_mock_is_checked_against_its_own_operation():
    inside, _ = callee("inside", [transaction([hold(), add(), submit()])])
    strict = strict_catalog("source-system.confirm-hold", ["confirmedAt"], inside)
    mocks = {"hold#confirm": {"output": {"holdId": "h-1"}, "source": "manual"}}
    found = saved(document(), strict, workflows={"inside@1.0.0": {"mocks": mocks}})
    assert found == {"/workflows/inside@1.0.0/mocks/hold#confirm": "WV-STUDIO-TESTDATA-SCHEMA"}
