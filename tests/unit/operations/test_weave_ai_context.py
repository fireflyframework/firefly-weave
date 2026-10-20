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
"""Weave AI fits what it sends into the model's context: a profile, a budget split, and trimming in a fixed order."""

import json
import typing

import pytest
import yaml

from firefly_weave.contracts.definitions import Step
from firefly_weave.contracts.lumi import LumiMessage
from firefly_weave.operations import lumi_context as context
from firefly_weave.operations.lumi_context import (
    COMPACT_INPUT_TOKENS,
    COMPACT_MAX_TOKENS,
    GATEWAY_RESERVE_TOKENS,
    GUIDE,
    choose_profile,
    estimate_tokens,
    input_budget,
    outline,
    output_tokens,
    shape,
)


@pytest.mark.parametrize(
    ("tokens", "chosen", "expected"),
    [
        (None, "auto", "full"),
        (8192, "auto", "compact"),
        (16383, "auto", "compact"),
        (16384, "auto", "standard"),
        (65536, "auto", "standard"),
        (65537, "auto", "full"),
        (8192, "full", "full"),
        (200000, "compact", "compact"),
    ],
)
def test_the_profile_follows_the_context_unless_an_administrator_chose_one(tokens, chosen, expected):
    assert choose_profile(tokens, chosen) == expected


def test_the_input_budget_leaves_room_for_the_answer_and_caps_the_compact_profile():
    assert input_budget("compact", 8192, 1024) == COMPACT_INPUT_TOKENS == 4096
    assert input_budget("compact", 4096, 1024) == 3072
    assert input_budget("standard", 32768, 1024) == 31744
    assert input_budget("full", None, 1024) is None


def test_compact_models_answer_in_at_most_five_hundred_twelve_tokens():
    assert output_tokens("compact", 1024) == COMPACT_MAX_TOKENS == 512
    assert output_tokens("compact", 256) == 256
    assert output_tokens("standard", 1024) == output_tokens("full", 1024) == 1024
    assert input_budget("compact", 4096, output_tokens("compact", 1024)) == 3584


def test_the_guide_and_the_gateway_text_fit_a_quarter_of_the_compact_budget():
    # The gateway's tests keep its instructions and reply format within this reserve.
    assert GATEWAY_RESERVE_TOKENS == 512
    assert estimate_tokens(GUIDE) + GATEWAY_RESERVE_TOKENS <= COMPACT_INPUT_TOKENS // 4


def test_the_guide_names_every_step_kind_the_language_has():
    kinds = {
        typing.get_args(member.model_fields["kind"].annotation)[0]
        for member in typing.get_args(typing.get_args(Step.__value__)[0])
    }
    assert kinds and all(f"`{kind}`" in GUIDE for kind in kinds)


def draft(steps=20):
    return yaml.safe_dump(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "support", "version": "1.0.0"},
            "spec": {
                "inputSchema": {"type": "object"},
                "outputSchema": {},
                "steps": [
                    {
                        "id": f"step-{index}",
                        "kind": "action",
                        "uses": f"crm-lookup@1.{index}.0",
                        "with": {"literal": {"note": "private customer detail " * 20}},
                    }
                    for index in range(steps)
                ],
                "output": {"literal": None},
            },
        }
    )


def test_an_outline_names_steps_and_keeps_only_the_selected_step_whole():
    shown = outline(yaml.safe_load(draft()), "step-3")
    assert shown["metadata"] == {"name": "support", "version": "1.0.0"}
    assert shown["steps"][0] == {"id": "step-0", "kind": "action", "uses": "crm-lookup@1.0.0"}
    assert len(shown["steps"]) == 20
    assert shown["selected"]["id"] == "step-3" and "private customer detail" in json.dumps(shown["selected"])
    assert json.dumps(shown).count("private customer detail") == 20


def test_an_outline_walks_into_nested_steps():
    document = {
        "kind": "Workflow",
        "metadata": {"name": "n", "version": "1.0.0"},
        "spec": {
            "steps": [
                {
                    "id": "route",
                    "kind": "switch",
                    "cases": [{"when": {"literal": True}, "steps": [{"id": "inner", "kind": "transform"}]}],
                    "default": {"steps": [{"id": "fallback", "kind": "fail"}]},
                }
            ]
        },
    }
    assert [step["id"] for step in outline(document, None)["steps"]] == ["route", "inner", "fallback"]


HISTORY = [
    LumiMessage(role="user" if index % 2 == 0 else "assistant", content=f"m{index} " * 40) for index in range(16)
]
RUN = {"kind": "run", "id": "r", "data": {"status": "failed", "failed_steps": [{"node_id": "step-3"}]}}


def sent_tokens(shaped, message):
    history = [item.model_dump() for item in shaped.history]
    request = json.dumps(
        {"prompt": message, "context": {"history": history, "attachments": shaped.context}}, ensure_ascii=False
    )
    return estimate_tokens(request) + GATEWAY_RESERVE_TOKENS


def test_a_compact_request_for_a_twenty_step_draft_stays_under_four_thousand_tokens():
    message = "Why does step-3 fail, and how do I fix it?"
    shaped = shape(
        "compact",
        input_budget("compact", 8192, 1024),
        message=message,
        history=HISTORY,
        resources=[RUN],
        draft=("yaml", draft()),
        selected_step="step-3",
        schemas={"workflow": {"type": "object"}},
    )
    assert sent_tokens(shaped, message) <= 4096
    assert shaped.context["profile"] == "compact" and shaped.context["guide"] == GUIDE
    assert "authoringSchemas" not in shaped.context
    assert shaped.context["draft"]["outline"]["selected"]["id"] == "step-3"
    assert len(shaped.history) <= 4 and shaped.history[-1] == HISTORY[-1]
    assert shaped.context["resources"] == [RUN]


def test_standard_sends_a_small_draft_whole_and_only_the_workflow_schema():
    small = draft(3)
    shaped = shape(
        "standard",
        input_budget("standard", 32768, 1024),
        message="Review it",
        history=HISTORY,
        resources=[],
        draft=("yaml", small),
        selected_step=None,
        schemas={"workflow": {"type": "object"}, "action": {}, "connector": {}, "decisiontable": {}},
    )
    assert shaped.context["draft"] == {"format": "yaml", "source": small}
    assert shaped.context["authoringSchemas"] == {"workflow": {"type": "object"}}
    assert len(shaped.history) == 8


def test_standard_outlines_a_draft_over_thirty_two_kibibytes():
    large = draft(120)
    assert len(large.encode()) > 32768
    shaped = shape(
        "standard",
        input_budget("standard", 65536, 1024),
        message="Review it",
        history=[],
        resources=[],
        draft=("yaml", large),
        selected_step=None,
        schemas={"workflow": {}},
    )
    assert "outline" in shaped.context["draft"] and "source" not in shaped.context["draft"]
    assert "outlined" in shaped.context["trimmed"]


def test_full_keeps_the_four_schemas_and_sixteen_messages():
    schemas = {"workflow": {}, "action": {}, "connector": {}, "decisiontable": {}}
    shaped = shape(
        "full", None, message="Hi", history=HISTORY, resources=[], draft=None, selected_step=None, schemas=schemas
    )
    assert shaped.context["authoringSchemas"] == schemas and "guide" not in shaped.context
    assert shaped.history == HISTORY


def test_over_budget_parts_are_trimmed_in_order_and_said_so():
    big = {"kind": "run", "id": "big", "data": {"failed_steps": [{"node_id": "x" * 40}] * 200}}
    long = [LumiMessage(role="user", content="y" * 600) for _ in range(6)]
    shaped = shape(
        "compact",
        3072,
        message="Explain",
        history=long,
        resources=[RUN, big],
        draft=("yaml", draft()),
        selected_step=None,
        schemas={"workflow": {}},
    )
    assert shaped.context["resources"] == [RUN]
    assert shaped.context["trimmed"] == ["outlined", "attachments", "history"]
    assert len(shaped.history) < 4
    assert sent_tokens(shaped, "Explain") <= 3072


def test_a_draft_that_does_not_parse_is_left_out_with_a_note():
    shaped = shape(
        "compact",
        4096,
        message="Help",
        history=[],
        resources=[],
        draft=("yaml", "steps: [unclosed"),
        selected_step=None,
        schemas={"workflow": {}},
    )
    assert shaped.context["draft"] == {"note": "The draft does not parse yet, so only its errors can be discussed."}


def test_operations_explanations_send_no_authoring_context():
    shaped = shape(
        "compact",
        4096,
        message="Explain",
        history=[],
        resources=[RUN],
        draft=None,
        selected_step=None,
        schemas={"workflow": {}},
        explanation_only=True,
    )
    assert "guide" not in shaped.context and "authoringSchemas" not in shaped.context
    assert shaped.context["purpose"] == "explain-operations"


def test_the_module_has_no_provider_imports():
    assert "pydantic_ai" not in json.dumps(sorted(vars(context)))


async def test_asking_sizes_the_request_to_the_endpoints_context(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from firefly_weave import private_origins as po
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR
    from firefly_weave.contracts.connectors import ConnectionRevision
    from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiAskRequest, LumiConfiguration, LumiReply
    from firefly_weave.operations import lumi as module
    from firefly_weave.operations.lumi import LumiService

    scope = Scope(tenant_id=uuid4(), project_id=uuid4(), environment_id=uuid4())
    origins = po.PrivateOrigins(platform=po.PLATFORM).with_entries(
        [
            po.PrivateOrigin(
                origin="http://ollama:11434", purpose="model", networks=("10.246.21.0/24",), credentials="none"
            )
        ]
    )
    connection = ConnectionRevision.model_validate(
        {
            "id": uuid4(),
            "revision": 1,
            "name": "ollama-local",
            "connector_version_id": uuid4(),
            "connector": "weave-agentic-provider@1.0.0",
            "connector_digest": AGENTIC_DESCRIPTOR.manifest.digest,
            "adapter": "weave-agentic-provider",
            "config": {"provider": "openai-chat", "endpoint": "http://ollama:11434/v1", "secretSlot": "apiKey"},
            "secretRef": {"apiKey": "no-credential"},
            "allowed_destinations": ("http://ollama:11434",),
        }
    )
    configuration = LumiConfiguration.model_validate(
        {
            "enabled": True,
            "connection_revision_id": connection.id,
            "revision": 1,
            "profile": {
                "provider": "openai-chat",
                "model": "qwen3:4b",
                "options": {"max_tokens": 1024},
                "timeoutSeconds": 180,
                "outputSchema": LUMI_REPLY_SCHEMA,
            },
        }
    )
    entry = SimpleNamespace(context_tokens=8192)
    policy = SimpleNamespace(current=lambda: SimpleNamespace(entry_for=lambda url: entry))

    @asynccontextmanager
    async def transaction(*args, **kwargs):
        yield SimpleNamespace(scope=scope)

    connections = SimpleNamespace(definitions=SimpleNamespace(transaction=transaction), secrets=None)
    gateway = SimpleNamespace(configured=True, ask=AsyncMock(return_value=LumiReply(answer="Fix step-3's input.")))
    service = LumiService(connections, None, None, gateway, None, policy, None)
    actor = SimpleNamespace(id=uuid4())
    service._require = AsyncMock(return_value=actor)
    service._read = AsyncMock(return_value=configuration)
    monkeypatch.setattr(
        module, "ConnectionRepository", lambda tx: SimpleNamespace(revision=AsyncMock(return_value=connection))
    )
    request = LumiAskRequest(
        message="Why does step-3 fail?",
        history=HISTORY,
        draft={"format": "yaml", "source": draft()},
        selected_step="step-3",
    )
    with po.installed(origins):
        await service.ask(actor, scope, request, context=AuditContext())
    sent_request, sent_context = gateway.ask.await_args.args[1], gateway.ask.await_args.args[2]
    assert sent_context["profile"] == "compact" and "outline" in sent_context["draft"]
    assert len(sent_request.history) <= 4 and sent_request.history[-1] == HISTORY[-1]
    assert gateway.ask.await_args.kwargs["context_tokens"] == 8192
    assert gateway.ask.await_args.args[0].profile.options.max_tokens == COMPACT_MAX_TOKENS
    assert configuration.profile.options.max_tokens == 1024
    shaped = context.Shaped(context=sent_context, history=list(sent_request.history), profile="compact")
    assert sent_tokens(shaped, request.message) <= 4096
