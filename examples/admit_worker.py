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

"""Admit a worker release and pin an example workflow through authorized public HTTP."""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx


def definitions(capability):
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "record-customer", "version": "1.0.0"},
        "spec": {
            "implementation": {
                "kind": "worker",
                "taskType": capability["taskType"],
                "taskVersion": capability["taskVersion"],
            },
            **{key: capability[key] for key in ("inputSchema", "outputSchema", "sideEffect", "timeoutSeconds")},
            "retry": {"maxAttempts": 2, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
        },
    }
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "worker-first-run", "version": "1.0.0"},
        "spec": {
            "inputSchema": capability["inputSchema"],
            "outputSchema": capability["outputSchema"],
            "steps": [{"id": "record", "kind": "action", "uses": "record-customer@1.0.0", "with": {"ref": "/input"}}],
            "output": {"ref": "/steps/record/output"},
        },
    }
    return action, workflow


async def admit(scope_receipt, principal_receipt, manifest_path, image, output):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("An exact local image identity is required")
    scope = json.loads(scope_receipt.read_text())["scope"]
    principal = json.loads(principal_receipt.read_text())["principal_id"]
    manifest = json.loads(manifest_path.read_text())
    capability = manifest["capabilities"][0]
    action, workflow = definitions(capability)
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as identity:
            response = await identity.post(
                os.environ["WEAVE_KEYCLOAK_TEST_URL"] + "/realms/weave/protocol/openid-connect/token",
                data={"grant_type": "client_credentials"},
                auth=("weave-host", os.environ["WEAVE_HOST_SECRET"]),
            )
            response.raise_for_status()
            token = response.json()["access_token"]
        project = f"/api/v1/tenants/{scope['tenant_id']}/projects/{scope['project_id']}"
        environment = project + "/environments/" + scope["environment_id"]
        async with httpx.AsyncClient(
            base_url=os.environ["WEAVE_API_URL"],
            timeout=15,
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
        ) as client:

            async def post(path, body):
                response = await client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
                if response.status_code not in (200, 201, 202):
                    raise RuntimeError("Public worker setup failed")
                return response.json()

            release = await post(environment + "/worker-releases", {"image_digest": image, **manifest})
            task = capability["taskType"] + "@" + capability["taskVersion"]
            await post(
                "/admin/grants",
                {
                    "principal_id": principal,
                    "grant": {"role": "worker", "scope": scope, "resources": [release["id"], task]},
                },
            )
            await post(project + "/actions", {"format": "json", "source": json.dumps(action)})
            version = await post(project + "/workflows", {"format": "json", "source": json.dumps(workflow)})
            activation = await post(
                environment + "/activations",
                {
                    "version_id": version["id"],
                    "artifact_digest": version["digest"],
                    "scope": scope,
                    "worker_release_ids": {capability["taskType"]: release["id"]},
                },
            )
        json.dump(
            {
                "release_id": release["id"],
                "activation_id": activation["id"],
                "environment_url": environment,
                "scope": scope,
            },
            stream,
            indent=2,
        )
        stream.write("\n")
    print("Release admitted, worker grant assigned, workflow activated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-receipt", type=Path, required=True)
    parser.add_argument("--principal-receipt", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(admit(args.scope_receipt, args.principal_receipt, args.manifest, args.image, args.output))
    except Exception:
        parser.exit(1, "Worker admission failed; check readiness, grants, inputs and a new output path.\n")
