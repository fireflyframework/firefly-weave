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

"""What workers and the operations runner print about a call that failed: never text a server or the network chose."""

import pytest

from firefly_weave.contracts.public import Problem
from firefly_weave.sdk._retry import kind
from firefly_weave.sdk.errors import WeaveError


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("WV-X\n2026-11-16 INFO forged", id="newline"),
        pytest.param("WV-X\x1b[2Jforged", id="escape-sequence"),
        pytest.param("WV-" + "A" * 62, id="longer-than-64"),
        pytest.param("WV-" + "A" * 1_000_000, id="one-megabyte"),
    ],
)
def test_a_code_the_platform_does_not_mint_is_never_printed(code):
    # Logs and terminals show the code as it is, so a newline or an escape sequence in it could forge a line.
    assert kind(WeaveError(Problem(status=503, code=code, message="Remote request failed"))) == "WV-HTTP (HTTP 503)"


def unresolvable(request=None):
    """Raise what httpx raises when the API's host name does not resolve: a ConnectError raised from a gaierror."""
    import socket

    import httpx

    raise httpx.ConnectError("[Errno 8] weave-api.internal") from socket.gaierror(8, "weave-api.internal")


async def test_a_failure_is_named_by_its_type_and_the_innermost_error_it_came_from_never_its_text():
    """A refused connection, a name that does not resolve and a TLS failure all reach a loop as ConnectError; the
    innermost error's type tells them apart, without the address the message carries."""
    from uuid import uuid4

    import httpx

    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.deployments import RunnerClaimRequest
    from firefly_weave.sdk.client import WeaveClient

    with pytest.raises(httpx.ConnectError) as direct:
        unresolvable()
    async with WeaveClient(
        "https://weave.example",
        lambda: "token",
        Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4()),
        transport=httpx.MockTransport(unresolvable),
    ) as client:
        with pytest.raises(WeaveError) as through_the_client:
            await client.invoke("deployment_runners.claim", body=RunnerClaimRequest(runner_id=uuid4()))
    assert [kind(direct.value), kind(through_the_client.value)] == [
        "ConnectError (gaierror)",
        "WV-TRANSPORT (HTTP 503) from ConnectError (gaierror)",
    ]
