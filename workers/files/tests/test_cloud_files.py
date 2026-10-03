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

"""Cloud request contracts enforce ancestry, bounded responses and credential isolation."""

import httpx
import pytest
from firefly_weave.contracts.connectors import ConnectorFailure

from weave_files_worker.cloud import CloudDrive
from weave_files_worker.policy import WorkerPolicy


async def test_graph_download_redirect_never_receives_bearer_credentials():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "downloads.example":
            return httpx.Response(200, content=b"hello")
        if request.url.path.endswith("/content"):
            return httpx.Response(302, headers={"location": "https://downloads.example/signed?token=opaque"})
        return httpx.Response(
            200,
            json={
                "id": "item",
                "name": "hello.txt",
                "size": 5,
                "file": {"mimeType": "text/plain"},
                "parentReference": {"id": "root", "driveId": "drive"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    provider = CloudDrive(
        "weave-microsoft-drive",
        {"rootFolderId": "root", "driveId": "drive"},
        "private-token",
        WorkerPolicy(frozenset({"https://graph.microsoft.com"}), frozenset({"https://downloads.example"})),
        client=client,
    )
    async with client:
        assert b"".join([data async for data in provider.download("item")]) == b"hello"
    assert requests[-1].headers.get("authorization") is None
    assert requests[0].headers["authorization"] == "Bearer private-token"


async def test_cloud_scope_rejects_an_item_outside_root():
    def respond(request):
        item = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": item, "name": "file", "mimeType": "text/plain", "parents": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = CloudDrive(
            "weave-google-drive",
            {"rootFolderId": "allowed"},
            "private-token",
            WorkerPolicy(frozenset({"https://www.googleapis.com"})),
            client=client,
        )
        with pytest.raises(ConnectorFailure, match="FILE_SCOPE"):
            await provider.read("outside")


async def test_graph_cursor_cannot_change_scoped_request_path():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": "root", "name": "root", "folder": {}}))
    ) as client:
        provider = CloudDrive(
            "weave-microsoft-drive",
            {"rootFolderId": "root", "driveId": "drive"},
            "token",
            WorkerPolicy(frozenset({"https://graph.microsoft.com"})),
            client=client,
        )
        with pytest.raises(ConnectorFailure, match="FILE_CURSOR"):
            await provider.list("root", 10, "https://graph.microsoft.com/v1.0/users")


@pytest.mark.parametrize("graph", [False, True])
async def test_cloud_file_operations_use_scoped_ids_and_stream_uploads(graph):
    import hashlib
    from uuid import uuid4

    from firefly_weave.contracts.files import FileReference

    requests = []

    def entry(item="item", folder=False):
        common = {"id": item, "name": "file.txt", "size": 4}
        return (
            {
                **common,
                **({"folder": {}} if folder else {"file": {"mimeType": "text/plain"}}),
                "parentReference": {"id": "root", "driveId": "drive"},
            }
            if graph
            else {
                **common,
                "mimeType": "application/vnd.google-apps.folder" if folder else "text/plain",
                "parents": ["root"],
            }
        )

    async def respond(request):
        requests.append(request)
        path = request.url.path
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method in {"PUT", "POST"}:
            assert b"data" in await request.aread()
            return httpx.Response(201, json=entry("written"))
        if request.method == "PATCH":
            return httpx.Response(200, json=entry("moved"))
        if path.endswith("/children") or (not graph and path.endswith("/files")):
            return httpx.Response(200, json={"value" if graph else "files": [entry()]})
        return httpx.Response(200, json=entry("root", True) if path.endswith("/root") else entry())

    name = "weave-microsoft-drive" if graph else "weave-google-drive"
    origin = "https://graph.microsoft.com" if graph else "https://www.googleapis.com"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = CloudDrive(
            name,
            {"rootFolderId": "root", "driveId": "drive"},
            "token",
            WorkerPolicy(frozenset({origin})),
            client=client,
        )
        assert (await provider.list("", 10, None))["items"][0]["id"] == "item"
        assert (await provider.read("item"))["name"] == "file.txt"
        file = FileReference(
            id=uuid4(),
            filename="file.txt",
            contentType="text/plain",
            sizeBytes=4,
            sha256=hashlib.sha256(b"data").hexdigest(),
        )

        async def chunks():
            yield b"data"

        assert (await provider.write("root", "file.txt", chunks(), file))["id"] == "written"
        assert (await provider.move("item", "root", "renamed.txt"))["id"] == "moved"
        assert (await provider.delete("item"))["deleted"]
    assert all(request.headers.get("authorization") == "Bearer token" for request in requests)


async def test_google_unexpected_media_redirect_is_rejected():
    def respond(request):
        if request.url.params.get("alt") == "media":
            return httpx.Response(302, headers={"location": "https://other.example/file"})
        return httpx.Response(
            200, json={"id": "item", "name": "file", "size": 1, "mimeType": "text/plain", "parents": ["root"]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = CloudDrive(
            "weave-google-drive",
            {"rootFolderId": "root"},
            "token",
            WorkerPolicy(frozenset({"https://www.googleapis.com"})),
            client=client,
        )
        with pytest.raises(ConnectorFailure):
            [data async for data in provider.download("item")]
