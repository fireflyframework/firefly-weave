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

"""Files are bounded opaque references, never arbitrary paths or workflow bytes."""

import base64
from uuid import uuid4

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.files import CHUNK_BYTES, FileChunk, FileCreate, FileReference


def file_request(**changes):
    return {"filename": "invoice.pdf", "contentType": "application/pdf", "sizeBytes": 10, "sha256": "a" * 64, **changes}


def test_reference_is_canonical_metadata_without_storage_url_or_credentials():
    ref = FileReference(id=uuid4(), **file_request())
    assert ref.model_dump(mode="json", by_alias=True)["kind"] == "weave/file"
    with pytest.raises(ValidationError):
        FileReference(id=uuid4(), url="https://private.example/file", **file_request())


@pytest.mark.parametrize(
    "changes",
    [
        {"filename": "../secret"},
        {"filename": "C:\\private"},
        {"filename": "bad\nheader"},
        {"filename": ""},
        {"filename": "."},
        {"contentType": "text/html\nX-Header: value"},
        {"sizeBytes": -1},
        {"sizeBytes": True},
        {"sizeBytes": 26214401},
        {"sha256": "not-a-hash"},
    ],
)
def test_invalid_metadata_is_rejected(changes):
    with pytest.raises(ValidationError):
        FileCreate(**file_request(**changes))


def test_chunk_decodes_only_strict_base64_within_bound():
    data = b"x" * CHUNK_BYTES
    chunk = FileChunk(index=0, contentBase64=base64.b64encode(data).decode())
    assert chunk.content() == data
    for text in ["!!", base64.b64encode(data + b"x").decode()]:
        with pytest.raises(ValidationError):
            FileChunk(index=0, contentBase64=text)
