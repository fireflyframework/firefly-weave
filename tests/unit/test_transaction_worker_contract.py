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


"""What workers and actions declare about failures that applied nothing."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import ActionDefinition, load_definition
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.contracts.workers import ClaimRequest, TaskError

ACTION = parse_source(Path("examples/definitions/check-customer.action.yaml").read_text(), format="yaml").value


def action(**spec: object) -> dict:
    return {**ACTION, "spec": {**ACTION["spec"], **spec}}


def test_a_failure_says_nothing_about_its_effect_unless_it_reports_none():
    completion = uuid4()
    plain = TaskError(completion_id=completion, code="VALIDATION", outcome="failed")
    assert plain.effect == "unknown"
    assert "effect" not in plain.model_dump(mode="json")
    definite = TaskError.model_validate_json(
        json.dumps({"completion_id": str(completion), "code": "VALIDATION", "outcome": "failed", "effect": "none"})
    )
    assert definite.model_dump(mode="json")["effect"] == "none"
    with pytest.raises(ValidationError):
        TaskError.model_validate_json(json.dumps({"completion_id": str(completion), "code": "X", "effect": "maybe"}))
    schema = export_schemas()["task-error"]
    assert schema["properties"]["effect"]["enum"] == ["none", "unknown"]


def test_a_claim_may_name_the_release_and_whether_it_is_transaction_aware():
    worker, release = uuid4(), uuid4()
    plain = ClaimRequest(worker_id=worker, limit=5)
    assert plain.model_dump(mode="json") == {"worker_id": str(worker), "limit": 5}
    aware = ClaimRequest.model_validate_json(
        json.dumps(
            {"worker_id": str(worker), "limit": 5, "worker": {"release": str(release), "transaction_aware": True}}
        )
    )
    assert aware.worker is not None and aware.worker.transaction_aware
    assert aware.worker.release == release
    with pytest.raises(ValidationError):
        ClaimRequest.model_validate_json(
            json.dumps({"worker_id": str(worker), "limit": 5, "worker": {"release_id": str(release)}})
        )
    Draft202012Validator(export_schemas()["claim-request"]).validate(aware.model_dump(mode="json"))


def test_existing_actions_keep_their_bytes():
    assert "definiteFailures" not in load_definition(ACTION).model_dump(by_alias=True)["spec"]
    assert "transactionAware" not in load_definition(ACTION).model_dump(by_alias=True)["spec"]


def test_an_action_declares_definite_failures_and_transaction_awareness():
    spec = load_definition(action(definiteFailures=["VALIDATION", "NOT_FOUND"], transactionAware=True))
    assert isinstance(spec, ActionDefinition)
    dumped = spec.model_dump(by_alias=True)["spec"]
    assert dumped["definiteFailures"] == ["VALIDATION", "NOT_FOUND"]
    assert dumped["transactionAware"] is True


@pytest.mark.parametrize(
    "fields",
    [
        {"definiteFailures": ["VALIDATION", "VALIDATION"]},
        {"definiteFailures": ["lowercase"]},
        {"definiteFailures": [f"CODE_{index}" for index in range(33)]},
        {"definiteFailures": None},
        {"transactionAware": "yes"},
    ],
)
def test_invalid_failure_declarations_are_refused(fields):
    with pytest.raises(ValidationError):
        load_definition(action(**fields))
    if fields.get("definiteFailures") != ["VALIDATION", "VALIDATION"]:
        assert not Draft202012Validator(export_schemas()["action"]).is_valid(action(**fields))
