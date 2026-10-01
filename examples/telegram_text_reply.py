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

"""Compile an offline Telegram text-trigger/reply bundle; never contact Telegram.

Run with the server dependencies installed. Publish and activate the emitted
resources through the normal catalog/worker/connection APIs before creating the
source with returned UUIDs. Provider ingress dispatches original human text only.
"""

import json
from typing import Any

from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.connectors.telegram import package
from firefly_weave.contracts.definitions import load_definition
from firefly_weave.contracts.providers import provider_schema_digest


def documents() -> dict[str, Any]:
    metadata = package.metadata.model
    reply = metadata.manifest.spec.actions["reply-text"]
    action = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Action",
        "metadata": {"name": "telegram.reply", "version": "1.0.0"},
        "spec": {
            "implementation": {
                "kind": "connector",
                "uses": "weave-telegram@1.0.0",
                "action": "reply-text",
                "config": {},
            },
            "connection": {"connector": "weave-telegram@1.0.0"},
            "inputSchema": reply.input_schema,
            "outputSchema": reply.output_schema,
            "sideEffect": "non_idempotent",
            "timeoutSeconds": 30,
        },
    }
    workflow = {
        "apiVersion": "weave/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "telegram-text-reply", "version": "1.0.0"},
        "spec": {
            "inputSchema": metadata.event_schemas["telegram-update"],
            "outputSchema": reply.output_schema,
            "connections": {"telegram": {"connector": "weave-telegram@1.0.0"}},
            "steps": [
                {
                    "id": "reply",
                    "kind": "action",
                    "uses": "telegram.reply@1.0.0",
                    "connection": "telegram",
                    "with": {
                        "object": {
                            "chat_id": {
                                "op": {"name": "coalesce", "args": [{"ref": "/input/chat_id"}, {"literal": "-1"}]}
                            },
                            "message_id": {
                                "op": {"name": "coalesce", "args": [{"ref": "/input/message_id"}, {"literal": "1"}]}
                            },
                            "text": {"literal": "Received."},
                        }
                    },
                }
            ],
            "output": {"ref": "/steps/reply/output"},
        },
    }
    catalog = CatalogSnapshot.from_definitions(
        [metadata.manifest, load_definition(action)], tasks=metadata.capabilities, adapters=["weave-telegram"]
    )
    for document in (action, workflow):
        result = compile_source(document, format="object", catalog=catalog)
        if not result.ok:
            raise ValueError(result.to_bytes().decode())
    return {
        "connector": metadata.manifest.model_dump(by_alias=True),
        "action": action,
        "workflow": workflow,
        "source": {
            "name": "telegram-text",
            "provider": "telegram",
            "package": "firefly-weave",
            "package_version": metadata.distribution_version,
            "adapter_version": metadata.version,
            "schema_digest": provider_schema_digest(metadata.event_schemas, metadata.dispatch_event_kinds),
            "connection_revision_id": "REPLACE_WITH_CREATED_REVISION_UUID",
            "kind": "run",
            "activation_id": "REPLACE_WITH_ACTIVATION_UUID",
            "mapping": {"ref": "/payload"},
            "policy": {"account_id": "123456", "mode": "webhook", "allowed_chat_ids": ["-987"]},
        },
    }


if __name__ == "__main__":
    print(json.dumps(documents(), indent=2))
