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

"""Explicit exporter configuration, independent of server and telemetry libraries."""

import math
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class TelemetryOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    enabled: bool = Field(default=False, strict=True)
    traces_endpoint: str | None = Field(default=None, max_length=2048, repr=False)
    metrics_endpoint: str | None = Field(default=None, max_length=2048, repr=False)
    authorization: SecretStr | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=5.0, gt=0, le=5)
    metrics_interval_seconds: float = Field(default=30.0, ge=5, le=300)

    @field_validator("traces_endpoint", "metrics_endpoint")
    @classmethod
    def endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            url = urlsplit(value)
            valid = (
                url.scheme in {"http", "https"}
                and url.hostname
                and url.port != 0
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and not any(ord(char) < 33 or ord(char) > 126 for char in value)
                and (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("An explicit TLS collector endpoint or loopback development endpoint is required")
        return value

    @field_validator("authorization")
    @classmethod
    def header(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raw = value.get_secret_value()
            if not 1 <= len(raw) <= 4096 or any(ord(c) < 32 or ord(c) > 126 for c in raw):
                raise ValueError("A bounded printable authorization header is required")
        return value

    @field_validator("timeout_seconds", "metrics_interval_seconds")
    @classmethod
    def finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("A finite telemetry interval is required")
        return value

    @model_validator(mode="after")
    def destination(self) -> "TelemetryOptions":
        if self.enabled and not (self.traces_endpoint or self.metrics_endpoint):
            raise ValueError("Enabled telemetry requires at least one explicit endpoint")
        return self
