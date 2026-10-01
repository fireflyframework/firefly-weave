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

"""Optional native documentation generation; importing this module stays offline."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pydantic.json_schema import GenerateJsonSchema

from firefly_weave.contracts.schema_export import _ContractSchemaGenerator

if TYPE_CHECKING:
    from pyfly.web.openapi import OpenAPIGenerator


def create_openapi_generator(
    title: str,
    version: str,
    description: str = "",
    *,
    schema_generator: type[GenerateJsonSchema] = _ContractSchemaGenerator,
    security_schemes: Mapping[str, dict[str, Any]] | None = None,
    security: Sequence[Mapping[str, Sequence[str]]] | None = None,
) -> OpenAPIGenerator:
    """Return PyFly's public generator with Weave's constraint-preserving policy.

    Callers supply public RouteMetadata to ``generate``. Documentation security
    describes the API and does not replace runtime authentication or authority.
    """
    try:
        from pyfly.web.openapi import OpenAPIGenerator
    except ModuleNotFoundError as exc:
        if exc.name != "pyfly":
            raise
        raise ImportError("OpenAPI generation requires the optional firefly-weave[openapi] dependency") from exc

    return OpenAPIGenerator(
        title,
        version,
        description,
        schema_generator=schema_generator,
        security_schemes=security_schemes,
        security=security,
    )


def export_openapi() -> dict[str, Any]:
    """Offline full product contract through supported native metadata only."""
    generator = create_openapi_generator(
        "Firefly Weave",
        "1",
        security_schemes={
            "bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
            "webhookSignature": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Weave-Signature",
                "description": (
                    "HMAC signature bound to timestamp, event ID and exact request bytes; "
                    "replay receipt belongs to one immutable trigger revision."
                ),
            },
        },
    )
    from pyfly.web import RouteMetadata

    from firefly_weave.contracts.surface import OPERATIONS

    return generator.generate(
        [
            RouteMetadata(spec.canonical_path, spec.method, spec.statuses[0], None, spec.id, operation=spec.native())
            for spec in OPERATIONS.values()
        ]
    )
