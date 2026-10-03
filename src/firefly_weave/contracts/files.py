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

"""Bounded file transfers keep binary content outside workflow state and event history."""

import base64
import binascii
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import Field, field_validator

from firefly_weave.contracts.definitions import ContractModel
from firefly_weave.contracts.values import JsonObject

CHUNK_BYTES: Literal[262144] = 262144
MAX_FILE_BYTES = 26214400
MAX_ENVIRONMENT_BYTES = 1073741824
MAX_ENVIRONMENT_FILES = 1000
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FileCreate(ContractModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(
        alias="contentType", max_length=128, pattern=r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$"
    )
    size_bytes: int = Field(alias="sizeBytes", ge=0, le=MAX_FILE_BYTES)
    sha256: Digest

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or any(char in value for char in "/\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Use a filename without folders or control characters")
        return value


class FileReference(FileCreate):
    kind: Literal["weave/file"] = "weave/file"
    id: UUID


class FileUpload(ContractModel):
    file: FileReference
    state: Literal["uploading", "ready", "deleted"]
    received_chunks: list[int] = Field(default_factory=list, max_length=100)
    chunk_bytes: Literal[262144] = CHUNK_BYTES


class FileChunk(ContractModel):
    index: int = Field(ge=0, le=99)
    content_base64: str = Field(alias="contentBase64", max_length=349528, repr=False)

    @field_validator("content_base64")
    @classmethod
    def bounded_base64(cls, value: str) -> str:
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Provide a valid base64 file chunk") from None
        if not data or len(data) > CHUNK_BYTES:
            raise ValueError("File chunk exceeds its bound")
        return value

    def content(self) -> bytes:
        return base64.b64decode(self.content_base64, validate=True)


class FileCommand(ContractModel):
    pass


class FileChunkRead(ContractModel):
    index: int = Field(ge=0, le=99)


def file_reference_schema() -> JsonObject:
    """Authoring schema requires the discriminator so references cannot become opaque objects."""
    schema = FileReference.model_json_schema(by_alias=True)
    schema["required"].append("kind")
    schema["properties"]["kind"].pop("default", None)
    return cast(JsonObject, schema)
