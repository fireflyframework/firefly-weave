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

"""Human-friendly file transfers use the shared SDK and the selected platform profile."""

import mimetypes
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

import click

from firefly_weave.cli.http_actions import FlowProblem, remote_params, run_remote
from firefly_weave.cli.remote import family, machine_result
from firefly_weave.contracts.files import CHUNK_BYTES

files = family("files", "files")
files.help = "Upload, download and inspect files in the selected environment."
# Raw chunk transfer stays available through the SDK/API. Downloads in the CLI always verify integrity.
for operation in ("chunk", "finish", "download"):
    files.commands.pop(operation, None)


@click.command("upload")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--content-type", help="Media type; default: infer from filename or application/octet-stream.")
@click.option("--idempotency-key", required=True, help="Stable transfer key; reuse it to resume the same file.")
@click.pass_context
def upload(ctx: click.Context, /, source: Path, content_type: str | None, idempotency_key: str, **options: Any) -> None:
    """Upload a local file and print its workflow reference as JSON (maximum 25 MiB)."""
    from firefly_weave.sdk.files import upload_file

    async def transfer(sdk: Any) -> Any:
        with source.open("rb") as stream:
            return await upload_file(
                sdk,
                stream,
                filename=source.name,
                content_type=content_type or mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                idempotency_key=idempotency_key,
            )

    machine_result(run_remote(ctx, "files.create", options, transfer))


@click.command("download")
@click.argument("identifier", type=click.UUID)
@click.option(
    "--to",
    "destination",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="New output file; an existing path is never overwritten.",
)
@click.pass_context
def download(ctx: click.Context, /, identifier: UUID, destination: Path, **options: Any) -> None:
    """Verify all chunks and SHA-256 before publishing the local file."""
    from firefly_weave.sdk.files import download_file

    async def transfer(sdk: Any) -> Any:
        if destination.exists() or destination.is_symlink():
            raise FlowProblem("WV-FILE-EXISTS", "Choose a new --to path; the destination already exists", 409)
        upload = await sdk.read_file(identifier)
        if upload.state != "ready":
            raise FlowProblem("WV-FILE-INCOMPLETE", "Finish the upload before downloading this file", 409)
        async with download_file(sdk, upload.file) as verified:
            # A same-directory temporary file plus an exclusive hard link prevents partial output and overwrite races.
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, prefix=".weave-download-", delete=False
                ) as local:
                    temporary = Path(local.name)
                    shutil.copyfileobj(verified, local, length=CHUNK_BYTES)
                    local.flush()
                    os.fsync(local.fileno())
                os.link(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return {"file": upload.file.model_dump(mode="json", by_alias=True), "path": str(destination)}

    machine_result(run_remote(ctx, "files.download", options, transfer))


upload.params.extend(remote_params("files.create"))
download.params.extend(remote_params("files.download"))
files.add_command(upload)
files.add_command(download)
