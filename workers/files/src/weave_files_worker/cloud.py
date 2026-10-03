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

"""Fixed-origin Graph and Drive APIs with bounded replies and scoped folder traversal."""

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from firefly_weave.connectors.egress import EgressPolicy, PinnedTransport
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.file_connectors import CLOUD_ORIGINS
from firefly_weave.contracts.files import CHUNK_BYTES, MAX_FILE_BYTES, FileReference
from firefly_weave.contracts.values import JsonObject, JsonValue

from weave_files_worker.policy import WorkerPolicy, relative_path

GOOGLE_FIELDS = "id,name,mimeType,size,modifiedTime,version,parents,trashed,shortcutDetails"
GRAPH_FIELDS = "id,name,size,file,folder,lastModifiedDateTime,eTag,parentReference,remoteItem"


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9!_.:-]{1,512}", value):
        raise ConnectorFailure("FILE_PATH", "not_started")
    return quote(value, safe="")


class CloudDrive:
    def __init__(
        self,
        name: str,
        config: JsonObject,
        credential: str,
        policy: WorkerPolicy,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.name, self.config, self.credential, self.policy = name, config, credential, policy
        self.origin = CLOUD_ORIGINS[name]
        self.root = str(config["rootFolderId"])
        identifier(self.root)
        self.graph = name == "weave-microsoft-drive"
        self.base = self.origin + ("/v1.0/drives/" + identifier(str(config["driveId"])) if self.graph else "/drive/v3")
        self.client = client
        self.calls = 0

    @asynccontextmanager
    async def client_for(self, url: str) -> AsyncIterator[httpx.AsyncClient]:
        if self.client is not None:
            yield self.client
        else:
            origin = f"https://{urlsplit(url).netloc}"
            async with httpx.AsyncClient(
                transport=PinnedTransport(EgressPolicy((origin,), self.policy.private_networks), url),
                timeout=httpx.Timeout(30),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                yield client

    def headers(self, url: str) -> dict[str, str]:
        self.calls += 1
        if self.calls > 128:
            raise ConnectorFailure("FILE_LIMIT", "failed")
        if urlsplit(url).scheme != "https":
            raise ConnectorFailure("FILE_POLICY", "not_started")
        if f"https://{urlsplit(url).netloc}" == self.origin:
            return {"authorization": "Bearer " + self.credential, "accept": "application/json"}
        self.policy.download_url(url)
        return {}

    async def json(
        self,
        method: str,
        url: str,
        *,
        body: JsonObject | None = None,
        content: AsyncIterator[bytes] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> JsonObject:
        headers = self.headers(url)
        headers.update(extra_headers or {})
        async with self.client_for(url) as client:
            async with client.stream(method, url, headers=headers, json=body, content=content) as response:
                if not 200 <= response.status_code < 300:
                    raise ConnectorFailure("FILE_PROVIDER", "unknown")
                data = bytearray()
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    if len(data) + len(chunk) > 1048576:
                        raise ConnectorFailure("FILE_LIMIT", "unknown")
                    data.extend(chunk)
                if not data:
                    return {}
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ConnectorFailure("FILE_OUTPUT", "unknown")
                return cast(JsonObject, value)

    def item_url(self, location: str) -> str:
        return self.base + ("/items/" if self.graph else "/files/") + identifier(location)

    async def scoped(self, location: str) -> JsonObject:
        location = location or self.root
        raw = await self.json(
            "GET",
            self.item_url(location)
            + ("?$select=" + GRAPH_FIELDS if self.graph else "?supportsAllDrives=true&fields=" + GOOGLE_FIELDS),
        )
        current = raw
        seen = set()
        for _ in range(32):
            if "remoteItem" in current or "shortcutDetails" in current or current.get("trashed"):
                raise ConnectorFailure("FILE_SCOPE", "not_started")
            current_id = str(current.get("id", ""))
            if current_id == self.root:
                return raw
            if not current_id or current_id in seen:
                break
            seen.add(current_id)
            if self.graph:
                parent = current.get("parentReference")
                if not isinstance(parent, dict) or parent.get("driveId") != self.config["driveId"]:
                    break
                parent_id = str(parent.get("id", ""))
            else:
                parents = current.get("parents")
                if not isinstance(parents, list) or len(parents) != 1:
                    break
                parent_id = str(parents[0])
            if parent_id == self.root:
                return raw
            current = await self.json(
                "GET",
                self.item_url(parent_id)
                + ("?$select=" + GRAPH_FIELDS if self.graph else "?supportsAllDrives=true&fields=" + GOOGLE_FIELDS),
            )
        raise ConnectorFailure("FILE_SCOPE", "not_started")

    def metadata(self, raw: JsonObject) -> JsonObject:
        folder = "folder" in raw if self.graph else raw.get("mimeType") == "application/vnd.google-apps.folder"
        result: JsonObject = {"id": str(raw["id"]), "name": str(raw["name"]), "isFolder": folder}
        if "size" in raw:
            result["sizeBytes"] = int(cast(int | str, raw["size"]))
        file = raw.get("file")
        content_type = file.get("mimeType") if isinstance(file, dict) else raw.get("mimeType")
        if content_type:
            result["contentType"] = str(content_type)
        for source, target in (
            ("lastModifiedDateTime" if self.graph else "modifiedTime", "modifiedAt"),
            ("eTag" if self.graph else "version", "version"),
        ):
            if source in raw:
                result[target] = str(raw[source])
        return result

    async def read(self, location: str) -> JsonObject:
        return self.metadata(await self.scoped(location))

    async def list(self, location: str, limit: int, cursor: str | None) -> JsonObject:
        location = location or self.root
        parent = await self.scoped(location)
        if not self.metadata(parent)["isFolder"]:
            raise ConnectorFailure("FILE_PATH", "not_started")
        if self.graph:
            prefix = self.item_url(location) + "/children"
            if cursor and (
                urlsplit(cursor).scheme != "https"
                or urlsplit(cursor).netloc != "graph.microsoft.com"
                or urlsplit(cursor).path != urlsplit(prefix).path
                or urlsplit(cursor).fragment
                or urlsplit(cursor).username
            ):
                raise ConnectorFailure("FILE_CURSOR", "not_started")
            url = cursor or prefix + f"?$top={limit}&$select=" + GRAPH_FIELDS
        else:
            query = {
                "q": f"'{location}' in parents and trashed = false",
                "pageSize": str(limit),
                "fields": "nextPageToken,files(" + GOOGLE_FIELDS + ")",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if cursor:
                query["pageToken"] = cursor
            url = str(httpx.URL(self.base + "/files", params=query))
        response = await self.json("GET", url)
        values = response.get("value" if self.graph else "files", [])
        if not isinstance(values, list) or len(values) > limit:
            raise ConnectorFailure("FILE_LIMIT", "failed")
        items = [
            self.metadata(value)
            for value in values
            if isinstance(value, dict) and "remoteItem" not in value and "shortcutDetails" not in value
        ]
        return {
            "items": cast(list[JsonValue], items),
            "nextCursor": response.get("@odata.nextLink" if self.graph else "nextPageToken"),
        }

    async def download(self, location: str) -> AsyncIterator[bytes]:
        raw = await self.scoped(location)
        metadata = self.metadata(raw)
        if metadata["isFolder"] or int(cast(int, metadata.get("sizeBytes", 0))) > MAX_FILE_BYTES:
            raise ConnectorFailure("FILE_LIMIT", "not_started")
        # Native Google documents need an explicit export operation and format, not a guessed byte representation.
        if not self.graph and str(raw.get("mimeType", "")).startswith("application/vnd.google-apps."):
            raise ConnectorFailure("FILE_EXPORT_REQUIRED", "not_started")
        url = self.item_url(location) + ("/content" if self.graph else "?alt=media&supportsAllDrives=true")
        for attempt in range(2):
            headers = self.headers(url) if attempt == 0 else {}
            async with self.client_for(url) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if self.graph and attempt == 0 and response.status_code == 302:
                        url = self.policy.download_url(response.headers.get("location", ""))
                        continue
                    if response.status_code != 200:
                        raise ConnectorFailure("FILE_PROVIDER", "failed")
                    size = 0
                    async for data in response.aiter_bytes(CHUNK_BYTES):
                        size += len(data)
                        if size > MAX_FILE_BYTES:
                            raise ConnectorFailure("FILE_LIMIT", "failed")
                        yield data
                    return
        raise ConnectorFailure("FILE_REDIRECT", "failed")

    async def write(self, destination: str, name: str, source: AsyncIterator[bytes], file: FileReference) -> JsonObject:
        if "/" in relative_path(name):
            raise ConnectorFailure("FILE_PATH", "not_started")
        parent = await self.scoped(destination)
        if not self.metadata(parent)["isFolder"]:
            raise ConnectorFailure("FILE_PATH", "not_started")
        if self.graph:
            url = (
                self.item_url(destination)
                + ":/"
                + quote(name, safe="")
                + ":/content?@microsoft.graph.conflictBehavior=fail"
            )
            result = await self.json(
                "PUT",
                url,
                content=source,
                extra_headers={
                    "content-type": file.content_type,
                    "content-length": str(file.size_bytes),
                    "if-none-match": "*",
                },
            )
        else:
            boundary = "weave_" + uuid4().hex
            prefix = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + json.dumps({"name": name, "parents": [destination], "mimeType": file.content_type})
                + f"\r\n--{boundary}\r\nContent-Type: {file.content_type}\r\n\r\n"
            ).encode()
            suffix = f"\r\n--{boundary}--\r\n".encode()

            async def multipart() -> AsyncIterator[bytes]:
                yield prefix
                async for chunk in source:
                    yield chunk
                yield suffix

            result = await self.json(
                "POST",
                self.origin
                + "/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true&fields="
                + GOOGLE_FIELDS,
                content=multipart(),
                extra_headers={
                    "content-type": "multipart/related; boundary=" + boundary,
                    "content-length": str(len(prefix) + file.size_bytes + len(suffix)),
                },
            )
        return self.metadata(result)

    async def move(self, location: str, destination: str, name: str) -> JsonObject:
        if location == self.root or "/" in relative_path(name):
            raise ConnectorFailure("FILE_SCOPE", "not_started")
        raw = await self.scoped(location)
        parent = await self.scoped(destination)
        if not self.metadata(parent)["isFolder"]:
            raise ConnectorFailure("FILE_PATH", "not_started")
        if self.graph:
            result = await self.json(
                "PATCH", self.item_url(location), body={"name": name, "parentReference": {"id": destination}}
            )
        else:
            url = str(
                httpx.URL(
                    self.item_url(location),
                    params={
                        "addParents": destination,
                        "removeParents": ",".join(cast(list[str], raw.get("parents", []))),
                        "supportsAllDrives": "true",
                        "fields": GOOGLE_FIELDS,
                    },
                )
            )
            result = await self.json("PATCH", url, body={"name": name})
        return self.metadata(result)

    async def delete(self, location: str) -> JsonObject:
        if location == self.root:
            raise ConnectorFailure("FILE_SCOPE", "not_started")
        if self.metadata(await self.scoped(location))["isFolder"]:
            raise ConnectorFailure("FILE_DIRECTORY", "not_started")
        await self.json("DELETE", self.item_url(location) + ("" if self.graph else "?supportsAllDrives=true"))
        return {"deleted": True}
