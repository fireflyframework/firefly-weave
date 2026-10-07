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

"""Local runner authority, independent of the platform's requested operations."""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.deployments import (
    AdapterKind,
    DeploymentCapability,
    DeploymentName,
    ExternalIdentity,
)


class ComponentBinding(ContractModel):
    name: DeploymentName
    kind: Literal["api", "worker", "lumi"]
    configuration: DeploymentName
    container: DeploymentName | None = None
    max_replicas: int = Field(default=10, ge=1, le=100)
    max_cpu_millis: int = Field(default=4000, ge=100, le=16000)
    max_memory_mib: int = Field(default=8192, ge=128, le=65536)


class DestinationPolicy(ContractModel):
    target_id: UUID
    adapter: AdapterKind
    external_identity: ExternalIdentity
    boundary: DeploymentName
    executable: str = Field(min_length=1, max_length=4096)
    context: DeploymentName
    capabilities: list[DeploymentCapability] = Field(min_length=1, max_length=5)
    components: list[ComponentBinding] = Field(min_length=1, max_length=100)
    image_repositories: list[str] = Field(min_length=1, max_length=100)
    compose_file: str | None = None
    lock_file: str | None = None

    @model_validator(mode="after")
    def bounded_local_authority(self) -> Self:
        if not Path(self.executable).is_absolute() or "\0" in self.executable:
            raise ValueError("An absolute operator-owned executable is required")
        supported = {"observe", "update", "scale_workers"}
        if self.adapter == "azure-container-apps" and not self.lock_file:
            raise ValueError("ACA requires a shared local runner lock")
        if self.adapter == "docker-compose":
            supported.add("deploy")
            if not self.compose_file or not self.lock_file:
                raise ValueError("Compose requires an operator-owned file and shared lock path")
        if "observe" not in self.capabilities or not set(self.capabilities) <= supported:
            raise ValueError("Only implemented capabilities including observe may be enabled")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("Capabilities must be distinct")
        if len({item.name for item in self.components}) != len(self.components):
            raise ValueError("Local component bindings must be distinct")
        if self.adapter in {"kubernetes", "azure-container-apps"} and any(
            not item.container for item in self.components
        ):
            raise ValueError("Kubernetes requires an explicit container for each deployment")
        for path in (self.compose_file, self.lock_file):
            if path is not None and (not Path(path).is_absolute() or "\0" in path):
                raise ValueError("Runner paths must be absolute")
        for repository in self.image_repositories:
            if (
                not repository
                or len(repository) > 440
                or "://" in repository
                or any(part in {"", ".", ".."} for part in repository.split("/"))
                or any(
                    char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/-"
                    for char in repository
                )
                or "@" in repository
                or repository.startswith("-")
            ):
                raise ValueError("An exact OCI repository allowlist is required")
        return self

    def allows_image(self, reference: str) -> bool:
        repository, separator, digest = reference.partition("@sha256:")
        return bool(
            separator
            and repository in self.image_repositories
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
        )
