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

"""Worker destinations and relative paths fail closed outside explicit policy."""

import pytest
from firefly_weave.contracts.connectors import ConnectorFailure

from weave_files_worker.policy import WorkerPolicy, relative_path, scoped_path


def test_paths_reject_traversal_and_control_sequences():
    for value in ("../secret", "/absolute", "a/../b", "a\\b", "a\r\nDELE b", "a//b", "a/./b"):
        with pytest.raises(ConnectorFailure):
            relative_path(value)
    assert scoped_path("/inbox", "invoices/one.pdf") == "/inbox/invoices/one.pdf"


def test_worker_requires_exact_origins_and_explicit_cleartext_ftp():
    policy = WorkerPolicy(origins=frozenset({"sftp://files.example:22", "ftp://files.example:21"}))
    assert policy.destination("sftp://files.example:22") == "sftp://files.example:22"
    with pytest.raises(ConnectorFailure):
        policy.destination("sftp://files.example:23")
    with pytest.raises(ConnectorFailure):
        policy.destination("ftp://files.example:21")
    with pytest.raises(ConnectorFailure):
        policy.download_url("https://graph.microsoft.com.evil.test/file")


async def test_ftp_credentials_cannot_inject_control_commands():
    from weave_files_worker.providers import open_provider

    with pytest.raises(ConnectorFailure, match="FILE_CONNECTION"):
        async with open_provider(
            "weave-ftp",
            {"host": "files.example", "port": 21, "rootPath": "/", "username": "user\r\nDELE invoice"},
            "password",
            WorkerPolicy(frozenset({"ftp://files.example:21"}), allow_cleartext_ftp=True),
        ):
            pass


@pytest.mark.parametrize("name", ["weave-ftp", "weave-ftps", "weave-sftp"])
@pytest.mark.parametrize("change", [{"serverRootIsolated": False}, {"rootPath": "/incoming"}])
async def test_remote_provider_requires_isolated_account_root_before_network(name, change, monkeypatch):
    from weave_files_worker.providers import open_provider

    async def no_network(*args):
        pytest.fail("Invalid isolation configuration must fail before opening a network connection")

    monkeypatch.setattr(WorkerPolicy, "address", no_network)
    config = {"host": "files.example", "port": 22, "username": "user", "rootPath": "/", "serverRootIsolated": True}
    with pytest.raises(ConnectorFailure, match="FILE_SCOPE"):
        async with open_provider(name, {**config, **change}, "password", WorkerPolicy(frozenset())):
            pass


@pytest.mark.parametrize("operation", ["write", "move"])
async def test_ftp_refuses_non_atomic_destination_operations_by_default(operation):
    from unittest.mock import AsyncMock

    from weave_files_worker.providers import RemoteFiles

    ftp = AsyncMock()
    provider = RemoteFiles("/", ftp=ftp)
    with pytest.raises(ConnectorFailure, match="FILE_ATOMIC_DESTINATION"):
        if operation == "write":
            await provider.write("invoice.pdf", "", None, None)
        else:
            await provider.move("old.pdf", "invoice.pdf", "")
    assert not ftp.mock_calls
