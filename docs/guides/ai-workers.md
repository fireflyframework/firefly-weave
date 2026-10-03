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

# Run AI steps with an Agentic worker

A workflow AI step resolves its named `llmProfiles` entry at compilation and
sends a pinned profile, prompt, and context to `weave-agentic.generate@1.0.0`.
The Weave server never imports Agentic or contacts a model provider. An
independently deployed Python 3.13 worker runs Firefly Agentic 26.9.0 through the
normal task lease, heartbeat, credential, and completion APIs.

The worker source and locked environment are in `workers/agentic`. Agentic is
pinned to the verified `v26.09.0` release wheel and its SHA-256. Weave retains
its Python 3.12 server requirement.

## Install and export the catalog

From the repository root:

```sh
uv sync --project workers/agentic --locked --python 3.13
workers/agentic/.venv/bin/weave-agentic-worker --catalog > agentic-catalog.json
workers/agentic/.venv/bin/weave-agentic-worker --release-manifest > agentic-release.json
```

The catalog contains `weave-agentic-provider@1.0.0`, the worker Action
`weave-agentic-generate@1.0.0`, and the exact task capability. Publish the
Connector and Action through the normal definition APIs. Register the worker
release with its image digest and the exported task and credential capabilities.
Use the normal worker connection grant API to authorize that release and task
capability for the selected connection revision. Activation pins the worker
release and connection revision.

The Action is `non_idempotent` and allows one attempt. A provider request can
incur charges even when its result is lost. Weave fences completion and preserves
accepted results for replay; it cannot promise exactly one provider request
after an ambiguous network failure.

## Configure the provider connection

Create a connection for the published provider Connector. Its configuration is:

```json
{
  "provider": "openai-chat",
  "endpoint": "https://api.openai.com/v1",
  "secretSlot": "apiKey"
}
```

Set `secretRef.apiKey` to the existing secret handle and add the endpoint's
origin, for example `https://api.openai.com`, to `allowed_destinations`. Azure
connections additionally require an explicit `apiVersion`, such as the version
approved for your deployment. The supported provider selectors are
`openai-chat`, `openai-responses`, `azure-chat`, `azure-responses`, and `anthropic`.
Model IDs and Azure deployment names are supplied explicitly.

The server validates this configuration without provider I/O. The generic
connection test returns unsuccessful because this connection is executed by the
remote worker; it does not claim to test provider credentials. A successful AI
task is the end-to-end provider check.

## Apply the worker's model and endpoint policy

Mount a policy file owned by the operator:

```json
{
  "models": [{"provider": "openai-chat", "model": "gpt-4o"}],
  "endpoints": ["https://api.openai.com/v1"]
}
```

These are exact allowlists, with no wildcard model or endpoint selection. The
model name above illustrates the format; use models available to your provider
account. A workflow cannot set a provider URL, secret value, custom tool class,
or arbitrary model settings. Every task must satisfy the deployment policy and
its bound connection's destination list. Configure the worker's network policy
to allow only those provider destinations and Weave.

The provider clients disable environment proxies, redirects, and automatic SDK
retries. Model options are checked by the pinned Agentic `ModelOptions` and
provider settings resolver. Unsupported options fail before credential lookup;
they are not silently dropped.

## Start the worker

Provide these explicit deployment settings:

| Setting | Value |
| --- | --- |
| `WEAVE_API_URL` | Weave API origin |
| `WEAVE_ENVIRONMENT_URL` | Scoped `/api/v1/tenants/.../projects/.../environments/...` path |
| `WEAVE_WORKER_RELEASE_ID` | Admitted worker release UUID |
| `WEAVE_WORKER_TOKEN_FILE` | Mounted file containing the worker principal's access token |
| `WEAVE_AGENTIC_POLICY_FILE` | Mounted policy JSON file |

The operator refreshes the token file; each HTTP request reads the current token.
The worker principal needs only its task/credential grants. The worker registers
one instance with capacity one and drains on `SIGTERM` or `SIGINT`. Lease renewal
failure cancels the running model call. No database or administrator credentials
are needed.

```sh
workers/agentic/.venv/bin/weave-agentic-worker
```

Or build the independent image from the repository root:

```sh
docker build -f workers/agentic/Dockerfile -t weave-agentic-worker:local .
```

Run it with the environment settings and read-only mounted policy/token files.
The image runs as UID 65532; make those files readable by that UID. It uses the
separate locked Python 3.13 environment and does not change the server image.

## Reasoning, limits, and results

Model reasoning effort (`options.reasoning`) is separate from the orchestration
pattern (`reasoning.pattern`). The patterns are `none`, `react`,
`chain_of_thought`, `plan_and_execute`, `reflexion`, `tree_of_thoughts`, and
`goal_decomposition`. They run without user-defined tools or persistent memory.
Each invocation constructs a new agent; workflow and assistant configurations
must be authorized separately.

`options.max_tokens` limits each model response. `maxCalls` bounds all model
calls across a reasoning pattern and its final structured answer. `timeoutSeconds`
bounds the entire task handler, including connection and credential lookups.
The worker additionally rejects provider-reported output usage above
`maxCalls * options.max_tokens`. Provider-reported token limits are checked after
responses; they are not an exact advance guarantee of billed input tokens.
`reasoning.maxSteps` bounds pattern iteration; a shared call budget also bounds
patterns that fan out. Tree of thoughts uses two candidate branches.

After reasoning, one final call synthesizes the declared result through a
structured output schema. This call uses the same request and time budget.
Without a reasoning pattern, there is only the structured answer call. Any
internal provider failure prevents completion, even if a pattern returns a
partial result. The result must pass Weave's bounded schema validator.

The only completion payload is:

```json
{
  "result": {"approved": true},
  "usage": {"requests": 2, "inputTokens": 120, "outputTokens": 40},
  "provider": "openai-chat",
  "model": "gpt-4o"
}
```

Raw reasoning traces, provider response objects, credentials, and exception text
are not returned or logged. The worker disables framework/provider content logs
and reports safe failure codes such as `LLM_LIMIT`, `LLM_TIMEOUT`, and
`LLM_OUTPUT`. Responses API storage is disabled.

Accepted results are durable workflow data. The profile's `outputSchema` is
part of the pinned result contract: a result containing an `x-secret` or
`writeOnly` value is rejected before its value or output hash enters history.
The run opens an incident with `WV-SCHEMA-SECRET_VALUE`. Manual reconciliation
uses the same profile contract; it cannot admit the rejected secret. Simulation
mocks also pass that contract before being stored, and history exports apply
the profile's classification defensively.

This guarantee applies to declared classifications. It does not detect
undeclared sensitive text. Keep credentials in connection secret slots, and
declare confidential result fields in the profile schema. Provider handling
of prompts and responses still follows the operator's provider agreement.

## Verify the worker

```sh
cd workers/agentic
uv run --locked pytest
uv run --locked ruff check src tests
uv run --locked mypy
```

Tests execute real Agentic patterns with deterministic fake
models, verify all five provider constructors, and exercise the real Weave SDK
worker through mocked HTTP lease/context/credential/completion endpoints. These
checks do not establish live model availability or provider account permissions.
The platform integration tests additionally activate the canonical catalog,
claim a real database-backed task, enforce the pinned credential grant, and
verify completion, output guards, replay, and classified-result rejection.
