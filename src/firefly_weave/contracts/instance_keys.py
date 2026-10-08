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

"""Instance keys: the runtime identity of one activation of a workflow node.

Grammar (frozen in language milestone M0; overview contract C11)::

    instance-key = node-id *("[" index "]") ["#" seg *("." seg)] ["~" count]
    index        = "0" / (%x31-39 *DIGIT)     ; loop iteration, outer loop first
    count        = %x31-39 *DIGIT             ; loop yield count, from 1
    seg          = 1*(ALPHA / DIGIT)          ; repeated activation (agent turn, tool call, review)

Node IDs never contain ``[``, ``]``, ``#`` or ``~`` (author IDs are ``ResourceName`` values; the synthetic
``@run``, ``@start``, ``@join:X`` and ``@branch:X:N`` use none of them), so parsing is unambiguous. Indexes and
counts are at most 2**53 - 1 so that API views can carry them as JSON integers.

``INSTANCE_KEY_PATTERN`` is this grammar, bound included, as regular-expression source that reads the same in
Python ``re``, ECMAScript and JSON Schema ``pattern``; ``split_instance`` validates with it. API models declare
key fields as ``InstanceKeyText``, or ``InstanceKeyTextOrEmpty`` where ``""`` stands for the step itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from firefly_weave.contracts.values import MAX_SAFE_INTEGER


def _positive_at_most(limit: int) -> str:
    """Regular-expression source for the decimals 1 to ``limit`` without sign or leading zeros."""
    digits = str(limit)
    branches = [f"[1-9][0-9]{{0,{len(digits) - 2}}}"] if len(digits) > 1 else []
    for position, digit in enumerate(digits):
        lowest = 1 if position == 0 else 0
        if int(digit) > lowest:
            below = str(lowest) if int(digit) - 1 == lowest else f"[{lowest}-{int(digit) - 1}]"
            rest = len(digits) - position - 1
            branches.append(digits[:position] + below + (f"[0-9]{{{rest}}}" if rest > 1 else "[0-9]" * rest))
    branches.append(digits)
    return "|".join(branches)


_NODE_ID = r"[^\[\]#~]+"
_INDEX = "0|" + _positive_at_most(MAX_SAFE_INTEGER)
_COUNT = _positive_at_most(MAX_SAFE_INTEGER)
_SEGMENT = r"[A-Za-z0-9]+"

# The whole grammar without anchors: re.fullmatch it, or wrap it in ^(?:...)$ as the API types below do.
INSTANCE_KEY_PATTERN: str = rf"{_NODE_ID}(?:\[(?:{_INDEX})\])*(?:#{_SEGMENT}(?:\.{_SEGMENT})*)?(?:~(?:{_COUNT}))?"

InstanceKeyText = Annotated[str, Field(max_length=512, pattern=rf"^(?:{INSTANCE_KEY_PATTERN})$")]
InstanceKeyTextOrEmpty = Annotated[str, Field(max_length=512, pattern=rf"^(?:{INSTANCE_KEY_PATTERN})?$")]

_SEPARATORS = re.compile(r"[\[#~]")
_KEY = re.compile(INSTANCE_KEY_PATTERN)
_NODE = re.compile(_NODE_ID)
_SEGMENT_TEXT = re.compile(_SEGMENT)


class InvalidInstanceKey(ValueError):
    """The text or parts do not form one canonical instance key."""


@dataclass(frozen=True)
class InstanceKey:
    """A parsed instance key; constructing one validates every part."""

    node_id: str
    indexes: tuple[int, ...] = ()
    segments: tuple[str, ...] = ()
    count: int | None = None

    def __post_init__(self) -> None:
        if type(self.node_id) is not str or _NODE.fullmatch(self.node_id) is None:
            raise InvalidInstanceKey("Node IDs are non-empty and contain no '[', ']', '#' or '~'")
        if type(self.indexes) is not tuple or any(
            type(index) is not int or not 0 <= index <= MAX_SAFE_INTEGER for index in self.indexes
        ):
            raise InvalidInstanceKey("Loop indexes are integers from 0 to 2**53 - 1")
        if type(self.segments) is not tuple or any(
            type(segment) is not str or _SEGMENT_TEXT.fullmatch(segment) is None for segment in self.segments
        ):
            raise InvalidInstanceKey("Activation segments contain only ASCII letters and digits")
        if self.count is not None and (type(self.count) is not int or not 1 <= self.count <= MAX_SAFE_INTEGER):
            raise InvalidInstanceKey("Yield counts are integers from 1 to 2**53 - 1")


@dataclass(frozen=True)
class InstanceView:
    """The C11 API view of a key: static step ID, full key ('' when it equals the step ID), loop indexes."""

    node_id: str
    instance_key: str
    iteration: list[int]


def node_of(key: str) -> str:
    """The static node ID: the key cut at its first '[', '#' or '~'. Cheap; does not validate the rest."""
    if type(key) is not str:
        raise InvalidInstanceKey("Instance keys are strings")
    match = _SEPARATORS.search(key)
    node = key if match is None else key[: match.start()]
    if not node:
        raise InvalidInstanceKey("Instance keys start with a node ID")
    return node


def split_instance(key: str) -> InstanceKey:
    """Parse a whole key; non-canonical keys (such as 'send[03]') are rejected."""
    if type(key) is not str:
        raise InvalidInstanceKey("Instance keys are strings")
    if _KEY.fullmatch(key) is None:
        raise InvalidInstanceKey("Text is not a canonical instance key")
    # The pattern proved the shape and the bounds, so splitting at the separators recovers the parts.
    rest, _, count = key.partition("~")
    rest, _, segments = rest.partition("#")
    node, bracket, indexes = rest.partition("[")
    return InstanceKey(
        node,
        tuple(int(index) for index in indexes[:-1].split("][")) if bracket else (),
        tuple(segments.split(".")) if segments else (),
        int(count) if count else None,
    )


def format_instance(key: InstanceKey) -> str:
    """The canonical text of a parsed key; the inverse of split_instance."""
    if not isinstance(key, InstanceKey):
        raise InvalidInstanceKey("Format an InstanceKey")
    text = key.node_id + "".join(f"[{index}]" for index in key.indexes)
    if key.segments:
        text += "#" + ".".join(key.segments)
    if key.count is not None:
        text += f"~{key.count}"
    return text


def instance_view(key: str) -> InstanceView:
    """API view fields for a stored key, after checking that it is canonical."""
    parsed = split_instance(key)
    return InstanceView(parsed.node_id, "" if key == parsed.node_id else key, list(parsed.indexes))
