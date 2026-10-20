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

"""Bounded server-to-service AI gateway transport; browser data cannot choose its destination.

The API reaches the AI gateway over HTTPS, or over plain HTTP to a loopback address in its
own network namespace when the private-origin policy has a ``model`` entry with loopback
credentials for it (the local developer platform). Connection tests and model discovery use
the sibling routes ``test`` and ``models`` of the configured endpoint.
"""

import asyncio
import ipaddress
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_weave import private_origins
from firefly_weave.contracts.ai import AI_ERRORS, GatewayModelList, ai_message
from firefly_weave.contracts.lumi import LumiAskRequest, LumiConfiguration, LumiReply
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError

GATEWAY_REFUSED = "A plain HTTP AI gateway needs a private-origin model entry with loopback credentials."
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
# Gateway and model codes the API answers as their own Weave AI problems; any other is unavailable.
_PROBLEMS: dict[str, tuple[int, str]] = {
    "LLM_MODEL_NOT_FOUND": (422, "WV-LUMI-MODEL-NOT-FOUND"),
    "LLM_UNREACHABLE": (503, "WV-LUMI-UNREACHABLE"),
    "LLM_CONTEXT_LIMIT": (413, "WV-LUMI-CONTEXT-LIMIT"),
    "LLM_OUTPUT": (422, "WV-LUMI-OUTPUT"),
    "LUMI_REPETITION": (422, "WV-LUMI-OUTPUT"),
    "LLM_TIMEOUT": (504, "WV-LUMI-TIMEOUT"),
    "LUMI_TIMEOUT": (504, "WV-LUMI-TIMEOUT"),
    "LLM_RATE_LIMITED": (429, "WV-LUMI-CAPACITY"),
    "LLM_CAPACITY": (429, "WV-LUMI-CAPACITY"),
    "LUMI_CAPACITY": (429, "WV-LUMI-CAPACITY"),
    "LUMI_LIMIT": (413, "WV-LUMI-LIMIT"),
}
_MESSAGES = {
    "LUMI_TIMEOUT": "Weave AI did not respond in time",
    "LLM_CAPACITY": "Weave AI is busy; try again later",
    "LUMI_CAPACITY": "Weave AI is busy; try again later",
    "LUMI_LIMIT": "Selected context is too large",
    "LUMI_REPETITION": (
        "Weave AI's model started repeating itself, so its answer was stopped. Try a larger model, "
        "or start a new conversation so less history is sent."
    ),
}


class GatewayRefused(Exception):
    """The gateway answered with an error status and, when it sent one, a product code."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def weave_ai_problem(
    code: str, configuration: LumiConfiguration, endpoint: str, context_tokens: int | None = None
) -> CatalogError:
    """The Weave AI problem for a gateway or model code; its message is product copy, never provider text."""
    status, problem = _PROBLEMS.get(code, (503, "WV-LUMI-UNAVAILABLE"))
    message = _MESSAGES.get(code)
    if message is None and code == "LLM_CONTEXT_LIMIT" and context_tokens is None:
        message = "The prompt is too long for this model's context."
    if message is None and code in AI_ERRORS:
        n = configuration.profile.timeout_seconds if code == "LLM_TIMEOUT" else context_tokens or 0
        message = ai_message(code, model=configuration.profile.model, endpoint=endpoint, n=n)
    return CatalogError(status, problem, message or "Weave AI is unavailable")


def _loopback(host: str | None) -> bool:
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LumiGatewaySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    endpoint: str | None = None
    token_file: str | None = None
    max_concurrency: int = Field(default=4, ge=1, le=32)

    @model_validator(mode="after")
    def trusted_service(self) -> "LumiGatewaySettings":
        if bool(self.endpoint) != bool(self.token_file):
            raise ValueError("Weave AI gateway endpoint and token file must be configured together")
        if self.endpoint:
            url = urlsplit(self.endpoint)
            secure = url.scheme == "https" or (url.scheme == "http" and _loopback(url.hostname))
            if not secure or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("The AI gateway needs an HTTPS endpoint, or plain HTTP to a loopback address")
            if url.path.endswith("/"):
                # Sibling routes such as ``test`` resolve next to the endpoint's last segment, not under it.
                raise ValueError("The AI gateway endpoint names its route without a trailing slash")
        return self


async def _refusal_code(response: httpx.Response) -> str:
    """The product code of a refusal, read from at most 4 KiB of its body; "" when it has none."""
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw.extend(chunk)
        if len(raw) > 4096:
            return ""
    try:
        code = json.loads(raw).get("code")
    except (ValueError, AttributeError):
        return ""
    return code if isinstance(code, str) and _CODE.fullmatch(code) else ""


class LumiGatewayClient:
    def __init__(
        self,
        settings: LumiGatewaySettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        origins: private_origins.PrivateOrigins | None = None,
    ) -> None:
        self.settings, self.transport = settings, transport
        self.active = 0
        self._origins: private_origins.PrivateOrigins | None = None
        if settings.endpoint and urlsplit(settings.endpoint).scheme == "http":
            policy = origins if origins is not None else private_origins.active()
            entry = policy.match("model", settings.endpoint)
            if entry is None or entry.source != "file" or entry.credentials != "loopback":
                raise ValueError(GATEWAY_REFUSED)
            self._origins = policy

    @property
    def configured(self) -> bool:
        return bool(self.settings.endpoint and self.settings.token_file)

    def route(self, name: str) -> str:
        """A sibling route of the configured endpoint, such as ``test`` next to ``…/v1/lumi``."""
        assert self.settings.endpoint
        return urljoin(self.settings.endpoint, name)

    def _client(self, timeout: float) -> httpx.AsyncClient:
        transport = self.transport
        if transport is None and self._origins is not None:
            from firefly_weave.connectors.egress import EgressPolicy, PinnedTransport

            assert self.settings.endpoint
            parsed = urlsplit(self.settings.endpoint)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            policy = EgressPolicy(
                allowed_origins=(origin,), purpose="model", origins=self._origins, sends_credentials=True
            )
            transport = PinnedTransport(policy, origin, max_connections=self.settings.max_concurrency)
        return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, timeout=timeout)

    async def _post(self, url: str, body: dict[str, Any], timeout: float, limit: int) -> bytes:
        assert self.settings.token_file
        token = read_service_token(Path(self.settings.token_file))
        async with (
            self._client(timeout) as client,
            client.stream("POST", url, json=body, headers={"Authorization": "Bearer " + token}) as response,
        ):
            if response.status_code >= 300:
                raise GatewayRefused(response.status_code, await _refusal_code(response))
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                if len(raw) + len(chunk) > limit:
                    raise ValueError("Gateway answer too large")
                raw.extend(chunk)
            return bytes(raw)

    async def ask(
        self,
        configuration: LumiConfiguration,
        request: LumiAskRequest,
        attachments: JsonValue,
        connection_config: dict[str, JsonValue],
        credential: str | None,
        context_tokens: int | None = None,
    ) -> LumiReply:
        if not self.configured:
            raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is not configured")
        if self.active >= self.settings.max_concurrency:
            raise CatalogError(429, "WV-LUMI-CAPACITY", "Weave AI is busy; try again later")
        self.active += 1
        try:
            assert self.settings.endpoint
            timeout = configuration.profile.timeout_seconds
            body = {
                "profile": configuration.profile.model_dump(mode="json", by_alias=True, exclude_none=True),
                "request": request.model_dump(mode="json", by_alias=True, exclude={"draft"}),
                "attachments": attachments,
                "endpoint": connection_config["endpoint"],
                "api_version": connection_config.get("apiVersion"),
                "expires_at": (datetime.now(UTC) + timedelta(seconds=timeout)).isoformat(),
            }
            if credential is not None:
                # A keyless local model endpoint gets no credential at all.
                body["credential"] = credential
            async with asyncio.timeout(timeout):
                raw = await self._post(self.settings.endpoint, body, timeout, 1048576)
            return LumiReply.model_validate_json(raw)
        except TimeoutError:
            raise CatalogError(504, "WV-LUMI-TIMEOUT", "Weave AI did not respond in time") from None
        except GatewayRefused as refused:
            endpoint = str(connection_config.get("endpoint", "the endpoint"))
            raise weave_ai_problem(refused.code, configuration, endpoint, context_tokens) from None
        except Exception:
            raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is unavailable") from None
        finally:
            self.active -= 1

    async def test(
        self,
        connection_config: dict[str, JsonValue],
        credential: str | None,
        *,
        provider: str,
        model: str | None,
        probe_tools: bool,
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        """One gateway connection test; the answer carries a code and timings, never the model's text."""
        if not self.configured:
            raise CatalogError(503, "WV-AI-GATEWAY-MISSING", "Tests need the AI gateway")
        if self.active >= self.settings.max_concurrency:
            raise CatalogError(429, "WV-LUMI-CAPACITY", "The AI gateway is busy; try again later")
        self.active += 1
        try:
            body: dict[str, Any] = {
                "provider": provider,
                "endpoint": connection_config["endpoint"],
                "probe_tools": probe_tools,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=timeout_seconds)).isoformat(),
            }
            if model is not None:
                body["model"] = model
            if connection_config.get("apiVersion") is not None:
                body["api_version"] = connection_config["apiVersion"]
            if credential is not None:
                body["credential"] = credential
            async with asyncio.timeout(timeout_seconds + 5):
                raw = await self._post(self.route("test"), body, timeout_seconds + 5, 65536)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Unexpected gateway answer")
            return value
        except TimeoutError:
            raise CatalogError(504, "WV-AI-GATEWAY-TIMEOUT", "The AI gateway did not answer in time") from None
        except Exception:
            raise CatalogError(503, "WV-AI-GATEWAY-UNAVAILABLE", "The AI gateway is unavailable") from None
        finally:
            self.active -= 1

    async def models(
        self,
        connection_config: dict[str, JsonValue],
        credential: str | None,
        *,
        provider: str,
        timeout_seconds: float = 15.0,
    ) -> GatewayModelList:
        """Discover bounded model metadata through the same owned, authenticated gateway transport."""
        if not self.configured:
            raise CatalogError(503, "WV-AI-GATEWAY-MISSING", "Discovery needs the AI gateway")
        if self.active >= self.settings.max_concurrency:
            raise CatalogError(429, "WV-LUMI-CAPACITY", "The AI gateway is busy; try again later")
        self.active += 1
        try:
            body: dict[str, Any] = {
                "provider": provider,
                "endpoint": connection_config["endpoint"],
                "expires_at": (datetime.now(UTC) + timedelta(seconds=timeout_seconds)).isoformat(),
            }
            if connection_config.get("apiVersion") is not None:
                body["api_version"] = connection_config["apiVersion"]
            if credential is not None:
                body["credential"] = credential
            async with asyncio.timeout(timeout_seconds + 5):
                raw = await self._post(self.route("models"), body, timeout_seconds + 5, 262144)
            return GatewayModelList.model_validate_json(raw)
        except TimeoutError:
            raise CatalogError(504, "WV-AI-GATEWAY-TIMEOUT", "The AI gateway did not answer in time") from None
        except Exception:
            raise CatalogError(503, "WV-AI-GATEWAY-UNAVAILABLE", "The AI gateway is unavailable") from None
        finally:
            self.active -= 1


def read_service_token(path: Path) -> str:
    """Read a bounded mounted token on every call so rotations take effect immediately."""
    with path.open() as source:
        token = source.read(16385).strip()
    if not token or len(token) > 16384 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
        raise ValueError("Invalid service token")
    return token
