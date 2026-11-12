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

"""The SDK client uses plain HTTP outside loopback only for an approved private origin and its pinned transport."""

import httpx
import pytest

from firefly_weave import private_origins as po
from firefly_weave.sdk.client import WeaveClient

ORIGIN = "http://weave-api.weave-system.svc:8000"
TRANSPORT = httpx.MockTransport(lambda request: httpx.Response(204))


def policy(*entries):
    return po.PrivateOrigins(platform=po.PLATFORM).with_entries(list(entries))


def entry(origin=ORIGIN, purpose="runner"):
    return po.PrivateOrigin(origin=origin, purpose=purpose, networks=("10.89.0.0/24",), credentials="bridge")


def refused(**options):
    with pytest.raises(ValueError, match="TLS is required outside loopback development"):
        WeaveClient(ORIGIN, lambda: "token", None, **options)


def test_plain_http_outside_loopback_is_refused_without_a_private_origin_purpose():
    with po.installed(policy(entry())):
        refused(transport=TRANSPORT)


def test_plain_http_outside_loopback_is_refused_without_a_pinned_transport():
    with po.installed(policy(entry())):
        refused(private_origin="runner")


def test_plain_http_outside_loopback_needs_an_entry_for_its_own_purpose():
    with po.installed(policy(entry(purpose="platform-api"))):
        refused(transport=TRANSPORT, private_origin="runner")


def test_a_runner_entry_for_another_host_does_not_open_this_one():
    with po.installed(policy(entry(origin="http://other.weave-system.svc:8000"))):
        refused(transport=TRANSPORT, private_origin="runner")


async def test_an_approved_runner_origin_with_its_transport_is_accepted():
    with po.installed(policy(entry())):
        async with WeaveClient(ORIGIN, lambda: "token", None, transport=TRANSPORT, private_origin="runner") as client:
            assert client.base_url == ORIGIN
