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

"""Pure compilation, partial authoring checks and verified artifact import."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from firefly_weave.compiler.action_config import ActionConfigValidator
from firefly_weave.compiler.analyzer import analyze, analyze_authoring, analyze_partial
from firefly_weave.compiler.canonical import canonical_bytes, canonical_digest
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument
from firefly_weave.compiler.expressions import ExpressionFailure, measure_value
from firefly_weave.compiler.ir import ArtifactEnvelope
from firefly_weave.compiler.lowering import DEFAULT_ARTIFACT_LIMITS, ArtifactLimits, ConstructionBudget, lower
from firefly_weave.compiler.parser import ParseFailure, parse_source
from firefly_weave.compiler.schema_profile import DEFAULT_CONTRACT_LIMITS, SchemaLimits
from firefly_weave.contracts.diagnostics import Diagnostic, SourceRange
from firefly_weave.contracts.limits import Limits
from firefly_weave.contracts.values import JsonObject, JsonValue

_DEFAULT_LIMITS = Limits()
_DEFAULT_SCHEMA_LIMITS = SchemaLimits()


@dataclass(frozen=True)
class CompiledArtifact:
    """Owned canonical storage; JSON/model accessors return independent copies."""

    _canonical: bytes = field(repr=False)
    _limits: ArtifactLimits = field(default=DEFAULT_ARTIFACT_LIMITS, repr=False)

    def __post_init__(self) -> None:
        if type(self._canonical) is not bytes or len(self._canonical) > self._limits.max_bytes:
            raise ValueError("Artifact storage budget exceeded")
        value = parse_source(self._canonical, format="json", limits=self._limits.value_limits()).value
        ArtifactEnvelope.model_validate(value)
        if canonical_bytes(value) != self._canonical:
            raise ValueError("Artifact storage must be canonical")

    @property
    def executable(self) -> JsonObject:
        return cast(JsonObject, json.loads(self._canonical)["executable"])

    @property
    def digest(self) -> str:
        return cast(str, json.loads(self._canonical)["digest"])

    @property
    def source_hash(self) -> str:
        return cast(str, json.loads(self._canonical)["sourceHash"])

    @property
    def source_map(self) -> dict[str, SourceRange]:
        return {
            key: SourceRange.model_validate(value) for key, value in json.loads(self._canonical)["sourceMap"].items()
        }

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(Diagnostic.model_validate(value) for value in json.loads(self._canonical)["diagnostics"])

    @property
    def omitted_count(self) -> int:
        return cast(int, json.loads(self._canonical)["omittedCount"])

    @property
    def schema_truncated(self) -> bool:
        return cast(bool, json.loads(self._canonical)["schemaTruncated"])

    @property
    def truncated(self) -> bool:
        return self.omitted_count > 0 or self.schema_truncated

    def to_bytes(self) -> bytes:
        return self._canonical


@dataclass(frozen=True)
class CompileResult:
    _diagnostics: tuple[FrozenDocument, ...] = field(repr=False)
    artifact: CompiledArtifact | None = None
    partial: bool = False
    error_count: int = 0
    omitted_count: int = 0
    schema_truncated: bool = False

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return tuple(Diagnostic.model_validate(d.value) for d in self._diagnostics)

    @property
    def validation_ok(self) -> bool:
        return self.error_count == 0

    @property
    def ok(self) -> bool:
        return self.validation_ok and not self.partial and self.artifact is not None

    @property
    def truncated(self) -> bool:
        return self.omitted_count > 0 or self.schema_truncated

    def to_bytes(self) -> bytes:
        return canonical_bytes(
            {
                "diagnostics": [d.value for d in self._diagnostics],
                "artifact": json.loads(self.artifact.to_bytes()) if self.artifact else None,
                "partial": self.partial,
                "ok": self.ok,
                "validationOk": self.validation_ok,
                "errorCount": self.error_count,
                "omittedCount": self.omitted_count,
                "schemaTruncated": self.schema_truncated,
                "truncated": self.truncated,
            }
        )


def _compile(
    source: str | bytes | JsonObject,
    *,
    format: Literal["yaml", "json", "object"],
    catalog: CatalogSnapshot | None,
    filename: str | None,
    strict: bool,
    limits: Limits,
    schema_limits: SchemaLimits,
    contract_limits: SchemaLimits,
    artifact_limits: ArtifactLimits,
    max_parallel_concurrency: int,
    action_validators: Mapping[str, ActionConfigValidator] | None = None,
    authoring: bool = False,
) -> CompileResult:
    partial = catalog is None
    try:
        parsed = parse_source(source, format=format, filename=filename, limits=limits)
    except ParseFailure as failure:
        return CompileResult(
            tuple(FrozenDocument.from_value(d.model_dump(by_alias=True)) for d in failure.diagnostics),
            partial=partial,
            error_count=max(1, len(failure.diagnostics) + failure.omitted_count),
            omitted_count=failure.omitted_count,
        )
    from firefly_weave.compiler.admission import admit_authoring
    from firefly_weave.compiler.schemas import _Failure, _issue

    try:
        admit_authoring(parsed.value, catalog or CatalogSnapshot.empty(), unresolved_literals=False)
    except _Failure as failure:
        return CompileResult(
            (FrozenDocument.from_value(_issue(failure.code).model_dump(by_alias=True)),), partial=partial, error_count=1
        )
    analysis = (
        (analyze_authoring if authoring else analyze_partial)(
            parsed, limits=limits, schema_limits=schema_limits, contract_limits=contract_limits
        )
        if catalog is None
        else analyze(
            parsed,
            catalog,
            strict=strict,
            limits=limits,
            schema_limits=schema_limits,
            contract_limits=contract_limits,
            max_parallel_concurrency=max_parallel_concurrency,
            action_validators=action_validators,
        )
    )
    diagnostics = tuple(FrozenDocument.from_value(d.model_dump(by_alias=True)) for d in analysis.diagnostics)
    if partial or not analysis.ok:
        return CompileResult(
            diagnostics,
            partial=partial,
            error_count=analysis.error_count,
            omitted_count=analysis.omitted_count,
            schema_truncated=analysis.schema_truncated,
        )
    try:
        executable = lower(analysis, limits=artifact_limits)
        budget = ConstructionBudget(artifact_limits)
        budget.charge(executable)
        source_map: JsonObject = {}
        for path, span in parsed.locations.items():
            item = span.model_dump(by_alias=True)
            budget.charge({path: item})
            source_map[path] = item
        envelope: JsonObject = {
            "executable": executable,
            "digest": canonical_digest(executable),
            "sourceHash": parsed.source_hash,
            "sourceMap": source_map,
            "diagnostics": cast(list[JsonValue], [d.value for d in diagnostics]),
            "omittedCount": analysis.omitted_count,
            "schemaTruncated": analysis.schema_truncated,
        }
        measure_value(envelope, limits=artifact_limits.value_limits())
        artifact = CompiledArtifact(canonical_bytes(envelope), artifact_limits)
    except (ValueError, ExpressionFailure, RecursionError):
        issue = Diagnostic(
            code="WV-COMP-ARTIFACT_LIMIT",
            severity="error",
            stage="lowering",
            path="",
            message="Executable construction exceeds its artifact contract or storage budget.",
        )
        issues = [*diagnostics, FrozenDocument.from_value(issue.model_dump(by_alias=True))]
        omitted = analysis.omitted_count
        if len(issues) > limits.max_diagnostics:
            omitted += len(issues) - limits.max_diagnostics
            issues = issues[: limits.max_diagnostics - 1] + [issues[-1]]
        return CompileResult(
            tuple(issues), error_count=1, omitted_count=omitted, schema_truncated=analysis.schema_truncated
        )
    return CompileResult(
        diagnostics, artifact=artifact, omitted_count=analysis.omitted_count, schema_truncated=analysis.schema_truncated
    )


def compile_source(
    source: str | bytes | JsonObject,
    *,
    format: Literal["yaml", "json", "object"],
    catalog: CatalogSnapshot,
    filename: str | None = None,
    strict: bool = False,
    limits: Limits = _DEFAULT_LIMITS,
    schema_limits: SchemaLimits = _DEFAULT_SCHEMA_LIMITS,
    contract_limits: SchemaLimits = DEFAULT_CONTRACT_LIMITS,
    artifact_limits: ArtifactLimits = DEFAULT_ARTIFACT_LIMITS,
    max_parallel_concurrency: int = 1000,
    action_validators: Mapping[str, ActionConfigValidator] | None = None,
) -> CompileResult:
    """Complete definition validity; never claims live deployment/activation readiness.

    ``action_validators`` maps an installed Connector manifest digest to its trusted descriptor
    check for Actions that use exactly that Connector. Callers supply them explicitly.
    """
    if not isinstance(catalog, CatalogSnapshot):
        raise TypeError("Complete compilation requires an explicit CatalogSnapshot")
    return _compile(
        source,
        format=format,
        catalog=catalog,
        filename=filename,
        strict=strict,
        limits=limits,
        schema_limits=schema_limits,
        contract_limits=contract_limits,
        artifact_limits=artifact_limits,
        max_parallel_concurrency=max_parallel_concurrency,
        action_validators=action_validators,
    )


def validate_source(
    source: str | bytes | JsonObject,
    *,
    format: Literal["yaml", "json", "object"],
    filename: str | None = None,
    limits: Limits = _DEFAULT_LIMITS,
    schema_limits: SchemaLimits = _DEFAULT_SCHEMA_LIMITS,
    contract_limits: SchemaLimits = DEFAULT_CONTRACT_LIMITS,
) -> CompileResult:
    """No-catalog partial authoring validation. Always artifact=None, partial=True, ok=False."""
    return _compile(
        source,
        format=format,
        catalog=None,
        filename=filename,
        strict=False,
        limits=limits,
        schema_limits=schema_limits,
        contract_limits=contract_limits,
        artifact_limits=DEFAULT_ARTIFACT_LIMITS,
        max_parallel_concurrency=1000,
    )


def validate_authoring(
    source: str | bytes | JsonObject,
    *,
    format: Literal["yaml", "json", "object"],
    filename: str | None = None,
    limits: Limits = _DEFAULT_LIMITS,
    schema_limits: SchemaLimits = _DEFAULT_SCHEMA_LIMITS,
    contract_limits: SchemaLimits = DEFAULT_CONTRACT_LIMITS,
) -> CompileResult:
    """No-catalog authoring validation with flow, dominance and type analysis, for editors.

    Runs every ``validate_source`` check, then analyzes the workflow against an absent catalog:
    each dependency reference yields ``WV-COMP-CATALOG_PENDING`` (info) instead of an
    unknown-resource error. Always artifact=None, partial=True, ok=False; ``validation_ok``
    reflects error-severity findings only. ``validate_source`` and its callers are unchanged.
    """
    return _compile(
        source,
        format=format,
        catalog=None,
        filename=filename,
        strict=False,
        limits=limits,
        schema_limits=schema_limits,
        contract_limits=contract_limits,
        artifact_limits=DEFAULT_ARTIFACT_LIMITS,
        max_parallel_concurrency=1000,
        authoring=True,
    )


def import_artifact(
    source: str | bytes | JsonObject, *, limits: ArtifactLimits = DEFAULT_ARTIFACT_LIMITS
) -> CompiledArtifact:
    """Recheck supported IR, strict contracts, graph invariants and content hashes.

    Hashes provide integrity, not provenance. Publication must recompile trusted source.
    Source hashes/maps/diagnostics are an untrusted author envelope, outside executable identity.
    """
    value = parse_source(
        source, format="object" if isinstance(source, dict) else "json", limits=limits.value_limits()
    ).value
    ArtifactEnvelope.model_validate(value)
    from firefly_weave.compiler.admission import admit_artifact
    from firefly_weave.compiler.schemas import _Failure

    try:
        admit_artifact(cast(JsonObject, value["executable"]))
    except _Failure:
        raise ValueError("Artifact contains classified or unclassifiable values") from None
    return CompiledArtifact(canonical_bytes(value), limits)
