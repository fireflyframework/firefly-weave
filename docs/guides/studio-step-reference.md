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

# Choose and configure a workflow step

Use this page alongside [Studio](studio.md). The editor works with the same
versioned definition language as YAML, the CLI, and the Python SDK. A step's
**kind** determines its properties; adding a field that belongs to a different
kind does not add that behavior.

Studio and native human tasks are included in alpha5. Install the matching host
and browser bundle using the [Studio installation guide](studio.md#install-the-alpha5-browser-application).

## Start with the outcome you need

| I need to… | Choose | What happens during a run |
| --- | --- | --- |
| Call a service, system, or connector | **Call an integration** (`action`) | Weave schedules a published Action and its admitted implementation |
| Build or select data | **Transform** (`transform`) | Weave evaluates an expression without an external call |
| Take one route based on data | **Decision** (`switch`) | Weave evaluates cases in order and follows the selected branch or default |
| Run independent branches | **Parallel** (`parallel`) | Weave runs named branches within the configured concurrency |
| Delay for a fixed period | **Wait** (`wait`) | Weave records a durable timer; a worker does not sleep for the duration |
| Wait for an external event | **Signal** (`signal`) | Weave waits for an authorized, schema-valid signal with the configured name |
| Ask a person to decide | **Human task** (`humanTask`) | Weave creates an assigned task with a form and allowed decisions |
| Stop with a business error | **Fail** (`fail`) | Weave records the supplied error code and message |

Start and End mark the boundaries of the process. They are fixed visual nodes,
not extra executable steps. Add work between them using an insertion target or
the palette. A blank Start → End process can return its input or a literal output;
it does not need an artificial Transform just to be a workflow.

## Understand integration calls before configuring one

![From the workflow step to the external system](../diagrams/studio-integration-call.svg)

There are three definitions involved:

1. The **Workflow step** says which Action version to call, what input to send,
   and which logical connection slot to use.
2. The **Action** describes the reusable operation: input/output schemas,
   implementation, timeout, retry policy, and side-effect behavior.
3. A **Connector** declares operations that its installed adapter can execute.
   An Action can instead use a remote worker task.

For example, a step can call your published `notify-customer@1.0.0` Action. That
Action may use an email connector's send operation. Another version could use a
worker implementation, but a workflow pinned to version `1.0.0` does not silently
switch implementations when someone publishes version `2.0.0`.

In the step's properties:

- **Action version** is the exact published `name@version`, not a destination URL.
- **Input** supplies business data matching the Action's input schema.
- **Connection slot**, when required, names a slot declared by the workflow.
  Activation maps that slot to an authorized connection revision for the chosen
  environment. It is not a field for passwords or access tokens.

The Action's timeout and retry policy are part of its published contract. To
change those settings, author and publish a new Action version and deliberately
select it in the workflow. Do not add `retry` or `timeoutSeconds` to an action
**step**: those fields are not part of that step's contract.

A valid graph alone does not install connector code, start a worker, create a
connection, or grant access. Use [connector authoring](custom-connectors-tutorial.md)
and [worker deployment](../operations/deployment.md) to prepare those dependencies.

## Configure each step

| Step | Editable configuration | Why it matters |
| --- | --- | --- |
| Integration | Exact Action version, input expression, optional connection slot | Chooses the reusable operation and its per-call business data |
| Transform | Value expression | Its result becomes the step output available to later steps |
| Decision | Ordered case conditions, case outputs, default output, and nested steps | Every route has an explicit output, including an empty branch |
| Parallel | Named branches, branch outputs, nested steps, positive concurrency | Controls independent work and the shape of the combined result |
| Wait | Positive duration in seconds | Sets a durable relative delay |
| Signal | Signal name, positive timeout, payload schema | Defines which event can resume the wait and what data is accepted |
| Human task | Assignment binding, title/context expressions, form schema, decisions, optional due/expiry seconds | Defines who may claim the work and what a valid decision contains |
| Fail | Error code and nonempty message | Gives operators and calling products a business failure they can recognize |

Every step also has a unique ID. References to earlier step results use that ID;
renaming or moving steps can affect reference scope. The compiler checks those
relationships. A nested branch cannot freely read results from a sibling branch.

**Due time and expiry are different.** A human task can become due without being
approved or rejected. Expiry ends the opportunity to complete it according to
the runtime's task behavior. See [human tasks](human-tasks.md) for claims,
eligibility, decisions, and audit history.

## Decide where a value comes from

A literal is fixed data; a reference reads execution data; an expression combines
values. These are the same rules in the property editor and YAML:

```yaml
# A fixed string, unchanged for every execution.
value:
  literal: "Ready for review"
```

```yaml
# Read the customer ID from this execution's input.
value:
  ref: /input/customerId
```

```yaml
# Build a business object from a reference and a fixed value.
value:
  object:
    customerId: {ref: /input/customerId}
    channel: {literal: email}
```

```yaml
# A decision condition: check whether the request is marked urgent.
when:
  op:
    name: eq
    args:
      - {ref: /input/urgent}
      - {literal: true}
```

Supported operations are `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `and`, `or`, `not`,
`exists`, and `coalesce`. This is a bounded expression language, not JavaScript
or arbitrary Python. The [schema and expression reference](../reference/schema-profile.md)
explains type rules and JSON pointers. Validate after changing a reference or condition.

## Configure the whole workflow too

Step properties are only part of a complete definition:

| Workflow property | Purpose |
| --- | --- |
| Name and version | Identify the definition and the version you intend to publish |
| Input schema | Describes the business data required when starting a run |
| Output schema | Describes the result returned when the run finishes |
| Output expression | Selects or constructs that final result |
| Connection slots | Declares the logical connections that activation must bind |
| Optional timeout in seconds | Bounds the workflow's total execution time |

For a first local example, keep object input/output schemas and a literal empty
object output. Add a Wait with a short duration, then validate. For an integration
example, choose an Action that your project actually publishes and use its schema
to build the input. A made-up Action name cannot become executable by drawing it.

## Check before running

1. **Apply configuration** to commit the property edits to the local definition.
2. **Validate** and fix the reported field, expression, or reference errors.
3. When connected, confirm catalog validation resolves every Action and Connector.
4. Save and publish the version, then activate it with the required connection,
   worker, and human-assignment bindings.
5. Start one execution with schema-valid input and inspect its progress.

The [Studio guide](studio.md#save-publish-activate-and-run) explains each lifecycle
button. [Execution management](execution-management.md) covers multiple runs,
business keys, archive/restore, and permanent content removal.
