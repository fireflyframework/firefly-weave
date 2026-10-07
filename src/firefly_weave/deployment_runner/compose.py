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

"""Compose adapter for a pinned daemon and an operator-owned project template."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.deployments import DeploymentPlan, ObservationReport, ObservedResource
from firefly_weave.contracts.values import JsonObject
from firefly_weave.deployment_runner.command import run_command
from firefly_weave.deployment_runner.policy import DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError

_INSPECT = (
    '{"id":{{json .Id}},"image":{{json .Config.Image}},"state":{{json .State.Status}},'
    '"started":{{json .State.StartedAt}},"restart":{{json .RestartCount}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"target":{{json (index .Config.Labels "firefly.weave.target")}},'
    '"health":{{with (index .State "Health")}}{{json .Status}}{{else}}""{{end}}}'
)


class ComposeAdapter:
    def __init__(self, policy: DestinationPolicy, *, command: Callable[..., Awaitable[bytes]] = run_command) -> None:
        if policy.adapter != "docker-compose" or not policy.compose_file or not policy.lock_file:
            raise RunnerPolicyError()
        self.policy, self.command = policy, command
        self.base = [policy.executable, "--context", policy.context]
        self.compose = [
            *self.base,
            "compose",
            "--project-name",
            policy.boundary,
            "--project-directory",
            str(Path(policy.compose_file).parent),
            "--file",
            policy.compose_file,
        ]
        # This private override is the durable configuration of zero-replica services.
        # Its path is derived only from the operator's configured lock, never the API.
        self.overrides = Path(policy.lock_file + ".compose.json")

    async def _json(self, argv: list[str], **kwargs: Any) -> Any:
        return json.loads(await self.command(argv, **kwargs))

    async def _identity(self) -> None:
        if await self._json([*self.base, "info", "--format", "{{json .ID}}"]) != self.policy.external_identity:
            raise RunnerPolicyError()

    def _files(self, override: Path | None = None) -> list[str]:
        path = override or self.overrides
        return [*self.compose, *(["--file", str(path)] if path.exists() else [])]

    async def _configuration(self, override: Path | None = None) -> dict[str, Any]:
        result = await self._json([*self._files(override), "config", "--format", "json"])
        services = result.get("services")
        allowed = {binding.name for binding in self.policy.components}
        if not isinstance(services, dict) or set(services) != allowed:
            raise RunnerPolicyError()
        if any(
            spec.get("labels", {}).get("firefly.weave.target") not in {None, str(self.policy.target_id)}
            for spec in services.values()
        ):
            raise RunnerPolicyError()
        return cast(dict[str, Any], result)

    async def observe(self) -> ObservationReport:
        await self._identity()
        configuration = await self._configuration()
        raw = await self.command([*self._files(), "ps", "--all", "--no-trunc", "--format", "json"])
        # Compose versions return either an array or one JSON object per line;
        # an empty project emits no bytes rather than an empty array.
        inventory = (
            json.loads(raw)
            if raw.lstrip().startswith(b"[")
            else [json.loads(line) for line in raw.splitlines() if line.strip()]
        )
        if not isinstance(inventory, list) or len(inventory) > 1000:
            raise RunnerPolicyError()
        bindings = {binding.name: binding for binding in self.policy.components}
        groups: dict[str, list[dict[str, Any]]] = {name: [] for name in bindings}
        for entry in inventory:
            name, identifier = entry.get("Service"), entry.get("ID")
            if (
                name not in groups
                or not isinstance(identifier, str)
                or len(identifier) != 64
                or any(c not in "0123456789abcdef" for c in identifier)
            ):
                raise RunnerPolicyError()
            fact = await self._json([*self.base, "inspect", "--format", _INSPECT, identifier])
            if (
                fact.get("project") != self.policy.boundary
                or fact.get("service") != name
                or fact.get("id") != identifier
                or fact.get("target") not in {None, "", str(self.policy.target_id)}
            ):
                raise RunnerPolicyError()
            groups[name].append(fact)
        resources = []
        settled = True
        for name, binding in bindings.items():
            members = sorted(groups[name], key=lambda member: member["id"])
            spec = configuration["services"][name]
            image = spec.get("image")
            images = {member.get("image") for member in members}
            if images and images != {image}:
                image = None
            if not isinstance(image, str) or not self.policy.allows_image(image):
                image = None
            ready = sum(item.get("state") == "running" and item.get("health") in {"", "healthy"} for item in members)
            desired = spec.get("deploy", {}).get("replicas", 1)
            stopped = not members and desired == 0
            stable = stopped or (ready == len(members) == desired)
            settled = settled and stable
            managed = spec.get("labels", {}).get("firefly.weave.target") == str(self.policy.target_id) and all(
                item.get("target") == str(self.policy.target_id) for item in members
            )
            resources.append(
                ObservedResource(
                    name=name,
                    external_identity=f"compose/{self.policy.boundary}/{name}",
                    kind=binding.kind,
                    image=image,
                    replicas=len(members),
                    ready_replicas=ready,
                    version=canonical_digest(cast(JsonObject, {"configuration": configuration, "members": members})),
                    ownership="managed" if managed else "imported",
                    state="stopped" if stopped else "ready" if stable else "progressing",
                )
            )
        return ObservationReport(resources=resources, complete=True, settled=settled)

    async def apply(self, plan: DeploymentPlan, before_write: Callable[[], Awaitable[None]]) -> ObservationReport:
        assert self.policy.lock_file is not None
        descriptor = os.open(self.policy.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RunnerPolicyError()
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return await self._apply_locked(plan, before_write)
        finally:
            os.close(descriptor)

    async def _apply_locked(
        self, plan: DeploymentPlan, before_write: Callable[[], Awaitable[None]]
    ) -> ObservationReport:
        current = await self.observe()
        observed = {resource.name: resource for resource in current.resources}
        if self.overrides.exists():
            if self.overrides.stat().st_size > 256 * 1024:
                raise RunnerPolicyError()
            override = json.loads(self.overrides.read_text())
        else:
            override = {"services": {}}
        scales = []
        for step in plan.steps:
            component = step.component
            fact = observed.get(component.name)
            if step.action not in self.policy.capabilities or fact is None or step.expected_version != fact.version:
                raise RunnerPolicyError()
            if step.action == "scale_workers" and (component.kind != "worker" or component.image != fact.image):
                raise RunnerPolicyError()
            spec = override["services"].setdefault(component.name, {})
            spec.setdefault("labels", {})["firefly.weave.target"] = str(self.policy.target_id)
            spec.setdefault("deploy", {})["replicas"] = component.replicas
            if step.action in {"deploy", "update"}:
                spec["image"] = component.image
                spec["cpus"] = component.cpu_millis / 1000
                spec["mem_limit"] = f"{component.memory_mib}M"
                spec["deploy"].setdefault("resources", {})["limits"] = {
                    "cpus": str(component.cpu_millis / 1000),
                    "memory": f"{component.memory_mib}M",
                }
            scales += ["--scale", f"{component.name}={component.replicas}"]
        with tempfile.NamedTemporaryFile(mode="w", dir=self.overrides.parent, suffix=".json", delete=False) as handle:
            candidate = Path(handle.name)
            json.dump(override, handle)
        try:
            await self._configuration(candidate)
            arguments = [
                "up",
                "--detach",
                "--no-deps",
                *scales,
                *[step.component.name for step in plan.steps],
            ]
            # Dry-run cannot make a stopped container ready; only the real command waits.
            await self.command([*self._files(candidate), "--dry-run", *arguments], timeout=120)
            latest = await self.observe()
            if latest != current:
                raise RunnerPolicyError()
            await before_write()
            os.replace(candidate, self.overrides)
            await self.command([*self._files(), *arguments, "--wait", "--wait-timeout", "90"], timeout=120)
            result = await self.observe()
            facts = {fact.name: fact for fact in result.resources}
            if not result.settled or any(
                facts[step.component.name].image != step.component.image
                or facts[step.component.name].ready_replicas != step.component.replicas
                for step in plan.steps
            ):
                raise RunnerPolicyError()
            return result
        finally:
            candidate.unlink(missing_ok=True)
