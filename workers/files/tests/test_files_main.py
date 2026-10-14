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

"""Worker boot policy and catalog exports use the public platform contracts."""

import json

import pytest
from firefly_weave.compiler.catalog import CatalogSnapshot

from weave_files_worker.main import read_policy, run


def test_catalog_exports_thirty_actions_and_five_connections(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["weave-files-worker", "--catalog"])
    run()
    catalog = CatalogSnapshot.from_lock(json.loads(capsys.readouterr().out))
    assert catalog.resolve("Action", "weave-sftp-download@1.0.0") is not None
    assert catalog.resolve("Action", "weave-microsoft-drive-write@1.0.0") is not None
    assert sum(resource.kind in {"Action", "Connector"} for resource in catalog.resources.values()) == 35


def test_worker_policy_requires_specific_origins(tmp_path):
    file = tmp_path / "policy.json"
    file.write_text('{"origins":["https://graph.microsoft.com"],"downloadOrigins":["https://tenant.sharepoint.com"]}')
    assert read_policy(file).download_url("https://tenant.sharepoint.com/file?key=signed")
    assert not read_policy(file).allow_non_atomic_ftp_destinations
    file.write_text('{"origins":["https://graph.microsoft.com"],"allowNonAtomicFtpDestinations":true}')
    assert read_policy(file).allow_non_atomic_ftp_destinations
    file.write_text('{"origins":["https://*.example.com"]}')
    with pytest.raises(ValueError):
        read_policy(file)


IDENTITY_SETTINGS = (
    "WEAVE_WORKER_REPORT_HOST",
    "WEAVE_WORKER_LABELS",
    "WEAVE_POD_NAMESPACE",
    "WEAVE_NODE_NAME",
    "KUBERNETES_SERVICE_HOST",
)
VALIDATION = {"status": 422, "code": "WV-VALIDATION", "message": "Invalid request contract"}


def registration_environment(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text('{"origins":["https://graph.microsoft.com"]}')
    token = tmp_path / "token"
    token.write_text("worker-token")
    for key, value in {
        "WEAVE_FILES_POLICY_FILE": str(policy),
        "WEAVE_WORKER_TOKEN_FILE": str(token),
        "WEAVE_API_URL": "https://weave.example",
        "WEAVE_ENVIRONMENT_URL": "/environment",
        "WEAVE_WORKER_RELEASE_ID": "release-1",
    }.items():
        monkeypatch.setenv(key, value)
    for name in IDENTITY_SETTINGS:
        monkeypatch.delenv(name, raising=False)


def files_registration(monkeypatch, responses):
    import httpx

    import weave_files_worker.main as main
    import weave_files_worker.providers  # noqa: F401  Imported before AsyncClient is replaced below.

    requests, configurations = [], []

    async def receive(request):
        requests.append(request)
        result = responses[len(requests) - 1]
        if isinstance(result, BaseException):
            raise result
        return result

    original = httpx.AsyncClient

    def client(**kwargs):
        configurations.append(kwargs)
        return original(**kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(main.httpx, "AsyncClient", client)
    return main, requests, configurations


async def test_main_retries_identity_validation_once_on_the_existing_client(tmp_path, monkeypatch):
    import httpx

    registration_environment(tmp_path, monkeypatch)
    main, requests, configurations = files_registration(
        monkeypatch, [httpx.Response(422, json={"code": "WV-VALIDATION"}), httpx.Response(503)]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await main.main()
    assert len(requests) == 2 and len(configurations) == 1
    assert configurations[0]["timeout"] == 15 and configurations[0]["trust_env"] is False
    assert configurations[0]["follow_redirects"] is False
    bodies = [json.loads(request.content) for request in requests]
    assert bodies[0]["identity"] and "identity" not in bodies[1]
    assert {k: v for k, v in bodies[0].items() if k != "identity"} == bodies[1]
    assert bodies[1]["capacity"] == 1
    assert [request.headers["authorization"] for request in requests] == ["Bearer worker-token"] * 2
    assert [str(request.url) for request in requests] == ["https://weave.example/environment/workers"] * 2


@pytest.mark.parametrize("first", ["accepted", "old server"])
async def test_main_serves_with_the_registered_id_after_at_most_one_fallback(tmp_path, monkeypatch, first):
    import asyncio
    from uuid import uuid4

    import httpx
    from firefly_weave.sdk.transport import WorkerTransport

    registration_environment(tmp_path, monkeypatch)
    worker_id = uuid4()
    accepted = httpx.Response(201, json={"id": str(worker_id)})
    responses = [accepted] if first == "accepted" else [httpx.Response(422, json=VALIDATION), accepted]
    main, requests, configurations = files_registration(monkeypatch, responses)
    served = []

    async def run(self):
        served.append(self)

    monkeypatch.setattr(main.Worker, "run", run)
    monkeypatch.setattr(type(asyncio.get_running_loop()), "add_signal_handler", lambda *args: None)
    await main.main()
    assert len(requests) == len(responses) and len(configurations) == 1
    assert "identity" in json.loads(requests[0].content)
    assert len(served) == 1 and type(served[0].client) is WorkerTransport
    assert served[0].client.worker_id == worker_id and served[0].concurrency == 1


@pytest.mark.parametrize(
    "status,body,network",
    [
        (401, {"status": 401, "code": "WV-UNAUTHENTICATED"}, None),
        (403, {"status": 403, "code": "WV-FORBIDDEN"}, None),
        (409, {"status": 409, "code": "WV-CONFLICT"}, None),
        (500, {"status": 500, "code": "WV-INTERNAL"}, None),
        (422, {"status": 422, "code": "WV-PAGE"}, None),
        (422, None, None),
        (None, None, "connect"),
        (None, None, "cancelled"),
    ],
)
async def test_main_registers_once_on_any_other_failure(tmp_path, monkeypatch, status, body, network):
    import asyncio

    import httpx

    registration_environment(tmp_path, monkeypatch)
    if network == "connect":
        first, raised = httpx.ConnectError("refused"), httpx.ConnectError
    elif network == "cancelled":
        first, raised = asyncio.CancelledError(), asyncio.CancelledError
    else:
        content = {"json": body} if body is not None else {"content": b"not json"}
        first, raised = httpx.Response(status, **content), httpx.HTTPStatusError
    main, requests, _ = files_registration(monkeypatch, [first, httpx.Response(201, json={"id": "unused"})])
    with pytest.raises(raised):
        await main.main()
    assert len(requests) == 1


async def test_main_stops_after_a_second_identity_refusal(tmp_path, monkeypatch):
    import httpx

    registration_environment(tmp_path, monkeypatch)
    main, requests, _ = files_registration(
        monkeypatch, [httpx.Response(422, json=VALIDATION), httpx.Response(422, json=VALIDATION)]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await main.main()
    assert len(requests) == 2


async def test_main_reports_its_bounded_identity(tmp_path, monkeypatch):
    import httpx

    registration_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("WEAVE_POD_NAMESPACE", "weave-files")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    main, requests, _ = files_registration(monkeypatch, [httpx.Response(503)])
    with pytest.raises(httpx.HTTPStatusError):
        await main.main()
    identity = json.loads(requests[0].content)["identity"]
    assert identity["namespace"] == "weave-files" and identity["platform_hint"] == "kubernetes"
    assert identity["runtime"] == "container"
