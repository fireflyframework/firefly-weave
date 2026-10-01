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

![From offline authoring to an API, worker, and host product](docs/diagrams/tutorial-route.svg)

Start with the top row. After the API is running, follow the worker branch for
external Actions or the host-client branch to integrate your product.

[Open diagram at full size](docs/diagrams/tutorial-route.svg)

## Choose your starting point

You do not need to deploy the platform to try the workflow language.
Choose the row that matches what you want to do today:

| I want to… | Start here | What I will have at the end |
| --- | --- | --- |
| **Try a workflow on my laptop** | [Install the CLI](docs/installation.md), then [run the quickstart](docs/quickstart.md) | A validated YAML workflow and a successful local simulation; no server or Docker required |
| **Use an existing Weave API** | [Install the CLI](docs/installation.md), then [connect to an existing API](docs/guides/connect-to-api.md) | A verified connection; continue to the CLI tutorial to publish and run |
| **Run the platform myself** | [Start a local installation](docs/guides/standalone.md) | PostgreSQL, Keycloak, a running API, and a successful saved run |

Installing the CLI gives you a terminal client. Running the platform adds the
services that store and execute workflows. Deploying a worker adds a process that
performs external work. Each has its own guide so you can stop at the result you need.

## Install and discover the CLI

On macOS, Linux, or WSL, install **Python 3.12 or newer** with `venv` support,
then run this block in Bash or Zsh. It installs the pinned **v0.1.0a2 alpha** into
your user account without `sudo`, Git, or Docker:

```sh
(
  set -o pipefail
  curl --proto '=https' --tlsv1.2 -fsSL \
    https://github.com/fireflyframework/firefly-weave/releases/download/v0.1.0a2/install.sh \
    | sh -s -- --version v0.1.0a2
)
```

After the installer finishes, make the default command directory available in
this terminal and check the result:

```sh
export PATH="$HOME/.local/bin:$PATH"
weave --version
weave
weave help workflow
```

Expected: version `0.1.0a2` and a command overview. The
[installation guide](docs/installation.md) explains Python selection, persistent
PATH setup, upgrades, removal, and troubleshooting. Installation includes the API
client and OpenAPI import dependencies; it does not start the platform.

`weave` displays the command overview. Help explains each command family and its
next steps. The [quickstart](docs/quickstart.md) walks through a complete example;
you do not need to learn every command first.

## Continue when you need more

- **Call another system:** [run an integration worker](docs/operations/deployment.md)
  after your local API works.
- **Add workflows to your product:** follow the [host integration guide](docs/guides/host-integration.md).
- **Deploy beyond your laptop:** follow [cloud deployment](docs/operations/cloud-deployment.md),
  then the setup for [AWS](docs/operations/aws.md), [Azure](docs/operations/azure.md),
  or [Google Cloud](docs/operations/gcp.md), and the shared [Kubernetes walkthrough](docs/operations/kubernetes.md).
- **Find a specific task or diagram:** open the [documentation home](docs/README.md)
  or [visual guide](docs/visual-guide.md).

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

The recommended installation is **v0.1.0a2**, an **alpha** release. Download
packages and checksums from
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
