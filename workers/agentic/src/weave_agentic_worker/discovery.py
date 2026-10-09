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

"""Model discovery for the AI gateway: Ollama's own listing, or the provider's model list.

Ollama discovery reads ``/api/tags`` and ``/api/show`` for at most 50 models within a 10
second budget; it never pulls. Responses are reduced to model facts; nothing else is kept.
Everything comes from the network, so answers are size bounded, names that are not plain
model names are skipped, and a model whose details do not arrive in time is still listed
with unknown facts.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx2
from firefly_weave.ai_policy import PolicyEndpoint
from firefly_weave.contracts.ai import MODEL_NAME_PATTERN
from firefly_weave.ollama import MAX_MODELS, ServedModel, facts

from weave_agentic_worker.providers import ProviderModel

DISCOVERY_SECONDS = 10.0
MAX_BODY = 4 * 1024 * 1024
_MODEL_NAME = re.compile(MODEL_NAME_PATTERN)


class UnexpectedAnswer(Exception):
    """An Ollama answer discovery cannot use: not HTTP 200, too large, or not JSON."""


async def _answer(client: httpx2.AsyncClient, method: str, url: str, body: Any = None) -> Any:
    async with client.stream(method, url, json=body) as response:
        if response.status_code != 200:
            raise UnexpectedAnswer
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                raise UnexpectedAnswer
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise UnexpectedAnswer from None


def _tags(answer: Any) -> list[tuple[str, dict[str, Any]]]:
    """Listed models with a usable name, sorted by name, at most ``MAX_MODELS``."""
    listed = answer.get("models") if isinstance(answer, dict) else None
    named = []
    for item in listed if isinstance(listed, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            named.append((facts(item, None).name, item))
        except ValueError:
            continue
    return sorted(named, key=lambda pair: pair[0])[:MAX_MODELS]


async def ollama_models(
    entry: PolicyEndpoint, transport: httpx2.AsyncBaseTransport, *, budget: float = DISCOVERY_SECONDS
) -> list[ServedModel]:
    """Served models with size, family, context and tool support, sorted by name.

    Raises when ``/api/tags`` does not answer within the budget, or answers unusably.
    """
    origin = entry.origin
    deadline = time.monotonic() + budget
    found: list[ServedModel] = []
    async with httpx2.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=budget
    ) as client:
        async with asyncio.timeout(budget):
            tags = _tags(await _answer(client, "GET", origin + "/api/tags"))
        for name, tag in tags:
            show = None
            left = deadline - time.monotonic()
            if left > 0:
                try:
                    async with asyncio.timeout(left):
                        answer = await _answer(client, "POST", origin + "/api/show", {"model": name})
                    show = answer if isinstance(answer, dict) else None
                except (TimeoutError, UnexpectedAnswer, httpx2.HTTPError):
                    # Details are best effort; the model is still listed, with unknown facts.
                    show = None
            found.append(facts(tag, show))
    return found


async def provider_models(owned: ProviderModel, provider: str) -> list[str]:
    """Model ids the provider lists; Azure deployments cannot be listed with a data-plane key."""
    if provider.startswith("azure-"):
        return []
    names: list[str] = []
    async for item in owned.client.models.list():
        name = item.id
        # Only plain model names leave the gateway; anything else from the provider is skipped.
        if isinstance(name, str) and len(name) <= 200 and _MODEL_NAME.fullmatch(name):
            names.append(name)
        if len(names) >= MAX_MODELS:
            break
    return names


async def first_model(entry: PolicyEndpoint, provider: str, transport: httpx2.AsyncBaseTransport) -> str | None:
    """The model a connection test uses when none is named: the first approved or served one."""
    if entry.pairs is not None:
        return next((model for name, model in sorted(entry.pairs) if name == provider), None)
    if not entry.served:
        return entry.models[0] if entry.models else None
    if entry.compat != "ollama":
        return None
    served = await ollama_models(entry, transport)
    return served[0].name if served else None


def unique_model_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first facts for each name in an already bounded discovery result."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        name = item["name"]
        if name not in seen:
            seen.add(name)
            unique.append(item)
    return unique
