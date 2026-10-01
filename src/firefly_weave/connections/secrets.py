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

"""Operator-only scoped handle grants; providers never interpret tenant-selected locators."""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from firefly_weave.connections.models import unavailable
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.connectors import ResolvedSecret


class SecretUnavailable(Exception):
    def __init__(self) -> None:
        super().__init__("Credential unavailable")


class SecretProvider(Protocol):
    def resolve(self, handle: str) -> ResolvedSecret: ...


class EnvironmentSecretProvider:
    def resolve(self, handle: str) -> ResolvedSecret:
        if not handle.startswith("WEAVE_CONNECTION_SECRET_"):
            raise SecretUnavailable()
        value = os.environ.get(handle)
        if value is None:
            raise SecretUnavailable()
        # A version is deliberately opaque; do not expose a digest of low-entropy secrets.
        return ResolvedSecret(value=value)


class MountedFileSecretProvider:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def resolve(self, handle: str) -> ResolvedSecret:
        parts = handle.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts) or "\x00" in handle:
            raise SecretUnavailable()
        descriptors: list[int] = []
        try:
            # Walk even the configured root using descriptors: no symlink component or
            # path check/open race can switch resolution to an outside directory.
            directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(directory)
            for part in (*self.root.parts[1:], *parts[:-1]):
                directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                descriptors.append(directory)
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            descriptors.append(descriptor)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 65536:
                raise SecretUnavailable()
            value = os.read(descriptor, 65537)
            after = os.fstat(descriptor)
            if len(value) > 65536 or len(value) != before.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise SecretUnavailable()
            version = hashlib.sha256(f"{before.st_dev}:{before.st_ino}:{before.st_mtime_ns}".encode()).hexdigest()
            return ResolvedSecret(value=value.decode(), provider_version=version)
        except (OSError, ValueError, UnicodeError):
            raise SecretUnavailable() from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


@dataclass(frozen=True)
class SecretGrant:
    scope: Scope
    handle: str
    provider: str
    locator: str


class ScopedSecrets:
    def __init__(
        self, providers: dict[str, SecretProvider] | None = None, grants: tuple[SecretGrant, ...] = ()
    ) -> None:
        self._providers = dict(providers or {})
        self._grants: dict[tuple[Scope, str], SecretGrant] = {}
        for grant in grants:
            key = (grant.scope, grant.handle)
            if grant.scope.environment_id is None or key in self._grants or grant.provider not in self._providers:
                raise ValueError("Invalid operator secret grant")
            self._grants[key] = grant

    def check(self, scope: Scope, handle: str) -> None:
        if (scope, handle) not in self._grants:
            raise unavailable()

    def resolve(self, scope: Scope, handle: str) -> ResolvedSecret:
        self.check(scope, handle)
        grant = self._grants[(scope, handle)]
        try:
            return self._providers[grant.provider].resolve(grant.locator)
        except Exception:
            raise SecretUnavailable() from None
