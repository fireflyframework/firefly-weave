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

"""Pure file catalogs compile each protocol operation through the shared compiler."""

from uuid import uuid4

import pytest

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot, FrozenDocument
from firefly_weave.contracts.connectors import ConnectionInvalid, ConnectionRequest
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.file_connectors import FILE_DESCRIPTORS, OPERATIONS, action_definition, task_capability


def test_every_file_protocol_has_compilable_worker_actions():
    assert len(FILE_DESCRIPTORS) == 5
    for name, descriptor in FILE_DESCRIPTORS.items():
        connector = load_definition(descriptor.manifest.value)
        assert connector.kind == "Connector"
        assert FrozenDocument.from_value(connector.model_dump(by_alias=True)).digest == descriptor.manifest.digest
        for operation in OPERATIONS:
            action = load_definition(action_definition(name, operation))
            capability = task_capability(name, operation)
            assert action.spec.implementation.task_type == capability.task_type
            assert action.spec.connection.connector == name + "@1.0.0"
            assert action.spec.retry.max_attempts == 1
            result = compile_source(
                action_definition(name, operation),
                format="object",
                catalog=CatalogSnapshot.from_definitions([connector], tasks=[capability], adapters=[name]),
            )
            assert result.ok, result.to_bytes()


def test_file_connection_requires_a_scoped_root_and_exact_destination():
    descriptor = FILE_DESCRIPTORS["weave-sftp"]
    request = ConnectionRequest(
        name="files",
        connector_version_id=uuid4(),
        config={
            "host": "files.example.test",
            "port": 22,
            "rootPath": "/",
            "serverRootIsolated": True,
            "username": "worker",
            "hostKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample",
            "operations": ["read"],
        },
        secretRef={"password": "sftp-password"},
        allowed_destinations=("sftp://files.example.test:22",),
    )
    descriptor.validate_connection(request)
    with pytest.raises(ConnectionInvalid):
        descriptor.validate_connection(request.model_copy(update={"allowed_destinations": ()}))
    with pytest.raises(ConnectionInvalid):
        descriptor.validate_connection(request.model_copy(update={"config": {**request.config, "rootPath": "/a/../b"}}))
    for change in ({"rootPath": "/incoming"}, {"serverRootIsolated": False}):
        with pytest.raises(ConnectionInvalid):
            descriptor.validate_connection(request.model_copy(update={"config": {**request.config, **change}}))


@pytest.mark.parametrize("name", ["weave-ftp", "weave-ftps"])
def test_ftp_destination_mutations_require_explicit_non_atomic_policy(name):
    descriptor = FILE_DESCRIPTORS[name]
    config = {
        "host": "files.example.test",
        "port": 21,
        "rootPath": "/",
        "serverRootIsolated": True,
        "username": "worker",
        "operations": ["read"],
    }
    request = ConnectionRequest(
        name="files",
        connector_version_id=uuid4(),
        config=config,
        secretRef={"password": "ftp-password"},
        allowed_destinations=(f"{name.removeprefix('weave-')}://files.example.test:21",),
    )
    descriptor.validate_connection(request)
    for operation in ("write", "move"):
        changed = {**config, "operations": [operation]}
        with pytest.raises(ConnectionInvalid):
            descriptor.validate_connection(request.model_copy(update={"config": changed}))
        descriptor.validate_connection(
            request.model_copy(
                update={
                    "config": {**changed, "destinationPolicy": "allowNonAtomic"},
                }
            )
        )
