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

# Learn and use Firefly Weave

Weave coordinates business processes and their integrations. A workflow describes
what happens; the API stores and starts it; workers perform external work. You can
call it from your own product or operate it as a standalone API/CLI service.

![Four tutorial stages and their dependencies](diagrams/tutorial-route.svg)

The arrows identify which running installation the next chapter reuses.
Use the [visual guide](visual-guide.md) to navigate the other illustrated topics.

[Open diagram at full size](diagrams/tutorial-route.svg)

## Start with the current release

The [installation guide](installation.md#install-a-release) installs the
**v0.1.0a2 alpha** with one pinned curl command. Start there if you do not have
`weave` yet. You need Python 3.12 or newer; you do not need a source checkout,
Docker, or administrator privileges for the client. Confirm `weave --version`
before following a learning path.

An alpha is a preview release. The guides distinguish features you can verify
locally from provider or cloud setup that you must validate in your environment.

## Choose a learning path

Start with your goal. The guides are not a reading list to complete before you can
use Weave. Reference pages explain details when you need them.

| Your goal | Follow this path | Stop when… |
| --- | --- | --- |
| **Try the workflow language** | [Install the CLI](installation.md) → [quickstart](quickstart.md) | The simulator returns `Hello, Weave` |
| **Use an API someone already runs** | [Install the CLI](installation.md) → [connect to an existing API](guides/connect-to-api.md) → publish, activate, and run | The API returns `Hello from the CLI` |
| **Set up your own installation** | [Local platform tutorial](guides/standalone.md) → [CLI tutorial](guides/cli-tutorial.md) | A workflow runs and its state is saved in PostgreSQL |
| **Add an external integration** | A working local installation → [worker deployment](operations/deployment.md) | Your worker completes an HTTP request and reports its result |
| **Move the platform to cloud infrastructure** | Working local deployment → [cloud deployment](operations/cloud-deployment.md) → one provider guide → [Kubernetes](operations/kubernetes.md) | Your own environment passes the documented acceptance checks |

For a first visit, take the **workflow language** path. It requires no database,
identity server, or cloud account. If your team already runs Weave, ask its
administrator for an API URL, a supported login method, and the tenant, project,
and environment IDs before taking the second path.

## Know which tool you are using

| Name in a guide | What it means | When you need it |
| --- | --- | --- |
| `weave` | The installed command-line client | Local workflow files, remote API requests, and command help |
| `uv run weave` | The CLI from a developer's source checkout | Contributing or trying unreleased code |
| `WEAVE_PYTHON` | The exact Python executable selected for an installation | Operator examples that must use the same package and dependencies as the API |
| API | The running Weave service | Saved workflows, runs, permissions, and task coordination |
| Worker | A separate process that performs integration tasks | Workflows that call external handlers |

Use the command form shown in the guide you are following. The local platform
tutorial explains how it selects an isolated server environment; a client-only
installation does not start that server.

## Read a tutorial, then extend it

Every main tutorial states what it creates, how to recognize success, and where to
continue. Run one command block at a time and check its expected result before
moving on. When a guide creates a private work directory, keep it: later steps
reuse its configuration and returned resource IDs.

The [concepts guide](concepts.md) explains names with a customer-onboarding example.
The [workflow authoring lab](guides/workflow-authoring.md) adds a deliberate mistake,
a compiler diagnostic, and a repair. The [visual guide](visual-guide.md) maps
questions to diagrams.

The local tutorials use development services. Cloud and messaging guides describe
additional infrastructure or provider-account setup. Their verification limits are
listed in the [capability matrix](capabilities.md).

## Find the guide for your next task

| I want to… | Guide |
| --- | --- |
| Write schemas, expressions, branches, and reusable actions | [Workflow authoring](guides/workflow-authoring.md) and [definition contracts](contracts.md) |
| Implement a Python worker | [Workers](guides/workers.md) |
| Call an existing HTTP API or receive a webhook | [HTTP and webhooks](reference/http-and-webhooks.md) |
| Generate connector definitions from OpenAPI | [OpenAPI import](connectors/metadata-import.md) |
| Package an integration as a reusable connector | [Connector authoring](connectors/authoring.md) |
| Use SQL or a message broker | [PostgreSQL](connectors/postgresql.md) or [Kafka](connectors/kafka.md) |
| Trigger or signal workflows from messaging | [Teams](connectors/teams.md), [WhatsApp](connectors/whatsapp.md), or [Telegram](connectors/telegram.md) |
| Run work on a timetable or wait for approval | [Schedules, timers, and signals](reference/schedules-and-timers.md) |
| Notify my product when workflow events occur | [Integration events and outbox](reference/integration-events.md) |
| Understand a failed or stuck run | [Troubleshooting](operations/troubleshooting.md), [history](reference/history-and-replay.md), and [incidents](reference/incident-operations.md) |
| Operate an installation | [Deployment](operations/deployment.md), [observability](operations/observability.md), and [backup/restore](operations/backup-restore.md) |

## Look up a contract

Reference pages describe exact interfaces and limits. Use them while implementing
a task; you do not need to read them all before running your first workflow.

| Topic | Reference |
| --- | --- |
| Language and values | [Definition contracts](contracts.md), [schema and expression profile](reference/schema-profile.md) |
| Compiler and diagnostics | [Compiler](reference/compiler.md), [simulation](reference/simulation.md) |
| Public clients | [API](reference/api.md), [Python SDK](reference/sdk.md), [CLI](reference/cli.md) |
| Native API documentation | [Generated OpenAPI](reference/native-openapi.md) |
| Application integration | [Embedding](reference/embedding.md) |
| Worker execution | [Worker protocol](reference/worker-protocol.md) |
| HTTP integration | [HTTP profiles](connectors/http-profiles.md), [HTTP/webhooks](reference/http-and-webhooks.md) |
| Incoming provider events | [Provider sources](reference/provider-sources.md) |
| State and operator decisions | [History/replay](reference/history-and-replay.md), [incident operations](reference/incident-operations.md) |
| Runtime setup details | [Local runtime reference](reference/local-runtime.md) |

## Operate and maintain the platform

- [Configuration](operations/configuration.md): which settings belong to the API,
  workers, database, identity provider, and optional components.
- [Identity and secrets](operations/identity-and-secrets.md): how a caller becomes
  an authorized principal and how credentials reach an allowed integration.
- [Observability](operations/observability.md): inspect health, telemetry, and execution evidence.
- [Upgrades](operations/upgrades.md): plan a version/schema change and check compatibility.
- [Retention](operations/retention.md): preview and apply supported data retention.
- [Backup and restore](operations/backup-restore.md): preserve ownership, data, and authority.
- [Troubleshooting](operations/troubleshooting.md): start from a symptom and follow a diagnostic path.

## Understand the project

[Architecture](architecture.md) explains the runtime using diagrams and a narrated
request path. [Capabilities](capabilities.md) distinguishes implemented behavior,
local verification, and live-provider verification. [Contributing](../CONTRIBUTING.md)
explains development checks; [source attribution](contributing/source-documentation.md)
and [visual assets](visual-assets.md) cover source and artwork conventions.
See [security reporting](../SECURITY.md) and the [changelog](../CHANGELOG.md) for
project policy and release context.
