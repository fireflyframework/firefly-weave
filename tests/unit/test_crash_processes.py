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

"""Owned subprocess limits and correlated signals use real local processes."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "weave_crash_process_tests", Path(__file__).parents[1] / "e2e/support/processes.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


async def test_output_budget_kills_only_owned_child(tmp_path):
    owner = module.ProcessOwner(tmp_path, limit=1024)
    with pytest.raises(module.ProcessFailure, match="output"):
        await owner.command(sys.executable, "-I", "-c", "print('x' * 10000)")
    assert owner.processes and all(p.returncode is not None for p in owner.processes)
    assert all(p.stat().st_size <= 1024 for p in tmp_path.glob("*.log"))


async def test_output_budget_holds_when_the_child_group_exited_before_the_kill(tmp_path, monkeypatch):
    # macOS can answer killpg with EPERM, not ESRCH, once every process of the group has exited. The child keeps
    # running after its output, so the budget always kills its group; the kill works, then answers EPERM.
    killpg = module.os.killpg

    def macos(pid, sig):
        killpg(pid, sig)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(module.os, "killpg", macos)
    owner = module.ProcessOwner(tmp_path, limit=1024)
    child = "import sys, time; sys.stdout.write('x' * 10000); sys.stdout.flush(); time.sleep(30)"
    with pytest.raises(module.ProcessFailure, match="output"):
        await asyncio.wait_for(owner.command(sys.executable, "-I", "-c", child), 10)
    assert owner.processes and all(p.returncode is not None for p in owner.processes)


async def test_command_timeout_is_failure_and_retains_log(tmp_path):
    owner = module.ProcessOwner(tmp_path)
    with pytest.raises(module.ProcessFailure, match="timed out"):
        await owner.command(sys.executable, "-I", "-c", "import time; time.sleep(10)", timeout=0.05)
    assert owner.processes[0].returncode is not None
    assert list(tmp_path.glob("*.log"))


async def test_command_spaces_are_argv_values(tmp_path):
    owner = module.ProcessOwner(tmp_path)
    text = "value with spaces ; $(nothing)"
    result = await owner.command(sys.executable, "-I", "-c", "import sys; print(sys.argv[1])", text)
    assert result.strip() == text
    await owner.close()


async def test_descendant_held_pipe_is_bounded_and_closed(tmp_path):
    owner = module.ProcessOwner(tmp_path)
    command = "import subprocess,sys; subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(30)'])"
    with pytest.raises(module.ProcessFailure, match="timed out"):
        await owner.command(sys.executable, "-I", "-c", command, timeout=0.1)
    assert owner.processes[0].returncode is not None


async def test_concurrent_commands_return_their_own_logs(tmp_path):
    import asyncio

    owner = module.ProcessOwner(tmp_path)
    first, second = await asyncio.gather(
        owner.command(sys.executable, "-I", "-c", "import time;time.sleep(0.1);print('first')"),
        owner.command(sys.executable, "-I", "-c", "print('second')"),
    )
    assert first.strip() == "first" and second.strip() == "second"
    await owner.close()
