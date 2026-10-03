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

"""Task-scoped transfer requests carry the current human claim revision."""

from uuid import UUID

from firefly_weave.contracts.files import FileChunk, FileChunkRead, FileCreate
from firefly_weave.contracts.human_tasks import HumanTaskCommand


class HumanFileCreate(HumanTaskCommand):
    file: FileCreate


class HumanFileCommand(HumanTaskCommand):
    file_id: UUID


class HumanFileChunk(HumanFileCommand):
    chunk: FileChunk


class HumanFileRead(HumanFileCommand):
    chunk: FileChunkRead
