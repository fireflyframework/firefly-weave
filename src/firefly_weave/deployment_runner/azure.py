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

"""Allowlisted Azure Container Apps observation and serialized worker changes.

ARM's Container Apps PATCH has no documented resource-version precondition.
This adapter rechecks drift under a local operator-shared lock and uses the
platform's target fence. Provider activity after an uncertain call must be
reconciled; it does not claim atomic exclusion against external Azure operators.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from firefly_weave.compiler.canonical import canonical_digest
from firefly_weave.contracts.deployments import DeploymentPlan, ObservationReport, ObservedResource
from firefly_weave.contracts.values import JsonObject
from firefly_weave.deployment_runner.command import run_command
from firefly_weave.deployment_runner.policy import ComponentBinding, DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError

_SHOW = (
    "{id:id,environment:properties.environmentId,managedEnvironment:properties.managedEnvironmentId,"
    "mode:properties.configuration.activeRevisionsMode,state:properties.provisioningState,"
    "latest:properties.latestRevisionName,tags:tags,template:properties.template}"
)
_REVISIONS = (
    "[?properties.active].{name:name,replicas:properties.replicas,health:properties.healthState,"
    "state:properties.runningState,provisioning:properties.provisioningState,"
    "images:properties.template.containers[].image}"
)
_API = "2025-07-01"


class AzureContainerAppsAdapter:
    def __init__(self, policy: DestinationPolicy, *, command: Callable[..., Awaitable[bytes]] = run_command) -> None:
        if policy.adapter != "azure-container-apps" or not policy.lock_file:
            raise RunnerPolicyError()
        try:
            UUID(policy.context)
        except ValueError:
            raise RunnerPolicyError() from None
        self.policy, self.command = policy, command

    def _args(self, name: str) -> list[str]:
        return [
            "--subscription",
            self.policy.context,
            "--resource-group",
            self.policy.boundary,
            "--name",
            name,
            "--only-show-errors",
            "--output",
            "json",
        ]

    async def _read(self, binding: ComponentBinding) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        app = json.loads(
            await self.command(
                [self.policy.executable, "containerapp", "show", *self._args(binding.name), "--query", _SHOW]
            )
        )
        expected = (
            f"/subscriptions/{self.policy.context}/resourceGroups/{self.policy.boundary}"
            f"/providers/Microsoft.App/containerApps/{binding.name}"
        )
        if (
            not isinstance(app, dict)
            or str(app.get("id", "")).lower() != expected.lower()
            or str(app.get("environment") or app.get("managedEnvironment", "")).lower()
            != self.policy.external_identity.lower()
        ):
            raise RunnerPolicyError()
        revisions = json.loads(
            await self.command(
                [
                    self.policy.executable,
                    "containerapp",
                    "revision",
                    "list",
                    *self._args(binding.name),
                    "--query",
                    _REVISIONS,
                ]
            )
        )
        if (
            not isinstance(revisions, list)
            or len(revisions) > 100
            or any(not isinstance(item, dict) for item in revisions)
        ):
            raise RunnerPolicyError()
        return app, revisions

    def _fact(
        self, binding: ComponentBinding, app: dict[str, Any], revisions: list[dict[str, Any]]
    ) -> ObservedResource:
        containers = [item for item in app["template"]["containers"] if item["name"] == binding.container]
        if len(containers) != 1:
            raise RunnerPolicyError()
        image = containers[0]["image"]
        immutable = image if self.policy.allows_image(image) else None
        replicas = sum(item.get("replicas") or 0 for item in revisions)
        ready = sum(
            (item.get("replicas") or 0)
            for item in revisions
            if item.get("health") == "Healthy" and item.get("provisioning") == "Provisioned"
        )
        stopped = not revisions
        stable = app.get("state") == "Succeeded" and (
            stopped
            or (
                len(revisions) == 1
                and revisions[0].get("name") == app.get("latest")
                and ready == replicas
                and revisions[0].get("health") == "Healthy"
                and image in revisions[0].get("images", [])
            )
        )
        if revisions and any(image not in item.get("images", []) for item in revisions):
            immutable = None
        return ObservedResource(
            name=binding.name,
            external_identity=app["id"],
            kind=binding.kind,
            image=immutable,
            replicas=replicas,
            ready_replicas=ready,
            version=canonical_digest(cast(JsonObject, {"app": app, "revisions": revisions})),
            ownership="managed"
            if (app.get("tags") or {}).get("firefly-weave-target") == str(self.policy.target_id)
            else "imported",
            state="stopped" if stopped and stable else "ready" if stable else "progressing",
        )

    async def observe(self) -> ObservationReport:
        resources = []
        for binding in self.policy.components:
            app, revisions = await self._read(binding)
            resources.append(self._fact(binding, app, revisions))
        return ObservationReport(
            resources=resources, complete=True, settled=all(item.state in {"ready", "stopped"} for item in resources)
        )

    async def _patch(self, identifier: str, body: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as handle:
            json.dump(body, handle)
            handle.flush()
            await self.command(
                [
                    self.policy.executable,
                    "rest",
                    "--method",
                    "PATCH",
                    "--url",
                    f"https://management.azure.com{identifier}?api-version={_API}",
                    "--body",
                    "@" + handle.name,
                    "--subscription",
                    self.policy.context,
                    "--query",
                    "{id:id}",
                    "--only-show-errors",
                    "--output",
                    "json",
                ],
                timeout=120,
            )

    async def apply(self, plan: DeploymentPlan, before_write: Callable[[], Awaitable[None]]) -> ObservationReport:
        if (
            plan.target_id != self.policy.target_id
            or plan.adapter != self.policy.adapter
            or plan.intent not in {"update", "scale_workers"}
            or plan.intent not in self.policy.capabilities
        ):
            raise RunnerPolicyError()
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
        bindings = {binding.name: binding for binding in self.policy.components}
        prepared = []
        for step in plan.steps:
            component = step.component
            binding = bindings.get(component.name)
            # ACA API revisions can overlap even in Single mode. Keep scheduler/API
            # upgrades in the deployment runbook; don't imply singleton-safe rolling updates.
            if (
                binding is None
                or binding.kind not in {"worker", "lumi"}
                or component.kind != binding.kind
                or component.configuration != binding.configuration
                or not self.policy.allows_image(component.image)
                or step.action != plan.intent
                or (step.action == "scale_workers" and component.kind != "worker")
            ):
                raise RunnerPolicyError()
            app, revisions = await self._read(binding)
            fact = self._fact(binding, app, revisions)
            if (
                fact.version != step.expected_version
                or app.get("mode") != "Single"
                or fact.state not in {"ready", "stopped"}
            ):
                raise RunnerPolicyError()
            if (app.get("tags") or {}).get("firefly-weave-target") not in {None, str(self.policy.target_id)}:
                raise RunnerPolicyError()
            template = copy.deepcopy(app["template"])
            scale = template.setdefault("scale", {})
            if scale.get("rules") or (step.action == "scale_workers" and component.image != fact.image):
                raise RunnerPolicyError()
            tags = {**(app.get("tags") or {}), "firefly-weave-target": str(self.policy.target_id)}
            body: dict[str, Any] = {"tags": tags}
            if component.replicas:
                container = next(item for item in template["containers"] if item["name"] == binding.container)
                if step.action == "update":
                    container["image"] = component.image
                    container["resources"] = {
                        **container.get("resources", {}),
                        "cpu": component.cpu_millis / 1000,
                        "memory": f"{component.memory_mib / 1024:g}Gi",
                    }
                scale["minReplicas"] = scale["maxReplicas"] = component.replicas
                template["revisionSuffix"] = "weave-" + plan.id.hex[:12]
                body["properties"] = {"template": template}
            elif step.action != "scale_workers":
                raise RunnerPolicyError()
            prepared.append((binding, step, app, revisions, body))
        for binding, step, app, revisions, body in prepared:
            latest, active = await self._read(binding)
            if self._fact(binding, latest, active).version != step.expected_version:
                raise RunnerPolicyError()
            await before_write()
            await self._patch(app["id"], body)
            if step.component.replicas == 0:
                for revision in revisions:
                    await before_write()
                    await self.command(
                        [
                            self.policy.executable,
                            "containerapp",
                            "revision",
                            "deactivate",
                            *self._args(binding.name),
                            "--revision",
                            revision["name"],
                        ],
                        timeout=120,
                    )
        async with asyncio.timeout(180):
            while True:
                report = await self.observe()
                facts = {item.name: item for item in report.resources}
                if report.settled and all(
                    facts[step.component.name].image == step.component.image
                    and facts[step.component.name].ready_replicas == step.component.replicas
                    and facts[step.component.name].replicas == step.component.replicas
                    and facts[step.component.name].ownership == "managed"
                    for step in plan.steps
                ):
                    return report
                await asyncio.sleep(2)
