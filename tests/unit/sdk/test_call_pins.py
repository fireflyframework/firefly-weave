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

"""Pinning the current activation of each called workflow version, on the client."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firefly_weave.compiler.parser import parse_source
from firefly_weave.contracts.access import Scope
from firefly_weave.contracts.catalog import Activation, ActivationRequest, PublishedVersion
from firefly_weave.contracts.public import Page
from firefly_weave.sdk.call_pins import CallPinError, current_call_pins

SCOPE = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
CALLER = parse_source(Path("examples/language/order-intake.workflow.yaml").read_text(), format="yaml").value
CALLED, OLDER = uuid4(), uuid4()


def version(identifier, name, number) -> PublishedVersion:
    return PublishedVersion(
        id=identifier, kind="Workflow", name=name, version=number, digest="a" * 64, definition_digest="b" * 64
    )


def activation(name, version_id, revision, *, retired=False) -> Activation:
    request = ActivationRequest(scope=SCOPE, version_id=version_id, artifact_digest="a" * 64)
    return Activation(id=uuid4(), revision=revision, name=name, request=request, retired=retired)


def client(activations: list[Activation]):
    async def export_definition(collection, identifier):
        return SimpleNamespace(document=CALLER)

    async def list_definitions(collection, *, limit, cursor):
        # Two pages, so the walk follows the cursor.
        if cursor is None:
            return Page[PublishedVersion](items=[version(OLDER, "notify-customer", "0.9.0")], next_cursor="next")
        return Page[PublishedVersion](items=[version(CALLED, "notify-customer", "1.0.0")])

    async def list_activations(*, limit, cursor):
        return Page[Activation](items=activations)

    return SimpleNamespace(
        export_definition=export_definition, list_definitions=list_definitions, list_activations=list_activations
    )


async def test_the_latest_activation_of_the_called_name_is_pinned_when_it_runs_the_called_version():
    current = activation("notify-customer", CALLED, 3)
    sdk = client([activation("notify-customer", OLDER, 2), current])
    request = ActivationRequest(scope=SCOPE, version_id=uuid4(), artifact_digest="c" * 64)
    pinned = await current_call_pins(sdk, request)  # type: ignore[arg-type]
    assert pinned.workflow_activation_ids == {"notify-customer@1.0.0": current.id}


async def test_a_pin_already_in_the_request_is_kept():
    chosen = uuid4()
    request = ActivationRequest(
        scope=SCOPE,
        version_id=uuid4(),
        artifact_digest="c" * 64,
        workflow_activation_ids={"notify-customer@1.0.0": chosen},
    )
    assert (await current_call_pins(client([]), request)).workflow_activation_ids == {"notify-customer@1.0.0": chosen}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "activations",
    [
        [],
        [activation("notify-customer", OLDER, 4)],
        [activation("notify-customer", CALLED, 4, retired=True)],
    ],
    ids=["none", "another-version", "retired"],
)
async def test_a_called_version_without_a_current_activation_is_refused_with_a_fix(activations):
    request = ActivationRequest(scope=SCOPE, version_id=uuid4(), artifact_digest="c" * 64)
    with pytest.raises(CallPinError, match="No current activation of notify-customer runs notify-customer@1.0.0"):
        await current_call_pins(client(activations), request)  # type: ignore[arg-type]


def test_activation_create_offers_to_pin_the_current_calls():
    from firefly_weave.cli.remote import command

    options = {option for parameter in command("activations.create").params for option in parameter.opts}
    assert "--pin-current-calls" in options
    assert "--pin-current-calls" not in {o for p in command("activations.list").params for o in p.opts}
