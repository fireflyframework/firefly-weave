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

"""File CLI transfers real bounded content without overwriting local files."""

import base64
import hashlib
import json
from uuid import UUID

import pytest
from click.testing import CliRunner

from firefly_weave.cli.main import cli
from firefly_weave.contracts.files import FileChunk, FileReference, FileUpload
from firefly_weave.sdk import client


@pytest.fixture
def platform(monkeypatch, tmp_path):
    monkeypatch.setenv("WEAVE_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("WEAVE_ACCESS_TOKEN", "fixture-token")
    state = {"content": b"", "calls": []}

    class Fake:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def create_file(self, request, *, idempotency_key):
            state["calls"].append(idempotency_key)
            state["reference"] = FileReference(id=UUID(int=10), **request.model_dump(by_alias=True))
            return FileUpload(file=state["reference"], state="uploading")

        async def put_file_chunk(self, identifier, chunk):
            state["content"] += chunk.content()
            return FileUpload(file=state["reference"], state="uploading", received_chunks=[chunk.index])

        async def finish_file(self, identifier):
            return FileUpload(file=state["reference"], state="ready")

        async def read_file(self, identifier):
            return FileUpload(file=state["reference"], state="ready")

        async def read_file_chunk(self, identifier, index):
            data = b"broken" if state.get("corrupt") else state["content"]
            return FileChunk(index=index, contentBase64=base64.b64encode(data).decode())

    monkeypatch.setattr(client, "WeaveClient", Fake)
    return state


def run(*args):
    target = [
        "--base-url",
        "https://weave.example.test",
        "--tenant",
        str(UUID(int=1)),
        "--project",
        str(UUID(int=2)),
        "--environment",
        str(UUID(int=3)),
    ]
    return CliRunner().invoke(cli, ["files", *[str(a) for a in args], *target])


def test_cli_upload_download_integrity_and_refusal_to_overwrite(platform, tmp_path):
    source = tmp_path / "invoice.txt"
    source.write_bytes(b"invoice")
    result = run("upload", source, "--idempotency-key", "invoice-v1")
    assert result.exit_code == 0, result.output
    reference = json.loads(result.output)
    assert reference["sha256"] == hashlib.sha256(b"invoice").hexdigest()
    target = tmp_path / "downloaded.txt"
    result = run("download", reference["id"], "--to", target)
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == b"invoice"
    result = run("download", reference["id"], "--to", target)
    assert result.exit_code != 0 and target.read_bytes() == b"invoice"
    platform["corrupt"] = True
    other = tmp_path / "bad.txt"
    assert run("download", reference["id"], "--to", other).exit_code != 0
    assert not other.exists()
