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

"""Handled failures carry frozen product copy: entries are only ever appended, never reworded or reordered."""

from firefly_weave.contracts.ai import AI_ERRORS
from firefly_weave.contracts.step_errors import HANDLED_KERNEL_CODES, STEP_ERROR_MESSAGES, step_error_message

# The table as released. Append new entries here and in the table; never edit or move one.
RELEASED = (
    ("WV-EXPR-MISSING", "A value the step reads is missing."),
    ("WV-EXPR-TYPE", "A value the step reads has the wrong type."),
    ("WV-EXPR-RESOURCE_LIMIT", "The step's expression exceeded its size or work limit."),
    ("WV-DECISION-NO_MATCH", "No rule of the decision table matched."),
    ("WV-DECISION-MULTIPLE_MATCHES", "More than one rule of the decision table matched."),
    ("WV-DECISION-INPUT", "The decision table's input does not match its schema."),
    ("WV-DECISION-OUTPUT", "The decision table's result does not match its output schema."),
    ("WV-DECISION-PREDICATE", "A rule of the decision table did not evaluate to true or false."),
    ("WV-DECISION-RESOURCE_LIMIT", "The decision table exceeded its work limit."),
    ("WV-SCHEMA-INVALID_INSTANCE", "The step's input does not match its schema."),
    ("WV-SCHEMA-INVALID_FORMAT", "The step's input has a value in the wrong format."),
    ("WV-SCHEMA-SECRET_VALUE", "The step's input contains a secret value."),
    ("WV-SCHEMA-RESOURCE_LIMIT", "The step's input exceeded its size limit."),
    ("WV-TASK-LEASE-EXPIRED", "The worker stopped responding before the step finished."),
    ("WV-TASK-DEADLINE", "No worker picked up the step before its deadline."),
    ("WV-TASK-RECONCILED-FAILURE", "Someone confirmed that the step had no effect."),
    ("WV-CALL-START", "The called workflow could not start."),
    ("WV-CALL-DEPTH", "The chain of called workflows is too deep."),
    ("WV-RUN-CANCELLED", "The called workflow was cancelled."),
    ("WV-RUN-TIMED_OUT", "The called workflow ran out of time."),
    ("WV-HUMAN-EXPIRED", "Nobody completed the human task before it expired."),
    ("WV-SIGNAL-TIMEOUT", "The signal did not arrive in time."),
    ("WV-AGENT-ITERATIONS", "The agent reached its limit of turns."),
    ("WV-AGENT-BUDGET", "The agent reached its budget."),
    ("WV-AGENT-TIMEOUT", "The agent ran out of time."),
    ("WV-AGENT-TOOL-FAILED", "A tool the agent called failed."),
    ("WV-TXN-ROLLED-BACK", "The transaction was undone."),
    ("TRANSIENT", "The step failed with a temporary error."),
    ("UNAVAILABLE", "The endpoint was unavailable."),
    ("RATE_LIMITED", "The endpoint refused the request because of its rate limit."),
    ("TIMEOUT", "The step timed out."),
    ("LLM_POLICY", "This model is not approved for the endpoint."),
    ("LLM_CONNECTION", "The AI connection does not match this step."),
    ("LLM_MODEL_NOT_FOUND", "The model is not installed on the endpoint."),
    ("LLM_NO_TOOL_SUPPORT", "The model cannot call tools."),
    ("LLM_UNREACHABLE", "Weave could not reach the endpoint."),
    ("LLM_TIMEOUT", "The model did not answer in time."),
    ("LLM_CONTEXT_LIMIT", "The prompt is too long for the model's context."),
    ("LLM_AUTH", "The provider refused the credential."),
    ("LLM_RATE_LIMITED", "The provider is rate limiting requests."),
    ("LLM_CAPACITY", "Weave was too busy to run the step."),
    ("LLM_OUTPUT", "The model's answer did not match the result fields."),
    ("LLM_LIMIT", "The step reached its token or request limit."),
    ("LLM_INPUT", "The AI step input is not valid."),
    ("LLM_OPTIONS", "The provider does not accept these model options."),
    ("LLM_REASONING", "The reasoning pattern did not reach a final answer."),
    ("LLM_PROVIDER", "The model provider returned an unexpected error."),
)


def test_released_messages_never_change_and_new_ones_are_appended():
    assert list(STEP_ERROR_MESSAGES.items())[: len(RELEASED)] == list(RELEASED)
    assert len(STEP_ERROR_MESSAGES) == len(RELEASED), "Append the new entries to RELEASED as well"


def test_every_handled_platform_and_shared_worker_code_has_copy():
    codes = HANDLED_KERNEL_CODES | {"TRANSIENT", "UNAVAILABLE", "RATE_LIMITED", "TIMEOUT"} | set(AI_ERRORS)
    assert codes <= set(STEP_ERROR_MESSAGES)
    assert all(0 < len(message) <= 512 and "{" not in message for message in STEP_ERROR_MESSAGES.values())
    assert step_error_message("REQUEST_REFUSED") == "The step failed with code REQUEST_REFUSED."
