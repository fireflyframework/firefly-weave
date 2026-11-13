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

"""Runner startup: private local authority and renewable dedicated machine identity."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any, Self, cast
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from firefly_weave import private_origins
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.deployments import (
    DeploymentLease,
    DeploymentRunner,
    RunnerClaimRequest,
    RunnerRegistration,
)
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import LOG, InfrastructureAdapter, OperationsRunner
from firefly_weave.sdk.client import AsyncTokenProvider, WeaveClient
from firefly_weave.sdk.token_relay import start_relay
from firefly_weave.sdk.worker_auth import ClientCredentialsTokenProvider, _read_mount, private_transport

LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
# Set in a cluster: the Keycloak address the loopback token relay forwards to, and the file a liveness probe reads.
TOKEN_RELAY = "WEAVE_TOKEN_RELAY"
HEARTBEAT_FILE = "WEAVE_RUNNER_HEARTBEAT_FILE"


def _private_plaintext(base_url: str) -> bool:
    """Plain HTTP outside loopback, approved by an exact runner entry of the active private-origin policy."""
    target = urlsplit(base_url)
    return (
        target.scheme == "http"
        and target.hostname not in LOOPBACK
        and private_origins.active().permits_plaintext("runner", base_url)
    )


class RunnerConfiguration(ContractModel):
    base_url: str = Field(min_length=1, max_length=2048)
    scope: Scope
    destination: DestinationPolicy
    oauth_file: str | None = None
    token_file: str | None = None
    poll_seconds: float = Field(default=2, ge=1, le=60)

    @model_validator(mode="after")
    def dedicated_credentials_and_scope(self) -> Self:
        target = urlsplit(self.base_url)
        if (
            target.scheme not in {"https", "http"}
            or not target.hostname
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.path not in {"", "/"}
            or (target.scheme == "http" and target.hostname not in LOOPBACK and not _private_plaintext(self.base_url))
            or self.scope.project_id is None
            or self.scope.environment_id is None
            or bool(self.oauth_file) == bool(self.token_file)
            or any(value is not None and not Path(value).is_absolute() for value in (self.oauth_file, self.token_file))
        ):
            raise ValueError("Use one mounted machine identity, an API origin, and a complete workspace")
        return self


def load_configuration(path: Path) -> RunnerConfiguration:
    return RunnerConfiguration.model_validate_json(_read_mount(path, 256 * 1024))


def adapter_for(policy: DestinationPolicy) -> InfrastructureAdapter:
    if policy.adapter == "docker-compose":
        from firefly_weave.deployment_runner.compose import ComposeAdapter

        return ComposeAdapter(policy)
    if policy.adapter == "kubernetes":
        from firefly_weave.deployment_runner.kubernetes import KubernetesAdapter

        return KubernetesAdapter(policy)
    from firefly_weave.deployment_runner.azure import AzureContainerAppsAdapter

    return AzureContainerAppsAdapter(policy)


class MountedToken:
    """Rotatable short-lived machine token; no interactive profile or keychain access."""

    def __init__(self, origin: str, path: Path) -> None:
        self.origin, self.path = origin.rstrip("/"), path

    async def get_access_token(self, target: str) -> str:
        if target.rstrip("/") != self.origin:
            raise ValueError("Runner identity belongs to another API origin")
        return _read_mount(self.path, 16384).strip()


# Adapters that remove exactly the members a scale-down plan names (`scale_down_members`); only their runners
# register adapter version 2, which the platform requires before it hands them a scale-down.
REVIEWED_MEMBER_ADAPTERS = frozenset({"docker-compose", "kubernetes"})


def runner_registration(destination: Any) -> RunnerRegistration:
    """What this runner registers: version 2 only where its adapter removes the reviewed members."""
    return RunnerRegistration(
        target_id=destination.target_id,
        adapter=destination.adapter,
        adapter_version="2" if destination.adapter in REVIEWED_MEMBER_ADAPTERS else "1",
        capabilities=destination.capabilities,
    )


def _beat(path: Path | None) -> None:
    """Touch the heartbeat file (0600) after the platform answered; a liveness probe restarts a silent runner."""
    if path is None:
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.utime(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        # Never stop the runner over it: a stale heartbeat makes the liveness probe restart it instead.
        LOG.warning("The runner couldn't record its heartbeat: %s", error.strerror or type(error).__name__)


async def serve(config: RunnerConfiguration, *, once: bool = False) -> None:
    adapter = adapter_for(config.destination)
    provider: AsyncTokenProvider
    if config.oauth_file:
        provider = ClientCredentialsTokenProvider.from_file(Path(config.oauth_file), config.base_url)
    else:
        assert config.token_file is not None
        provider = MountedToken(config.base_url, Path(config.token_file))
    heartbeat = Path(os.environ[HEARTBEAT_FILE]) if os.environ.get(HEARTBEAT_FILE) else None
    # In a cluster the API is plain HTTP on the cluster network: only through its runner entry and the pinned,
    # peer-checked transport. HTTPS and loopback keep the client's own transport.
    plain = _private_plaintext(config.base_url)
    transport = private_transport(config.base_url, "runner") if plain else None
    relay = await start_relay(os.environ[TOKEN_RELAY]) if os.environ.get(TOKEN_RELAY) else None
    try:
        async with WeaveClient(
            config.base_url,
            provider,
            config.scope,
            timeout=20,
            transport=transport,
            private_origin="runner" if plain else None,
        ) as client:
            registration = cast(
                DeploymentRunner,
                await client.invoke(
                    "deployment_runners.create",
                    body=runner_registration(config.destination),
                ),
            )
            runner = OperationsRunner(client, config.destination, adapter, on_contact=lambda: _beat(heartbeat))
            loop, task = asyncio.get_running_loop(), asyncio.current_task()
            assert task is not None
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            try:
                while True:
                    lease = cast(
                        DeploymentLease | None,
                        await client.invoke(
                            "deployment_runners.claim", body=RunnerClaimRequest(runner_id=registration.id)
                        ),
                    )
                    _beat(heartbeat)
                    if lease is not None:
                        await runner.execute(lease)
                    if once:
                        return
                    await asyncio.sleep(config.poll_seconds)
            finally:
                loop.remove_signal_handler(signal.SIGTERM)
    finally:
        if relay is not None:
            relay.close()
            await relay.wait_closed()
