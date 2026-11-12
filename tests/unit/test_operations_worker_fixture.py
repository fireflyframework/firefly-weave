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

"""The sleep fixture worker the local platform runs for Operations: bounded, unprivileged, database-free."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from firefly_weave.contracts.workers import ReleaseRequest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/operations-worker"


def fixture():
    spec = importlib.util.spec_from_file_location("operations_worker_fixture", FIXTURE / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest():
    return json.loads((FIXTURE / "manifest.json").read_text())


async def test_the_fixture_worker_returns_after_sleeping_the_requested_time(monkeypatch):
    module = fixture()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    assert await module.sleep(SimpleNamespace(input={"ms": 25})) == {"slept_ms": 25}
    assert slept == [0.025]


def test_the_fixture_manifest_bounds_the_sleep():
    (capability,) = manifest()["capabilities"]
    assert (capability["taskType"], capability["taskVersion"], capability["sideEffect"]) == (
        "example-sleep",
        "1.0.0",
        "read_only",
    )
    # The manifest is what the platform admits as the fixture worker's release.
    ReleaseRequest.model_validate_json(json.dumps({"image_digest": "sha256:" + "a" * 64, **manifest()}))
    assert manifest()["credential_capabilities"] == []
    schema = capability["inputSchema"]
    jsonschema.validate({"ms": 60000}, schema)
    for refused in ({"ms": 60001}, {"ms": -1}, {"ms": 5, "extra": True}, {}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(refused, schema)
    jsonschema.validate({"slept_ms": 1}, capability["outputSchema"])


@pytest.mark.parametrize("name", ["WEAVE_DATABASE_URL", "PGPASSWORD", "WEAVE_KC_ADMIN_PASSWORD"])
async def test_the_fixture_worker_refuses_database_settings(monkeypatch, name):
    module = fixture()
    monkeypatch.setenv(name, "anything")

    def no_network(*args, **kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr(module.httpx, "AsyncClient", no_network)
    with pytest.raises(RuntimeError, match="database or administrator"):
        await module.main()


def test_the_fixture_worker_prints_its_manifest(capsys):
    module = fixture()
    assert module.cli(["--release-manifest"]) == 0
    assert json.loads(capsys.readouterr().out) == manifest()


@pytest.mark.parametrize(("value", "expected"), [(None, 2), ("1", 1), ("8", 8)])
def test_the_fixture_worker_capacity_is_bounded(value, expected):
    environ = {} if value is None else {"WEAVE_WORKER_CAPACITY": value}
    assert fixture().capacity(environ) == expected
    for refused in ("0", "9", "two"):
        with pytest.raises(ValueError):
            fixture().capacity({"WEAVE_WORKER_CAPACITY": refused})


async def test_the_fixture_reports_an_unknown_outcome_only_when_faults_are_turned_on(monkeypatch):
    """O-J9 opens an incident through the fixture: {"fail": "unknown"} reports an unknown outcome, and only on a
    fixture service the journey started with WEAVE_FIXTURE_FAULTS=1; the platform's own workers ignore the field,
    so retrying the step on them succeeds."""
    from firefly_weave.contracts.connectors import ConnectorFailure

    module = fixture()
    lease = SimpleNamespace(input={"ms": 0, "fail": "unknown"})
    monkeypatch.delenv("WEAVE_FIXTURE_FAULTS", raising=False)
    assert await module.sleep(lease) == {"slept_ms": 0}
    monkeypatch.setenv("WEAVE_FIXTURE_FAULTS", "1")
    with pytest.raises(ConnectorFailure) as fault:
        await module.sleep(lease)
    assert (fault.value.code, fault.value.outcome) == ("FIXTURE_FAULT", "unknown")
    assert await module.sleep(SimpleNamespace(input={"ms": 0})) == {"slept_ms": 0}


def test_the_manifest_accepts_only_the_unknown_fault():
    (capability,) = manifest()["capabilities"]
    schema = capability["inputSchema"]
    jsonschema.validate({"ms": 0, "fail": "unknown"}, schema)
    jsonschema.validate({"ms": 10}, schema)
    for wrong in ({"ms": 0, "fail": "boom"}, {"fail": "unknown"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(wrong, schema)


async def test_registration_waits_out_a_busy_platform():
    """A platform out of capacity answers 429 to a registration: the fixture tries again instead of exiting, which
    made a restarted container exit and its change request end in reconciliation."""
    import httpx

    module = fixture()
    answers = [429, 429, 201]

    def handler(request):
        return httpx.Response(answers.pop(0), json={"id": "00000000-0000-4000-8000-000000000001"})

    async with httpx.AsyncClient(base_url="http://platform.test", transport=httpx.MockTransport(handler)) as client:
        response = await module.register(client, "/env", {"release_id": "r", "task_types": [], "capacity": 1}, pause=0)
    assert response.status_code == 201 and answers == []


def test_a_failure_names_its_kind_and_place_but_no_address(capsys, monkeypatch):
    import httpx

    module = fixture()

    async def failing():
        request = httpx.Request("POST", "http://secret-host.test/api?token=abc")
        raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(503, request=request))

    monkeypatch.setattr(module, "main", failing)
    assert module.cli([]) == 1
    printed = capsys.readouterr().err
    assert "HTTPStatusError" in printed and "503" in printed and "failing" in printed
    assert "secret-host" not in printed and "token" not in printed


async def test_registration_gives_up_at_once_on_any_other_refusal():
    import httpx

    module = fixture()
    answers = [503, 201]

    def handler(request):
        return httpx.Response(answers.pop(0), json={"id": "00000000-0000-4000-8000-000000000001"})

    async with httpx.AsyncClient(base_url="http://platform.test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await module.register(client, "/env", {"release_id": "r", "task_types": [], "capacity": 1}, pause=0)
    assert answers == [201]


def _jwt(claims):
    import base64

    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.c2lnbmF0dXJl"


def test_the_token_probe_signs_in_as_a_worker_and_prints_only_the_issuer(capsys, monkeypatch, tmp_path):
    """connect compares the issuer with the platform's; the token, its other claims and the secret stay in the pod."""
    module = fixture()
    token = _jwt({"iss": "http://localhost:8080/realms/weave", "azp": "weave-cluster-worker-1", "sub": "canary-sub"})
    asked = []

    class Provider:
        @classmethod
        def from_file(cls, path, api):
            asked.append((path, api))
            return cls()

        async def get_access_token(self, target):
            asked.append(target)
            return token

    monkeypatch.setattr(module, "ClientCredentialsTokenProvider", Provider)
    monkeypatch.setenv("WEAVE_API_URL", "http://weave-api.weave-system.svc:8000")
    monkeypatch.setenv("WEAVE_WORKER_OAUTH_CONFIG_FILE", str(tmp_path / "oauth.json"))
    monkeypatch.delenv("WEAVE_TOKEN_RELAY", raising=False)
    assert module.cli(["--probe-token"]) == 0
    printed = capsys.readouterr()
    assert printed.out == '{"iss": "http://localhost:8080/realms/weave"}\n' and printed.err == ""
    assert asked == [
        (tmp_path / "oauth.json", "http://weave-api.weave-system.svc:8000"),
        "http://weave-api.weave-system.svc:8000",
    ]


def test_the_closed_probe_fails_when_an_address_answers():
    import socket

    module = fixture()
    with socket.socket() as listening, socket.socket() as released:
        listening.bind(("127.0.0.1", 0))
        listening.listen()
        released.bind(("127.0.0.1", 0))
        closed = f"127.0.0.1:{released.getsockname()[1]}"
        released.close()
        open_ = f"127.0.0.1:{listening.getsockname()[1]}"
        assert module.cli(["--probe-closed", closed]) == 0
        assert module.cli(["--probe-closed", closed, open_]) == 1


@pytest.mark.parametrize("address", ["weave-api.weave-system.svc:8000", "127.0.0.1", "127.0.0.1:0", "[::1]:x"])
def test_the_closed_probe_accepts_only_ip_addresses_and_ports(address):
    """A name that doesn't resolve would look closed, so only literal addresses count."""
    assert fixture().cli(["--probe-closed", address]) == 2
