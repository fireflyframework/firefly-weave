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

"""API-owned access to the operator's reloadable AI approval policy."""

from pathlib import Path

from pyfly.container import service

from firefly_weave.ai_policy import AIPolicy, PolicyFile, PolicyInvalid
from firefly_weave.definitions.models import CatalogError
from firefly_weave.private_origins import PrivateOrigins


@service
class APIAIPolicy:
    def __init__(self, path: Path | None = None, origins: PrivateOrigins | None = None) -> None:
        self.path = path
        self.origins = origins if origins is not None else PrivateOrigins.empty()
        self._file: PolicyFile | None = None

    def current(self) -> AIPolicy | None:
        if self.path is None:
            return None
        try:
            if self._file is None:
                self._file = PolicyFile(self.path, self.origins)
            return self._file.current()
        except (PolicyInvalid, OSError):
            self._file = None
            raise CatalogError(503, "WV-AI-POLICY", "The AI policy is unavailable") from None
