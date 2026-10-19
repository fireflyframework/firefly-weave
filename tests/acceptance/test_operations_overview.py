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

"""J5: platform administration on the real platform, through its public API.

Runs only through scripts/acceptance.py (the run stage). The platform summary must answer with every section the
host may read and healthy background loops; a workflow whose step runs on an external worker then shows its
summary, steps, incident and worker through the operational reads. Studio's Operate pages are checked against
the same platform by the operate-real browser suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

pytestmark = pytest.mark.acceptance

# One name per test run, so the step can run again on a kept platform.
SUFFIX = uuid4().hex[:6]
TASK = "operations-echo-" + SUFFIX
CAPABILITY = TASK + "@1.0.0"
WORKFLOW = "operations-journey-" + SUFFIX
SCHEMA = {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}
# Loops that are on in this profile; the others answer "disabled".
LOOPS = {"recovery_loop", "schedules", "outbox_loop", "provider_loop", "operations_loop"}
SECTIONS = ("load", "workers", "targets", "incidents", "attention")
SUMMARY_SECONDS = 240


class Platform:
    """The run's platform: its API, Keycloak, the host token and the demo environment."""

    def __init__(self, run: Any) -> None:
        from firefly_weave.sdk.platform import _env_file

        state = run.platform_state()
        ports = state["ports"]
        self.api = f"http://127.0.0.1:{ports['container_api' if state.get('mode') == 'docker' else 'api']}"
        self.keycloak = f"http://localhost:{ports['keycloak']}"
        identity = _env_file(run.platform_dir / "identity.env")
        self.admin_secret, self.worker_secret = identity["WEAVE_KC_ADMIN_SECRET"], identity["WEAVE_WORKER_SECRET"]
        run.require(run.platform("token", "--output", "json"))
        self.token = json.loads((run.platform_dir / "host-token.json").read_text(encoding="utf-8"))["access_token"]
        person = json.loads((run.private / "people" / "builder.json").read_text(encoding="utf-8"))
        self.scope = next(grant["scope"] for grant in person["grants"] if "environment_id" in grant["scope"])
        self.project = f"/api/v1/tenants/{self.scope['tenant_id']}/projects/{self.scope['project_id']}"
        self.environment = self.project + f"/environments/{self.scope['environment_id']}"

    def client(self, token: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.api, trust_env=False, timeout=30, headers={"Authorization": "Bearer " + (token or self.token)}
        )


async def post(client: httpx.AsyncClient, path: str, body: dict, expected: int = 201) -> dict:
    response = await client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == expected, (path, response.status_code, response.text[:500])
    return dict(response.json())


async def get(client: httpx.AsyncClient, path: str, **params: Any) -> dict:
    response = await client.get(path, params=params)
    assert response.status_code == 200, (path, response.status_code, response.text[:500])
    return dict(response.json())


async def capabilities(platform: Platform) -> set[str]:
    """What the host's grants allow in the demo environment, as the identity operation reports them."""
    async with platform.client() as client:
        identity = await get(client, "/api/v1/identity")
    found: set[str] = set()
    for grant in identity["grants"]:
        scope = grant.get("scope") or {}
        keys = ("tenant_id", "project_id", "environment_id")
        if not grant.get("resources") and all(scope.get(key) in (None, platform.scope[key]) for key in keys):
            found.update(grant.get("capabilities", []))
    return found


async def healthy_summary(platform: Platform) -> dict:
    """The summary once the database and every loop of this profile report ok, within a bounded wait."""
    deadline = time.monotonic() + SUMMARY_SECONDS
    async with platform.client() as client:
        while True:
            summary = await get(client, platform.environment + "/operations/summary")
            health = {item["component"]: item for item in summary["health"]}
            ready = health["database"]["state"] == "ok" and all(
                health[name]["state"] == "ok" and health[name].get("last_ok_at") for name in LOOPS
            )
            if ready or time.monotonic() > deadline:
                return summary
            await asyncio.sleep(5)


async def worker_sign_in(platform: Platform) -> str:
    """A token of the weave-worker client, as an external worker signs in."""
    async with httpx.AsyncClient(trust_env=False, timeout=30) as identity:
        response = await identity.post(
            platform.keycloak + "/realms/weave/protocol/openid-connect/token",
            data={"grant_type": "client_credentials"},
            auth=("weave-worker", platform.worker_secret),
        )
    assert response.status_code == 200, "The worker client could not sign in"
    return str(response.json()["access_token"])


async def worker_journey(platform: Platform) -> dict:
    """Publish, release and activate a one-step workflow, then run it twice on an external worker:
    once to success, once to a failed step that opens an incident."""
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.members import MemberGrantRequest, PrincipalCreateRequest, PrincipalIdentityRequest
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.members import MembersClient
    from firefly_weave.sdk.platform_ai_setup import ensure_worker_scope, worker_subject
    from firefly_weave.sdk.transport import WorkerTransport
    from firefly_weave.sdk.worker import Worker

    capability = {
        "taskType": TASK,
        "taskVersion": "1.0.0",
        "inputSchema": SCHEMA,
        "outputSchema": {"type": "object"},
        "sideEffect": "idempotency_key",
        "timeoutSeconds": 60,
    }
    async with platform.client() as client:
        # The worker runs in this process; its release names a digest of no image.
        digest = "sha256:" + hashlib.sha256(("acceptance operations worker " + SUFFIX).encode()).hexdigest()
        release = await post(
            client, platform.environment + "/worker-releases", {"image_digest": digest, "capabilities": [capability]}
        )
        await post(
            client,
            platform.project + "/actions",
            {
                "format": "json",
                "source": json.dumps(
                    {
                        "apiVersion": "weave/v1alpha1",
                        "kind": "Action",
                        "metadata": {"name": TASK, "version": "1.0.0"},
                        "spec": {
                            "implementation": {"kind": "worker", "taskType": TASK, "taskVersion": "1.0.0"},
                            **{key: capability[key] for key in ("inputSchema", "outputSchema", "sideEffect")},
                            "timeoutSeconds": 60,
                            "retry": {"maxAttempts": 1, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
                        },
                    }
                ),
            },
        )
        version = await post(
            client,
            platform.project + "/workflows",
            {
                "format": "json",
                "source": json.dumps(
                    {
                        "apiVersion": "weave/v1alpha1",
                        "kind": "Workflow",
                        "metadata": {"name": WORKFLOW, "version": "1.0.0"},
                        "spec": {
                            "inputSchema": SCHEMA,
                            "outputSchema": {"type": "object"},
                            "timeoutSeconds": 300,
                            "steps": [{"id": "work", "kind": "action", "uses": CAPABILITY, "with": {"ref": "/input"}}],
                            "output": {"ref": "/steps/work/output"},
                        },
                    }
                ),
            },
        )
        await ensure_worker_scope(platform.keycloak, platform.admin_secret)
        worker_token = await worker_sign_in(platform)
        async with platform.client(worker_token) as worker_client:
            known = await worker_client.get("/api/v1/identity")
        async with WeaveClient(platform.api, lambda: platform.token, Scope.model_validate(platform.scope)) as weave:
            members = MembersClient(weave)
            if known.status_code == 200:
                # A step run earlier on this platform already linked the worker client.
                principal_id = UUID(known.json()["principal_id"])
            else:
                principal_id = (await members.create_principal(PrincipalCreateRequest(kind="worker"))).id
                subject = await worker_subject(platform.keycloak, platform.admin_secret)
                await members.link_identity(
                    principal_id,
                    PrincipalIdentityRequest(
                        provider_id="local-keycloak", issuer=platform.keycloak + "/realms/weave", subject=subject
                    ),
                )
            await members.grant(
                MemberGrantRequest(
                    principal_id=principal_id,
                    role="worker",
                    project_id=UUID(platform.scope["project_id"]),
                    environment_id=UUID(platform.scope["environment_id"]),
                    resources=(release["id"], CAPABILITY),
                )
            )
        activation = await post(
            client,
            platform.environment + "/activations",
            {
                "scope": platform.scope,
                "version_id": version["id"],
                "artifact_digest": version["digest"],
                "worker_release_ids": {TASK: release["id"]},
            },
        )
        runs = {
            outcome: (
                await post(
                    client,
                    platform.environment + "/runs",
                    {"activation_id": activation["id"], "input": {"customer": outcome}},
                )
            )["id"]
            for outcome in ("succeeds", "fails")
        }
    async with platform.client(worker_token) as client:
        worker = await post(
            client,
            platform.environment + "/workers",
            {"release_id": release["id"], "task_types": [CAPABILITY], "capacity": 1},
        )
        handled: list[str] = []

        async def handle(lease):
            handled.append(lease.input["customer"])
            if lease.input["customer"] == "fails":
                raise RuntimeError("planted failure")
            return {"customer": lease.input["customer"]}

        process = Worker(WorkerTransport(client, platform.environment, UUID(worker["id"])), {CAPABILITY: handle}, 1)
        running = asyncio.create_task(process.run())
        try:
            async with asyncio.timeout(120):
                while sorted(handled) != ["fails", "succeeds"]:
                    await asyncio.sleep(0.5)
                await asyncio.sleep(2)
        finally:
            await process.stop()
            await running
    return {"runs": runs, "worker": worker["id"], "release": release["id"]}


async def operational_reads(platform: Platform, journey: dict) -> dict:
    """The run summaries, steps, incident and worker the journey left, as the operational reads report them."""
    runs = journey["runs"]
    async with platform.client() as client:
        deadline = time.monotonic() + 60
        while True:
            listed = await get(client, platform.environment + "/run-summaries", workflow=WORKFLOW)
            statuses = {item["id"]: item.get("status") for item in listed["items"]}
            if statuses.get(runs["succeeds"]) == "succeeded" and statuses.get(runs["fails"]) == "suspended":
                break
            assert time.monotonic() < deadline, statuses
            await asyncio.sleep(1)
        steps = await get(client, platform.environment + f"/runs/{runs['succeeds']}/steps")
        incidents = await get(client, platform.environment + "/incidents", workflow=WORKFLOW)
        workers = await get(client, platform.environment + "/workers", release_id=journey["release"])
        summary = await get(client, platform.environment + "/operations/summary")
    return {"listed": listed, "steps": steps, "incidents": incidents, "workers": workers, "summary": summary}


@pytest.mark.journey_step("J5.1")
def test_j5_01_overview_and_operational_facts(run):
    platform = Platform(run)
    summary = asyncio.run(healthy_summary(platform))
    (run.evidence / "operations-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    health = {item["component"]: item["state"] for item in summary["health"]}
    assert health["database"] == "ok", health
    assert all(health[name] == "ok" for name in LOOPS), health
    assert all(state in ("ok", "disabled") for state in health.values()), health
    granted = asyncio.run(capabilities(platform))
    needs = {"targets": "deployment.read", "incidents": "incident.read"}
    for section in SECTIONS:
        value = summary[section]
        state = value.get("state") if isinstance(value, dict) else None
        assert state != "unavailable", section
        # A section is forbidden exactly when the host lacks the capability that reads it.
        assert (state == "forbidden") == (section in needs and needs[section] not in granted), (section, state)
    assert "runs_24h" not in summary and "runs_24h" not in json.dumps(summary)
    assert summary["api"]["compatibility"]["mode"] == "ready", summary["api"]

    journey = asyncio.run(worker_journey(platform))
    reads = asyncio.run(operational_reads(platform, journey))
    succeeded = next(item for item in reads["listed"]["items"] if item["id"] == journey["runs"]["succeeds"])
    assert succeeded["workflow"]["name"] == WORKFLOW
    assert succeeded["origin"] == "manual" and succeeded["test"] is False
    assert succeeded["started_at"] and succeeded["ended_at"] and succeeded["duration_ms"] is not None
    work = [item for item in reads["steps"]["items"] if item["node_id"] == "work"]
    assert len(work) == 1 and work[0]["status"] == "succeeded" and work[0]["worker_id"] == journey["worker"], work
    assert reads["steps"]["complete"] is True
    opened = [item for item in reads["incidents"]["items"] if item["run_id"] == journey["runs"]["fails"]]
    assert len(opened) == 1 and opened[0]["status"] == "active" and opened[0]["code"] == "WV-TASK-RETRIES-EXHAUSTED", (
        opened
    )
    assert opened[0]["workflow"]["name"] == WORKFLOW and opened[0].get("opened_at"), opened[0]
    listed = {item["id"]: item for item in reads["workers"]["items"]}
    assert journey["worker"] in listed and listed[journey["worker"]]["presence"] == "recent", listed
    assert reads["summary"]["incidents"]["active"] >= 1 and reads["summary"]["workers"]["online"] >= 1
    (run.evidence / "operations-journey.json").write_text(
        json.dumps({"runs": journey["runs"], "statuses": "succeeded, suspended"}, indent=2) + "\n", encoding="utf-8"
    )
