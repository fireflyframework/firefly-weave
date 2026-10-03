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

"""References are data, not bearer capabilities; fetching always checks scoped authority."""

import json
from uuid import UUID

from firefly_weave.contracts.files import FileReference
from firefly_weave.contracts.values import JsonValue


def file_references(value: JsonValue) -> dict[UUID, FileReference]:
    pending = [value]
    result: dict[UUID, FileReference] = {}
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("kind") == "weave/file":
                reference = FileReference.model_validate_json(json.dumps(item))
                previous = result.get(reference.id)
                if previous is not None and previous != reference:
                    raise ValueError("Conflicting file metadata")
                result[reference.id] = reference
            else:
                pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return result
