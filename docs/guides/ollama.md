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

# Run AI tasks with Ollama

Run the AI task step (`kind: llm`) on a model that runs on your computer. On the
Docker development platform, `weave platform ai enable` sets up everything: the
AI gateway, the Agentic worker, the AI policy, development-only private-origin
entries for the model endpoint and an `ollama-local` connection that uses no
credential. Nothing here applies to production deployments.

## Before you start

- A Docker platform created with `weave platform up` and its demo environment
  (`local`); see [Keep a development platform running in Docker](docker-development.md).
  If you gave `up` a custom `--directory`, put the same `--directory PATH` before
  `ai` in every command below.
- About 5 GB of free disk for the worker image and a small model, and about 5 GB
  of memory for the platform with `qwen2.5:1.5b` (about 7 GB with `qwen3:4b`).
- For `--ollama host`, Ollama on this computer, listening where containers can
  reach it and started with `OLLAMA_CONTEXT_LENGTH=8192` (see
  [When something goes wrong](#when-something-goes-wrong)).

## Enable AI

```sh
# Development only: run Ollama in a Weave-managed container and pull a small model.
weave platform ai enable --ollama container --model qwen2.5:1.5b --yes
```

Expected, at the end:

```text
AI is ready on this platform (development only).
Ollama: container · http://ollama:11434/v1
Models: qwen2.5:1.5b
Connection: ollama-local (revision ID …)
Test: qwen2.5:1.5b answered in 1.2 s and can call tools.
Changed: …
Status: weave platform --directory /path/to/.local/platform ai status
```

`enable` also turns Weave AI on for `ollama-local` with the tested model,
1,024 output tokens and three minutes per answer, unless Weave AI is already set
up; then it keeps that configuration and says so (`weave_ai` in
`--output json` is `configured`, `unchanged` or `kept`).

The `Changed:` line names what this run set up or updated; a repeated command
that finds nothing to do prints `No changes.` instead. A `Warning:` line appears
when something needs your attention, such as a lowered context size.

Without `--yes`, a terminal asks before each download and before adding the
private-origin entries; a script without a terminal stops and names `--yes`.
Add `--verify` to also run a one-step AI workflow and replay it. Repeating the
command changes only what differs.

If an AI private-origin update is interrupted, the next platform command finishes
its recorded transition under the installation lock. Changed files or unrecognized
pending records still stop the command. Secret repairs save opaque mount generations
before publication so the API, gateway or worker that consumes a repaired secret is
recreated. Superseded copies stay inside the private installation directory until
that installation is removed; do not delete copies mounted by running containers.

For worker sign-in, enable includes the managed Keycloak realm's existing `basic`
scope in token responses. It preserves the scope's mappers and existing grants;
this local identity setting remains after `ai disable`.

The worker uses the endpoint policy's structured output mode: native JSON schema
for Ollama, tool output by default for cloud providers, or prompted JSON when
selected. If the model profile does not support native JSON schema, it chooses
prompted output before making the request. A rejected `response_format` is a
failed call; it does not retry with another mode. Output validation and request,
token and time limits still apply.

| Choice | Ollama runs | Endpoint |
| --- | --- | --- |
| `--ollama container` | In a Weave-managed `ollama` service with 8,192 tokens of context; models live in the `weave-local-ID-ollama` volume | `http://ollama:11434/v1` |
| `--ollama host` | On this computer | `http://host.docker.internal:11434/v1` |
| `--ollama auto` | On this computer when it answers on port 11434, otherwise in a container | as above |
| `--ollama-url URL` | On a server you run on a private network; Weave never pulls models there | `URL/v1` |

`--ollama-url` takes a plain `http://` origin on a private network, never a
loopback or metadata address or one of this platform's own services. Once AI is
enabled, switching to another choice needs `weave platform ai disable` first.

On macOS, Docker cannot use the Apple GPU, so `--ollama host` is much faster
when Ollama runs on your Mac; `weave platform ai status` warns about CPU-only
inference when Ollama runs in a container on macOS.

## Set up models in Studio

Open **Settings › AI models**. The **Readiness** checklist shows what the platform checked: the Agentic worker, the
published AI actions, a worker online, an AI connection, its key and authorization, a model that answered a test in
the last 24 hours, and Weave AI. Each item you can fix has its button: **Publish** publishes the built-in AI actions,
**Authorize** lets the worker use your credentialed connections, and **Test connection** opens the connection to test.

Choose **Connect** on the Ollama card (**Manage** after `weave platform ai enable`) to open the AI connection side
sheet. Pick an approved endpoint; plain `http://` addresses work only when a platform operator approved them, and
local endpoints use no credential. **Save connection**, choose a model (Studio suggests `qwen3:4b` when it is
installed) and **Test connection**. A first test of a local model can take up to two minutes while the model loads;
**Cancel** stops it.

On the **Weave AI** card, choose `ollama-local` and a model, then **Save Weave AI settings**; Weave AI uses the keyless connection without a credential, and every item turns ready.

A keyless connection reaches only an origin a platform operator approved in the private-origin file, on loopback or
private networks (RFC 1918, CGNAT such as Tailscale, or IPv6 ULA), never a public host. An approved Ollama on another
machine of your LAN or tailnet is reached over plain HTTP, so the draft, run facts and questions Weave AI sends cross
that network unencrypted; approve such an origin only for a network you trust.

## Choose a model

| Use | Model | Download |
| --- | --- | --- |
| Default offered when no model is served | `qwen3:4b` | about 2.5 GB |
| Quick checks and continuous integration | `qwen2.5:1.5b` | about 1.0 GB |
| A model without tool calling, to see that error | `gemma3:270m` | about 0.3 GB |

Weave AI asks a thinking model such as `qwen3:4b` to answer without thinking,
which needs **Ollama 0.40 or later**. Ollama 0.12.3 refuses that setting for
every model, so on an older Ollama Weave AI does not send it, and a thinking
model may spend its whole answer thinking: Weave AI then says to upgrade Ollama
or choose a model that does not think. With `--ollama host`, upgrade the Ollama
you run. A container installation moves to Ollama 0.40.6 on the next
`weave platform ai enable`, which keeps its `ollama-models` volume and the models
in it.

```sh
# Pull another model, then list what the endpoint serves.
weave platform ai models pull qwen3:4b --yes
weave platform ai status
```

`weave platform ai status` lists each served model with its tool support and the
context size Ollama reports, the state of the AI gateway, the Agentic worker and
the Ollama service, and the last connection test. `models pull` is not available
with `--ollama-url`: pull on that server with `ollama pull NAME`, then run
`weave platform ai models refresh`.

The AI policy approves every model the endpoint serves. To approve an exact list
instead, approve models one at a time; `--served` approves every served model again:

```sh
# The first approval replaces "every served model" with this one model; later ones add to the list.
weave platform ai models approve --provider openai-chat --model qwen3:4b
# Withdraw one model's approval; the other models stay approved.
weave platform ai models remove --provider openai-chat --model qwen3:4b
# Approve every model the endpoint serves again.
weave platform ai models approve --provider openai-chat --served
```

The API, the worker and the AI gateway read the policy again at their next use,
so no restart is needed. After a model's approval is withdrawn:

- A new activation of a workflow whose AI task uses that model answers 422
  `WV-ACTIVATION-AI-MODEL`, and nothing is created. A step whose `provider`
  differs from the connection's provider answers 422 `WV-ACTIVATION-AI-PROVIDER`.
- Activations created earlier stay active, and their AI tasks fail at the
  worker with `LLM_POLICY`.
- Approval does not install a model: an approved model that Ollama does not
  serve still fails at the worker with `LLM_MODEL_NOT_FOUND`.

If an exact approval leaves no eligible served model and enable stops after writing
the policy, use `models approve` to choose an available model (or `--served`), then
rerun `enable`. Approval recovery validates the saved installation and policy; it
does not itself finish setup or start services.

The policy's context size is the smaller of 8,192 tokens and the context Ollama
reports for each approved model, whether you approved an exact list or every served
model. Weave measures it when it reads the served models (`enable`, `models pull`
and `models refresh`) and again whenever the approval changes. `weave platform ai
status` names the model that lowered it. A served model that reports fewer than 512
tokens of context is left out of the approval, with a warning, and the policy then
lists the other served models by name: a model you pull outside Weave on such an
endpoint is approved only after `weave platform ai models refresh` (or
`models pull`). Run `models refresh` after any pull outside Weave so the context
size reflects it.

## Use the model in a workflow

Use the AI task example in [Run AI steps](ai-workers.md#author-your-first-ai-task)
with `provider: openai-chat` and `model: qwen2.5:1.5b`. At activation, bind the
connection slot to the revision ID of `ollama-local` (printed by `enable`) and the
task to the worker release. `weave platform ai status --output json` reports both
as `connection_revision_id` and `release_id`.

Keep `options.max_tokens` at 4,096 or less: a larger value fails with
`LLM_OPTIONS`. The worker counts the prompt, the context and the result fields
conservatively (ordinary words at about 2.5 characters per token; identifiers,
digits and punctuation runs at one token per character; text outside ASCII,
such as Chinese or emoji, at one token per UTF-8 byte), adds `max_tokens`, and
fails with `LLM_CONTEXT_LIMIT` before any request when the sum does not fit the
policy's context size, instead of letting Ollama cut the prompt silently. When
Ollama reports fewer prompt tokens than the whole prompt can count, the answer
is not used and the step fails with `LLM_CONTEXT_LIMIT` too. These checks cover
the task's first model call: the later calls of a reasoning pattern, or the
retry that puts the result format in the prompt, are not checked again. When Ollama reports no
prompt count at all, that check cannot run: the worker logs a warning
(`weave_agentic_worker.prompts`) and uses the answer.

## Test the connection

`weave connections test ID`, with the connection's revision ID, calls one approved
model through the AI gateway (the first the endpoint lists) and answers whether it
worked; model and gateway failures read as `failed`. The `ai_connections.test`
operation (API and SDK) tests the model you name, reports the answer time and
whether the model can call tools, and never returns the model's text. Both need
`connection.manage`, and AI connection tests, dedicated or generic, share a limit
of six per minute per person; exceeding it answers 429 `WV-AI-RATE-LIMITED`.

## See models and readiness through the API

The platform API answers what `weave platform ai status` prints, for scripts and
other clients. Every operation works in one environment and rechecks your
current grants:

- `ai_endpoints.list` lists the approved endpoints, such as `ollama-local`, marked
  as development only. It needs `catalog.read`.
- `ai_models.list` with `connection=REVISION_ID` returns the approved models from
  the last refresh, with `catalog.read`. Add `refresh=true` (which needs
  `connection.manage`) to ask Ollama again through the AI gateway. Each model
  reports `tools: yes`, `no` or `unknown` from Ollama's own model details, and
  the context size and download size Ollama reports. A refresh reads at most 50
  models, in name order, within 10 seconds, and never pulls a model: use
  `weave platform ai models pull`.
- `ai_readiness.read` returns eight checks, from the installed worker to the last
  connection test, each `done`, `action`, `not_needed` or `unknown`. A check you
  may not read is `unknown` with the role it needs. The `ollama-local` connection
  needs no key and no worker authorization, so those checks are `not_needed`.
- `ai_setup.publish` and `ai_setup.grant` repeat the publication and worker
  authorization that `enable` performs; repeating them changes nothing.

The model list is kept in the API process for up to five minutes. After a
restart it reads `discovery: "unknown"` until the next refresh, test or
`enable`; a read without `refresh=true` never calls Ollama. Refreshes and
connection tests share the limit of six per minute per person. See
[AI discovery, readiness and setup](../reference/api.md#ai-discovery-readiness-and-setup)
for every field.

## What enable sets up

- **AI policy** `ai-config/ai-policy.json` in the installation directory, version 2,
  with one `ollama-local` endpoint: no credential, every served model approved (or the exact list you
  chose), 8,192 tokens of context (or the smaller value the models report, with a
  `status` warning) and at most 4,096 output tokens. Do not edit it: `status`
  warns about a file that changed outside platform commands, the `models` commands
  refuse to continue, and `weave platform ai enable` restores it.
- **Private-origin entries**, development only, in the installation's
  `private-origins.json`: the Ollama origin for model calls, with no credentials
  and networks taken from the installation (the platform subnet, the host-gateway
  address or the resolved addresses); the AI gateway on loopback; and loopback
  entries for the worker's sign-in to Keycloak and its calls to the API.
- **Connection** `ollama-local`: `openai-chat` at the Ollama endpoint with the
  reserved secret handle `no-credential`, which is never resolved, leased, logged
  or sent.
- **Services**: the AI gateway and the Agentic worker run read-only as UID 65532
  in the same network namespace as the API. `weave platform start` and `up` start
  them again, and `weave platform stop` stops them.

```sh
# Stop the AI services and remove their settings; downloaded models stay.
weave platform ai disable
```

`disable --remove-model-data` also deletes the Weave-managed Ollama volume. The
connection, the worker release and the definitions stay, and `enable` reuses them.
Before deletion, Weave checks the volume's current Compose project and volume labels;
it refuses a same-named volume whose labels belong to another owner.

## When something goes wrong

Model failures reach a workflow step as these codes. Weave reports codes only,
never the provider's reply or your prompt.

| Code or message | What it means | What to do |
| --- | --- | --- |
| `LLM_MODEL_NOT_FOUND` | The model is not installed on the endpoint | `weave platform ai models pull NAME`, or `ollama pull NAME` on that server |
| `LLM_UNREACHABLE` | Weave could not reach the endpoint | Check that Ollama is running |
| `LLM_POLICY` | The model or endpoint is not approved, or the policy file is invalid | Approve the model; restore the policy with `weave platform ai enable` |
| `LLM_CONTEXT_LIMIT` | The prompt does not fit in the model's context | Shorten the prompt or context, or lower `max_tokens` |
| `LLM_NO_TOOL_SUPPORT` | The model cannot call tools | Choose a tool-capable model such as `qwen3:4b` |
| `LLM_TIMEOUT` | The model did not answer in time | Local models are slow on first load; raise `timeoutSeconds` |
| `LLM_OUTPUT` | The answer did not match the result fields | Simplify the result schema or use a larger model |
| `LLM_OPTIONS` | A model option is above the policy's limit, such as `max_tokens` over 4,096 | Lower the option |
| `LLM_CAPACITY` | Weave was busy before the model call | Retry the failed step when capacity is available |
| `LLM_CONNECTION` | The AI connection does not match the step | Choose the connection again at activation |
| `LLM_LIMIT` | The step reached its token or request limit | Raise `maxCalls` or `max_tokens` |
| `LLM_REASONING` | The reasoning pattern did not reach a final answer | Use the `none` pattern, or simplify the task |
| `LLM_INPUT` | The AI step input or its model options are not valid | Check the prompt, the context and the profile's options |
| `LLM_AUTH`, `LLM_RATE_LIMITED` | A credentialed provider refused the key or is limiting requests | Check the secret handle; wait and retry |
| `LLM_PROVIDER` | Any other provider failure | Inspect the endpoint's own logs |

Setting up and operating AI can stop with these messages:

| What you see | What it means | What to do |
| --- | --- | --- |
| Ollama listens on 127.0.0.1 only (Linux) | Containers cannot reach Ollama on this computer | Start it with `OLLAMA_HOST=0.0.0.0:11434`, which makes it listen on every interface of this computer, or use `--ollama container` |
| Containers cannot reach Ollama on this computer (macOS) | Ollama accepts only local connections | Quit Ollama, run `launchctl setenv OLLAMA_HOST 0.0.0.0:11434`, start Ollama again, or use `--ollama container` |
| `status` warns to start Ollama with `OLLAMA_CONTEXT_LENGTH=8192` | Ollama on this computer may keep a different context size than Weave assumes | Restart Ollama with `OLLAMA_CONTEXT_LENGTH=8192` |
| This step needs your confirmation | A script ran without a terminal | Rerun with `--yes` |
| This installation already runs AI with Ollama in another mode | Another mode is enabled | `weave platform ai disable`, then enable the new mode |
| The AI policy file changed outside platform commands | Someone edited `ai-config/ai-policy.json` | Run `weave platform ai enable` to restore it |
| The AI services configuration changed outside platform commands | Someone edited `compose.ai.json` | Run `weave platform ai disable`, which restores the services file before removing it, then enable AI again |
| The Agentic worker image is missing from Docker, so the AI services were not started | `weave platform start` or `up` found no worker image | Run `weave platform ai enable`, which rebuilds the image |
| The Agentic worker did not come online within 120 seconds | The worker started but did not register | Run `weave platform ai status` and read the `agentic-worker` container logs |

The AI connection test can also answer with a problem code from the API.
`weave connections test` reports only `ok` or `failed` for these, except the rate
limit; call `ai_connections.test` for the reason.

| Problem code | Status | What it means | What to do |
| --- | --- | --- | --- |
| `WV-AI-RATE-LIMITED` | 429 | One person ran more than six AI connection tests in a minute | Wait a minute, then retry |
| `WV-LUMI-CAPACITY` | 429 | The AI gateway is already running its maximum number of calls | Retry shortly |
| `WV-AI-CONNECTION` | 422 | The revision is not an AI connection, or it has no `apiKey` secret handle | Choose a connection of the AI provider Connector; keyless connections use `no-credential` |
| `WV-AI-SECRET` | 503 | The connection's secret handle could not be read in time | Ask an operator to check the secret grant and source |
| `WV-AI-GATEWAY-MISSING` | 503 | The API has no AI gateway | Run `weave platform ai enable`, or ask an operator to deploy the gateway |
| `WV-AI-GATEWAY-UNAVAILABLE` | 503 | The AI gateway did not answer, or its answer was not valid | Run `weave platform ai status` and read the `ai-gateway` container logs |
| `WV-AI-GATEWAY-TIMEOUT` | 504 | The AI gateway did not answer in time | Retry; a model is slow on its first load |

Asking Weave AI can answer with these codes; the message names the model and the endpoint, never provider text:

| Problem code | Status | What it means | What to do |
| --- | --- | --- | --- |
| `WV-LUMI-MODEL-NOT-FOUND` | 422 | The model is not installed on the endpoint | Pull it with `ollama pull`, or choose another model on the Weave AI card |
| `WV-LUMI-UNREACHABLE` | 503 | Weave could not reach the model endpoint | Check that Ollama is running |
| `WV-LUMI-CONTEXT-LIMIT` | 413 | The request does not fit the model's context | Attach less, or choose a model with a larger context |
| `WV-LUMI-OUTPUT` | 422 | The model's answer did not match Weave AI's reply format | Ask again; a larger model answers more reliably |
| `WV-LUMI-TIMEOUT` | 504 | The model did not answer in time | Retry; a local model is slow on its first load |
| `WV-LUMI-CAPACITY` | 429 | Weave AI or the provider is busy | Retry shortly |
| `WV-LUMI-UNAVAILABLE` | 503 | Weave AI is not configured, the gateway refused the request, or the model is not approved | Run `weave platform ai status`; check the Weave AI card |

Activating a workflow and the setup operations can answer with these codes:

| Problem code | Status | What it means | What to do |
| --- | --- | --- | --- |
| `WV-ACTIVATION-AI-MODEL` | 422 | The policy does not approve the step's model for the connection's endpoint | Approve the model with `weave platform ai models approve`, or choose an approved one |
| `WV-ACTIVATION-AI-PROVIDER` | 422 | The step's `provider` differs from the connection's provider | Use the connection's provider (`openai-chat` for `ollama-local`) or bind another connection |
| `WV-AI-POLICY` | 503 | The API cannot read its AI policy file. Only activations of workflows with AI task (`llm`) steps are refused, and setup never answers it; the endpoint and model lists answer it too | Restore the policy with `weave platform ai enable` |
| `WV-AI-WORKER-MISSING` | 422 | No admitted Agentic worker release provides the AI task | Run `weave platform ai enable`, which admits the worker release |
