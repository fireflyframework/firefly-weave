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

"""Guarded complete restore with original lease/deadline authority and durable receiver."""

import asyncio
import hashlib
import importlib.util
import json
import os
import shlex
import sys
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

SUPPORT = Path(__file__).resolve().parent
ROOT = SUPPORT.parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


matrix = load("restore_installed_matrix", SUPPORT / "crash_matrix.py")
backup = load("restore_operator", ROOT / "scripts/restore_database.py")
providers = load("restore_providers", SUPPORT / "provider_matrix.py")
receivers = load("restore_receivers", SUPPORT / "effect_receiver.py")


predecessor_access = load("restore_predecessor_access", SUPPORT / "predecessor_access.py")


class PredecessorSlice(matrix.InstalledSlice):
    async def provision_access(self, keycloak):
        from uuid import UUID

        result = await predecessor_access.invoke(
            self.python,
            self.artifact,
            self.artifact_sha,
            self.directory,
            {"operation": "bootstrap", "runtime": self.runtime, "keycloak": keycloak, "tag": self.tag},
        )
        for name in ("host_id", "worker_id", "native_id", "admin_id"):
            setattr(self, name, UUID(result[name]))
        self.other_tenant = result["other_tenant"]
        self.tokens, self.token_url = result["tokens"], result["token_url"]
        self.migration_head = result["head"]
        self.engine = create_async_engine(self.runtime["WEAVE_DATABASE_URL"], hide_parameters=True)
        return UUID(result["tenant_id"])

    async def grant_principal(self, principal, grant):
        return await predecessor_access.invoke(
            self.python,
            self.artifact,
            self.artifact_sha,
            self.directory,
            {
                "operation": "grant",
                "runtime": self.runtime,
                "admin_id": str(self.admin_id),
                "principal_id": str(principal),
                "grant": grant.model_dump(mode="json"),
            },
        )


async def workflow(trial, name, steps, *, releases=None):
    value = await trial.host.publish(
        "workflows",
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": name, "version": "1.0.0"},
            "spec": {
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object"},
                "timeoutSeconds": 900,
                "steps": steps,
                "output": {"ref": "/input"},
            },
        },
    )
    return await trial.host.post(
        trial.host.environment_url + "/activations",
        {
            "scope": trial.scope.model_dump(mode="json"),
            "version_id": value["id"],
            "artifact_digest": value["digest"],
            "worker_release_ids": releases or {},
        },
    )


async def seed_core(trial):
    signal_activation, _, _ = await trial.prepare_workflow()
    signal_run = await trial.host.post(
        trial.host.environment_url + "/runs",
        {"activation_id": signal_activation["id"], "input": {}, "correlation_key": "restore-signal"},
    )
    timer_activation = await workflow(trial, "restore-timer", [{"id": "delay", "kind": "wait", "durationSeconds": 30}])
    schedule = await trial.host.post(
        trial.host.environment_url + "/schedules",
        {
            "cron": "* * * * *",
            "timezone": "UTC",
            "missed_policy": "skip",
            "activation_id": signal_activation["id"],
            "input": {},
        },
    )
    from firefly_weave.access.models import Grant

    capabilities = [
        {
            "taskType": "restore-safe" if safe else "restore-unsafe",
            "taskVersion": "1.0.0",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "sideEffect": "idempotency_key" if safe else "non_idempotent",
            "timeoutSeconds": 300,
        }
        for safe in (True, False)
    ]
    release = await trial.host.post(
        trial.host.environment_url + "/worker-releases",
        {"image_digest": trial.worker_image, "capabilities": capabilities},
    )
    await trial.grant_principal(
        trial.worker_id,
        Grant(
            role="worker",
            scope=trial.scope,
            resources=(release["id"], *(capability["taskType"] + "@1.0.0" for capability in capabilities)),
        ),
    )
    tasks = []
    for safe, capability in zip((True, False), capabilities, strict=True):
        name = capability["taskType"]
        await trial.host.publish(
            "actions",
            {
                "apiVersion": "weave/v1alpha1",
                "kind": "Action",
                "metadata": {"name": name, "version": "1.0.0"},
                "spec": {
                    "implementation": {"kind": "worker", "taskType": name, "taskVersion": "1.0.0"},
                    **{key: capability[key] for key in ("inputSchema", "outputSchema", "sideEffect", "timeoutSeconds")},
                    "retry": {"maxAttempts": 3, "initialDelaySeconds": 1, "maxDelaySeconds": 1},
                },
            },
        )
        activation = await workflow(
            trial,
            name,
            [
                {"id": "work", "kind": "action", "uses": name + "@1.0.0", "with": {"ref": "/input"}},
                {
                    "id": "approval",
                    "kind": "signal",
                    "name": "approved",
                    "timeoutSeconds": 600,
                    "payloadSchema": {"type": "object"},
                },
            ],
            releases={name: release["id"]},
        )
        run = await trial.host.post(
            trial.host.environment_url + "/runs",
            {"activation_id": activation["id"], "input": {}, "correlation_key": name},
        )
        worker = await trial.host.post(
            trial.host.environment_url + "/workers",
            {"release_id": release["id"], "task_types": [name + "@1.0.0"], "capacity": 1},
            headers={"Authorization": "Bearer " + trial.tokens[1]},
        )
        tasks.append({"safe": safe, "run": run, "activation": activation, "worker": worker})
    return {"signal": signal_run, "timer_activation": timer_activation, "schedule": schedule, "tasks": tasks}


async def seed_providers(trial):
    stored = []
    for provider in ("teams", "whatsapp"):
        source, body, headers, source_at = await providers.prepare(trial, provider, matrix)
        raw = json.dumps(body).encode()
        route = "/provider-ingress/" + source["id"]
        response = await trial.client.post(route, content=raw, headers=headers(raw))
        assert response.status_code in {200, 202}
        item = {"provider": provider, "source": source, "body": body, "headers": headers, "route": route}
        if provider == "teams":
            await providers.set_scheduler(trial, True)
            async with asyncio.timeout(30):
                while not any(
                    row["state"] == "dispatched" for row in (await providers.rows(trial, source))["receipts"]
                ):
                    await asyncio.sleep(0.1)
            removal = json.dumps(
                {**body, "type": "installationUpdate", "action": "remove", "id": "restore-remove"}
            ).encode()
            for _ in range(2):
                assert (await trial.client.post(route, content=removal, headers=headers(removal))).status_code == 202
            state = await providers.rows(trial, source)
            reference = state["teams"][0]
            assert reference["state"] == "revoked" and len(state["lifecycle"]) == 1
            replacement = await source_at(2)
            command = {"expected_generation": 1, "source_id": replacement["id"], "request_id": str(uuid4())}
            command_route = trial.host.environment_url + "/teams-references/" + reference["id"] + "/reactivate"
            response = await trial.client.post(command_route, json=command)
            assert response.status_code == 200
            item.update(
                {
                    "command": command,
                    "command_route": command_route,
                    "command_response": response.json(),
                    "replacement": replacement,
                    "removal": removal,
                }
            )
            await providers.set_scheduler(trial, False)
            pending = json.dumps({**body, "id": "restore-pending"}).encode()
            assert (
                await trial.client.post(
                    "/provider-ingress/" + replacement["id"], content=pending, headers=headers(pending)
                )
            ).status_code in {200, 202}
        item["before"] = await providers.rows(trial, source)
        item["pending_source"] = item.get("replacement", source)
        item["pending_before"] = await providers.rows(trial, item["pending_source"])
        assert any(row["state"] == "pending" for row in item["pending_before"]["receipts"])
        stored.append(item)
    trial.api_env["WEAVE_CONNECTOR_PACKAGES"] = json.dumps(
        [
            f"firefly-weave:weave-{provider}:firefly_weave.connectors.{provider}:package"
            for provider in ("teams", "whatsapp")
        ]
    )
    await providers.set_scheduler(trial, False)
    return stored


async def seed_outbox(trial, receiver):
    from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR

    trial.teams_receiver_origin = receiver.url
    trial.api_env["WEAVE_CONNECTION_SECRET_RESTORE_OUTBOX"] = receiver.signing_key
    grants = json.loads(trial.api_env["WEAVE_SECRET_GRANTS"])
    grants.append(
        {
            "scope": trial.scope.model_dump(mode="json"),
            "handle": "restore-outbox",
            "provider": "env",
            "locator": "WEAVE_CONNECTION_SECRET_RESTORE_OUTBOX",
        }
    )
    trial.api_env["WEAVE_SECRET_GRANTS"] = json.dumps(grants)
    trial.api_env["WEAVE_HTTP_PRIVATE_NETWORKS"] = '["127.0.0.0/8"]'
    await providers.set_scheduler(trial, False)
    version = await trial.host.publish("connectors", HTTP_DESCRIPTOR.manifest.value)
    connection = await trial.host.post(
        trial.host.environment_url + "/connections",
        {
            "name": "restore-outbox",
            "connector_version_id": version["id"],
            "config": {"baseUrl": receiver.url, "auth": "bearer"},
            "secretRef": {"token": "restore-outbox"},
            "allowed_destinations": [receiver.url],
        },
    )
    subscription = await trial.host.post(
        trial.host.environment_url + "/subscriptions",
        {
            "name": "restore-outbox",
            "connection_revision_id": connection["id"],
            "signing_slot": "token",
            "path": "/effect",
            "event_types": ["run.transition"],
        },
    )
    activation, _, _ = await trial.prepare_workflow()
    await trial.host.post(trial.host.environment_url + "/runs", {"activation_id": activation["id"], "input": {}})
    return subscription


async def fence_processes(trial):
    assert not trial.containers, "Every source writer must have explicit ownership and be fenced"
    live = []
    for process in trial.processes:
        if process.returncode is None:
            live.append(process.pid)
            process.terminate()
            await trial.owner.joined(process, 20)
    await trial.engine.dispose()
    await trial.observer.dispose()
    assert all(process.returncode is not None for process in trial.processes)
    matrix.private_json(
        trial.directory / "source-fence.json", {"stopped_pids": live, "source": make_url(trial.owner_url).database}
    )


async def switch_target(trial, restored, current):
    runtime = dict(line.split("=", 1) for line in (restored / "target.env").read_text().splitlines())
    runtime = {key: shlex.split(value)[0] for key, value in runtime.items()}
    trial.runtime = runtime
    trial.owner_url = runtime["WEAVE_MIGRATION_DATABASE_URL"]
    trial.api_env.update({key: value for key, value in runtime.items() if key != "WEAVE_MIGRATION_DATABASE_URL"})
    trial.python = current["teams_python"] if trial.teams_jwks is not None else current["python"]
    trial.artifact, trial.artifact_sha = Path(current["wheel"]), current["sha"]
    await trial.command(
        current["python"],
        "-I",
        "-m",
        "firefly_weave.cli.main",
        "admin",
        "migrate",
        env={**trial.base_env, **runtime},
        cwd=trial.directory,
    )
    trial.engine = create_async_engine(runtime["WEAVE_DATABASE_URL"], hide_parameters=True)
    trial.observer = create_async_engine(trial.owner_url, hide_parameters=True)
    trial.api_env["WEAVE_SCHEDULER_ENABLED"] = "true"
    await trial.start_api()
    trial.replica = await trial.start_api(port=trial.replica_port)


async def resume_core(trial, seeded, receiver, current):
    signal_route = trial.host.environment_url + "/runs/" + seeded["signal"]["id"] + "/signals"
    signal = {"eventId": "restore-approved", "name": "approved", "payload": {}}
    first = await trial.client.post(signal_route, json=signal)
    second = await trial.client.post(signal_route, json=signal)
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    safe = seeded["tasks"][0]
    config = {
        "wheel": current["wheel"],
        "wheel_sha256": current["sha"],
        "case": str(uuid4()),
        "api_url": trial.api_url,
        "environment": trial.host.environment_url,
        "token": trial.tokens[1],
        "worker_id": safe["worker"]["id"],
        "receiver": receiver.url,
        "completion_id": str(uuid4()),
        "scope": trial.scope.model_dump(mode="json"),
        "result": str(trial.directory / "restored-worker-result.json"),
        "phase": None,
        "lease_result": str(trial.directory / "restored-worker-lease.json"),
    }
    path = trial.directory / "restored-worker-config.json"
    matrix.private_json(path, config)
    worker = await trial.spawn(
        "restored-worker",
        [current["worker_python"], "-I", str(SUPPORT / "external_worker.py"), "--config", str(path)],
        trial.base_env,
    )
    assert await trial.owner.joined(worker, 120) == 0
    observed = {}
    try:
        async with asyncio.timeout(120):
            while True:
                for kind in ("signal", "timer"):
                    response = await trial.client.get(trial.host.environment_url + "/runs/" + seeded[kind]["id"])
                    assert response.status_code == 200
                    observed[kind] = response.json()
                unsafe = await trial.observe("restore-unsafe")
                if (
                    observed["signal"]["state"]["status"] == "succeeded"
                    and observed["timer"]["state"]["status"] == "succeeded"
                    and unsafe["tasks"][0]["status"] == "incident"
                ):
                    break
                await asyncio.sleep(0.2)
    finally:
        matrix.private_json(trial.directory / "signal-timer-observation.json", observed)
    assert observed["signal"]["state"]["output"] == {}
    signal_final = await trial.observe("restore-signal")
    matrix.private_json(trial.directory / "signal-persisted-observation.json", signal_final)
    consumed = [receipt for receipt in signal_final["signals"] if receipt["external_event_id"] == signal["eventId"]]
    assert len(consumed) == 1 and consumed[0]["consumed"] is True
    transitions = [event for event in signal_final["events"] if event["type"] == "signal_received"]
    assert len(transitions) == 1
    assert transitions[0]["id"] == consumed[0]["id"] and transitions[0]["run_id"] == seeded["signal"]["id"]
    assert len(signal_final["runs"]) == 1
    stored = signal_final["runs"][0]
    assert stored["id"] == seeded["signal"]["id"] and stored["status"] == "succeeded"
    assert 1 < transitions[0]["sequence"] <= int(stored["sequence"])
    safe_final = await trial.observe("restore-safe")
    assert safe_final["tasks"][0]["status"] == "completed"
    assert safe_final["tasks"][0]["operation_key"] == safe["lease"]["operation_key"]
    stale = await trial.client.post(
        trial.host.environment_url + "/tasks/complete",
        json={"lease": safe["lease"]["proof"], "completion_id": str(uuid4()), "output": {}},
        headers={"Authorization": "Bearer " + trial.tokens[1]},
    )
    assert stale.status_code == 409
    snapshot = receiver.snapshot()
    unsafe_key = seeded["tasks"][1]["lease"]["operation_key"]
    attempts = sum(row["operation_key"] == unsafe_key for row in snapshot["attempts"])
    assert attempts == 1
    assert set(snapshot["effects"]) == {task["lease"]["operation_key"] for task in seeded["tasks"]}
    cross = await trial.client.get(trial.host.environment_url.replace(str(trial.scope.tenant_id), trial.other_tenant))
    assert cross.status_code == 403
    async with asyncio.timeout(100):
        while True:
            response = await trial.client.get(
                trial.host.environment_url + "/schedules/" + seeded["schedule"]["id"] + "/occurrences"
            )
            assert response.status_code == 200
            history = response.json()
            assert isinstance(history, list)
            if history:
                break
            await asyncio.sleep(0.2)
    return {
        "safe_operation_key_preserved": True,
        "unsafe_effect_attempts": attempts,
        "signal_replay_same_receipt": first.json() == second.json(),
        "signal_status": observed["signal"]["state"]["status"],
        "signal_output": observed["signal"]["state"]["output"],
        "signal_final": signal_final,
        "timer_status": observed["timer"]["state"]["status"],
        "schedule_occurrences": history,
        "receiver": snapshot,
        "safe_final": safe_final,
        "unsafe_final": unsafe,
    }


async def close_owned(*resources):
    errors = []
    for resource in resources:
        if resource is not None:
            try:
                await resource.close()
            except BaseException as error:
                errors.append(error)
    if errors:
        raise BaseExceptionGroup("Restore trial cleanup failed", errors)


async def event_history(trial):
    async with trial.observer.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT e.run_id,e.sequence,to_jsonb(e)::text FROM run_events e "
                    "JOIN runs r ON r.id=e.run_id WHERE r.project_id=:project ORDER BY e.run_id,e.sequence"
                ),
                {"project": trial.scope.project_id},
            )
        ).all()
    return {str(run) + ":" + str(sequence): value for run, sequence, value in rows}


async def exercise(tmp_path, *, predecessor):
    assert os.environ.get("WEAVE_KEYCLOAK_TEST_URL") == "http://localhost:18081"
    current = {
        "python": os.environ["WEAVE_E2E_PYTHON"],
        "worker_python": os.environ["WEAVE_E2E_WORKER_PYTHON"],
        "teams_python": os.environ["WEAVE_E2E_TEAMS_PYTHON"],
        "wheel": os.environ["WEAVE_E2E_WHEEL"],
        "sha": os.environ["WEAVE_E2E_WHEEL_SHA256"],
    }
    selected = current
    if predecessor:
        selected = {
            "python": os.environ["WEAVE_PREDECESSOR_PYTHON"],
            "wheel": os.environ["WEAVE_PREDECESSOR_WHEEL"],
            "sha": os.environ["WEAVE_PREDECESSOR_WHEEL_SHA256"],
        }
        assert (
            selected["sha"] != current["sha"]
            and hashlib.sha256(Path(selected["wheel"]).read_bytes()).hexdigest() == selected["sha"]
        )
    directory = Path(os.environ.get("WEAVE_D5_EVIDENCE", str(tmp_path))) / (
        "restore-predecessor" if predecessor else "restore-current"
    )
    directory.mkdir(mode=0o700)
    trial = (PredecessorSlice if predecessor else matrix.InstalledSlice)(directory)
    trial.artifact, trial.artifact_sha = Path(selected["wheel"]), selected["sha"]
    receiver = await receivers.EffectReceiver(directory / "receiver.sqlite3").open()
    outbox_receiver = None
    try:
        original_python = os.environ["WEAVE_E2E_PYTHON"]
        os.environ["WEAVE_E2E_PYTHON"] = selected["python"]
        try:
            await trial.bootstrap()
        finally:
            os.environ["WEAVE_E2E_PYTHON"] = original_python
        assert trial.migration_head == ("0012_secret_admission" if predecessor else "0021_operations")
        await providers.set_scheduler(trial, False)
        seeded = await seed_core(trial)
        provider_state = []
        subscription = None
        if not predecessor:
            provider_state = await seed_providers(trial)
            import secrets

            outbox_receiver = await receivers.EffectReceiver(
                directory / "outbox-receiver.sqlite3", signing_key=secrets.token_urlsafe(32)
            ).open()
            subscription = await seed_outbox(trial, outbox_receiver)
        seeded["timer"] = await trial.host.post(
            trial.host.environment_url + "/runs",
            {"activation_id": seeded["timer_activation"]["id"], "input": {}, "correlation_key": "restore-timer"},
        )
        async with httpx.AsyncClient(trust_env=False) as remote:
            for task in seeded["tasks"]:
                response = await trial.client.post(
                    trial.host.environment_url + "/tasks/claim",
                    json={"worker_id": task["worker"]["id"], "limit": 1},
                    headers={"Authorization": "Bearer " + trial.tokens[1]},
                )
                assert response.status_code == 200 and len(response.json()) == 1
                task["lease"] = response.json()[0]
                result = await remote.post(
                    receiver.url + "/effect", json={}, headers={"Idempotency-Key": task["lease"]["operation_key"]}
                )
                assert result.status_code == 200
        before_runs = {}
        for kind in ("signal", "timer"):
            before_runs[kind] = (
                await trial.client.get(trial.host.environment_url + "/runs/" + seeded[kind]["id"])
            ).json()
        matrix.private_json(
            directory / "seed.json",
            {
                "head": trial.migration_head,
                "wheel_sha256": selected["sha"],
                "seeded": seeded,
                "before_runs": before_runs,
                "receiver": receiver.snapshot(),
            },
        )
        committed_history = await event_history(trial)
        assert committed_history
        matrix.private_json(directory / "committed-event-history.json", committed_history)
        await fence_processes(trial)
        restored = directory / "backup"
        result = await backup.restore(
            directory / "runtime.env", restored, "colima-weave-tests", os.environ["WEAVE_TEST_POSTGRES_CONTAINER"]
        )
        await switch_target(trial, restored, current)
        upgraded_history = await event_history(trial)
        assert all(upgraded_history.get(key) == value for key, value in committed_history.items())
        continuation = await resume_core(trial, seeded, receiver, current)
        if not predecessor:
            for item in provider_state:
                raw = json.dumps(item["body"]).encode()
                response = await trial.client.post(item["route"], content=raw, headers=item["headers"](raw))
                if item["provider"] == "teams":
                    assert response.status_code in {200, 202, 409}
                    response = await trial.client.post(item["command_route"], json=item["command"])
                    assert response.status_code == 200 and response.json() == item["command_response"]
                    late = json.dumps({**item["body"], "id": "restore-late"}).encode()
                    assert (
                        await trial.client.post(item["route"], content=late, headers=item["headers"](late))
                    ).status_code == 409
                else:
                    assert response.status_code in {200, 202}
                async with asyncio.timeout(60):
                    while True:
                        final_pending = await providers.rows(trial, item["pending_source"])
                        pending = {row["id"] for row in item["pending_before"]["receipts"] if row["state"] == "pending"}
                        if pending <= {row["id"] for row in final_pending["receipts"] if row["state"] == "dispatched"}:
                            break
                        await asyncio.sleep(0.2)
                final = await providers.rows(trial, item["source"])
                assert {row["id"] for row in item["before"]["receipts"]} <= {row["id"] for row in final["receipts"]}
                if item["provider"] == "whatsapp":
                    assert final["wa_facts"] == item["before"]["wa_facts"]
            outbox = matrix.load("restore_outbox_observer", SUPPORT / "outbox_matrix.py")
            async with asyncio.timeout(60):
                while True:
                    deliveries = await outbox.observe(trial, subscription)
                    if deliveries["deliveries"] and all(
                        row["status"] == "delivered" for row in deliveries["deliveries"]
                    ):
                        break
                    await asyncio.sleep(0.2)
            assert outbox_receiver.snapshot()["effects"]
            continuation["outbox"] = deliveries
        async with trial.observer.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0021_operations"
        result = {
            **result,
            **continuation,
            "predecessor": predecessor,
            "preserved_committed_event_count": len(committed_history),
            "source_wheel_sha256": selected["sha"],
            "target_wheel_sha256": current["sha"],
            "replicas": 2,
            "separate_worker": True,
        }
        matrix.private_json(directory / "continuation.json", result)
        return result
    finally:
        await close_owned(receiver, outbox_receiver, trial)
