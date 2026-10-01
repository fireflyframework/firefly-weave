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

"""Admit one native HTTP read through public scope, release and connection APIs."""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx


async def admit(scope_receipt, principal_receipt, manifest_path, image, output):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Exact local native image identity required")
    scope = json.loads(scope_receipt.read_text())["scope"]
    principal = json.loads(principal_receipt.read_text())["principal_id"]
    descriptor = json.loads(manifest_path.read_text())
    binding = next(item for item in descriptor["connector_bindings"] if item["action"] == "read")
    task = binding["task_reference"]
    capability = next(
        item for item in descriptor["capabilities"] if item["taskType"] + "@" + item["taskVersion"] == task
    )
    origin = "http://host.docker.internal:8090"
    project = f"/api/v1/tenants/{scope['tenant_id']}/projects/{scope['project_id']}"
    environment = project + "/environments/" + scope["environment_id"]
    with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as identity:
            response = await identity.post(
                os.environ["WEAVE_KEYCLOAK_TEST_URL"] + "/realms/weave/protocol/openid-connect/token",
                data={"grant_type": "client_credentials"},
                auth=("weave-host", os.environ["WEAVE_HOST_SECRET"]),
            )
            response.raise_for_status()
            token = response.json()["access_token"]
        async with httpx.AsyncClient(
            base_url=os.environ["WEAVE_API_URL"],
            timeout=15,
            trust_env=False,
            headers={"Authorization": "Bearer " + token},
        ) as client:

            async def post(path, body):
                response = await client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
                if response.status_code not in (200, 201, 202):
                    raise RuntimeError("Public native setup failed")
                return response.json()

            connector = await post(
                project + "/connectors", {"format": "json", "source": json.dumps(descriptor["connector"])}
            )
            release = await post(
                environment + "/worker-releases",
                {
                    "image_digest": image,
                    "capabilities": [capability],
                    "connector_bindings": [binding],
                    "credential_capabilities": [task],
                },
            )
            await post(
                "/admin/grants",
                {
                    "principal_id": principal,
                    "grant": {"role": "worker", "scope": scope, "resources": [release["id"], task]},
                },
            )
            connection = await post(
                environment + "/connections",
                {
                    "name": "native-local-receiver",
                    "connector_version_id": connector["id"],
                    "config": {"baseUrl": origin, "auth": "none"},
                    "secretRef": {},
                    "allowed_destinations": [origin],
                },
            )
            await post(
                environment + "/worker-connection-grants",
                {"release_id": release["id"], "connection_revision_id": connection["id"], "capability": task},
            )
            action = {
                "apiVersion": "weave/v1alpha1",
                "kind": "Action",
                "metadata": {"name": "native-read-health", "version": "1.0.0"},
                "spec": {
                    "implementation": {
                        "kind": "connector",
                        "uses": "weave-http@1.0.0",
                        "action": "read",
                        "config": {"method": "GET", "path": "/health", "statuses": [200]},
                    },
                    "connection": {"connector": "weave-http@1.0.0"},
                    **{key: capability[key] for key in ("inputSchema", "outputSchema", "sideEffect", "timeoutSeconds")},
                },
            }
            await post(project + "/actions", {"format": "json", "source": json.dumps(action)})
            workflow = {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": "native-first-run", "version": "1.0.0"},
                "spec": {
                    "inputSchema": {"type": "object"},
                    "outputSchema": capability["outputSchema"],
                    "connections": {"http": {"connector": "weave-http@1.0.0"}},
                    "steps": [
                        {
                            "id": "read",
                            "kind": "action",
                            "uses": "native-read-health@1.0.0",
                            "connection": "http",
                            "with": {"literal": {}},
                        }
                    ],
                    "output": {"ref": "/steps/read/output"},
                },
            }
            version = await post(project + "/workflows", {"format": "json", "source": json.dumps(workflow)})
            activation = await post(
                environment + "/activations",
                {
                    "scope": scope,
                    "version_id": version["id"],
                    "artifact_digest": version["digest"],
                    "connection_revision_ids": {"http": connection["id"]},
                    "connector_release_ids": {connector["id"]: release["id"]},
                },
            )
        json.dump(
            {
                "environment_url": environment,
                "activation_id": activation["id"],
                "executor": {
                    "scope": scope,
                    "principal_id": principal,
                    "release_id": release["id"],
                    "task_types": [task],
                    "capacity": 1,
                },
            },
            stream,
            indent=2,
        )
        stream.write("\n")
    print("Native read release, scoped authority, connection and workflow prepared")


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
        parser.exit(1, "Native admission failed; check readiness, grants, exact inputs and a new receipt path.\n")
