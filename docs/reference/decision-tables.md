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

# Reuse a decision table

A `DecisionTable` is a versioned, deterministic policy. Workflows reference its
name and version; compilation embeds the exact definition and its digest in the
workflow artifact. Evaluation runs inside the durable workflow reducer and does
not schedule a worker, call a provider, or read a live catalog.

```yaml
apiVersion: weave/v1alpha1
kind: DecisionTable
metadata: {name: payment-policy, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties: {amount: {type: number}}
    required: [amount]
  outputSchema: {type: string, enum: [review, approve]}
  hitPolicy: first
  rules:
    - id: large-payment
      when: {op: {name: gte, args: [{ref: /input/amount}, {literal: 1000}]}}
      output: {literal: review}
  defaultOutput: {literal: approve}
```

Use the published table in a workflow:

```yaml
- id: payment-decision
  kind: decisionTable
  uses: payment-policy@1.0.0
  with: {ref: /input}
```

Read its result at `/steps/payment-decision/output`. Table expressions can only
reference `/input` and its descendants. Rule IDs must be unique. Predicates must
produce JSON booleans; truthy numbers and strings are rejected.

| Hit policy | Matches | No matches |
| --- | --- | --- |
| `first` | Evaluates the first matching row's output; later predicates are skipped. | Evaluates `defaultOutput`, or raises `WV-DECISION-NO_MATCH`. |
| `unique` | Exactly one row may match; a second match raises `WV-DECISION-MULTIPLE_MATCHES`. | Evaluates `defaultOutput`, or raises `WV-DECISION-NO_MATCH`. |
| `collect` | Returns every matching output in row order. | Returns `[]`. |

`outputSchema` describes the complete returned value. For `collect`, it must have
`type: array` and an object-valued `items` schema, accept `[]`, and omit
`prefixItems`. Each row produces one item. `defaultOutput` is forbidden for this
policy. Output schema constraints still apply to the final collected result.

The compiler checks every row, including unselected rows, and points diagnostics
to its `when` or `output` expression. Runtime checks enforce input and output
schemas and the chosen policy. Failure suspends a durable workflow behind its
normal incident barrier. Execution evidence records matched rule IDs and whether
the default was used; it does not record predicate operands. Business output
remains separate from this evidence.

Tables have at most 1,000 rows. Expression-node, comparison-work, value-depth,
schema-work, and result-size limits apply across the complete evaluation.
Collected output is charged incrementally before it is appended. Classified
literal admission and runtime payload classification use the same policies as
other definitions.

## Publish and test

The project collection is `/api/v1/tenants/{tenant}/projects/{project}/decision-tables`.
It supports ordinary definition publication, listing, reading, export, and
retirement, with the existing capabilities, idempotency rules, and tenant/project
isolation. A published version is immutable. Retirement preserves the table
embedded in existing workflow artifacts.

Use `WeaveClient.publish("decision-tables", source, "yaml", idempotency_key=...)`
or `weave definitions publish --collection decision-tables --request publish.json`.
The request file contains `source` and `format`, as for other definitions.

For a pure authoring check, post a `DecisionEvaluationRequest` to
`/api/v1/tenants/{tenant}/projects/{project}/compiler/evaluate-decision`, call
`WeaveClient.evaluate_decision(request)`, or run
`weave remote evaluate-decision --request evaluation.json`.
The request extends a compiler request with `input`; the response contains
`output`, `matched_rule_ids`, and `used_default`. The endpoint requires `compile`
authority, uses the shared bounded compiler and evaluator, and creates no
definition, run, task, or provider call. The simulator uses the production reducer
and needs no decision-table mock.

Decision table artifacts and workflows containing `decisionTable` require
`weave/ir-v1alpha3`. Earlier IR versions remain valid for their original features.
Decision rules cannot use the text operators `concat` and `join` yet: compilation
and evaluation report `WV-DECISION-OPERATOR`. Build the text in a Transform step
and pass it to the table as input.
Apply migration `0026_decision_tables` before serving table publication. It widens
the catalog kind constraint while preserving existing catalog rows and RLS.

Studio's **Decision table** step selects a published version or opens a table editor with typed input/result fields, ordered rules, explicit matching policy, sample evaluation, YAML export, and publication. The **AI task** step configures a workflow profile through the canonical profile schema served by the local host; model, limits, reasoning, result fields, prompt and context remain explicit authoring choices.
