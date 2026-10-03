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

"""Public client configuration built once from the administrator's server settings.

Each published sign-in option pairs one `WEAVE_CLIENT_SIGN_IN` entry with its
configured OIDC provider. The issuer and loopback trust come only from that
provider, never from request input, and verifier internals (JWKS location,
audience, claim mapping, application clients) are never published.
"""

from pyfly.container import service

from firefly_weave.contracts.client_configuration import ClientConfiguration
from firefly_weave.settings import Settings


@service
class ClientConfigurationService:
    def __init__(self, settings: Settings) -> None:
        # Settings already rejected anything unpublishable; every read returns this same frozen document.
        self._configuration = ClientConfiguration(
            display_name=settings.display_name,
            sign_in=[entry.option(settings.sign_in_provider(entry)) for entry in settings.client_sign_in],
        )

    def read(self) -> ClientConfiguration:
        return self._configuration
