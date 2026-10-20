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


"""A runtime that does not run transactions never runs a transaction's steps unprotected."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.runtime.kernel import transition
from firefly_weave.runtime.models import RunState, RuntimeEvent, TaskIntent


def test_the_kernel_opens_an_incident_at_a_transaction_instead_of_running_its_steps():
    fixture = yaml.safe_load(Path("tests/fixtures/transactions/catalog.yaml").read_text())
    catalog = CatalogSnapshot.from_definitions(
        [load_definition(item) for item in fixture["definitions"]], tasks=fixture["tasks"]
    )
    text = Path("examples/language/move-between-systems.workflow.yaml").read_text()
    result = compile_source(parse_source(text, format="yaml").value, format="object", catalog=catalog)
    assert result.ok
    started = RuntimeEvent(
        id=uuid4(), type="started", timestamp=datetime(2026, 10, 20, tzinfo=UTC), sequence=1, data={}
    )
    outcome = transition(RunState(input={"moveId": "M-1", "quantity": 2}), started, result.artifact)
    assert outcome.state.status == "suspended"
    assert list(outcome.state.incidents.values())[0].code == "WV-RUNTIME-ARTIFACT"
    assert not any(isinstance(command, TaskIntent) for command in outcome.commands)
