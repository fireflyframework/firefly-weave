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

"""Explicit server settings, loaded only by the server entry point."""

import os

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, field_validator, model_validator
from sqlalchemy.engine import make_url

from firefly_weave.access.oidc import ProviderConfig
from firefly_weave.connections.secrets import SecretGrant
from firefly_weave.connectors.broker import BrokerPolicy
from firefly_weave.connectors.dispatcher import ExecutorConfig
from firefly_weave.contracts.operational_policy import OperationsPolicy
from firefly_weave.contracts.telemetry import TelemetryOptions


def operations_from_env() -> OperationsPolicy:
    raw = os.environ.get("WEAVE_OPERATIONS_POLICY", "{}")
    try:
        if len(raw.encode("utf-8")) > 32768:
            raise ValueError
        return OperationsPolicy.model_validate_json(raw)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid WEAVE_OPERATIONS_POLICY configuration") from None


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    operations: OperationsPolicy = Field(default_factory=OperationsPolicy)
    telemetry: TelemetryOptions = Field(default_factory=TelemetryOptions)
    connector_packages: tuple[str, ...] = ()
    docs_enabled: bool = False
    kafka_consumer_enabled: bool = False
    broker: BrokerPolicy = Field(default_factory=BrokerPolicy)
    providers: tuple[ProviderConfig, ...] = ()
    postgres_private_networks: tuple[str, ...] = ()
    postgres_plaintext_networks: tuple[str, ...] = ()
    postgres_ca_file: str | None = None
    postgres_max_connections: int = Field(default=8, ge=1, le=64)
    http_private_networks: tuple[str, ...] = ()
    native_executors: tuple[ExecutorConfig, ...] = ()
    native_image_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    secret_grants: tuple[SecretGrant, ...] = ()
    secret_root: str | None = None
    scheduler_enabled: bool = True
    scheduler_database_url: SecretStr | None = None
    scheduler_poll_seconds: float = Field(default=1.0, ge=0.05, le=60)
    database_url: SecretStr
    database_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    shutdown_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def require_broker_profile(self) -> "Settings":
        if self.kafka_consumer_enabled and not self.broker.enabled:
            raise ValueError("Kafka consumers require the enabled broker profile")
        return self

    @field_validator("database_url", "scheduler_database_url")
    @classmethod
    def require_postgresql(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
            valid = url.drivername == "postgresql+asyncpg" and bool(url.host and url.database and url.username)
        except Exception:
            valid = False
        if not valid:
            raise ValueError("A complete PostgreSQL asyncpg URL is required")
        return value

    @classmethod
    def from_env(cls) -> "Settings":
        value = os.environ.get("WEAVE_DATABASE_URL")
        if not value:
            raise ValueError("WEAVE_DATABASE_URL is required")
        import json

        enabled = os.environ.get("WEAVE_SCHEDULER_ENABLED", "true").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("WEAVE_SCHEDULER_ENABLED must be true or false")
        kafka_enabled = os.environ.get("WEAVE_KAFKA_CONSUMER_ENABLED", "false").lower()
        if kafka_enabled not in {"true", "false"}:
            raise ValueError("WEAVE_KAFKA_CONSUMER_ENABLED must be true or false")
        docs_enabled = os.environ.get("WEAVE_DOCS_ENABLED", "false").lower()
        if docs_enabled not in {"true", "false"}:
            raise ValueError("WEAVE_DOCS_ENABLED must be true or false")
        providers = json.loads(os.environ.get("WEAVE_OIDC_PROVIDERS", "[]"))
        telemetry_raw = os.environ.get("WEAVE_TELEMETRY", "{}")
        try:
            if len(telemetry_raw.encode("utf-8")) > 32768:
                raise ValueError
            telemetry = TelemetryOptions.model_validate_json(telemetry_raw)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid WEAVE_TELEMETRY configuration") from None
        return cls(
            database_url=SecretStr(value),
            telemetry=telemetry,
            docs_enabled=docs_enabled == "true",
            operations=operations_from_env(),
            connector_packages=TypeAdapter(tuple[str, ...]).validate_json(
                os.environ.get("WEAVE_CONNECTOR_PACKAGES", "[]")
            ),
            kafka_consumer_enabled=kafka_enabled == "true",
            broker=BrokerPolicy.model_validate_json(os.environ.get("WEAVE_BROKER_POLICY", "{}")),
            providers=providers,
            postgres_private_networks=json.loads(os.environ.get("WEAVE_POSTGRES_PRIVATE_NETWORKS", "[]")),
            postgres_plaintext_networks=json.loads(os.environ.get("WEAVE_POSTGRES_PLAINTEXT_NETWORKS", "[]")),
            postgres_ca_file=os.environ.get("WEAVE_POSTGRES_CA_FILE"),
            postgres_max_connections=int(os.environ.get("WEAVE_POSTGRES_MAX_CONNECTIONS", "8")),
            http_private_networks=json.loads(os.environ.get("WEAVE_HTTP_PRIVATE_NETWORKS", "[]")),
            native_executors=TypeAdapter(tuple[ExecutorConfig, ...]).validate_json(
                os.environ.get("WEAVE_NATIVE_EXECUTORS", "[]")
            ),
            native_image_digest=os.environ.get("WEAVE_NATIVE_IMAGE_DIGEST"),
            secret_grants=TypeAdapter(tuple[SecretGrant, ...]).validate_json(
                os.environ.get("WEAVE_SECRET_GRANTS", "[]")
            ),
            secret_root=os.environ.get("WEAVE_SECRET_ROOT"),
            scheduler_enabled=enabled == "true",
            scheduler_database_url=SecretStr(os.environ["WEAVE_SCHEDULER_DATABASE_URL"])
            if os.environ.get("WEAVE_SCHEDULER_DATABASE_URL")
            else None,
        )
