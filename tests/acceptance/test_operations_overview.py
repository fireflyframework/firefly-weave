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
from pathlib import Path
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


def incident_settled(incidents: dict, run_id: str) -> bool:
    """Whether the run's incident has its final code. The worker's failure opens WV-TASK-FAILED, and the recovery
    scan turns it into WV-TASK-RETRIES-EXHAUSTED once no retry is left; the run is suspended from the first code."""
    return any(item["run_id"] == run_id and item["code"] == "WV-TASK-RETRIES-EXHAUSTED" for item in incidents["items"])


async def operational_reads(platform: Platform, journey: dict) -> dict:
    """The run summaries, steps, incident and worker the journey left, as the operational reads report them."""
    runs = journey["runs"]
    async with platform.client() as client:
        # Long enough for the recovery scan to settle the failing run's incident.
        deadline = time.monotonic() + 180
        while True:
            listed = await get(client, platform.environment + "/run-summaries", workflow=WORKFLOW)
            statuses = {item["id"]: item.get("status") for item in listed["items"]}
            incidents = await get(client, platform.environment + "/incidents", workflow=WORKFLOW)
            if (
                statuses.get(runs["succeeds"]) == "succeeded"
                and statuses.get(runs["fails"]) == "suspended"
                and incident_settled(incidents, runs["fails"])
            ):
                break
            assert time.monotonic() < deadline, (statuses, [item["code"] for item in incidents["items"]])
            await asyncio.sleep(1)
        steps = await get(client, platform.environment + f"/runs/{runs['succeeds']}/steps")
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
    # The last day's runs come from run statistics: counts, and a rate that is a fraction or null.
    day = summary["runs_24h"]
    assert set(day) == {"started", "succeeded", "failed", "failure_rate"}, day
    assert all(isinstance(day[key], int) and day[key] >= 0 for key in ("started", "succeeded", "failed")), day
    assert day["failure_rate"] is None or 0 <= day["failure_rate"] <= 1, day
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


# J5.2–J5.7 run on the fixture worker J0.10 started on local-docker. order-review-lite is this journey's own
# one-step workflow on the fixture's example-sleep task: the reference catalog has no lite variant.
LITE = "order-review-lite-" + SUFFIX
LITE_ACTION = "order-review-lite-sleep-" + SUFFIX
SLEEP_TASK = "example-sleep"
FIXTURE_TTL_SECONDS = 60


def fixture_worker(run: Any) -> tuple[str, str]:
    """The fixture worker J0.10 brought online, and the release local operations admitted for it."""
    worker = run.private_record("fixture-worker")["worker_id"]
    release = json.loads((run.platform_dir / "operations.json").read_text(encoding="utf-8"))["release_id"]
    return str(worker), str(release)


async def lite_activation(
    platform: Platform, release: str, *, name: str = LITE, action: str = LITE_ACTION, timeout_seconds: int = 300
) -> dict:
    """Publish and activate order-review-lite (or a copy named ``name``): one step that sleeps on the fixture worker."""
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "tests/fixtures/operations-worker/manifest.json").read_text(
            encoding="utf-8"
        )
    )["capabilities"][0]
    async with platform.client() as client:
        await post(
            client,
            platform.project + "/actions",
            {
                "format": "json",
                "source": json.dumps(
                    {
                        "apiVersion": "weave/v1alpha1",
                        "kind": "Action",
                        "metadata": {"name": action, "version": "1.0.0"},
                        "spec": {
                            "implementation": {"kind": "worker", "taskType": SLEEP_TASK, "taskVersion": "1.0.0"},
                            **{key: manifest[key] for key in ("inputSchema", "outputSchema", "sideEffect")},
                            "timeoutSeconds": manifest["timeoutSeconds"],
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
                        "metadata": {"name": name, "version": "1.0.0"},
                        "spec": {
                            "inputSchema": manifest["inputSchema"],
                            "outputSchema": {"type": "object"},
                            "timeoutSeconds": timeout_seconds,
                            "steps": [
                                {
                                    "id": "sleep",
                                    "kind": "action",
                                    "uses": action + "@1.0.0",
                                    "with": {"ref": "/input"},
                                }
                            ],
                            "output": {"ref": "/steps/sleep/output"},
                        },
                    }
                ),
            },
        )
        return await post(
            client,
            platform.environment + "/activations",
            {
                "scope": platform.scope,
                "version_id": version["id"],
                "artifact_digest": version["digest"],
                "worker_release_ids": {SLEEP_TASK: release},
            },
        )


async def control(run: Any, worker: str, action: str) -> Any:
    """Drain or resume the fixture worker as the operator, who holds worker.drain (the host account does not)."""
    from firefly_weave.contracts.workers import WorkerControlRequest

    async with run.client("operator") as operator:
        current = await operator.invoke("workers.read", identifier=UUID(worker))
        return await operator.invoke(
            "workers." + action,
            identifier=UUID(worker),
            body=WorkerControlRequest(expected_revision=current.revision),
            idempotency_key=str(uuid4()),
        )


async def drained_runs(run: Any, platform: Platform, worker: str, release: str) -> dict:
    activation = await lite_activation(platform, release)
    drained = await control(run, worker, "drain")
    assert drained.draining is True
    async with platform.client() as client:
        runs = [
            (
                await post(
                    client, platform.environment + "/runs", {"activation_id": activation["id"], "input": {"ms": 500}}
                )
            )["id"]
            for _ in range(5)
        ]
        # While the only worker drains, the five tasks wait and the oldest grows older.
        ages = []
        deadline = time.monotonic() + 60
        while len(ages) < 2:
            load = (await get(client, platform.environment + "/operations/summary"))["load"]
            if load["tasks_ready"] >= 5 and load["oldest_ready_seconds"] is not None:
                ages.append(load["oldest_ready_seconds"])
                await asyncio.sleep(3)
            else:
                assert time.monotonic() < deadline, load
                await asyncio.sleep(1)
        assert ages[1] > ages[0], ages
        await control(run, worker, "resume")
        resumed = time.monotonic()
        while (await get(client, platform.environment + "/operations/summary"))["load"]["tasks_ready"]:
            assert time.monotonic() - resumed < 60, "ready tasks remain 60 s after the worker resumed"
            await asyncio.sleep(1)
        deadline = time.monotonic() + 120
        while True:
            listed = await get(client, platform.environment + "/run-summaries", workflow=LITE)
            statuses = {item["id"]: item.get("status") for item in listed["items"]}
            if all(statuses.get(run) == "succeeded" for run in runs):
                break
            assert time.monotonic() < deadline, statuses
            await asyncio.sleep(1)
    return {"runs": runs, "ages": ages}


@pytest.mark.journey_step("J5.2")
def test_j5_02_a_drained_worker_holds_ready_tasks_until_it_resumes(run):
    platform = Platform(run)
    worker, release = fixture_worker(run)
    journey = asyncio.run(drained_runs(run, platform, worker, release))
    (run.evidence / "operations-drain.json").write_text(json.dumps(journey, indent=2) + "\n", encoding="utf-8")


# J5.4 counts its own copy of order-review-lite, so the runs of J5.2 never change its figures. A run of it times
# out after RUN_TIMEOUT seconds: three planted runs sleep longer and end as timed out, which counts as a failure.
METRICS_LITE = "order-review-lite-metrics-" + SUFFIX
METRICS_ACTION = "order-review-lite-metrics-sleep-" + SUFFIX
RUN_TIMEOUT = 10
STATISTICS_SECONDS = 180


async def statistics(client: httpx.AsyncClient, platform: Platform, read: str, **params: Any) -> dict:
    response = await client.get(platform.environment + "/run-statistics/" + read, params=params)
    assert response.status_code == 200, (read, response.status_code, response.text[:500])
    return dict(response.json())


async def counted_runs(platform: Platform, release: str) -> dict:
    """Twenty runs, three of them timed out; the statistics count them as the run list does."""
    from datetime import UTC, datetime, timedelta

    activation = await lite_activation(
        platform, release, name=METRICS_LITE, action=METRICS_ACTION, timeout_seconds=RUN_TIMEOUT
    )
    began = datetime.now(UTC) - timedelta(minutes=1)
    async with platform.client() as client:

        async def start(inputs: list[dict]) -> list[str]:
            return [
                (
                    await post(
                        client, platform.environment + "/runs", {"activation_id": activation["id"], "input": item}
                    )
                )["id"]
                for item in inputs
            ]

        async def ended(runs: list[str], status: str) -> None:
            deadline = time.monotonic() + 180
            while True:
                listed = await get(client, platform.environment + "/run-summaries", workflow=METRICS_LITE, limit=100)
                statuses = {item["id"]: item.get("status") for item in listed["items"]}
                if all(statuses.get(run) == status for run in runs):
                    return
                assert time.monotonic() < deadline, statuses
                await asyncio.sleep(1)

        # The short runs first, so none waits for a slot long enough to time out; then the three that time out.
        short = await start([{"ms": 50}] * 17)
        await ended(short, "succeeded")
        planted = await start([{"ms": RUN_TIMEOUT * 3000}] * 3)
        await ended(planted, "timed_out")
        listed = await get(client, platform.environment + "/run-summaries", workflow=METRICS_LITE, limit=100)
        statuses = [item.get("status") for item in listed["items"]]
        window = {"from": began.isoformat().replace("+00:00", "Z")}
        # The rollup recomputes the last ten minutes every cycle: wait until it counted every run.
        deadline = time.monotonic() + STATISTICS_SECONDS
        while True:
            window["to"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            table = await statistics(client, platform, "workflows", **window)
            row = next((item for item in table["workflows"] if item["workflow"] == METRICS_LITE), None)
            if row is not None and row["runs"] == 20:
                break
            assert time.monotonic() < deadline, row
            await asyncio.sleep(5)
        series = await statistics(
            client,
            platform,
            "series",
            **window,
            workflow=METRICS_LITE,
            signals=["runs_started", "runs_succeeded", "runs_failed", "runs_cancelled"],
        )
        steps = await statistics(client, platform, "steps", **window, workflow=METRICS_LITE)
    totals = {
        item["signal"]: sum(value or 0 for _, value in item["points"])
        for item in series["series"]
        if not item["labels"]
    }
    return {
        "row": row,
        "totals": totals,
        "statuses": statuses,
        "steps": steps["steps"],
        "complete_until": table["complete_until"],
    }


@pytest.mark.journey_step("J5.4")
def test_j5_04_metrics_count_runs_failures_and_durations_as_the_run_list_does(run):
    platform = Platform(run)
    _, release = fixture_worker(run)
    counted = asyncio.run(counted_runs(platform, release))
    row, totals, statuses = counted["row"], counted["totals"], counted["statuses"]
    # The run list and the statistics agree: 20 started, 17 succeeded, 3 timed out, which count as failed.
    assert sorted(statuses) == ["succeeded"] * 17 + ["timed_out"] * 3, statuses
    assert (row["runs"], row["succeeded"], row["failed"], row["cancelled"]) == (20, 17, 3, 0), row
    assert row["failure_rate"] == pytest.approx(0.15), row
    assert totals == {"runs_started": 20, "runs_succeeded": 17, "runs_failed": 3, "runs_cancelled": 0}, totals
    assert row["duration_p50_ms"] is not None and row["duration_p95_ms"] >= row["duration_p50_ms"], row
    (step,) = counted["steps"]
    assert step["node_id"] == "sleep" and step["kind"] == "action", step
    assert step["duration_p50_ms"] is not None and step["duration_p95_ms"] is not None, step
    (run.evidence / "operations-metrics.json").write_text(json.dumps(counted, indent=2) + "\n", encoding="utf-8")


def fixture_container(run: Any) -> str:
    project = f"weave-local-{run.platform_state()['id']}-workers"
    (container,) = run.require(
        run.docker(
            "ps",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=fixture-worker",
        )
    ).stdout.split()
    return container


async def worker_view(platform: Platform, worker: str) -> dict:
    async with platform.client() as client:
        return await get(client, platform.environment + f"/workers/{worker}")


@pytest.mark.journey_step("J5.3")
def test_j5_03_workers_show_placement_load_contact_and_go_stale_when_their_container_stops(run):
    platform = Platform(run)
    worker, release = fixture_worker(run)
    seen = asyncio.run(worker_view(platform, worker))
    placement = seen.get("placement") or {}
    assert (placement.get("target_name"), placement.get("component")) == ("local-docker", "fixture-worker"), seen
    assert seen["release_id"] == release and seen["task_types"] == ["example-sleep@1.0.0"]
    assert seen["capacity"] == 2 and 0 <= seen["active_leases"] <= seen["capacity"]
    assert seen["presence"] == "recent"
    from datetime import datetime

    contact = datetime.fromisoformat(seen["observed_at"]) - datetime.fromisoformat(seen["last_seen_at"])
    assert contact.total_seconds() <= 10, contact
    # Stopped outside the runner, the worker stops reporting: stale after its TTL.
    run.require(run.docker("stop", "--time", "30", fixture_container(run)))
    deadline = time.monotonic() + FIXTURE_TTL_SECONDS + 60
    while asyncio.run(worker_view(platform, worker))["presence"] != "stale":
        assert time.monotonic() < deadline, "the stopped worker is not stale after its TTL"
        time.sleep(3)

    # The Workers page's offline filter lists it. The overview's Needs attention band lists an offline worker only
    # while it still holds active leases, and this one finished its tasks when it was stopped.
    async def offline() -> set[str]:
        async with platform.client() as client:
            listed = await get(client, platform.environment + "/workers", presence="stale", limit=100)
        return {item["id"] for item in listed["items"]}

    assert worker in asyncio.run(offline())


async def incidents_inbox(platform: Platform) -> dict:
    """J5.1's failed run left an active incident: the inbox lists it by filter; terminating it moves it to resolved."""
    async with platform.client() as client:
        active = await get(client, platform.environment + "/incidents", workflow=WORKFLOW, status="active")
        (incident,) = [item for item in active["items"] if item["code"] == "WV-TASK-RETRIES-EXHAUSTED"]
        coded = await get(client, platform.environment + "/incidents", code="WV-TASK-RETRIES-EXHAUSTED")
        assert incident["id"] in {item["id"] for item in coded["items"]}
        response = await client.post(
            platform.environment + f"/incidents/{incident['id']}/resolve",
            json={"receipt_id": str(uuid4()), "kind": "terminate", "reason": "Acceptance: the order was withdrawn"},
            headers={"If-Match": f'"{incident["revision"]}"', "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 200, response.text[:500]
        deadline = time.monotonic() + 60
        while True:
            still = await get(client, platform.environment + "/incidents", workflow=WORKFLOW, status="active")
            ended = {
                item["id"]
                for status in ("resolved", "closed")
                for item in (await get(client, platform.environment + "/incidents", workflow=WORKFLOW, status=status))[
                    "items"
                ]
            }
            if incident["id"] not in {item["id"] for item in still["items"]} and incident["id"] in ended:
                return {"incident": incident["id"]}
            assert time.monotonic() < deadline, "the terminated incident did not move to resolved"
            await asyncio.sleep(1)


@pytest.mark.journey_step("J5.7")
def test_j5_07_the_incidents_inbox_filters_and_resolves(run):
    result = asyncio.run(incidents_inbox(Platform(run)))
    (run.evidence / "operations-incidents.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
