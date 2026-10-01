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

"""Generate isolated local Keycloak secrets; never overwrite persistent credentials."""

import argparse
import os
import secrets
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
keys = (
    "WEAVE_KC_DB_PASSWORD",
    "WEAVE_KC_ADMIN_SECRET",
    "WEAVE_HOST_SECRET",
    "WEAVE_WORKER_SECRET",
    "WEAVE_DENIED_SECRET",
)
content = "".join(f"{key}={secrets.token_urlsafe(36)}\n" for key in keys)
try:
    with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
        stream.write(content)
except OSError:
    raise SystemExit("Secret destination must be new and writable") from None
print("Local identity secrets created with owner-only permissions")
