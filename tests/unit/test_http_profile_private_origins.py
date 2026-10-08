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

"""HTTP profile connections accept plain HTTP only for an origin the private-origin policy approves."""

import json
from uuid import uuid4

import pytest

from firefly_weave import private_origins as po
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.http_profile_checks import connection_issues
from firefly_weave.contracts.http_profiles import ProfileConnection, check_profile_connection, fixed_server

ACME = "http://acme.acceptance.test:8080"


def request(base=ACME, destinations=(ACME,), auth=None, secrets=None):
    # The platform reads connection requests from JSON, as the API does.
    body = {
        "name": "acme",
        "connector_version_id": str(uuid4()),
        "config": {"baseUrl": base, "auth": auth or {"kind": "none"}},
        "secretRef": secrets or {},
        "allowed_destinations": list(destinations),
    }
    return ConnectionRequest.model_validate_json(json.dumps(body))


def approved(purpose="http-connector"):
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [po.PrivateOrigin(origin=ACME, purpose=purpose, networks=("10.231.1.0/24",), credentials="bridge")]
    )


def test_fixed_server_reads_plain_http_only_when_asked():
    assert fixed_server(ACME + "/", plain_http=True) == (ACME, "")
    assert fixed_server("https://api.example.com") == ("https://api.example.com", "")
    with pytest.raises(ValueError):
        fixed_server(ACME)


def test_without_an_entry_plain_http_is_refused_with_a_reason():
    with po.installed(po.PrivateOrigins.empty()):
        issues = connection_issues(request())
        assert [(issue.code, issue.path) for issue in issues] == [
            ("CONFIG", "/config/baseUrl"),
            ("DESTINATION", "/allowed_destinations/0"),
        ]
        assert "approved" in issues[0].message
        with pytest.raises(ValueError):
            check_profile_connection(request())


def test_an_approved_origin_is_accepted_for_the_http_connector_purpose_only():
    with po.installed(approved()):
        assert connection_issues(request()) == []
        check_profile_connection(request())
    with po.installed(approved("event-delivery")):
        assert connection_issues(request())


def test_legacy_private_networks_never_open_plain_http_for_profiles():
    legacy = po.PrivateOrigins.empty().with_legacy(
        ("http-connector",), ("10.0.0.0/8",), setting="WEAVE_HTTP_PRIVATE_NETWORKS"
    )
    with po.installed(legacy):
        assert connection_issues(request())


def test_machine_token_endpoints_still_need_https():
    auth = {"kind": "machine-token", "client_id": "weave", "endpoint": ACME + "/token"}
    with po.installed(approved()):
        assert connection_issues(request(auth=auth, secrets={"client_secret": "acme-client"}))


def test_execution_reads_the_connection_and_leaves_reachability_to_egress():
    connection = ProfileConnection.model_validate({"baseUrl": ACME, "auth": {"kind": "none"}})
    assert connection.base_url == ACME
