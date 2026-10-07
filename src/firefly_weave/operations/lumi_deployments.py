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

"""Allowlisted deployment snapshots for read-only assistant explanations."""

from typing import Any, cast

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonValue

COLLECTIONS = {
    "deployment-target": "targets",
    "deployment": "deployments",
    "deployment-observation": "observations",
    "deployment-plan": "plans",
    "deployment-job": "jobs",
}
FIELDS = {
    "deployment-target": ("name", "adapter", "revision", "disabled", "capabilities"),
    "deployment": ("name", "revision", "ownership", "created_at"),
    "deployment-observation": ("target_revision", "observed_at", "expires_at", "complete", "settled"),
    "deployment-plan": (
        "adapter",
        "target_revision",
        "deployment_revision",
        "intent",
        "risks",
        "created_at",
        "expires_at",
    ),
    "deployment-job": (
        "kind",
        "state",
        "revision",
        "target_revision",
        "created_at",
        "deadline",
        "reconciliation_started_at",
    ),
}


def selected(data: dict[str, Any], fields: tuple[str, ...]) -> dict[str, JsonValue]:
    return {key: cast(JsonValue, data[key]) for key in fields if key in data}


def component(data: dict[str, Any]) -> dict[str, JsonValue]:
    result = selected(
        data, ("name", "kind", "replicas", "cpu_millis", "memory_mib", "ready_replicas", "ownership", "state")
    )
    if data.get("image"):
        result["image_digest"] = str(data["image"]).rsplit("@", 1)[-1]
    return result


def deployment_context(kind: str, record: ContractModel) -> dict[str, JsonValue]:
    data = record.model_dump(mode="json")
    result = selected(data, FIELDS[kind])
    if kind == "deployment":
        result["components"] = [component(item) for item in data["components"]]
    elif kind == "deployment-observation":
        result["resources"] = [component(item) for item in data["resources"]]
    elif kind == "deployment-plan":
        result["steps"] = [
            {"action": item["action"], "component": component(item["component"])} for item in data["steps"]
        ]
    elif kind == "deployment-job" and data.get("receipt"):
        receipt = data["receipt"]
        result["receipt"] = selected(receipt, ("code", "external_effects_may_continue")) | {
            "changed_resource_count": len(receipt["changed_resources"])
        }
    return result
