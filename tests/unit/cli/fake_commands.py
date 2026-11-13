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

"""A stand-in for the platform's command runner: records every call and answers from a table.

`FakeCommands` replaces `firefly_weave.sdk.platform._run`. Each call records its name, argv, stdin and environment,
and, as the real runner does, writes a private log file with the argv and the output in the installation
directory, so tests can check that no secret reaches argv, the environment or a log. Answers are keyed by tokens
that must appear in argv in that order; the longest matching key answers. An unmatched call fails the test.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

Answer = bytes | str | BaseException | Callable[["Call"], bytes | str]


@dataclass
class Call:
    name: str
    argv: list[str]
    stdin: bytes | None
    env: dict[str, str] | None
    timeout: float


@dataclass
class FakeCommands:
    directory: Path
    calls: list[Call] = field(default_factory=list)
    answers: list[tuple[tuple[str, ...], Answer]] = field(default_factory=list)

    def answer(self, tokens: Sequence[str], output: Answer) -> None:
        """Answer calls whose argv holds these tokens in order; a later answer for the same tokens wins."""
        self.answers = [item for item in self.answers if item[0] != tuple(tokens)]
        self.answers.append((tuple(tokens), output))

    def __call__(
        self,
        state: dict[str, Any],
        name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 300,
        stdin: bytes | None = None,
    ) -> bytes:
        call = Call(name, list(command), stdin, env, timeout)
        self.calls.append(call)
        matches = [item for item in self.answers if _within(item[0], command)]
        if not matches:
            raise AssertionError(f"Unexpected command {command}")
        output = max(matches, key=lambda item: len(item[0]))[1]
        if isinstance(output, BaseException):
            self._log(name, command, b"")
            raise output
        result = output(call) if callable(output) else output
        data = result.encode() if isinstance(result, str) else result
        self._log(name, command, data)
        return data

    def argv(self, executable: str) -> list[list[str]]:
        """Every recorded argv that runs this executable."""
        return [call.argv for call in self.calls if call.argv[0] == executable]

    def logs(self) -> list[Path]:
        return sorted(self.directory.glob("*.log"))

    def _log(self, name: str, command: list[str], output: bytes) -> None:
        path = self.directory / f"{name}-{uuid4().hex[:8]}.log"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(("\0".join(command) + "\n").encode() + output)


def _within(tokens: tuple[str, ...], argv: list[str]) -> bool:
    position = 0
    for item in argv:
        if position < len(tokens) and item == tokens[position]:
            position += 1
    return position == len(tokens)
