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

"""weave-http@2 reaches plain-HTTP origins exactly like weave-http@1, and every such connection is flagged."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from firefly_weave import private_origins as po
from firefly_weave.connections.secrets import ResolvedSecret
from firefly_weave.connectors.egress import EgressDenied
from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.connectors.http_profiles import HttpProfileConnector
from firefly_weave.contracts.connectors import (
    ActionContext,
    ConnectionRevision,
    ConnectionTestResult,
    ConnectorInvocation,
    plain_text_warning,
    transport_encrypted,
)

PUBLIC = "http://api.example.test"
ACME = "http://acme.acceptance.test:8080"


def acme_entry():
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [po.PrivateOrigin(origin=ACME, purpose="http-connector", networks=("10.231.1.0/24",), credentials="bridge")]
    )


class Wire:
    def __init__(self):
        self.calls = []

    async def request_bounded(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs["egress_policy"]))
        return httpx.Response(200, json={"orderId": "O-1", "status": "shipped"})


async def profile_egress(policy, origin):
    """Run one weave-http@2 read with an API key over plain HTTP; return the egress policy it used."""

    async def credentials(slot):
        return ResolvedSecret(value="acme-api-key-value")

    async def authorize():
        return None

    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="acme",
        connector_version_id=uuid4(),
        connector="weave-http@2.0.0",
        connector_digest="a" * 64,
        adapter="weave-http-v2",
        config={"baseUrl": origin, "auth": {"kind": "api-key", "header": "X-Api-Key"}},
        secretRef={"api_key": "acme-api-key"},
        allowed_destinations=(origin,),
    )
    config = {
        "profileVersion": "2.0.0",
        "method": "GET",
        "path": "/orders/{id}/status",
        "sideEffect": "read_only",
        "statuses": [200],
        "parameters": [{"name": "id", "location": "path", "type": "string", "required": True}],
    }
    invocation = ConnectorInvocation(
        connection, config, "read", {"type": "object"}, {"type": "object"}, 1048576, 1048576
    )
    context = ActionContext("op", datetime.now(UTC) + timedelta(seconds=10), credentials, invocation, authorize)
    wire = Wire()
    result = await HttpProfileConnector(wire, policy, None).execute({"path": {"id": "O-1"}}, context)
    assert result == {"status": 200, "body": {"orderId": "O-1", "status": "shipped"}}
    method, url, egress = wire.calls[0]
    assert (method, url) == ("GET", origin + "/orders/O-1/status")
    assert egress.purpose == "http-connector" and egress.sends_credentials is True
    return egress


async def test_public_plain_http_needs_no_entry_and_may_carry_credentials():
    url = PUBLIC + "/orders/O-1/status"
    for policy in (HttpPolicy(), HttpPolicy(("10.0.0.0/8",)), HttpPolicy(origins=acme_entry())):
        egress = await profile_egress(policy, PUBLIC)
        egress.validate(url, ("93.184.216.34",))
        # The connection's origin allowlist still applies to public addresses.
        with pytest.raises(EgressDenied):
            egress.validate("http://other.example.test/orders/O-1/status", ("93.184.216.34",))


async def test_private_and_cgnat_plain_http_need_an_entry():
    url = ACME + "/orders/O-1/status"
    unapproved = await profile_egress(HttpPolicy(), ACME)
    for addresses in (("10.231.1.37",), ("127.0.0.1",), ("100.100.100.7",)):
        with pytest.raises(EgressDenied):
            unapproved.validate(url, addresses)
    approved = await profile_egress(HttpPolicy(origins=acme_entry()), ACME)
    approved.validate(url, ("10.231.1.37",))
    cgnat = await profile_egress(HttpPolicy(("100.64.0.0/10",)), ACME)
    cgnat.validate(url, ("100.100.100.7",))
    # Always-refused addresses stay refused, entry or not.
    for egress, addresses in ((approved, ("169.254.169.254",)), (cgnat, ("100.100.100.200",))):
        with pytest.raises(EgressDenied):
            egress.validate(url, addresses)


def test_plain_http_connections_are_flagged_not_encrypted():
    assert transport_encrypted({"baseUrl": PUBLIC, "auth": "none"}) is False
    assert transport_encrypted({"baseUrl": "HTTPS://api.example.test"}) is True
    assert transport_encrypted({"host": "db.example.test"}) is None
    assert transport_encrypted({"baseUrl": "http://[::1"}) is None
    assert plain_text_warning({"baseUrl": ACME + "/"}) == (
        "Not encrypted: requests to http://acme.acceptance.test:8080 travel in plain text."
    )
    assert plain_text_warning({"baseUrl": "https://api.example.test"}) is None
    # The warning names the host and port only, never user information.
    assert plain_text_warning({"baseUrl": "http://user:secret@api.example.test"}) == (
        "Not encrypted: requests to http://api.example.test travel in plain text."
    )


def test_connection_test_results_carry_the_flag_only_when_known():
    assert "encrypted" not in ConnectionTestResult(ok=True).model_dump(mode="json")
    flagged = ConnectionTestResult(ok=True, encrypted=False)
    assert flagged.model_dump(mode="json")["encrypted"] is False
    assert ConnectionTestResult.model_validate_json(flagged.model_dump_json()) == flagged
