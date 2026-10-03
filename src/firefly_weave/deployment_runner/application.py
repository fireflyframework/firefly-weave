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
import signal
from pathlib import Path
from typing import Self, cast
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.deployments import (
    DeploymentLease,
    DeploymentRunner,
    RunnerClaimRequest,
    RunnerRegistration,
)
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import InfrastructureAdapter, OperationsRunner
from firefly_weave.sdk.client import AsyncTokenProvider, WeaveClient
from firefly_weave.sdk.worker_auth import ClientCredentialsTokenProvider, _read_mount


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
            or (target.scheme == "http" and target.hostname not in {"localhost", "127.0.0.1", "::1"})
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


async def serve(config: RunnerConfiguration, *, once: bool = False) -> None:
    adapter = adapter_for(config.destination)
    provider: AsyncTokenProvider
    if config.oauth_file:
        provider = ClientCredentialsTokenProvider.from_file(Path(config.oauth_file), config.base_url)
    else:
        assert config.token_file is not None
        provider = MountedToken(config.base_url, Path(config.token_file))
    async with WeaveClient(config.base_url, provider, config.scope, timeout=20) as client:
        registration = cast(
            DeploymentRunner,
            await client.invoke(
                "deployment_runners.create",
                body=RunnerRegistration(
                    target_id=config.destination.target_id,
                    adapter=config.destination.adapter,
                    capabilities=config.destination.capabilities,
                ),
            ),
        )
        runner = OperationsRunner(client, config.destination, adapter)
        loop, task = asyncio.get_running_loop(), asyncio.current_task()
        assert task is not None
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        try:
            while True:
                lease = cast(
                    DeploymentLease | None,
                    await client.invoke("deployment_runners.claim", body=RunnerClaimRequest(runner_id=registration.id)),
                )
                if lease is not None:
                    await runner.execute(lease)
                if once:
                    return
                await asyncio.sleep(config.poll_seconds)
        finally:
            loop.remove_signal_handler(signal.SIGTERM)
