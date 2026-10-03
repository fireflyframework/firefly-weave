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

"""Real installed API process trials with independent committed-state observers."""

import asyncio
import importlib.util
import inspect
import json
import os
import signal
import socket
import sys
from collections import Counter
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

SUPPORT = Path(__file__).resolve().parent
ROOT = SUPPORT.parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


barriers = load("weave_matrix_barriers", SUPPORT / "barriers.py")
processes = load("weave_matrix_processes", SUPPORT / "processes.py")
base = load("weave_matrix_base", SUPPORT.parent / "conftest.py")


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def invariant_counts(committed, final, *, expected_event_types, deliveries=(), safe=True):
    """Derive violations from stored identities, sequence, and receiver attempts."""
    keys = [(row["run_id"], row["sequence"]) for row in final["events"]]
    duplicate_sequence = sum(count - 1 for count in Counter(keys).values())
    types = Counter(row["type"] for row in final["events"])
    duplicate_semantic = sum(max(0, types[kind] - count) for kind, count in expected_event_types.items())
    identities = {
        "events": ("run_id", "id"),
        "runs": ("id",),
        "tasks": ("id",),
        "completions": ("task_id", "completion_id"),
        "signals": ("id",),
    }
    lost = 0
    for collection, fields in identities.items():
        previous = {tuple(row[field] for field in fields) for row in committed.get(collection, [])}
        current = {tuple(row[field] for field in fields) for row in final.get(collection, [])}
        lost += len(previous - current)
    operation_attempts = Counter(row["operation_key"] for row in deliveries)
    return {
        "duplicate_accepted_transitions": duplicate_sequence + duplicate_semantic,
        "lost_committed_work": lost,
        "unsafe_blind_retries": sum(count - 1 for count in operation_attempts.values() if not safe),
    }


class InstalledSlice(base.VerticalSlice):
    """Retain all trial state; only stop processes created by this instance."""

    def __init__(self, directory):
        super().__init__(directory)
        self.owner = processes.ProcessOwner(directory)
        self.processes = self.owner.processes
        self.logs = self.owner.logs
        self.barrier_socket = None
        self.observer = None
        self.target_spec = None
        self.teams_jwks = None
        self.teams_receiver_origin = None
        self.artifact = Path(os.environ.get("WEAVE_E2E_WHEEL", ""))
        self.artifact_sha = os.environ.get("WEAVE_E2E_WHEEL_SHA256", "")

    async def command(self, *args, env=None, cwd=None):
        return (await self.owner.command(*args, env=env, cwd=cwd)).strip()

    async def spawn(self, name, args, env):
        return await self.owner.spawn(name, args, env)

    async def start_api(self, target=None, *, port=None):
        assert self.artifact.is_file() and len(self.artifact_sha) == 64, (
            "Exact release artifact required: WEAVE_E2E_WHEEL and WEAVE_E2E_WHEEL_SHA256"
        )
        port = self.port if port is None else port
        args = [
            self.python,
            "-I",
            str(SUPPORT / "installed_api.py"),
            "--wheel",
            str(self.artifact),
            "--wheel-sha256",
            self.artifact_sha,
            "--port",
            str(port),
        ]
        if self.teams_jwks is not None:
            args += ["--teams-jwks", str(self.teams_jwks)]
            if self.teams_receiver_origin is not None:
                args += ["--teams-receiver-origin", self.teams_receiver_origin]
        descriptors = ()
        child = None
        if target:
            path = self.directory / ("target-" + target["case"] + ".json")
            private_json(path, target)
            self.barrier_socket, child = socket.socketpair()
            self.barrier_socket.setblocking(False)
            descriptors = (child.fileno(),)
            args.extend(["--target", str(path), "--barrier-fd", str(child.fileno())])
        try:
            process = await self.owner.spawn(
                "api-" + str(len(self.processes)), args, self.api_env, pass_fds=descriptors
            )
        finally:
            if child:
                child.close()
        if port == self.port:
            self.api = process
            self.target_spec = target
            await self.ready()
        else:
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client, asyncio.timeout(30):
                while True:
                    assert process.returncode is None, "Replica exited before readiness"
                    try:
                        if (await client.get(f"http://127.0.0.1:{port}/health/ready")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
        return process

    async def replace_api(self, target=None):
        if self.api.returncode is None:
            self.api.terminate()
            await self.owner.joined(self.api, 15)
        if self.barrier_socket is not None:
            self.barrier_socket.close()
            self.barrier_socket = None
        await self.start_api(target)

    async def bootstrap(self):
        assert os.environ.get("WEAVE_TEST_DOCKER_CONTEXT") == "colima-weave-tests", "Explicit owned context required"
        base.BACKENDS["keycloak_endpoint"]()
        await super().bootstrap()
        self.observer = create_async_engine(self.owner_url, hide_parameters=True)
        async with self.observer.connect() as connection:
            assert await connection.scalar(text("SELECT rolsuper FROM pg_roles WHERE rolname=current_user")), (
                "Independent full-visibility test observer requires the existing owned administrator"
            )
        # The second real process is kept alive during every selected-process kill.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.replica_port = sock.getsockname()[1]
        self.replica = await self.start_api(port=self.replica_port)

    async def observe(self, correlation):
        async with self.observer.connect() as connection:
            params = {"project": self.scope.project_id, "correlation": correlation}
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT id,state->>'status' AS status,state->>'accepted_sequence' AS sequence "
                            "FROM runs WHERE project_id=:project "
                            "AND request->>'correlation_key'=:correlation ORDER BY id"
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
            runs = [{k: str(v) for k, v in row.items()} for row in rows]
            events, tasks, receipts, signals = [], [], [], []
            for run in runs:
                query = {"run": UUID(run["id"])}
                for output, sql in (
                    (events, "SELECT run_id,id,sequence,type FROM run_events WHERE run_id=:run ORDER BY sequence"),
                    (tasks, "SELECT id,operation_key,status FROM task_intents WHERE run_id=:run ORDER BY id"),
                    (
                        receipts,
                        "SELECT c.task_id,c.completion_id,c.generation FROM completion_receipts c "
                        "JOIN task_intents t ON t.id=c.task_id WHERE t.run_id=:run ORDER BY c.generation",
                    ),
                    (
                        signals,
                        "SELECT id,external_event_id,consumed FROM signal_receipts WHERE run_id=:run ORDER BY id",
                    ),
                ):
                    values = (await connection.execute(text(sql), query)).mappings().all()
                    output.extend({k: str(v) if isinstance(v, UUID) else v for k, v in row.items()} for row in values)
        return {"runs": runs, "events": events, "tasks": tasks, "completions": receipts, "signals": signals}

    async def kill_at_barrier(self):
        target = self.target_spec
        message = await barriers.read_message(self.barrier_socket, timeout=20)
        assert message == {
            "nonce": target["case"],
            "pid": self.api.pid,
            "phase": target["phase"],
            "detail": {"case": target["case"], "scope": target["scope"]},
        }, "Wrong process/case/phase/scope handshake; refusing to signal"
        return message

    async def kill_selected(self):
        assert self.api.returncode is None, "Selected process exited before kill"
        self.api.kill()
        assert await self.owner.joined(self.api, 10) == -signal.SIGKILL
        assert self.replica.returncode is None, "Unselected replica must remain alive"

    async def close(self):
        if self.closed:
            return
        failures = []
        operations = [self.owner.close]
        if self.barrier_socket is not None:
            operations.append(self.barrier_socket.close)
        if hasattr(self, "client"):
            operations.append(self.client.aclose)
        if self.engine:
            operations.append(self.engine.dispose)
        if self.observer:
            operations.append(self.observer.dispose)
        if self.target:
            operations.extend((self.target.close, self.target.wait_closed))
        for operation in operations:
            try:
                result = operation()
                if inspect.isawaitable(result):
                    await result
            except BaseException as error:
                failures.append(error)
        if self.containers:
            failures.append(AssertionError("Matrix must explicitly track any separately deployed workers"))
        self.closed = True
        if failures:
            raise BaseExceptionGroup("Owned matrix resource cleanup failed", failures)

    async def prepare_workflow(self, *, task=False, safe=True):
        tag = "matrix-" + uuid4().hex[:12]
        steps = [
            {
                "id": "approval",
                "kind": "signal",
                "name": "approved",
                "timeoutSeconds": 600,
                "payloadSchema": {"type": "object"},
            }
        ]
        release = None
        if task:
            from firefly_weave.access.models import Grant

            capability = {
                "taskType": tag,
                "taskVersion": "1.0.0",
                "sideEffect": "idempotent" if safe else "non_idempotent",
                "timeoutSeconds": 300,
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
            }
            release = await self.host.post(
                self.host.environment_url + "/worker-releases",
                {
                    "image_digest": self.worker_image,
                    "capabilities": [capability],
                },
            )
            await self.access.grant(
                self.admin,
                self.worker_id,
                Grant(
                    role="worker",
                    scope=self.scope,
                    resources=(release["id"], tag + "@1.0.0"),
                ),
            )
            await self.host.publish(
                "actions",
                {
                    "apiVersion": "weave/v1alpha1",
                    "kind": "Action",
                    "metadata": {"name": tag, "version": "1.0.0"},
                    "spec": {
                        "implementation": {"kind": "worker", "taskType": tag, "taskVersion": "1.0.0"},
                        **{k: capability[k] for k in ("sideEffect", "timeoutSeconds", "inputSchema", "outputSchema")},
                        "retry": {"maxAttempts": 4, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
                    },
                },
            )
            steps.insert(0, {"id": "work", "kind": "action", "uses": tag + "@1.0.0", "with": {"ref": "/input"}})
        version = await self.host.publish(
            "workflows",
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Workflow",
                "metadata": {"name": tag, "version": "1.0.0"},
                "spec": {
                    "inputSchema": {"type": "object"},
                    "outputSchema": {"type": "object"},
                    "timeoutSeconds": 900,
                    "steps": steps,
                    "output": {"ref": "/input"},
                },
            },
        )
        activation = await self.host.post(
            self.host.environment_url + "/activations",
            {
                "scope": self.scope.model_dump(mode="json"),
                "version_id": version["id"],
                "artifact_digest": version["digest"],
                "worker_release_ids": {tag: release["id"]} if release else {},
            },
        )
        return activation, release, tag

    async def commit_case(self, boundary, *, consecutive=1):
        correlation = str(uuid4())
        kind = boundary.split("_", 1)[1].removesuffix("_commit")
        activation, release, task_type = await self.prepare_workflow(task=kind == "completion")
        body = {"activation_id": activation["id"], "input": {}, "correlation_key": correlation}
        route = self.host.environment_url + "/runs"
        key = str(uuid4())
        headers = {"Idempotency-Key": key}
        if kind != "run":
            run = await self.host.post(route, body, headers=headers)
            if kind == "signal":
                route += "/" + run["id"] + "/signals"
                body = {"eventId": str(uuid4()), "name": "approved", "payload": {}}
            else:
                worker_headers = {"Authorization": "Bearer " + self.tokens[1]}
                worker = await self.host.post(
                    self.host.environment_url + "/workers",
                    {
                        "release_id": release["id"],
                        "task_types": [task_type + "@1.0.0"],
                        "capacity": 1,
                    },
                    headers=worker_headers,
                )
                leases = await self.host.post(
                    self.host.environment_url + "/tasks/claim",
                    {
                        "worker_id": worker["id"],
                        "limit": 1,
                    },
                    expected=200,
                    headers=worker_headers,
                )
                assert len(leases) == 1, "A real lease must exist before completion faults"
                body = {"lease": leases[0]["proof"], "completion_id": str(uuid4()), "output": {}}
                route = self.host.environment_url + "/tasks/complete"
                headers.update(worker_headers)
        before = await self.observe(correlation)
        crashes = []
        for _ in range(consecutive):
            target = {
                "case": str(uuid4()),
                "phase": boundary,
                "method": "POST",
                "path": route,
                "scope": self.scope.model_dump(mode="json"),
            }
            await self.replace_api(target)
            request = asyncio.create_task(
                self.client.post(
                    route,
                    json=body,
                    headers={
                        **headers,
                        "X-Weave-Crash-Case": target["case"],
                    },
                    timeout=25,
                )
            )
            try:
                message = await self.kill_at_barrier()
                committed = await self.observe(correlation)
                collection = {"run": "runs", "completion": "completions", "signal": "signals"}[kind]
                assert len(committed[collection]) == (1 if boundary.startswith("after_") else 0)
                if boundary.startswith("before_"):
                    assert committed == before, "Uncommitted mutation leaked to independent observer"
                await self.kill_selected()
                with __import__("contextlib").suppress(httpx.HTTPError):
                    await request
                crashes.append({"handshake": message, "committed": committed, "exit_code": self.api.returncode})
            finally:
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)
            # The after-commit replay stays the same logical identity across consecutive kills.
        await self.replace_api()
        result = await self.client.post(route, json=body, headers=headers)
        assert result.status_code == {"run": 201, "signal": 202, "completion": 200}[kind]
        replay = await self.client.post(route, json=body, headers=headers)
        assert replay.status_code == result.status_code and replay.json() == result.json()
        final = await self.observe(correlation)
        assert len(final["runs"]) == 1
        expected = {"started": 1, "task_completed": int(kind == "completion"), "signal_received": int(kind == "signal")}
        assert final["runs"][0]["status"] == ("succeeded" if kind == "signal" else "waiting")
        assert len(final["signals"]) == int(kind == "signal")
        assert len(final["completions"]) == int(kind == "completion")
        counts = invariant_counts(crashes[-1]["committed"], final, expected_event_types=expected)
        evidence = {
            "boundary": boundary,
            "correlation": correlation,
            "crashes": crashes,
            "final": final,
            "invariants": counts,
            "artifact_sha256": self.artifact_sha,
            "replicas": 2,
        }
        private_json(self.directory / ("case-" + correlation + ".json"), evidence)
        return evidence

    async def external_case(self, boundary, *, safe=True, consecutive=1):
        receiver_module = load("weave_matrix_receiver", SUPPORT / "effect_receiver.py")
        receiver = await receiver_module.EffectReceiver(self.directory / "receiver.sqlite3").open()
        try:
            correlation = str(uuid4())
            activation, release, task_type = await self.prepare_workflow(task=True, safe=safe)
            run = await self.host.post(
                self.host.environment_url + "/runs",
                {
                    "activation_id": activation["id"],
                    "input": {},
                    "correlation_key": correlation,
                },
            )
            worker_headers = {"Authorization": "Bearer " + self.tokens[1]}
            worker = await self.host.post(
                self.host.environment_url + "/workers",
                {
                    "release_id": release["id"],
                    "task_types": [task_type + "@1.0.0"],
                    "capacity": 1,
                },
                headers=worker_headers,
            )
            before = await self.observe(correlation)
            python = os.environ.get("WEAVE_E2E_WORKER_PYTHON", "")
            assert Path(python).is_file(), "Exact worker-only installed interpreter required: WEAVE_E2E_WORKER_PYTHON"
            crashes = []
            stale_proofs = []

            async def spawn_worker(phase):
                case = str(uuid4())
                config = {
                    "wheel": str(self.artifact),
                    "wheel_sha256": self.artifact_sha,
                    "case": case,
                    "api_url": f"http://127.0.0.1:{self.replica_port}",
                    "environment": self.host.environment_url,
                    "token": self.tokens[1],
                    "worker_id": worker["id"],
                    "receiver": receiver.url,
                    "completion_id": str(uuid4()),
                    "scope": self.scope.model_dump(mode="json"),
                    "result": str(self.directory / ("worker-result-" + case + ".json")),
                    "lease_result": str(self.directory / ("worker-lease-" + case + ".json")),
                    "phase": phase,
                }
                path = self.directory / ("worker-config-" + case + ".json")
                private_json(path, config)
                args = [python, "-I", str(SUPPORT / "external_worker.py"), "--config", str(path)]
                parent, child = socket.socketpair()
                parent.setblocking(False)
                if phase:
                    args += ["--barrier-fd", str(child.fileno())]
                try:
                    process = await self.owner.spawn(
                        "worker-" + case, args, self.base_env, pass_fds=(child.fileno(),) if phase else ()
                    )
                finally:
                    child.close()
                return process, parent, config

            for _ in range(consecutive):
                process, channel, config = await spawn_worker(boundary)
                try:
                    message = await barriers.read_message(channel, timeout=60)
                    assert message["nonce"] == config["case"] and message["pid"] == process.pid
                    assert message["phase"] == boundary and message["detail"]["scope"] == config["scope"]
                    stale_proofs.append(json.loads(Path(config["lease_result"]).read_bytes())["proof"])
                    snapshot = receiver.snapshot()
                    key = message["detail"]["operation_key"]
                    if boundary == "external_request_in_flight":
                        assert key not in snapshot["effects"]
                    else:
                        assert key in snapshot["effects"]
                    process.kill()
                    assert await self.owner.joined(process, 10) == -signal.SIGKILL
                    crashes.append({"handshake": message, "receiver": snapshot, "exit_code": process.returncode})
                finally:
                    channel.close()
            survivor, channel, config = await spawn_worker(None)
            channel.close()
            async with asyncio.timeout(90):
                while True:
                    final = await self.observe(correlation)
                    status = final["tasks"][0]["status"]
                    if status == ("completed" if safe else "incident"):
                        break
                    assert survivor.returncode is None, "Separate recovery worker exited before expected state"
                    await asyncio.sleep(0.2)
            if safe:
                assert await self.owner.joined(survivor, 10) == 0
            else:
                survivor.terminate()
                await self.owner.joined(survivor, 10)
            for proof in stale_proofs:
                response = await self.client.post(
                    self.host.environment_url + "/tasks/complete",
                    headers=worker_headers,
                    json={"lease": proof, "completion_id": str(uuid4()), "output": {}},
                )
                assert response.status_code == 409, "Expired/replaced proof must not advance the recovered task"
            generations = [item["handshake"]["detail"]["generation"] for item in crashes]
            assert generations == sorted(set(generations))
            accepted = receiver.snapshot()
            keys = {item["handshake"]["detail"]["operation_key"] for item in crashes}
            assert len(keys) == 1 and {v["operation_key"] for v in accepted["attempts"]} <= keys
            assert len(accepted["effects"]) == int(safe or boundary != "external_request_in_flight")
            assert len(final["completions"]) == int(safe)
            counts = invariant_counts(
                before,
                final,
                expected_event_types={"started": 1, "task_completed": int(safe)},
                deliveries=accepted["attempts"],
                safe=safe,
            )
            evidence = {
                "boundary": boundary,
                "safe": safe,
                "run_id": run["id"],
                "crashes": crashes,
                "receiver": accepted,
                "final": final,
                "invariants": counts,
                "artifact_sha256": self.artifact_sha,
                "replicas": 2,
                "separate_worker": True,
            }
            private_json(self.directory / ("external-" + correlation + ".json"), evidence)
            return evidence
        finally:
            await receiver.close()
