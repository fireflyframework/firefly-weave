# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Provider-independent mail contracts; routing headers are never caller inputs."""

import base64
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from firefly_weave.contracts.definitions import ContractModel

ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")


def address(value: str) -> str:
    if len(value) > 254 or not ADDRESS.fullmatch(value) or ".." in value:
        raise ValueError("Invalid mailbox address")
    return value


class EmailAttachment(ContractModel):
    filename: str = Field(min_length=1, max_length=200)
    content_type: str = Field(pattern=r"^[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+$")
    content_base64: str = Field(max_length=1400000)

    @field_validator("filename")
    @classmethod
    def filename_safe(cls, value: str) -> str:
        if any(ord(c) < 32 for c in value) or "/" in value or "\\" in value:
            raise ValueError("Invalid filename")
        return value

    @field_validator("content_base64")
    @classmethod
    def valid_data(cls, value: str) -> str:
        if len(base64.b64decode(value, validate=True)) > 1048576:
            raise ValueError("Attachment too large")
        return value


class EmailSendRequest(ContractModel):
    request_id: UUID
    connection_revision_id: UUID
    to: tuple[str, ...] = Field(min_length=1, max_length=50)
    cc: tuple[str, ...] = Field(default=(), max_length=50)
    bcc: tuple[str, ...] = Field(default=(), max_length=50)
    subject: str = Field(max_length=998)
    text: str = Field(max_length=1048576)
    html: str | None = Field(default=None, max_length=1048576)
    attachments: tuple[EmailAttachment, ...] = Field(default=(), max_length=10)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def addresses(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(address(v) for v in values)

    @field_validator("subject")
    @classmethod
    def header_safe(cls, value: str) -> str:
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid subject")
        return value

    @model_validator(mode="after")
    def limits(self) -> "EmailSendRequest":
        if len(self.to + self.cc + self.bcc) > 50:
            raise ValueError("Too many recipients")
        return self


class EmailReplyRequest(ContractModel):
    request_id: UUID
    connection_revision_id: UUID
    parent_message_id: UUID
    reply_all: bool = False
    text: str = Field(max_length=1048576)
    html: str | None = Field(default=None, max_length=1048576)
    attachments: tuple[EmailAttachment, ...] = Field(default=(), max_length=10)


class AttachmentMetadata(ContractModel):
    filename: str | None
    content_type: str
    size: int


class NormalizedEmail(ContractModel):
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    reply_to: str | None = None
    subject: str
    text: str
    message_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    attachments: tuple[AttachmentMetadata, ...] = ()
    automated: bool = False


class EmailSubmission(ContractModel):
    id: UUID
    conversation_id: UUID
    message_id: UUID
    state: Literal["queued", "attempting", "accepted", "rejected", "unknown"]
    accepted_recipients: tuple[str, ...] = ()
    rejected_recipients: tuple[str, ...] = ()


class EmailMessage(ContractModel):
    id: UUID
    conversation_id: UUID | None
    direction: Literal["inbound", "outbound"]
    state: str
    accepted_at: datetime
    mail: NormalizedEmail


class EmailConversation(ContractModel):
    id: UUID
    connection_revision_id: UUID
    subject: str
    created_at: datetime
    run_id: UUID | None = None


class MailProfile(ContractModel):
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.-]+$")
    port: int = Field(ge=1, le=65535)
    tls: Literal["tls", "starttls", "local_fixture"] = "tls"
    auth: Literal["none", "password", "oauth"] = "none"
    sender: str
    allowed_recipients: tuple[str, ...]
    private_networks: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=30, gt=0, le=60)
    automated_response_limit: int = Field(default=3, ge=0, le=20)
    correlation_retention_seconds: int = Field(default=86400, ge=60, le=604800)
    routing_domain: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.-]+$")
    folder: str = Field(default="INBOX", pattern=r"^[A-Za-z0-9_. /-]{1,100}$")

    @field_validator("sender")
    @classmethod
    def sender_valid(cls, value: str) -> str:
        return address(value)

    @field_validator("allowed_recipients")
    @classmethod
    def recipient_valid(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(address(v) for v in values)


class EmailConversationDetail(ContractModel):
    conversation: EmailConversation
    messages: tuple[EmailMessage, ...]
    next_cursor: str | None = None


class EmailSourceRequest(ContractModel):
    connection_revision_id: UUID
    activation_id: UUID | None = None


class EmailCorrelationRequest(ContractModel):
    conversation_id: UUID
    run_id: UUID
    signal: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")


class EmailSourceResult(ContractModel):
    id: UUID


class EmailSourceStatus(ContractModel):
    state: str
    uid: int | None = None
    admitted: int | None = None
    run_id: str | None = None
    reason: str | None = None


class EmailTokenRequest(ContractModel):
    source_id: UUID
    conversation_id: UUID
    run_id: UUID
    signal: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    validity_seconds: int = Field(default=86400, ge=1, le=604800)


class EmailCorrelationToken(ContractModel):
    id: UUID
    reply_address: str
    expires_at: datetime


class EmailReceipt(ContractModel):
    id: UUID
    source_id: UUID
    transport_key: str
    message_id: UUID | None
    state: str
    reason: str | None = None
    run_id: UUID | None = None
    signal_id: UUID | None = None
    message: EmailMessage | None = None
