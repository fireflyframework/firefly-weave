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

"""Infrastructure subprocesses stay bounded and never leak provider output."""

import asyncio
import os
import sys

import pytest

from firefly_weave.deployment_runner.command import CommandError, run_command


@pytest.mark.asyncio
async def test_command_returns_stdout_without_returning_diagnostic_content():
    output = await run_command(
        [sys.executable, "-c", "import sys; print('ready'); print('private detail', file=sys.stderr)"], timeout=2
    )
    assert output == b"ready\n"


@pytest.mark.asyncio
async def test_command_bounds_combined_output_and_hides_failure_contents():
    with pytest.raises(CommandError) as failure:
        await run_command(
            [sys.executable, "-c", "import sys; print('sensitive-provider-message' * 5000, file=sys.stderr)"],
            timeout=2,
            limit=1024,
        )
    assert "sensitive" not in str(failure.value)
    assert failure.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_reap_owned_process(tmp_path, cancel):
    marker = tmp_path / "child.pid"
    code = (
        "import os,time,pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(30)"
    )
    task = asyncio.create_task(run_command([sys.executable, "-c", code], timeout=2 if cancel else 0.15))
    async with asyncio.timeout(2):
        while not marker.exists():
            await asyncio.sleep(0.005)
    pid = int(marker.read_text())
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else CommandError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_stdin_is_explicit_and_shell_expansion_is_never_used():
    output = await run_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1]); print(sys.stdin.read())", "$(not-a-command)"],
        timeout=2,
        stdin=b"owned template",
    )
    assert output == b"$(not-a-command)\nowned template\n"


@pytest.mark.asyncio
async def test_a_process_group_that_refuses_the_kill_still_ends_the_command_cleanly(tmp_path, monkeypatch):
    """macOS can answer killpg on a group whose processes have all exited with EPERM, not ESRCH. A finished command
    still returns its output, a timeout still reports CommandError, and its process is still reaped."""
    from firefly_weave.deployment_runner import command

    killpg = command.os.killpg

    def macos(pid, signum):
        try:
            killpg(pid, signum)
        except ProcessLookupError:
            raise PermissionError(1, "Operation not permitted") from None

    monkeypatch.setattr(command.os, "killpg", macos)
    assert await run_command([sys.executable, "-c", "print('ready')"], timeout=5) == b"ready\n"
    marker = tmp_path / "child.pid"
    code = (
        "import os,time,pathlib; pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(30)"
    )
    task = asyncio.create_task(run_command([sys.executable, "-c", code], timeout=0.5))
    async with asyncio.timeout(2):
        while not marker.exists():
            await asyncio.sleep(0.005)
    with pytest.raises(CommandError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


@pytest.mark.asyncio
async def test_a_running_group_that_refuses_the_kill_fails_at_once_instead_of_waiting(tmp_path, monkeypatch):
    """EPERM while the leader still runs means the kill really failed; the runner reports it rather than waiting."""
    import contextlib
    import signal
    import time

    from firefly_weave.deployment_runner import command

    def refused(pid, signum):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(command.os, "killpg", refused)
    marker = tmp_path / "child.pid"
    code = f"import os, pathlib, time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(5)"
    started = time.monotonic()
    try:
        with pytest.raises(CommandError):
            await run_command([sys.executable, "-c", code], timeout=0.5)
        assert time.monotonic() - started < 3
    finally:
        if marker.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(marker.read_text()), signal.SIGKILL)


def refusing_and_captured(monkeypatch):
    """killpg always refused, and every process the runner starts kept for the test to inspect."""
    from firefly_weave.deployment_runner import command

    def refused(pid, signum):
        raise PermissionError(1, "Operation not permitted")

    started = []
    create = command.asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await create(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(command.os, "killpg", refused)
    monkeypatch.setattr(command.asyncio, "create_subprocess_exec", capture)
    return started


@pytest.mark.asyncio
async def test_a_leader_that_exits_soon_after_a_refused_kill_is_still_reaped(monkeypatch):
    """EPERM can come just before the leader's exit is seen; the runner waits briefly for it, so the process is
    reaped and its pipes are closed, instead of reporting a kill that didn't fail."""
    started = refusing_and_captured(monkeypatch)
    code = "import time; print('x' * 5000, flush=True); time.sleep(0.3)"
    with pytest.raises(CommandError):
        await run_command([sys.executable, "-c", code], timeout=5, limit=100)
    (process,) = started
    assert process.returncode == 0


@pytest.mark.asyncio
async def test_a_refused_kill_never_hides_the_callers_cancellation(tmp_path, monkeypatch):
    """When the caller cancels (here, asyncio.timeout), the runner raises CancelledError even if the cleanup failed,
    so the timeout still reports TimeoutError."""
    import contextlib
    import signal

    refusing_and_captured(monkeypatch)
    marker = tmp_path / "child.pid"
    code = f"import os, pathlib, time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(5)"
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3):
                await run_command([sys.executable, "-c", code], timeout=30)
    finally:
        if marker.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(marker.read_text()), signal.SIGKILL)
