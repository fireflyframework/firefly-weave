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

"""Bounded infrastructure commands; provider diagnostics never cross the runner."""

from __future__ import annotations

import asyncio
import math
import os
import signal
from contextlib import suppress
from pathlib import Path


class CommandError(Exception):
    def __init__(self) -> None:
        super().__init__("Infrastructure command failed; inspect the target using operator access.")


async def _reap(process: asyncio.subprocess.Process) -> None:
    # The command owns a new session, including any child processes it creates.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def _finish_cleanup(process: asyncio.subprocess.Process) -> None:
    cleanup = asyncio.create_task(_reap(process))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def run_command(
    argv: list[str],
    *,
    timeout: float = 30,
    limit: int = 1024 * 1024,
    stdin: bytes | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> bytes:
    """Run fixed adapter argv without a shell; bound all output, time, and input.

    This POSIX runner owns process cleanup even when the caller is cancelled.
    Adapters, not API payloads, choose executables and argument structure.
    """
    if (
        os.name != "posix"
        or not argv
        or len(argv) > 128
        or any(not isinstance(value, str) or len(value) > 4096 or "\0" in value for value in argv)
        or not Path(argv[0]).is_absolute()
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
        or not 1 <= limit <= 1024 * 1024
        or (stdin is not None and len(stdin) > 256 * 1024)
    ):
        raise CommandError()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            cwd=cwd,
            env=env,
        )
    except (OSError, ValueError):
        raise CommandError() from None
    count = 0

    async def consume(stream: asyncio.StreamReader, *, keep: bool) -> bytes:
        nonlocal count
        output = bytearray()
        while chunk := await stream.read(16384):
            count += len(chunk)
            if count > limit:
                raise CommandError()
            if keep:
                output.extend(chunk)
        return bytes(output)

    async def feed() -> None:
        if process.stdin is not None:
            process.stdin.write(stdin or b"")
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()

    try:
        async with asyncio.timeout(timeout):
            assert process.stdout is not None and process.stderr is not None
            async with asyncio.TaskGroup() as tasks:
                stdout = tasks.create_task(consume(process.stdout, keep=True))
                tasks.create_task(consume(process.stderr, keep=False))
                tasks.create_task(feed())
                status = await process.wait()
            if status != 0:
                raise CommandError()
            return stdout.result()
    except Exception:
        raise CommandError() from None
    finally:
        await _finish_cleanup(process)
