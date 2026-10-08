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

"""Bounded server-to-service Lumi transport; browser data cannot choose its destination."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from firefly_weave.contracts.lumi import LumiAskRequest, LumiConfiguration, LumiReply
from firefly_weave.contracts.values import JsonValue
from firefly_weave.definitions.models import CatalogError


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
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Weave AI requires an explicit HTTPS service endpoint")
        return self


class LumiGatewayClient:
    def __init__(self, settings: LumiGatewaySettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings, self.transport = settings, transport
        self.active = 0

    @property
    def configured(self) -> bool:
        return bool(self.settings.endpoint and self.settings.token_file)

    async def ask(
        self,
        configuration: LumiConfiguration,
        request: LumiAskRequest,
        attachments: JsonValue,
        connection_config: dict[str, JsonValue],
        credential: str,
    ) -> LumiReply:
        if not self.configured:
            raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is not configured")
        if self.active >= self.settings.max_concurrency:
            raise CatalogError(429, "WV-LUMI-CAPACITY", "Weave AI is busy; try again later")
        self.active += 1
        try:
            assert self.settings.token_file and self.settings.endpoint
            token = read_service_token(Path(self.settings.token_file))
            body = {
                "profile": configuration.profile.model_dump(mode="json", by_alias=True, exclude_none=True),
                "request": request.model_dump(mode="json", by_alias=True, exclude={"draft"}),
                "attachments": attachments,
                "endpoint": connection_config["endpoint"],
                "api_version": connection_config.get("apiVersion"),
                "credential": credential,
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=configuration.profile.timeout_seconds)
                ).isoformat(),
            }
            async with asyncio.timeout(configuration.profile.timeout_seconds):
                async with (
                    httpx.AsyncClient(
                        transport=self.transport,
                        trust_env=False,
                        follow_redirects=False,
                        timeout=configuration.profile.timeout_seconds,
                    ) as client,
                    client.stream(
                        "POST", self.settings.endpoint, json=body, headers={"Authorization": "Bearer " + token}
                    ) as response,
                ):
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > 1048576:
                            raise ValueError
                        raw.extend(chunk)
                    return LumiReply.model_validate_json(raw)
        except TimeoutError:
            raise CatalogError(504, "WV-LUMI-TIMEOUT", "Weave AI did not respond in time") from None
        except Exception:
            raise CatalogError(503, "WV-LUMI-UNAVAILABLE", "Weave AI is unavailable") from None
        finally:
            self.active -= 1


def read_service_token(path: Path) -> str:
    """Read a bounded mounted token on every call so rotations take effect immediately."""
    with path.open() as source:
        token = source.read(16385).strip()
    if not token or len(token) > 16384 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
        raise ValueError("Invalid service token")
    return token
