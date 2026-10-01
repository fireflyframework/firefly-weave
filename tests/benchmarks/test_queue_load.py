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

"""Measure a fixed real queue dataset without an invented performance threshold."""

import asyncio
import importlib.util
import json
import math
import os
import platform
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]
DATASET = 100
MAX_ADMISSION_ATTEMPTS = 720


def distribution(values):
    if not values or any(value < 0 or not math.isfinite(value) for value in values):
        raise ValueError("Finite nonnegative observations required")
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50_seconds": ordered[math.ceil(len(ordered) * 0.50) - 1],
        "p95_seconds": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max_seconds": ordered[-1],
    }


async def admit(client, path, body, key, attempts, deadline, *, max_attempts=MAX_ADMISSION_ATTEMPTS):
    """Retry only explicitly transient admission responses, preserving request identity."""
    content = json.dumps(body, separators=(",", ":")).encode()
    async with asyncio.timeout_at(deadline):
        for number in range(1, max_attempts + 1):
            attempt = {"idempotency_key": key, "attempt": number, "status": None, "code": None, "delay_seconds": 0.0}
            attempts.append(attempt)
            started = time.monotonic()
            try:
                response = await client.post(
                    path, content=content, headers={"Idempotency-Key": key, "Content-Type": "application/json"}
                )
            finally:
                attempt["request_seconds"] = time.monotonic() - started
            attempt["status"] = response.status_code
            try:
                value = response.json()
            except ValueError:
                value = None
            code = value.get("code") if isinstance(value, dict) else None
            attempt["code"] = code if isinstance(code, str) and len(code) <= 80 else None
            if response.status_code == 201:
                assert isinstance(value, dict) and isinstance(value.get("id"), str), "Queue admission receipt invalid"
                return value["id"]
            assert response.status_code == 429 and code in {"WV-REQUEST-CAPACITY", "WV-OPERATION-CAPACITY"}, (
                "Queue admission failed without an explicitly transient capacity response"
            )
            assert number < max_attempts, "Queue admission retry attempt limit reached"
            started = time.monotonic()
            try:
                await asyncio.sleep(min(0.025 * 2 ** min(number - 1, 4), 0.25))
            finally:
                attempt["delay_seconds"] = time.monotonic() - started
    raise AssertionError("Queue admission attempt limit must be positive")


def load_matrix():
    path = ROOT / "tests/e2e/support/crash_matrix.py"
    spec = importlib.util.spec_from_file_location("queue_matrix", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def prepare(slice):
    from firefly_weave.access.models import Grant

    name = "queue-" + uuid4().hex[:12]
    schema = {"type": "object", "properties": {"customer": {"type": "string"}}, "required": ["customer"]}
    capability = {
        "taskType": name,
        "taskVersion": "1.0.0",
        "inputSchema": schema,
        "outputSchema": {"type": "object"},
        "sideEffect": "idempotency_key",
        "timeoutSeconds": 180,
    }
    release = await slice.host.post(
        slice.host.environment_url + "/worker-releases",
        {"image_digest": slice.worker_image, "capabilities": [capability]},
    )
    await slice.access.grant(
        slice.admin,
        slice.worker_id,
        Grant(role="worker", scope=slice.scope, resources=(release["id"], name + "@1.0.0")),
    )
    await slice.host.publish(
        "actions",
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Action",
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": {
                "implementation": {"kind": "worker", "taskType": name, "taskVersion": "1.0.0"},
                **{k: capability[k] for k in ("inputSchema", "outputSchema", "sideEffect", "timeoutSeconds")},
                "retry": {"maxAttempts": 2, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
            },
        },
    )
    version = await slice.host.publish(
        "workflows",
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": {
                "inputSchema": schema,
                "outputSchema": {"type": "object"},
                "timeoutSeconds": 300,
                "steps": [{"id": "work", "kind": "action", "uses": name + "@1.0.0", "with": {"ref": "/input"}}],
                "output": {"ref": "/steps/work/output"},
            },
        },
    )
    activation = await slice.host.post(
        slice.host.environment_url + "/activations",
        {
            "scope": slice.scope.model_dump(mode="json"),
            "version_id": version["id"],
            "artifact_digest": version["digest"],
            "worker_release_ids": {name: release["id"]},
        },
    )
    return name, release, activation


@pytest.mark.parametrize("replicas,concurrency", [(1, 1), (1, 4), (2, 1), (2, 4)])
async def test_fixed_queue_dataset(replicas, concurrency, tmp_path):
    matrix = load_matrix()
    assert os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") == "colima-weave-tests", "Owned backend required"
    python = Path(os.environ.get("WEAVE_E2E_WORKER_PYTHON", ""))
    assert python.is_file(), "Separate clean installed worker-only Python is required"
    destination = Path(os.environ.get("WEAVE_D5_EVIDENCE", str(tmp_path))) / f"queue-{replicas}-{concurrency}"
    destination.mkdir(mode=0o700)
    slice = matrix.InstalledSlice(destination)
    try:
        await slice.bootstrap()
        if replicas == 1:
            slice.replica.terminate()
            await slice.owner.joined(slice.replica, 15)
        name, release, activation = await prepare(slice)
        workers = []
        count = 2 if concurrency == 4 else 1
        for index in range(count):
            config = {
                "wheel": str(slice.artifact),
                "wheel_sha256": slice.artifact_sha,
                "api_url": slice.api_url if index % replicas == 0 else f"http://127.0.0.1:{slice.replica_port}",
                "environment_url": slice.host.environment_url,
                "token_url": slice.token_url,
                "release_id": release["id"],
                "capability": name + "@1.0.0",
                "concurrency": concurrency // count,
                "effect_url": f"http://127.0.0.1:{slice.target_port}/effect",
            }
            path = destination / f"worker-{index}.json"
            matrix.private_json(path, config)
            process = await slice.spawn(
                f"worker-{index}",
                [str(python), "-I", str(ROOT / "tests/e2e/support/queue_worker.py"), "--config", str(path)],
                {**slice.base_env, "WEAVE_WORKER_SECRET": os.environ["WEAVE_WORKER_SECRET"]},
            )
            workers.append(process)
            async with asyncio.timeout(30):
                while True:
                    assert process.returncode is None, "Measured worker exited before readiness"
                    if '"ready": true' in (destination / f"worker-{index}.log").read_text():
                        break
                    await asyncio.sleep(0.05)
        started = time.monotonic()
        runs, attempts, logical_requests = [], [], []
        deadline = asyncio.get_running_loop().time() + 180
        semaphore = asyncio.Semaphore(10)
        clients = [slice.client]
        if replicas == 2:
            clients.append(
                httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{slice.replica_port}",
                    trust_env=False,
                    timeout=15,
                    headers={"Authorization": "Bearer " + slice.tokens[0]},
                )
            )

        async def start(index):
            body = {"activation_id": activation["id"], "input": {"customer": f"record-{index:03}"}}
            key = str(uuid4())
            logical_requests.append({"index": index, "idempotency_key": key, "body": body})
            async with semaphore:
                identifier = await admit(
                    clients[index % len(clients)], slice.host.environment_url + "/runs", body, key, attempts, deadline
                )
                runs.append(identifier)

        terminal = {}
        try:
            async with asyncio.timeout_at(deadline):
                async with asyncio.TaskGroup() as admissions:
                    for index in range(DATASET):
                        admissions.create_task(start(index))
                while len(terminal) != DATASET:
                    assert all(process.returncode is None for process in workers), (
                        "Measured worker exited during dataset"
                    )
                    for identifier in runs:
                        if identifier in terminal:
                            continue
                        response = await slice.client.get(slice.host.environment_url + "/runs/" + identifier)
                        assert response.status_code == 200
                        state = response.json()["state"]
                        if state["status"] in {"succeeded", "failed", "timed_out", "suspended", "cancelled"}:
                            terminal[identifier] = state["status"]
                    await asyncio.sleep(0.05)
        finally:
            elapsed = time.monotonic() - started
            matrix.private_json(
                destination / "admission-attempts.json",
                {
                    "logical_requests": logical_requests,
                    "attempts": attempts,
                    "run_ids": runs,
                    "terminal": terminal,
                    "elapsed_seconds": elapsed,
                    "worker_returncodes": [process.returncode for process in workers],
                    "dataset_deadline_seconds": 180,
                    "max_attempts_per_record": MAX_ADMISSION_ATTEMPTS,
                },
            )
            for client in clients[1:]:
                await client.aclose()
        for process in workers:
            process.terminate()
            assert await slice.owner.joined(process, 20) == 0
        assert all(status == "succeeded" for status in terminal.values())
        async with slice.observer.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT t.id,t.run_id,t.operation_key,l.generation,l.claimed_at,t.enqueued_at,"
                            "s.created_at AS admitted_at,"
                            "e.created_at AS completed_at FROM task_intents t JOIN task_leases l ON l.task_id=t.id "
                            "JOIN run_events s ON s.run_id=t.run_id AND s.type='started' "
                            "JOIN run_events e ON e.run_id=t.run_id AND e.type='task_completed' "
                            "WHERE t.project_id=:project ORDER BY t.id,l.generation"
                        ),
                        {"project": slice.scope.project_id},
                    )
                )
                .mappings()
                .all()
            )
            pg_version = await connection.scalar(text("SHOW server_version"))
        assert len(rows) == DATASET
        assert len({row["run_id"] for row in rows}) == DATASET
        assert all(row["generation"] == 1 for row in rows)
        assert len(slice.effects) == len(slice.deliveries) == DATASET
        assert all(row["enqueued_at"] is not None for row in rows)
        queue = [(row["claimed_at"] - row["enqueued_at"]).total_seconds() for row in rows]
        total = [(row["completed_at"] - row["admitted_at"]).total_seconds() for row in rows]
        docker_info = json.loads(await slice.docker("info", "--format", "{{json .}}"))
        proof = {
            "dataset": DATASET,
            "replicas": replicas,
            "concurrency": concurrency,
            "worker_processes": count,
            "accepted": len(runs),
            "admission_attempts": len(attempts),
            "admission_capacity_rejections": sum(item["status"] == 429 for item in attempts),
            "admission_retry_delay_seconds": sum(item["delay_seconds"] for item in attempts),
            "elapsed_includes_admission_backpressure": True,
            "succeeded": len(terminal),
            "retries": sum(row["generation"] - 1 for row in rows),
            "effects": len(slice.effects),
            "elapsed_seconds": elapsed,
            "accepted_completions_per_second": DATASET / elapsed,
            "enqueue_to_first_claim": distribution(queue),
            "start_to_terminal": distribution(total),
            "timestamp_basis": (
                "database task enqueued_at / first lease claimed_at / run started and task_completed events"
            ),
            "wheel_sha256": slice.artifact_sha,
            "server_image": slice.image,
            "worker_image": slice.worker_image,
            "artifact_digest": activation["request"]["artifact_digest"],
            "postgres_version": pg_version,
            "api_python": slice.python,
            "worker_python": str(python),
            "host_platform": platform.platform(),
            "host_cpus": os.cpu_count(),
            "docker_cpus": docker_info["NCPU"],
            "docker_memory_bytes": docker_info["MemTotal"],
            "run_ids": runs,
            "task_ids": [str(row["id"]) for row in rows],
            "database": str(__import__("sqlalchemy").make_url(slice.owner_url).database),
        }
        matrix.private_json(destination / "measurement.json", proof)
    finally:
        await slice.close()
