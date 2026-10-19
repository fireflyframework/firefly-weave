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

"""Human tasks inside a loop: one task per element in My tasks, answered in any order, collected in element order."""

import pytest
from test_human_tasks import author as author
from test_human_tasks import headers as headers
from test_human_tasks import human_identity as human_identity
from test_human_tasks import mutate

from firefly_weave.access.models import Grant

pytestmark = pytest.mark.integration

SOURCE = """apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: review-invoices, version: 1.0.0}
spec:
  inputSchema: {type: array, items: {type: string}}
  outputSchema: {}
  steps:
    - id: each
      kind: forEach
      items: {ref: /input}
      concurrency: 3
      body:
        steps:
          - id: review
            kind: humanTask
            assignment: reviewers
            title:
              op:
                name: concat
                args: [{literal: "Review "}, {ref: /item}]
            context: {object: {position: {ref: /index}}}
            formSchema: {type: object}
            decisions: [approve, reject]
            expirySeconds: 600
        output: {ref: /steps/review/output/decision}
  output: {ref: /steps/each/output}
"""


async def test_each_element_gets_its_own_task_and_answers_in_any_order_keep_element_order(
    author, access_db, provisioned, headers, project_url, env_url
):
    client, principal, scope = author
    for role in ("operator", "task_participant", "task_manager"):
        await access_db[2].grant(provisioned[0], principal.id, Grant(role=role, scope=scope))
    binding = await client.post(
        env_url + "/human-assignments",
        headers={**headers, "Idempotency-Key": "reviewers"},
        json={"name": "reviewers", "principal_ids": [str(principal.id)]},
    )
    assert binding.status_code == 200, binding.text
    published = await client.post(
        project_url + "/workflows",
        headers={**headers, "Idempotency-Key": "loop-flow"},
        json={"format": "yaml", "source": SOURCE},
    )
    assert published.status_code == 201, published.text
    activation = await client.post(
        env_url + "/activations",
        headers={**headers, "Idempotency-Key": "loop-activation"},
        json={
            "version_id": published.json()["id"],
            "artifact_digest": published.json()["digest"],
            "scope": scope.model_dump(mode="json"),
            "assignment_binding_ids": {"reviewers": binding.json()["binding_id"]},
        },
    )
    assert activation.status_code == 201, activation.text
    started = await client.post(
        env_url + "/runs",
        headers={**headers, "Idempotency-Key": "loop-run"},
        json={"activation_id": activation.json()["id"], "input": ["INV-7", "INV-8", "INV-9"]},
    )
    assert started.status_code == 201, started.text
    listed = await client.get(env_url + "/human-tasks", headers=headers)
    assert listed.status_code == 200, listed.text
    tasks = sorted(listed.json()["items"], key=lambda task: task["iteration"])
    assert [(t["node_id"], t["instance_key"], t["iteration"], t["title"]) for t in tasks] == [
        ("review", "review[0]", [0], "Review INV-7"),
        ("review", "review[1]", [1], "Review INV-8"),
        ("review", "review[2]", [2], "Review INV-9"),
    ]
    for task, decision in ((tasks[2], "reject"), (tasks[0], "approve"), (tasks[1], "approve")):
        claimed = await mutate(client, headers, env_url, task, "claim", {"expected_revision": 1}, f"claim-{task['id']}")
        assert claimed.status_code == 200, claimed.text
        payload = {"expected_revision": 2, "decision": decision, "data": {}}
        completed = await mutate(client, headers, env_url, task, "complete", payload, f"complete-{task['id']}")
        assert completed.status_code == 200, completed.text
    view = (await client.get(f"{env_url}/runs/{started.json()['id']}", headers=headers)).json()
    assert view["state"]["status"] == "succeeded" and view["state"]["output"] == ["approve", "approve", "reject"]
