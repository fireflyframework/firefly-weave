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

"""Server records for AI on the local platform, created through public API operations with the host token.

Every step is repeatable: definitions publish with keys derived from their digests, the API
returns the existing release for the same image, and the receipt keeps the worker principal
and the connection so a repeated command reuses them. Refusals report their code; the
connection uses the reserved no-credential handle, so no secret is ever sent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from firefly_weave.compiler.catalog import FrozenDocument
from firefly_weave.contracts.agentic import (
    AGENTIC_DESCRIPTOR,
    CONNECTOR_REFERENCE,
    TASK_TYPE,
    TASK_VERSION,
    action_definition,
    task_capability,
)
from firefly_weave.contracts.ai import AIConnectionTestRequest, ai_message
from firefly_weave.contracts.catalog import ActivationRequest
from firefly_weave.contracts.connectors import NO_CREDENTIAL, ConnectionRequest, ConnectionRevision
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.members import MemberGrantRequest, PrincipalCreateRequest, PrincipalIdentityRequest
from firefly_weave.contracts.runtime import StartRunRequest
from firefly_weave.contracts.workers import ReleaseRequest
from firefly_weave.sdk import platform as local
from firefly_weave.sdk.deployment import strict_json

if TYPE_CHECKING:
    import httpx

    from firefly_weave.sdk.client import WeaveClient

CAPABILITY = f"{TASK_TYPE}@{TASK_VERSION}"
CONNECTOR_DIGEST = "7dfc18419ba042e7dff2a15198373c13da3e1a5e83a78976c422993f57a937e7"
ACTION_DIGEST = "cd1ab7add02d438b419cb206fdb7164c027fe5c5a5b4b3029af2402ce60ebbf0"
WORKER_CLIENT = "weave-worker"
CONNECTION = "ollama-local"
SMOKE_INPUT = {"text": "Firefly Weave runs durable workflows and records every step, so a run can be replayed."}
ONLINE_SECONDS = 120.0
SMOKE_SECONDS = 900.0
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
Sleep = Callable[[float], Awaitable[None]]


def refused(step: str, error: Any) -> local.PlatformError:
    code = getattr(error, "code", None) or "WV-UNAVAILABLE"
    return local.PlatformError(
        f"The local API refused the AI {step} step ({code}). No secret was sent; "
        "fix the cause, then rerun weave platform ai enable."
    )


def _normal(value: Any) -> Any:
    return json.loads(json.dumps(value))


def verify_catalog(output: bytes) -> dict[str, dict[str, Any]]:
    """The Connector and Action the worker image carries, only when they are this server's built-ins."""
    try:
        value = strict_json(output)
        documents = {item["document"]["kind"]: item["document"] for item in value["definitions"]}
        connector, action = documents["Connector"], documents["Action"]
        if (
            connector != _normal(load_definition(AGENTIC_DESCRIPTOR.manifest.value).model_dump(by_alias=True))
            or FrozenDocument.from_value(connector).digest != CONNECTOR_DIGEST
            or action != _normal(load_definition(action_definition()).model_dump(by_alias=True))
            or FrozenDocument.from_value(action).digest != ACTION_DIGEST
            or value["tasks"] != [_normal(task_capability().model_dump(by_alias=True))]
        ):
            raise ValueError("Catalog differs")
        return {"connector": connector, "action": action}
    except (ValueError, KeyError, TypeError):
        raise local.PlatformError(
            "The Agentic worker image does not carry this server's AI catalog; nothing was published."
        ) from None


def release_manifest(output: bytes) -> dict[str, Any]:
    """The image's task and credential capabilities, only when they are exactly the typed AI call's."""
    try:
        value = strict_json(output)
        capabilities, credentials = value["capabilities"], value["credential_capabilities"]
        if capabilities != [_normal(task_capability().model_dump(by_alias=True))] or credentials != [CAPABILITY]:
            raise ValueError("Manifest differs")
        return {"capabilities": capabilities, "credential_capabilities": credentials}
    except (ValueError, KeyError, TypeError):
        raise local.PlatformError("The Agentic worker image reported an unexpected release manifest.") from None


def _key(kind: str, digest: str) -> str:
    return f"weave-platform-ai-{kind}-{digest[:40]}"


async def publish(client: WeaveClient, collection: Any, document: dict[str, Any], digest: str) -> str:
    source = json.dumps(document, sort_keys=True, separators=(",", ":"))
    published = await client.publish(collection, source, "json", idempotency_key=_key(collection, digest))
    return str(published.id)


async def admit_release(client: WeaveClient, image_id: str, manifest: dict[str, Any]) -> str:
    request = ReleaseRequest.model_validate_json(json.dumps({"image_digest": image_id, **manifest}))
    release = await client.invoke("releases.create", body=request)
    return str(release.id)


async def worker_subject(keycloak: str, admin_secret: str, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """The Keycloak subject of the weave-worker client's service account: the identity the worker signs in as."""
    import httpx

    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client:
        token = await local._keycloak_admin(client, keycloak, admin_secret)
        status, clients = await local._keycloak(
            client, "GET", keycloak + "/admin/realms/weave/clients", token=token, params={"clientId": WORKER_CLIENT}
        )
        records = (
            [item for item in clients if isinstance(item, dict) and item.get("clientId") == WORKER_CLIENT]
            if isinstance(clients, list)
            else []
        )
        if status != 200 or len(records) != 1 or not isinstance(records[0].get("id"), str):
            raise local.PlatformError("The local Keycloak worker client could not be read; nothing was linked.")
        status, account = await local._keycloak(
            client,
            "GET",
            f"{keycloak}/admin/realms/weave/clients/{records[0]['id']}/service-account-user",
            token=token,
        )
        subject = account.get("id") if isinstance(account, dict) else None
        try:
            if status != 200 or not isinstance(subject, str) or str(UUID(subject)) != subject:
                raise ValueError("Unexpected account")
        except ValueError:
            raise local.PlatformError(
                "The local Keycloak returned an unexpected worker account; nothing was linked."
            ) from None
        return subject


async def worker_principal(
    client: WeaveClient,
    scope: dict[str, str],
    *,
    release_id: str,
    issuer: str,
    subject: Callable[[], Awaitable[str]],
    receipt: dict[str, Any],
    save: Callable[[dict[str, Any]], None],
) -> bool:
    """Create the worker principal, link it to the weave-worker identity and grant it this release; True on change."""
    from firefly_weave.sdk.members import MembersClient

    members = MembersClient(client)
    changed = False
    principal = receipt.get("principal_id")
    if principal is None:
        principal = str((await members.create_principal(PrincipalCreateRequest(kind="worker"))).id)
        save({"principal_id": principal, "linked": False})
        changed = True
    if not receipt.get("linked"):
        request = PrincipalIdentityRequest(provider_id=local._PROVIDER_ID, issuer=issuer, subject=await subject())
        await members.link_identity(UUID(principal), request)
        save({"linked": True})
        changed = True
    if receipt.get("granted_release_id") != release_id:
        await members.grant(
            MemberGrantRequest(
                principal_id=UUID(principal),
                role="worker",
                project_id=UUID(scope["project_id"]),
                environment_id=UUID(scope["environment_id"]),
                resources=(release_id, CAPABILITY),
            )
        )
        save({"granted_release_id": release_id})
        changed = True
    return changed


def connection_request(connector_version_id: str, endpoint: str, origin: str) -> ConnectionRequest:
    """The keyless ollama-local connection: plain HTTP to an approved origin, the reserved handle, no secret."""
    return ConnectionRequest.model_validate_json(
        json.dumps(
            {
                "name": CONNECTION,
                "connector_version_id": connector_version_id,
                "config": {"provider": "openai-chat", "endpoint": endpoint, "secretSlot": "apiKey"},
                "secretRef": {"apiKey": NO_CREDENTIAL},
                "allowed_destinations": [origin],
            }
        )
    )


async def ensure_connection(client: WeaveClient, request: ConnectionRequest) -> tuple[str, bool]:
    """The revision with exactly this configuration, created when missing; (revision id, created)."""
    cursor: str | None = None
    for _ in range(50):
        query: dict[str, str | int] = {"limit": 100}
        if cursor is not None:
            query["cursor"] = cursor
        page = await client.invoke("connections.list", query=query)
        for item in page.items:
            if (
                isinstance(item, ConnectionRevision)
                and item.name == request.name
                and item.connector_version_id == request.connector_version_id
                and item.config == request.config
                and item.secret_refs == request.secret_refs
                and tuple(item.allowed_destinations) == tuple(request.allowed_destinations)
            ):
                return str(item.id), False
        cursor = page.next_cursor
        if cursor is None:
            break
    created = await client.invoke("connections.create", body=request)
    return str(created.id), True


async def presence(client: WeaveClient, release_id: str) -> dict[str, Any]:
    page = await client.invoke("workers.list", query={"limit": 100})
    found = [item for item in page.items if str(item.release_id) == release_id and not item.revoked]
    best = next((item for item in found if item.presence == "recent"), found[0] if found else None)
    if best is None:
        return {"presence": "unknown", "last_seen_at": None}
    seen = best.last_seen_at.isoformat() if best.last_seen_at else None
    return {"presence": best.presence, "last_seen_at": seen, "worker_id": str(best.id)}


async def wait_online(
    client: WeaveClient,
    release_id: str,
    *,
    seconds: float = ONLINE_SECONDS,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    deadline = clock() + seconds
    while True:
        found = await presence(client, release_id)
        if found["presence"] == "recent":
            return found
        if clock() >= deadline:
            raise local.PlatformError(
                "The Agentic worker did not come online within 120 seconds. Inspect it with weave platform ai status "
                "and the agentic-worker container logs; nothing else was changed."
            )
        await sleep(2)


async def test_connection(client: WeaveClient, revision_id: str, model: str) -> dict[str, Any]:
    result = await client.invoke(
        "ai_connections.test", identifier=UUID(revision_id), body=AIConnectionTestRequest(model=model, probe_tools=True)
    )
    return {**result.model_dump(mode="json"), "tested_at": datetime.now(UTC).isoformat()}


def smoke_workflow(model: str) -> dict[str, Any]:
    """One AI task that summarizes a text; the name carries the model so each model has its own version."""
    result = {"type": "string", "minLength": 1, "maxLength": 2000}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "ai-smoke-" + hashlib.sha256(model.encode()).hexdigest()[:12], "version": "1.0.0"},
        "spec": {
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            "outputSchema": result,
            "connections": {"ai": {"connector": CONNECTOR_REFERENCE}},
            "llmProfiles": {
                "smoke": {
                    "provider": "openai-chat",
                    "model": model,
                    "options": {"max_tokens": 256, "temperature": 0, "seed": 42},
                    "reasoning": {"pattern": "none"},
                    "maxCalls": 2,
                    "timeoutSeconds": 300,
                    "outputSchema": result,
                }
            },
            "steps": [
                {
                    "id": "summarize",
                    "kind": "llm",
                    "uses": "weave-agentic-generate@1.0.0",
                    "profile": "smoke",
                    "connection": "ai",
                    "prompt": {"literal": "Summarize the supplied text in one sentence."},
                    "context": {"ref": "/input/text"},
                }
            ],
            "output": {"ref": "/steps/summarize/output/result"},
        },
    }


def _output(event: Any) -> dict[str, Any]:
    data = event.data if isinstance(event.data, dict) else {}
    output = data.get("output")
    return output if isinstance(output, dict) else {}


async def _failure(client: WeaveClient, run_id: UUID) -> str | None:
    for event in (await client.history(run_id, limit=100)).events:
        if event.type == "task_failed":
            code = _output(event).get("code")
            if isinstance(code, str):
                return code
    return None


async def smoke(
    client: WeaveClient,
    scope: dict[str, str],
    *,
    release_id: str,
    revision_id: str,
    model: str,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    seconds: float = SMOKE_SECONDS,
) -> dict[str, Any]:
    """Publish, activate and run the one-step AI workflow; require success, usage and a consistent replay."""
    source = json.dumps(smoke_workflow(model), sort_keys=True, separators=(",", ":"))
    published = await client.publish(
        "workflows", source, "json", idempotency_key=_key("workflows", hashlib.sha256(source.encode()).hexdigest())
    )
    request = ActivationRequest.model_validate_json(
        json.dumps(
            {
                "version_id": str(published.id),
                "artifact_digest": published.digest,
                "scope": scope,
                "connection_revision_ids": {"ai": revision_id},
                "worker_release_ids": {TASK_TYPE: release_id},
            }
        )
    )
    pins = hashlib.sha256(f"{published.id}:{release_id}:{revision_id}".encode()).hexdigest()
    activation = await client.activate(request, idempotency_key=_key("activation", pins))
    run = await client.start_run(
        StartRunRequest.model_validate_json(json.dumps({"activation_id": str(activation.id), "input": SMOKE_INPUT})),
        idempotency_key=str(uuid4()),
    )
    deadline = clock() + seconds
    while True:
        current = await client.read_run(run.id)
        if current.state.status in TERMINAL:
            break
        code = await _failure(client, run.id)
        if code is not None:
            raise local.PlatformError(
                f"The AI smoke run failed with {code}: {ai_message(code, model=model, endpoint=CONNECTION)}"
            )
        if clock() >= deadline:
            raise local.PlatformError("The AI smoke run did not finish within 15 minutes; inspect it in Operate.")
        await sleep(2)
    output = current.state.output
    if current.state.status != "succeeded" or not isinstance(output, str) or not output:
        raise local.PlatformError(f"The AI smoke run ended {current.state.status}; inspect it in Operate.")
    events = (await client.history(run.id, limit=100)).events
    usage = next((_output(event).get("usage") for event in events if event.type == "task_completed"), None)
    requests = usage.get("requests") if isinstance(usage, dict) else None
    replay = await client.replay(run.id)
    if replay.status != "consistent":
        raise local.PlatformError("The AI smoke run did not replay consistently; inspect it in Operate.")
    return {
        "run_id": str(run.id),
        "status": "succeeded",
        "requests": requests if isinstance(requests, int) else 0,
        "replay": replay.status,
    }
