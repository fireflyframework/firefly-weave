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

"""Host integration using public HTTP only; no server or database imports."""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx


class HostProduct:
    def __init__(self, client: httpx.AsyncClient, scope: dict | None = None):
        self.client = client
        self.scope = scope or {}

    @property
    def project_url(self):
        return f"/tenants/{self.scope['tenant_id']}/projects/{self.scope['project_id']}"

    @property
    def environment_url(self):
        return self.project_url + f"/environments/{self.scope['environment_id']}"

    async def post(self, path, body, *, expected=201, headers=None):
        response = await self.client.post(path, json=body, headers={"Idempotency-Key": str(uuid4()), **(headers or {})})
        if response.status_code != expected:
            raise RuntimeError(f"Public operation {path} returned {response.status_code}: {response.text}")
        return response.json()

    async def provision_scope(self, tenant_id, name):
        project = await self.post(f"/tenants/{tenant_id}/projects", {"name": name}, expected=200)
        environment = await self.post(
            f"/tenants/{tenant_id}/projects/{project['id']}/environments", {"name": "local"}, expected=200
        )
        self.scope = {"tenant_id": tenant_id, "project_id": project["id"], "environment_id": environment["id"]}
        return self.scope

    async def publish(self, collection, definition):
        return await self.post(
            self.project_url + "/" + collection, {"format": "json", "source": json.dumps(definition)}
        )

    async def prepare(self, native_image, worker_image, external_origin):
        descriptor = json.loads(Path(__file__).with_name("http-manifest.json").read_text())
        worker = json.loads((Path(__file__).parents[1] / "worker/manifest.json").read_text())
        connector = await self.publish("connectors", descriptor["connector"])
        native_release = await self.post(
            self.environment_url + "/worker-releases",
            {
                "image_digest": native_image,
                "capabilities": descriptor["capabilities"],
                "connector_bindings": descriptor["connector_bindings"],
                "credential_capabilities": [descriptor["connector_bindings"][0]["task_reference"]],
            },
        )
        worker_release = await self.post(
            self.environment_url + "/worker-releases",
            {
                "image_digest": worker_image,
                **worker,
            },
        )
        connection = await self.post(
            self.environment_url + "/connections",
            {
                "name": "customer-http",
                "connector_version_id": connector["id"],
                "config": {"baseUrl": external_origin, "auth": "bearer"},
                "secretRef": {"token": "http-token"},
                "allowed_destinations": [external_origin],
            },
        )
        await self.post(
            self.environment_url + "/worker-connection-grants",
            {
                "release_id": native_release["id"],
                "connection_revision_id": connection["id"],
                "capability": descriptor["connector_bindings"][0]["task_reference"],
            },
        )
        read_contract = descriptor["connector"]["spec"]["actions"]["read"]
        await self.publish(
            "actions",
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Action",
                "metadata": {"name": "read-customer", "version": "1.0.0"},
                "spec": {
                    "implementation": {
                        "kind": "connector",
                        "uses": "weave-http@1.0.0",
                        "action": "read",
                        "config": {"method": "GET", "path": "/customer", "statuses": [200]},
                    },
                    "connection": {"connector": "weave-http@1.0.0"},
                    "sideEffect": "read_only",
                    "timeoutSeconds": 30,
                    "inputSchema": read_contract["inputSchema"],
                    "outputSchema": read_contract["outputSchema"],
                },
            },
        )
        capability = worker["capabilities"][0]
        await self.publish(
            "actions",
            {
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
            },
        )
        version = await self.publish("workflows", self.workflow())
        activation_body = {
            "scope": self.scope,
            "version_id": version["id"],
            "artifact_digest": version["digest"],
            "connection_revision_ids": {"http": connection["id"]},
            "connector_release_ids": {connector["id"]: native_release["id"]},
            "worker_release_ids": {"example-record": worker_release["id"]},
        }
        activation = await self.post(self.environment_url + "/activations", activation_body)
        trigger = await self.post(
            self.environment_url + "/triggers",
            {
                "name": "onboard",
                "kind": "run",
                "activation_id": activation["id"],
                "secret_ref": "webhook-key",
                "payload_schema": {"type": "object"},
            },
        )
        return {
            "scope": self.scope,
            "activation": activation,
            "activation_body": activation_body,
            "version": version,
            "trigger": trigger,
            "native_release": native_release,
            "worker_release": worker_release,
        }

    @staticmethod
    def workflow(version="1.0.0"):
        return {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "onboarding", "version": version},
            "spec": {
                "inputSchema": {"type": "object"},
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "approved": {"type": "boolean"},
                        "receipt": {"type": "string"},
                        "customer": {"type": "string"},
                    },
                    "required": ["approved", "receipt", "customer"],
                    "additionalProperties": False,
                },
                "timeoutSeconds": 600,
                "connections": {"http": {"connector": "weave-http@1.0.0"}},
                "steps": [
                    {
                        "id": "read",
                        "kind": "action",
                        "uses": "read-customer@1.0.0",
                        "connection": "http",
                        "with": {"object": {"query": {"ref": "/input"}}},
                    },
                    {
                        "id": "record",
                        "kind": "action",
                        "uses": "record-customer@1.0.0",
                        "with": {"ref": "/steps/read/output/body"},
                    },
                    {
                        "id": "approval",
                        "kind": "signal",
                        "name": "approved",
                        "timeoutSeconds": 300,
                        "payloadSchema": {
                            "type": "object",
                            "properties": {"approved": {"type": "boolean"}},
                            "required": ["approved"],
                            "additionalProperties": False,
                        },
                    },
                ],
                "output": {
                    "object": {
                        "approved": {"ref": "/steps/approval/output/approved"},
                        "receipt": {"ref": "/steps/record/output/receipt"},
                        "customer": {"ref": "/steps/record/output/customer"},
                    }
                },
            },
        }

    async def trigger(self, trigger_id, secret, event_id, payload):
        raw = json.dumps({"eventId": event_id, "payload": payload}, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        response = await self.client.post(
            "/webhooks/" + trigger_id,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Weave-Timestamp": timestamp,
                "X-Weave-Signature": signature,
            },
        )
        response.raise_for_status()
        return response.json()

    async def inspect(self, run_id):
        response = await self.client.get(self.environment_url + "/runs/" + run_id)
        response.raise_for_status()
        return response.json()

    async def approve(self, run_id, event_id):
        return await self.post(
            self.environment_url + "/runs/" + run_id + "/signals",
            {
                "eventId": event_id,
                "name": "approved",
                "payload": {"approved": True},
            },
            expected=202,
        )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "trigger", "approve", "inspect"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    async with httpx.AsyncClient(
        base_url=os.environ["WEAVE_API_URL"],
        trust_env=False,
        headers={"Authorization": "Bearer " + os.environ["WEAVE_ACCESS_TOKEN"]},
    ) as client:
        host = HostProduct(client, state["scope"])
        if args.operation == "prepare":
            result = await host.prepare(
                os.environ["WEAVE_NATIVE_IMAGE_DIGEST"],
                os.environ["WEAVE_WORKER_IMAGE_DIGEST"],
                os.environ["WEAVE_EXTERNAL_ORIGIN"],
            )
            args.state.write_text(json.dumps(result, indent=2) + "\n")
            print("Host definitions, activation and trigger prepared")
        elif args.operation == "trigger":
            print(
                json.dumps(
                    await host.trigger(
                        state["trigger"]["id"], os.environ["WEAVE_WEBHOOK_SECRET"], str(uuid4()), {"customer": "demo"}
                    )
                )
            )
        elif args.operation == "approve":
            print(json.dumps(await host.approve(args.run_id, str(uuid4()))))
        else:
            print(json.dumps(await host.inspect(args.run_id), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
