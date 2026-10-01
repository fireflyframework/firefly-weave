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

"""Documentation metadata cannot change Weave's native binding or authority."""

import inspect
from uuid import UUID, uuid4

import pytest
from test_definitions import author as author

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def documented_draft():
    from pyfly.web import OpenAPIHeader, OpenAPIParameter, OpenAPIRequestBody, OpenAPIResponse, openapi_operation

    from firefly_weave.api.definitions import DefinitionController
    from firefly_weave.contracts.catalog import Draft, DraftRequest

    handler = DefinitionController.save_draft
    attributes, signature, annotations = (
        handler.__dict__.copy(),
        inspect.signature(handler),
        handler.__annotations__.copy(),
    )
    try:
        decorated = openapi_operation(
            operation_id="drafts.save",
            parameters=[OpenAPIParameter(name, "path", UUID) for name in ("tenant", "project", "identifier")],
            request_body=OpenAPIRequestBody({"application/json": DraftRequest}),
            responses={
                status: OpenAPIResponse("Saved", {"application/json": Draft}, {"ETag": OpenAPIHeader(str)})
                for status in (200, 201)
            },
            replace_responses=True,
            security=[],
        )(handler)
        assert decorated is handler
        assert inspect.signature(handler) == signature and handler.__annotations__ == annotations
        yield
    finally:
        handler.__dict__.clear()
        handler.__dict__.update(attributes)


def test_actual_controller_metadata_acquires_no_resources_or_descriptors(monkeypatch):
    from pyfly.context.application_context import ApplicationContext
    from pyfly.core.config import Config
    from pyfly.web.adapters.starlette.controller import ControllerRegistrar

    from firefly_weave.api.definitions import DefinitionController
    from firefly_weave.contracts.openapi import create_openapi_generator

    class ForbiddenDescriptor:
        def __get__(self, instance, owner):
            raise AssertionError("Descriptor evaluated during metadata export")

    monkeypatch.setattr(DefinitionController, "documentation_probe", ForbiddenDescriptor(), raising=False)
    context = ApplicationContext(Config({}))
    context.register_bean(DefinitionController)

    def forbidden(*args, **kwargs):
        raise AssertionError("Bean acquired during metadata export")

    monkeypatch.setattr(context, "get_bean", forbidden)
    metadata = ControllerRegistrar().collect_route_metadata(context)
    route = next(r for r in metadata if r.handler_name == "save_draft")
    spec = create_openapi_generator("Weave", "1").generate([route])
    operation = spec["paths"][route.path]["put"]
    assert operation["operationId"] == "drafts.save"
    assert set(operation["responses"]) == {"200", "201"}
    assert operation["security"] == []
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/DraftRequest")


async def test_manual_handler_retains_etag_status_validation_and_authority(author, headers, other_headers, project_url):
    client = author[0]
    url = project_url + "/drafts/" + str(uuid4())
    denied = await client.put(url, json={"document": {}})
    assert denied.status_code == 401 and denied.headers["www-authenticate"] == "Bearer"
    cross = await client.put(url, headers=other_headers, json={"document": {}})
    assert cross.status_code == 403
    created = await client.put(url, headers=headers, json={"document": {"value": 1}})
    assert created.status_code == 201 and created.headers["etag"] == '"1"'
    assert created.json()["document"] == {"value": 1}
    changed = await client.put(url, headers={**headers, "If-Match": '"1"'}, json={"document": {"value": 2}})
    assert changed.status_code == 200 and changed.headers["etag"] == '"2"'
    assert changed.json()["document"] == {"value": 2}
    stale = await client.put(url, headers={**headers, "If-Match": '"1"'}, json={"document": {}})
    assert stale.status_code == 412
    invalid = await client.put(url, headers={**headers, "If-Match": "unquoted"}, json={"document": {}})
    assert invalid.status_code == 422
    malformed = await client.put(url, headers=headers, json={"unexpected": True})
    assert malformed.status_code == 422
    assert all("X-Weave-Request-ID" in response.headers for response in (created, changed, stale, invalid))
