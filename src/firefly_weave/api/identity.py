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

"""Identity discovery never accepts another actor identifier from the caller."""

from pyfly.container.stereotypes import rest_controller
from pyfly.web import request_mapping
from starlette.requests import Request

from firefly_weave.access.discovery import IdentityDiscoveryService
from firefly_weave.api.surface import operation
from firefly_weave.contracts.identity import IdentityView


@rest_controller
@request_mapping("")
class IdentityController:
    def __init__(self, service: IdentityDiscoveryService) -> None:
        self.service = service

    @operation("identity.read")
    async def read(self, request: Request) -> IdentityView:
        return await self.service.read(request.state.principal)
