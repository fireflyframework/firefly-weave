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

"""Domain errors carry only deliberate, safe public details."""

from typing import Any


class CatalogError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.result = result
        # Whole seconds a client should wait before sending a refused request again.
        self.retry_after = retry_after


def ir_unsupported(missing: tuple[str, ...]) -> CatalogError:
    """The answer when this platform does not run an artifact's IR version or language features."""
    return CatalogError(
        422,
        "WV-IR-UNSUPPORTED",
        "This platform does not run the IR version or language features of this workflow",
        result={"reason": "ir_unsupported", "missing_features": list(missing)},
    )


# Admission rejections: the operation ran nothing, so the identical call may be sent again.
CAPACITY_CODES = frozenset({"WV-OPERATION-CAPACITY", "WV-REQUEST-CAPACITY"})


def capacity_rejected(error: BaseException) -> bool:
    return isinstance(error, CatalogError) and error.status == 429 and error.code in CAPACITY_CODES
