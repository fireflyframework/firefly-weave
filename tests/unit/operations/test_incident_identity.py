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

"""An incident's step identity comes from the incident and its facts; a disagreement is withheld, never guessed."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from firefly_weave.compiler.ir import accepted_ir_versions
from firefly_weave.contracts import language_features
from firefly_weave.contracts.operations import IncidentView
from firefly_weave.contracts.public import UnavailableResource
from firefly_weave.operations.fact_reads import incident_of
from firefly_weave.runtime import admission

AT = datetime(2026, 10, 1, tzinfo=UTC)


def detail(source, static, instance="", **changes):
    """A selected incident as the detail statement returns it, on a run whose metadata is available."""
    return {
        "id": uuid4(),
        "run_id": uuid4(),
        "incident_key": source or "@legacy",
        "node_id": source,
        "generation": 0,
        "origin_code": "WV-EXPR-MISSING",
        "code": "WV-EXPR-MISSING",
        "status": "active",
        "revision": 1,
        "actor_id": None,
        "resolved_at": None,
        "resolution": None,
        "oversized": False,
        "projected": True,
        "static_node_id": static,
        "instance_key": instance,
        "opened_at": AT,
        "updated_at": AT,
        "opened_at_estimated": False,
        "workflow_name": "flow",
        "workflow_version": "1.0.0",
        "test": False,
        "run_status": "suspended",
        "classification_state": "available",
        "classification_policy": admission.POLICY,
        "pinned_ir_version": accepted_ir_versions(language_features.ADVERTISED_FEATURES)[0],
        "pinned_features": [],
        "policy_blocked": False,
        **changes,
    }


@pytest.mark.parametrize(
    "source,static,instance,served",
    [
        ("work", "work", "", ("work", "")),
        ("work[2]", "work", "work[2]", ("work", "work[2]")),
        ("work[1]#3.review", "work", "work[1]#3.review", ("work", "work[1]#3.review")),
        (None, None, "", (None, "")),
        # Switch and parallel branch outputs and joins are control steps: their facts carry no step ID.
        ("@branch:fork:0", None, "", ("@branch:fork:0", "")),
        ("@join:fork", None, "", ("@join:fork", "")),
    ],
)
def test_incident_identity_is_served_from_matching_facts(source, static, instance, served):
    view = incident_of(detail(source, static, instance))
    assert isinstance(view, IncidentView), view
    assert (view.node_id, view.instance_key) == served
    assert view.code == "WV-EXPR-MISSING" and view.revision == 1
    if not served[1]:
        assert "instance_key" not in view.model_dump(mode="json")


@pytest.mark.parametrize(
    "source,static,instance",
    [
        # An author step whose facts lost the step ID could not be normalized.
        ("work", None, ""),
        ("work[2]", None, "work[2]"),
        # A run-level incident never has step facts.
        (None, "work", ""),
        (None, None, "work[2]"),
        # A control step is never attributed to an author step or an instance.
        ("@branch:fork:0", "fork", ""),
        ("@branch:fork:0", None, "@branch:fork:0"),
        # Both identities are present but disagree.
        ("work[2]", "work", ""),
        ("work", "other", ""),
        ("work[2]", "work", "work[3]"),
        ("work", "work", "work[2]"),
    ],
)
def test_incident_identity_that_disagrees_with_its_facts_is_withheld(source, static, instance):
    row = detail(source, static, instance)
    view = incident_of(row)
    assert isinstance(view, UnavailableResource) and view.id == row["id"]
    assert [omission.reason for omission in view.omissions] == ["classification_unavailable"]


@pytest.mark.parametrize("changes", [{"projected": False}, {"policy_blocked": True}])
def test_control_step_incident_still_needs_facts_and_available_run_metadata(changes):
    assert isinstance(incident_of(detail("@branch:fork:0", None, **changes)), UnavailableResource)
