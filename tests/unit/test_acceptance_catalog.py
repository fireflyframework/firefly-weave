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

"""The catalog J0 publishes authors offline and matches the Acme test service and the risk-tier rules."""

import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def load():
    spec = importlib.util.spec_from_file_location("acceptance_catalog", ROOT / "tests/acceptance/acceptance_catalog.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


catalog = load()


def test_the_five_acme_actions_author_offline_on_the_http_connector():
    documents = catalog.actions()
    assert (
        tuple(documents)
        == catalog.ACTION_NAMES
        == (
            "acme-get-customer",
            "acme-list-orders",
            "acme-order-status",
            "acme-post-approval",
            "acme-post-notification",
        )
    )
    for name, document in documents.items():
        assert document["kind"] == "Action" and document["metadata"] == {"name": name, "version": "1.0.0"}
        assert document["spec"]["implementation"]["uses"] == "weave-http@2.0.0"
    paths = {name: documents[name]["spec"]["implementation"]["config"]["path"] for name in documents}
    assert paths == {
        "acme-get-customer": "/customers/{id}",
        "acme-list-orders": "/customers/{id}/orders",
        "acme-order-status": "/orders/{id}/status",
        "acme-post-approval": "/approvals",
        "acme-post-notification": "/notifications",
    }


def test_the_action_paths_are_the_test_service_operations():
    served = set(catalog.acme().openapi()["paths"])
    assert {request["pathTemplate"] for request in catalog.action_requests()} == served


@pytest.mark.parametrize(("order", "tier"), [("O-1", "auto"), ("O-2", "review"), ("O-3", "reject")])
def test_the_risk_tier_table_compiles_and_decides_each_fixture_order(order, tier):
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.compiler.decision_tables import evaluate_decision_table

    source = catalog.decision_table()
    compiled = compile_source(source, format="yaml", catalog=CatalogSnapshot.empty(), filename="risk-tier.yaml")
    assert compiled.ok and [d.code for d in compiled.diagnostics] == []
    table = yaml.safe_load(source)
    assert table["metadata"] == {"name": "risk-tier", "version": "1.0.0"} and len(table["spec"]["rules"]) == 3
    item = next(item for item in catalog.acme().DATA["orders"]["C-1001"] if item["id"] == order)
    assert evaluate_decision_table(table["spec"], item).output == {"tier": tier}


def test_the_child_workflow_is_callable_and_compiles_without_pins():
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot

    source = catalog.child_workflow()
    compiled = compile_source(source, format="yaml", catalog=CatalogSnapshot.empty(), filename="notify-customer.yaml")
    assert compiled.ok and [d.code for d in compiled.diagnostics] == []
    document = yaml.safe_load(source)
    assert document["metadata"] == {"name": catalog.CHILD_WORKFLOW, "version": "1.0.0"}
    assert document["spec"]["callable"] == {}
    # Nothing to pin at activation: no step uses a connection, a connector, a worker or another workflow.
    assert {step["kind"] for step in document["spec"]["steps"]} == {"transform"}


def test_connection_requests_reference_the_secret_handle_never_a_value():
    version = uuid4()
    request = catalog.connection_request(version)
    assert request.name == "acme-local" and request.connector_version_id == version
    assert request.config["baseUrl"] == "http://acme.acceptance.test:8080"
    assert request.secret_refs == {"api_key": "acme-api-key"}
    open_request = catalog.open_connection_request("metadata-j9", "http://169.254.169.254", version)
    assert open_request.secret_refs == {} and open_request.config["baseUrl"] == "http://169.254.169.254"


def test_the_reader_workflow_calls_the_acme_customer_action_through_one_connection_slot():
    from firefly_weave.compiler.api import compile_source
    from firefly_weave.compiler.catalog import CatalogSnapshot
    from firefly_weave.contracts.definitions import ConnectorDefinition, load_definition
    from firefly_weave.contracts.http_profiles import HTTP_PROFILE_DESCRIPTOR
    from firefly_weave.sdk.http_actions import HTTP_ADAPTER

    snapshot = CatalogSnapshot.from_definitions(
        [
            ConnectorDefinition.model_validate(HTTP_PROFILE_DESCRIPTOR.manifest.value),
            *(load_definition(document) for document in catalog.actions().values()),
        ],
        tasks=list(HTTP_PROFILE_DESCRIPTOR.capabilities),
        adapters=[HTTP_ADAPTER],
    )
    source = catalog.reader_workflow()
    compiled = compile_source(source, format="yaml", catalog=snapshot, filename="acme-reader.yaml")
    assert compiled.ok, [d.code for d in compiled.diagnostics if d.severity == "error"]
    document = yaml.safe_load(source)
    assert document["metadata"] == {"name": catalog.READER_WORKFLOW, "version": "1.0.0"}
    assert document["spec"]["connections"] == {catalog.READER_SLOT: {"connector": "weave-http@2.0.0"}}
