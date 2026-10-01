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

"""Pure broker-family configuration and operator-owned exact numeric routes."""

import ipaddress
import json
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.definitions import ContractModel, ResourceName


def host_name(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
        if str(address) != value or "%" in value:
            raise ValueError
        return value
    except ValueError:
        if not re.fullmatch(r"[a-z][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*", value) or len(value) > 253:
            raise ValueError("Invalid broker hostname") from None
        return value


def destination(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "kafka"
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is None
        or any(ord(c) <= 32 for c in value)
        or "\\" in value
        or not 1 <= parsed.port <= 65535
    ):
        raise ValueError("Invalid broker destination")
    host = host_name(parsed.hostname or "")
    if value != f"kafka://{'[' + host + ']' if ':' in host else host}:{parsed.port}":
        raise ValueError("Canonical broker destination required")
    return host, parsed.port


class BrokerConnectionConfig(ContractModel):
    driver: Literal["kafka"]
    cluster_id: ResourceName
    bootstrap: tuple[str, ...] = Field(min_length=1, max_length=16)
    topics: tuple[str, ...] = Field(min_length=1, max_length=64)
    security_protocol: Literal["SASL_SSL", "PLAINTEXT"] = "SASL_SSL"
    sasl_mechanism: Literal["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"] = "PLAIN"
    username: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def supported(self) -> "BrokerConnectionConfig":
        for value in self.bootstrap:
            destination(value)
        if len(set(self.topics)) != len(self.topics) or any(
            not re.fullmatch(r"[a-zA-Z0-9_-][a-zA-Z0-9._-]{0,248}", v) or v in {".", ".."} for v in self.topics
        ):
            raise ValueError("Exact bounded Kafka topics required")
        if (self.security_protocol == "SASL_SSL") != (self.username is not None):
            raise ValueError("Verified TLS credentials required")
        return self


class BrokerPublishConfig(ContractModel):
    topic: str = Field(min_length=1, max_length=249)


class BrokerRoutePin(ContractModel):
    connection_revision_id: UUID
    advertised: str
    addresses: tuple[str, ...] = Field(min_length=1, max_length=4)
    ca_file: str | None = None
    plaintext: bool = False

    @field_validator("advertised")
    @classmethod
    def endpoint(cls, value: str) -> str:
        destination(value)
        return value

    @model_validator(mode="after")
    def numeric(self) -> "BrokerRoutePin":
        for value in self.addresses:
            address = ipaddress.ip_address(value)
            if str(address) != value or "%" in value:
                raise ValueError("Canonical numeric route required")
            if self.plaintext and not address.is_loopback:
                raise ValueError("Plaintext Kafka is isolated loopback development only")
        return self


class BrokerPolicy(ContractModel):
    enabled: bool = False
    routes: tuple[BrokerRoutePin, ...] = Field(default=(), max_length=1024)
    max_clients: int = Field(default=8, ge=1, le=8)
    cleanup_seconds: float = Field(default=5.0, gt=0, le=5)

    @model_validator(mode="after")
    def unique(self) -> "BrokerPolicy":
        keys = [(route.connection_revision_id, route.advertised) for route in self.routes]
        if len(keys) != len(set(keys)):
            raise ValueError("Ambiguous broker routes")
        return self


def validate_broker_connection(request: ConnectionRequest) -> None:
    from firefly_weave.connections.models import unavailable

    try:
        config = BrokerConnectionConfig.model_validate_json(json.dumps(request.config))
        approved = {destination(v) for v in request.allowed_destinations}
        if not {destination(v) for v in config.bootstrap}.issubset(approved):
            raise ValueError
        if set(request.secret_refs) != ({"password"} if config.security_protocol == "SASL_SSL" else set()):
            raise ValueError
    except ValueError:
        raise unavailable() from None
