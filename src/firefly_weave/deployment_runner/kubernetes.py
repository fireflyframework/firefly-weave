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

from firefly_weave.contracts.deployments import (
    ComponentSpec,
    DeploymentPlan,
    ObservationReport,
    ObservedMember,
    ObservedResource,
)
from firefly_weave.deployment_runner.command import run_command
from firefly_weave.deployment_runner.policy import ComponentBinding, DestinationPolicy
from firefly_weave.deployment_runner.runtime import RunnerPolicyError, observed_time

TARGET_LABEL = "firefly.weave/target"
CONFIGURATION_ANNOTATION = "firefly.weave/configuration"
# Kubernetes label syntax, so a selector built from a deployment never carries selector operators of its own.
_LABEL_KEY = re.compile(r"(?:[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?/)?[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?")
_LABEL_VALUE = re.compile(r"(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?")
# The only pod fields read for members, so the listing stays small: about 250 bytes a pod. Free-text status
# fields stay out, so no value can carry a line break into the listing; evicted pods always have phase Failed.
_POD_FIELDS = (
    ".metadata.name",
    ".metadata.namespace",
    ".metadata.deletionTimestamp",
    ".metadata.labels.pod-template-hash",
    ".metadata.ownerReferences[?(@.controller==true)].apiVersion",
    ".metadata.ownerReferences[?(@.controller==true)].kind",
    ".metadata.ownerReferences[?(@.controller==true)].name",
    ".status.phase",
    '.status.conditions[?(@.type=="Ready")].status',
    '.status.containerStatuses[?(@.name=="{container}")].restartCount',
    '.status.containerStatuses[?(@.name=="{container}")].state.running.startedAt',
    ".spec.nodeName",
)


def _pod_fields(container: str) -> str:
    """A kubectl JSONPath printing one tab-separated line per pod; `container` is a policy name."""
    fields = '{"\\t"}'.join("{" + field.replace("{container}", container) + "}" for field in _POD_FIELDS)
    return "{range .items[*]}" + fields + '{"\\n"}{end}'


def _label(key: object, value: object = "") -> bool:
    return (
        isinstance(key, str)
        and isinstance(value, str)
        and _LABEL_KEY.fullmatch(key) is not None
        and _LABEL_VALUE.fullmatch(value) is not None
    )


def _selector(selector: object) -> str | None:
    """A deployment selector as a kubectl label selector, or None when kubectl cannot express it exactly.

    `matchLabels` become `key=value`; `matchExpressions` become set-based requirements (`key in (a,b)`,
    `key notin (a,b)`, `key`, `!key`). Anything else, including an empty selector, lists no pods.
    """
    if not isinstance(selector, dict) or not set(selector) <= {"matchLabels", "matchExpressions"}:
        return None
    labels, expressions = selector.get("matchLabels") or {}, selector.get("matchExpressions") or []
    if not isinstance(labels, dict) or not isinstance(expressions, list):
        return None
    if not all(_label(key, value) for key, value in labels.items()):
        return None
    requirements = [f"{key}={value}" for key, value in sorted(labels.items())]
    for expression in expressions:
        if not isinstance(expression, dict) or not set(expression) <= {"key", "operator", "values"}:
            return None
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values") or []
        if not _label(key) or not isinstance(values, list) or not all(_label(key, item) and item for item in values):
            return None
        if operator in {"In", "NotIn"} and values:
            requirements.append(f"{key} {operator.lower()} ({','.join(sorted(values))})")
        elif operator == "Exists" and not values:
            requirements.append(f"{key}")
        elif operator == "DoesNotExist" and not values:
            requirements.append(f"!{key}")
        else:
            return None
    return ",".join(requirements) or None


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

    async def _run(self, *args: str, stdin: bytes | None = None) -> bytes:
        return await self.command(
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

    async def _json(self, *args: str, stdin: bytes | None = None) -> dict[str, Any]:
        raw = await self._run(*args, stdin=stdin)
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

    async def _members(self, value: dict[str, Any], binding: ComponentBinding) -> list[ObservedMember] | None:
        """The deployment's live pods as members, or None when they cannot all be listed.

        Members are placement only, so listing them never fails an observation or an apply: a selector kubectl
        cannot express, a failed or unreadable listing, a malformed pod, or more than 100 live pods leaves them
        out (unknown) rather than reporting part of them.
        """
        selector = _selector(value.get("spec", {}).get("selector"))
        if selector is None or binding.container is None:
            return None
        try:
            raw = await self._run(
                "get", "pods", "--selector=" + selector, "-o=jsonpath=" + _pod_fields(binding.container)
            )
            text = raw.decode()
            if text and not text.endswith("\n"):
                raise ValueError("Incomplete pod listing")
            members = []
            for line in text.split("\n")[:-1]:
                member = self._member(line.split("\t"), value["metadata"]["name"])
                if member is not None:
                    members.append(member)
                if len(members) > 100:
                    return None
            return sorted(members, key=lambda member: member.host)
        except Exception:
            # Placement stays unknown; cancellation is not an Exception and still propagates.
            return None

    def _member(self, fields: list[str], deployment: str) -> ObservedMember | None:
        """A live pod of this deployment, None for any other pod, or an error for an unreadable one."""
        if len(fields) != len(_POD_FIELDS):
            raise ValueError("Unexpected pod fields")
        name, namespace, deleting, template, api, kind, owner, phase, ready, restarts, started, node = fields
        # The deployment controller names each ReplicaSet `<deployment>-<pod-template-hash>`; exactly one
        # controller owner is listed, so several controllers never match.
        if namespace != self.policy.boundary or (api, kind) != ("apps/v1", "ReplicaSet") or not template:
            return None
        if owner != f"{deployment}-{template}":
            return None
        # Terminating, finished, failed and evicted pods run no worker that placement could describe.
        if deleting or phase in {"Succeeded", "Failed"}:
            return None
        # A Ready status of Unknown (for example on a lost node) is not ready.
        if ready not in {"", "True", "False", "Unknown"} or not re.fullmatch(r"[0-9]{0,10}", restarts):
            raise ValueError("Unexpected pod status")
        return ObservedMember(
            host=name,
            ordinal=None,
            state=phase.lower() if phase else "unknown",
            ready=ready == "True",
            started_at=observed_time(started or None),
            restarts=int(restarts or "0"),
            node=node or None,
        )

    def _fact(
        self, value: dict[str, Any], binding: ComponentBinding, members: list[ObservedMember] | None
    ) -> ObservedResource:
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
            members=members or [],
        )

    async def observe(self) -> ObservationReport:
        await self._identity()
        resources = []
        for binding in self.policy.components:
            value = await self._deployment(binding)
            if value:
                # Placement only concerns workers; other components need no pod reads.
                members = await self._members(value, binding) if binding.kind == "worker" else None
                resources.append(self._fact(value, binding, members))
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
