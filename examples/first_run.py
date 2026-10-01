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

"""Create explicit local grants and run a minimal workflow through public HTTP."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx


def workflow():
    schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "first-run", "version": "1.0.0"},
        "spec": {
            "inputSchema": schema,
            "outputSchema": schema,
            "steps": [{"id": "echo", "kind": "transform", "value": {"ref": "/input"}}],
            "output": {"ref": "/steps/echo/output"},
        },
    }


async def first_run(base_url, token_file, bootstrap_receipt, output):
    principal = json.loads(bootstrap_receipt.read_text())["principal_id"]
    token = json.loads(token_file.read_text())["access_token"]
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as evidence:
        async with httpx.AsyncClient(
            base_url=base_url, timeout=15, trust_env=False, headers={"Authorization": "Bearer " + token}
        ) as client:

            async def post(path, body):
                response = await client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
                if response.status_code not in (200, 201, 202):
                    raise RuntimeError("Public operation failed; inspect local readiness and grants")
                return response.json()

            tenant = await post("/admin/tenants", {"name": "first-run-" + uuid4().hex[:12]})
            scope = {"tenant_id": tenant["id"]}
            await post("/admin/grants", {"principal_id": principal, "grant": {"role": "tenant_admin", "scope": scope}})
            project = await post(f"/api/v1/tenants/{tenant['id']}/projects", {"name": "first-project"})
            project_url = f"/api/v1/tenants/{tenant['id']}/projects/{project['id']}"
            environment = await post(project_url + "/environments", {"name": "local"})
            scope.update(project_id=project["id"], environment_id=environment["id"])
            environment_url = project_url + "/environments/" + environment["id"]
            for role in ("developer", "deployer", "operator", "viewer"):
                granted_scope = (
                    {key: value for key, value in scope.items() if key != "environment_id"}
                    if role == "developer"
                    else scope
                )
                await post(
                    "/admin/grants", {"principal_id": principal, "grant": {"role": role, "scope": granted_scope}}
                )
            version = await post(project_url + "/workflows", {"format": "json", "source": json.dumps(workflow())})
            activation = await post(
                environment_url + "/activations",
                {"version_id": version["id"], "artifact_digest": version["digest"], "scope": scope},
            )
            started = await post(
                environment_url + "/runs", {"activation_id": activation["id"], "input": {"message": "Hello, Weave"}}
            )
            response = await client.get(environment_url + "/runs/" + started["id"])
            if response.status_code != 200:
                raise RuntimeError("Run inspection failed")
            run = response.json()
            if run["state"]["status"] != "succeeded" or run["state"]["output"] != {"message": "Hello, Weave"}:
                raise RuntimeError("First run did not produce its declared output")
            result = {
                "scope": scope,
                "version_id": version["id"],
                "activation_id": activation["id"],
                "run_id": run["id"],
                "status": run["state"]["status"],
                "output": run["state"]["output"],
            }
            json.dump(result, evidence, indent=2)
            evidence.write("\n")
            print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--bootstrap-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(first_run(args.api_url, args.token_file, args.bootstrap_receipt, args.output))
    except Exception:
        parser.exit(1, "First run failed; check readiness, token lifetime, grants and a new output path.\n")
