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

"""Projection classification and trusted start metadata boundaries."""

import json
import runpy
from pathlib import Path
from uuid import uuid4

import pytest

from firefly_weave.operations import facts


@pytest.mark.parametrize(
    "key,want",
    [
        ("work", ("work", "")),
        ("work[3]", ("work", "work[3]")),
        ("work[3]#retry~2", ("work", "work[3]#retry~2")),
        ("@join:x", None),
        ("@any malformed synthetic", None),
    ],
)
def test_author_identity(key, want):
    project = getattr(facts, "author_instance", None)
    assert callable(project), "Author identity projection is missing"
    actual = project(key)
    assert (None if actual is None else (actual.node_id, actual.instance_key)) == want


@pytest.mark.parametrize("key", ["", "work[03]", "work:x", "work@x", "work[1]\n", 3, None, "x" * 513])
def test_invalid_author_identity_is_not_hidden(key):
    project = getattr(facts, "author_instance", None)
    assert callable(project), "Author identity projection is missing"
    with pytest.raises(ValueError):
        project(key)


@pytest.mark.parametrize(
    "values",
    [
        {"origin": "user"},
        {"origin": "retry"},
        {"origin": "manual", "retried_from_run_id": uuid4()},
        {"origin": "call"},
        {"origin": "test"},
        {"test": True},
        {"test": "yes"},
    ],
)
def test_rejects_incoherent_trusted_origin(values):
    with pytest.raises(ValueError):
        facts.RunStartFacts(**values)


def test_classification_matches_migration_with_raw_status_normalization(worker_runtime_fixture):
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.catalog import Activation, ActivationRequest
    from firefly_weave.contracts.runtime import StartRunRequest
    from firefly_weave.runtime.models import RunState

    project = getattr(facts, "listing_metadata", None)
    assert callable(project), "Listing classification projection is missing"
    frozen = runpy.run_path(str(Path(__file__).resolve().parents[3] / "migrations/versions/0031_operations_facts.py"))[
        "classification"
    ]
    artifact = json.loads(worker_runtime_fixture["artifact"].to_bytes())
    activation = Activation(
        id=uuid4(),
        name="worker-flow",
        revision=1,
        request=ActivationRequest(
            version_id=uuid4(),
            artifact_digest=artifact["digest"],
            scope=Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        ),
    )
    row = dict(
        state=RunState(admission_policy="classified-v1").model_dump(mode="json"),
        activation=activation.model_dump(mode="json"),
        request=StartRunRequest(activation_id=activation.id, input=3).model_dump(mode="json"),
        artifact=artifact,
    )
    cases = [
        row,
        *[{**row, "artifact": bad} for bad in (None, [], {}, {"executable": []}, {"executable": {"features": 4}})],
        *[{**row, "state": bad} for bad in (None, [], {}, {"status": "alien"}, {"status": 1})],
    ]
    cases.extend(
        {**row, "state": {**row["state"], "status": status}}
        for status in (
            None,
            7,
            [],
            "unknown",
            "queued",
            "running",
            "waiting",
            "suspended",
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
        )
    )
    for changes in (
        {"irVersion": "x" * 256},
        {"irVersion": "x" * 257},
        {"features": ["future-" + str(index) for index in range(256)]},
        {"features": ["future-" + str(index) for index in range(257)]},
        {"features": ["é" * 100 for _ in range(100)]},
        {"features": [None]},
    ):
        changed = {**artifact, "executable": {**artifact["executable"], **changes}}
        cases.append({**row, "artifact": changed})
    for key, value in (("id", "bad:author"), ("kind", "unknownAuthorKind")):
        changed = json.loads(json.dumps(artifact))
        node = next(node for node in changed["executable"]["graph"]["nodes"] if node["id"] == "work")
        node[key] = value
        cases.append({**row, "artifact": changed})
    for case in cases:
        expected = frozen(case)
        # The retained migration also normalizes raw status after classification.
        state = case.get("state")
        status = state.get("status") if isinstance(state, dict) else None
        if type(status) is not str or status not in {
            "queued",
            "running",
            "waiting",
            "suspended",
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
        }:
            expected.update(classification_state="unavailable", node_kinds="{}")
        expected["node_kinds"] = json.loads(expected["node_kinds"])
        expected["pinned_features"] = json.loads(expected["pinned_features"])
        assert project(case) == expected
    assert project(row)["node_kinds"] == {"work": "action"}


def test_call_origin_requires_a_typed_caller():
    with pytest.raises(ValueError):
        facts.RunStartFacts(origin="call", caller={"run_id": uuid4(), "node_id": "caller", "instance_key": ""})


def test_origin_is_not_a_request_body_field():
    from firefly_weave.contracts.runtime import StartRunRequest

    with pytest.raises(ValueError):
        StartRunRequest.model_validate({"activation_id": str(uuid4()), "input": 3, "origin": "provider"})
