<!--
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
-->

# AI agent steps

This page lists the fields of the `agent` step, its limits, how its tool calls are reviewed, and how it fails. An agent
step lets a model work in turns: each turn, the model answers or calls the tools the step declares, and the platform
runs those calls as durable work before the next turn.

The step needs the `ai.agent` language feature, which the platform advertises in its capabilities.

## Step fields

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | Yes | The step's ID. |
| `kind` | Yes | `agent`. |
| `uses` | Yes | `weave-agentic-turn@1.0.0`, the platform's model turn. No other Action can run a turn. |
| `profile` | Yes | The name of an entry in the workflow's `llmProfiles`: provider, model, options and `outputSchema`. |
| `connection` | Yes | The AI connection the turns run on. |
| `instructions` | Yes | An expression for the model's instructions. |
| `prompt` | Yes | An expression for the request the model answers. |
| `context` | No | An expression for data the model reads as context. |
| `tools` | No | Up to 32 tools; see [Tools](#tools). |
| `review` | No | The default reviewer for tools that change data; see [Reviews](#reviews). |
| `memory` | No | What the agent remembers; see [Memory](#memory). |
| `limits` | No | Budgets and the deadline; see [Limits](#limits). |
| `onError`, `errorPath` | No | What a failure of the agent does, as for any step that handles its errors. |

A value marked secret (`x-secret` or `writeOnly`) can't reach the model: the compiler refuses an `instructions`,
`prompt`, `context` or fixed tool value that reads one (`WV-COMP-AI_SECRET_INPUT`).

## Tools

Each tool has a `name`, a `description` the model reads, and exactly one target:

| Target | Field | Runs |
| --- | --- | --- |
| Action | `action` (with `connection` when the Action needs one) | A published Action, as a task |
| Workflow | `workflow` | A callable workflow, as a child run the agent waits for |
| Person | `human` (`questionSchema`, `formSchema`, `assignment`) | A human task that asks a person |

`fixed` sets arguments the model can't change: they are merged last and always win. The model sees only the other
arguments. A tool result is redacted by the tool's output schema and by the secret patterns before the model reads it.

## Reviews

Every tool that changes data waits for a person's review before it runs: any Action that isn't `read_only`, and every
workflow tool. Read-only Actions and questions to a person run directly. A tool that changes data can't skip its
review (`WV-COMP-AGENT_UNREVIEWED_EFFECT`).

| Field | Default | Meaning |
| --- | --- | --- |
| `assignment` | — | Who reviews the call, from the activation's assignment bindings. |
| `expirySeconds` | 86,400 (at most 604,800) | When the review expires; it never outlives the agent's own deadline. |
| `distinctReviewer` | `false` | Excludes the person who started the run. |

The reviewer sees the side effect first, then the tool, its description, the arguments classified by the target's
input schema, which arguments the workflow fixed, and a mark that the model wrote them. They approve or reject, with an
optional comment the model reads after a rejection. An approval runs exactly the input the review showed. An expired
review tells the model `{"status": "expired"}`.

With `distinctReviewer: true`, the run's starter gets `403 WV-REVIEW-DISTINCT` when they read, claim, complete or
reassign the review, even when they manage human tasks, and the review doesn't appear in their task list. For a run
started by an API key, a trigger or a schedule, `distinctReviewer` excludes that service principal, which can't complete
tasks anyway, so any assigned person may review.

## Memory

| Field | Default | Meaning |
| --- | --- | --- |
| `scope` | `step` | `step` (one activation) or `run` (shared by activations with the same `key`). |
| `key` | — | The run memory's name; required for `run`. |
| `window` | 24 (2 to 200) | How many messages are kept. |
| `contextTokens` | 8,192 (512 to 262,144) | The context the turns fit, capped by the AI policy. |

## Limits

| Field | Default | Maximum |
| --- | --- | --- |
| `maxIterations` | 6 | 32 |
| `maxToolCalls` | 12 | 64 |
| `parallelToolCalls` | 4 | 8 |
| `maxTotalTokens` | 60,000 | 2,000,000 |
| `timeoutSeconds` | 900 | 604,800 |
| `onToolError` | `return_to_model` | `incident` stops the agent at a tool failure |

A run spends at most 2,000,000 tokens across all its agents.

## Failures

| Code | Raised when |
| --- | --- |
| `WV-AGENT-ITERATIONS` | The agent used its turns without a final answer. |
| `WV-AGENT-BUDGET` | A turn crossed the step's or the run's token budget. Retry grants one forced final turn. |
| `WV-AGENT-TIMEOUT` | The agent's deadline passed. |
| `WV-AGENT-TOOL-FAILED` | A tool failed with `onToolError: incident`. |
| `WV-TASK-AMBIGUOUS` | A call that changes data and is never retried may have applied. |

`onError` takes the agent's failure, like any step's. A call that changes data and is never retried, still running when
the agent fails, is different: it may have applied. It keeps its own key and task under `WV-TASK-AMBIGUOUS`, whatever
`onToolError` and `onError` say. Once a person reconciles it in Operate, the agent's failure goes to `onError`.

## Activation

Activation checks an agent like an AI task: its connection must use the profile's provider
(`WV-ACTIVATION-AI-PROVIDER`), and the AI policy must approve the model for the connection's endpoint
(`WV-ACTIVATION-AI-MODEL`). Activation also refuses any step other than an agent that would run the model turn, and an
agent whose turn action isn't the platform's (`WV-ACTIVATION-AI-RESERVED`).

See also: [history and replay](history-and-replay.md), [incident operations](incident-operations.md), and the
[worker protocol](worker-protocol.md).
