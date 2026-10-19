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

"""Pin the current activation of every workflow version an activation request's workflow calls.

The platform never chooses pins: this resolves them on the client, from the catalog and the environment's
activations, and the activation request carries them like any other pin.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from firefly_weave.compiler.calls import called_references
from firefly_weave.contracts.catalog import Activation, ActivationRequest, PublishedVersion

if TYPE_CHECKING:
    from firefly_weave.sdk.client import WeaveClient


class CallPinError(ValueError):
    """A called workflow version has no current activation in the environment."""


async def current_call_pins(client: "WeaveClient", request: ActivationRequest) -> ActivationRequest:
    """``request`` with a pin to the current activation of each called version it does not pin yet.

    The current activation of a workflow is the latest activation revision of its name; it must run the called
    version and must not be retired. Pins already in the request are kept.
    """
    exported = await client.export_definition("workflows", request.version_id)
    missing = sorted(set(called_references(exported.document)) - set(request.workflow_activation_ids))
    if not missing:
        return request
    versions: dict[str, UUID] = {}
    cursor = None
    while True:
        page = await client.list_definitions("workflows", limit=100, cursor=cursor)
        for item in page.items:
            if isinstance(item, PublishedVersion):
                versions[f"{item.name}@{item.version}"] = item.id
        cursor = page.next_cursor
        if cursor is None:
            break
    latest: dict[str, Activation] = {}
    cursor = None
    while True:
        activations = await client.list_activations(limit=100, cursor=cursor)
        for activation in activations.items:
            if activation.name not in latest or activation.revision > latest[activation.name].revision:
                latest[activation.name] = activation
        cursor = activations.next_cursor
        if cursor is None:
            break
    pins = dict(request.workflow_activation_ids)
    for reference in missing:
        name = reference.partition("@")[0]
        current = latest.get(name)
        if current is None or current.retired or current.request.version_id != versions.get(reference):
            raise CallPinError(
                f"No current activation of {name} runs {reference}; activate that version, or pin an activation "
                "in workflow_activation_ids"
            )
        pins[reference] = current.id
    return request.model_copy(update={"workflow_activation_ids": pins})
