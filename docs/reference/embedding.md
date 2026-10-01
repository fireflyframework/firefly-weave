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

# Embedding authoring and orchestration

The compiler and immutable builder work without PyFly, a server, network access, database connections or secret providers. The builder reuses the canonical definition models; it does not interpret Python callables as workflows.

## Build the same workflow without YAML

Use this when your application generates definitions, such as a form-based
workflow editor. The [authoring guide](../guides/workflow-authoring.md) writes
echo in YAML; this complete Python example builds the same input-copying behavior.
Install the base package and run it with Python 3.12 or later:

```python
from datetime import UTC, datetime
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import TransformStep, RefExpression
from firefly_weave.operations.debug.simulator import Simulator
from firefly_weave.sdk.builder import WorkflowBuilder

schema = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}
builder = WorkflowBuilder(
    "echo", "1.0.0", input_schema=schema, output_schema=schema,
    output=RefExpression(ref="/steps/echo/output"),
)
builder = builder.add_step(TransformStep(
    id="echo", kind="transform", value=RefExpression(ref="/input"),
))
result = compile_source(builder.to_document(), format="object",
                        catalog=CatalogSnapshot.empty(), strict=True)
assert result.ok and result.artifact is not None
view = Simulator(result.artifact, mocks={}, input={"message": "Hello, Weave"},
                 now=datetime(2026, 1, 1, tzinfo=UTC)).continue_until_breakpoint()
assert view.status == "succeeded"
print(view.variables["output"])
```

Expect `{'message': 'Hello, Weave'}`. The builder creates data; the compiler checks
it; the simulator evaluates it. None of those steps creates a durable run. The
[standalone tutorial](../guides/standalone.md) introduces that next stage.

Every builder update returns a new builder. `to_document()` and `to_definition()` return fresh validated values. Nested switch/parallel branches, schemas, expression models, connections and timeouts use the same canonical models as parsed documents. Builder construction enforces model validity; complete compilation remains responsible for semantic dependency, type, dominance and budget checks. Never place credentials in authoring literals.

## Choose an execution boundary

An external host uses the [typed SDK](sdk.md) and [public API](api.md) to discover catalogs, edit/retire drafts, publish custom Actions/Connectors, create connection revisions, activate pinned artifacts and operate runs. It supplies an explicitly provisioned principal with the required local grants. Worker credentials alone do not authorize authoring. No UI-only privileged path or orchestration-database access is needed by a host.

### In-process service composition

Use in-process orchestration only when you own the trusted server application.
A **unit of work (UoW)** is the transaction boundary for one operation; **row-level
security (RLS)** restricts database rows to the authorized tenant. Supplying a
transaction does not supply an identity or grant permission.

In-process embedding uses the existing native `@service` constructor-injected application graph and explicit actor, scope, audit context and transaction ports. Singleton services do not retain request actors, database sessions or per-user credential state. A supplied transaction is enlisted; callers must preserve authorization and RLS boundaries rather than constructing alternate service factories. Compilation never establishes execution authority or credential availability.

## Effects and evidence

Simulation and replay remain effect-free. Simulated connector/worker actions require mocks; recorded replay validates evidence and explicitly reports incomplete prefixes/redactions. Durable execution is at least once, with idempotency keys and receipt fences describing accepted facts. Neither an SDK retry nor a historical acknowledgment proves exactly-once external effects.

Use SQL, Kafka, outbox, Teams, WhatsApp, Telegram, or generated
[OpenAPI HTTP profiles](../connectors/metadata-import.md) as integration boundaries.
See the [capability matrix](../capabilities.md) for supported scope and verification.
