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

"""Bounded owned process execution with retained private logs and no shell."""

import asyncio
import os
import signal
from contextlib import suppress
from pathlib import Path


class ProcessFailure(RuntimeError):
    """A private diagnostic log retains details omitted from public errors."""


class ProcessOwner:
    def __init__(self, directory: Path, *, limit: int = 2 * 1024 * 1024):
        self.directory = directory
        self.limit = limit
        self.processes = []
        self.drains = {}
        self.failures = {}
        self.logs = []
        self.process_logs = {}
        self.command_counter = 0

    async def spawn(self, name, args, env=None, *, pass_fds=(), cwd=None):
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Safe private log name required")
        path = self.directory / (name + ".log")
        stream = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                env=env,
                cwd=cwd or self.directory,
                pass_fds=pass_fds,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except BaseException:
            stream.close()
            raise
        self.processes.append(process)
        self.logs.append(path)
        self.process_logs[process.pid] = path

        async def drain():
            size = 0
            try:
                while part := await process.stdout.read(65536):
                    remaining = self.limit - size
                    stream.write(part[:remaining])
                    stream.flush()
                    size += len(part)
                    if size > self.limit:
                        self.failures[process.pid] = "Owned child output exceeded limit"
                        if process.returncode is None:
                            self.kill_group(process)
                        await process.wait()
                        return
            finally:
                stream.close()

        self.drains[process.pid] = asyncio.create_task(drain())
        return process

    async def joined(self, process, timeout=30):
        try:
            async with asyncio.timeout(timeout):
                await process.wait()
                await asyncio.shield(self.drains[process.pid])
        except TimeoutError:
            self.kill_group(process)
            async with asyncio.timeout(5):
                await process.wait()
                await asyncio.shield(self.drains[process.pid])
            raise ProcessFailure("Owned command timed out") from None
        if process.pid in self.failures:
            raise ProcessFailure(self.failures[process.pid])
        return process.returncode

    async def command(self, *args, env=None, cwd=None, timeout=60):
        name = "command-" + str(self.command_counter)
        self.command_counter += 1
        process = await self.spawn(name, args, env, cwd=cwd)
        result = await self.joined(process, timeout)
        if result:
            raise ProcessFailure("Owned command failed; inspect its private retained log")
        return self.process_logs[process.pid].read_text()

    def kill_group(self, process):
        # start_new_session assigns a group exclusively owned by this process tree.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    async def close(self):
        failures = []
        for process in reversed(self.processes):
            if process.returncode is None:
                process.terminate()
            try:
                await self.joined(process, timeout=15)
            except (ProcessFailure, TimeoutError) as error:
                failures.append(type(error).__name__)
        if failures:
            raise ProcessFailure("Owned process cleanup reported a bounded failure; inspect retained logs")
