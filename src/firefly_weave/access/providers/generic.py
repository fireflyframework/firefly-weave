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

"""Explicit claim profiles; normalized claims are descriptive, never local grants."""

from dataclasses import dataclass

from firefly_weave.access.models import ExternalClaims, VerifiedIdentity


@dataclass(frozen=True)
class GenericClaimsMapper:
    role_path: tuple[str, ...] = ("roles",)
    scope_claim: str = "scope"

    def map(self, identity: VerifiedIdentity) -> ExternalClaims:
        value = identity.claims
        for part in self.role_path:
            value = value.get(part, {}) if isinstance(value, dict) else {}
        roles = tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()
        scope = identity.claims.get(self.scope_claim, "")
        return ExternalClaims(
            subject=identity.subject,
            client_id=identity.client_id,
            application_roles=roles if identity.actor_kind == "application" else (),
            delegated_scopes=tuple(scope.split()) if identity.actor_kind == "human" and isinstance(scope, str) else (),
        )
