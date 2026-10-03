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

"""Public, nonsecret sign-in settings that clients read from a server origin.

The server publishes which identity provider and public login client people
should use. Clients treat every value as a proposal: a person reviews the
issuer once, the saved profile pins it, and endpoint trust stays limited to the
issuer origin plus the reviewed origins listed here. Nothing in this contract
can carry a client secret, token, or verifier configuration.
"""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from firefly_weave.contracts.definitions import ContractModel

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SignInFlow = Literal["browser", "device"]


def _all_flows() -> list[SignInFlow]:
    return ["browser", "device"]


def _printable(value: str, limit: int) -> str:
    # C0, DEL and C1 controls (U+0000-U+001F, U+007F-U+009F) could carry terminal escape sequences.
    if (
        not value
        or len(value) > limit
        or value != value.strip()
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
    ):
        raise ValueError("A bounded printable value is required")
    return value


def exact_origin(value: str, *, allow_loopback_http: bool) -> str:
    """Return the value when it is an exact HTTPS origin (HTTP only on loopback)."""
    url = urlsplit(value)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
        or url.port == 0
        or url.scheme + "://" + url.netloc != value
    ):
        raise ValueError("An exact origin is required")
    if url.scheme != "https" and not (allow_loopback_http and url.scheme == "http" and url.hostname in LOOPBACK_HOSTS):
        raise ValueError("HTTPS is required outside loopback development")
    return value


class SignInOption(ContractModel):
    """One identity provider and public login client offered to people."""

    provider_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=100)
    issuer: str = Field(min_length=1, max_length=2048)
    client_id: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(min_length=1, max_length=32)
    trusted_endpoint_origins: list[str] = Field(default_factory=list, max_length=8)
    allow_loopback_http: bool = False
    flows: list[SignInFlow] = Field(default_factory=_all_flows, min_length=1, max_length=2)
    require_refresh_rotation: bool = True

    @field_validator("provider_id", "client_id")
    @classmethod
    def identifier(cls, value: str) -> str:
        return _printable(value, 200)

    @field_validator("display_name")
    @classmethod
    def label(cls, value: str) -> str:
        return _printable(value, 100)

    @field_validator("scopes")
    @classmethod
    def bounded_scopes(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not scope or len(scope) > 200 or any(ord(c) < 33 or ord(c) > 126 for c in scope) for scope in value
        ):
            raise ValueError("Bounded, unique OAuth scopes are required")
        return value

    @field_validator("flows")
    @classmethod
    def unique_flows(cls, value: list[SignInFlow]) -> list[SignInFlow]:
        if len(set(value)) != len(value):
            raise ValueError("Each sign-in flow may appear once")
        return value

    @model_validator(mode="after")
    def trusted(self) -> "SignInOption":
        issuer = urlsplit(self.issuer)
        if (
            not issuer.hostname
            or issuer.username
            or issuer.password
            or issuer.query
            or issuer.fragment
            or (
                issuer.scheme != "https"
                and not (self.allow_loopback_http and issuer.scheme == "http" and issuer.hostname in LOOPBACK_HOSTS)
            )
        ):
            raise ValueError("The issuer must be an HTTPS URL without credentials, query, or fragment")
        if len(set(self.trusted_endpoint_origins)) != len(self.trusted_endpoint_origins):
            raise ValueError("Trusted endpoint origins must be unique")
        for origin in self.trusted_endpoint_origins:
            exact_origin(origin, allow_loopback_http=self.allow_loopback_http)
        return self


class ClientConfiguration(ContractModel):
    """Versioned public onboarding document at `/api/v1/client-configuration`."""

    service: Literal["firefly-weave"] = "firefly-weave"
    configuration_version: Literal[1] = 1
    api_version: Literal["weave/api-v1"] = "weave/api-v1"
    display_name: str | None = Field(default=None, max_length=100)
    sign_in: list[SignInOption] = Field(default_factory=list, max_length=8)

    @field_validator("display_name")
    @classmethod
    def label(cls, value: str | None) -> str | None:
        return None if value is None else _printable(value, 100)

    @model_validator(mode="after")
    def unique_providers(self) -> "ClientConfiguration":
        if len({option.provider_id for option in self.sign_in}) != len(self.sign_in):
            raise ValueError("Each provider may be offered once")
        return self
