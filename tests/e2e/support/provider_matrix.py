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

"""Installed provider inbox crash trials with signed local protocol fixtures."""

import asyncio
import hashlib
import hmac
import importlib
import json
import os
import secrets
from uuid import UUID, uuid4

from sqlalchemy import text


async def rows(trial, source):
    queries = {
        "receipts": (
            "SELECT id,event_id,kind,state,receipt->>'run_id' AS run_id FROM provider_receipts "
            "WHERE source_id=:source ORDER BY id"
        ),
        "intents": "SELECT receipt_id,pending FROM provider_intents WHERE source_id=:source ORDER BY receipt_id",
        "teams": "SELECT id,generation,state FROM teams_references WHERE project_id=:project ORDER BY id",
        "lifecycle": (
            "SELECT reference_id,kind,event_id FROM teams_lifecycle_events WHERE project_id=:project ORDER BY event_id"
        ),
        "wa_states": (
            "SELECT id,progress,failed_seen,deleted_seen,fact_count FROM whatsapp_message_states "
            "WHERE project_id=:project ORDER BY id"
        ),
        "wa_facts": "SELECT id,state_id,status FROM whatsapp_status_facts WHERE project_id=:project ORDER BY id",
    }
    result = {}
    async with trial.observer.connect() as connection:
        for key, query in queries.items():
            found = (
                await connection.execute(text(query), {"source": UUID(source["id"]), "project": trial.scope.project_id})
            ).mappings()
            result[key] = [{k: str(v) if isinstance(v, UUID) else v for k, v in row.items()} for row in found]
        run_ids = [UUID(row["run_id"]) for row in result["receipts"] if row["run_id"]]
        result["events"] = []
        for run_id in run_ids:
            found = (
                await connection.execute(
                    text("SELECT run_id,id,sequence,type FROM run_events WHERE run_id=:run"), {"run": run_id}
                )
            ).mappings()
            result["events"].extend({k: str(v) if isinstance(v, UUID) else v for k, v in row.items()} for row in found)
    return result


async def set_scheduler(trial, enabled):
    trial.api_env["WEAVE_SCHEDULER_ENABLED"] = "true" if enabled else "false"
    if trial.replica.returncode is None:
        trial.replica.terminate()
        await trial.owner.joined(trial.replica, 15)
    await trial.replace_api()
    trial.replica = await trial.start_api(port=trial.replica_port)


async def prepare(trial, provider, matrix):
    await set_scheduler(trial, False)
    activation, _, _ = await trial.prepare_workflow()
    package = importlib.import_module("firefly_weave.connectors." + provider).package
    from firefly_weave.contracts.providers import provider_schema_digest

    identity = f"firefly-weave:weave-{provider}:firefly_weave.connectors.{provider}:package"
    packages = json.loads(trial.api_env.get("WEAVE_CONNECTOR_PACKAGES", "[]"))
    if identity not in packages:
        packages.append(identity)
    trial.api_env["WEAVE_CONNECTOR_PACKAGES"] = json.dumps(packages)
    slot_values = {}
    if provider == "teams":
        fixture = matrix.load("weave_matrix_signed_fixture", matrix.ROOT / "tests/conftest.py")
        policy, body, token, jwks = fixture.signed_activity.__wrapped__()
        trial.teams_jwks = trial.directory / "teams-fixture-jwks.json"
        matrix.private_json(trial.teams_jwks, json.loads(jwks))
        trial.python = os.environ.get("WEAVE_E2E_TEAMS_PYTHON", "")
        assert trial.python, "Installed Teams closure required: WEAVE_E2E_TEAMS_PYTHON"
        slot_values = {"client_secret": secrets.token_urlsafe(32)}
        destinations = ["https://login.microsoftonline.com", "https://smba.trafficmanager.net"]

        def headers(raw):
            return {"Authorization": "Bearer " + token()}
    else:
        policy = {
            "app_id": "100",
            "account_id": "200",
            "phone_number_id": "300",
            "business_phone_number": "15550000000",
            "graph_version": "v26.0",
            "recipient_allowlist": ["15550000001"],
            "approved_templates": [{"name": "appointment", "locale": "en_US", "body_parameters": 1}],
        }
        slot_values = {slot: secrets.token_urlsafe(32) for slot in ("accessToken", "appSecret", "verifyToken")}
        destinations = ["https://graph.facebook.com"]
        fixture = matrix.load("weave_matrix_wa_fixture", matrix.ROOT / "tests/integration/providers/test_whatsapp.py")
        body = fixture.body("read", "sent", "delivered", "failed", "deleted", message=True)

        def headers(raw):
            return {
                "x-hub-signature-256": "sha256="
                + hmac.new(slot_values["appSecret"].encode(), raw, hashlib.sha256).hexdigest()
            }

    grants = json.loads(trial.api_env["WEAVE_SECRET_GRANTS"])
    for index, (slot, value) in enumerate(slot_values.items()):
        name = "WEAVE_CONNECTION_SECRET_MATRIX_" + provider.upper() + "_" + str(index)
        trial.api_env[name] = value
        grants.append(
            {
                "scope": trial.scope.model_dump(mode="json"),
                "handle": "matrix-" + provider + "-" + slot,
                "provider": "env",
                "locator": name,
            }
        )
    trial.api_env["WEAVE_SECRET_GRANTS"] = json.dumps(grants)
    await set_scheduler(trial, False)
    version = await trial.host.publish(
        "connectors", package.metadata.model.manifest.model_dump(mode="json", by_alias=True)
    )

    async def source_at(generation=1):
        profile = {**policy, **({"installation_generation": generation} if provider == "teams" else {})}
        connection = await trial.host.post(
            trial.host.environment_url + "/connections",
            {
                "name": "matrix-" + provider,
                "connector_version_id": version["id"],
                "config": profile,
                "secretRef": {slot: "matrix-" + provider + "-" + slot for slot in slot_values},
                "allowed_destinations": destinations,
            },
        )
        source = await trial.host.post(
            trial.host.environment_url + "/provider-sources",
            {
                "name": "matrix-" + provider,
                "provider": provider,
                "package": "firefly-weave",
                "package_version": "0.1.0a6",
                "schema_digest": provider_schema_digest(
                    package.metadata.model.event_schemas, package.metadata.model.dispatch_event_kinds
                ),
                "connection_revision_id": connection["id"],
                "policy": profile,
                "kind": "run",
                "activation_id": activation["id"],
            },
        )
        return source

    source = await source_at()
    return source, body, headers, source_at


async def exercise(trial, provider, boundary, matrix):
    source, body, headers, source_at = await prepare(trial, provider, matrix)
    route = "/provider-ingress/" + source["id"]
    raw = json.dumps(body).encode()
    expected_count = 1 if provider == "teams" else 6
    dispatch = "dispatch" in boundary
    if dispatch:
        response = await trial.client.post(route, content=raw, headers=headers(raw))
        assert response.status_code in {200, 202}
        admitted = await rows(trial, source)
        selected = next(row["id"] for row in admitted["receipts"] if row["state"] == "pending")
        target = {
            "case": str(uuid4()),
            "phase": boundary,
            "method": "BACKGROUND",
            "path": "/background/provider/" + selected,
            "scope": trial.scope.model_dump(mode="json"),
            "resource": selected,
        }
        trial.api_env["WEAVE_SCHEDULER_ENABLED"] = "true"
        await trial.replace_api(target)
        request = None
    else:
        target = {
            "case": str(uuid4()),
            "phase": boundary,
            "method": "POST",
            "path": route,
            "scope": trial.scope.model_dump(mode="json"),
        }
        await trial.replace_api(target)
        request = asyncio.create_task(
            trial.client.post(
                route,
                content=raw,
                headers={
                    **headers(raw),
                    "X-Weave-Crash-Case": target["case"],
                },
                timeout=25,
            )
        )
    try:
        message = await trial.kill_at_barrier()
        committed = await rows(trial, source)
        if dispatch:
            target_row = next(row for row in committed["receipts"] if row["id"] == selected)
            assert target_row["state"] == ("dispatched" if boundary.startswith("after") else "pending")
            # Other batch receipts may commit before this explicitly correlated barrier.
            target_events = [row for row in committed["events"] if row["run_id"] == target_row["run_id"]]
            assert len(target_events) == int(boundary.startswith("after"))
            assert bool(target_row["run_id"]) == boundary.startswith("after")
            if target_events:
                assert target_events[0]["type"] == "started"
        else:
            assert len(committed["receipts"]) == (expected_count if boundary.startswith("after") else 0)
            assert len(committed["teams"]) == int(provider == "teams" and boundary.startswith("after"))
            assert len(committed["wa_facts"]) == (5 if provider == "whatsapp" and boundary.startswith("after") else 0)
        await trial.kill_selected()
    finally:
        if request is not None:
            await asyncio.gather(request, return_exceptions=True)
    trial.api_env["WEAVE_SCHEDULER_ENABLED"] = "false"
    await trial.replace_api()
    for _ in range(2):
        response = await trial.client.post(route, content=raw, headers=headers(raw))
        assert response.status_code in {200, 202}
    replay = await rows(trial, source)
    assert len(replay["receipts"]) == expected_count
    if provider == "whatsapp":
        assert len(replay["wa_facts"]) == 5
        state = replay["wa_states"][0]
        assert state["progress"] == 3 and state["failed_seen"] and state["deleted_seen"] and state["fact_count"] == 5
    await set_scheduler(trial, True)
    async with asyncio.timeout(30):
        while True:
            final = await rows(trial, source)
            if sum(row["state"] == "dispatched" for row in final["receipts"]) == expected_count:
                break
            await asyncio.sleep(0.2)
    # WhatsApp messages and all five known status facts each dispatch their own run.
    run_ids = {row["run_id"] for row in final["receipts"]}
    assert None not in run_ids and len(run_ids) == expected_count
    starts = [row for row in final["events"] if row["type"] == "started"]
    assert len(starts) == expected_count
    assert {row["run_id"] for row in starts} == run_ids
    if provider == "teams":
        removal = {**body, "type": "installationUpdate", "action": "remove", "id": "removed"}
        encoded = json.dumps(removal).encode()
        for _ in range(2):
            response = await trial.client.post(route, content=encoded, headers=headers(encoded))
            assert response.status_code == 202
        removed = await rows(trial, source)
        reference = removed["teams"][0]
        assert reference["state"] == "revoked" and len(removed["lifecycle"]) == 1
        late = json.dumps({**body, "id": "late"}).encode()
        assert (await trial.client.post(route, content=late, headers=headers(late))).status_code == 409
        replacement = await source_at(2)
        response = await trial.client.post(
            trial.host.environment_url + "/teams-references/" + reference["id"] + "/reactivate",
            json={
                "expected_generation": 1,
                "source_id": replacement["id"],
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 200 and response.json()["generation"] == 2
        assert (await trial.client.post(route, content=late, headers=headers(late))).status_code == 409
        assert (
            await trial.client.post("/provider-ingress/" + replacement["id"], content=encoded, headers=headers(encoded))
        ).status_code == 202
        final = await rows(trial, source)
        assert final["teams"][0]["state"] == "active" and final["teams"][0]["generation"] == 2
    previous_ids = {row["id"] for row in committed["receipts"]}
    final_ids = {row["id"] for row in final["receipts"]}
    counts = matrix.invariant_counts(committed, final, expected_event_types={"started": expected_count})
    counts["lost_committed_work"] += len(previous_ids - final_ids)
    evidence = {
        "boundary": boundary,
        "provider": provider,
        "handshake": message,
        "committed": committed,
        "replay": replay,
        "final": final,
        "invariants": counts,
        "artifact_sha256": trial.artifact_sha,
        "authentication": "native-signature-and-claims-with-local-jwks" if provider == "teams" else "native-hmac",
        "scope": "local protocol fixture; no live provider traffic",
    }
    matrix.private_json(trial.directory / ("provider-case-" + target["case"] + ".json"), evidence)
    return evidence
