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
accepts (a contract test keeps the two in step): optional fields are omitted, never null, and ``parse_test_data``
reads field aliases only. Entry keys are instance keys typed ``InstanceKeyText`` from
``firefly_weave.contracts.instance_keys``, whose schema the JSON Schema repeats as ``$defs.key``.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal, Self, cast
from uuid import UUID

from pydantic import AwareDatetime, BeforeValidator, ConfigDict, Field, ValidationInfo, model_validator

from firefly_weave.contracts.definitions import ContractModel, OmissionOnly, _is_absent, _omit_absent_default
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


_UUID_TEXT = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_DATE_TIME_TEXT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
)


def _hyphenated(value: object) -> object:
    """Pydantic also reads UUIDs without hyphens, in braces or as URNs; the schema's ``uuid`` format does not."""
    if isinstance(value, str) and _UUID_TEXT.fullmatch(value) is None:
        raise ValueError("A UUID is written as 8-4-4-4-12 hexadecimal digits with hyphens")
    return value


def _rfc3339(value: object) -> object:
    """Pydantic also reads a space for the T, offsets without a colon and times without seconds; the schema does not.

    Text must be an RFC 3339 date-time; a ``datetime`` (Python code building a model) passes; nothing else does, because
    the lax datetime below would also read numbers and dates.
    """
    if not (isinstance(value, datetime) or (isinstance(value, str) and _DATE_TIME_TEXT.fullmatch(value))):
        raise ValueError("A timestamp is an RFC 3339 date-time, such as 2026-10-07T10:00:00Z")
    return value


def _not_boolean(value: object) -> object:
    """``Literal[1]`` also matches ``true``, because ``True == 1``; the schema's ``const`` does not."""
    if isinstance(value, bool):
        raise ValueError("A boolean is not a number")
    return value


type HyphenatedUUID = Annotated[UUID, BeforeValidator(_hyphenated)]
# A validator ahead of the datetime hands it a Python str, which strict mode refuses; so it is lax and _rfc3339 gates.
type Rfc3339DateTime = Annotated[AwareDatetime, Field(strict=False), BeforeValidator(_rfc3339)]
type SchemaVersion = Annotated[Literal[1], BeforeValidator(_not_boolean)]


class _Sidecar(ContractModel):
    """Studio documents are camelCase on the wire; Python code may also use the field names.

    Only code that builds a model may use the field names. ``parse_test_data`` reads aliases alone, because the schema
    knows no other names.
    """

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def aliases_only_in_json(cls, data: object, info: ValidationInfo) -> object:
        """Pydantic's JSON reader lets a field name sit beside its alias, which the schema does not allow."""
        if info.mode == "json" and isinstance(data, dict):
            for name, field in cls.model_fields.items():
                if field.alias is not None and field.alias != name and name in data:
                    raise ValueError(f"Write {field.alias}, not {name}")
        return data


class _Provenanced(_Sidecar):
    run_id: OmissionOnly[HyphenatedUUID] = Field(
        default=None, alias="runId", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    run_environment_id: OmissionOnly[HyphenatedUUID] = Field(
        default=None, alias="runEnvironmentId", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )

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
    pinned_at: Rfc3339DateTime = Field(alias="pinnedAt")
    config_hash: Fingerprint = Field(alias="configHash")
    contract_hash: Fingerprint = Field(alias="contractHash")
    upstream_hash: Fingerprint = Field(alias="upstreamHash")


class MockEntry(_Provenanced):
    output: JsonData
    source: DataSource


class SignalScript(_Provenanced):
    payload: JsonData
    after_seconds: OmissionOnly[Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]] = Field(
        default=None, alias="afterSeconds", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    frame: OmissionOnly[FrameKey] = Field(default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default)
    source: OmissionOnly[DataSource] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


class SignalTimeout(_Sidecar):
    outcome: Literal["timeout"]
    frame: OmissionOnly[FrameKey] = Field(default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default)


class HumanScript(_Provenanced):
    decision: UnicodeString
    data: JsonData
    frame: OmissionOnly[FrameKey] = Field(default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default)
    source: OmissionOnly[DataSource] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


class HumanExpire(_Sidecar):
    outcome: Literal["expire"]
    frame: OmissionOnly[FrameKey] = Field(default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default)


class AiMemoryMessage(_Sidecar):
    role: Literal["user", "assistant"]
    content: JsonData


class AiTurnsEntry(_Provenanced):
    turns: Annotated[list[JsonObjectData], Field(max_length=100)]
    memory: OmissionOnly[Annotated[list[AiMemoryMessage], Field(max_length=200)]] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    auto_approve: OmissionOnly[bool] = Field(
        default=None, alias="autoApprove", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    source: OmissionOnly[AiTurnsSource] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    config_hash: OmissionOnly[Fingerprint] = Field(
        default=None, alias="configHash", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    upstream_hash: OmissionOnly[Fingerprint] = Field(
        default=None, alias="upstreamHash", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


class CalleeMocks(_Sidecar):
    mocks: Annotated[dict[InstanceKeyText, MockEntry], Field(max_length=1000)]


class WaitScripts(_Sidecar):
    auto_advance: OmissionOnly[bool] = Field(
        default=None, alias="autoAdvance", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


type SignalEntry = SignalScript | SignalTimeout
type HumanEntry = HumanScript | HumanExpire


class StudioTestData(_Sidecar):
    """One workflow's test data: input, pins, scripts, AI turns and called-workflow mocks."""

    schema_version: SchemaVersion = Field(alias="schemaVersion")
    kind: Literal["weave.studio/testData"]
    updated_at: Rfc3339DateTime = Field(alias="updatedAt")
    input: OmissionOnly[InputEntry] = Field(default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default)
    pins: OmissionOnly[Annotated[dict[InstanceKeyText, PinEntry], Field(max_length=1000)]] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    signals: OmissionOnly[Annotated[dict[InstanceKeyText, SignalEntry], Field(max_length=1000)]] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    humans: OmissionOnly[Annotated[dict[InstanceKeyText, HumanEntry], Field(max_length=1000)]] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    ai_turns: OmissionOnly[Annotated[dict[InstanceKeyText, AiTurnsEntry], Field(max_length=1000)]] = Field(
        default=None, alias="aiTurns", exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    workflows: OmissionOnly[Annotated[dict[CalleeReference, CalleeMocks], Field(max_length=50)]] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )
    waits: OmissionOnly[WaitScripts] = Field(
        default=None, exclude_if=_is_absent, json_schema_extra=_omit_absent_default
    )


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
    """A test-data document from its JSON bytes, by alias only; larger than 1 MiB raises StudioDocumentTooLarge."""
    if len(raw) > TEST_DATA_MAX_BYTES:
        raise StudioDocumentTooLarge(StudioDocumentTooLarge.code)
    return StudioTestData.model_validate_json(raw, by_alias=True, by_name=False)
