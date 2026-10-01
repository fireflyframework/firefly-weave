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

"""Redacted native controller error boundary."""

from pydantic import ValidationError
from pyfly.web import controller_advice, exception_handler
from starlette.responses import JSONResponse

from firefly_weave.access.authorization import AccessDenied
from firefly_weave.definitions.models import CatalogError
from firefly_weave.operations.debug.models import DebugError


@controller_advice
class ErrorAdvice:
    @exception_handler(AccessDenied)
    async def access_denied(self, error: AccessDenied) -> JSONResponse:
        return JSONResponse({"code": "WV-FORBIDDEN", "message": "Access denied"}, status_code=403)

    @exception_handler(CatalogError)
    async def catalog_error(self, error: CatalogError) -> JSONResponse:
        body = {"code": error.code, "message": error.message}
        if error.result is not None:
            return JSONResponse({**body, "result": error.result}, status_code=error.status)
        return JSONResponse(body, status_code=error.status)

    @exception_handler(ValidationError)
    async def invalid_contract(self, error: ValidationError) -> JSONResponse:
        return JSONResponse({"code": "WV-VALIDATION", "message": "Invalid request contract"}, status_code=422)

    @exception_handler(DebugError)
    async def debug_error(self, error: DebugError) -> JSONResponse:
        return JSONResponse(
            {"code": error.code, "message": "Invalid or over-budget simulation command"}, status_code=422
        )

    @exception_handler(ValueError)
    async def invalid_value(self, error: ValueError) -> JSONResponse:
        return JSONResponse({"code": "WV-VALIDATION", "message": "Invalid request value"}, status_code=422)

    @exception_handler(Exception)
    async def unexpected(self, error: Exception) -> JSONResponse:
        return JSONResponse({"code": "WV-INTERNAL", "message": "Request failed"}, status_code=500)
