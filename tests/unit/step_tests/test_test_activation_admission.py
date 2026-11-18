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

"""What a draft test activation checks before anything is written: how long it lives and what it may change."""

from uuid import UUID

import pytest

from firefly_weave.definitions.models import CatalogError
from firefly_weave.step_tests.test_activations import (
    confirmation_refusal,
    draft_lifetime,
    step_effects,
    synthetic_document,
)


def action(digest, effect, timeout=30):
    return {
        "kind": "Action",
        "reference": f"{digest}@1.0.0",
        "digest": digest,
        "document": {"spec": {"sideEffect": effect, "timeoutSeconds": timeout}},
    }


def workflow_dependency(digest, reference):
    return {"kind": "Workflow", "reference": reference, "digest": digest, "document": {"spec": {}}}


def executable(*dependencies):
    return {"dependencies": list(dependencies)}


def refusal(call) -> CatalogError:
    with pytest.raises(CatalogError) as raised:
        call()
    return raised.value


def test_the_lifetime_is_the_request_or_the_default():
    assert draft_lifetime(900, executable(action("a", "read_only", 30))) == 900
    assert draft_lifetime(3600, executable()) == 3600


def test_a_shorter_lifetime_is_raised_to_the_longest_step_timeout():
    assert draft_lifetime(60, executable(action("a", "read_only", 30), action("b", "read_only", 1200))) == 1200


def test_a_draft_whose_steps_need_more_than_an_hour_is_refused():
    error = refusal(lambda: draft_lifetime(900, executable(action("a", "read_only", 3601))))
    assert (error.status, error.code) == (422, "WV-TESTACTIVATION-LIFETIME")


def test_each_step_takes_the_strongest_effect_of_what_it_runs():
    dependencies = executable(
        action("fetch", "read_only"),
        action("charge", "non_idempotent"),
        action("notify", "idempotency_key"),
        workflow_dependency("callee", "notify-customer@1.0.0"),
    )
    effects = step_effects(
        {
            "fetch-customer": ["fetch"],
            # An agent's step runs every tool it may call.
            "support": ["fetch", "notify"],
            "charge-card": ["charge"],
            "notify-customer": ["callee"],
        },
        dependencies,
        {"notify-customer@1.0.0": "idempotent"},
    )
    assert effects == {
        "fetch-customer": "read_only",
        "support": "idempotency_key",
        "charge-card": "non_idempotent",
        "notify-customer": "idempotent",
    }


def test_a_call_whose_tree_changes_data_counts_as_strong_as_its_tree():
    effects = step_effects(
        {"notify-customer": ["callee"]},
        executable(workflow_dependency("callee", "notify-customer@1.0.0")),
        {"notify-customer@1.0.0": "non_idempotent"},
    )
    assert effects == {"notify-customer": "non_idempotent"}


def test_a_draft_that_changes_data_needs_its_effect_acknowledged_and_names_the_steps():
    effects = {"fetch": "read_only", "charge-card": "non_idempotent", "refund": "non_idempotent"}
    for acknowledged in (None, "idempotency_key"):
        error = confirmation_refusal("non_idempotent", effects, acknowledged)
        assert error is not None
        assert (error.status, error.code) == (409, "WV-STEPTEST-CONFIRM")
        assert error.result == {"side_effect": "non_idempotent", "steps": ["charge-card", "refund"]}
    assert confirmation_refusal("non_idempotent", effects, "non_idempotent") is None


@pytest.mark.parametrize("effect", ["read_only", "idempotent"])
def test_a_draft_that_changes_nothing_or_repeats_safely_runs_directly(effect):
    assert confirmation_refusal(effect, {"fetch": effect}, None) is None


def test_the_test_document_is_the_draft_marked_test_with_its_runs_capped_at_an_hour():
    activation = UUID("0123456789ab4def8123456789abcdef")
    draft = {"metadata": {"name": "orders", "version": "1.2.0+build.7"}, "spec": {"timeoutSeconds": 7200, "steps": []}}
    document = synthetic_document(draft, activation)
    assert document["metadata"] == {"name": "orders", "version": "1.2.0+test.0123456789ab"}
    assert document["spec"]["timeoutSeconds"] == 3600
    assert draft["spec"]["timeoutSeconds"] == 7200
    assert (
        synthetic_document({"metadata": {"name": "orders", "version": "1.2.0"}, "spec": {}}, activation)["spec"][
            "timeoutSeconds"
        ]
        == 3600
    )
    shorter = {"metadata": {"name": "orders", "version": "1.2.0"}, "spec": {"timeoutSeconds": 120}}
    assert synthetic_document(shorter, activation)["spec"]["timeoutSeconds"] == 120
