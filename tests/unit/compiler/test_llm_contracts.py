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

"""AI workflow profiles expose bounded portable controls and no executable configuration."""

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.llm import LLMProfile


def profile(**changes):
    return {
        "provider": "openai-responses",
        "model": "fixture-model",
        "options": {"max_tokens": 500},
        "outputSchema": {"type": "string"},
        **changes,
    }


def test_profile_uses_explicit_provider_model_and_bounded_defaults():
    parsed = LLMProfile.model_validate(profile())
    assert parsed.max_calls == 8 and parsed.timeout_seconds == 120
    assert parsed.reasoning.pattern == "none"
    assert parsed.options.max_tokens == 500
    assert "temperature" not in parsed.model_dump(mode="json", by_alias=True)["options"]


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "unknown"},
        {"model": ""},
        {"baseUrl": "https://untrusted.example"},
        {"apiKey": "forbidden-fixture"},
        {"maxCalls": 0},
        {"maxCalls": 65},
        {"timeoutSeconds": 601},
        {"options": {"max_tokens": 0}},
        {"options": {"max_tokens": True}},
        {"options": {"max_tokens": 32769}},
        {"options": {"max_tokens": 100, "store_responses": True}},
        {"options": {"max_tokens": 100, "reasoning": "high", "reasoning_budget_tokens": 20}},
        {"options": {"max_tokens": 100, "request_timeout": 121}},
        {"reasoning": {"pattern": "python", "maxSteps": 2}},
        {"reasoning": {"pattern": "react", "maxSteps": 33}},
        {"tools": ["os.system"]},
        {"outputSchema": {"$ref": "https://untrusted.example/schema"}},
    ],
)
def test_profile_rejects_unsupported_or_unbounded_controls(changes):
    with pytest.raises(ValidationError):
        LLMProfile.model_validate(profile(**changes))


def test_optional_options_schema_never_advertises_explicit_null_or_default_null():
    from firefly_weave.compiler.schemas import validate_payload
    from firefly_weave.contracts.llm import LLMOptions

    schema = LLMOptions.model_json_schema()
    for name, field in schema["properties"].items():
        if name == "max_tokens":
            continue
        assert "default" not in field
        assert validate_payload(schema, {"max_tokens": 100, name: None}, {})
