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

"""Retained-state provenance and metadata-only handling of uncertain legacy runs."""

from typing import Any

from firefly_weave.compiler.api import import_artifact
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.redaction import Omission, SafeProjection, has_markers

POLICY = "classified-v1"


def unavailable(row: dict[str, Any]) -> bool:
    if row["state"].get("unavailable"):
        return True
    try:
        artifact = import_artifact(row["artifact"])
        if row["state"].get("admission_policy") == POLICY:
            return False
        return has_markers(artifact.executable)
    except (ValueError, RecursionError, KeyError):
        return True


def require_available(row: dict[str, Any]) -> None:
    if unavailable(row):
        projection = SafeProjection(
            available=False,
            value={"id": str(row["id"]), "status": row["state"]["status"]},
            omissions=[Omission(path="/state", reason="uncertain_derived")],
        )
        raise CatalogError(
            409,
            "WV-LEGACY-UNAVAILABLE",
            "Legacy execution evidence is unavailable",
            result=projection.model_dump(mode="json"),
        )


def terminal_signals(row: dict[str, Any]) -> dict[str, str] | None:
    """Structurally verified pinned control metadata; no secret-policy/execution bypass."""
    from firefly_weave.compiler.ir import ArtifactEnvelope, SignalNode, WorkflowIR
    from firefly_weave.compiler.lowering import DEFAULT_ARTIFACT_LIMITS
    from firefly_weave.compiler.parser import ParseFailure, parse_source

    try:
        parsed = parse_source(row["artifact"], format="object", limits=DEFAULT_ARTIFACT_LIMITS.value_limits())
        executable = ArtifactEnvelope.model_validate(parsed.value).executable
        if not isinstance(executable, WorkflowIR):
            return None
        return {node.id: node.name for node in executable.graph.nodes if isinstance(node, SignalNode)}
    except (ValueError, RecursionError, ParseFailure):
        return None
