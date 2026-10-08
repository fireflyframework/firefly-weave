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

"""Retained-state provenance, metadata-only handling of uncertain legacy runs, and runs this platform cannot run."""

import json
from typing import Any, Literal

from firefly_weave.compiler.api import import_artifact
from firefly_weave.compiler.ir import UnsupportedIR, accepted_ir_versions
from firefly_weave.contracts import language_features
from firefly_weave.definitions.models import CatalogError, ir_unsupported
from firefly_weave.operations.redaction import Omission, SafeProjection, has_markers

POLICY = "classified-v1"

type Admission = Literal["available", "unavailable", "unsupported"]


def admission(row: dict[str, Any]) -> Admission:
    """How this platform may treat a retained run.

    ``unsupported``: the run pins an IR version or language feature this platform does not run, for example after a
    rollback. It is transient: scanners skip the run without recording anything, and an upgrade runs it again.
    ``unavailable``: legacy or malformed evidence; scanners record a permanent policy block.
    """
    return _admit(row)[0]


def _admit(row: dict[str, Any]) -> tuple[Admission, tuple[str, ...]]:
    """``admission``, with the features this platform does not list for an ``unsupported`` run."""
    if row["state"].get("unavailable"):
        return "unavailable", ()
    try:
        artifact = import_artifact(row["artifact"])
        if row["state"].get("admission_policy") == POLICY:
            return "available", ()
        return ("unavailable" if has_markers(artifact.executable) else "available"), ()
    except UnsupportedIR as error:
        return "unsupported", error.missing
    except (ValueError, RecursionError, KeyError):
        return "unavailable", ()


def unavailable(row: dict[str, Any]) -> bool:
    """Whether reads and controls must withhold the run's state: unavailable or unsupported."""
    return admission(row) != "available"


def runnable(run: str) -> str:
    """SQL condition: the ``run`` row pins an IR version and language features this platform runs.

    It mirrors ``require_supported_ir``, so scanners never select a run they would only skip and the runs behind it
    are not held back. Malformed shapes stay selected and are left to admission. Bind ``runnable_parameters()``.

    It reads the stored artifact once per row: a subquery fenced with ``OFFSET 0``, which the planner cannot inline,
    extracts the executable, and every check reads that copy. Without the fence each reference would fetch and
    decompress the whole artifact again. Place it where it is evaluated only for rows that passed the cheaper
    conditions.
    """
    features = "pinned.executable->'features'"
    return (
        "NOT coalesce((SELECT jsonb_typeof(pinned.executable)='object' AND ("
        "NOT coalesce(jsonb_typeof(pinned.executable->'irVersion')='string' "
        "AND pinned.executable->>'irVersion'=ANY(cast(:ir_versions AS text[])),false) "
        f"OR (jsonb_typeof({features})='array' AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements("
        f"CASE WHEN jsonb_typeof({features})='array' THEN {features} ELSE '[]'::jsonb END) f "
        f"WHERE jsonb_typeof(f)<>'string') AND NOT {features} <@ cast(:ir_features AS jsonb))) "
        f"FROM (SELECT {run}.artifact->'executable' AS executable OFFSET 0) pinned),false)"
    )


def runnable_parameters() -> dict[str, Any]:
    """The IR versions and language features this platform runs, for ``runnable``."""
    features = language_features.ADVERTISED_FEATURES
    return {"ir_versions": accepted_ir_versions(features), "ir_features": json.dumps(list(features))}


def require_available(row: dict[str, Any]) -> None:
    """Refuse to show or change a run whose state this platform withholds.

    A run waiting for a platform that runs its IR answers ``ir_unsupported`` (422 ``WV-IR-UNSUPPORTED`` naming the
    missing features), as catalog reads of its version do; legacy or malformed evidence answers 409
    ``WV-LEGACY-UNAVAILABLE``.
    """
    decision, missing = _admit(row)
    if decision == "unsupported":
        raise ir_unsupported(missing)
    if decision == "unavailable":
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
    from firefly_weave.compiler.ir import ArtifactEnvelope, HumanTaskNode, SignalNode, WorkflowIR
    from firefly_weave.compiler.lowering import DEFAULT_ARTIFACT_LIMITS
    from firefly_weave.compiler.parser import ParseFailure, parse_source

    try:
        parsed = parse_source(row["artifact"], format="object", limits=DEFAULT_ARTIFACT_LIMITS.value_limits())
        executable = ArtifactEnvelope.model_validate(parsed.value).executable
        if not isinstance(executable, WorkflowIR):
            return None
        return {
            node.id: node.name if isinstance(node, SignalNode) else "@human:" + node.id
            for node in executable.graph.nodes
            if isinstance(node, (SignalNode, HumanTaskNode))
        }
    except (ValueError, RecursionError, ParseFailure):
        return None
