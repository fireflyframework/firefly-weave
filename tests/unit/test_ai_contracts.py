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

"""Model error codes, their outcomes and copy, and the AI connection test contract."""

from typing import get_args

import pytest
from pydantic import ValidationError

from firefly_weave.contracts.ai import (
    AI_ERRORS,
    TRANSIENT_MODEL_CODES,
    AIConnectionTestRequest,
    AIConnectionTestResult,
    AIErrorCode,
    ai_message,
)
from firefly_weave.runtime.recovery import TRANSIENT_FAILURE_CODES


def test_every_code_has_one_outcome_and_message():
    assert set(AI_ERRORS) == set(get_args(AIErrorCode))
    assert all(error.message and error.outcome in {"not_started", "failed", "unknown"} for error in AI_ERRORS.values())


def test_a_missing_model_has_exactly_one_name():
    names = [code for code in AI_ERRORS if "MODEL" in code or "MISSING" in code]
    assert names == ["LLM_MODEL_NOT_FOUND"]
    assert AI_ERRORS["LLM_MODEL_NOT_FOUND"].outcome == "not_started"


def test_only_transport_codes_are_retryable():
    assert {"LLM_UNREACHABLE", "LLM_TIMEOUT", "LLM_RATE_LIMITED", "LLM_CAPACITY"} == TRANSIENT_MODEL_CODES


def test_recovery_retries_model_transport_codes_for_safe_actions_only():
    assert TRANSIENT_MODEL_CODES <= TRANSIENT_FAILURE_CODES
    assert {"TRANSIENT", "UNAVAILABLE", "RATE_LIMITED", "TIMEOUT"} <= TRANSIENT_FAILURE_CODES
    assert not {"LLM_POLICY", "LLM_MODEL_NOT_FOUND", "LLM_PROVIDER", "LLM_OUTPUT"} & TRANSIENT_FAILURE_CODES


def test_messages_are_filled_from_the_step_never_from_the_provider():
    assert (
        ai_message("LLM_MODEL_NOT_FOUND", model="qwen3:4b", endpoint="ollama-local")
        == "qwen3:4b is not installed on ollama-local. Pull it with `ollama pull qwen3:4b`."
    )
    assert ai_message("LLM_CONTEXT_LIMIT", n=8192) == "The prompt is too long for this model's context (8,192 tokens)."
    assert ai_message("LLM_UNKNOWN_CODE") == "The model provider returned an unexpected error."


def test_the_test_result_never_carries_model_text():
    value = {"ok": True, "code": "ok", "latency_ms": 12, "model": "qwen2.5:1.5b", "tool_calling": "supported"}
    assert AIConnectionTestResult.model_validate(value).model_dump(mode="json") == value
    with pytest.raises(ValidationError):
        AIConnectionTestResult.model_validate({**value, "text": "ready"})
    with pytest.raises(ValidationError):
        AIConnectionTestResult.model_validate({**value, "code": "SOMETHING_ELSE"})


def test_the_test_request_takes_an_exact_model_name():
    assert AIConnectionTestRequest(model="qwen3:4b").probe_tools is True
    with pytest.raises(ValidationError):
        AIConnectionTestRequest(model="*")
