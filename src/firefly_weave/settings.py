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
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.engine import make_url

from firefly_weave.access.oidc import ProviderConfig
from firefly_weave.connections.secrets import SecretGrant
from firefly_weave.connectors.broker import BrokerPolicy
from firefly_weave.connectors.dispatcher import ExecutorConfig
from firefly_weave.contracts.client_configuration import ClientConfiguration, SignInFlow, SignInOption
from firefly_weave.contracts.operational_policy import OperationsPolicy
from firefly_weave.contracts.telemetry import TelemetryOptions
from firefly_weave.operations.lumi_gateway import LumiGatewaySettings


def operations_from_env() -> OperationsPolicy:
    raw = os.environ.get("WEAVE_OPERATIONS_POLICY", "{}")
    try:
        if len(raw.encode("utf-8")) > 32768:
            raise ValueError
        return OperationsPolicy.model_validate_json(raw)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid WEAVE_OPERATIONS_POLICY configuration") from None


CLIENT_SIGN_IN_INVALID = "Invalid WEAVE_CLIENT_SIGN_IN configuration"
LOCAL_BUILD_INVALID = "Invalid local development native execution configuration"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ClientSignIn(BaseModel):
    """One `WEAVE_CLIENT_SIGN_IN` entry: a public login client offered to people.

    The issuer and loopback trust are not configurable here; they come only from
    the matching OIDC provider. No field can hold a client secret.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    provider_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=100)
    client_id: str = Field(min_length=1, max_length=200)
    scopes: tuple[str, ...] = Field(min_length=1, max_length=32)
    trusted_endpoint_origins: tuple[str, ...] = Field(default=(), max_length=8)
    flows: tuple[SignInFlow, ...] = Field(default=("browser", "device"), min_length=1, max_length=2)
    require_refresh_rotation: bool = True

    def option(self, provider: ProviderConfig) -> SignInOption:
        """Return the published option; raise ValueError, without values, when it cannot be published."""
        # Verifiers map the token client claim to an actor kind; only a human client may sign people in.
        if provider.provider_id != self.provider_id or provider.clients.get(self.client_id) != "human":
            raise ValueError(CLIENT_SIGN_IN_INVALID + ": client_id must be a human client of the provider")
        try:
            return SignInOption(
                provider_id=self.provider_id,
                display_name=self.display_name,
                issuer=provider.issuer,
                client_id=self.client_id,
                scopes=list(self.scopes),
                trusted_endpoint_origins=list(self.trusted_endpoint_origins),
                allow_loopback_http=provider.local_development,
                flows=list(self.flows),
                require_refresh_rotation=self.require_refresh_rotation,
            )
        except ValidationError:
            raise ValueError(
                CLIENT_SIGN_IN_INVALID + ": labels, scopes, flows, and endpoint origins must be publishable"
            ) from None


def client_sign_in_from_env() -> tuple[ClientSignIn, ...]:
    raw = os.environ.get("WEAVE_CLIENT_SIGN_IN", "[]")
    try:
        if len(raw.encode("utf-8")) > 32768:
            raise ValueError
        return TypeAdapter(Annotated[tuple[ClientSignIn, ...], Field(max_length=8)]).validate_json(raw)
    except (ValueError, UnicodeError):
        raise ValueError(CLIENT_SIGN_IN_INVALID) from None


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    lumi: LumiGatewaySettings = Field(default_factory=LumiGatewaySettings)
    operations: OperationsPolicy = Field(default_factory=OperationsPolicy)
    telemetry: TelemetryOptions = Field(default_factory=TelemetryOptions)
    connector_packages: tuple[str, ...] = ()
    docs_enabled: bool = False
    kafka_consumer_enabled: bool = False
    broker: BrokerPolicy = Field(default_factory=BrokerPolicy)
    providers: tuple[ProviderConfig, ...] = ()
    client_sign_in: tuple[ClientSignIn, ...] = Field(default=(), max_length=8)
    display_name: str | None = None
    postgres_private_networks: tuple[str, ...] = ()
    postgres_plaintext_networks: tuple[str, ...] = ()
    postgres_ca_file: str | None = None
    postgres_max_connections: int = Field(default=8, ge=1, le=64)
    http_private_networks: tuple[str, ...] = ()
    mail_private_networks: tuple[str, ...] = Field(default=(), max_length=128)
    mail_allowed_ports: tuple[int, ...] = Field(default=(25, 465, 587, 143, 993), min_length=1, max_length=128)
    mail_allow_local_fixture: bool = False

    @field_validator("mail_allowed_ports")
    @classmethod
    def valid_mail_ports(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(not 1 <= port <= 65535 for port in value):
            raise ValueError("Mail ports must be between 1 and 65535")
        return value

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
    def local_development_build(self) -> "Settings":
        """Accept local development build attestation only on a loopback development platform.

        Production images always use the packaged image attestation; this guard keeps a
        copied executor configuration from relaxing it outside a local platform.
        """
        builds = {entry.build for entry in self.native_executors}
        if "local-development" not in builds:
            return self
        if builds != {"local-development"}:
            raise ValueError(LOCAL_BUILD_INVALID + ": executors must share one build attestation")
        databases = {
            make_url(url.get_secret_value()).host
            for url in (self.database_url, self.scheduler_database_url)
            if url is not None
        }
        trusted = {
            urlsplit(endpoint).hostname
            for provider in self.providers
            for endpoint in (provider.issuer, provider.jwks_uri)
        }
        if (
            not databases <= _LOOPBACK_HOSTS
            or not self.providers
            or not all(provider.local_development for provider in self.providers)
            or not trusted <= _LOOPBACK_HOSTS
        ):
            raise ValueError(LOCAL_BUILD_INVALID + ": only a loopback local development platform may use it")
        return self

    @model_validator(mode="after")
    def require_broker_profile(self) -> "Settings":
        if self.kafka_consumer_enabled and not self.broker.enabled:
            raise ValueError("Kafka consumers require the enabled broker profile")
        return self

    @field_validator("display_name")
    @classmethod
    def publishable_display_name(cls, value: str | None) -> str | None:
        try:
            return ClientConfiguration(display_name=value).display_name
        except ValidationError:
            raise ValueError("Invalid WEAVE_DISPLAY_NAME configuration") from None

    @model_validator(mode="after")
    def publishable_client_sign_in(self) -> "Settings":
        # Fail at startup instead of publishing a sign-in option the verifiers would reject.
        if len({entry.provider_id for entry in self.client_sign_in}) != len(self.client_sign_in):
            raise ValueError(CLIENT_SIGN_IN_INVALID + ": each provider may appear once")
        for entry in self.client_sign_in:
            entry.option(self.sign_in_provider(entry))
        return self

    def sign_in_provider(self, entry: ClientSignIn) -> ProviderConfig:
        """Return the single configured OIDC provider that an entry names."""
        matches = [provider for provider in self.providers if provider.provider_id == entry.provider_id]
        if len(matches) != 1:
            raise ValueError(CLIENT_SIGN_IN_INVALID + ": provider_id must name exactly one OIDC provider")
        return matches[0]

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
        mail_fixture = os.environ.get("WEAVE_MAIL_ALLOW_LOCAL_FIXTURE", "false").lower()
        if mail_fixture not in {"true", "false"}:
            raise ValueError("WEAVE_MAIL_ALLOW_LOCAL_FIXTURE must be true or false")
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
            lumi=LumiGatewaySettings.model_validate_json(os.environ.get("WEAVE_LUMI_GATEWAY", "{}")),
            connector_packages=TypeAdapter(tuple[str, ...]).validate_json(
                os.environ.get("WEAVE_CONNECTOR_PACKAGES", "[]")
            ),
            kafka_consumer_enabled=kafka_enabled == "true",
            broker=BrokerPolicy.model_validate_json(os.environ.get("WEAVE_BROKER_POLICY", "{}")),
            providers=providers,
            client_sign_in=client_sign_in_from_env(),
            display_name=os.environ.get("WEAVE_DISPLAY_NAME") or None,
            postgres_private_networks=json.loads(os.environ.get("WEAVE_POSTGRES_PRIVATE_NETWORKS", "[]")),
            postgres_plaintext_networks=json.loads(os.environ.get("WEAVE_POSTGRES_PLAINTEXT_NETWORKS", "[]")),
            postgres_ca_file=os.environ.get("WEAVE_POSTGRES_CA_FILE"),
            postgres_max_connections=int(os.environ.get("WEAVE_POSTGRES_MAX_CONNECTIONS", "8")),
            http_private_networks=json.loads(os.environ.get("WEAVE_HTTP_PRIVATE_NETWORKS", "[]")),
            mail_private_networks=json.loads(os.environ.get("WEAVE_MAIL_PRIVATE_NETWORKS", "[]")),
            mail_allowed_ports=json.loads(os.environ.get("WEAVE_MAIL_ALLOWED_PORTS", "[25,465,587,143,993]")),
            mail_allow_local_fixture=mail_fixture == "true",
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
