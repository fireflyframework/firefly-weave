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

"""Conversation memory: what an AI agent remembers across runs under a key, and the API shapes that show it.

A conversation is stored per environment and workflow under ``key_hash``, the SHA-256 of the workflow's name, a zero
byte and the key's value. The raw key is never stored or sent: anyone who can guess a key can compute its hash, so
the hash is pseudonymous, not anonymous. These are platform API bodies, so every field is snake_case.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

import rfc8785
from pydantic import Field

from firefly_weave.contracts.definitions import ContractModel, ResourceName
from firefly_weave.contracts.values import JsonData, JsonValue

# The longest key an expression may evaluate to, in characters.
CONVERSATION_KEY_LENGTH: Final = 256
# How long a conversation is kept after its last exchange when the step sets no ttlSeconds: 30 days.
DEFAULT_CONVERSATION_TTL_SECONDS: Final = 2_592_000
# Messages a conversation holds at most: a step's largest window.
CONVERSATION_MESSAGES: Final = 200
# Expired conversations one sweep deletes per environment, conversations one workflow purge deletes, and the runs a
# delete names as having loaded the conversation.
SWEEP_BATCH: Final = 500
PURGE_BATCH: Final = 1_000
LOADED_BY_LIMIT: Final = 100

type ConversationKeyHash = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
type ConversationWorkflow = Annotated[ResourceName, Field(max_length=200)]


def conversation_key_hash(workflow: str, key: str) -> str:
    """The lowercase hex SHA-256 of ``workflow``, a zero byte and ``key``; the zero byte keeps ``("ab", "c")`` and
    ``("a", "bc")`` apart."""
    if not 1 <= len(key) <= CONVERSATION_KEY_LENGTH:
        raise ValueError(f"A conversation key is 1 to {CONVERSATION_KEY_LENGTH} characters")
    return hashlib.sha256(workflow.encode() + b"\x00" + key.encode()).hexdigest()


def messages_digest(messages: JsonValue) -> str:
    """The digest a load's receipt keeps of the messages it delivered: SHA-256 of ``{"messages": ...}`` in RFC 8785
    form, so replay can check the recorded event against its receipt."""
    return hashlib.sha256(rfc8785.dumps({"messages": messages})).hexdigest()


class ConversationMessage(ContractModel):
    """One remembered message, in the exact shape of a memory entry the model reads."""

    role: Literal["user", "assistant"]
    content: JsonData


class AgentConversationSummary(ContractModel):
    workflow: ConversationWorkflow
    key_hash: ConversationKeyHash
    message_count: int = Field(ge=0, le=CONVERSATION_MESSAGES)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class AgentConversation(AgentConversationSummary):
    messages: list[ConversationMessage] = Field(max_length=CONVERSATION_MESSAGES)
    last_run_id: UUID


class AgentConversationQuery(ContractModel):
    """`agent_conversations.list` query parameters: newest update first, expired conversations left out."""

    workflow: ConversationWorkflow | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Annotated[str, Field(min_length=1, max_length=2048, pattern=r"^[A-Za-z0-9_-]+$")] | None = None


class AgentConversationDeletion(ContractModel):
    """What a delete did: whether a conversation was stored, and up to 100 runs that loaded it, so an erasure can
    purge them too."""

    workflow: ConversationWorkflow
    key_hash: ConversationKeyHash
    deleted: bool
    loaded_by: list[UUID] = Field(max_length=LOADED_BY_LIMIT)
    more_loaded_by: bool


class ConversationPurgeRequest(ContractModel):
    confirm_workflow: ConversationWorkflow


class ConversationPurge(ContractModel):
    workflow: ConversationWorkflow
    deleted: int = Field(ge=0, le=PURGE_BATCH)
    # More of the workflow's conversations remain: call again.
    more: bool
