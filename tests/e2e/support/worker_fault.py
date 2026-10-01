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

"""Test-only installed worker launcher: stop after a real accepted receiver effect."""

import asyncio
import importlib.util
import os
from pathlib import Path

from firefly_weave.sdk.worker import Worker


class FaultWorker(Worker):
    def __init__(self, transport, handlers, concurrency):
        async def accepted_then_exit(lease):
            await handlers["example-record@1.0.0"](lease)
            with os.fdopen(
                os.open("/tmp/crashed-lease.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as stream:
                stream.write(lease.model_dump_json())
            os._exit(75)

        super().__init__(transport, {"example-record@1.0.0": accepted_then_exit}, concurrency)


if __name__ == "__main__":
    path = Path("/opt/weave/examples/worker/main.py")
    spec = importlib.util.spec_from_file_location("installed_worker_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.Worker = FaultWorker
    asyncio.run(module.main())
