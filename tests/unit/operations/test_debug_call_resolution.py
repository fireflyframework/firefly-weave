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

"""Server debug sessions resolve the published versions a workflow calls, as deep as a session can run them."""

import asyncio
import json

from firefly_weave.compiler.api import compile_source, import_artifact
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.language_features import MAX_CALL_DEPTH
from firefly_weave.definitions.calls import called_dependencies
from firefly_weave.operations.debug.models import DebugCreate
from firefly_weave.operations.debug.store import published_workflows

CUSTOMER = {"type": "object", "properties": {"customerId": {"type": "string"}}, "required": ["customerId"]}


def workflow(name, calls=None):
    """``name``, which calls ``calls`` (or returns its input), callable unless it is the simulated workflow."""
    steps = []
    output = {"ref": "/input"}
    if calls is not None:
        with_ = {"object": {"customerId": {"ref": "/input/customerId"}}}
        steps = [{"id": "next", "kind": "callWorkflow", "uses": calls + "@1.0.0", "with": with_}]
        output = {"ref": "/steps/next/output"}
    spec = {"inputSchema": CUSTOMER, "outputSchema": CUSTOMER, "steps": steps, "output": output}
    if name != "orders":
        spec["callable"] = {}
    return {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": name, "version": "1.0.0"},
        "spec": spec,
    }


def compiled(document, called=None):
    catalog = CatalogSnapshot.from_definitions([load_definition(called)] if called is not None else [])
    result = compile_source(json.dumps(document), format="json", catalog=catalog)
    assert result.ok, result.to_bytes()
    return json.loads(result.artifact.to_bytes())


class Published:
    """Published versions by reference, answering the version query as the server's repository does."""

    def __init__(self, versions):
        self.versions = versions
        self.read = []

    async def rows(self, sql, **values):
        reference = f"{values['name']}@{values['version']}"
        self.read.append(reference)
        if reference not in self.versions:
            return []
        digest, artifact = self.versions[reference]
        return [{"definition_digest": digest, "artifact": artifact}]


def test_resolution_loads_called_versions_as_deep_as_a_session_runs_them_and_no_deeper():
    # orders -> level-1 -> ... -> level-9: eight levels of calls a session can run, and one more.
    names = [f"level-{depth}" for depth in range(1, MAX_CALL_DEPTH + 2)]
    documents = {
        name: workflow(name, names[index + 1] if index + 1 < len(names) else None) for index, name in enumerate(names)
    }
    root = compiled(workflow("orders", names[0]), documents[names[0]])
    versions = {}
    caller = root
    for index, name in enumerate(names):
        artifact = compiled(documents[name], documents[names[index + 1]] if index + 1 < len(names) else None)
        reference = name + "@1.0.0"
        versions[reference] = (called_dependencies(caller["executable"])[reference], artifact)
        caller = artifact
    body = {"artifact": root, "mocks": {}, "input": {"customerId": "c-1"}, "now": "2026-10-20T00:00:00Z"}
    request = DebugCreate.model_validate_json(json.dumps(body | {"resolve_workflows": True}))
    repository = Published(versions)
    resolved = asyncio.run(published_workflows(repository, request, import_artifact(root)))
    expected = [name + "@1.0.0" for name in names[:MAX_CALL_DEPTH]]
    assert sorted(resolved) == sorted(expected) and len(resolved) == MAX_CALL_DEPTH
    assert repository.read == expected
