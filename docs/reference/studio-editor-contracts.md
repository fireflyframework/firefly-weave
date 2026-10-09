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
| `llm` (AI task) | `ai-task` | `ai` | Yes | — |
| `decisionTable` | `rules` | `data` | No | — |
| `transform` | `transform` | `data` | No | — |
| `switch` (Decision) | `decision` | `flow` | No | — |
| `parallel` | `parallel` | `flow` | No | — |
| `fail` | `fail` | `flow` | No | — |
| `wait` | `wait` | `wait` | No | — |
| `signal` | `signal` | `wait` | No | `signal` |
| `humanTask` | `human` | `human` | No | `human` |

A nested step list lives at the path of a branch plus `steps`:
`["cases", 0, "steps"]` and `["default", "steps"]` for a decision,
`["branches", "first", "steps"]` for a parallel step. The language manifest lists
the fixed branches of a kind, such as `default` for a decision and `body` for a
loop (`["body", "steps"]`); decision cases and parallel branches are found by
position and by name. Output handles are `case:<index>` and `default` for a
decision and `branch:<name>` for a parallel step; `fail` has none.

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
allow that caller stay listed and carry `disabled` with the reason.

### Running a step in an environment

A kind that can run in an environment sets `real` on its descriptor. Its
`request(ctx)` returns `{body}` or `{blocked}` with the reason the step can't
run, such as `Choose an action first.` The `body` is a `StepTestRequest`
(snake_case, see [In an environment](#in-an-environment)) without `draft_id` and
`draft_revision`: the kind sets `kind`, `step_id`, `input`, the connection and
release bindings, the side effect it acknowledges, tool mocks, `real_tools` and
`timeout_seconds`, and the editor adds the saved draft's `draft_id` and
`draft_revision` when it sends `POST {ENV}/step-tests`. `NdvContext` doesn't
expose the draft, so a kind can't set those two fields.

### Coverage against the language manifest

`studio/tests/schema-coverage.test.ts` fails when a step kind the language
manifest marks `studio: "ready"` has no descriptor, or when a descriptor names a
kind the manifest doesn't list or a different language feature than the manifest
gives it. Kinds marked `pending` may lack one. A change that adds a step kind to
the language adds its descriptor or the `pending` mark.

## Test data

One test data document per workflow keeps the test input, pinned outputs, signal
and human task scripts, recorded AI turns and mocks for steps inside called
workflows. Its JSON Schema, `urn:firefly-weave:schema:studio-test-data:v1`, is
[studio-test-data-v1.json](../../src/firefly_weave/contracts/schemas/studio-test-data-v1.json);
the Python models are in `firefly_weave.contracts.studio_documents` and the
TypeScript types in `studio/src/app/editor/state/test-data.ts`.

| Field | Holds |
| --- | --- |
| `schemaVersion`, `kind`, `updatedAt` | `1`, `weave.studio/testData`, and an RFC 3339 time |
| `input` | The test input: `{value, source}` |
| `pins` | Pinned outputs, with the time they were pinned (`pinnedAt`) and the fingerprints that mark them out of date (`configHash`, `contractHash`, `upstreamHash`) |
| `signals`, `humans` | Scripts: a payload or a decision, or `{"outcome": "timeout"}` and `{"outcome": "expire"}`; an optional `frame` |
| `aiTurns` | Recorded agent turns: `{turns, memory?, autoApprove?}` with their source and fingerprints; each item of `turns` is an object whose shape `urn:firefly-weave:schema:studio-ai-turns:v1` defines |
| `workflows` | Mocks for steps inside called workflows, by callee reference: `{"notify-customer@1.0.0": {"mocks": {...}}}` |
| `waits` | `autoAdvance`, true when absent |

- **Keys** of `pins`, `signals`, `humans`, `aiTurns` and each called workflow's
  `mocks` are a step ID, which applies to every iteration, or an instance key
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
  (an `aiTurns` entry is never `schema`). Data from a run or a test call names
  `runId` and `runEnvironmentId`, and `runId` never appears without `source` and
  `runEnvironmentId`.
- **Size.** A document is at most 1 MiB.
- **Secrets.** Test data never holds a value a schema marks `x-secret` or
  `writeOnly`; such entries are refused and not kept anywhere.
- **Strictness.** The Python models accept exactly what the JSON Schema accepts.
  An optional field is left out, never `null`; JSON uses the camelCase names
  only; a UUID is written with hyphens, 8-4-4-4-12; and a timestamp is an
  RFC 3339 date-time such as `2026-10-07T10:00:00Z`.

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
- **Called workflows.** `workflows` carries each callee's local source and
  format, or only its mocks when the callee is published.
- **Result.** `status` is `completed`, `blocked`, `failed` or `limit`. Every
  response carries `trace` and a `compile` summary, and the summary carries
  `diagnostics` and `stubs`; any of the three lists can be empty. `blocked`
  names its `reason` (`missing_mock`, `signal`, `human`, `path`, `callee`)
  exactly when the status is `blocked`, and a definition that doesn't compile
  reports `failed` with its diagnostics in `compile.diagnostics`. The
  response's own `diagnostics` list holds debugger errors, such as a missing
  mock. Each `trace` entry names `nodeId` (the step ID) and `instanceKey` (`""`
  when it equals the step ID), and `frame` inside a called workflow; `loops`,
  `frames` and `selectedScope` describe loop progress, sub-workflow frames and
  the picked iteration's roots.
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

| Status | Codes |
| --- | --- |
| 403 | `WV-FORBIDDEN`, `WV-STEPTEST-DISABLED` |
| 404 | `WV-NOT-FOUND` |
| 409 | `WV-STEPTEST-CONFIRM`, `WV-REVISION` |
| 410 | `WV-TESTACTIVATION-EXPIRED` |
| 422 | `WV-STEPTEST-INPUT`, `WV-SCHEMA-SECRET_VALUE`, `WV-STEPTEST-CONNECTION`, `WV-STEPTEST-RELEASE`, `WV-STEPTEST-KIND`, `WV-STEPTEST-TOOL-MOCK`, `WV-TESTACTIVATION-COMPILE`, `WV-TESTACTIVATION-LIFETIME`, `WV-ETAG` |
| 429 | `WV-STEPTEST-BUSY` |
| 503 | `WV-COMPATIBILITY` |

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

## Try the new editor

The new editor is a per-viewer preference, off by default. The key
`ui:weave.editorNext` holds `true` or `false`, and `?editor=next` in the URL
turns the new editor on for that page, which is how tests open it. Settings ›
Preferences offers the same choice with the **Try the new editor** toggle.
