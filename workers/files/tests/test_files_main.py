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

"""Worker boot policy and catalog exports use the public platform contracts."""

import json

import pytest
from firefly_weave.compiler.catalog import CatalogSnapshot

from weave_files_worker.main import read_policy, run


def test_catalog_exports_thirty_actions_and_five_connections(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["weave-files-worker", "--catalog"])
    run()
    catalog = CatalogSnapshot.from_lock(json.loads(capsys.readouterr().out))
    assert catalog.resolve("Action", "weave-sftp-download@1.0.0") is not None
    assert catalog.resolve("Action", "weave-microsoft-drive-write@1.0.0") is not None
    assert sum(resource.kind in {"Action", "Connector"} for resource in catalog.resources.values()) == 35


def test_worker_policy_requires_specific_origins(tmp_path):
    file = tmp_path / "policy.json"
    file.write_text('{"origins":["https://graph.microsoft.com"],"downloadOrigins":["https://tenant.sharepoint.com"]}')
    assert read_policy(file).download_url("https://tenant.sharepoint.com/file?key=signed")
    assert not read_policy(file).allow_non_atomic_ftp_destinations
    file.write_text('{"origins":["https://graph.microsoft.com"],"allowNonAtomicFtpDestinations":true}')
    assert read_policy(file).allow_non_atomic_ftp_destinations
    file.write_text('{"origins":["https://*.example.com"]}')
    with pytest.raises(ValueError):
        read_policy(file)
