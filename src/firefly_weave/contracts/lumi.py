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

"""Ephemeral Studio assistance: fixed replies, explicit resource attachments."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_weave.contracts.llm import LLMProfile
from firefly_weave.contracts.values import JsonObjectData


class LumiProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["workflow", "decisionTable", "action", "connector"]
    format: Literal["yaml", "json"]
    source: str = Field(min_length=1, max_length=100000)


class LumiReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)
    answer: str = Field(min_length=1, max_length=20000)
    proposals: list[LumiProposal] = Field(default_factory=list, max_length=5)
    follow_ups: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=5, alias="followUps"
    )

    @model_validator(mode="after")
    def bounded_followups(self) -> Self:
        if any(not item or len(item) > 1000 for item in self.follow_ups):
            raise ValueError("Follow-up questions must contain 1 to 1000 characters")
        return self


class LumiCheckedProposal(LumiProposal):
    # Error-severity findings when the platform checked the source; 0 when it is a valid document of its kind.
    diagnostics: int = Field(ge=0)


class LumiAnswer(BaseModel):
    """What asking Weave AI returns: its reply, with every proposal checked by the platform."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)
    answer: str = Field(min_length=1, max_length=20000)
    proposals: list[LumiCheckedProposal] = Field(default_factory=list, max_length=5)
    follow_ups: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=5, alias="followUps"
    )


def reply_schema() -> JsonObjectData:
    schema = LumiReply.model_json_schema(by_alias=True)
    # A schema carried as const data must contain no refs for Pydantic's schema exporter.
    proposal = schema.pop("$defs")["LumiProposal"]
    schema["properties"]["proposals"]["items"] = proposal
    return schema


LUMI_REPLY_SCHEMA = reply_schema()


class LumiProfile(LLMProfile):
    output_schema: JsonObjectData = Field(alias="outputSchema", json_schema_extra={"const": LUMI_REPLY_SCHEMA})

    @model_validator(mode="after")
    def fixed_reply(self) -> Self:
        if self.output_schema != LUMI_REPLY_SCHEMA:
            raise ValueError("Weave AI requires its fixed proposal reply schema")
        return self


class LumiConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = True
    connection_revision_id: UUID
    profile: LumiProfile
    # How much authoring context Weave AI sends: chosen from the model's context size, or set here.
    context_profile: Literal["auto", "compact", "standard", "full"] = Field(
        default="auto", exclude_if=lambda value: value == "auto"
    )


class LumiConfiguration(LumiConfigurationRequest):
    revision: int = Field(ge=1)


class LumiStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    configured: bool
    provider: str | None = None
    model: str | None = None
    revision: int | None = None


class LumiMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class LumiAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    kind: Literal[
        "draft",
        "run",
        "simulation",
        "deployment-target",
        "deployment",
        "deployment-observation",
        "deployment-plan",
        "deployment-job",
    ]
    id: UUID


class LumiDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    format: Literal["yaml", "json"]
    source: str = Field(min_length=1, max_length=262144)

    @model_validator(mode="after")
    def bounded_source(self) -> Self:
        if len(self.source.encode("utf-8")) > 262144:
            raise ValueError("Inline source exceeds its UTF-8 byte limit")
        return self


class LumiAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    message: str = Field(min_length=1, max_length=20000)
    draft: LumiDraft | None = Field(default=None, exclude_if=lambda value: value is None)
    history: list[LumiMessage] = Field(default_factory=list, max_length=16)
    attachments: list[LumiAttachment] = Field(default_factory=list, max_length=4)
    # The step selected in the editor; a compact request sends it whole and outlines the rest of the draft.
    selected_step: str | None = Field(
        default=None, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", exclude_if=lambda value: value is None
    )
    # Ask Weave AI to fix a proposal: the draft is that proposal, sent whole on every profile when it fits.
    fix: bool = Field(default=False, exclude_if=lambda value: not value)

    @property
    def explanation_only(self) -> bool:
        return any(item.kind.startswith("deployment") for item in self.attachments)

    @model_validator(mode="after")
    def fix_names_its_proposal(self) -> Self:
        if self.fix and self.draft is None:
            raise ValueError("A fix request carries the proposal to fix as its draft")
        return self

    @model_validator(mode="after")
    def separate_operations_context(self) -> Self:
        if self.explanation_only and (
            self.draft is not None or any(not item.kind.startswith("deployment") for item in self.attachments)
        ):
            raise ValueError("Operations explanations accept only Operations attachments")
        return self
