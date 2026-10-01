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

"""Generate owner-only local credentials once; never replace an existing secret file."""

import argparse
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / ".env.weave")
path = parser.parse_args().output
password = secrets.token_urlsafe(36)
port = int(os.environ.get("WEAVE_POSTGRES_PORT", "55432"))
content = (
    f"WEAVE_POSTGRES_PORT={port}\n"
    f"WEAVE_POSTGRES_PASSWORD={password}\n"
    f"WEAVE_DATABASE_URL=postgresql+asyncpg://weave_b1_owner:{password}@127.0.0.1:{port}/weave_dev\n"
    f"WEAVE_TEST_DATABASE_URL=postgresql+asyncpg://weave_b1_owner:{password}@127.0.0.1:{port}/weave_b1_control\n"
)
with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
    output.write(content)
print("Generated owner-only PostgreSQL configuration; existing credentials are never overwritten.")
