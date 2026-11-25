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

# Studio editor contracts

Studio's editor is extended and driven through a small set of frozen contracts:
the **step details registry** every step kind registers in, the **test data**
document that keeps inputs, pinned outputs and scripts, the **Execute step**
requests that run a step offline or in an environment, and the **storage and
keyboard** conventions Studio follows. This page is the reference for each.

**Who it is for.** Contributors who add a step kind, an AI step or an editor
feature, and integrators who read or write Studio test data. **Status.**
Version 1 of each contract is frozen. Studio and the platform add the editor
views and endpoints described here in later releases, and Studio starts applying
the storage rules then too; the types, the JSON Schema and the models already
exist, so code can be written against them.

## Field casing

| Where | Casing |
| --- | --- |
| Platform API bodies, query parameters and error `result` payloads | snake_case |
| The definition language, Studio documents (test data, canvas), and the Studio host's `/studio/local/*` routes | camelCase |
| A Studio document stored on the platform | camelCase inside a snake_case envelope |

A TypeScript interface follows the wire body it describes, so those for platform
bodies, such as `StepTestRequest`, are snake_case. One host route answers in the
platform's shape: `POST /studio/local/decision/evaluate` returns
`matched_rule_ids` and `used_default`.

## Step details registry

The registry is `studio/src/app/editor/ndv/registry.ts` (`ndv` is the code name
of step details), version 1 (`NDV_REGISTRY_VERSION`). Step details, the canvas
and the Add a step panel read it; nothing else extends step details.

| Function | Registers |
| --- | --- |
| `registerKind(descriptor)` | A step kind: names, icon, role, Add a step category, ID prefix, defaults, language feature, expression fields, nested step lists, labeled outputs, summary, output schema, whether its output can be pinned, the test data script it needs and how it runs in an environment |
| `registerParameters(registration)` | The component that edits a kind's parameters, or one AI agent slot's (`subNode`), loaded on first use |
| `registerSubNodes(kind, slots)` | The slots under an AI agent (Model, Memory, Tools, Output): whether each is required and how many entries it takes, chip data, one chip per entry of a many-valued slot, and the entries the scoped Add a step panel offers, grouped, with a reason when one can't be used |

Registering the same kind, the same parameters or the same kind's slots twice
throws `RegistryError`, as does an invalid ID prefix, a slot listed twice or a
slot maximum that isn't a whole number of at least 1. Registration modules are
listed in `studio/src/app/editor/ndv/kinds/index.ts` and imported lazily; each
exports `register()`. Studio awaits `loadKindRegistrations()` before it reads
the registry. The modules load in parallel and register in list order, so the
registry lists kinds the same way however the chunks arrive. A module that fails
to load is tried again on the next call, after the modules that did load have
registered, and a module is never registered twice.

### Built-in kinds

Role and category are the registry's `NodeRole` and `PanelCategory` values.

| Kind | Role | Add a step category | Output can be pinned | Test data script |
| --- | --- | --- | --- | --- |
| `action` | `app` | `app` | Yes | — |
| `llm` (AI task) | `ai-task` | `ai` | Yes | `ai-turns` |
| `decisionTable` | `rules` | `data` | No | — |
| `transform` | `transform` | `data` | No | — |
| `switch` (Decision) | `decision` | `flow` | No | — |
| `parallel` | `parallel` | `flow` | No | — |
| `fail` | `fail` | `flow` | No | — |
| `wait` | `wait` | `wait` | No | — |
| `signal` | `signal` | `wait` | No | `signal` |
| `humanTask` | `human` | `human` | No | `human` |
| `callWorkflow` (Call a workflow) | `sub-workflow` | `flow` | When it waits | — |
| `forEach` (Loop over items) | `loop` | `flow` | No (the steps inside are pinned per item) | — |

A nested step list lives at the path of a branch plus `steps`:
`["cases", 0, "steps"]` and `["default", "steps"]` for a decision,
`["branches", "first", "steps"]` for a parallel step. The language manifest lists
the fixed branches of a kind, such as `default` for a decision and `body` for a
loop (`["body", "steps"]`); decision cases and parallel branches are found by
position and by name. Output handles are `case:<index>` and `default` for a
decision and `branch:<name>` for a parallel step; `fail` has none.

**A step's blocks.** `blocksOf(step)` in `studio/src/app/editor/step-blocks.ts`
lists a step's blocks, each a step list with its output, in document order:
its own blocks, then its error path, as the Studio host's slicer orders them. Every walk of a workflow's
structure reads blocks through it: sequences, step IDs and rename, the
breadcrumb and Outline, copy and paste, delete, Execute's step list and the
offline scope. A new block kind is one entry there.

| `StepBlock` field | Holds |
| --- | --- |
| `name` | The block's name in its sequence's owner, `"<step>/<name>"`: `case 1` (counted from 1), `default`, a parallel branch's own name, `body`, or `errorPath` |
| `path` | Where the block sits, relative to the step; its steps are at `path` plus `steps` |
| `branch` | The block as the document holds it: `{steps, output}` |
| `kind` | `case`, `default`, `branch`, `body` or `errorPath` |

A step's error path is the `errorPath` block (`ERROR_PATH`) at `["errorPath"]`,
present on a step whose `onError` is `errorOutput`. Its owner is
`"<step>/errorPath"`, like a loop's `"<loop>/body"`, so insertion, selection
and moves work on it as on any sequence. On the canvas the step's outputs are
`<step>:path:success` and `<step>:path:errorPath`, and an empty path's slot
inserts at `{owner: "<step>/errorPath", index: 0}`. The Settings tab writes
`onError` and `errorPath` as one edit: Continue with error output writes
`onError: errorOutput` and, when the step has none, `errorPath: {steps: [],
output: {literal: null}}`; Continue writes `onError: continue` and deletes
`errorPath`; Stop the run deletes both, so `onError: stop` is never written.
**Result on error** edits `["errorPath", "output"]`. Its `ParamSpec` has
`mapping: "both"`, `default: null`, `whenRemoved: "default"` and
`emptyMapped: true`: while the field holds its default, it reads as an empty
Mapped field that shows its `placeholder` ("Empty (null)") until Fixed is
chosen, and clearing it writes `{literal: null}` back, since the language needs
the path's output.

A `forEach` step's descriptor is gated by `flow.forEach`. Its expression fields
are `["items"]` (expected schema `{type: "array"}`) and `["body", "output"]`;
its one container is `["body", "steps"]` with the `frame` layout. A new loop
starts as `{concurrency: 1, maxItems: 1000, collect: "all", body: {steps: [],
output: {ref: "/item"}}}` with no `items`. Its Parameters form shows **Items**
and offers `concurrency`, `maxItems`, `collect` and `body.output` under **Add
option**, each `whenRemoved: "default"`; `body.output` defaults to a mapping, so
its `ParamSpec` carries `defaultExpression: {ref: "/item"}` instead of
`default`, and `isDefault` and Remove read and write that mapping.

Inside a loop's body, `NdvContext.instanceKey` is the step's instance for the
items picked in the loops around it (`send[2]`, or `send[2][0]` inside two
loops); it is null outside loops and in run mode. The picks live in the step
details controller's `Iterations`, one per open draft: an index per loop, 0
until another is picked, kept while the Items of the loop and of the loops
around it are unchanged and across renames. `sample.context()` adds `item`,
`index` and `loops` for the picked items, from each loop's Items evaluated
against the current data. A pin inside a body is written under the picked
instance key (`pins["send[2]"]`), wins over the step's pin for that item only,
and is judged against the step's fingerprints; the step's own pin
(`pins["send"]`) stands for every item without one.

A `callWorkflow` step's output is the called workflow's `outputSchema` when the
call waits (the result status around it when **If it fails** continues), and
`null` when it doesn't wait; its tile summary is the reference it calls, such as
`notify-customer 1.0.0`, and a new one starts as `{mode: "wait", with:
{literal: {}}}`. `KindContext.workflowContract(uses)` returns the called
version's `{inputSchema, outputSchema, callable}`, or null while Studio doesn't
know it.

### Editing from a parameters component

A parameters component receives `NdvContext` through a `context` input declared
with `input.required()`. It reads the step, its scope, sample data and the
catalog, and changes the definition only through `edit(changes, label)`, which
`applyEdits()` in `studio/src/app/editor/ndv/edits.ts` carries out on a copy:

- A path is relative to the step. With `scope: "workflow"` it is relative to
  the workflow document and must name a field under `spec` (never `spec.steps`)
  or under `metadata`, for example `["spec", "llmProfiles", "support"]`;
  `["spec"]` or `["metadata"]` alone is refused.
- `value: undefined` deletes the key or list item. Its parent stays, so
  deleting the last field of `{"object": {...}}` leaves `{"object": {}}`.
- A list position is a number or `"-"`. Writing at the position just after the
  last item, or at `"-"`, appends; a write past that is refused. When the list
  isn't there yet, `"-"` or position 0 starts a one-item list and any other
  position is refused. A position under a field that holds something other than
  a list is refused.
- A step's `id` and `kind` can't be edited, including those of steps inside a
  case, a branch or a loop: rename steps with Rename, which rewrites
  references, and move steps on the canvas.
- A refused change throws `EditError`, and none of the call's changes apply.
- One call is one undo step, and edits within 600 ms merge into it.

`openAddStep({slot?, after?})` opens the Add a step panel, scoped to an AI agent
slot when `slot` is set. `openSubNode(slot, index?)` opens a slot, or one entry
of a many-valued slot such as Tools. `sample.context(at)` returns what an
expression at that path can read: `input`, the `output` of each step under
`steps`, and, inside a loop, the roots of the iteration picked in the Input
pane: `item`, `index` and `loops` (every enclosing loop by step ID).
`catalog.workflows({callableBy})` lists published workflows; those that don't
allow that caller stay listed and carry `disabled` with the reason. Studio reads
them from the project's catalog lock, `GET {PROJECT}/catalog`, with the
`catalog.read` capability: once per account and environment while the editor is
open, again on Refresh, and after a failed read only after a pause that starts
at 5 seconds and doubles up to 60. Without `catalog.read`, or not connected,
only the drafts on this computer are listed. Before Execute sends the workflows
a draft calls, it waits for that read at most 3 seconds, so a published version
wins over a draft on this computer with the same name and version.

### Running a step in an environment

A kind that can run in an environment sets `real` on its descriptor. Its
`request(ctx, kind)` returns `{body, confirm?}` or `{blocked}` with the reason
the step can't run, such as `Choose an action first.` `kind` is the
`KindContext`, which reads the contracts the editor already holds (an action's
`timeoutSeconds`, for example). The `body` is a `StepTestRequest` (snake_case,
see [In an environment](#in-an-environment)) without `draft_id` and
`draft_revision`: the kind sets `kind`, `step_id`, `input`, the connection and
release bindings, tool mocks, `real_tools` and `timeout_seconds`, and the
editor adds the saved draft's `draft_id` and `draft_revision`, and the side
effect the person acknowledged, when it sends `POST {ENV}/step-tests`.
`NdvContext` doesn't expose the draft, so a kind can't set those two fields.
`confirm` is a sentence of the kind's own that the confirmation shows after
"*step* changes data in *environment*. Repeating it repeats the change." (a
cost notice, say); when a kind sets it, Studio asks even for a step that
doesn't change data.

The built-in action kind sends the step's resolved input, `connections` with
the revision bound to the step's slot in this environment, `releases: {}` and
`timeout_seconds` from the action's `timeoutSeconds`, at most 300. It reports
`Choose the connection for slot crm first.` without a binding and
`send-invoice needs data from earlier steps. Execute previous steps first.`
while the input can't be computed. `NdvContext.connections.devBinding(slot)`
returns `{connectionId, revisionId, name, tested}` from the binding kept on
this computer (`tested` is `ok`, `failed` or null), and `bind(slot)` opens the
slot's Connection field.

### Coverage against the language manifest

`studio/tests/schema-coverage.test.ts` fails when a step kind the language
manifest marks `studio: "ready"` has no descriptor, or when a descriptor names a
kind the manifest doesn't list or a different language feature than the manifest
gives it. Kinds marked `pending` may lack one. A change that adds a step kind to
the language adds its descriptor or the `pending` mark.

## Test data

One test data document per workflow keeps the test input, pinned outputs, signal
and human task scripts, recorded AI turns, mocks for steps inside called
workflows and simulated step failures. Its JSON Schema,
`urn:firefly-weave:schema:studio-test-data:v1`, is
[studio-test-data-v1.json](../../src/firefly_weave/contracts/schemas/studio-test-data-v1.json);
the Python models are in `firefly_weave.contracts.studio_documents` and the
TypeScript types in `studio/src/app/editor/state/test-data.ts`.

| Field | Holds |
| --- | --- |
| `schemaVersion`, `kind`, `updatedAt` | `1`, `weave.studio/testData`, and an RFC 3339 time |
| `input` | The test input: `{value, source}` |
| `pins` | Pinned outputs, with the time they were pinned (`pinnedAt`) and the fingerprints that mark them out of date (`configHash`, `contractHash`, `upstreamHash`) |
| `signals`, `humans` | Scripts: a payload or a decision, or `{"outcome": "timeout"}` and `{"outcome": "expire"}`; an optional `frame` |
| `aiTurns` | Recorded AI turns: `{turns, memory?, autoApprove?}` with their source and fingerprints. Each entry follows `urn:firefly-weave:schema:studio-ai-turns:v1` ([studio-ai-turns-v1.json](../../src/firefly_weave/contracts/schemas/studio-ai-turns-v1.json)): a turn is `{toolCalls: [{name, arguments}], text?}` or `{final, text?}`, with at most 33 turns and 16 calls a turn. An AI task replays one `final` turn |
| `workflows` | Mocks for steps inside called workflows, by callee reference: `{"notify-customer@1.0.0": {"mocks": {...}}}` |
| `waits` | `autoAdvance`, true when absent |
| `errors` | Simulated failures, at most 1,000: `{code, outcome?, attempts?, source?}` with `runId` and `runEnvironmentId` for one copied from a run. Only Execute in Studio reads them; see [Simulated failures](#simulated-failures) |

- **Keys** of `pins`, `signals`, `humans`, `aiTurns`, `errors` and each called
  workflow's `mocks` are a step ID, which applies to every iteration, or an
  instance key
  (`send[1]`, `support#2.1`), which overrides it for one iteration or
  activation. Lookups try the instance key first. An instance key is the step
  ID, then `[i]` per enclosing loop, `#` and dot-separated segments for repeated
  activations, and `~n` for a loop's yields. The step ID part of a key is ASCII:
  an optional `@`, then a letter or digit, then letters, digits and `_`, `.`,
  `:`, `@` and `-`. Authored step IDs are narrower, with only letters, digits,
  `_`, `.` and `-`; the `@` and `:` forms are the platform's own IDs. A key that
  doesn't follow this grammar, such as `send mail` or `send[01]`, is refused;
  `firefly_weave.contracts.instance_keys` defines it, and `$defs.key` in the
  JSON Schema repeats it.
- **Frames.** A script with `frame` (`enrich`, `notify[2]/enrich`) applies only
  inside that sub-workflow.
- **Source.** `source` is `manual`, `schema`, `simulated`, `test-call` or `run`
  (an `aiTurns` entry is never `schema`; an `errors` entry is only `manual` or
  `run`, and may leave it out). Data from a run or a test call names
  `runId` and `runEnvironmentId`, and `runId` never appears without `source` and
  `runEnvironmentId`.
- **Size.** A document is at most 1 MiB.
- **Secrets.** Test data never holds a value a schema marks `x-secret` or
  `writeOnly`; such entries are refused and not kept anywhere.
- **Strictness.** The Python models accept exactly what the JSON Schema accepts.
  An optional field is left out, never `null`; JSON uses the camelCase names
  only; a UUID is written with hyphens, 8-4-4-4-12; and a timestamp is an
  RFC 3339 date-time such as `2026-10-07T10:00:00Z`.

### Simulated failures

An `errors` entry (`ErrorEntry` in Python and TypeScript) makes a step fail
when Studio executes the workflow, in place of its result. Real runs never read
it: Execute in an environment, for one step or the whole draft, sends no
`errors`.

| Field | Holds |
| --- | --- |
| `code` | Required. 1–128 characters matching `^[A-Z][A-Z0-9_.-]*$`, such as `REQUEST_REFUSED` |
| `outcome` | `not_started`, `failed` (when absent) or `unknown`: what the platform would report after the step's own retries |
| `attempts` | 0–1000, 1 when absent |
| `source` | `manual`, or `run` with `runId` and `runEnvironmentId` |

An entry never holds a message: the platform's copy for the code applies, and
a code it doesn't know reads "The step failed with code REQUEST_REFUSED." A failure
copied from a run keeps only its code, outcome, attempts and the run it came
from, and counts as run data like a pin from a run. Studio copies a step's
failures with the step only when their `source` is `manual` or absent. A
document without `errors` keeps its bytes, and `schemaVersion` stays 1.

Simulate a failure writes `outcome` only when it isn't `failed`. For a
`transform` or a `decisionTable` it asks no outcome and writes `not_started`:
such a step changes nothing outside the run, and Execute reports its failure
as `not_started` whatever the entry holds. Studio's banners read that outcome
for these kinds, also for an entry pasted with another one.

### Stored on the platform

When Studio is connected, the document is stored with its draft at
`{PROJECT}/drafts/{identifier}/studio/{kind}`, where `kind` is `canvas` or
`test-data`. The envelope is snake_case:

| Field | Meaning |
| --- | --- |
| `draft_id`, `kind`, `revision`, `draft_revision` | The draft, the document, and their revisions |
| `document` | The document, in its own camelCase |
| `contains_run_data`, `source_environment_ids` | Derived by the server and sticky: set when the document holds data from a run or a test call, cleared only by deleting the document |
| `updated_at`, `updated_by` | When and by whom |

A save sends only `{draft_revision, document}`; a body that sets
`contains_run_data` or `source_environment_ids` is refused. Reading or writing a
document with run data needs `run.read` in each source environment. A canvas
document is at most 256 KiB and a test data document at most 1 MiB
(`413 WV-STUDIO-DOCUMENT-SIZE`).

A platform that predates `errors` refuses a test data document that holds them
with `WV-STUDIO-DOCUMENT-SCHEMA` and no path. Studio then saves the same
document without `errors`, and only when that save succeeds keeps the failures
for this session alone: it never sends them again, never writes them to its copy
on this computer, and drops them when another account, environment or workflow
opens. Any other refusal is reported as before.

## Execute step

### Simulated, on the Studio host

These routes run in the Studio host, offline, through the simulator; their
bodies are camelCase. The Python models are in
`firefly_weave.studio.local_contracts` and the TypeScript types in
`studio/src/app/editor/state/execution.ts`.

| Route | Body | Result |
| --- | --- | --- |
| `POST /studio/local/debug/execute` | `ExecuteRequest` | `ExecuteResponse` |
| `POST /studio/local/debug/sessions` | `SimulationRequest` (the Execute body without `target`) | A debug session, in the platform's `debug.*` shape |
| `POST /studio/local/testdata/check` | `CheckTestDataRequest` | `{entries: [{path, ok, code?}]}` |
| `POST /studio/local/decision/evaluate` | `{table, input}` | `{output, matched_rule_ids, used_default}` |

- **Trying a table.** `decision/evaluate` compiles the table against an empty
  catalog and evaluates it with `evaluate_decision_table`, as the platform does,
  on the same two slots as validation. A table that gives no answer is refused
  with 422, its `WV-DECISION-*` or `WV-EXPR-*` code, a sentence and
  `result.path` (`result.diagnostics` as `{code, path}` when it doesn't compile);
  a refusal never repeats a value from the input.

- **Target.** `mode` is `workflow` (Execute workflow, which names no step),
  `step` (Execute step: until the step has output) or `before` (Execute
  previous steps: paused before the step). `step` and `before` name `stepId`;
  `instanceKey`, when set, is an instance of that step and picks one iteration
  (iteration 0 otherwise).
- **Mocks** are keyed `node:<instance key>`, `node:<step ID>`,
  `agent:<instance key>`, `tool:<instance key>/<tool>` or
  `action:<reference>`, where a step ID is itself an instance key; the most
  specific applies, in that order. Scripts come from test data: `signals`,
  `humans`, `aiTurns` and `autoAdvanceWaits`.
- **Failures.** `errors` holds simulated failures, keyed like mocks, at most
  1,000: `ScriptedFailure {code, outcome = "failed", attempts = 1}`, with the
  same limits as test data's `errors`. A key in both `mocks` and `errors` is
  refused (`422 WV-DEBUG-MOCK-CONFLICT`), and so is a failure for a step that
  can't fail in a simulation (`422 WV-DEBUG-MOCK-KEY`), such as a Transform
  without `onError` or a call that doesn't wait. An action, an AI task or a
  waiting call reports the failure's `outcome` and `attempts`; a Transform or
  a Decision table takes only its `code`, fails before it is evaluated and
  reports `not_started`. Failures apply
  only to the simulated workflow, never inside a called one.
  `POST /studio/local/debug/sessions` applies them as Execute does. Studio
  sends `errors` only when it holds some, as `node:<key>`, and leaves out a
  failure whose step can no longer take one. It sends no pin at a failure's
  key, and no last result or AI turns for an instance that fails, by its own
  key or its step's; an item's own pin still wins over its step's failure for
  every item, as the more specific key.
- **Called workflows.** `workflows` carries each callee's local source and
  format, or only its mocks when the callee is published.
- **Result.** `status` is `completed`, `blocked`, `failed` or `limit`. Every
  response carries `trace` and a `compile` summary, and the summary carries
  `diagnostics` and `stubs`; any of the three lists can be empty. `blocked`
  names its `reason` (`missing_mock`, `signal`, `human`, `path`, `callee`)
  exactly when the status is `blocked`, and `frame` when the step it stopped
  at is inside a called workflow. A definition that doesn't compile reports
  `failed` with its diagnostics in `compile.diagnostics`. The
  response's own `diagnostics` list holds debugger errors, such as a missing
  mock. Each `trace` entry names `nodeId` (the step ID) and `instanceKey` (`""`
  when it equals the step ID), and `frame` inside a called workflow; `loops`,
  `frames` and `selectedScope` describe loop progress, sub-workflow frames and
  the picked iteration's roots.
- **Failures in the trace.** A step whose `onError` handled its failure has
  `status: "failed"`, its `error` and `handled` (`continue` or
  `errorOutput`); with `errorOutput`, its `output` is the error path's output
  once the path ended. The failure that stopped the run has `status: "failed"`
  and `error` without `handled`. `error` is the platform's error object,
  `{code, message, outcome, attempts, instanceKey, childRunId?}`; `handled`
  without `error`, or on another status, is refused by the model. A step
  inside an error path its step didn't take blocks with `reason: "path"` and,
  for example, "With this test data, submit succeeds, so notify-team isn't
  reached."
- **Test data checks** report each entry by JSON pointer; refused entries name
  `WV-SCHEMA-SECRET_VALUE`, `WV-SCHEMA-CLASSIFICATION`,
  `WV-STUDIO-TESTDATA-SCHEMA` or `WV-STUDIO-TESTDATA-UNRESOLVED`.

| Status | Code | When |
| --- | --- | --- |
| 422 | `WV-STUDIO-REQUEST` | The body doesn't fit its model |
| 422 | `WV-DEBUG-*` | The simulator refuses a mock, a command or a limit |
| 422 | `WV-SCHEMA-SECRET_VALUE` | The input or a mock holds a secret |
| 409 | `WV-STUDIO-CATALOG-CONFLICT` | A local definition conflicts with the catalog |
| 409 | `WV-DEBUG-REVISION` | A session changed since it was read |
| 404, 410 | `WV-STUDIO-DEBUG-NOT-FOUND`, `WV-DEBUG-EXPIRED` | Unknown or expired session |
| 429 | `WV-STUDIO-SESSIONS-FULL` | Four sessions are open |
| 502 | `WV-STUDIO-CATALOG` | The platform catalog is unavailable |
| 503 | `WV-STUDIO-BUSY` | The simulation slot is busy, or the work took over 30 seconds |

### In an environment

Execute in *environment* runs a step, or a whole draft, through the normal
runtime. It needs the `step.test` capability on the environment, the
environment's test policy set to `enabled`, and `connection.bind` on every
connection revision the test uses. Bodies are snake_case; the Python models are
in `firefly_weave.contracts.step_tests` and the TypeScript types in
`studio/src/app/editor/state/execution.ts`.

| Operation | Body | Result |
| --- | --- | --- |
| `POST {ENV}/step-tests` | `StepTestRequest` | `{id, run_id, status: "running", side_effect, expires_at}` |
| `GET {ENV}/step-tests/{id}` | — | `StepTestView` |
| `POST {ENV}/step-tests/{id}/answers` | `{instance_key, confirm}` | — |
| `POST {ENV}/step-tests/{id}/cancel` | — | — |
| `POST {ENV}/test-activations` | `DraftTestActivationRequest` | `{id, activation_id, artifact_digest, side_effect, expires_at}` |
| `POST {ENV}/test-activations/{id}/runs` | `{input}` | The run, with `test: true` |
| `POST {ENV}/test-activations/{id}/listen` | `{secret_ref, max_body_bytes?, tolerance_seconds?}` | `{test_url, expires_at}` |
| `DELETE {ENV}/test-activations/{id}` | — | — |
| `GET` and `PUT {ENV}/test-policy` | `{test_calls}`, with `If-Match` on `PUT` | `{test_calls, revision}` |

- **Side effects.** A step that changes data (`idempotency_key` or
  `non_idempotent`) runs only when `acknowledged_side_effect` names that
  effect; otherwise the answer is `409 WV-STEPTEST-CONFIRM`. An AI agent's
  non-idempotent tools use their `tool_mocks` unless listed in `real_tools`,
  and each real call of such a tool waits for a `confirm` answer: `true` runs
  the call, `false` returns its mock.
- **Waiting.** While `status` is `waiting`, `pending` lists `confirm`, `review`
  and `person` items by `node_id` and `instance_key`. A `confirm` item names its
  `tool`, `side_effect` and `arguments`; reviews and person answers are human
  tasks, named by `task_id`, that also show in My tasks.
- **Status** is `running`, `waiting`, `succeeded`, `failed`, `timed_out` or
  `cancelled` (shown as "Canceled").
- **Limits.** A step test runs for at most 300 seconds, an AI agent's for 900.
  A draft test activation lasts 900 seconds by default and at most 3,600. Only
  the person who created a step test or a test activation can read, answer,
  cancel or delete it; anyone else gets `404 WV-NOT-FOUND`. The listen URL
  carries no secret: requests are signed with the secret handle.
- **Test policy.** An environment without a stored policy is `disabled` at
  revision 0. The demo environment `local` that `weave platform up` creates is
  `enabled`.
- **Omitted output.** A `StepTestView` whose output the platform left out has
  `output: null` and `omitted` set to `classified_secret`,
  `classification_unavailable`, `uncertain_derived`, `confidential_text` or
  `resource_limit`; `omitted` is absent otherwise.
- **Releases.** Studio sends `releases: {}`; the platform picks the only
  release that can run each dependency. When several can, it answers
  `422 WV-STEPTEST-RELEASE` with `result.choices`, a list of `{id, label}` per
  dependency, and Studio asks and sends the chosen IDs, remembered per
  environment for the session.
- **Answers.** `POST {ENV}/step-tests/{id}/answers` is published but not served
  yet (`501 WV-UNAVAILABLE`): action and AI task step tests never wait.
- **Refusal results.** `WV-STEPTEST-CONFIRM` carries
  `{side_effect, action, environment}`; `WV-STEPTEST-INPUT`
  `{diagnostics: [{path, code}]}`; `WV-SCHEMA-SECRET_VALUE` `{paths}`;
  `WV-STEPTEST-CONNECTION` `{slots: {slot: reason}}`; `WV-STEPTEST-DRAFT`
  `{diagnostics: [{path, code}]}`, with the message Studio shows.
  `WV-STEPTEST-BUSY` comes with `Retry-After`: a person runs at most 4 step
  tests at once, and starts at most 30 a minute, per environment.
- **Who the change is for.** Every change Studio sends to the platform carries
  `X-Weave-Principal` with the principal Studio shows as signed in. When the
  platform's sign-in now belongs to someone else, it refuses the change with
  `409 WV-PRINCIPAL-CHANGED` before anything runs, and Studio doesn't send it
  again.
- **Execute workflow in an environment.** Studio saves the draft, then keeps
  one test activation per environment, draft revision and bindings, until a
  minute before it expires. It binds the connections this computer bound to
  the workflow's slots, asks before steps that change data with the steps
  `WV-STEPTEST-CONFIRM` names, and opens the run in the Runs tab.
- **Listening for a test event.** Listening needs `step.test` and
  `trigger.manage`. Studio sends the webhook's test secret handle
  (`testSecretRef`), never its production one, then reads
  `GET {ENV}/run-summaries?activation_id=…&include_test=true&started_after=…`
  every 2 seconds, from two minutes before this computer's clock, until a run
  starts that Execute workflow didn't start and listening didn't take before,
  the URL expires, or three reads in a row fail. The run's input becomes the workflow's test input, with
  `source: "test-call"`, `runId` and `runEnvironmentId`. Stop listening
  deletes the test activation, which retires its test URL.
- **Retiring.** Studio deletes its test activation when the editor's draft is
  another one or another saved revision, when the editor closes, and when the
  account, environment or platform changes; listening on it ends.
- **Following.** Studio reads a step test every second for 30 seconds, then
  every 2 seconds, until it ends or its timeout plus a minute passes; a read
  that fails waits twice as long (at most 30 seconds), and three in a row stop
  following. A start whose answer was lost is sent again once with the same
  `Idempotency-Key`, so one click starts at most one test run.

| Status | Codes |
| --- | --- |
| 403 | `WV-FORBIDDEN`, `WV-STEPTEST-DISABLED` |
| 404 | `WV-NOT-FOUND` |
| 409 | `WV-STEPTEST-CONFIRM`, `WV-REVISION`, `WV-PRINCIPAL-CHANGED` |
| 410 | `WV-TESTACTIVATION-EXPIRED` |
| 422 | `WV-STEPTEST-INPUT`, `WV-SCHEMA-SECRET_VALUE`, `WV-STEPTEST-CONNECTION`, `WV-STEPTEST-RELEASE`, `WV-STEPTEST-KIND`, `WV-STEPTEST-DRAFT`, `WV-STEPTEST-TOOL-MOCK`, `WV-ACTIVATION-AI-PROVIDER`, `WV-ACTIVATION-AI-MODEL`, `WV-ACTIVATION-AI-RESERVED`, `WV-TESTACTIVATION-COMPILE`, `WV-TESTACTIVATION-LIFETIME`, `WV-ETAG` |
| 429 | `WV-STEPTEST-BUSY` |
| 503 | `WV-COMPATIBILITY`, `WV-AI-POLICY` |

## Trigger choices

A workflow's trigger choices live in its canvas sidecar (`triggers`), one per
trigger tile, each with an `id` and a `kind` and the settings its step details
hold. Studio creates or moves the platform resource behind each one when the
workflow is activated.

An email trigger starts runs with the received message, so the workflow's input
must take one. Studio reads that message's schema, with its definitions inlined,
from its host at `GET /studio/contracts/email-message`, a read-only route that
answers only a paired session, like the other contract routes.

A platform event trigger (`kind: event`, at most one) holds no settings of its
own: it mirrors the definition's `spec.eventTrigger`, and a definition that has
one gets the tile when it opens. Studio writes the events in the platform's
order, leaves `from` out for any workflow, and sets `spec.inputSchema` to
`{$ref: weave.platform-event-v1}`. It reads that schema from its host at
`GET /studio/contracts/platform-event`, once per session, and tries again 5
seconds after a failure, doubling to 60. The data picker, the sample events, and
the check of a pasted event use it.

### What Studio keeps about a trigger's resource

When an activation creates or moves a trigger choice's resource, Studio keeps
these fields in the choice, each keyed by environment ID:

| Field | Value |
| --- | --- |
| `resources` | The ID of the webhook, schedule, Kafka trigger, email source, app event source or platform event trigger in that environment. |
| `activated` | A key of the choice's settings when that activation took them: its fields other than `id`, `resources` and `activated`, as canonical JSON. |
| `pendingStops` | Platform event triggers only: the stops Studio still owes, at most 64, each with the `triggerId`, `revision` and `activationId` it was read with. A stop counts only while the trigger still has that revision and activation. |

The next activation in that environment moves the stored resource instead of
creating one, so a webhook keeps its ID and its URL. A tile reads "Set up at
activation" until `activated` holds the key of the settings the choice has now.
A webhook's production URL is `{platform URL}/webhooks/{resources[environment]}`.

### The activation dialog

The dialog reads, before Activate:

| Step | Read | Rows |
| --- | --- | --- |
| Checks | `GET {PROJECT}/language` and the version's export (`artifact.executable.features`) | The first feature the platform doesn't run stops the dialog. |
| Connections | `GET {ENV}/bindable-connections?connector=…` per slot connector | One row per slot. A required slot is chosen for the person from this computer's dev binding, else the connection named like the slot when exactly one candidate has that name, else the only candidate; an optional one only from a dev binding. The listing has no revision number, so several revisions with the slot's name are left for the person. |
| Called workflows | The version's export (`document`), `GET {PROJECT}/workflows` and `GET {ENV}/activations` | Each callee's current activation, sent as `workflow_activation_ids`; a callee without one blocks Activate. |
| The activation it replaces | `GET {ENV}/activations`, every page | No row: the revision of the workflow's newest activation, sent as `If-Match`, or none for a first one. When it was activated meanwhile, the platform answers `412 WV-ETAG`, and the dialog says so and turns **Activate version** off. |
| Triggers | `GET {ENV}/triggers`, `schedules`, `broker-triggers`, `email/sources`, `provider-sources` and `event-triggers` | One row per trigger choice: create, move, a schedule to save, a choice the CLI sets up, or a resource this workflow ran that is left behind, with **Stop it**. A platform event trigger has rows of its own, below. |

A new Kafka, email or app event trigger chooses its connection on its own row:
Studio lists every bindable connection (`GET {ENV}/bindable-connections`,
without `connector`) and keeps those of `weave-kafka` or `weave-email` at any
version, or the app's exact package. After the activation succeeds, Studio sets
the triggers up row by row: it creates what's missing, moves a resource with
the revision it read (`If-Match`), saves a moved schedule with its calendar and
input, and disables what the person ticked. A failure stays on its row with
**Retry**, which reads the resource again first, and never undoes the
activation. Every row is bound to the environment and the account it was read
with: once either changes, its requests aren't sent.

### A platform event trigger's rows

The rows compare what the published version declares with the environment's
triggers of this workflow: enabled, with its `workflow_name`, and on an
activation of it. The platform's stop checks neither, so this filter is the only
guard per workflow.

| Row | When | What Studio does |
| --- | --- | --- |
| Create | No trigger of this workflow to use | Creates one named after the workflow, or `NAME-2`, `NAME-3`… when taken, with `may_reconcile: false` |
| Move | The stored trigger, or one with a name Studio gives, reacts as declared | `retarget` with the revision read; its runs then start as the mover |
| Replace | The stored trigger reacts to other events or workflows | Creates the new one with the same `may_reconcile`, then stops the old one |
| Blocked | A trigger named after the workflow that Studio didn't make and reacts to something else, or one without `may_reconcile` in its view | Nothing; **Stop it** is off, and offered only for the first |
| Duplicate | Another trigger that reacts as declared, or whose stop is owed; **Stop it** is on | Stops it once the declared trigger is set up and still reacts |
| Left behind | Any other trigger of the workflow; **Stop it** is off | Stops it when the person asks |

- **A create whose answer is lost** is adopted: Studio lists first and takes a
  trigger equal in name, workflow, activation, events, workflows and authority.
  A trigger that shares an event and a workflow with the declaration, and wasn't
  in the list the dialog read, is someone else's: the row answers "changed" and
  nothing is created.
- **The activation and every row after it** go only to the environment, and as
  the person, the dialog opened with. When either changed meanwhile, nothing is
  sent, and the stop of a duplicate not stopped yet stays owed.
- **Every stop** first reads the trigger's starts, at most ten pages, and asks
  while any is `requested`, or when they can't be read; then reads the trigger
  again, stops nothing that changed, and sends the revision as `If-Match`.
- **Starts** lists `GET {ENV}/event-triggers/{id}/starts`, 20 at a time, for the
  stored trigger, when the target lists `flow.eventTrigger`. A failed read waits
  5 seconds, doubling to 60. An answer for an environment, account, workflow or
  trigger the person left is dropped.

## Storage on this computer

Every key the new editor keeps on this computer goes through
`studio/src/app/editor/state/browser-store.ts`. These rules apply once the new
editor ships; until then Studio keeps its drafts and preferences under the keys
it already uses.

| Prefix | Holds |
| --- | --- |
| `<profileId>:<subjectHash>:<tenantId>:<projectId>:` | Connected work for one account in one project |
| `local:` | Local drafts and their test data |
| `ui:` | Preferences that hold no workflow data, such as pane widths and the new editor preference |

- `profileId` is `p` and the first 16 hex digits of the SHA-256 of the saved
  platform's name; `subjectHash` is the first 16 hex digits of the SHA-256 of
  the signed-in subject. Without a subject, connected work is kept in memory
  only.
- Keys match `^[A-Za-z0-9:._-]{1,256}$`.
- Signing out of a platform, or removing it, deletes every key with that
  platform's `profileId`, in browser storage and in the desktop store. `local:`
  and `ui:` keys stay.
- Keys from earlier releases move once: local drafts under `local:`, pane
  preferences under `ui:`. Remembered run inputs (`weave-studio-run-input:`)
  can't be attributed to an account, so they are deleted.
- The desktop app keeps the same keys in the Studio host store under its
  configuration directory instead of browser storage.
- **Connections a slot uses.** `<account prefix>weave.devBindings.v1` holds,
  per environment ID, workflow name and slot, the chosen connection's ID, its
  revision ID, its name and, once tested, whether the test passed and when:
  `{"v": 1, "bindings": {environmentId: {workflow: {slot: [connectionId,
  revisionId, name, ok?, at?]}}}}`. Nothing else, and never a secret. At most
  500 bindings and 64 KiB; a write past either is refused and the document
  stays as it was. A binding follows its connection: the newest revision in the
  environment's list replaces the kept one and clears the test result. Without
  an account prefix, bindings last for the session only.
- **Real results stay in memory.** A step's last result from Execute in an
  environment is never written to `weave.lastExecution.*`; Pin output keeps it
  as test data with `source: "test-call"`, `runId` and `runEnvironmentId`.

## Keyboard map

`studio/src/app/editor/state/keymap.ts` holds the editor's one shortcut table;
the key handler and the shortcuts sheet (`?`) both read it. Each key combination
appears once per platform, and a row says what it does on each surface: the
canvas, the outline, step details, a Fixed field, the formula editor, the Add a
step panel, the decision table grid and the run log. `Mod` is Ctrl on Windows
and Linux and Cmd on macOS. Shortcuts never act while you type in a field,
except Ctrl/Cmd+S, Ctrl/Cmd+Enter, undo and redo outside the formula editor, and
`=` in a Fixed field, which switches an empty one to Mapped. `D` is not
assigned, because Weave has no deactivated steps, and Ctrl/Cmd+K is reserved.
The last section of the sheet, Differences from n8n, lists each row that differs
as two sentences: what the keys do in Studio, then what they do in n8n.

The `runs` surface is the Runs tab's list: Up and Down choose the previous and
next run, Home and End the first and last, and Enter shows the chosen run on
the canvas. Choosing moves the selection and never moves focus out of the list.
Escape on the Runs canvas returns focus to the chosen row.

## The Runs tab's address

`/workflows/<draft>/designer?tab=runs` opens a workflow on its Runs tab, and
`&run=<run ID>` (a UUID) chooses that run. Choosing another run replaces the
address without adding a history entry; leaving the tab drops the query. The
address never carries filters or an environment: a link opens where the session
is, and it never writes test data.

## The new editor

The new editor is on by default. It is a per-viewer preference: the key
`ui:weave.editorNext` holds `"false"` when the person turned it off (and `"true"`
when they turned it on again); any other value, no value, or browser storage that
refuses reads means the new editor. `?editor=classic` and `?editor=next` in the URL
choose for that page without changing the stored choice, which is how tests open
either editor. Settings › Preferences offers the same choice with the **Use the
new editor** switch; the classic designer stays one switch away for now.
