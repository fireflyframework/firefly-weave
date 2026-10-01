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

![Firefly Weave — workflow orchestration and integration](assets/banner.svg)

# Firefly Weave

**Define a business process, connect its steps to other systems, and follow every execution.**

[![License: Apache 2.0](assets/badges/license.svg)](LICENSE) [![Python: 3.12+](assets/badges/python.svg)](pyproject.toml) [![Maturity: alpha](assets/badges/alpha.svg)](docs/capabilities.md)

Weave is an API-first workflow and integration platform built on
[PyFly](https://github.com/fireflyframework/fireflyframework-pyfly). Use YAML, JSON,
or the Python SDK to describe a process. Weave checks its definition, stores each
execution in PostgreSQL, and assigns integration work to workers.

For example, your product could accept an order, ask another system to check the
customer, wait for an approval, and send a notification. Your product starts the
workflow and reads its status through an API. A worker performs the external
calls. The workflow definition determines what happens next.

You can run Weave as a standalone service or integrate it into another product.
The current interface is the **API, CLI, and Python SDK**. There is no graphical
workflow editor in this alpha.

## Start here

Follow these chapters in order. Each chapter explains its prerequisites, commands,
expected results, and the state carried into the next chapter.

| Chapter | What you will do | What you need |
| --- | --- | --- |
| **1. [Write and simulate your first workflow](docs/quickstart.md)** | Create a small YAML definition, validate it, compile it, and inspect its output | Git, Python 3.12+, `uv` |
| **2. [Run a workflow through the API](docs/guides/standalone.md)** | Start PostgreSQL and Keycloak, create an identity, launch Weave, and inspect a real saved run | Chapter 1 checkout; Docker with Compose |
| **3. [Run an integration worker](docs/operations/deployment.md)** | Package a worker, connect it to the API, and execute an HTTP integration | The running installation from chapter 2 |
| **4. [Connect your own product](docs/guides/host-integration.md)** | Publish definitions, start workflows, and read their state from your application | The API and scoped identity from chapter 2 |

Start with chapter 1 even if your eventual goal is deployment. It explains the
language without requiring a database or identity server. Read
[the core concepts](docs/concepts.md) alongside the tutorial when a term is new.
The [documentation home](docs/README.md) organizes the remaining guides and references.

## What can I build with it?

- **Business processes:** versioned workflows with typed inputs and outputs,
  branches, parallel work, waits, signals, and schedules.
- **Integrations:** HTTP/webhooks, PostgreSQL, Kafka, and packaged connectors.
  The [OpenAPI importer](docs/connectors/metadata-import.md) generates supported
  HTTP connector definitions that you can review and publish.
- **Messaging workflows:** Teams personal-bot text, WhatsApp Cloud API
  text/templates/statuses, and Telegram webhook text.
- **Product features:** expose workflow authoring through your own product and
  call Weave's API or SDK to manage definitions and executions.
- **Operations:** inspect run history, simulate with mocks, investigate incidents,
  and manage workers, identity, retention, backup, and restore.

See the [capability matrix](docs/capabilities.md) for precise scope. Salesforce,
SAP, and Oracle do not have bundled named adapters; supported HTTP interfaces can
be integrated through reviewed HTTP profiles or connector packages.

## Offline quickstart

This installs the source checkout and confirms that the CLI is available:

```sh
git clone https://github.com/fireflyframework/firefly-weave.git
cd firefly-weave
uv sync --locked --python 3.12
uv run weave version --output json
```

Already have this checkout? Run the last two commands from its root instead.
You do not need to install or run the PyFly repository separately.
Continue with [chapter 1](docs/quickstart.md) to create and execute the local
simulation. Installing dependencies uses the network; the compiler and simulator
then work with local files.

## How the pieces fit together

![Weave API, compiler, PostgreSQL, identity provider, and workers](docs/diagrams/system-context.svg)

Your application sends requests to the **Weave API**. **Keycloak** identifies the
caller; Weave's own grants decide what that caller may do. **PostgreSQL** keeps
workflow versions, runs, and task state. A **worker** asks Weave for a task,
performs the work, and reports its result. Remote workers can run in separate processes
or containers and do not need database credentials.

The API is one modular application built with native PyFly controllers and
services. You can deploy workers separately without splitting the platform into
many microservices. See [architecture](docs/architecture.md) for a narrated
execution path and the detailed diagrams.

## Current release and limits

Weave is an **alpha**. Download packages and checksums from
[GitHub Releases](https://github.com/fireflyframework/firefly-weave/releases).
The checked-in documentation describes the source on its branch; a release tag
preserves the documentation and code for that release.

External work is delivered at least once. If a worker crashes after another
system accepts a request, that effect may already have happened. Use that system's
idempotency support or an explicit reconciliation process. The
[worker guide](docs/guides/workers.md) explains this with an example.

Messaging integrations have local protocol and backend verification; live account
setup and delivery still need validation in your environment. Generic OIDC and
Entra claim profiles do not imply live certification for every identity provider.

## Contribute and learn more

- [Documentation](docs/README.md) and [architecture](docs/architecture.md).
- [Contributing](CONTRIBUTING.md), [source attribution](docs/contributing/source-documentation.md),
  and [visual assets](docs/visual-assets.md).
- [Security reporting](SECURITY.md) and [changelog](CHANGELOG.md).

Firefly Weave is part of the [Firefly Framework](https://github.com/fireflyframework)
ecosystem and is licensed under [Apache License 2.0](LICENSE).
See [NOTICE](NOTICE) for attribution and dependency boundaries.
