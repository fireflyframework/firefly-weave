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

"""HTTP profile connections accept plain HTTP like HTTPS; the private-origin policy decides reach at egress."""

import json
from uuid import uuid4

import pytest

from firefly_weave import private_origins as po
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.http_profile_checks import connection_issues
from firefly_weave.contracts.http_profiles import ProfileConnection, check_profile_connection, fixed_server

ACME = "http://acme.acceptance.test:8080"
PUBLIC = "http://api.example.com"


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


POLICIES = {
    "none": po.PrivateOrigins.empty(),
    "approved": approved(),
    "webhooks-only": approved("event-delivery"),
    "legacy": po.PrivateOrigins.empty().with_legacy(
        ("http-connector",), ("10.0.0.0/8",), setting="WEAVE_HTTP_PRIVATE_NETWORKS"
    ),
}


def test_fixed_server_reads_plain_http_only_when_asked():
    assert fixed_server(ACME + "/", plain_http=True) == (ACME, "")
    assert fixed_server("https://api.example.com") == ("https://api.example.com", "")
    with pytest.raises(ValueError):
        fixed_server(ACME)


@pytest.mark.parametrize("policy", list(POLICIES.values()), ids=list(POLICIES))
@pytest.mark.parametrize("base", [ACME, PUBLIC])
def test_plain_http_connections_pass_the_checks_under_every_policy(policy, base):
    # Connection checks never resolve names: the egress check decides reach before the first write (C8).
    with po.installed(policy):
        assert connection_issues(request(base, (base,))) == []
        check_profile_connection(request(base, (base,)))


def test_secrets_may_travel_over_plain_http():
    value = request(PUBLIC, (PUBLIC,), {"kind": "api-key", "header": "X-Api-Key"}, {"api_key": "acme-api-key"})
    assert connection_issues(value) == []
    check_profile_connection(value)


def test_machine_token_endpoints_still_need_https():
    auth = {"kind": "machine-token", "client_id": "weave", "endpoint": ACME + "/token"}
    with po.installed(approved()):
        issues = connection_issues(request(auth=auth, secrets={"client_secret": "acme-client"}))
    assert ("AUTH", "/config/auth/endpoint") in [(issue.code, issue.path) for issue in issues]


def test_other_schemes_are_refused_with_a_reason():
    ftp = "ftp://acme.acceptance.test"
    issues = connection_issues(request(ftp, (ftp,)))
    assert [(issue.code, issue.path) for issue in issues] == [
        ("CONFIG", "/config/baseUrl"),
        ("DESTINATION", "/allowed_destinations/0"),
    ]
    assert all("HTTPS or HTTP" in issue.message for issue in issues)
    with pytest.raises(ValueError):
        check_profile_connection(request(ftp, (ftp,)))


def test_execution_reads_the_connection_and_leaves_reachability_to_egress():
    connection = ProfileConnection.model_validate({"baseUrl": ACME, "auth": {"kind": "none"}})
    assert connection.base_url == ACME
