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

"""Publish, activate and complete native approval using an authorized human token.

Run with WEAVE_BASE_URL, WEAVE_ACCESS_TOKEN, WEAVE_TENANT_ID,
WEAVE_PROJECT_ID, WEAVE_ENVIRONMENT_ID and WEAVE_PRINCIPAL_ID.
The authenticated principal needs developer, deployer, operator,
task_manager and task_participant grants in the appropriate scope.
"""

import asyncio
import json
import os
from uuid import UUID, uuid4

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.human_tasks import AssignmentBindingRequest, CompleteHumanTask
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.sdk.client import WeaveClient


async def main() -> None:
    scope = Scope(
        tenant_id=UUID(os.environ["WEAVE_TENANT_ID"]),
        project_id=UUID(os.environ["WEAVE_PROJECT_ID"]),
        environment_id=UUID(os.environ["WEAVE_ENVIRONMENT_ID"]),
    )
    principal = UUID(os.environ["WEAVE_PRINCIPAL_ID"])
    document = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "expense-approval-" + uuid4().hex[:8], "version": "1.0.0"},
        "spec": {
            "inputSchema": {"type": "object"},
            "outputSchema": {},
            "timeoutSeconds": 3600,
            "steps": [
                {
                    "id": "review",
                    "kind": "humanTask",
                    "assignment": "expense-reviewers",
                    "title": {"literal": "Review expense"},
                    "context": {"ref": "/input"},
                    "formSchema": {
                        "type": "object",
                        "properties": {"note": {"type": "string"}},
                        "required": ["note"],
                        "additionalProperties": False,
                    },
                    "dueSeconds": 300,
                    "expirySeconds": 1800,
                }
            ],
            "output": {"ref": "/steps/review/output"},
        },
    }
    async with WeaveClient(os.environ["WEAVE_BASE_URL"], lambda: os.environ["WEAVE_ACCESS_TOKEN"], scope) as client:
        bindings = await client.assignment_bindings()
        binding = next((value for value in bindings.items if value.name == "expense-reviewers"), None)
        if binding is None:
            binding = await client.put_assignment_binding(
                AssignmentBindingRequest(name="expense-reviewers", principal_ids=[principal]),
                idempotency_key=str(uuid4()),
            )
        version = await client.publish("workflows", json.dumps(document), "json", idempotency_key=str(uuid4()))
        activation = await client.activate(
            ActivationRequest(
                version_id=version.id,
                artifact_digest=version.digest,
                scope=scope,
                assignment_binding_ids={"expense-reviewers": binding.binding_id},
            ),
            idempotency_key=str(uuid4()),
        )
        run = await client.start_run(
            StartRunRequest(activation_id=activation.id, input={"amount": 10}), idempotency_key=str(uuid4())
        )
        tasks = await client.human_tasks(status="ready")
        task = next(value for value in tasks.items if value.run_id == run.id)
        task = await client.claim_human_task(task.id, revision=task.revision, idempotency_key=str(uuid4()))
        task = await client.complete_human_task(
            task.id,
            CompleteHumanTask(expected_revision=task.revision, decision="approve", data={"note": "Reviewed"}),
            idempotency_key=str(uuid4()),
        )
        print(json.dumps({"run_id": str(run.id), "task_id": str(task.id), "status": task.status}))


if __name__ == "__main__":
    asyncio.run(main())
