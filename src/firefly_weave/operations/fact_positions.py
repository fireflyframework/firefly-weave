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

"""Typed positions for chronological run and step fact queries."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter

from firefly_weave.contracts.definitions import ResourceName
from firefly_weave.contracts.instance_keys import InstanceKeyText, instance_view
from firefly_weave.contracts.run_views import utc_time
from firefly_weave.contracts.values import JsonValue

type Position = tuple[JsonValue, str] | None

_NODE: TypeAdapter[str] = TypeAdapter(ResourceName)
_INSTANCE: TypeAdapter[str] = TypeAdapter(InstanceKeyText)


@dataclass(frozen=True)
class RunPosition:
    """A nullable recorded time and the UUID tie breaker."""

    at: datetime | None
    id: UUID


@dataclass(frozen=True)
class StepPosition:
    """A nullable scheduled time and the canonical step instance identity."""

    scheduled_at: datetime | None
    node_id: str
    instance_key: str


def run_position(position: Position) -> RunPosition | None:
    """Validate a decoded run cursor before using its values in SQL."""
    if position is None:
        return None
    value, identifier = position
    if type(identifier) is not str:
        raise ValueError("Invalid run position identifier")
    return RunPosition(utc_time(value), UUID(identifier))


def step_position(position: Position) -> StepPosition | None:
    """Validate a decoded step cursor: the scheduled time and the full author instance key it ends at.

    The key alone determines the step ID and the stored instance key ('' for the step itself).
    """
    if position is None:
        return None
    at, identifier = position
    identity = instance_view(_INSTANCE.validate_python(identifier, strict=True))
    _NODE.validate_python(identity.node_id, strict=True)
    return StepPosition(utc_time(at), identity.node_id, identity.instance_key)
