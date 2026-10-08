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

"""Studio documents: the test-data sidecar and the platform envelope that stores it.

The sidecar keeps camelCase, like every Studio document; the envelope is a platform API body and uses snake_case. The
JSON Schema ``schemas/studio-test-data-v1.json`` is normative, and these models accept exactly the documents it
accepts (a contract test keeps the two in step). Entry keys are instance keys typed ``InstanceKeyText`` from
``firefly_weave.contracts.instance_keys``, whose schema the JSON Schema repeats as ``$defs.key``.
"""

import json
from pathlib import Path
from typing import Annotated, Final, Literal, Self, cast
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.instance_keys import InstanceKeyText
from firefly_weave.contracts.values import MAX_SAFE_INTEGER, JsonData, JsonObject, JsonObjectData, UnicodeString

TEST_DATA_SCHEMA_ID: Final = "urn:firefly-weave:schema:studio-test-data:v1"
TEST_DATA_MAX_BYTES: Final = 1_048_576
CANVAS_MAX_BYTES: Final = 262_144
STUDIO_DOCUMENT_MAX_BYTES: Final[dict[str, int]] = {"canvas": CANVAS_MAX_BYTES, "test-data": TEST_DATA_MAX_BYTES}
FRAME_PATTERN: Final = (
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*(\[(0|[1-9][0-9]*)\])*(/[A-Za-z0-9][A-Za-z0-9_.-]*(\[(0|[1-9][0-9]*)\])*)*$"
)
REFERENCE_PATTERN: Final = (
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
)
HASH_PATTERN: Final = r"^fnv1a64:[0-9a-f]{16}$"

type FrameKey = Annotated[UnicodeString, Field(max_length=1024, pattern=FRAME_PATTERN)]
type CalleeReference = Annotated[UnicodeString, Field(max_length=300, pattern=REFERENCE_PATTERN)]
type Fingerprint = Annotated[UnicodeString, Field(pattern=HASH_PATTERN)]
type DataSource = Literal["manual", "schema", "simulated", "test-call", "run"]
type AiTurnsSource = Literal["manual", "simulated", "test-call", "run"]
type StudioDocumentKind = Literal["canvas", "test-data"]


def _absent(value: object) -> bool:
    return value is None


class _Sidecar(ContractModel):
    """Studio documents are camelCase on the wire; Python code may also use the field names."""

    model_config = ConfigDict(populate_by_name=True)


class _Provenanced(_Sidecar):
    run_id: UUID | None = Field(default=None, alias="runId", exclude_if=_absent)
    run_environment_id: UUID | None = Field(default=None, alias="runEnvironmentId", exclude_if=_absent)

    @model_validator(mode="after")
    def provenance_rule(self) -> Self:
        source = getattr(self, "source", None)
        if source in ("run", "test-call") and (self.run_id is None or self.run_environment_id is None):
            raise ValueError("Data from a run or a test call names runId and runEnvironmentId")
        if self.run_id is not None and (source is None or self.run_environment_id is None):
            raise ValueError("runId needs source and runEnvironmentId")
        return self


class InputEntry(_Provenanced):
    value: JsonData
    source: DataSource


class PinEntry(_Provenanced):
    output: JsonData
    source: DataSource
    pinned_at: AwareDatetime = Field(alias="pinnedAt")
    config_hash: Fingerprint = Field(alias="configHash")
    contract_hash: Fingerprint = Field(alias="contractHash")
    upstream_hash: Fingerprint = Field(alias="upstreamHash")


class MockEntry(_Provenanced):
    output: JsonData
    source: DataSource


class SignalScript(_Provenanced):
    payload: JsonData
    after_seconds: int | None = Field(default=None, ge=0, le=MAX_SAFE_INTEGER, alias="afterSeconds", exclude_if=_absent)
    frame: FrameKey | None = Field(default=None, exclude_if=_absent)
    source: DataSource | None = Field(default=None, exclude_if=_absent)


class SignalTimeout(_Sidecar):
    outcome: Literal["timeout"]
    frame: FrameKey | None = Field(default=None, exclude_if=_absent)


class HumanScript(_Provenanced):
    decision: UnicodeString
    data: JsonData
    frame: FrameKey | None = Field(default=None, exclude_if=_absent)
    source: DataSource | None = Field(default=None, exclude_if=_absent)


class HumanExpire(_Sidecar):
    outcome: Literal["expire"]
    frame: FrameKey | None = Field(default=None, exclude_if=_absent)


class AiMemoryMessage(_Sidecar):
    role: Literal["user", "assistant"]
    content: JsonData


class AiTurnsEntry(_Provenanced):
    turns: Annotated[list[JsonObjectData], Field(max_length=100)]
    memory: Annotated[list[AiMemoryMessage], Field(max_length=200)] | None = Field(default=None, exclude_if=_absent)
    auto_approve: bool | None = Field(default=None, alias="autoApprove", exclude_if=_absent)
    source: AiTurnsSource | None = Field(default=None, exclude_if=_absent)
    config_hash: Fingerprint | None = Field(default=None, alias="configHash", exclude_if=_absent)
    upstream_hash: Fingerprint | None = Field(default=None, alias="upstreamHash", exclude_if=_absent)


class CalleeMocks(_Sidecar):
    mocks: Annotated[dict[InstanceKeyText, MockEntry], Field(max_length=1000)]


class WaitScripts(_Sidecar):
    auto_advance: bool | None = Field(default=None, alias="autoAdvance", exclude_if=_absent)


type SignalEntry = SignalScript | SignalTimeout
type HumanEntry = HumanScript | HumanExpire


class StudioTestData(_Sidecar):
    """One workflow's test data: input, pins, scripts, AI turns and called-workflow mocks."""

    schema_version: Literal[1] = Field(alias="schemaVersion")
    kind: Literal["weave.studio/testData"]
    updated_at: AwareDatetime = Field(alias="updatedAt")
    input: InputEntry | None = Field(default=None, exclude_if=_absent)
    pins: Annotated[dict[InstanceKeyText, PinEntry], Field(max_length=1000)] | None = Field(
        default=None, exclude_if=_absent
    )
    signals: Annotated[dict[InstanceKeyText, SignalEntry], Field(max_length=1000)] | None = Field(
        default=None, exclude_if=_absent
    )
    humans: Annotated[dict[InstanceKeyText, HumanEntry], Field(max_length=1000)] | None = Field(
        default=None, exclude_if=_absent
    )
    ai_turns: Annotated[dict[InstanceKeyText, AiTurnsEntry], Field(max_length=1000)] | None = Field(
        default=None, alias="aiTurns", exclude_if=_absent
    )
    workflows: Annotated[dict[CalleeReference, CalleeMocks], Field(max_length=50)] | None = Field(
        default=None, exclude_if=_absent
    )
    waits: WaitScripts | None = Field(default=None, exclude_if=_absent)


class StudioDocument(ContractModel):
    """A canvas or test-data document attached to a platform draft; the server derives the run-data fields."""

    draft_id: UUID
    kind: StudioDocumentKind
    revision: int = Field(ge=1)
    draft_revision: int = Field(ge=1)
    document: JsonObjectData
    contains_run_data: bool
    source_environment_ids: list[UUID]
    updated_at: AwareDatetime
    updated_by: UUID


class StudioDocumentSave(ContractModel):
    """The body of a save; run-data fields are not accepted, because only the server derives them."""

    draft_revision: int = Field(ge=1)
    document: JsonObjectData


class StudioDocumentTooLarge(ValueError):
    code = "WV-STUDIO-DOCUMENT-SIZE"


def studio_test_data_schema() -> JsonObject:
    """The normative JSON Schema of a test-data document (a fresh copy on every call)."""
    text = (Path(__file__).parent / "schemas" / "studio-test-data-v1.json").read_text("utf-8")
    return cast(JsonObject, json.loads(text))


def parse_test_data(raw: bytes) -> StudioTestData:
    """A test-data document from its JSON bytes; larger than 1 MiB raises StudioDocumentTooLarge."""
    if len(raw) > TEST_DATA_MAX_BYTES:
        raise StudioDocumentTooLarge(StudioDocumentTooLarge.code)
    return StudioTestData.model_validate_json(raw)
