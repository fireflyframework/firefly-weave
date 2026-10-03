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

"""Cancellation-aware sessions within server-isolated accounts and pinned peers."""

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import cast

import aioftp
import asyncssh
from firefly_weave.contracts.connectors import ConnectorFailure
from firefly_weave.contracts.file_connectors import CLOUD_ORIGINS
from firefly_weave.contracts.files import CHUNK_BYTES, MAX_FILE_BYTES, FileReference
from firefly_weave.contracts.values import JsonObject, JsonValue

from weave_files_worker.cloud import CloudDrive
from weave_files_worker.handler import Provider
from weave_files_worker.policy import WorkerPolicy, relative_path, scoped_path


class PinnedFTP(aioftp.Client):
    def __init__(self, address: str, hostname: str, port: int):
        super().__init__(
            socket_timeout=30, connection_timeout=15, passive_commands=("epsv",), trust_server_pasv_ipv4_address=False
        )
        self.address, self.hostname, self.control_port = address, hostname, port

    async def _open_connection(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if host not in {self.address, self.hostname} or (port != self.control_port and not 1024 <= port <= 65535):
            raise ConnectorFailure("FILE_POLICY", "not_started")
        context = None
        if self._stream is not None:
            previous = self._stream.writer.get_extra_info("ssl_object")
            if previous is not None:
                context = aioftp.common.SSLSessionBoundContext(
                    ssl.PROTOCOL_TLS_CLIENT, context=previous.context, session=previous.session
                )
        return await asyncio.open_connection(
            self.address, port, ssl=context, server_hostname=self.hostname if context else None
        )


class RemoteFiles:
    def __init__(
        self,
        root: str,
        *,
        ftp: aioftp.Client | None = None,
        sftp: asyncssh.SFTPClient | None = None,
        allow_non_atomic_destinations: bool = False,
    ):
        self.root = root.rstrip("/") or "/"
        self.ftp, self.sftp = ftp, sftp
        self.allow_non_atomic_destinations = allow_non_atomic_destinations

    async def path(self, value: str, *, new: bool = False) -> str:
        result = scoped_path(self.root, value)
        parts = PurePosixPath(result).parts
        end = len(parts) - 1 if new else len(parts)
        for count in range(1, end):
            current = str(PurePosixPath(*parts[: count + 1]))
            if self.sftp:
                attrs = await self.sftp.lstat(current)
                if attrs.type not in {asyncssh.FILEXFER_TYPE_DIRECTORY, asyncssh.FILEXFER_TYPE_REGULAR}:
                    raise ConnectorFailure("FILE_SYMLINK", "not_started")
            else:
                assert self.ftp
                attrs_ftp = await self.ftp.stat(current)
                if attrs_ftp.get("type") not in {"dir", "file"}:
                    raise ConnectorFailure("FILE_SYMLINK", "not_started")
        if self.sftp:
            checked = str(await self.sftp.realpath(str(PurePosixPath(result).parent) if new else result))
            if self.root != "/" and checked != self.root and not checked.startswith(self.root + "/"):
                raise ConnectorFailure("FILE_SCOPE", "not_started")
        return result

    async def read(self, location: str) -> JsonObject:
        path = await self.path(location)
        metadata: JsonObject = {"id": location or ".", "name": PurePosixPath(path).name or "/", "isFolder": False}
        if self.sftp:
            value = await self.sftp.lstat(path)
            metadata["isFolder"] = value.type == asyncssh.FILEXFER_TYPE_DIRECTORY
            if value.size is not None:
                metadata["sizeBytes"] = value.size
            if value.mtime is not None:
                metadata["modifiedAt"] = str(value.mtime)
        else:
            assert self.ftp
            facts = await self.ftp.stat(path)
            metadata["isFolder"] = facts.get("type") == "dir"
            if "size" in facts:
                metadata["sizeBytes"] = int(facts["size"])
            if "modify" in facts:
                metadata["modifiedAt"] = facts["modify"]
        if not metadata["isFolder"]:
            metadata["contentType"] = "application/octet-stream"
        return metadata

    async def list(self, location: str, limit: int, cursor: str | None) -> JsonObject:
        path = await self.path(location)
        try:
            offset = int(cursor or "0")
        except ValueError:
            raise ConnectorFailure("FILE_CURSOR", "not_started") from None
        if not 0 <= offset <= 1000:
            raise ConnectorFailure("FILE_CURSOR", "not_started")
        names = []
        if self.sftp:
            async for item in self.sftp.scandir(path):
                name = str(item.filename)
                if name in {".", ".."}:
                    continue
                names.append(name)
                if len(names) > 1000:
                    raise ConnectorFailure("FILE_LIMIT", "failed")
        else:
            assert self.ftp
            async for ftp_item, _ in self.ftp.list(path, recursive=False):
                names.append(ftp_item.name)
                if len(names) > 1000:
                    raise ConnectorFailure("FILE_LIMIT", "failed")
        names.sort()
        items = []
        for name in names[offset : offset + limit]:
            relative_path(name)
            items.append(await self.read(str(PurePosixPath(location) / name)))
        next_offset = offset + limit
        return {
            "items": cast(list[JsonValue], items),
            "nextCursor": str(next_offset) if next_offset < len(names) else None,
        }

    async def download(self, location: str) -> AsyncIterator[bytes]:
        metadata = await self.read(location)
        if metadata["isFolder"] or int(cast(int, metadata.get("sizeBytes", 0))) > MAX_FILE_BYTES:
            raise ConnectorFailure("FILE_LIMIT", "not_started")
        path = await self.path(location)
        size = 0
        if self.sftp:
            async with self.sftp.open(path, "rb") as file:
                while data := await file.read(CHUNK_BYTES):
                    assert isinstance(data, bytes)
                    size += len(data)
                    if size > MAX_FILE_BYTES:
                        raise ConnectorFailure("FILE_LIMIT", "failed")
                    yield data
        else:
            assert self.ftp
            async with self.ftp.download_stream(path) as stream:
                async for ftp_data in stream.iter_by_block(CHUNK_BYTES):
                    size += len(ftp_data)
                    if size > MAX_FILE_BYTES:
                        raise ConnectorFailure("FILE_LIMIT", "failed")
                    yield ftp_data

    async def destination(self, value: str) -> str:
        relative_path(value)
        path = await self.path(value, new=True)
        if self.sftp:
            if await self.sftp.lexists(path):
                raise ConnectorFailure("FILE_EXISTS", "not_started")
        else:
            assert self.ftp
            if await self.ftp.exists(path):
                raise ConnectorFailure("FILE_EXISTS", "not_started")
        return path

    async def write(self, destination: str, name: str, source: AsyncIterator[bytes], file: FileReference) -> JsonObject:
        self.require_destination_policy()
        path = await self.destination(destination)
        size = 0
        if self.sftp:
            async with self.sftp.open(path, "xb") as stream:
                async for data in source:
                    size += len(data)
                    if size > file.size_bytes:
                        raise ConnectorFailure("FILE_LIMIT", "unknown")
                    await stream.write(data)
        else:
            assert self.ftp
            async with self.ftp.upload_stream(path) as ftp_stream:
                async for data in source:
                    size += len(data)
                    if size > file.size_bytes:
                        raise ConnectorFailure("FILE_LIMIT", "unknown")
                    await ftp_stream.write(data)
        if size != file.size_bytes:
            raise ConnectorFailure("FILE_CHANGED", "unknown")
        return await self.read(destination)

    async def move(self, location: str, destination: str, name: str) -> JsonObject:
        self.require_destination_policy()
        relative_path(location)
        source = await self.path(location)
        target = await self.destination(destination)
        if self.sftp:
            await self.sftp.rename(source, target)
        else:
            assert self.ftp
            await self.ftp.rename(source, target)
        return await self.read(destination)

    def require_destination_policy(self) -> None:
        # FTP's existence check cannot make STOR or RNTO atomic against other writers.
        if self.ftp is not None and not self.allow_non_atomic_destinations:
            raise ConnectorFailure("FILE_ATOMIC_DESTINATION", "not_started")

    async def delete(self, location: str) -> JsonObject:
        relative_path(location)
        if (await self.read(location))["isFolder"]:
            raise ConnectorFailure("FILE_DIRECTORY", "not_started")
        path = await self.path(location)
        if self.sftp:
            await self.sftp.remove(path)
        else:
            assert self.ftp
            await self.ftp.remove_file(path)
        return {"deleted": True}


@asynccontextmanager
async def open_provider(
    name: str, config: JsonObject, credential: str, policy: WorkerPolicy
) -> AsyncIterator[Provider]:
    if name in CLOUD_ORIGINS:
        yield CloudDrive(name, config, credential, policy)
        return
    if any(ord(char) < 32 or ord(char) == 127 for char in str(config["username"]) + credential):
        raise ConnectorFailure("FILE_CONNECTION", "not_started")
    # Client path checks are defense in depth, not a substitute for a server jail.
    if config.get("serverRootIsolated") is not True or config.get("rootPath") != "/":
        raise ConnectorFailure("FILE_SCOPE", "not_started")
    host, port = str(config["host"]), int(cast(int, config["port"]))
    origin = f"{name.removeprefix('weave-')}://{host}:{port}"
    address = await policy.address(origin)
    if name == "weave-sftp":
        key = asyncssh.import_public_key(str(config["hostKey"]))
        sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            await asyncio.get_running_loop().sock_connect(sock, (address, port))
            async with asyncssh.connect(
                host,
                port,
                sock=sock,
                username=str(config["username"]),
                password=credential,
                known_hosts=([key], [], []),
                client_keys=[],
                agent_path=None,
                connect_timeout=15,
                login_timeout=15,
            ) as connection:
                async with connection.start_sftp_client() as sftp:
                    yield RemoteFiles(str(config["rootPath"]), sftp=sftp)
        finally:
            sock.close()
    else:
        ftp = PinnedFTP(address, host, port)
        try:
            await ftp.connect(host, port)
            if name == "weave-ftps":
                await ftp.upgrade_to_tls(ssl.create_default_context())
            await ftp.login(str(config["username"]), credential)
            yield RemoteFiles(
                str(config["rootPath"]),
                ftp=ftp,
                allow_non_atomic_destinations=(
                    config.get("destinationPolicy") == "allowNonAtomic" and policy.allow_non_atomic_ftp_destinations
                ),
            )
        finally:
            ftp.close()
