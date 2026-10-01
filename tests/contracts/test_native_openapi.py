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

"""Weave's optional native documentation boundary, using real contract types."""

import json
import subprocess
import sys
from typing import Annotated
from uuid import UUID

from pydantic import Field, TypeAdapter

from firefly_weave.contracts.catalog import Draft, DraftRequest
from firefly_weave.contracts.definitions import Definition, ResourceName
from firefly_weave.contracts.workers import CompletionAcknowledgment


def routes():
    from pyfly.web import (
        OpenAPIHeader,
        OpenAPIOperation,
        OpenAPIParameter,
        OpenAPIRequestBody,
        OpenAPIResponse,
        RouteMetadata,
    )

    return [
        RouteMetadata(
            "/drafts/{identifier}",
            "PUT",
            200,
            None,
            "save_draft",
            operation=OpenAPIOperation(
                operation_id="drafts.save",
                parameters=[
                    OpenAPIParameter("identifier", "path", UUID),
                    OpenAPIParameter("limit", "query", Annotated[int, Field(ge=1, le=100)], required=False, default=50),
                    OpenAPIParameter(
                        "If-Match", "header", Annotated[str, Field(pattern=r'^"[1-9][0-9]{0,9}"$')], required=False
                    ),
                ],
                request_body=OpenAPIRequestBody({"application/json": DraftRequest}),
                responses={
                    200: OpenAPIResponse("Updated", {"application/json": Draft}, {"ETag": OpenAPIHeader(str)}),
                    201: OpenAPIResponse("Created", {"application/json": Draft}, {"ETag": OpenAPIHeader(str)}),
                    "4XX": OpenAPIResponse("Rejected"),
                },
                replace_responses=True,
                security=[{"bearer": []}],
            ),
        ),
        RouteMetadata("/ack", "GET", 200, None, "ack", return_type=CompletionAcknowledgment),
        RouteMetadata(
            "/definitions",
            "POST",
            200,
            None,
            "definitions",
            request_body_model=TypeAdapter(Definition),
            return_type=TypeAdapter(Definition),
        ),
        RouteMetadata("/names", "GET", 200, None, "names", return_type=ResourceName),
    ]


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def resolve(spec, ref):
    assert ref.startswith("#/")
    target = spec
    for key in ref[2:].split("/"):
        target = target[key.replace("~1", "/").replace("~0", "~")]
    return target


def test_native_optional_contract_preserves_parameters_unions_and_constraints():
    from firefly_weave.contracts.openapi import create_openapi_generator

    generator = create_openapi_generator(
        "Weave compatibility", "1", security_schemes={"bearer": {"type": "http", "scheme": "bearer"}}
    )
    spec = generator.generate(routes())
    assert spec == generator.generate(list(reversed(routes())))
    operation = spec["paths"]["/drafts/{identifier}"]["put"]
    assert operation["operationId"] == "drafts.save"
    assert set(operation["responses"]) == {"200", "201", "4XX"}
    assert operation["security"] == [{"bearer": []}]
    assert operation["responses"]["201"]["headers"]["ETag"]["schema"]["type"] == "string"
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert parameters["identifier"]["schema"]["format"] == "uuid"
    assert parameters["identifier"]["required"] is True
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 100
    assert parameters["limit"]["schema"]["default"] == 50
    assert parameters["If-Match"]["schema"]["pattern"] == r'^"[1-9][0-9]{0,9}"$'
    ack = spec["paths"]["/ack"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert len(resolve(spec, ack["$ref"])["anyOf"]) == 2
    names = spec["paths"]["/names"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert any(n.get("pattern") == r"^[A-Za-z0-9][A-Za-z0-9_.-]*$" for n in nodes(resolve(spec, names["$ref"])))
    discriminators = []
    for node in nodes(spec):
        if "$ref" in node:
            resolve(spec, node["$ref"])
        if "discriminator" in node:
            discriminators.append(node["discriminator"])
            for ref in node["discriminator"].get("mapping", {}).values():
                resolve(spec, ref)
    assert any(d["propertyName"] == "kind" for d in discriminators)


def test_optional_factory_import_and_missing_extra_error_are_offline():
    source = """
import importlib.abc, sys
class NoInfrastructure(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'pyfly', 'httpx', 'starlette', 'sqlalchemy', 'asyncpg'}:
            raise ModuleNotFoundError(fullname, name=fullname.split('.')[0])
sys.meta_path.insert(0, NoInfrastructure())
from firefly_weave.contracts.openapi import create_openapi_generator
try:
    create_openapi_generator('Weave', '1')
except ImportError as exc:
    assert 'firefly-weave[openapi]' in str(exc)
else:
    raise AssertionError('Missing optional dependency did not fail')
from firefly_weave.contracts.schema_export import export_schemas
from firefly_weave.compiler.api import compile_source
assert export_schemas()['workflow']
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_native_document_uses_validation_and_serialization_aliases():
    from pydantic import BaseModel
    from pyfly.web import RouteMetadata

    from firefly_weave.contracts.openapi import create_openapi_generator

    class Alias(BaseModel):
        value: int = Field(validation_alias="input", serialization_alias="output")

    spec = create_openapi_generator("Weave", "1").generate(
        [RouteMetadata("/alias", "POST", 200, None, "alias", request_body_model=Alias, return_type=Alias)]
    )
    op = spec["paths"]["/alias"]["post"]
    request = resolve(spec, op["requestBody"]["content"]["application/json"]["schema"]["$ref"])
    response = resolve(spec, op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"])
    assert set(request["properties"]) == {"input"}
    assert set(response["properties"]) == {"output"}


if __name__ == "__main__":
    from pathlib import Path

    from firefly_weave.contracts.openapi import create_openapi_generator

    spec = create_openapi_generator(
        "Weave compatibility", "1", security_schemes={"bearer": {"type": "http", "scheme": "bearer"}}
    ).generate(routes())
    Path(sys.argv[1]).write_text(json.dumps(spec, sort_keys=True, separators=(",", ":")) + "\n")


def test_public_customization_preserves_security_and_schema_extension():
    from pydantic.json_schema import GenerateJsonSchema
    from pyfly.web import OpenAPIOperation, RouteMetadata
    from pyfly.web.openapi import OpenAPIGenerator

    from firefly_weave.contracts.openapi import create_openapi_generator

    class ExtendedSchema(GenerateJsonSchema):
        def int_schema(self, schema):
            return {**super().int_schema(schema), "x-weave-probe": True}

    generator = create_openapi_generator(
        "Custom",
        "2",
        "Offline public options",
        schema_generator=ExtendedSchema,
        security_schemes={"bearer": {"type": "http", "scheme": "bearer"}},
        security=[{"bearer": []}],
    )
    assert type(generator) is OpenAPIGenerator
    spec = generator.generate(
        [
            RouteMetadata("/protected", "GET", 200, None, "protected", return_type=int),
            RouteMetadata("/public", "GET", 200, None, "public", operation=OpenAPIOperation(security=[])),
        ]
    )
    assert spec["info"]["description"] == "Offline public options"
    assert spec["security"] == [{"bearer": []}]
    assert spec["paths"]["/public"]["get"]["security"] == []
    assert spec["paths"]["/protected"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "x-weave-probe"
    ]


def test_native_contract_bytes_are_stable_between_fresh_processes(tmp_path):
    from pathlib import Path

    outputs = [tmp_path / f"openapi-{index}.json" for index in range(2)]
    for output in outputs:
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), str(output)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
    assert outputs[0].read_bytes() == outputs[1].read_bytes(), (
        "Native OpenAPI must not expose process-dependent schema names"
    )
