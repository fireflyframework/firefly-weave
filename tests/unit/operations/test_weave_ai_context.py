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


# Characters and the tokens llama3.2:3b counted for them (Ollama 0.40.2 prompt_eval_count): Weave AI's
# instructions, reply format and guide, two shaped compact requests, and the most token-dense requests measured
# (hex identifiers and Japanese literals, 1.55 and 1.48 times characters / 4).
MEASURED = [(655, 142), (1013, 316), (1240, 351), (4312, 1522), (3532, 1155), (28660, 11140), (28180, 10420)]


@pytest.mark.parametrize(("characters", "counted"), MEASURED)
def test_the_estimate_never_counts_fewer_tokens_than_the_model(characters, counted):
    assert estimate_tokens("x" * characters) >= counted


def test_the_estimate_counts_two_and_a_half_characters_per_token():
    assert estimate_tokens("x" * 1000) == 400 and estimate_tokens("x") == 1 and estimate_tokens("") == 0


# Text outside ASCII, measured on llama3.2:3b in compact requests: 5,000 Chinese characters counted 4,838 tokens,
# 5,000 Japanese 5,031, and 3,000 emoji 9,206. Each such character counts its UTF-8 bytes, a true upper bound.
@pytest.mark.parametrize(
    ("text", "counted"),
    [("中" * 5000, 4838), ("日本" * 2500, 5031), ("😀" * 3000, 9206)],
    ids=["chinese", "japanese", "emoji"],
)
def test_text_outside_ascii_counts_its_bytes_and_never_fewer_tokens_than_the_model(text, counted):
    assert estimate_tokens(text) == len(text.encode()) >= counted


def test_mixed_text_counts_ascii_by_characters_and_the_rest_by_bytes():
    assert estimate_tokens("ab" * 5 + "é" + "中") == 4 + 2 + 3


def test_the_gateway_text_and_the_guide_leave_two_thirds_of_the_compact_budget():
    # The gateway's tests keep its instructions and reply format within this reserve.
    assert GATEWAY_RESERVE_TOKENS == 736
    assert estimate_tokens(GUIDE) + GATEWAY_RESERVE_TOKENS <= COMPACT_INPUT_TOKENS / 3


def test_the_guide_shows_a_whole_workflow_the_platform_accepts_without_a_warning():
    from firefly_weave.compiler.api import validate_authoring
    from firefly_weave.operations.lumi_context import GUIDE_VERSION

    lines = GUIDE.splitlines()
    start = lines.index("Example of a whole workflow document:") + 1
    example = []
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        example.append(line[2:])
    assert example[:2] == ["apiVersion: weave/v1alpha1", "kind: Workflow"]
    assert list(validate_authoring("\n".join(example) + "\n", format="yaml").diagnostics) == []
    assert GUIDE_VERSION == 2


def test_a_standard_request_counts_the_workflow_schema_against_its_budget():
    message = "Review it"
    workflow = {"type": "object", "description": "s" * 15000}
    budget = input_budget("standard", 16384, 1024)
    shaped = shape(
        "standard",
        budget,
        message=message,
        history=HISTORY,
        resources=[RUN],
        draft=("yaml", draft(120)),
        selected_step=None,
        schemas={"workflow": workflow},
    )
    assert shaped.context["authoringSchemas"] == {"workflow": workflow}
    assert sent_tokens(shaped, message) <= budget


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


def test_a_draft_that_does_not_parse_is_sent_whole_when_it_fits_and_left_out_with_a_note_when_not():
    def shaped(source):
        return shape(
            "compact",
            4096,
            message="Help",
            history=[],
            resources=[],
            draft=("yaml", source),
            selected_step=None,
            schemas={"workflow": {}},
        ).context["draft"]

    assert shaped("steps: [unclosed") == {"format": "yaml", "source": "steps: [unclosed"}
    note = {"note": "The draft does not parse yet, so only its errors can be discussed."}
    assert shaped("steps: [unclosed " + "x" * 20000) == note


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


def large_draft(steps=20, selected_bytes=0, description_bytes=0):
    document = yaml.safe_load(draft(steps))
    if selected_bytes:
        document["spec"]["steps"][3]["with"] = {"literal": {"note": "s" * selected_bytes}}
    if description_bytes:
        document["metadata"]["description"] = "d" * description_bytes
    return yaml.safe_dump(document)


@pytest.mark.parametrize(
    ("steps", "selected_bytes", "description_bytes", "trims"),
    [
        (300, 0, 0, {"outlined", "steps"}),
        (600, 0, 0, {"outlined", "steps"}),
        (20, 20000, 0, {"outlined", "selected_step"}),
        (20, 0, 20000, {"outlined", "metadata"}),
    ],
)
def test_a_large_compact_draft_stays_within_its_share_and_the_cap(steps, selected_bytes, description_bytes, trims):
    message = "Why does step-3 fail?"
    budget = input_budget("compact", 8192, 512)
    shaped = shape(
        "compact",
        budget,
        message=message,
        history=HISTORY,
        resources=[RUN],
        draft=("yaml", large_draft(steps, selected_bytes, description_bytes)),
        selected_step="step-3",
        schemas={},
    )
    assert budget == COMPACT_INPUT_TOKENS
    assert sent_tokens(shaped, message) <= budget
    assert estimate_tokens(json.dumps(shaped.context["draft"])) <= budget * 0.40
    assert trims <= set(shaped.context["trimmed"])
    assert "d" * 100 not in json.dumps(shaped.context) and "s" * 100 not in json.dumps(shaped.context)


def test_a_shortened_outline_keeps_the_first_and_last_steps_and_says_how_many_are_left_out():
    shaped = shape(
        "compact",
        COMPACT_INPUT_TOKENS,
        message="Review it",
        history=[],
        resources=[],
        draft=("yaml", draft(600)),
        selected_step=None,
        schemas={},
    )
    listed = shaped.context["draft"]["outline"]["steps"]
    marker = next(item for item in listed if isinstance(item, str))
    shown = [item for item in listed if isinstance(item, dict)]
    assert listed[0]["id"] == "step-0" and listed[-1]["id"] == "step-599"
    assert marker == f"... {600 - len(shown)} more steps"
    assert listed.index(marker) == len(shown) // 2


def test_an_outline_keeps_only_the_name_and_version_of_the_metadata():
    document = {"kind": "Workflow", "metadata": {"name": "a", "version": "1.0.0", "labels": {"team": "x"}}}
    assert outline(document, None)["metadata"] == {"name": "a", "version": "1.0.0"}


def test_a_selected_step_too_large_to_send_whole_is_named_with_a_note():
    shaped = shape(
        "compact",
        COMPACT_INPUT_TOKENS,
        message="Why does step-3 fail?",
        history=[],
        resources=[],
        draft=("yaml", large_draft(20, 20000)),
        selected_step="step-3",
        schemas={},
    )
    selected = shaped.context["draft"]["outline"]["selected"]
    assert selected == {
        "id": "step-3",
        "kind": "action",
        "uses": "crm-lookup@1.3.0",
        "note": "This step is too large to send whole.",
    }


def test_weave_ai_stays_a_registered_service_and_its_helpers_do_not():
    from firefly_weave.operations import lumi

    assert vars(lumi.LumiService).get("__pyfly_stereotype__") == "service"
    assert "__pyfly_stereotype__" not in vars(lumi._answer_limit)


def test_an_administrator_chosen_compact_profile_keeps_its_cap_without_a_context_size():
    assert input_budget("compact", None, 1024) == COMPACT_INPUT_TOKENS
    assert input_budget("standard", None, 1024) is None


def resource(name, characters):
    return {"kind": "run", "id": name, "data": {"status": "failed", "note": "n" * characters}}


def test_attachments_over_their_share_are_left_out_from_the_last():
    first, large, last = resource("a", 100), resource("b", 4000), resource("c", 100)
    shaped = shape(
        "compact",
        COMPACT_INPUT_TOKENS,
        message="m",
        history=[],
        resources=[first, large, last],
        draft=None,
        selected_step=None,
        schemas={},
    )
    assert shaped.context["resources"] == [first] and shaped.context["trimmed"] == ["attachments"]


def test_standard_attachments_are_capped_at_thirty_two_kibibytes():
    resources = [resource(str(index), 6000) for index in range(8)]
    shaped = shape(
        "standard",
        input_budget("standard", 200000, 1024),
        message="m",
        history=[],
        resources=resources,
        draft=None,
        selected_step=None,
        schemas={},
    )
    kept = shaped.context["resources"]
    assert kept == resources[: len(kept)] and 0 < len(kept) < len(resources)
    assert len(json.dumps(kept, ensure_ascii=False).encode()) <= 32768
    assert shaped.context["trimmed"] == ["attachments"]


LONG_HISTORY = [
    LumiMessage(role="user" if index % 2 == 0 else "assistant", content=f"m{index} " * 150) for index in range(16)
]


def compact_history(message):
    return shape(
        "compact",
        COMPACT_INPUT_TOKENS,
        message=message,
        history=LONG_HISTORY,
        resources=[],
        draft=None,
        selected_step=None,
        schemas={},
    )


def test_history_over_its_share_keeps_the_newest_messages():
    shaped = compact_history("Why?")
    assert 0 < len(shaped.history) < 4
    assert shaped.history == LONG_HISTORY[-len(shaped.history) :]
    assert "history" in shaped.context["trimmed"]


def test_the_question_counts_against_the_conversations_share():
    assert len(compact_history("Why? " * 150).history) < len(compact_history("Why?").history)


def test_the_shares_of_the_draft_and_attachments_are_forty_and_twenty_percent():
    from firefly_weave.operations.lumi_context import ATTACHMENT_SHARE, DRAFT_SHARE

    assert (DRAFT_SHARE, ATTACHMENT_SHARE) == (0.40, 0.20)


def broken_proposal(steps=3, broken=1):
    """A whole workflow proposal whose step `step-<broken>` has an empty value the platform rejects."""
    return yaml.safe_dump(
        {
            "apiVersion": "weave/v1alpha1",
            "kind": "Workflow",
            "metadata": {"name": "greet", "version": "1.0.0"},
            "spec": {
                "inputSchema": {"type": "object"},
                "outputSchema": {},
                "steps": [
                    {
                        "id": f"step-{index}",
                        "kind": "transform",
                        "value": {} if index == broken else {"object": {"note": {"literal": "keep " * 20}}},
                    }
                    for index in range(steps)
                ],
                "output": {"ref": f"/steps/step-{steps - 1}/output"},
            },
        },
        sort_keys=False,
    )


def fix_shape(source, fix=True):
    return shape(
        "compact",
        COMPACT_INPUT_TOKENS,
        message="Fix the proposal",
        history=[],
        resources=[],
        draft=("yaml", source),
        selected_step=None,
        schemas={},
        fix=fix,
    )


def test_a_fix_request_sends_a_proposal_that_fits_whole():
    source = broken_proposal()
    shaped = fix_shape(source)
    assert shaped.context["draft"] == {"format": "yaml", "source": source}
    assert "trimmed" not in shaped.context
    # An ordinary compact request sends a draft that fits whole too (0 of 5 valid changes from an outline, 4 of 5
    # from the whole draft on qwen3:4b).
    assert fix_shape(source, fix=False).context["draft"] == {"format": "yaml", "source": source}


def test_a_fix_request_too_large_to_send_whole_sends_the_diagnosed_steps_and_the_problems():
    source = broken_proposal(steps=300, broken=5)
    shaped = fix_shape(source)
    draft = shaped.context["draft"]
    assert "source" not in draft and "outline" in draft
    assert [step["id"] for step in draft["diagnosed"]] == ["step-5"] and draft["diagnosed"][0]["value"] == {}
    assert draft["problems"] == [
        {
            "code": "WV-SCHEMA-INVALID_INSTANCE",
            "path": "/spec/steps/5",
            "message": "Value does not satisfy the declared contract.",
        }
    ]
    assert sent_tokens(shaped, "Fix the proposal") <= COMPACT_INPUT_TOKENS
    assert estimate_tokens(json.dumps(draft)) <= COMPACT_INPUT_TOKENS * 0.40
    assert {"outlined", "steps"} <= set(shaped.context["trimmed"])


def test_an_ollama_endpoint_without_a_context_size_is_shaped_as_compact():
    from types import SimpleNamespace

    from firefly_weave.operations.lumi import LumiService

    entry = SimpleNamespace(context_tokens=None, compat="ollama")
    cloud = SimpleNamespace(context_tokens=None, compat=None)
    for endpoint, expected in ((entry, 8192), (cloud, None)):
        policy = SimpleNamespace(current=lambda endpoint=endpoint: SimpleNamespace(entry_for=lambda url: endpoint))
        service = LumiService(SimpleNamespace(definitions=None), None, None, None, None, policy, None)
        connection = SimpleNamespace(config={"endpoint": "http://ollama:11434/v1"})
        assert service._context_tokens(connection) == expected
    assert choose_profile(8192) == "compact"


async def ask_through_the_service(monkeypatch, request, reply):
    """LumiService.ask on a keyless compact endpoint with a spy gateway; returns the answer and the spy."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from firefly_weave import private_origins as po
    from firefly_weave.access.audit import AuditContext
    from firefly_weave.contracts.access import Scope
    from firefly_weave.contracts.agentic import AGENTIC_DESCRIPTOR
    from firefly_weave.contracts.connectors import ConnectionRevision
    from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiConfiguration
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
    entry = SimpleNamespace(context_tokens=8192, compat="ollama")
    policy = SimpleNamespace(current=lambda: SimpleNamespace(entry_for=lambda url: entry))

    @asynccontextmanager
    async def transaction(*args, **kwargs):
        yield SimpleNamespace(scope=scope)

    connections = SimpleNamespace(definitions=SimpleNamespace(transaction=transaction), secrets=None)
    gateway = SimpleNamespace(configured=True, ask=AsyncMock(return_value=reply))
    service = LumiService(connections, None, None, gateway, None, policy, None)
    actor = SimpleNamespace(id=uuid4())
    service._require = AsyncMock(return_value=actor)
    service._read = AsyncMock(return_value=configuration)
    monkeypatch.setattr(
        module, "ConnectionRepository", lambda tx: SimpleNamespace(revision=AsyncMock(return_value=connection))
    )
    with po.installed(origins):
        answer = await service.ask(actor, scope, request, context=AuditContext())
    return answer, gateway.ask


async def test_asking_returns_each_proposal_with_the_platforms_problem_count(monkeypatch):
    from firefly_weave.contracts.lumi import LumiAskRequest, LumiReply

    proposal = {"title": "Broken", "kind": "workflow", "format": "yaml", "source": broken_proposal()}
    reply = LumiReply.model_validate({"answer": "Here it is.", "proposals": [proposal]})
    answer, _ = await ask_through_the_service(monkeypatch, LumiAskRequest(message="Write it"), reply)
    assert [item.diagnostics for item in answer.proposals] == [1]


async def test_a_fix_request_sends_the_faulty_value_to_the_model_on_a_compact_endpoint(monkeypatch):
    from firefly_weave.contracts.lumi import LumiAskRequest, LumiReply

    faulty = broken_proposal().replace("value: {}", "value: {objct: 1}")
    request = LumiAskRequest(message="Fix the proposal", fix=True, draft={"format": "yaml", "source": faulty})
    _, spy = await ask_through_the_service(monkeypatch, request, LumiReply(answer="Fixed."))
    sent = spy.await_args.args[2]
    assert sent["profile"] == "compact" and "objct" in sent["draft"]["source"]
    large = broken_proposal(steps=200).replace("value: {}", "value: {objct: 1}")
    ordinary = request.model_copy(update={"fix": False, "draft": request.draft.model_copy(update={"source": large})})
    _, spy = await ask_through_the_service(monkeypatch, ordinary, LumiReply(answer="Explained."))
    # A draft too large for its share is outlined, so the value of a step that is not selected is not sent.
    assert "objct" not in json.dumps(spy.await_args.args[2])


def test_a_compact_request_sends_a_draft_that_fits_whole():
    small = draft(3)
    shaped = shape(
        "compact",
        COMPACT_INPUT_TOKENS,
        message="Add a step after step-2",
        history=[],
        resources=[],
        draft=("yaml", small),
        selected_step=None,
        schemas={},
    )
    assert shaped.context["draft"] == {"format": "yaml", "source": small}
    assert "trimmed" not in shaped.context


async def test_only_a_fix_request_sends_the_problems_of_a_draft_over_its_share(monkeypatch):
    from firefly_weave.contracts.lumi import LumiAskRequest, LumiReply

    large = broken_proposal(steps=200)
    fix = LumiAskRequest(message="Fix the proposal", fix=True, draft={"format": "yaml", "source": large})
    _, spy = await ask_through_the_service(monkeypatch, fix, LumiReply(answer="Fixed."))
    sent = spy.await_args.args[2]["draft"]
    assert "outline" in sent and sent["problems"] and [step["id"] for step in sent["diagnosed"]] == ["step-1"]
    ordinary = fix.model_copy(update={"fix": False})
    _, spy = await ask_through_the_service(monkeypatch, ordinary, LumiReply(answer="Explained."))
    sent = spy.await_args.args[2]["draft"]
    assert "outline" in sent and "problems" not in sent and "diagnosed" not in sent
