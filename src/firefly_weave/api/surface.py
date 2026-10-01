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

"""Public native mapping/metadata and same-endpoint canonical route aliases."""

from collections.abc import Callable
from typing import Any, TypeVar

from pyfly.web import delete_mapping, get_mapping, openapi_operation, post_mapping, put_mapping
from starlette.applications import Starlette
from starlette.routing import Route

from firefly_weave.contracts.surface import OPERATIONS

F = TypeVar("F", bound=Callable[..., Any])


def operation(identifier: str) -> Callable[[F], F]:
    selected = OPERATIONS[identifier]
    metadata = selected.native()
    mapping = {"GET": get_mapping, "POST": post_mapping, "PUT": put_mapping, "DELETE": delete_mapping}[selected.method]

    def decorate(handler: F) -> F:
        documented = openapi_operation(
            operation_id=identifier,
            summary=metadata.summary,
            description=metadata.description,
            tags=metadata.tags,
            parameters=metadata.parameters,
            request_body=metadata.request_body,
            responses=metadata.responses,
            replace_responses=True,
            security=metadata.security,
        )(handler)
        return mapping(
            selected.path, name="legacy." + identifier if selected.path.startswith("/tenants/") else identifier
        )(documented)

    return decorate


def install_aliases(app: Starlette) -> None:
    routes = [route for route in app.routes if isinstance(route, Route)]
    covered = set()
    aliases = []
    for identifier, spec in OPERATIONS.items():
        name = "legacy." + identifier if spec.path.startswith("/tenants/") else identifier
        matches = [
            route
            for route in routes
            if route.name == name and route.path == spec.path and spec.method in (route.methods or set())
        ]
        if len(matches) != 1:
            raise RuntimeError("Native operation coverage mismatch: " + identifier)
        covered.add(id(matches[0]))
        if spec.canonical_path != spec.path:
            if any(route.path == spec.canonical_path and spec.method in (route.methods or set()) for route in routes):
                raise RuntimeError("Canonical route collision")
            aliases.append(
                Route(spec.canonical_path, endpoint=matches[0].endpoint, methods=[spec.method], name=identifier)
            )
    if any(id(route) not in covered for route in routes):
        raise RuntimeError("Undocumented native route")
    # Static resource routes must precede the legacy catalog {collection} matcher.
    app.router.routes[:] = sorted(
        [*aliases, *app.router.routes],
        key=lambda route: (str(getattr(route, "path", "")).count("{"), -len(str(getattr(route, "path", "")))),
    )
