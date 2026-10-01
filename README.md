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

**Typed workflow orchestration and integration for Python, built on the Firefly Framework.**

[![License: Apache 2.0](assets/badges/license.svg)](LICENSE) [![Python: 3.12+](assets/badges/python.svg)](pyproject.toml) [![Maturity: alpha](assets/badges/alpha.svg)](docs/capabilities.md)

[Firefly Framework](https://github.com/fireflyframework)

Weave lets product teams define, compile, version, and run workflows with explicit
schemas and durable execution state. Embed its pure compiler in an authoring tool,
connect a host product through the typed SDK, or operate the standalone API with
native and remote workers.

[Documentation](docs/README.md) · [Architecture](docs/architecture.md) · [Offline quickstart](#offline-quickstart) · [Run the platform](docs/guides/standalone.md) · [Deploy](docs/operations/deployment.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## Current scope

This alpha includes the compiler, typed API/SDK/CLI, durable PostgreSQL runtime,
workers, simulation/replay, HTTP/webhooks, PostgreSQL, Kafka, integration outbox,
connector authoring/provider inbox, OpenAPI connector generation, and
Teams/WhatsApp/Telegram integrations.
These capabilities have local contract/backend verification. Messaging-provider
fixtures do not establish live account provisioning or delivery certification.

The [capability matrix](docs/capabilities.md) lists supported integration scopes
and verification boundaries. [OpenAPI import](docs/connectors/metadata-import.md)
produces reviewable definitions for the supported HTTP profile; arbitrary
OpenAPI features and additional named vendor adapters are outside this scope.

- [Learn the concepts](docs/concepts.md) and [author workflows](docs/guides/workflow-authoring.md).
- [Embed in a host product](docs/guides/host-integration.md) or [implement a worker](docs/guides/workers.md).
- [Run your first workflow through the API](docs/guides/standalone.md), then
  [configure identity and secrets](docs/operations/identity-and-secrets.md).
- [Deploy the API and workers](docs/operations/deployment.md) and
  [prepare backup and recovery](docs/operations/backup-restore.md).

Execution is at least once. A worker can perform an external effect before its
completion is accepted; use supported provider idempotency or reconciliation.
Compilation does not grant execution authority, and replay can report incomplete
evidence. This source tree does not promise production readiness.

## Offline quickstart

From a source checkout, use Python 3.12+ and `uv`. Dependency installation may
access the network; the following compiler commands themselves use local files
and need no PostgreSQL, PyFly server, identity provider, or credentials.

```sh
uv sync --locked
uv run weave version --output json
uv run weave workflow validate examples/definitions/customer-onboarding.workflow.yaml \
  --output json
uv run weave workflow compile examples/definitions/customer-onboarding.workflow.yaml \
  --catalog tests/fixtures/catalog/onboarding.lock.json --strict --output json
```

Validation without a catalog returns `partial: true`, `validationOk: true`,
`ok: false`, and no artifact for this example. Complete compilation with the
included fixture catalog returns `partial: false` and `ok: true`. The fixture
catalog supports learning the compiler contract; it does not provision runnable
connections or workers. See [compiler behavior](docs/reference/compiler.md) and
[CLI commands and exit codes](docs/reference/cli.md).

To use the compiler from Python:

```python
from pathlib import Path
from firefly_weave.compiler.api import validate_source

result = validate_source(
    Path("examples/definitions/customer-onboarding.workflow.yaml").read_bytes(),
    format="yaml",
)
assert result.validation_ok
assert result.partial and result.artifact is None
```

## Running the service

Durable execution requires the `server` extra, PostgreSQL, explicitly applied
migrations, configured identity verification, linked principals, scoped grants,
and provisioned connections/worker releases. The `client` extra supplies remote
SDK/CLI dependencies; `worker` supplies remote worker dependencies. Kafka has a
separate optional extra and broker configuration. See the actual
[dependency declarations](pyproject.toml) for the pinned PyFly artifact.

Follow the [standalone quickstart](docs/guides/standalone.md) to configure local
services, provision identities, launch the API, and run a workflow. Then use
[host integration](docs/reference/embedding.md) or the [worker protocol](docs/reference/worker-protocol.md).
The [quickstart](docs/quickstart.md) starts offline and points to explicit runtime
prerequisites. [Configuration](docs/operations/configuration.md) separates API,
native executor and remote worker authority. The [deployment guide](docs/operations/deployment.md)
covers packaging and launching services; the [recovery guide](docs/operations/backup-restore.md)
covers retained state and restore prerequisites.

## Architecture

![Firefly Weave modular monolith, pure compiler, PostgreSQL and external boundaries](docs/diagrams/system-context.svg)

[Open diagram at full size](docs/diagrams/system-context.svg)

Native PyFly controllers and application services share one modular monolith.
The pure compiler is independent of runtime resources. PostgreSQL persists scoped
state; native connectors and remote workers share the task/lease contract.
[Read the architecture](docs/architecture.md) for source links and execution semantics.

## Development and license

The repository's [Makefile](Makefile) defines unit/contract tests, Ruff, strict
mypy and build checks. Backend suites require explicitly owned test services.
Read the [source documentation and attribution policy](docs/contributing/source-documentation.md)
before contributing; it includes strict coverage and exact commentless-file
licensing policy. The [visual asset guide](docs/visual-assets.md)
explains editing and reproducing the SVG renders.

Firefly Weave is licensed under [Apache License 2.0](LICENSE).
See [NOTICE](NOTICE) for first-party attribution and dependency boundaries.
The sibling [PyFly framework](https://github.com/fireflyframework/fireflyframework-pyfly)
provides the native application platform.
