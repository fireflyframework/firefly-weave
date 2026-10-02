# Copyright 2026 Firefly Software Foundation.
# Author: Firefly Software Foundation
# SPDX-License-Identifier: Apache-2.0
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
"""Code-owned optional provider catalog; operator selection precedes imports.

Only exact first-party declarations may use reserved adapter identities. Supporting
services are classes resolved by the application's one native container.
"""

from dataclasses import dataclass

from firefly_weave.connectors.packages import PackageDocument, PackageMetadata


@dataclass(frozen=True)
class BuiltinDeclaration:
    entry_point: str
    service: str
    verifier_service: str | None
    supporting_services: tuple[str, ...] = ()


BUILTIN_DECLARATIONS = {
    "weave-email": BuiltinDeclaration(
        "firefly_weave.connectors.email:package",
        "firefly_weave.connectors.email:EmailConnector",
        None,
    ),
    "weave-whatsapp": BuiltinDeclaration(
        "firefly_weave.connectors.whatsapp:package",
        "firefly_weave.connectors.whatsapp:WhatsAppConnector",
        "firefly_weave.providers.whatsapp:WhatsAppVerifier",
        ("firefly_weave.providers.whatsapp_status:WhatsAppStatusService",),
    ),
    "weave-telegram": BuiltinDeclaration(
        "firefly_weave.connectors.telegram:package",
        "firefly_weave.connectors.telegram:TelegramConnector",
        "firefly_weave.providers.telegram:TelegramVerifier",
    ),
    "weave-teams": BuiltinDeclaration(
        "firefly_weave.connectors.teams:package",
        "firefly_weave.connectors.teams:TeamsConnector",
        "firefly_weave.providers.teams.bridge:TeamsVerifier",
        (
            "firefly_weave.connections.machine_tokens:MachineTokenService",
            "firefly_weave.providers.teams.references:TeamsReferences",
        ),
    ),
}


class BuiltinPackageMetadata(PackageMetadata):
    def validate_identity(self, doc: PackageDocument) -> None:
        expected = BUILTIN_DECLARATIONS.get(doc.manifest.spec.adapter)
        if (
            expected is None
            or doc.distribution != "firefly-weave"
            or doc.distribution_version is None
            or doc.service != expected.service
            or doc.verifier_service != expected.verifier_service
        ):
            raise ValueError("Invalid first-party declaration")
