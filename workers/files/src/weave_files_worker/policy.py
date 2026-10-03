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

"""Explicit egress and remote path bounds for independently operated workers."""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from firefly_weave.connectors.egress import EgressPolicy, resolve
from firefly_weave.contracts.connectors import ConnectorFailure


def relative_path(value: str, *, empty: bool = False) -> str:
    if (
        (not value and not empty)
        or value.startswith("/")
        or "\\" in value
        or len(value) > 1024
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise ConnectorFailure("FILE_PATH", "not_started")
    if value and any(part in {"", ".", ".."} for part in value.split("/")):
        raise ConnectorFailure("FILE_PATH", "not_started")
    return value


def scoped_path(root: str, value: str) -> str:
    if not root.startswith("/") or ".." in PurePosixPath(root).parts:
        raise ConnectorFailure("FILE_PATH", "not_started")
    relative_path(value, empty=True)
    return str(PurePosixPath(root) / value)


@dataclass(frozen=True)
class WorkerPolicy:
    origins: frozenset[str]
    download_origins: frozenset[str] = field(default_factory=frozenset)
    private_networks: tuple[str, ...] = ()
    allow_cleartext_ftp: bool = False
    allow_non_atomic_ftp_destinations: bool = False

    def destination(self, value: str) -> str:
        parsed = urlsplit(value)
        if (
            value not in self.origins
            or parsed.scheme not in {"https", "ftp", "ftps", "sftp"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ConnectorFailure("FILE_POLICY", "not_started")
        if parsed.scheme == "ftp" and not self.allow_cleartext_ftp:
            raise ConnectorFailure("FILE_POLICY", "not_started")
        return value

    def download_url(self, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.hostname
            or f"https://{parsed.netloc}" not in self.download_origins
        ):
            raise ConnectorFailure("FILE_REDIRECT", "not_started")
        return value

    async def address(self, origin: str) -> str:
        self.destination(origin)
        parsed = urlsplit(origin)
        port = parsed.port or 443
        addresses = await resolve(str(parsed.hostname), port)
        surrogate = f"https://{parsed.hostname}:{port}"
        EgressPolicy((surrogate,), self.private_networks).validate(surrogate, addresses)
        return addresses[0]
