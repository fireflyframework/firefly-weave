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
import contextlib
import math
import os
import signal
from dataclasses import dataclass
from pathlib import Path

# The longest one command may run: within the runner's 600 s job budget, long enough to stop drained workers
# with their 240 s grace.
MAX_COMMAND_SECONDS = 600


class CommandError(Exception):
    def __init__(self) -> None:
        super().__init__("Infrastructure command failed; inspect the target using operator access.")


async def _reap(process: asyncio.subprocess.Process) -> None:
    # The command owns a new session, including any child processes it creates. A group that is already gone needs
    # nothing; once all its processes have exited, macOS can answer EPERM instead of ESRCH. EPERM while the leader
    # still runs means the kill really failed, and waiting for it could last forever.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # The leader may have exited a moment before its exit was seen: wait briefly, so it is reaped and its pipes
        # are closed, and refuse only when it really keeps running.
        try:
            await asyncio.wait_for(process.wait(), 1)
        except TimeoutError:
            raise CommandError() from None
    await process.wait()


async def _finish_cleanup(process: asyncio.subprocess.Process) -> None:
    cleanup = asyncio.create_task(_reap(process))
    # A caller already being cancelled (asyncio.timeout, a TaskGroup) gets its CancelledError whatever the cleanup
    # found, so its cancellation keeps working.
    current = asyncio.current_task()
    cancelled = current is not None and current.cancelling() > 0
    while not cleanup.done():
        try:
            # Waits without cancelling the cleanup and without raising its error here.
            await asyncio.wait({cleanup})
        except asyncio.CancelledError:
            cancelled = True
    failure = cleanup.exception()
    if cancelled:
        raise asyncio.CancelledError
    if failure is not None:
        raise failure


def _fixed(argv: list[str], timeout: float) -> bool:
    """A bounded argument list for an absolute executable, and a bounded time, on POSIX."""
    return (
        os.name == "posix"
        and bool(argv)
        and len(argv) <= 128
        and all(isinstance(value, str) and len(value) <= 4096 and "\0" not in value for value in argv)
        and Path(argv[0]).is_absolute()
        and math.isfinite(timeout)
        and 0 < timeout <= MAX_COMMAND_SECONDS
    )


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
    if not _fixed(argv, timeout) or not 1 <= limit <= 1024 * 1024 or (stdin is not None and len(stdin) > 256 * 1024):
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


# Reading logs keeps the last 512 KiB of each stream and reads at most 8 MiB in all before it stops the command.
OUTPUT_KEEP = 512 * 1024
OUTPUT_READ = 8 * 1024 * 1024


@dataclass(frozen=True)
class CommandOutput:
    """What a command printed: the last whole lines of stdout and of stderr, and whether earlier output was left out
    or the read stopped at its bound."""

    stdout: bytes
    stderr: bytes
    cut: bool


async def read_output(
    argv: list[str], *, timeout: float = 20, keep: int = OUTPUT_KEEP, limit: int = OUTPUT_READ
) -> CommandOutput:
    """Run fixed adapter argv without a shell and keep the last `keep` bytes of whole lines of each stream.

    For reading logs, where a container's own stderr is output too: unlike `run_command`, both streams are kept, and
    output past `limit` bytes in all stops the command instead of failing it. What was read is returned marked cut,
    without the partial line the stop left. A command that exits with an error, or runs past `timeout`, fails.
    """
    if not _fixed(argv, timeout) or not 1 <= keep <= limit <= 64 * 1024 * 1024:
        raise CommandError()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError):
        raise CommandError() from None
    count = 0
    cut = stopped = False

    async def consume(stream: asyncio.StreamReader) -> bytes:
        nonlocal count, cut, stopped
        kept = bytearray()
        while not stopped and (chunk := await stream.read(16384)):
            count += len(chunk)
            kept.extend(chunk)
            if len(kept) > keep:
                # Drop the oldest bytes, then the rest of the line they cut, so the output starts with a whole line.
                del kept[: len(kept) - keep]
                del kept[: kept.find(b"\n") + 1]
                cut = True
            if count > limit:
                cut = stopped = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
        if stopped and not kept.endswith(b"\n"):
            del kept[kept.rfind(b"\n") + 1 :]
        return bytes(kept)

    try:
        async with asyncio.timeout(timeout):
            assert process.stdout is not None and process.stderr is not None
            async with asyncio.TaskGroup() as tasks:
                stdout = tasks.create_task(consume(process.stdout))
                stderr = tasks.create_task(consume(process.stderr))
                status = await process.wait()
            if status != 0 and not stopped:
                raise CommandError()
            return CommandOutput(stdout=stdout.result(), stderr=stderr.result(), cut=cut)
    except Exception:
        raise CommandError() from None
    finally:
        await _finish_cleanup(process)
