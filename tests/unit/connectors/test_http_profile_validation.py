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

"""HTTP profile 2.0.0 explanations stay exactly as strict as the executor's own policy."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from firefly_weave.connectors.http import HttpPolicy
from firefly_weave.connectors.http_profiles import HttpProfileConnector
from firefly_weave.contracts.connectors import (
    ActionContext,
    ConnectionInvalid,
    ConnectionRequest,
    ConnectionRevision,
    ConnectorFailure,
    ConnectorInvocation,
)
from firefly_weave.contracts.http_profiles import (
    HTTP_PROFILE_DESCRIPTOR,
    check_profile_connection,
    validate_profile_connection,
)

# Pinned by every published weave-http@2.0.0 Connector and worker release binding.
PUBLISHED_DIGEST = "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8"


def test_callbacks_never_change_the_published_manifest_digest():
    assert HTTP_PROFILE_DESCRIPTOR.manifest.digest == PUBLISHED_DIGEST
    assert {binding.connector_digest for binding in HTTP_PROFILE_DESCRIPTOR.bindings} == {PUBLISHED_DIGEST}
    assert HTTP_PROFILE_DESCRIPTOR.validate_action_config is not None
    assert HTTP_PROFILE_DESCRIPTOR.manifest.value["spec"]["actions"]["read"]["configSchema"] == {"type": "object"}


def invocation_context(config):
    connection = ConnectionRevision(
        id=uuid4(),
        revision=1,
        name="profile",
        connector_version_id=uuid4(),
        connector="weave-http@2.0.0",
        connector_digest=PUBLISHED_DIGEST,
        adapter="weave-http-v2",
        config={"baseUrl": "https://api.example.test", "auth": {"kind": "none"}},
        allowed_destinations=("https://api.example.test",),
    )
    invocation = ConnectorInvocation(connection, config, "read", {"type": "object"}, {"type": "object"}, 1024, 1024)

    async def credentials(slot):
        raise AssertionError("Invalid configuration must not lease credentials")

    async def authorize():
        raise AssertionError("Invalid configuration must not reach authorization")

    return ActionContext("op", datetime.now(UTC) + timedelta(seconds=10), credentials, invocation, authorize)


class NoWire:
    async def request_bounded(self, *args, **kwargs):
        raise AssertionError("Invalid configuration must never dispatch")


@pytest.mark.parametrize(
    "config",
    [
        {"foo": 1},
        {"method": "GET", "path": "/items/{id}", "sideEffect": "read_only", "statuses": [200]},
        {"method": "POST", "path": "/items", "sideEffect": "read_only", "statuses": [200]},
    ],
)
async def test_runtime_reports_invalid_configuration_as_config_not_input(config):
    with pytest.raises(ConnectorFailure) as failure:
        await HttpProfileConnector(NoWire(), HttpPolicy(), None).execute({}, invocation_context(config))
    assert failure.value.code == "HTTP_PROFILE_CONFIG"
    assert failure.value.outcome == "not_started"


def request(config=None, slots=None, destinations=("https://api.example.test",)):
    return ConnectionRequest(
        name="pets",
        connector_version_id=uuid4(),
        config=config if config is not None else {"baseUrl": "https://api.example.test", "auth": {"kind": "none"}},
        secretRef=slots or {},
        allowed_destinations=destinations,
    )


def issues(value):
    with pytest.raises(ConnectionInvalid) as failure:
        validate_profile_connection(value)
    return {(issue.code, issue.path): issue.message for issue in failure.value.issues}


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            request({"baseUrl": "ftp://api.example.test", "auth": {"kind": "none"}}),
            {("CONFIG", "/config/baseUrl")},
        ),
        (
            request({"baseUrl": "https://api.example.test/v1", "auth": {"kind": "none"}}),
            {("CONFIG", "/config/baseUrl")},
        ),
        (
            request({"baseUrl": "https://api.example.test", "auth": {"kind": "api-key"}}, {"api_key": "h"}),
            {("AUTH", "/config/auth/header")},
        ),
        (
            request(
                {"baseUrl": "https://api.example.test", "auth": {"kind": "api-key", "header": "Authorization"}},
                {"api_key": "h"},
            ),
            {("AUTH", "/config/auth/header")},
        ),
        (
            request({"baseUrl": "https://api.example.test", "auth": {"kind": "oauth"}}),
            {("AUTH", "/config/auth/kind")},
        ),
        (
            request({"baseUrl": "https://api.example.test", "auth": {"kind": "bearer"}}),
            {("SECRET", "/secretRef/token")},
        ),
        (
            request({"baseUrl": "https://api.example.test", "auth": {"kind": "none"}}, {"token": "h"}),
            {("SECRET", "/secretRef/token")},
        ),
        (request(destinations=("https://other.example.test",)), {("DESTINATION", "/allowed_destinations")}),
        (
            request(destinations=("https://api.example.test", "ftp://plain.example.test")),
            {("DESTINATION", "/allowed_destinations/1")},
        ),
        (
            request(
                {
                    "baseUrl": "https://api.example.test",
                    "auth": {
                        "kind": "machine-token",
                        "client_id": "client",
                        "endpoint": "https://auth.example.test/token",
                    },
                },
                {"client_secret": "h"},
            ),
            {("DESTINATION", "/allowed_destinations")},
        ),
        (
            request(
                {"baseUrl": "https://api.example.test", "auth": {"kind": "machine-token", "client_id": "client"}},
                {"client_secret": "h"},
            ),
            {("AUTH", "/config/auth/endpoint")},
        ),
        (
            request({"baseUrl": "https://api.example.test", "auth": {"kind": "none"}, "proxy": "x"}),
            {("CONFIG", "/config/proxy")},
        ),
    ],
)
def test_connection_rejections_point_at_the_field_to_fix(value, expected):
    found = issues(value)
    assert set(found) == expected, found
    assert all(message and message != "h" for message in found.values())
    with pytest.raises(ValueError):
        check_profile_connection(value)


def test_destination_message_names_the_origin_to_allow():
    found = issues(request(destinations=("https://other.example.test",)))
    assert "https://api.example.test" in found[("DESTINATION", "/allowed_destinations")]


@pytest.mark.parametrize(
    "auth,slots",
    [
        ({"kind": "none"}, {}),
        ({"kind": "api-key", "header": "X-API-Key"}, {"api_key": "h"}),
        ({"kind": "basic"}, {"username": "u", "password": "p"}),
        ({"kind": "bearer"}, {"token": "t"}),
    ],
)
def test_valid_connections_still_pass_both_checks(auth, slots):
    value = request({"baseUrl": "https://api.example.test", "auth": auth}, slots)
    validate_profile_connection(value)
    check_profile_connection(value)
