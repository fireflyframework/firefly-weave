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
"""Typed facade over the canonical SDK registry; SMTP secrets stay server-side."""

from typing import Any, cast
from uuid import UUID

from firefly_weave.contracts.email import (
    EmailCorrelationRequest,
    EmailCorrelationToken,
    EmailReplyRequest,
    EmailSendRequest,
    EmailSourceRequest,
    EmailSourceResult,
    EmailSourceStatus,
    EmailSubmission,
    EmailTokenRequest,
)
from firefly_weave.sdk.client import WeaveClient


class EmailClient:
    def __init__(self, client: WeaveClient) -> None:
        self.client = client

    async def conversations(self, *, limit: int = 50) -> Any:
        return await self.client.invoke("email_conversations.list", query={"limit": limit})

    async def conversation(self, identifier: UUID) -> Any:
        return await self.client.invoke("email_conversations.read", identifier=identifier)

    async def send(self, request: EmailSendRequest) -> EmailSubmission:
        return cast(EmailSubmission, await self.client.invoke("email_submissions.send", body=request))

    async def reply(self, conversation_id: UUID, request: EmailReplyRequest) -> EmailSubmission:
        return cast(
            EmailSubmission,
            await self.client.invoke("email_submissions.reply", identifier=conversation_id, body=request),
        )

    async def status(self, identifier: UUID) -> EmailSubmission:
        return cast(EmailSubmission, await self.client.invoke("email_submissions.read", identifier=identifier))

    async def execute(self, identifier: UUID) -> EmailSubmission:
        return cast(EmailSubmission, await self.client.invoke("email_submissions.execute", identifier=identifier))

    async def create_source(self, request: "EmailSourceRequest") -> "EmailSourceResult":
        return cast("EmailSourceResult", await self.client.invoke("email_sources.create", body=request))

    async def poll(self, identifier: UUID) -> "EmailSourceStatus":
        return cast("EmailSourceStatus", await self.client.invoke("email_sources.poll", identifier=identifier))

    async def rebaseline(self, identifier: UUID) -> "EmailSourceStatus":
        return cast("EmailSourceStatus", await self.client.invoke("email_sources.rebaseline", identifier=identifier))

    async def correlate(self, receipt_id: UUID, request: "EmailCorrelationRequest") -> "EmailSourceStatus":
        return cast(
            "EmailSourceStatus",
            await self.client.invoke("email_receipts.correlate", identifier=receipt_id, body=request),
        )

    async def dispatch(self, receipt_id: UUID) -> "EmailSourceStatus":
        return cast("EmailSourceStatus", await self.client.invoke("email_receipts.dispatch", identifier=receipt_id))

    async def inbox(self, *, limit: int = 50, cursor: str | None = None) -> Any:
        query: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        return await self.client.invoke("email_receipts.list", query=query)

    async def issue_token(self, request: "EmailTokenRequest") -> "EmailCorrelationToken":
        return cast("EmailCorrelationToken", await self.client.invoke("email_tokens.create", body=request))

    async def revoke_token(self, identifier: UUID) -> "EmailSourceStatus":
        return cast("EmailSourceStatus", await self.client.invoke("email_tokens.revoke", identifier=identifier))
