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
from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from uuid import UUID

from pydantic import BaseModel

from firefly_weave.runtime.models import Transition

STATE_BYTES = 32 * 1024 * 1024
TRANSITION_BYTES = 64 * 1024 * 1024
TERMINAL_STATE_BYTES = 128 * 1024 * 1024
MAX_REDUCTIONS = 10_000
MAX_INTENTS = 1_000
# One event's transition is admitted at its final checkpoint, and earlier once this many reductions or this many
# bytes of new step output have accumulated since the last admission.
ADMISSION_REDUCTIONS = 64
ADMISSION_OUTPUT_BYTES = 1024 * 1024
# Loop work one event may start: each started iteration costs one unit plus one per node of the loop body. A loop
# also stops starting iterations once the event holds MAX_INTENTS // 2 intents, and yields the rest to a later event.
LOOP_TURN_UNITS = 4000
capacity_failures: ContextVar[list[bool] | None] = ContextVar("weave_capacity_failures", default=None)


class RuntimeCapacityError(ValueError):
    def __init__(self) -> None:
        super().__init__("WV-RUNTIME-LIMIT")
        observed = capacity_failures.get()
        if observed is not None and not observed:
            observed.append(True)


@dataclass
class _Measured:
    value: object
    count: int = 0
    size: int = 0
    nodes: int = 0
    depth: int = 0


# JSON lists and objects below a model field with at least this many elements are measured once per event.
SHARED_VALUE_ELEMENTS = 16


class GrowingContainers:
    """Containers whose measured elements never change while one event is reduced, measured once per element.

    Registered: the transition's steps and commands, the run input, and each loop's elements and collected
    outputs, which only grow. Also, while registered, every JSON list or object of at least
    ``SHARED_VALUE_ELEMENTS`` elements found below a model field: the kernel never changes a JSON value in place,
    it replaces it. Admitting a long event therefore does not walk its earlier work or its shared data again.
    Entries keep their container alive, so an identity is never reused while it is registered.
    """

    def __init__(self) -> None:
        self.measured: dict[int, _Measured] = {}

    def add(self, value: object) -> _Measured:
        entry = self.measured.get(id(value))
        if entry is None or entry.value is not value:
            entry = self.measured[id(value)] = _Measured(value)
        return entry

    def discard(self, value: object) -> None:
        entry = self.measured.get(id(value))
        if entry is not None and entry.value is value:
            del self.measured[id(value)]

    def entry(self, value: object) -> _Measured | None:
        entry = self.measured.get(id(value))
        return entry if entry is not None and entry.value is value else None


growing_containers: ContextVar[GrowingContainers | None] = ContextVar("weave_growing_containers", default=None)


def logical_size(value: object, ceiling: int) -> int:
    """Visit the live graph without first copying/dumping it; count shared values each time.

    Model defaults and optional fields are conservatively counted even when a JSON
    serializer might omit them. String chunks bound temporary encoder allocations.
    Registered growing containers (``growing_containers``) contribute the same bytes, nodes and depth as a full
    visit, measured once per element.
    """
    strings: dict[int, tuple[str, int]] = {}
    return _walk(value, ceiling, growing_containers.get(), strings, 0, 0, 0)[0]


def _walk(
    value: object,
    ceiling: int,
    known: GrowingContainers | None,
    strings: dict[int, tuple[str, int]],
    total: int,
    nodes: int,
    base: int,
) -> tuple[int, int, int]:
    """Running ``(total, nodes, deepest stack level)`` after visiting ``value`` at stack level ``base``."""
    deepest = base
    active: set[int] = set()
    # Each level: its iterator, the identity of its container, and whether it yields model fields.
    stack: list[tuple[Iterator[object], int | None, bool]] = [(iter((value,)), None, True)]

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
        iterator, identity, fields = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            if identity is not None:
                active.remove(identity)
            continue
        nodes += 1
        level = base + len(stack)
        deepest = max(deepest, level)
        if nodes > 2_000_000 or level > 128:
            raise RuntimeCapacityError()
        model = isinstance(item, BaseModel)
        if model:
            item = item.__dict__
        if known is not None and not model and isinstance(item, (list, dict)):
            entry = known.entry(item)
            if entry is None and not fields and len(item) >= SHARED_VALUE_ELEMENTS:
                entry = known.add(item)
            if entry is not None:
                # A growing container: measure only the elements added since its last visit.
                if len(item) < entry.count:
                    entry.count = entry.size = entry.nodes = entry.depth = 0
                total += 2 + max(0, len(item) - 1)
                fresh: Iterator[tuple[str | None, object]]
                if isinstance(item, dict):
                    fresh = islice(item.items(), entry.count, None)
                else:
                    fresh = ((None, element) for element in item[entry.count :])
                for key, element in fresh:
                    before, counted = total + entry.size, nodes + entry.nodes
                    if key is not None:
                        before += string_size(str(key)) + 1
                    after, counted_after, reached = _walk(element, ceiling, known, strings, before, counted, level)
                    entry.size += after - (total + entry.size)
                    entry.nodes += counted_after - counted
                    entry.depth = max(entry.depth, reached - level)
                    entry.count += 1
                total += entry.size
                nodes += entry.nodes
                deepest = max(deepest, level + entry.depth)
                if total > ceiling or nodes > 2_000_000 or level + entry.depth > 128:
                    raise RuntimeCapacityError()
                continue
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
            stack.append((children, id(item), model))
    return total, nodes, deepest


def admit_transition(result: Transition) -> None:
    logical_size(result, TRANSITION_BYTES)
    logical_size(result.state, STATE_BYTES)
    commands = result.commands
    intents = {"task", "human_task", "deadline", "call"}
    if sum(getattr(command, "kind", None) in intents for command in commands) > MAX_INTENTS:
        raise RuntimeCapacityError()
