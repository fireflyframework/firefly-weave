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
