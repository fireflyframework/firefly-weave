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

"""Operations on the local Docker platform: a registry, a sleep fixture worker, an operator-owned workers
project and its runner.

`enable` runs once per `weave platform up` and resumes from `operations.json` stage by stage; every stage is
repeatable. The receipt keeps identifiers, image references, ports and secret generation names only, never a
secret value. The runner mounts the Docker socket, which controls the Docker engine: this is for local
development only, and `weave platform doctor` says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from firefly_weave import private_origins
from firefly_weave.contracts.deployments import DeploymentCapability
from firefly_weave.sdk import platform as local
from firefly_weave.sdk import platform_ai_files as files
from firefly_weave.sdk import platform_origins
from firefly_weave.sdk.deployment import DeploymentError, read_file, strict_json

if TYPE_CHECKING:
    import httpx

    from firefly_weave.deployment_runner.policy import ComponentBinding
    from firefly_weave.sdk.client import WeaveClient

FORMAT = "weave/local-operations-v1"
RECEIPT = "operations.json"
LOCK = ".operations.lock"
COMPOSE = "compose.operations.json"
ROOT = "operations"
REGISTRY_IMAGE = "registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
DOCKER_CLI_IMAGE = "docker:27.5.1-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c"
TARGET = "local-docker"
DEPLOYMENT = "local-workers"
COMPONENT = "fixture-worker"
RUNNER = "runner-local-docker"
REGISTRY = "registry"
TASK_TYPE = "example-sleep@1.0.0"
WORKER_CLIENT = "weave-operations-worker"
RUNNER_CLIENT = "weave-operations-runner"
CLIENTS = {WORKER_CLIENT: "application", RUNNER_CLIENT: "application"}
STOP_GRACE = "240s"
CAPABILITIES: tuple[DeploymentCapability, ...] = ("observe", "deploy", "update", "scale_workers", "restart_workers")
STAGES = ("registry", "images", "identities", "release", "workers", "origins", "target", "runner", "ready")
CONTAINER = "/run/weave-operations"
SOCKET = "/var/run/docker.sock"
API_ORIGIN = "http://127.0.0.1:8000"
KEYCLOAK_ORIGIN = "http://127.0.0.1:8080"
BRIDGE_API = "http://keycloak:8000"
BRIDGE_KEYCLOAK = "http://keycloak:8080"
TOKEN_PATH = "/realms/weave/protocol/openid-connect/token"
RUNNER_SECONDS = 60.0
DOCTOR_NOTE = (
    "Operations: the local runner mounts the Docker socket, which controls this computer's Docker engine. "
    "This is for local development only."
)
CHANGED_OUTSIDE = "The operations configuration changed outside platform commands; nothing was changed."
_SECRET_NAMES = ("worker-client-secret", "runner-client-secret")
_GENERATION = re.compile(r"[a-f0-9]{32}")
_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")

Notice = Callable[[str], None]


def _quiet(message: str) -> None:
    return None


# Receipt


def receipt(directory: Path) -> dict[str, Any] | None:
    """The private operations receipt, or None before the first enable."""
    path = directory / RECEIPT
    if not os.path.lexists(path):
        return None
    try:
        value = strict_json(read_file(path, 65536, private=True))
        if not isinstance(value, dict) or value.get("format") != FORMAT or value.get("stage") not in STAGES:
            raise ValueError("Unknown receipt")
        local._scope_value(value["scope"])
        _generations(value)
        for key in ("runner_image", "worker_image_id"):
            if key in value and _IMAGE_ID.fullmatch(str(value[key])) is None:
                raise ValueError("Image identity required")
        return value
    except (OSError, ValueError, KeyError, TypeError, DeploymentError):
        raise local.PlatformError(
            "The saved operations settings (operations.json) are malformed or not private. Keep the file for "
            "inspection; nothing was changed."
        ) from None


def _save(directory: Path, value: dict[str, Any]) -> None:
    path = directory / RECEIPT
    local._write(path, value, replace=path.exists())


def _generations(value: dict[str, Any]) -> dict[str, str]:
    found = value.get("secret_generations")
    if found is None:
        return {}
    if (
        not isinstance(found, dict)
        or not set(found) <= set(_SECRET_NAMES)
        or any(not isinstance(item, str) or _GENERATION.fullmatch(item) is None for item in found.values())
    ):
        raise ValueError("Invalid secret generations")
    return dict(found)


def _project(state: dict[str, Any]) -> str:
    return f"weave-local-{state['id']}-workers"


def _docker(state: dict[str, Any]) -> list[str]:
    return ["docker", "--context", state["context"]]


def _root(state: dict[str, Any]) -> Path:
    path = Path(state["directory"]) / ROOT
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise local.PlatformError(CHANGED_OUTSIDE)
    else:
        path.mkdir(mode=0o700)
    return path


def _shared(state: dict[str, Any], name: str) -> Path:
    return files._shared(_root(state) / name)


def _environment_url(scope: dict[str, str]) -> str:
    return f"/api/v1/tenants/{scope['tenant_id']}/projects/{scope['project_id']}/environments/{scope['environment_id']}"


# Provider


def prepare_provider(state: dict[str, Any]) -> bool:
    """Let the API accept the two operations clients; True when runtime.env changed (the API is recreated)."""
    path = Path(state["directory"]) / "runtime.env"
    runtime = local._env_file(path)
    providers = json.loads(runtime["WEAVE_OIDC_PROVIDERS"])
    if not isinstance(providers, list) or len(providers) != 1 or not isinstance(providers[0].get("clients"), dict):
        raise local.PlatformError("The saved identity configuration is outside this installation.")
    clients = providers[0]["clients"]
    if all(clients.get(name) == kind for name, kind in CLIENTS.items()):
        return False
    providers[0]["clients"] = {**clients, **CLIENTS}
    encoded = json.dumps(providers, separators=(",", ":"))
    lines = []
    for line in read_file(path, 65536, private=True).decode().splitlines():
        lines.append("WEAVE_OIDC_PROVIDERS='" + encoded + "'" if line.startswith("WEAVE_OIDC_PROVIDERS=") else line)
    local._atomic(path, ("\n".join(lines) + "\n").encode(), replace=True)
    return True


# Rendered files


def entries(state: dict[str, Any], subnet: str) -> list[private_origins.PrivateOrigin]:
    """The workers reach the API and Keycloak over the platform network; the runner shares Keycloak's loopback."""
    entry = private_origins.PrivateOrigin
    return [
        entry(origin=BRIDGE_API, purpose="platform-api", networks=(subnet,), credentials="bridge"),
        entry(origin=BRIDGE_KEYCLOAK, purpose="worker-auth", networks=(subnet,), credentials="bridge"),
        entry(origin=API_ORIGIN, purpose="platform-api", networks=files.LOOPBACK, credentials="loopback"),
        entry(origin=KEYCLOAK_ORIGIN, purpose="worker-auth", networks=files.LOOPBACK, credentials="loopback"),
    ]


def components(state: dict[str, Any]) -> list[ComponentBinding]:
    """The workers project's component bindings; another platform step may add its own here."""
    from firefly_weave.deployment_runner.policy import ComponentBinding

    return [ComponentBinding(name=COMPONENT, kind="worker", configuration="local", max_replicas=10)]


def _secret(state: dict[str, Any], value: dict[str, Any], name: str) -> Path:
    generation = _generations(value).get(name)
    if generation is None:
        raise local.PlatformError("An operations secret generation is missing; run weave platform up again.")
    return _root(state) / "secrets" / (name + "-" + generation)


def _oauth(token_origin: str, client: str, secret: str) -> dict[str, str]:
    return {
        "token_endpoint": token_origin + TOKEN_PATH,
        "client_id": client,
        "scope": "basic",
        "client_secret_file": CONTAINER + "/secrets/" + secret,
    }


def workers_document(state: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    """The operator-owned workers project: the fixture worker at 0 replicas on the platform network."""
    root = _root(state)
    labels = {"weave.installation": state["id"]}
    if "target_id" in value:
        labels["firefly.weave.target"] = value["target_id"]
    secret = "worker-client-secret"
    return {
        "name": _project(state),
        "services": {
            COMPONENT: {
                "image": value["worker_image"],
                "deploy": {"replicas": 0},
                "stop_grace_period": STOP_GRACE,
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "user": "65532:65532",
                "tmpfs": ["/tmp:size=16777216,mode=1777"],
                "labels": labels,
                "networks": ["platform"],
                "healthcheck": {
                    "test": ["CMD", "python", "-c", "import os; os.kill(1, 0)"],
                    "interval": "10s",
                    "timeout": "3s",
                    "retries": 3,
                },
                "environment": {
                    "WEAVE_API_URL": BRIDGE_API,
                    "WEAVE_ENVIRONMENT_URL": _environment_url(value["scope"]),
                    "WEAVE_WORKER_RELEASE_ID": value["release_id"],
                    "WEAVE_WORKER_OAUTH_CONFIG_FILE": CONTAINER + "/config/worker-oauth.json",
                    "WEAVE_PRIVATE_ORIGINS_FILE": platform_origins.CONTAINER_PATH,
                    "WEAVE_TOKEN_RELAY": "keycloak:8080",
                },
                "volumes": [
                    files._bind(root / "config" / "worker-oauth.json", CONTAINER + "/config/worker-oauth.json"),
                    files._bind(_secret(state, value, secret), CONTAINER + "/secrets/" + secret),
                    files._bind(value["origins_copy"], platform_origins.CONTAINER_PATH),
                ],
            }
        },
        "networks": {"platform": {"name": f"weave-local-{state['id']}_default", "external": True}},
    }


def runner_document(state: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    """The platform's own services: the registry, then the hardened runner that shares Keycloak's network."""
    services: dict[str, Any] = {
        REGISTRY: {
            "image": REGISTRY_IMAGE,
            "restart": "unless-stopped",
            "ports": [f"127.0.0.1:{value['registry_port']}:5000"],
            "volumes": [{"type": "volume", "source": "registry-data", "target": "/var/lib/registry"}],
            "healthcheck": {
                "test": ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:5000/v2/"],
                "interval": "2s",
                "timeout": "3s",
                "retries": 60,
            },
        }
    }
    volumes: dict[str, Any] = {"registry-data": {"name": f"weave-local-{state['id']}-registry"}}
    if "socket_gid" in value:
        root = _root(state)
        secret = "runner-client-secret"
        services[RUNNER] = {
            **files._hardened(value["runner_image"]),
            "group_add": [str(value["socket_gid"])],
            "environment": {
                "DOCKER_CONFIG": CONTAINER + "/docker-config",
                "WEAVE_PRIVATE_ORIGINS_FILE": platform_origins.CONTAINER_PATH,
            },
            "volumes": [
                {"type": "bind", "source": SOCKET, "target": SOCKET},
                files._bind(root / "workers", CONTAINER + "/workers"),
                {"type": "volume", "source": "runner-state", "target": CONTAINER + "/state"},
                files._bind(root / "config", CONTAINER + "/config"),
                files._bind(_secret(state, value, secret), CONTAINER + "/secrets/" + secret),
                files._bind(root / "docker-config", CONTAINER + "/docker-config"),
                files._bind(value["origins_copy"], platform_origins.CONTAINER_PATH),
            ],
        }
        volumes["runner-state"] = {"name": f"weave-local-{state['id']}-runner-state"}
    return {"services": services, "volumes": volumes}


def runner_configuration(state: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    from firefly_weave.contracts.access import Scope
    from firefly_weave.deployment_runner.application import RunnerConfiguration
    from firefly_weave.deployment_runner.policy import DestinationPolicy

    policy = DestinationPolicy(
        target_id=UUID(value["target_id"]),
        adapter="docker-compose",
        external_identity=state["engine"],
        boundary=_project(state),
        executable="/usr/local/bin/docker",
        context="weave-runner",
        capabilities=list(CAPABILITIES),
        components=components(state),
        image_repositories=[f"127.0.0.1:{value['registry_port']}/example-sleep"],
        compose_file=CONTAINER + "/workers/compose.json",
        lock_file=CONTAINER + "/state/runner.lock",
    )
    configuration = RunnerConfiguration(
        base_url=API_ORIGIN,
        scope=Scope.model_validate(value["scope"]),
        destination=policy,
        oauth_file=CONTAINER + "/config/runner-oauth.json",
    )
    return configuration.model_dump(mode="json", exclude_none=True)


def _docker_config() -> dict[str, bytes]:
    """A private Docker configuration whose current context is the mounted socket, by relative path."""
    meta = {"Name": "weave-runner", "Metadata": {}, "Endpoints": {"docker": {"Host": "unix://" + SOCKET}}}
    digest = hashlib.sha256(b"weave-runner").hexdigest()
    return {
        "config.json": files._json({"currentContext": "weave-runner"}),
        f"contexts/meta/{digest}/meta.json": files._json(meta),
    }


def _write_files(state: dict[str, Any], value: dict[str, Any]) -> bool:
    """Render every file the receipt describes; True when any changed. Files are 0444 in 0755 directories."""
    changed = False
    config = _shared(state, "config")
    if "release_id" in value and "origins_copy" in value:
        workers = _shared(state, "workers")
        changed = files._publish(workers / "compose.json", files._json(workers_document(state, value))) or changed
        # OAuth clients take plain HTTP on loopback only: the worker signs in through its own loopback relay,
        # which forwards to Keycloak on the platform network after checking the bridge entry.
        oauth = _oauth(KEYCLOAK_ORIGIN, WORKER_CLIENT, "worker-client-secret")
        changed = files._publish(config / "worker-oauth.json", files._json(oauth)) or changed
    if "target_id" in value:
        runner = files._json(runner_configuration(state, value))
        changed = files._publish(config / "runner.json", runner) or changed
        oauth = _oauth(KEYCLOAK_ORIGIN, RUNNER_CLIENT, "runner-client-secret")
        changed = files._publish(config / "runner-oauth.json", files._json(oauth)) or changed
        for name, data in _docker_config().items():
            path = _shared(state, "docker-config")
            for part in Path(name).parent.parts:
                path = files._shared(path / part)
            changed = files._publish(path / Path(name).name, data) or changed
    if "registry_port" in value:
        changed = files._private(Path(state["directory"]) / COMPOSE, files._json(runner_document(state, value))) or (
            changed
        )
    return changed


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(read_file(path, 65536, private=path.name == COMPOSE)).hexdigest()
    except (OSError, DeploymentError):
        return None


def _rendered(state: dict[str, Any]) -> dict[str, Path]:
    directory = Path(state["directory"])
    return {
        COMPOSE: directory / COMPOSE,
        "workers/compose.json": directory / ROOT / "workers" / "compose.json",
        "config/runner.json": directory / ROOT / "config" / "runner.json",
    }


def compose_file(state: dict[str, Any]) -> Path | None:
    """compose.operations.json for platform Compose commands, only when it is the file platform commands wrote.

    The receipt keeps each rendered file's digest, so an outside edit is refused while a newer CLI that renders
    the same receipt differently still starts the services it wrote, then rewrites them on its next up.
    """
    path = Path(state["directory"]) / COMPOSE
    if not os.path.lexists(path):
        return None
    value = receipt(Path(state["directory"]))
    if value is None or _digest(path) != (value.get("files") or {}).get(COMPOSE):
        raise local.PlatformError(CHANGED_OUTSIDE)
    return path


def verify_files(state: dict[str, Any], value: dict[str, Any]) -> None:
    """Refuse when a rendered file differs from the one platform commands wrote; nothing is restored silently."""
    recorded = value.get("files") or {}
    for name, path in _rendered(state).items():
        if name in recorded and os.path.lexists(path) and _digest(path) != recorded[name]:
            raise local.PlatformError(CHANGED_OUTSIDE)


# Secrets


def _publish_secrets(state: dict[str, Any], value: dict[str, Any]) -> dict[str, bytes]:
    """The two client secrets as 0444 generation files; an existing generation is kept."""
    store = _shared(state, "secrets")
    generations = _generations(value)
    found: dict[str, bytes] = {}
    for name in _SECRET_NAMES:
        generation = generations.get(name)
        path = store / (name + "-" + generation) if generation else None
        data = files._current(path, 0o444, 4096) if path is not None else None
        if data is None:
            generation = secrets.token_hex(16)
            generations[name] = generation
            data = (secrets.token_urlsafe(32) + "\n").encode()
            value["secret_generations"] = dict(generations)
            # The generation name is saved before its file exists, so a resumed run never writes a second secret.
            _save(Path(state["directory"]), value)
            local._atomic(store / (name + "-" + generation), data, mode=0o444)
        found[name] = data.strip()
    value["secret_generations"] = dict(generations)
    return found


# Stages


async def _keycloak_clients(
    state: dict[str, Any], secrets_by_name: dict[str, bytes], transport: httpx.AsyncBaseTransport | None
) -> dict[str, str]:
    """Create or update the two confidential clients from weave-worker's mappers; their service-account subjects."""
    import httpx

    keycloak = f"http://localhost:{state['ports']['keycloak']}"
    admin = local._env_file(Path(state["directory"]) / "identity.env").get("WEAVE_KC_ADMIN_SECRET", "")
    clients = keycloak + "/admin/realms/weave/clients"
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client:
        token = await local._keycloak_admin(client, keycloak, admin)

        async def find(client_id: str) -> dict[str, Any] | None:
            status, found = await local._keycloak(client, "GET", clients, token=token, params={"clientId": client_id})
            records = [item for item in found or [] if isinstance(item, dict) and item.get("clientId") == client_id]
            if status != 200 or len(records) > 1:
                raise local.PlatformError("The local Keycloak clients could not be read; nothing was changed.")
            return records[0] if records else None

        template = await find("weave-worker")
        if template is None or not isinstance(template.get("protocolMappers"), list):
            raise local.PlatformError("The local Keycloak worker client could not be read; nothing was changed.")
        mappers = [{k: v for k, v in item.items() if k != "id"} for item in template["protocolMappers"]]
        subjects = {}
        for client_id, name in ((WORKER_CLIENT, "worker-client-secret"), (RUNNER_CLIENT, "runner-client-secret")):
            representation = {
                "clientId": client_id,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "secret": secrets_by_name[name].decode(),
                "serviceAccountsEnabled": True,
                "directAccessGrantsEnabled": False,
                "standardFlowEnabled": False,
                "implicitFlowEnabled": False,
                "fullScopeAllowed": False,
                "defaultClientScopes": ["basic"],
                "protocolMappers": mappers,
            }
            existing = await find(client_id)
            if existing is None:
                async with client.stream(
                    "POST", clients, headers={"Authorization": "Bearer " + token}, json=representation
                ) as response:
                    if response.status_code != 201:
                        raise local.PlatformError("The local Keycloak refused an operations client; nothing was used.")
                existing = await find(client_id)
                if existing is None:
                    raise local.PlatformError("The local Keycloak did not keep an operations client.")
            else:
                update = {**existing, "secret": representation["secret"]}
                async with client.stream(
                    "PUT", clients + "/" + existing["id"], headers={"Authorization": "Bearer " + token}, json=update
                ) as response:
                    if response.status_code not in (200, 204):
                        raise local.PlatformError("The local Keycloak refused an operations client update.")
            status, account = await local._keycloak(
                client, "GET", f"{clients}/{existing['id']}/service-account-user", token=token
            )
            subject = account.get("id") if isinstance(account, dict) else None
            if status != 200 or not isinstance(subject, str) or str(UUID(subject)) != subject:
                raise local.PlatformError("The local Keycloak returned an unexpected service account.")
            subjects[client_id] = subject
        return subjects


async def _principals(
    client: WeaveClient, state: dict[str, Any], value: dict[str, Any], save: Callable[[], None]
) -> None:
    """The worker and runner principals, each linked to its client's service account."""
    from firefly_weave.contracts.members import PrincipalCreateRequest, PrincipalIdentityRequest
    from firefly_weave.sdk.members import MembersClient

    members = MembersClient(client)
    issuer = f"http://localhost:{state['ports']['keycloak']}/realms/weave"
    for role, kind, client_id in (("worker", "worker", WORKER_CLIENT), ("runner", "application", RUNNER_CLIENT)):
        if value.get(role + "_principal_id") is None:
            created = await members.create_principal(PrincipalCreateRequest(kind=kind))  # type: ignore[arg-type]
            value[role + "_principal_id"] = str(created.id)
            save()
        if not value.get(role + "_linked"):
            request = PrincipalIdentityRequest(
                provider_id=local._PROVIDER_ID, issuer=issuer, subject=value["subjects"][client_id]
            )
            await members.link_identity(UUID(value[role + "_principal_id"]), request)
            value[role + "_linked"] = True
            save()


async def _release(
    client: WeaveClient, value: dict[str, Any], manifest: dict[str, Any], save: Callable[[], None]
) -> None:
    from firefly_weave.contracts.members import MemberGrantRequest
    from firefly_weave.contracts.workers import ReleaseRequest
    from firefly_weave.sdk.members import MembersClient

    if value.get("release_id") is None:
        request = ReleaseRequest.model_validate_json(json.dumps({"image_digest": value["worker_digest"], **manifest}))
        value["release_id"] = str((await client.invoke("releases.create", body=request)).id)
        save()
    if value.get("worker_granted") != value["release_id"]:
        scope = value["scope"]
        await MembersClient(client).grant(
            MemberGrantRequest(
                principal_id=UUID(value["worker_principal_id"]),
                role="worker",
                project_id=UUID(scope["project_id"]),
                environment_id=UUID(scope["environment_id"]),
                resources=(value["release_id"], TASK_TYPE),
            )
        )
        value["worker_granted"] = value["release_id"]
        save()


def target_request(state: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    """local-docker: one person may plan and approve on their own laptop."""
    return {
        "name": TARGET,
        "adapter": "docker-compose",
        "external_identity": state["engine"],
        "boundary": _project(state),
        "runner_principal_id": value["runner_principal_id"],
        "capabilities": list(CAPABILITIES),
        "approval_policy": {"distinct_approver": False},
    }


async def _target(client: WeaveClient, state: dict[str, Any], value: dict[str, Any], save: Callable[[], None]) -> None:
    from firefly_weave.contracts.deployments import DeploymentRequest, TargetRequest
    from firefly_weave.contracts.members import MemberGrantRequest
    from firefly_weave.sdk.members import MembersClient

    key = "weave-platform-operations-" + state["id"]
    if not value.get("host_planner"):
        # The host account administers the demo tenant but plans no deployments; registering local-docker and its
        # deployment needs target.manage, so it grants itself the planner role here (audited like any grant).
        host = strict_json(read_file(Path(state["directory"]) / "bootstrap.json", 65536, private=True))["principal_id"]
        scope = value["scope"]
        await MembersClient(client).grant(
            MemberGrantRequest(
                principal_id=UUID(host),
                role="deployment_planner",
                project_id=UUID(scope["project_id"]),
                environment_id=UUID(scope["environment_id"]),
            )
        )
        value["host_planner"] = True
        save()
    if value.get("target_id") is None:
        request = TargetRequest.model_validate_json(json.dumps(target_request(state, value)))
        created = await client.invoke("deployment_targets.create", body=request, idempotency_key=key + "-target")
        value["target_id"] = str(created.id)
        save()
    if value.get("deployment_id") is None:
        deployment = DeploymentRequest.model_validate_json(
            json.dumps(
                {
                    "target_id": value["target_id"],
                    "name": DEPLOYMENT,
                    "ownership": "managed",
                    "components": [
                        {
                            "name": COMPONENT,
                            "kind": "worker",
                            "image": value["worker_image"],
                            "configuration": "local",
                            "replicas": 0,
                            "worker_release_id": value["release_id"],
                        }
                    ],
                }
            )
        )
        created = await client.invoke("deployments.create", body=deployment, idempotency_key=key + "-deployment")
        value["deployment_id"] = str(created.id)
        save()
    if value.get("runner_granted") != value["target_id"]:
        scope = value["scope"]
        await MembersClient(client).grant(
            MemberGrantRequest(
                principal_id=UUID(value["runner_principal_id"]),
                role="deployment_runner",
                project_id=UUID(scope["project_id"]),
                environment_id=UUID(scope["environment_id"]),
                resources=(value["target_id"],),
            )
        )
        value["runner_granted"] = value["target_id"]
        save()


async def _runner_online(client: WeaveClient, value: dict[str, Any], *, seconds: float = RUNNER_SECONDS) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        page = await client.invoke("deployment_runners.list", query={"target_id": value["target_id"]})
        now = datetime.now(UTC)
        if any(not item.revoked and item.expires_at > now for item in page.items):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(2)


async def _api(
    state: dict[str, Any],
    value: dict[str, Any],
    work: Callable[[WeaveClient], Awaitable[Any]],
    transport: httpx.AsyncBaseTransport | None,
) -> Any:
    from firefly_weave.contracts.access import Scope
    from firefly_weave.sdk.client import WeaveClient
    from firefly_weave.sdk.errors import WeaveError

    api = local._summary(state)["api_url"]
    token = local._host_token(state)
    async with WeaveClient(
        api, lambda: token, Scope.model_validate(value["scope"]), timeout=60, transport=transport
    ) as client:
        try:
            return await work(client)
        except WeaveError as error:
            raise local.PlatformError(
                f"The local API refused an operations setup request ({error.code}); finished stages are kept."
            ) from None


def _build(state: dict[str, Any], name: str, arguments: list[str]) -> str:
    directory = Path(state["directory"])
    path = directory / f"operations-{name}-{os.urandom(4).hex()}.id"
    local._run(
        state, f"operations-{name}-build", [*_docker(state), "build", "--iidfile", str(path), *arguments], timeout=1800
    )
    built = read_file(path, 128).decode().strip()
    path.unlink()
    if _IMAGE_ID.fullmatch(built) is None:
        raise local.PlatformError("Docker did not report an immutable image identity; nothing was started.")
    return built


def _images(state: dict[str, Any], value: dict[str, Any], notice: Notice) -> None:
    context = Path(state["directory"]) / "release" / "images"
    if not (context / "operations-worker.py").is_file():
        raise local.PlatformError(
            "The retained release has no operations fixture worker; create a new installation to use operations."
        )
    notice("Building the operations runner and the sleep fixture worker")
    if "runner_image" not in value:
        value["runner_image"] = _build(state, "runner", ["--target", "runner", str(context)])
        _save(Path(state["directory"]), value)
    if "worker_image_id" not in value:
        value["worker_image_id"] = _build(
            state, "worker", ["-f", str(context / "worker.Dockerfile"), "--target", "operations-worker", str(context)]
        )
        _save(Path(state["directory"]), value)
    worker = value["worker_image_id"]
    tag = f"127.0.0.1:{value['registry_port']}/example-sleep:1.0.0"
    local._run(state, "operations-worker-tag", [*_docker(state), "tag", worker, tag])
    local._run(state, "operations-worker-push", [*_docker(state), "push", tag], timeout=600)
    inspected = strict_json(
        local._run(state, "operations-worker-inspect", [*_docker(state), "image", "inspect", worker])
    )
    repository = f"127.0.0.1:{value['registry_port']}/example-sleep@"
    digests = [item for item in inspected[0].get("RepoDigests") or [] if item.startswith(repository)]
    if len(digests) != 1 or re.fullmatch(r"sha256:[a-f0-9]{64}", digests[0].rsplit("@", 1)[1]) is None:
        raise local.PlatformError("The local registry did not report the fixture worker's digest.")
    value["worker_image"] = digests[0]
    value["worker_digest"] = digests[0].rsplit("@", 1)[1]


def _manifest(state: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    output = local._run(
        state,
        "operations-manifest",
        [
            *_docker(state),
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            value["worker_image_id"],
            "python",
            "-I",
            "/opt/weave/operations-worker/main.py",
            "--release-manifest",
        ],
        timeout=120,
    )
    manifest = strict_json(output)
    if not isinstance(manifest, dict) or [
        f"{item.get('taskType')}@{item.get('taskVersion')}" for item in manifest.get("capabilities", [])
    ] != [TASK_TYPE]:
        raise local.PlatformError("The fixture worker image reported an unexpected manifest; nothing was admitted.")
    return manifest


def _socket_group(state: dict[str, Any], value: dict[str, Any]) -> int:
    output = local._run(
        state,
        "operations-socket",
        [
            *_docker(state),
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "-v",
            f"{SOCKET}:{SOCKET}:ro",
            value["runner_image"],
            "-c",
            f"import os; print(os.stat('{SOCKET}').st_gid)",
        ],
        timeout=120,
    )
    text = output.decode().strip()
    if not text.isdigit():
        raise local.PlatformError("The Docker socket's group could not be read; the runner was not started.")
    return int(text)


def enable(
    directory: Path,
    state: dict[str, Any],
    progress: Notice = _quiet,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Set up local operations, resuming from the receipt; each finished stage is kept."""
    from firefly_weave.sdk import platform_ai, platform_ai_setup

    with local._exclusive(directory, LOCK, "Another operations command is active; retry after it ends."):
        scope = local._scope_value(local._demo_receipt(directory)["scope"])
        saved = receipt(directory)
        if saved is not None and saved["scope"] != scope:
            raise local.PlatformError("The saved operations settings belong to another workspace; nothing was changed.")
        value: dict[str, Any] = dict(saved or {"format": FORMAT, "stage": STAGES[0], "scope": scope})
        if saved is not None:
            verify_files(state, value)

        def advance(stage: str | None = None) -> None:
            if stage is not None and STAGES.index(stage) > STAGES.index(value["stage"]):
                value["stage"] = stage
            _save(directory, value)
            _write_files(state, value)
            value["files"] = {
                name: digest for name, path in _rendered(state).items() if (digest := _digest(path)) is not None
            }
            _save(directory, value)

        def save() -> None:
            advance(None)

        stage = "registry"
        try:
            ports = state.setdefault("ports", {})
            if "registry" not in ports:
                ports["registry"] = platform_ai._free_port()
                local._write(directory / "platform.json", state, replace=True)
            value["registry_port"] = ports["registry"]
            advance()
            progress("Starting the local registry")
            local._run(
                state,
                "operations-registry",
                [*local._compose(state), "up", "--detach", "--no-deps", "--wait", "--wait-timeout", "180", REGISTRY],
                env=local._compose_env(state),
                timeout=600,
            )
            advance("registry")

            stage = "images"
            if value.get("worker_image") is None:
                _images(state, value, progress)
            advance("images")

            stage = "identities"
            found = _publish_secrets(state, value)
            asyncio.run(
                platform_ai_setup.ensure_worker_scope(
                    f"http://localhost:{state['ports']['keycloak']}",
                    local._env_file(directory / "identity.env").get("WEAVE_KC_ADMIN_SECRET", ""),
                    transport,
                )
            )
            value["subjects"] = asyncio.run(_keycloak_clients(state, found, transport))
            advance()
            asyncio.run(_api(state, value, lambda client: _principals(client, state, value, save), transport))
            advance("identities")

            stage = "release"
            manifest = _manifest(state, value)
            asyncio.run(_api(state, value, lambda client: _release(client, value, manifest, save), transport))
            advance("release")

            stage = "workers"
            # The workers' copy of the private-origin file is named by its content; origins rewrites it.
            advance("workers")

            stage = "origins"
            subnet = platform_ai._subnet(state)
            platform_origins.set_operations_entries(state, entries(state, subnet))
            value["origins_copy"] = str(platform_origins.container_copy(state))
            advance("origins")

            stage = "target"
            asyncio.run(_api(state, value, lambda client: _target(client, state, value, save), transport))
            advance("target")

            stage = "runner"
            if "socket_gid" not in value:
                value["socket_gid"] = _socket_group(state, value)
            advance()
            progress("Starting the operations runner")
            local._run(
                state,
                "operations-runner",
                [
                    *local._compose(state),
                    "up",
                    "--detach",
                    "--no-deps",
                    "--pull",
                    "never",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    RUNNER,
                ],
                env=local._compose_env(state),
                timeout=600,
            )
            if not asyncio.run(_api(state, value, lambda client: _runner_online(client, value), transport)):
                raise local.PlatformError("The operations runner did not register within 60 seconds.")
            advance("runner")
            advance("ready")
        except local.PlatformError as error:
            raise local.PlatformError(
                f"Operations setup stopped at {stage}: {error} Inspect the private operations-*.log file, then run "
                "up again; finished stages are kept."
            ) from None
        return {
            "stage": value["stage"],
            "registry": f"127.0.0.1:{value['registry_port']}",
            "target_id": value["target_id"],
            "runner": RUNNER,
            "component": COMPONENT,
            "release_id": value["release_id"],
        }


def status(state: dict[str, Any]) -> dict[str, Any]:
    """The setup stage and the runner container's state; "skipped" before operations were enabled."""
    value = receipt(Path(state["directory"]))
    if value is None:
        return {"stage": "skipped", "runner": "absent", "target": TARGET}
    output = local._run(
        state,
        "operations-status",
        [
            *_docker(state),
            "ps",
            "--all",
            "--format",
            "{{.State}}",
            "--filter",
            f"label=com.docker.compose.project=weave-local-{state['id']}",
            "--filter",
            f"label=com.docker.compose.service={RUNNER}",
        ],
    )
    states = output.decode().split()
    runner = "absent" if not states else "online" if states == ["running"] else "offline"
    return {"stage": value["stage"], "runner": runner, "target": TARGET}


def doctor_notes() -> list[str]:
    return [DOCTOR_NOTE]


def stop_services(state: dict[str, Any]) -> None:
    """Stop the runner and the registry with the platform; their data is kept."""
    path = compose_file(state)
    if path is None:
        return
    services = sorted(strict_json(read_file(path, 65536, private=True))["services"])
    local._run(
        state,
        "operations-stop",
        [*local._compose(state), "stop", "--timeout", "30", *services],
        env=local._compose_env(state),
    )
