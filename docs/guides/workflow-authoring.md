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

# Author and deploy a workflow

Start with [core concepts](../concepts.md). Definitions use the `weave/v1alpha1`
language and explicit schemas. Reuse the [onboarding workflow](../../examples/definitions/customer-onboarding.workflow.yaml)
and its [Action](../../examples/definitions/check-customer.action.yaml) to inspect
real wire spelling, step references and typed input/output.

```sh
weave workflow validate examples/definitions/customer-onboarding.workflow.yaml --output json
weave workflow compile examples/definitions/customer-onboarding.workflow.yaml \
  --catalog tests/fixtures/catalog/onboarding.lock.json --strict --output json
weave workflow explain examples/definitions/customer-onboarding.workflow.yaml \
  --catalog tests/fixtures/catalog/onboarding.lock.json
```

The first command performs partial validation. The next commands resolve the
immutable catalog and explain the compiled dependency/control structure. A useful
editor preserves each diagnostic code, source span and structured path rather
than flattening errors into an unstructured string. See [compiler diagnostics](../reference/compiler.md)
and the [schema/expression profile](../reference/schema-profile.md) for limits and
supported operators. Present secret-classified workflow values are rejected;
credentials belong in authorized connection secret references.

For Python authoring, the immutable `WorkflowBuilder` uses the same canonical
models as YAML/JSON. Each change returns a new builder. The executable example
in [embedding](../reference/embedding.md) compiles against an empty catalog because
its transform does not call an external Action.

## Move from compilation to durable execution

1. An operator provisions the host's verified identity link and local scoped grants.
2. The host reads the catalog and saves a draft using the [typed SDK](../reference/sdk.md)
   or [native API](../reference/api.md), supplying the expected revision for updates.
3. Register/admit the required worker releases and create immutable connection
   revisions with scoped secret references. Compilation alone does not do this.
4. Publish an immutable version and activate it with the required bindings.
5. Start a run through the scoped host API. Inspect its pinned activation/artifact,
   then supply authorized signals or worker completions as the workflow requires.
6. Use [history/replay](../reference/history-and-replay.md) and [incidents](../reference/incident-operations.md)
   to inspect evidence and operator controls. Use [simulation](../reference/simulation.md)
   with explicit mocks before connecting external effects.

The [host example](../../examples/host_product/client.py) demonstrates the existing
`prepare`, `trigger`, `approve`, and `inspect` flow after its documented fixture
prerequisites are supplied. It is not a credential/bootstrap bypass. The workflow
compiler fixture above and the authenticated host example have different purposes.

Publish a new immutable version to change behavior. Activate that version for
new work; existing runs retain their pins. Retiring authoring state preserves
historical revisions and is separate from deleting runtime evidence.
