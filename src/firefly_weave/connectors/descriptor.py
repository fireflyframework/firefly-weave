# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Pure immutable executable descriptor contract shared with package authors."""

from collections.abc import Callable
from dataclasses import dataclass

from firefly_weave.compiler.action_config import ActionConfigValidator
from firefly_weave.compiler.catalog import FrozenDocument, TaskCapability
from firefly_weave.contracts.connectors import ConnectionRequest
from firefly_weave.contracts.workers import ConnectorBinding


@dataclass(frozen=True)
class ConnectorDescriptor:
    manifest: FrozenDocument
    implementation_version: str
    capabilities: tuple[TaskCapability, ...]
    bindings: tuple[ConnectorBinding, ...]
    validate_connection: Callable[[ConnectionRequest], None] | None = None
    # Compile-time Action configuration checks; invoked only for this exact manifest digest.
    validate_action_config: ActionConfigValidator | None = None
