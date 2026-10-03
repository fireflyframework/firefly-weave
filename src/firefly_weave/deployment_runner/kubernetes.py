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

"""Pinned-namespace Kubernetes observation and conditional changes to existing deployments."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from firefly_weave.contracts.deployments import ComponentSpec, DeploymentPlan, ObservationReport, ObservedResource
from firefly_weave.deployment_runner.command import run_command
from firefly_weave.deployment_runner.policy import ComponentBinding, DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError

TARGET_LABEL = "firefly.weave/target"
CONFIGURATION_ANNOTATION = "firefly.weave/configuration"


def _quantity(value: Any) -> Decimal:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)([numkKMGTPE]|[KMGTPE]i)?", str(value))
    if match is None or len(str(value)) > 64:
        raise RunnerPolicyError()
    suffix = match[2] or ""
    factors: dict[str, Decimal | int] = {
        "": 1,
        "n": Decimal("1e-9"),
        "u": Decimal("1e-6"),
        "m": Decimal("1e-3"),
        "k": 1000,
    }
    factors.update({unit: 1000**power for power, unit in enumerate("KMGTPE", 1)})
    factors.update({unit + "i": 1024**power for power, unit in enumerate("KMGTPE", 1)})
    return Decimal(match[1]) * factors[suffix]


class KubernetesAdapter:
    def __init__(self, policy: DestinationPolicy, *, command: Callable[..., Awaitable[bytes]] = run_command) -> None:
        if policy.adapter != "kubernetes":
            raise RunnerPolicyError()
        self.policy, self.command = policy, command

    async def _json(self, *args: str, stdin: bytes | None = None) -> dict[str, Any]:
        raw = await self.command(
            [
                self.policy.executable,
                "--context",
                self.policy.context,
                "--namespace",
                self.policy.boundary,
                "--request-timeout=20s",
                *args,
            ],
            stdin=stdin,
            timeout=30,
            limit=1024 * 1024,
        )
        try:
            value = json.loads(raw) if raw.strip() else {}
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise RunnerPolicyError() from None

    async def _identity(self) -> None:
        namespace = await self._json("get", "namespace", self.policy.boundary, "-o=json")
        metadata = namespace.get("metadata", {})
        if metadata.get("name") != self.policy.boundary or metadata.get("uid") != self.policy.external_identity:
            raise RunnerPolicyError()

    async def _deployment(self, binding: ComponentBinding) -> dict[str, Any]:
        value = await self._json("get", "deployments.apps", binding.name, "--ignore-not-found=true", "-o=json")
        if value:
            metadata = value.get("metadata", {})
            if (
                value.get("apiVersion") != "apps/v1"
                or value.get("kind") != "Deployment"
                or metadata.get("name") != binding.name
                or metadata.get("namespace") != self.policy.boundary
                or not metadata.get("uid")
                or not metadata.get("resourceVersion")
            ):
                raise RunnerPolicyError()
        return value

    def _container(self, value: dict[str, Any], binding: ComponentBinding) -> tuple[int, dict[str, Any]]:
        containers = value.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        found = [(i, item) for i, item in enumerate(containers) if item.get("name") == binding.container]
        if len(found) != 1:
            raise RunnerPolicyError()
        return found[0]

    def _fact(self, value: dict[str, Any], binding: ComponentBinding) -> ObservedResource:
        _, container = self._container(value, binding)
        metadata, spec, status = value["metadata"], value["spec"], value.get("status", {})
        desired = spec.get("replicas", 1)
        replicas = max(desired, status.get("replicas", 0))
        ready = status.get("readyReplicas", 0)
        generation = metadata.get("generation")
        settled = (
            isinstance(generation, int)
            and status.get("observedGeneration", -1) >= generation
            and replicas == desired
            and ready == desired
            and status.get("updatedReplicas", 0) == desired
            and status.get("availableReplicas", 0) == desired
        )
        image = container.get("image", "")
        # An imported tag is observable, but cannot be presented as an immutable release.
        immutable = image if self.policy.allows_image(image) else None
        return ObservedResource(
            name=binding.name,
            external_identity="deployment/" + str(metadata["uid"]),
            kind=binding.kind,
            image=immutable,
            replicas=replicas,
            ready_replicas=ready,
            version=metadata["resourceVersion"],
            ownership="managed"
            if metadata.get("labels", {}).get(TARGET_LABEL) == str(self.policy.target_id)
            else "imported",
            state=("stopped" if desired == 0 else "ready") if settled else "progressing",
        )

    async def observe(self) -> ObservationReport:
        await self._identity()
        resources = []
        for binding in self.policy.components:
            value = await self._deployment(binding)
            if value:
                resources.append(self._fact(value, binding))
        return ObservationReport(
            resources=resources, complete=True, settled=all(item.state in {"ready", "stopped"} for item in resources)
        )

    async def _singleton_policy(self, binding: ComponentBinding) -> None:
        if binding.kind != "api":
            return
        autoscalers = await self._json("get", "horizontalpodautoscalers.autoscaling", "--chunk-size=101", "-o=json")
        items = autoscalers.get("items")
        if not isinstance(items, list) or len(items) > 100 or autoscalers.get("metadata", {}).get("continue"):
            raise RunnerPolicyError()
        if any(
            item.get("spec", {}).get("scaleTargetRef", {}).get("name") == binding.name
            and item.get("spec", {}).get("scaleTargetRef", {}).get("kind") == "Deployment"
            for item in items
        ):
            raise RunnerPolicyError()

    async def apply(self, plan: DeploymentPlan, before_write: Callable[[], Awaitable[None]]) -> ObservationReport:
        if (
            plan.target_id != self.policy.target_id
            or plan.adapter != "kubernetes"
            or plan.intent not in {"update", "scale_workers"}
            or plan.intent not in self.policy.capabilities
        ):
            raise RunnerPolicyError()
        await self._identity()
        bindings = {item.name: item for item in self.policy.components}
        prepared = []
        for step in plan.steps:
            component = step.component
            binding = bindings.get(component.name)
            if (
                binding is None
                or binding.kind != component.kind
                or binding.configuration != component.configuration
                or step.action != plan.intent
                or not self.policy.allows_image(component.image)
                or (step.action == "scale_workers" and component.kind != "worker")
            ):
                raise RunnerPolicyError()
            await self._singleton_policy(binding)
            value = await self._deployment(binding)
            if not value or value["metadata"]["resourceVersion"] != step.expected_version:
                raise RunnerPolicyError()
            index, container = self._container(value, binding)
            spec = value["spec"]
            if (
                value["metadata"].get("labels", {}).get(TARGET_LABEL) not in {None, str(self.policy.target_id)}
                or spec["template"].get("metadata", {}).get("annotations", {}).get(CONFIGURATION_ANNOTATION)
                != binding.configuration
                or (
                    component.kind == "api"
                    and (spec.get("strategy", {}).get("type") != "Recreate" or spec.get("replicas", 1) != 1)
                )
                or (step.action == "scale_workers" and container.get("image") != component.image)
            ):
                raise RunnerPolicyError()
            patch: list[dict[str, Any]] = [
                {"op": "test", "path": "/metadata/uid", "value": value["metadata"]["uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": step.expected_version},
            ]
            if "labels" not in value["metadata"]:
                patch.append({"op": "add", "path": "/metadata/labels", "value": {}})
            patch.extend(
                [
                    {
                        "op": "add",
                        "path": "/metadata/labels/firefly.weave~1target",
                        "value": str(self.policy.target_id),
                    },
                    {"op": "add", "path": "/spec/replicas", "value": component.replicas},
                ]
            )
            base = f"/spec/template/spec/containers/{index}"
            if step.action == "update":
                amounts = {"cpu": f"{component.cpu_millis}m", "memory": f"{component.memory_mib}Mi"}
                resources = container.get("resources", {})
                patch.extend(
                    [
                        {"op": "add", "path": base + "/image", "value": component.image},
                        {
                            "op": "add",
                            "path": base + "/resources",
                            "value": {
                                **resources,
                                "requests": {**resources.get("requests", {}), **amounts},
                                "limits": {**resources.get("limits", {}), **amounts},
                            },
                        },
                    ]
                )
            else:
                patch.append({"op": "test", "path": base + "/image", "value": component.image})
            prepared.append((binding, component, json.dumps(patch, separators=(",", ":")).encode()))
        # Validate all components before the first effect; each actual write still has server-side preconditions.
        for binding, component, patch_bytes in prepared:
            predicted = await self._patch(binding, patch_bytes, dry=True)
            self._desired(predicted, binding, component, resources=plan.intent == "update")
        for binding, component, patch_bytes in prepared:
            await self._identity()
            await self._singleton_policy(binding)
            await before_write()
            applied = await self._patch(binding, patch_bytes, dry=False)
            self._desired(applied, binding, component, resources=plan.intent == "update")
        async with asyncio.timeout(90):
            while True:
                result = await self.observe()
                for binding, component, _ in prepared:
                    self._desired(
                        await self._deployment(binding), binding, component, resources=plan.intent == "update"
                    )
                facts = {item.name: item for item in result.resources}
                if all(
                    step.component.name in facts
                    and facts[step.component.name].image == step.component.image
                    and facts[step.component.name].replicas == step.component.replicas
                    and facts[step.component.name].ready_replicas == step.component.replicas
                    and facts[step.component.name].ownership == "managed"
                    and facts[step.component.name].state in {"ready", "stopped"}
                    for step in plan.steps
                ):
                    return result
                await asyncio.sleep(1)

    def _desired(
        self, value: dict[str, Any], binding: ComponentBinding, component: ComponentSpec, *, resources: bool
    ) -> None:
        _, container = self._container(value, binding)
        if (
            (binding.kind == "api" and value.get("spec", {}).get("strategy", {}).get("type") != "Recreate")
            or container.get("image") != component.image
            or value.get("spec", {}).get("replicas") != component.replicas
            or value.get("metadata", {}).get("labels", {}).get(TARGET_LABEL) != str(self.policy.target_id)
        ):
            raise RunnerPolicyError()
        if resources:
            for field in ("requests", "limits"):
                amounts = container.get("resources", {}).get(field, {})
                if (
                    _quantity(amounts.get("cpu")) != Decimal(component.cpu_millis) / 1000
                    or _quantity(amounts.get("memory")) != component.memory_mib * 1024**2
                ):
                    raise RunnerPolicyError()

    async def _patch(self, binding: ComponentBinding, patch: bytes, *, dry: bool) -> dict[str, Any]:
        args = ["patch", "deployments.apps", binding.name, "--type=json", "--patch-file=/dev/stdin", "-o=json"]
        if dry:
            args.append("--dry-run=server")
        return await self._json(*args, stdin=patch)
