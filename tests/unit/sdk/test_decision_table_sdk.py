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

"""Decision authoring is available through the typed SDK and generated CLI surface."""

import json
from uuid import uuid4

import httpx
from click.testing import CliRunner

from firefly_weave.cli.main import cli
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.public import DecisionEvaluationRequest
from firefly_weave.sdk.client import WeaveClient


async def test_sdk_publishes_decision_collection_and_evaluates_typed_result():
    source = "kind: DecisionTable"
    request = DecisionEvaluationRequest(source=source, format="yaml", input={"amount": 7})
    identifier = uuid4()
    calls = []

    def receive(message):
        calls.append(message)
        if message.url.path.endswith("/decision-tables"):
            assert json.loads(message.content) == {"source": source, "format": "yaml"}
            assert message.headers["idempotency-key"] == "publish-policy"
            return httpx.Response(
                201,
                json={
                    "id": str(identifier),
                    "kind": "DecisionTable",
                    "name": "policy",
                    "version": "1.0.0",
                    "digest": "a" * 64,
                    "definition_digest": "b" * 64,
                },
            )
        assert message.url.path.endswith("/compiler/evaluate-decision")
        assert json.loads(message.content) == request.model_dump(mode="json", by_alias=True)
        return httpx.Response(200, json={"output": "approved", "matched_rule_ids": ["small"], "used_default": False})

    async with WeaveClient(
        "https://platform.example",
        lambda: "fixture-token",
        Scope(tenant_id=uuid4(), project_id=uuid4()),
        transport=httpx.MockTransport(receive),
    ) as client:
        published = await client.publish("decision-tables", source, "yaml", idempotency_key="publish-policy")
        assert published.id == identifier
        result = await client.evaluate_decision(request)
        assert result.output == "approved" and result.matched_rule_ids == ["small"]
    assert len(calls) == 2


def test_cli_exposes_decision_collection_and_pure_evaluation_request():
    runner = CliRunner()
    publication = runner.invoke(cli, ["definitions", "publish", "--help"])
    evaluation = runner.invoke(cli, ["remote", "evaluate-decision", "--help"])
    assert publication.exit_code == 0 and "decision-tables" in publication.output
    assert evaluation.exit_code == 0, evaluation.output
    assert "--request" in evaluation.output
