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

"""Kill the installed dispatcher after real signed HTTP ACK, before settlement."""

import asyncio
import json
import secrets
from uuid import UUID, uuid4

from sqlalchemy import text


async def observe(trial, subscription):
    async with trial.observer.connect() as connection:
        found = (
            await connection.execute(
                text(
                    "SELECT id,event_id,status,attempts,attempt_limit,code FROM event_deliveries "
                    "WHERE subscription_id=:id ORDER BY id"
                ),
                {"id": UUID(subscription["id"])},
            )
        ).mappings()
        deliveries = [{k: str(v) if isinstance(v, UUID) else v for k, v in row.items()} for row in found]
        attempts = []
        for delivery in deliveries:
            found = (
                await connection.execute(
                    text(
                        "SELECT generation,outcome,finished_at IS NOT NULL AS settled FROM delivery_attempts "
                        "WHERE delivery_id=:id ORDER BY generation"
                    ),
                    {"id": UUID(delivery["id"])},
                )
            ).mappings()
            attempts.extend(dict(row) for row in found)
    return {"deliveries": deliveries, "attempts": attempts}


async def exercise(trial, matrix, *, consecutive=1):
    providers = matrix.load("weave_outbox_scheduler_helpers", matrix.SUPPORT / "provider_matrix.py")
    await providers.set_scheduler(trial, False)
    receiver_module = matrix.load("weave_outbox_effect_receiver", matrix.SUPPORT / "effect_receiver.py")
    key = secrets.token_urlsafe(32)
    receiver = await receiver_module.EffectReceiver(trial.directory / "outbox-receiver.sqlite3", signing_key=key).open()
    try:
        trial.api_env["WEAVE_CONNECTION_SECRET_MATRIX_OUTBOX"] = key
        grants = json.loads(trial.api_env["WEAVE_SECRET_GRANTS"])
        grants.append(
            {
                "scope": trial.scope.model_dump(mode="json"),
                "handle": "matrix-outbox",
                "provider": "env",
                "locator": "WEAVE_CONNECTION_SECRET_MATRIX_OUTBOX",
            }
        )
        trial.api_env["WEAVE_SECRET_GRANTS"] = json.dumps(grants)
        trial.api_env["WEAVE_HTTP_PRIVATE_NETWORKS"] = '["127.0.0.0/8"]'
        await providers.set_scheduler(trial, False)
        from firefly_weave.connectors.manifest import HTTP_DESCRIPTOR

        version = await trial.host.publish("connectors", HTTP_DESCRIPTOR.manifest.value)
        connection = await trial.host.post(
            trial.host.environment_url + "/connections",
            {
                "name": "matrix-outbox",
                "connector_version_id": version["id"],
                "config": {"baseUrl": receiver.url, "auth": "bearer"},
                "secretRef": {"token": "matrix-outbox"},
                "allowed_destinations": [receiver.url],
            },
        )
        subscription = await trial.host.post(
            trial.host.environment_url + "/subscriptions",
            {
                "name": "matrix-outbox",
                "connection_revision_id": connection["id"],
                "signing_slot": "token",
                "path": "/effect",
                "event_types": ["run.transition"],
            },
        )
        activation, _, _ = await trial.prepare_workflow()
        correlation = str(uuid4())
        await trial.host.post(
            trial.host.environment_url + "/runs",
            {
                "activation_id": activation["id"],
                "input": {},
                "correlation_key": correlation,
            },
        )
        baseline = await observe(trial, subscription)
        assert len(baseline["deliveries"]) == 1 and baseline["deliveries"][0]["status"] == "pending"
        selected = baseline["deliveries"][0]
        crashes = []
        for _ in range(consecutive):
            target = {
                "case": str(uuid4()),
                "phase": "after_outbox_accept",
                "method": "BACKGROUND",
                "path": "/background/outbox/" + selected["id"],
                "scope": trial.scope.model_dump(mode="json"),
                "resource": selected["id"],
            }
            trial.api_env["WEAVE_SCHEDULER_ENABLED"] = "true"
            await trial.replace_api(target)
            # Recovery waits for the original 30-second lease naturally; no SQL expiry edits.
            message = await matrix.barriers.read_message(trial.barrier_socket, timeout=60)
            assert message == {
                "nonce": target["case"],
                "pid": trial.api.pid,
                "phase": target["phase"],
                "detail": {"case": target["case"], "scope": target["scope"]},
            }
            visible = await observe(trial, subscription)
            accepted = receiver.snapshot()
            assert accepted["effects"] == [selected["event_id"]]
            assert visible["deliveries"][0]["status"] == "leased"
            assert visible["attempts"][-1]["outcome"] is None and not visible["attempts"][-1]["settled"]
            await trial.kill_selected()
            crashes.append({"handshake": message, "visible": visible, "receiver": accepted})
        await providers.set_scheduler(trial, True)
        async with asyncio.timeout(90):
            while True:
                final = await observe(trial, subscription)
                if final["deliveries"][0]["status"] == "delivered":
                    break
                await asyncio.sleep(0.2)
        accepted = receiver.snapshot()
        assert len(accepted["attempts"]) == consecutive + 1
        assert len(accepted["effects"]) == 1
        assert all(row["operation_key"] == selected["event_id"] for row in accepted["attempts"])
        assert [row["outcome"] for row in final["attempts"]] == ["ACK_UNKNOWN"] * consecutive + ["ACK"]
        counts = {
            "duplicate_accepted_transitions": max(
                0, sum(row["status"] == "delivered" for row in final["deliveries"]) - 1
            ),
            "lost_committed_work": len(
                {row["id"] for row in baseline["deliveries"]} - {row["id"] for row in final["deliveries"]}
            ),
            "unsafe_blind_retries": len(
                {row["operation_key"] for row in accepted["attempts"]} - {selected["event_id"]}
            ),
        }
        evidence = {
            "boundary": "after_outbox_accept",
            "crashes": crashes,
            "final": final,
            "receiver": accepted,
            "invariants": counts,
            "artifact_sha256": trial.artifact_sha,
            "replicas": 2,
        }
        matrix.private_json(trial.directory / ("outbox-" + correlation + ".json"), evidence)
        return evidence
    finally:
        await receiver.close()
