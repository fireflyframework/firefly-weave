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

"""The Acme API fixture serves the data, key check, faults and journal the acceptance journeys rely on."""

import hashlib
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
KEY = "wv-canary-acme-key"


def load_fixture():
    spec = importlib.util.spec_from_file_location(
        "acme_api_fixture", ROOT / "tests/acceptance/fixtures/acme_api/server.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


acme = load_fixture()


@pytest.fixture
def servers():
    api, control, _ = acme.make_servers(
        host="127.0.0.1", api_port=0, control_port=0, key_sha256=hashlib.sha256(KEY.encode()).hexdigest()
    )
    for server in (api, control):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{api.server_address[1]}", f"http://127.0.0.1:{control.server_address[1]}"
    for server in (api, control):
        server.shutdown()
        server.server_close()


def call(url, method="GET", body=None, headers=None):
    data = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else None


def test_customers_need_the_api_key_and_the_journal_keeps_only_its_hash(servers):
    api, control = servers
    assert call(api + "/customers/C-1001")[0] == 401
    assert call(api + "/customers/C-1001", headers={"X-Api-Key": "wrong"})[0] == 401
    status, customer = call(api + "/customers/C-1001", headers={"X-Api-Key": KEY})
    assert status == 200 and customer["id"] == "C-1001" and customer["country"] == "ES"
    assert call(api + "/customers/C-9999", headers={"X-Api-Key": KEY})[0] == 404
    entries = call(control + "/_control/journal")[1]["entries"]
    assert set(entries[0]) == {"method", "path", "time", "key_sha256", "status", "body_sha256"}
    assert entries[0]["key_sha256"] is None
    assert entries[2]["key_sha256"] == hashlib.sha256(KEY.encode()).hexdigest()
    assert KEY not in json.dumps(entries)


def test_orders_and_statuses_match_the_spec(servers):
    api, _ = servers
    orders = call(api + "/customers/C-1001/orders", headers={"X-Api-Key": KEY})[1]
    assert [(o["id"], o["amount"], o["currency"], o["country"]) for o in orders["items"]] == [
        ("O-1", 120.0, "EUR", "ES"),
        ("O-2", 4800.0, "EUR", "ES"),
        ("O-3", 75.0, "EUR", "ZZ"),
    ]
    assert [call(api + f"/orders/{order}/status")[1]["status"] for order in ("O-1", "O-2", "O-3")] == [
        "shipped",
        "on_hold",
        "cancelled",
    ]
    assert call(api + "/orders/O-9/status")[0] == 404


def test_the_openapi_document_has_five_operations_on_the_fixture_origin(servers):
    api, _ = servers
    status, document = call(api + "/openapi.json")
    assert status == 200 and document["openapi"] == "3.1.0"
    assert document["servers"] == [{"url": "http://acme.acceptance.test:8080"}]
    operations = sorted(op["operationId"] for path in document["paths"].values() for op in path.values())
    assert operations == ["createApproval", "createNotification", "getCustomer", "getOrderStatus", "listCustomerOrders"]


def test_faults_answer_the_next_requests_and_reset_clears_everything(servers):
    api, control = servers
    fault = {"status": 503, "count": 2, "path": "/customers/C-1002"}
    assert call(control + "/_control/faults", "POST", fault)[0] == 204
    statuses = [call(api + "/customers/C-1002", headers={"X-Api-Key": KEY})[0] for _ in range(3)]
    assert statuses == [503, 503, 200]
    assert call(control + "/_control/faults", "POST", {"status": 200, "count": 1, "path": "/x"})[0] == 400
    assert call(control + "/_control/faults", "POST", {"status": 503, "count": 1})[0] == 400
    assert call(control + "/_control/reset", "POST", {})[0] == 204
    assert call(control + "/_control/journal")[1] == {"entries": []}


def test_side_effects_are_counted_and_bodies_bounded(servers):
    api, control = servers
    approval = {"customerId": "C-1001", "lines": ["O-2"]}
    assert call(api + "/approvals", "POST", approval) == (201, {"id": "A-1", "status": "received"})
    notification = {"customerId": "C-1001", "message": "Order O-2 needs review"}
    assert call(api + "/notifications", "POST", notification) == (201, {"id": "N-2", "status": "received"})
    assert call(api + "/approvals", "POST", b"[1]")[0] == 400
    assert call(api + "/sink/alerts", "POST", {"alert": "firing"})[0] == 204
    assert call(api + "/sink/alerts", "POST", b"x" * (acme.MAX_BODY + 1))[0] == 413
    assert call(api + "/customers/C-1001", "PUT", {})[0] == 405
    entries = call(control + "/_control/journal")[1]["entries"]
    assert [entry["path"] for entry in entries[:2]] == ["/approvals", "/notifications"]
    assert entries[0]["body_sha256"] == hashlib.sha256(json.dumps(approval).encode()).hexdigest()


def test_compose_keeps_acme_on_the_egress_network_with_a_loopback_control_port():
    document = yaml.safe_load((ROOT / "tests/acceptance/compose.fixtures.yaml").read_text(encoding="utf-8"))
    service = document["services"]["acme-api"]
    assert list(service["networks"]) == ["egress"]
    assert service["networks"]["egress"]["aliases"] == ["acme.acceptance.test"]
    assert all(port.startswith("127.0.0.1:") for port in service["ports"])
    assert service["read_only"] and service["cap_drop"] == ["ALL"] and service["pull_policy"] == "never"
    assert document["networks"]["egress"]["external"] is True
