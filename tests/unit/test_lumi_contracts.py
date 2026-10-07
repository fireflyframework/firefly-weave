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

"""Lumi authoring never accepts provider authority or arbitrary claimed run context."""

import pytest
from pydantic import ValidationError

from firefly_weave.compiler.schemas import validate_payload
from firefly_weave.contracts.lumi import LUMI_REPLY_SCHEMA, LumiAskRequest, LumiConfigurationRequest, LumiReply


def profile():
    return {
        "provider": "openai-chat",
        "model": "gpt-4o",
        "options": {"max_tokens": 1024},
        "outputSchema": LUMI_REPLY_SCHEMA,
    }


def test_lumi_reply_is_plain_proposal_data_with_no_execution_fields():
    reply = LumiReply(
        answer="Review this draft",
        proposals=[{"title": "Draft", "kind": "workflow", "format": "yaml", "source": "kind: Workflow"}],
        followUps=[],
    )
    assert not validate_payload(LUMI_REPLY_SCHEMA, reply.model_dump(mode="json", by_alias=True), {})
    with pytest.raises(ValidationError):
        LumiReply(answer="x", execute={"kind": "delete"})


@pytest.mark.parametrize(
    "extra", [{"profile": profile()}, {"runState": {"input": "pretend-authorized"}}, {"credential": "secret"}]
)
def test_ask_cannot_override_configuration_or_claim_unread_context(extra):
    with pytest.raises(ValidationError):
        LumiAskRequest(message="Help", **extra)


def test_lumi_configuration_cannot_replace_fixed_reply_schema():
    from uuid import uuid4

    value = profile()
    value["outputSchema"] = {}
    with pytest.raises(ValidationError):
        LumiConfigurationRequest(connection_revision_id=uuid4(), profile=value)


def test_attachments_are_typed_resource_identifiers():
    from uuid import uuid4

    request = LumiAskRequest(message="Explain", attachments=[{"kind": "run", "id": str(uuid4())}])
    assert request.attachments[0].kind == "run"
    with pytest.raises(ValidationError):
        LumiAskRequest(message="Explain", attachments=[{"kind": "run", "state": {}}])


@pytest.mark.parametrize(
    "draft",
    [
        {"format": "html", "source": "<p>x</p>"},
        {"format": "yaml", "source": ""},
        {"format": "yaml", "source": "x", "url": "https://private.invalid"},
        {"format": "yaml", "source": "é" * 131073},
    ],
)
def test_inline_lumi_source_rejects_wrong_format_remote_fetch_fields_and_oversized_utf8(draft):
    with pytest.raises(ValidationError):
        LumiAskRequest(message="Help", draft=draft)


def test_inline_unsaved_source_is_explicit_untrusted_text_even_when_not_valid_yaml():
    value = LumiAskRequest(message="Fix this", draft={"format": "yaml", "source": "steps: [invalid"})
    assert value.draft.source == "steps: [invalid"


def test_configuration_schema_publishes_the_same_fixed_reply_schema_as_validation():
    schema = LumiConfigurationRequest.model_json_schema(by_alias=True)
    assert schema["$defs"]["LumiProfile"]["properties"]["outputSchema"]["const"] == LUMI_REPLY_SCHEMA


@pytest.mark.parametrize(
    "kind", ["deployment-target", "deployment", "deployment-observation", "deployment-plan", "deployment-job"]
)
def test_operations_context_is_identifier_only_and_explanation_only(kind):
    from uuid import uuid4

    attachment = {"kind": kind, "id": str(uuid4())}
    assert LumiAskRequest(message="Explain capacity", attachments=[attachment]).explanation_only
    with pytest.raises(ValidationError):
        LumiAskRequest(message="Explain", attachments=[attachment | {"data": {"replicas": 3}}])
    with pytest.raises(ValidationError):
        LumiAskRequest(message="Explain", attachments=[attachment], draft={"format": "yaml", "source": "private"})
    with pytest.raises(ValidationError):
        LumiAskRequest(message="Explain", attachments=[attachment, {"kind": "run", "id": str(uuid4())}])
