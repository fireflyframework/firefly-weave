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

"""Model failures classified into product codes; only codes leave the worker, never provider text.

Classification reads the HTTP status, a few fixed body patterns and the cause chain: a
connect error (refused, DNS, TLS before the request is written) has not started, while a
read error after the request was written leaves the outcome unknown.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Literal

import httpx2
from firefly_weave.contracts.ai import AI_ERRORS
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.private_origins import PrivateOriginDenied
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded

from weave_agentic_worker.egress import ModelEgressDenied

_MODEL_NOT_FOUND = re.compile(r"model .* not found", re.IGNORECASE)
_NO_TOOLS = re.compile(r"does not support tools", re.IGNORECASE)
_CONTEXT = re.compile(r"context_length_exceeded|maximum context length|context window", re.IGNORECASE)


def failure(code: str, outcome: Literal["not_started", "failed", "unknown"] | None = None) -> ConnectorFailure:
    return ConnectorFailure(code, outcome or AI_ERRORS[code].outcome)


def _chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 32:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _body(value: object) -> str:
    # Matched against fixed patterns only; never logged, stored or returned.
    if isinstance(value, str):
        return value[:8192]
    try:
        return json.dumps(value)[:8192]
    except (TypeError, ValueError):
        return ""


def classify(error: BaseException) -> ConnectorFailure:
    """The product code and outcome for one model failure."""
    chain = list(_chain(error))
    for item in chain:
        if isinstance(item, ConnectorFailure):
            return item
    if any(isinstance(item, ModelEgressDenied | PrivateOriginDenied) for item in chain):
        return failure("LLM_POLICY")
    if any(isinstance(item, UsageLimitExceeded) for item in chain):
        return failure("LLM_LIMIT")
    http = next((item for item in chain if isinstance(item, ModelHTTPError)), None)
    if http is not None:
        body = _body(http.body)
        if http.status_code == 404 or _MODEL_NOT_FOUND.search(body):
            return failure("LLM_MODEL_NOT_FOUND")
        if http.status_code == 400 and _NO_TOOLS.search(body):
            return failure("LLM_NO_TOOL_SUPPORT")
        if http.status_code in (401, 403):
            return failure("LLM_AUTH")
        if http.status_code == 429:
            return failure("LLM_RATE_LIMITED")
        if _CONTEXT.search(body):
            return failure("LLM_CONTEXT_LIMIT")
        return failure("LLM_PROVIDER")
    if any(isinstance(item, httpx2.ConnectError | httpx2.ConnectTimeout) for item in chain):
        return failure("LLM_UNREACHABLE")
    if any(isinstance(item, httpx2.TimeoutException) for item in chain):
        return failure("LLM_TIMEOUT", "unknown")
    if any(isinstance(item, UnexpectedModelBehavior) for item in chain):
        return failure("LLM_OUTPUT")
    return failure("LLM_PROVIDER")
