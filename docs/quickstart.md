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

# Quickstart

To launch the API and run your first workflow, use the [standalone quickstart](guides/standalone.md).
To begin with definitions and validation without starting services, follow the
compiler walkthrough below.

## Compile a workflow locally

Use Python 3.12+ and `uv` from a source checkout. Installation may access the
network; the compiler commands below need only local files and no credentials,
database, identity provider, server startup or external provider.

```sh
uv sync --locked
uv run weave version --output json
uv run weave workflow validate examples/definitions/customer-onboarding.workflow.yaml --output json
uv run weave workflow compile examples/definitions/customer-onboarding.workflow.yaml \
  --catalog tests/fixtures/catalog/onboarding.lock.json --strict --output json
```

Validation without a catalog returns `validationOk: true`, `partial: true`,
`ok: false`, and no artifact for this example. Complete compilation returns
`partial: false` and `ok: true`. The included catalog is a compiler teaching
fixture; it does not provision runnable connections, external systems or workers.

To export the model-derived schemas into a new directory:

```sh
uv run weave schema export --directory ./weave-schemas --output json
```

Exports refuse accidental replacement by default. Keep temporary exports and
private configuration outside source publication. See [CLI exit/output behavior](reference/cli.md).

## Choose the next path

- **Workflow author:** follow [schemas, expressions and compilation](guides/workflow-authoring.md).
- **Host-product integrator:** choose [pure, remote or in-process embedding](guides/host-integration.md).
- **Worker developer:** follow [worker implementation and admission](guides/workers.md).
- **Standalone operator:** follow [configure, launch, and run](guides/standalone.md),
  then [deploy the API and workers](operations/deployment.md).

Durable execution needs the `server` extra, explicit migrations, a nonowner app
login, separate scheduler authority, verified and linked identities, local grants,
and admitted connections/releases. There is no implicit administrative shortcut
in the quickstart. Review the [capability matrix](capabilities.md) and
[backup and restore](operations/backup-restore.md) prerequisites before deployment.

The [contribution guide](../CONTRIBUTING.md) explains offline checks and the
separate owned-backend acceptance suites. Running an end-to-end test harness is
not required to learn the compiler.
