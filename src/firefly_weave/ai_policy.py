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

"""The operator AI policy (version 2): approved model endpoints, models, context sizes and output modes.

One read-only JSON file describes models, never networks. Plain HTTP and private addresses
come only from the private-origin policy, which every process enforces on its own. The
Agentic worker reads ``WEAVE_AGENTIC_POLICY_FILE``, the AI gateway ``WEAVE_LUMI_POLICY_FILE``
and the API ``WEAVE_AI_POLICY_FILE``; each re-reads the file when it changes, so edits need
no restart. A version 1 file (exact provider and model pairs plus HTTPS endpoints) still
parses, with a warning to migrate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from firefly_weave import private_origins

PROVIDERS = ("openai-chat", "openai-responses", "azure-chat", "azure-responses", "anthropic")
Provider = Literal["openai-chat", "openai-responses", "azure-chat", "azure-responses", "anthropic"]
MAX_FILE_BYTES = 65536
MAX_ENDPOINTS = 100
MAX_MODELS = 100
DEFAULT_CONTEXT_TOKENS = 8192
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
_LOG = logging.getLogger("weave.ai_policy")


class PolicyInvalid(ValueError):
    """The AI policy file is unusable; a process refuses every model call until it is fixed."""


def _emit(level: int, record: dict[str, Any]) -> None:
    _LOG.log(level, json.dumps({"version": 1, **record}, separators=(",", ":"), sort_keys=True))


class PolicyEndpoint(BaseModel):
    """One approved endpoint: where requests go, which providers and models, and how much context."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2048)
    providers: tuple[Provider, ...] = Field(min_length=1, max_length=5)
    compat: Literal["ollama"] | None = None
    credential: Literal["none", "required"] = "required"
    models: Literal["served"] | tuple[str, ...]
    context_tokens: int | None = Field(default=None, ge=512, le=2_000_000, alias="contextTokens")
    structured_output: Literal["tool", "native", "prompted"] | None = Field(default=None, alias="structuredOutput")
    max_output_tokens: int | None = Field(default=None, ge=1, le=32768, alias="maxOutputTokens")
    ca_bundle: str | None = Field(default=None, max_length=4096, alias="caBundle")
    # A version 1 file approved exact (provider, model) pairs; a migrated entry approves exactly those.
    pairs: frozenset[tuple[str, str]] | None = Field(default=None, exclude=True)

    @field_validator("models")
    @classmethod
    def exact_models(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if value == "served":
            return value
        if len(value) > MAX_MODELS or len(set(value)) != len(value) or any(not MODEL_NAME.fullmatch(m) for m in value):
            raise ValueError("List each approved model once, by its exact name")
        return value

    @model_validator(mode="after")
    def checked(self) -> PolicyEndpoint:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
        ):
            raise ValueError("Use an http or https endpoint URL without credentials, query or fragment")
        if parsed.hostname.rstrip(".") in private_origins.METADATA_HOSTS:
            raise ValueError("Metadata endpoints are never model endpoints")
        if parsed.scheme == "http" and self.credential != "none":
            raise ValueError("Plain HTTP endpoints use credential none")
        if self.ca_bundle is not None and (parsed.scheme != "https" or not Path(self.ca_bundle).is_absolute()):
            raise ValueError("caBundle is an absolute path for an https endpoint")
        if len(set(self.providers)) != len(self.providers):
            raise ValueError("List each provider once")
        return self

    @property
    def origin(self) -> str:
        parsed = urlsplit(self.url)
        return private_origins.canonical_origin(f"{parsed.scheme}://{parsed.netloc}", "model")

    @property
    def output_mode(self) -> str:
        return self.structured_output or ("native" if self.compat == "ollama" else "tool")

    @property
    def served(self) -> bool:
        return self.models == "served"

    def approves(self, provider: str, model: str) -> bool:
        if provider not in self.providers or MODEL_NAME.fullmatch(model) is None:
            return False
        if self.pairs is not None:
            return (provider, model) in self.pairs
        return self.models == "served" or model in self.models


class AIPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1, 2]
    endpoints: tuple[PolicyEndpoint, ...] = Field(max_length=MAX_ENDPOINTS)
    sha256: str | None = None

    @model_validator(mode="after")
    def unique(self) -> AIPolicy:
        ids = [endpoint.id for endpoint in self.endpoints]
        urls = [endpoint.url for endpoint in self.endpoints]
        if len(set(ids)) != len(ids) or len(set(urls)) != len(urls):
            raise ValueError("Each endpoint id and URL appears once")
        return self

    def entry_for(self, url: str) -> PolicyEndpoint | None:
        """The entry for exactly this endpoint URL, as the connection stores it."""
        return next((endpoint for endpoint in self.endpoints if endpoint.url == url), None)

    def approves_anywhere(self, provider: str, model: str) -> bool:
        return any(endpoint.approves(provider, model) for endpoint in self.endpoints)


class _VersionOneModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    provider: str
    model: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")


class _VersionOne(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    models: list[_VersionOneModel] = Field(min_length=1, max_length=MAX_MODELS)
    endpoints: list[str] = Field(min_length=1, max_length=MAX_ENDPOINTS)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _version_one(pairs: frozenset[tuple[str, str]], urls: Iterable[str], sha256: str | None) -> AIPolicy:
    if not pairs or any(provider not in PROVIDERS for provider, _ in pairs):
        raise PolicyInvalid("The AI policy names an unsupported provider.")
    endpoints = []
    for index, url in enumerate(sorted(urls)):
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise PolicyInvalid("Version 1 AI policy endpoints must use HTTPS.")
        endpoints.append(
            PolicyEndpoint(
                id=f"endpoint-{index + 1}",
                label=(parsed.hostname or "endpoint")[:80],
                url=url,
                providers=cast("tuple[Provider, ...]", tuple(sorted({provider for provider, _ in pairs}))),
                credential="required",
                models=tuple(sorted({model for _, model in pairs})),
                pairs=pairs,
            )
        )
    return AIPolicy(version=1, endpoints=tuple(endpoints), sha256=sha256)


def from_pairs(models: Iterable[tuple[str, str]], endpoints: Iterable[str]) -> AIPolicy:
    """A version 1 policy built in code: exact provider and model pairs and HTTPS endpoints."""
    try:
        return _version_one(frozenset(models), list(endpoints), None)
    except ValidationError:
        raise PolicyInvalid("The AI policy is invalid.") from None


def _reachable(policy: AIPolicy, origins: private_origins.PrivateOrigins) -> None:
    for endpoint in policy.endpoints:
        entry = origins.match("model", endpoint.url)
        exact = entry is not None and entry.source == "file"
        keyless = exact and entry is not None and entry.credentials == "none"
        if urlsplit(endpoint.url).scheme == "http" and not (
            keyless and origins.permits_plaintext("model", endpoint.url)
        ):
            raise PolicyInvalid(
                f"Endpoint {endpoint.id} uses plain HTTP without a private-origin model entry "
                "that sends no credentials."
            )
        if endpoint.served and not exact:
            raise PolicyInvalid(
                f"Endpoint {endpoint.id} can approve every installed model only through a private-origin model entry."
            )


def parse(data: bytes, origins: private_origins.PrivateOrigins | None = None) -> AIPolicy:
    """Validate the bytes of an AI policy file against this process's private-origin policy."""
    allowed = origins if origins is not None else private_origins.PrivateOrigins.empty()
    if len(data) > MAX_FILE_BYTES:
        raise PolicyInvalid("The AI policy file is larger than 64 KiB.")
    try:
        raw = json.loads(data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise PolicyInvalid("The AI policy file is not strict JSON.") from None
    digest = hashlib.sha256(data).hexdigest()
    try:
        if isinstance(raw, dict) and "version" not in raw:
            legacy = _VersionOne.model_validate(raw)
            policy = _version_one(
                frozenset((item.provider, item.model) for item in legacy.models), legacy.endpoints, digest
            )
            _emit(logging.WARNING, {"action": "ai_policy.legacy", "endpoints": len(policy.endpoints)})
        elif isinstance(raw, dict) and raw.get("version") == 2:
            policy = AIPolicy.model_validate(raw).model_copy(update={"sha256": digest})
            if any(endpoint.pairs is not None for endpoint in policy.endpoints):
                raise PolicyInvalid("Version 2 endpoints list their models; exact pairs come only from version 1.")
        else:
            raise PolicyInvalid("The AI policy file must declare version 2.")
    except ValidationError as error:
        where = "/".join(str(part) for part in error.errors()[0]["loc"]) or "document"
        raise PolicyInvalid(f"The AI policy file is invalid at {where}.") from None
    _reachable(policy, allowed)
    return policy


def render(endpoints: Sequence[Mapping[str, Any]]) -> bytes:
    """The canonical bytes of a version 2 policy: sorted keys, two-space indent, trailing newline."""
    document = {"version": 2, "endpoints": [dict(endpoint) for endpoint in endpoints]}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def read_file(path: Path) -> bytes:
    """Read the policy: a regular file, never a symbolic link, never writable by other users."""
    if path.is_symlink():
        raise PolicyInvalid("The AI policy file must not be a symbolic link.")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise PolicyInvalid("The AI policy file is missing or unreadable.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PolicyInvalid("The AI policy file must be a regular file.")
        if os.name == "posix" and info.st_mode & 0o022:
            raise PolicyInvalid("The AI policy file must not be writable by other users.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(descriptor)


class PolicyFile:
    """A policy file re-read when its modification time or size changes, checked once per call.

    A file that becomes invalid fails closed: every call raises until a valid edit lands.
    """

    def __init__(self, path: Path, origins: private_origins.PrivateOrigins) -> None:
        self.path, self.origins = path, origins
        self._stamp: tuple[int, int] | None = None
        self._policy: AIPolicy | None = None
        self.current()

    def current(self) -> AIPolicy:
        try:
            info = os.stat(self.path)
        except OSError:
            raise PolicyInvalid("The AI policy file is missing or unreadable.") from None
        stamp = (info.st_mtime_ns, info.st_size)
        if self._policy is None or stamp != self._stamp:
            self._policy = None
            policy = parse(read_file(self.path), self.origins)
            self._policy, self._stamp = policy, stamp
            _emit(logging.INFO, {"action": "ai_policy.loaded", "sha256": policy.sha256})
        return self._policy
