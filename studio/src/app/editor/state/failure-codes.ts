/*
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
*/
// The platform's failure codes with their messages (STEP_ERROR_MESSAGES in
// contracts/step_errors.py) and the step kinds that may return each, as
// scripts/language_fixtures.py writes them to the step-errors fixture; a
// test holds this copy to it. Simulate a failure suggests a kind's codes
// under "Platform failures". These are the platform's own words, shown as
// it wrote them. Loaded with step details, not the first page.

export interface PlatformFailure {
  code: string;
  message: string;
  kinds: string[];
}

export const PLATFORM_FAILURES: readonly PlatformFailure[] = [
  {
    code: "WV-EXPR-MISSING",
    message: "A value the step reads is missing.",
    kinds: ["transform", "decisionTable"],
  },
  {
    code: "WV-EXPR-TYPE",
    message: "A value the step reads has the wrong type.",
    kinds: ["transform", "decisionTable"],
  },
  {
    code: "WV-EXPR-RESOURCE_LIMIT",
    message: "The step's expression exceeded its size or work limit.",
    kinds: ["transform", "decisionTable"],
  },
  {
    code: "WV-DECISION-NO_MATCH",
    message: "No rule of the decision table matched.",
    kinds: ["decisionTable"],
  },
  {
    code: "WV-DECISION-MULTIPLE_MATCHES",
    message: "More than one rule of the decision table matched.",
    kinds: ["decisionTable"],
  },
  {
    code: "WV-DECISION-INPUT",
    message: "The decision table's input does not match its schema.",
    kinds: ["decisionTable"],
  },
  {
    code: "WV-DECISION-OUTPUT",
    message: "The decision table's result does not match its output schema.",
    kinds: ["decisionTable"],
  },
  {
    code: "WV-DECISION-PREDICATE",
    message: "A rule of the decision table did not evaluate to true or false.",
    kinds: ["decisionTable"],
  },
  {
    code: "WV-DECISION-RESOURCE_LIMIT",
    message: "The decision table exceeded its work limit.",
    kinds: ["decisionTable"],
  },
  {
    code: "WV-SCHEMA-INVALID_INSTANCE",
    message: "The step's input does not match its schema.",
    kinds: ["action", "callWorkflow"],
  },
  {
    code: "WV-SCHEMA-INVALID_FORMAT",
    message: "The step's input has a value in the wrong format.",
    kinds: ["action", "callWorkflow"],
  },
  {
    code: "WV-SCHEMA-SECRET_VALUE",
    message: "The step's input contains a secret value.",
    kinds: ["action", "callWorkflow"],
  },
  {
    code: "WV-SCHEMA-RESOURCE_LIMIT",
    message: "The step's input exceeded its size limit.",
    kinds: ["action", "callWorkflow"],
  },
  {
    code: "WV-TASK-LEASE-EXPIRED",
    message: "The worker stopped responding before the step finished.",
    kinds: ["action"],
  },
  {
    code: "WV-TASK-DEADLINE",
    message: "No worker picked up the step before its deadline.",
    kinds: ["action"],
  },
  {
    code: "WV-TASK-RECONCILED-FAILURE",
    message: "Someone confirmed that the step had no effect.",
    kinds: [],
  },
  {
    code: "WV-CALL-START",
    message: "The called workflow could not start.",
    kinds: ["callWorkflow"],
  },
  {
    code: "WV-CALL-DEPTH",
    message: "The chain of called workflows is too deep.",
    kinds: ["callWorkflow"],
  },
  {
    code: "WV-RUN-CANCELLED",
    message: "The called workflow was cancelled.",
    kinds: ["callWorkflow"],
  },
  {
    code: "WV-RUN-TIMED_OUT",
    message: "The called workflow ran out of time.",
    kinds: ["callWorkflow"],
  },
  {
    code: "WV-HUMAN-EXPIRED",
    message: "Nobody completed the human task before it expired.",
    kinds: ["humanTask"],
  },
  {
    code: "WV-SIGNAL-TIMEOUT",
    message: "The signal did not arrive in time.",
    kinds: ["signal"],
  },
  {
    code: "WV-AGENT-ITERATIONS",
    message: "The agent reached its limit of turns.",
    kinds: ["agent"],
  },
  {
    code: "WV-AGENT-BUDGET",
    message: "The agent reached its budget.",
    kinds: ["agent"],
  },
  {
    code: "WV-AGENT-TIMEOUT",
    message: "The agent ran out of time.",
    kinds: ["agent"],
  },
  {
    code: "WV-AGENT-TOOL-FAILED",
    message: "A tool the agent called failed.",
    kinds: ["agent"],
  },
  {
    code: "WV-TXN-ROLLED-BACK",
    message: "The transaction was undone.",
    kinds: ["transaction"],
  },
  {
    code: "TRANSIENT",
    message: "The step failed with a temporary error.",
    kinds: ["action", "llm", "agent"],
  },
  {
    code: "UNAVAILABLE",
    message: "The endpoint was unavailable.",
    kinds: ["action", "llm", "agent"],
  },
  {
    code: "RATE_LIMITED",
    message: "The endpoint refused the request because of its rate limit.",
    kinds: ["action", "llm", "agent"],
  },
  {
    code: "TIMEOUT",
    message: "The step timed out.",
    kinds: ["action", "llm", "agent"],
  },
  {
    code: "LLM_POLICY",
    message: "This model is not approved for the endpoint.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_CONNECTION",
    message: "The AI connection does not match this step.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_MODEL_NOT_FOUND",
    message: "The model is not installed on the endpoint.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_NO_TOOL_SUPPORT",
    message: "The model cannot call tools.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_UNREACHABLE",
    message: "Weave could not reach the endpoint.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_TIMEOUT",
    message: "The model did not answer in time.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_CONTEXT_LIMIT",
    message: "The prompt is too long for the model's context.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_AUTH",
    message: "The provider refused the credential.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_RATE_LIMITED",
    message: "The provider is rate limiting requests.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_CAPACITY",
    message: "Weave was too busy to run the step.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_OUTPUT",
    message: "The model's answer did not match the result fields.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_LIMIT",
    message: "The step reached its token or request limit.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_INPUT",
    message: "The AI step input is not valid.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_OPTIONS",
    message: "The provider does not accept these model options.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_REASONING",
    message: "The reasoning pattern did not reach a final answer.",
    kinds: ["llm", "agent"],
  },
  {
    code: "LLM_PROVIDER",
    message: "The model provider returned an unexpected error.",
    kinds: ["llm", "agent"],
  },
];

/** The platform's codes a step of `kind` may return, in the platform's order. */
export function platformFailures(kind: string): PlatformFailure[] {
  return PLATFORM_FAILURES.filter((item) => item.kinds.includes(kind));
}

/** The platform's words for a code it has no message of its own for (step_error_message). */
export const genericFailureMessage = (code: string): string =>
  `The step failed with code ${code}.`;
