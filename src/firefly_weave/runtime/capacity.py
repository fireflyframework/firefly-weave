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

"""Pure bounded logical allocation accounting; independent of debugger capacity."""

import json
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from firefly_weave.runtime.models import Transition

STATE_BYTES = 32 * 1024 * 1024
TRANSITION_BYTES = 64 * 1024 * 1024
TERMINAL_STATE_BYTES = 128 * 1024 * 1024
MAX_REDUCTIONS = 10_000
MAX_INTENTS = 1_000
capacity_failures: ContextVar[list[bool] | None] = ContextVar("weave_capacity_failures", default=None)


class RuntimeCapacityError(ValueError):
    def __init__(self) -> None:
        super().__init__("WV-RUNTIME-LIMIT")
        observed = capacity_failures.get()
        if observed is not None and not observed:
            observed.append(True)


def logical_size(value: object, ceiling: int) -> int:
    """Visit the live graph without first copying/dumping it; count shared values each time.

    Model defaults and optional fields are conservatively counted even when a JSON
    serializer might omit them. String chunks bound temporary encoder allocations.
    """
    total = nodes = 0
    strings: dict[int, tuple[str, int]] = {}
    active: set[int] = set()
    stack: list[tuple[Iterator[object], int | None]] = [(iter((value,)), None)]

    def string_size(item: str) -> int:
        cached = strings.get(id(item))
        if cached is not None:
            return cached[1]
        if len(item) > ceiling:
            raise RuntimeCapacityError()
        size = 2
        for offset in range(0, len(item), 4096):
            size += len(json.dumps(item[offset : offset + 4096], ensure_ascii=False).encode()) - 2
            if size > ceiling:
                raise RuntimeCapacityError()
        if len(strings) < 10_000:
            strings[id(item)] = (item, size)
        return size

    while stack:
        iterator, identity = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            if identity is not None:
                active.remove(identity)
            continue
        nodes += 1
        if nodes > 2_000_000 or len(stack) > 128:
            raise RuntimeCapacityError()
        if isinstance(item, BaseModel):
            item = item.__dict__
        if isinstance(item, dict):
            total += 2 + max(0, len(item) - 1)
            for key in item:
                total += string_size(str(key)) + 1
                if total > ceiling:
                    raise RuntimeCapacityError()
            children = iter(item.values())
        elif isinstance(item, (list, tuple)):
            total += 2 + max(0, len(item) - 1)
            children = iter(item)
        else:
            if isinstance(item, (datetime, UUID)):
                item = item.isoformat() if isinstance(item, datetime) else str(item)
            total += string_size(item) if isinstance(item, str) else len(json.dumps(item, allow_nan=False))
            children = None
        if total > ceiling:
            raise RuntimeCapacityError()
        if children is not None:
            if id(item) in active:
                raise RuntimeCapacityError()
            active.add(id(item))
            stack.append((children, id(item)))
    return total


def admit_transition(result: Transition) -> None:
    logical_size(result, TRANSITION_BYTES)
    logical_size(result.state, STATE_BYTES)
    commands = result.commands
    if sum(getattr(command, "kind", None) in {"task", "deadline"} for command in commands) > MAX_INTENTS:
        raise RuntimeCapacityError()
