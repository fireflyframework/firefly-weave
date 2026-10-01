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

## Follow the tutorial

If this is your first visit, follow these chapters in order. They form one path
from an empty checkout to an integration worker. Keep the checkout and local
installation created in earlier chapters; later chapters explain which files and
terminal variables they reuse.

| Chapter | You will learn | Successful checkpoint |
| --- | --- | --- |
| **1. [Write and simulate a workflow](quickstart.md)** | YAML, schemas, validation, compilation, and simulation | A local simulation returns `Hello, Weave` |
| **2. [Run a workflow through the API](guides/standalone.md)** | PostgreSQL, Keycloak, identity links, roles, API startup, publication, and activation | The API, SDK, and CLI read the same saved successful run |
| **3. [Add an integration worker](operations/deployment.md)** | Worker identity, release admission, container packaging, and an HTTP effect | A separate worker completes a workflow and returns the receiver's result |
| **4. [Integrate your own product](guides/host-integration.md)** | Choose an integration model and use a scoped client | Your application can publish, start, and inspect a workflow |

The [concepts guide](concepts.md) explains unfamiliar names using a customer-onboarding
example. The [workflow authoring lab](guides/workflow-authoring.md) extends chapter 1
with a deliberate mistake, diagnostics, repair, and debugging.

The tutorial uses local development services. It does not provision a production
identity tenant or a live messaging account. Before deploying beyond your machine,
read [configuration](operations/configuration.md), [identity and secrets](operations/identity-and-secrets.md),
and the [capability matrix](capabilities.md).

## Choose a task after the tutorial

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
