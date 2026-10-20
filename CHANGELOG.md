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

# Changelog

## Unreleased

- Studio keeps test data for each workflow: the trigger's test input and
  pinned output on steps. Step details offer **Pin output**, **Edit output**
  (a JSON editor), **Unpin** with Undo and **Generate sample**; the Input pane
  shows values beside each field; formula previews read pinned output; tiles
  and the Output pane mark pinned output that may be out of date, with **Keep
  pinned**. **P** pins or unpins in step details and on the canvas. Pinned
  output follows copy and paste, renames and deletions, and the simulation
  starts from it. **Save to file** adds `<name>.testdata.json`, asking first
  about data recorded from runs.
- Platform drafts save themselves and keep their canvas and test data on the
  platform (`studio_documents.read`, `.save` and `.delete`). Test data recorded
  from runs needs `run.read` in each environment it came from, and secret
  values are refused with document pointers only. Migration
  `0034_editor_studio_documents`.
- The desktop app keeps local drafts and their test data in a private store,
  so they survive quitting.
- Studio keeps everything it stores on this computer under account, local or
  preference prefixes, and deletes a platform's keys when you sign out of it
  or remove it. Remembered run-form values from earlier versions are removed.

- Workflows can call other workflows. A **Call a workflow** step starts the
  exact version it names as a separate child run of the pinned activation, under
  the calling run's principal, which must still hold `run.start`. A waiting call
  returns the child's output, or with `onFailure: continue` a
  `{status, runId, output, error}` envelope; with `stop`, a failed child fails the
  caller with the child's code, and a cancelled or timed-out child opens
  `WV-CALL-INTERRUPTED`. A call that cannot start opens `WV-CALL-START` (or
  `WV-CALL-DEPTH`); `retry_safe` starts a new child, and only the newest child's
  result is used. Cancelling a caller cancels its waiting children and theirs;
  detached children keep running, also after their caller finished. The platform
  now lists `flow.callWorkflow` and `callWorkflow`, publishes, imports and
  activates workflows that call others, and records `call_completed` events that
  replay from the caller's own history. New: `RunView.caller`,
  `GET …/runs/{id}/calls` (`weave runs calls`, `run_calls()` in the SDK),
  `weave definitions activations create --pin-current-calls`, the `weave.calls`
  counter with `call.dispatch` and `call.delivery` queue ages, and the "Call
  another workflow" guide. Migration `0033_language_workflow_calls` adds two empty
  tables and changes no existing row; it adds the call rules to run purging and
  charges call rows to each run's reserved control capacity, so cancelling or
  timing out a run that takes part in a call still works at a full byte quota,
  as for any other run. A run cannot be purged while its calls run or before its result
  reached its caller (`WV-RUN-PURGE-BLOCKED`). Upgrade CLIs and SDKs together
  with the server: older clients reject the new `call_completed` event type and
  `caller` field when they validate history and runs.
- With the new editor, Mapped values in step details are **formulas** such as
  `input.amount > 1000 and exists(input.email)`: suggestions from the data the
  field can read, references as pills, plain-word messages that never block
  typing (text that doesn't parse shows **Not applied** and keeps the last
  formula), a standard form on blur, **Edit formula** beside the input's
  fields, and drops at the pointer. Templates take any formula between
  `{{ }}`. Under a Mapped field, `= value` and the **Result** panel show what it
  gives with the trigger's test input, computed in the browser with the
  platform's rules (held to them by `scripts/studio_expression_corpus.py`);
  formulas reading secret values are never previewed, and previews send
  nothing. **Call an API** shows the full address of its request. Studio now
  includes CodeMirror (`@codemirror/state`, `@codemirror/view`, MIT), loaded
  with the first formula field. See `docs/reference/formulas.md`.
- Publish… and Simulate show why they stopped (for example, an account that
  can't publish actions) as soon as Studio has loaded the code that publishes
  the workflow's own actions, instead of waiting for another change on the
  page.
- Simulations and debug sessions address loop iterations. A mock under
  `node:send[2]` answers iteration 2 of `send` only and wins over `node:send`,
  which wins over `action:` mocks; a breakpoint on `send[2]` stops before that
  iteration only, while `send` still stops every iteration. Keys must give one
  position per enclosing loop, below its `maxItems` (`WV-DEBUG-MOCK-KEY`,
  `WV-DEBUG-BREAKPOINT`). The debugger view adds `loops` (each loop instance's
  `count`, `completed`, `running`, `next_index`, and `status`) and
  `selected_scope` (the `item`, `index`, and `loops` values the selected step
  reads); both are omitted for workflows without loops. `WV-DEBUG-MISSING_MOCK`
  now hints at the keys that would answer the step, and `weave workflow simulate`
  prints one progress line per loop. A long simulation checks its session size
  every 64 boundaries or 1 MiB of new output instead of after every boundary, so
  a 2,000-element loop simulates in seconds instead of a minute. A debug session
  or `weave workflow simulate` for a workflow whose language features this
  version does not run now answers `WV-IR-UNSUPPORTED` naming the missing
  features, as publishing does, instead of a generic validation error: a server
  lists them in `result.missing_features` when a session is created, read or
  sent a command, and the CLI lists them in its message. Upgrade the SDK
  together with the server to read debug views of workflows with loops.
- Serve bounded run summaries and classified step timelines with fresh scoped
  authorization. Timeline output remains optional; legacy timings stay unknown.
  Logs remain unavailable with HTTP 501.
- Change `runs.list` to newest-started-first order. Existing v1 cursors remain
  supported with explicit `order=id`; chronological orders use v2 cursors.
  Update-time pagination is a live view and may require a refresh after changes.
  Restored executions reappear in every order.
- `runs.list` accepts the run summary filters and leaves out test runs unless
  `include_test=true`. It rejects unknown query parameters, and an out-of-range
  `limit` answers `WV-VALIDATION` instead of `WV-PAGE`. Pages stay limited to
  8 MiB of retained run data.
- Step timeline outputs are limited to 8 MiB per page (`429 WV-PAGE-LIMIT`).
  Outputs of agent, tool or yield instance keys stay withheld until they have
  their own schemas. A run waiting for a platform that runs it answers
  `422 WV-IR-UNSUPPORTED` as `runs.read` does.
- The scheduler re-checks stored run listing decisions in the background: runs
  recorded while this platform could not run them, which it now runs, and
  available or unsupported decisions recorded under an earlier classification
  policy. Unavailable decisions stay unavailable. Each minute it visits up to 16
  tenants and gives one environment of each up to one second of database work
  and at most 100 runs. Until a run is re-checked, run summaries, step timelines
  and incident lists keep withholding it, even when `runs.read` serves it again:
  its timeline answers `422 WV-IR-UNSUPPORTED` when this platform runs it again,
  and `409 WV-LEGACY-UNAVAILABLE` when it was recorded under an earlier policy.
  A retained run larger than 128 MiB is recorded as unavailable without being
  read.
- The Python SDK's `list_runs` rejects a `query` combined with keyword filters.
- Request telemetry records authorized `501 WV-UNAVAILABLE` answers with the
  status `unavailable` instead of `failed`.
- A new run's `started` history event records how the run started as `origin`,
  the value run summaries report. History pages and exports include it, and
  replay accepts it.
- Serve incident lists from incident facts without loading runs, newest opened
  first. They filter by status, code, workflow, step, opening time, and test
  runs (left out unless `include_test=true`). Explicit `order=id` continues
  earlier cursors. Unknown query parameters are rejected, and an out-of-range
  `limit` answers `WV-VALIDATION` instead of `WV-PAGE`.
- New operation `incidents.read` (`weave runs incidents read`, SDK
  `read_incident`) serves direct incident links and sends the revision as `ETag`.
- Incident views add optional `opened_at`, `opened_at_estimated`, `updated_at`,
  `workflow`, `run_status`, `test`, and `instance_key`; for a repeated step
  instance, `node_id` is the static step ID. Upgrade CLIs and SDKs with the
  server: earlier releases reject the new fields and fail with `502 WV-WIRE` on
  incident lists. Resolution receipts, `incidents.resolve` answers, and the
  earlier plain array of a run's incidents keep their bytes. A resolve answer is
  the incident as stored, so read it again with `incidents.read`.
- An incident on a switch or parallel branch output or a join keeps its
  control-step `node_id` in incident lists and reads. An incident whose step
  does not match its incident facts is withheld as unavailable.
- The Python SDK's `list_incidents` and `run_incidents` accept a typed `query`
  and reject one combined with keyword arguments.
- Workers report where they run when they register: an optional, bounded
  `identity` with host, runtime, versions, platform, Kubernetes namespace and
  node, and up to 8 labels from `WEAVE_WORKER_LABELS`
  (`WEAVE_WORKER_REPORT_HOST=false` leaves out the host). The Agentic and Files
  workers and the SDK's `register_worker` send it and retry once without it when
  an older server answers `422 WV-VALIDATION`; native executors send it inside
  the API process. With native executors configured, an invalid
  `WEAVE_WORKER_LABELS` stops the API from starting. New SDK helpers:
  `worker_identity` and `register_worker_http`.
- Worker reads, lists, and drain or resume answers add optional `identity` and
  `registered_at`. Upgrade CLIs and SDKs with the server: earlier releases fail
  with `502 WV-WIRE` on workers registered after the upgrade. The registration
  answer and stored worker records keep their bytes, and a worker cannot report
  a placement.
- Deployment observations list up to 100 live `members` of each worker
  component: running or starting Compose containers and Kubernetes pods, with
  host, ordinal, state, readiness, start time, restarts and node. Stopped,
  terminating, finished, failed and evicted ones are left out. With more than 100
  live members, or when the runner cannot list them all, `members` is left out
  (placement unknown) and the observation still succeeds. Kubernetes runners
  read members through the deployment's selector, including set-based
  `matchExpressions`, and need `list` on the namespace's pods. Observations
  without members keep their bytes and digests.
- Upgrade the server before its deployment runners: an older server refuses the
  `members` an upgraded runner reports, so observe jobs fail and applies end
  `reconciliation_required`. Earlier CLIs and SDKs fail with `502 WV-WIRE` on
  observation reads and lists that carry members.
- `workers.list` reads each page with one query and lists current workers by
  default: not revoked, and seen (or, never seen, registered) in the last hour;
  `state=all` lists every worker. It filters by `presence`, `draining`,
  `release_id`, `task_type`, `target_id` and `host_prefix` (a literal,
  case-sensitive prefix) and orders by `last_seen_desc` (default) or
  `registered_desc`, unknown times last. Cursors issued before the upgrade are
  refused, unknown query parameters are rejected, and an out-of-range `limit`
  answers `WV-VALIDATION` instead of `WV-PAGE`. Pages stay limited to 8 MiB.
- Studio's Workers page asks the platform for current workers, last contact
  first, and for every worker with **Show retired workers**. Earlier Studio
  hosts and desktop apps get the
  reduced default list from an upgraded server, because the Studio bridge
  forwards answers unchanged: their Workers page lists only current workers, and
  **Show revoked workers** shows none. Upgrade Studio and the desktop app with
  the server.
- Worker reads and lists add optional `placement` (target, deployment and
  component), matched from the latest complete, unexpired observation of the
  targets the reader may read, and `drained_at`, when the worker started
  draining; drain and resume answers add `drained_at`. Upgrade CLIs and SDKs
  with the server: earlier releases fail with `502 WV-WIRE` on placed workers
  and on workers drained since the upgrade.
- New operation `workers.retire_stale` (`weave workers retire-stale`, SDK
  `retire_stale_workers`) revokes up to 100 workers per command that hold no
  active lease and were last seen, or registered if never seen, longer ago than
  `older_than_seconds` (one hour to 30 days), oldest first. `dry_run` previews
  the same batch, and `matched` counts every eligible worker.
- New operation `releases.usage` (`weave workers releases usage`, SDK
  `release_usage`) counts a release's online, offline, draining and revoked
  workers and the activations that pin it, and pages the deployment components
  that run it on targets the reader may read; `deployments_hidden: true` says the
  reader may read no target.
- The Python SDK's `list_workers` accepts a typed `query` and rejects one
  combined with keyword arguments.
- Kubernetes runners no longer read a pod's free-text status reason, and a pod
  whose readiness is unknown is listed as a member that is not ready.
- New operation `operations_summary.read` (`weave operations summary`) answers
  an environment's health, load, workers, targets, incidents and up to 20 items
  that need attention, in one read that reloads the caller's grants. Each
  section and item needs its own capability; one the caller may not read is
  `{"state": "forbidden"}` without running its queries or recording a denial,
  and one whose queries use up its two seconds of database time between them
  is `{"state": "unavailable"}` while the rest are still served. Run figures
  leave out test runs, and opening times the upgrade estimated are not counted.
  Telemetry export health is degraded while exports are being dropped.
- New operation `operations_telemetry.read` (`weave operations telemetry`)
  answers whether telemetry export is on, the collector host names, the
  metrics interval and how many items were dropped, never endpoint paths,
  credentials or headers. It needs `status.read` for the project.
  `WEAVE_TELEMETRY` now refuses a collector host name longer than 253
  characters.
- Background loop health, which the summary reports, ages successes with a
  monotonic clock, counts a cycle still within its loop's budget as on time,
  and reads degraded after 3 failed cycles in a row or among the last 20. An
  outbox cycle that cannot start a delivery, because the platform is not ready
  or no delivery slot is free, is neither a success nor a failure.
- Studio's host bridge forwards the environment summary, the telemetry
  settings and `releases.usage`.
- Studio's Operate area opens on a new Overview page. It shows the health of
  the API, the database, the scheduler, the outbox, compatibility and
  telemetry export, each with its details; the current load; workers,
  clusters and incidents; and up to 20 items that need attention, most of
  them linking to their record. It reads the summary every 15 seconds while
  visible. A section you may not read names the permission it needs instead
  of showing zeros.
- Operate › Runs reads run summaries, newest first, with time range, workflow,
  version, origin, status, test, handled-error, incident, top-level and
  archive filters. The address keeps every filter except the business and
  correlation keys. Load more continues the same list, Auto-refresh reads the
  first page every 10 seconds, and up to 50 selected runs can be canceled or
  retried together. A run's detail draws its steps with queue wait and run
  time, groups loop iterations, links to called runs, shows a step's output
  on request and follows a running run every 2 seconds. Starting, canceling,
  retrying, archiving or restoring a run, or deleting its data, from the page
  reads the list again at once, and a run started there shows first while
  the list puts new runs first (with Oldest first it waits for its place).
  The line under the title names the order in use.
- Operate › Incidents filters on the platform by status, code, workflow,
  step, opening range and test runs, with a choice of order, so a match
  beyond the loaded pages shows. The address keeps the filters and the open
  incident (`?incident=ID`), which opens from `incidents.read` even when no
  loaded page lists it. The drawer shows the workflow, the full step instance,
  the run's status and when the incident opened, marked estimated when the
  upgrade inferred it and Unknown when it isn't known; Refresh incident reads
  the incident itself.
- Operate › Workers has Fleet and Releases tabs. Fleet filters on the platform
  by status, draining, release, task type, target and host prefix (kept on the
  page, never in the address) with a choice of order, shows each worker's
  host, placement, versions and registration time, drains, resumes or revokes
  up to 50 selected workers one by one, and **Retire offline workers…**
  previews and then revokes offline workers that hold no work, 100 at a time.
  Releases lists the admitted releases and, for one, its workers by state, the
  activations that pin it and the deployments on targets you may read; it shows
  the CLI command that admits a release and never admits one itself. On a narrow
  screen both pages fold their filters behind **Filters (n)**, and every filter
  checkbox is a 36 px target with a mouse and 44 px on touch.
  A retirement whose answer arrives after its dialog closed, or after the
  workspace or person changed, says what happened in a notice; a bulk action
  stops sending when the person changes, and the host prefix is cleared for the
  next person.

- The compiler checks and compiles **Call a workflow** (`callWorkflow`) against
  the exact published version it calls: that version must opt in with
  `spec.callable` (any workflow, or only `allowedCallers`), `with` is checked
  against its input schema, a `businessKey` must be text of 1 to 200 characters,
  and the step returns the called workflow's output, a `{status, runId, output,
  error}` envelope with `onFailure: continue`, or `null` with `mode: detach`. New
  diagnostics `WV-COMP-UNKNOWN_WORKFLOW`, `CALL_NOT_CALLABLE`, `CALL_RECURSION`,
  `CALL_DEPTH` (more than 8 calls deep, the new compiler limit
  `max_call_depth`), and `CALL_MODE` point at the field to fix. The called
  version is locked as an interface dependency: the caller's artifact carries
  that version's definition, so its digest matches the published one, but
  compiling, importing, and activating the caller read only its input and output
  schemas. Activation takes `workflow_activation_ids`, one live
  activation of each called version in the same environment, and refuses wrong
  or missing pins with `422 WV-READINESS-CALL`. This platform does not start child
  runs yet, so it refuses to publish, import, or activate a workflow that calls
  another with `WV-IR-UNSUPPORTED`; workflows without calls compile to the same
  bytes as before. `weave workflow explain` and `weave workflow graph` show calls,
  and the SDK's `WorkflowBuilder.with_callable()` writes `spec.callable`.
- Loops run. A **Loop over items** (`forEach`) step runs its body once per
  element, `concurrency` at a time, in one run, and its output lists one result
  per element in element order, whatever order the iterations finish in. Each
  iteration's steps, tasks, human tasks, waits, and incidents use their own
  instance key (`send[2]`); a task incident in one iteration suspends the run, a
  `fail` step in one fails it, and canceling ends every iteration. Finished
  iterations are removed from the run state. An `items` value that is not a list,
  more elements than `maxItems`, or results over the 1 MiB payload limit open
  `WV-RUNTIME-LOOP-ITEMS`, `WV-RUNTIME-LOOP-LIMIT`, or `WV-RUNTIME-LOOP-OUTPUT`.
  A long loop yields after 4,000 units of work in one event and continues about
  one scanner poll later; the capabilities response reports the budget as
  `operations.runtime.loop_units_per_turn`. The platform now lists
  `flow.forEach` and `forEach` in its language manifest and capabilities, so it
  publishes, imports, and activates workflows with loops. Debug sessions run
  loops with the same reducer. New counters `weave.loop_iterations` (`status:
  ok` for each collected result, `cancelled` for each iteration still running
  when its run ends) and `weave.loop_yields`. Upgrade the CLI and SDK together with the server: older
  clients do not know the human task `instance_key` and `iteration` fields or
  the new capability fields.
- Evaluating an expression inside a loop body and admitting a long event no
  longer walk data that has not changed since the event started, so a loop's
  cost grows linearly with its number of elements.
- The compiler checks and compiles **Loop over items** (`forEach`): `/item`,
  `/index`, and `/loops/<id>/…` inside a loop body, a typed list of results after
  it, `WV-COMP-LOOP_*` diagnostics for wrong items, scope, nesting, limits, and
  signals in a body (with a suggested `maxItems` for a loop budget that is too
  high), and an IR `forEach` node with an `each` edge and an `each` join that
  requires the `flow.forEach` feature. A platform that does not list the
  feature refuses to publish, import, or activate such a workflow with
  `WV-IR-UNSUPPORTED`; workflows without loops compile to the same bytes as
  before. The compiler limits add `max_loop_items`, `max_loop_depth`, and
  `max_run_iterations`. Studio's local host answers `POST /studio/local/scope`
  with the data a field may use, including the current loop item and position.
- Starting a run no longer slows down as executions accumulate. The usage accounting that runs on every run start
  and every change to quota-counted records read every reservation in the database, one per run ever started, and
  now finds its reservation and allocation entries by primary key. Usage figures and limits are unchanged. New
  migration `0031_operations_key_collation` replaces the two internal accounting functions in place and changes no
  table, data or role; migrate the server explicitly before restarting it.
- Studio has one place to set up AI: **Settings › AI models**. It shows provider cards, your AI connections, a
  readiness checklist with **Publish**, **Authorize** and **Test connection**, and the Weave AI card. The AI
  connection side sheet is the only AI connection form; it also opens from Build › Connections, an AI task and
  Home's **Connect a model**. It offers plain HTTP only for endpoints a platform operator approved, asks for a secret
  handle (never a key), lists the approved models the provider serves, and tests a model through the AI gateway with
  tool support. The old AI setup wizard and the Weave AI settings in the Ask Weave AI panel are gone.
- Studio waits up to 140 seconds for an AI connection test, so a local model loading for the first time no longer
  fails the test early; canceling the test in Studio stops it on the platform.
- `weave platform up --username` grants the new account the local owner roles, and
  `weave platform user grant --username NAME --role ROLE` adds roles to an account the local platform created.
- Run state, history, tasks, deadlines, and incidents name each run of a step by
  its instance key, which equals the step ID outside loops, so existing workflows
  keep byte-identical transitions and history. Deadline scans, incident
  resolution, replay, exported history, and debug sessions find the step behind
  any instance key.
- Human task views add `instance_key` and `iteration` for tasks created inside a
  loop iteration; both are omitted for every other task.
- The capacity check of one event's work runs at its end and after every 64
  steps or 1 MiB of new step output, instead of after every step. Work over
  capacity is still refused, but the refusal can happen at a later step of the
  same event, and when a later step of that event fails, the run opens that
  step's incident instead.
- Keep typing in the selected form field when deferred row autofocus completes.
  Rapid row additions focus the newest item without overriding a later choice.
- Update Studio's YAML parser to 2.8.3 so deeply nested documents produce a
  parser diagnostic instead of an unexpected stack-overflow exception. Update
  Vitest to 4.1.11 to include fixes for its development-server security advisories.
- Add step details to Studio's new editor (**Settings → Preferences → Try the
  new editor**, off by default). Double-click a step, or focus it and press
  Enter, to configure it over the canvas with **Input**, **Parameters** and
  **Output** panes, **Fixed | Mapped** fields, drag to map, **Settings** with
  retries, timeouts and notes, and issues under the field they concern. The
  trigger and End open their own details, and the gear opens **Workflow
  settings**. **Add a step** lists **Call an API**, email and file steps under
  **Action in an app**; each gets an action of its own, which **Publish…** and
  **Simulate** publish first, after you confirm. A draft saved on a platform
  keeps those actions' settings and step notes in this browser for your
  account, and **Save draft** says so. With the new editor off, Studio keeps
  the inspector.
- Add **Add a step** to Studio's new editor: a panel on the right (the whole
  window below 768 pixels) that every **+**, a dragged handle, N, / and the
  toolbar open at that place. It starts with **What triggers this workflow?**
  on an empty workflow and **What happens next?** afterwards, searches by
  everyday words, opens apps on their actions and triggers, and keeps entries
  the platform can't run visible with the reason. The new canvas also gets
  trigger choices that are set up when you activate the workflow, copy and
  paste of steps as YAML between workflows, sticky notes with Markdown, **Tidy
  layout**, a step toolbar above the step, and a `.canvas.json` file that
  **Save to file** writes and **Import workflow** reads. A draft on a platform
  keeps its notes and trigger choices in this browser for your account.
- With the new editor on or off, **Import workflow** starts a fresh Undo history
  (opening a file is no longer a step you can undo, which emptied the canvas),
  and a `.canvas.json` file chosen on its own is refused with a sentence that
  asks for the workflow file too.
- Studio's API builder measures pasted samples in bytes, so a large sample in a
  script that takes several bytes per character gets the size message instead
  of a request error.
- Going Back to a workflow that was opened from a file and left unchanged now
  says that Studio didn't keep a copy and how to open the file again, instead
  of saying that the workflow may have been deleted.
- Leaving **Source** with text you typed but haven't applied now asks **Apply
  changes**, **Discard changes** or **Keep editing**, with the new editor on or
  off: switching tabs, opening a step, running a command, going to another page,
  browser Back, Undo and Redo, and a toast's **Undo** all ask first. Closing the
  window uses the browser's own prompt.
- Studio dialogs and toasts fit touch screens better: close and toast buttons
  are at least 44 pixels on touch, dialog footers stack on narrow screens and
  stay clear of the focused field, dialog headings line up with their close
  button, and toasts no longer cover the page header or step details' top bars.
- Studio loads the Clusters page, and the code behind **Publish…** and
  **Simulate**, only when it is first used, so its first download is smaller.
- The Studio host adds `GET /studio/contracts/connector-actions`, which returns
  the built-in email and file connector actions, as Action documents, that
  Studio's email and file steps copy into actions of their own. Its API action
  builder (`POST /studio/local/http-action`) refuses a request larger than
  1 MiB with 413 `WV-STUDIO-REQUEST`.
- Run AI tasks on a local Ollama model: `weave platform ai enable --ollama auto|host|container`
  (or `--ollama-url URL`) sets up the AI gateway, the Agentic worker, a version 2 AI
  policy, development-only private-origin entries and a keyless `ollama-local`
  connection, and tests a real model call; `ai status`, `ai disable` and `ai models`
  complete it.
- Model endpoints accept plain HTTP only through private-origin entries with
  `purpose: model` that send no credential. The reserved secret handle
  `no-credential` marks such connections and is never resolved; operator grants
  under that name are ignored with a warning, and `weave platform secret set`
  refuses it.
- New operation `ai_connections.test`; `weave connections test` on an AI connection
  now calls the model through the AI gateway instead of always reporting failure.
  AI connection tests, dedicated or generic, share a limit of six per minute per
  person and answer 429 `WV-AI-RATE-LIMITED` beyond it.
- The AI gateway gains `POST /v1/test` and `POST /v1/models`, and the API accepts a
  plain HTTP gateway endpoint on a loopback address that a private-origin `model`
  entry with loopback credentials lists. An endpoint with a trailing slash is refused.
- The Agentic worker reports `LLM_MODEL_NOT_FOUND`, `LLM_UNREACHABLE`, `LLM_AUTH`,
  `LLM_RATE_LIMITED`, `LLM_CONTEXT_LIMIT` and `LLM_NO_TOOL_SUPPORT` instead of
  `LLM_PROVIDER`, accepts `WEAVE_AGENTIC_CAPACITY`, and signs in and calls the API
  over plain HTTP only through `worker-auth` and `platform-api` private-origin entries.
- The worker and the AI gateway read version 2 AI policies; version 1 files keep
  working with a warning to migrate.
- New AI operations, each in one environment and rechecking the caller's current
  grants: `ai_endpoints.list` and `ai_models.list` (`catalog.read`) return the
  approved endpoints and cached model metadata, with tool support as Ollama
  reports it; `refresh=true` asks the AI gateway again and needs
  `connection.manage`. `ai_readiness.read` returns eight ordered setup checks from
  stored records, without calling a provider. `ai_setup.publish` publishes the
  built-in AI Connector and Action (`definition.publish`), and `ai_setup.grant`
  authorizes admitted Agentic releases for a connection (`connection.manage`);
  repeating either changes nothing. `bindable_connections.list`
  (`connection.bind`) lists only the connection revisions the caller may bind,
  without configuration or secret handles. Discovery never pulls a model.
- With an AI policy mounted on the API (`WEAVE_AI_POLICY_FILE`), new activations
  of workflows with AI task (`llm`) steps answer 422 `WV-ACTIVATION-AI-PROVIDER`
  when a step's provider differs from its connection's, and 422
  `WV-ACTIVATION-AI-MODEL` when the policy does not approve the model for the
  connection's endpoint. An unreadable policy answers 503 `WV-AI-POLICY` for
  activations with AI task steps only. Without a policy, activation is unchanged.
- Saving an AI connection now refreshes its model list before `connections.create`
  answers, which can take up to about 20 seconds. When the policy approves the
  connection's endpoint and the AI gateway is configured, the refresh contacts the
  provider through the gateway and spends one of the six AI test admissions per
  person per minute; a refresh that cannot call the gateway spends none.
- `ai_connections.test` and `connections.test` of an AI connection refresh the
  model list after the test, within the test's admission, so they answer later.
- `connections.test` is cancelled when its client disconnects, for every connector
  type: the result and the `connection.test.completed` audit are not written.
- A new activation checks `connection.bind` for each bound connection revision on
  both the request's token and the caller's stored grants. A token whose stored
  grant was removed gets 403, and grants limited to specific revisions now bind
  exactly those revisions.
- Replaying an activation `Idempotency-Key` now requires the caller's current
  `release.activate` grant: a caller whose grant was revoked gets 403 (or 401)
  instead of the stored 201 answer.
- AI connection tests are dated with the database clock, which readiness also
  uses, so a test that just passed reads as healthy on every API process.
- Event delivery keeps working after the platform becomes ready or withdraws
  readiness. Before, each delivery turn that the outbox started while the
  platform was not ready kept one of the process's four delivery slots. That
  happened while the platform opened its background work, at startup and when a
  compatibility rescan restored readiness, and while it closed that work after
  readiness was withdrawn, which can last up to the shutdown timeout (10 seconds
  by default) while native connector calls finish. Once all four slots were
  lost, scheduled deliveries and manual retries delivered nothing until the
  process restarted, and nothing reported it. A turn cancelled before it
  starts, as when the event loop shuts down, also gives its slot back now.
- While the platform is not ready, the outbox waits for readiness instead of
  starting a delivery turn every second that is refused and logged as
  `Outbox turn failed` at ERROR.

## 0.1.0a15

### Workflow language

- Build text in expressions with `concat`, which joins one or more strings,
  numbers, or Booleans, and `join`, which joins a list of them with a separator.
  Numbers are written as JavaScript writes them (`2.0` becomes `2`). The compiler
  reports an operand that can never be text as `WV-COMP-TYPE_MISMATCH` and guards
  one that might not be at run time. Decision table rules cannot use either
  operator yet (`WV-DECISION-OPERATOR`), and Studio does not edit them yet.
- Compile a workflow that uses `concat` or `join` to `weave/ir-v1alpha4`, with
  `text.concat`, `text.join`, or both in the executable's new `features` list.
  Every other workflow keeps its IR version and digest.
- List the language features a platform runs in `language_features` in the
  capabilities response, and add `weave/ir-v1alpha4` to `ir_versions`. An older
  platform omits `language_features`, which means none. Activating a workflow
  that needs a feature the platform does not list answers HTTP 422
  `WV-IR-UNSUPPORTED` with `result.missing_features`; artifact import and the
  compatibility report call it `ir_unsupported`.
- Add the language manifest, which lists the step kinds, operators, workflow
  fields, language features, and limits a platform supports:
  `GET /api/v1/tenants/{tenant}/projects/{project}/language` (`catalog.read`),
  `weave remote language`, `WeaveClient.language()`, the Studio host, and
  `language-manifest.schema.json` from `weave schema export`. The capabilities
  response now lists all ten runnable step kinds in `step_kinds`.
- Accept `forEach`, `callWorkflow`, and `spec.callable` in workflow definitions
  and the workflow schema, so documents can be prepared for loops and workflow
  calls. The compiler refuses `forEach` and `callWorkflow` with
  `WV-COMP-UNSUPPORTED_FEATURE` until a release runs them; `callable` only
  declares an interface and compiles. Existing documents and artifacts keep
  their bytes and digests.
- Runs in progress whose workflow uses a language feature the server does not
  run, for example after rolling back to an earlier release that lists fewer
  language features, now wait for an upgrade instead of being blocked for good.
  The server offers none of their tasks to workers, leaves their deadlines and
  expired attempts pending, records nothing about them, and does not let them
  hold back other runs. After the upgrade they continue, and deadlines that
  passed in the meantime apply then. A server that predates language features,
  such as 0.1.0a14, still blocks such runs permanently, so finish or cancel them
  before rolling back that far.
- Reading such a run, sending it a signal, or reporting a task result for it
  answers HTTP 422 `WV-IR-UNSUPPORTED` with `result.missing_features` instead of
  HTTP 409 `WV-LEGACY-UNAVAILABLE`. Reading a definition version the server does
  not run, or repeating the publish request that created it, answers the same
  way. Run and incident lists still show such runs as unavailable, and runs with
  unavailable legacy evidence still answer `WV-LEGACY-UNAVAILABLE`.
- Catalog list pages can contain a new kind of item for a version the server
  does not run: `unavailable: true`, `reason: ir_unsupported`, and
  `missing_features`. SDKs and CLIs older than this release reject such a page;
  upgrade them together with the server.

### Connections and private networks

- Accept `http://` base URLs in no-code HTTP connections, as HTTP connector
  actions already do, and never send plain text silently: `weave connections
  create`, `read`, and `test` and `weave connector import-openapi --target
  builtin` print a "Not encrypted" warning on standard error, and Studio's
  connection form shows a **Not encrypted** notice. Private, loopback, and CGNAT
  addresses still need an approved entry, and machine-token endpoints stay
  HTTPS-only.
- Connection test answers carry a new `encrypted` field. CLIs and SDKs older
  than this release cannot read `weave connections test` answers for HTTP
  connections from an upgraded server; upgrade the CLI and SDK together with the
  server.
- Add development-only private origins to the Docker development platform:
  `weave platform up --allow-private-origin ORIGIN` (repeatable) lets HTTP
  connector actions and signed webhooks reach a local `http://` test service at
  that exact origin on the installation's own egress network. The API reads the
  approved entries from the read-only file named by `WEAVE_PRIVATE_ORIGINS_FILE`
  and refuses to start when that file is malformed, a symbolic link, or writable
  by other users. Requests to an approved origin never follow redirects, and
  Weave refuses the connection when it cannot tell whether the address belongs
  to the platform itself.
- Map `WEAVE_HTTP_PRIVATE_NETWORKS`, `WEAVE_MAIL_PRIVATE_NETWORKS`, and
  `WEAVE_POSTGRES_PRIVATE_NETWORKS` at startup to "Legacy setting" entries of
  the same private-origin policy, with a `private_origins.legacy` warning and
  unchanged reach.
- Parse `WEAVE_HTTP_PRIVATE_NETWORKS`, `WEAVE_MAIL_PRIVATE_NETWORKS`, and the
  PostgreSQL private and plaintext network settings strictly at startup: a CIDR
  with host bits set, or more than 128 networks, in any of them stops the API.
  Before, the HTTP and mail settings accepted a CIDR with host bits set and
  failed the requests that used it, and the HTTP and PostgreSQL settings had no
  limit. Reach is otherwise unchanged.

### Reliability under load

- Keep a platform ready when a compatibility rescan confirms it. A burst of API
  requests no longer makes the periodic rescan restrict the platform until the
  next one, and changes are no longer refused for a moment while a confirming
  rescan cleans up. `503 WV-COMPATIBILITY` answers carry `Retry-After`, the
  seconds until the next automatic rescan, and a rescan that withdraws readiness
  logs `Compatibility rescan withdrew readiness` with the finding kinds and codes.
- Retry a Studio read, or a change that carries an idempotency key, after
  `503 WV-COMPATIBILITY` when `Retry-After` is 5 seconds or less, within the
  existing limit of four attempts.
- Background work that starts when a compatibility check makes the platform
  ready, such as event delivery, native connectors, broker and provider inboxes,
  and recovery, no longer keeps failing with `WV-OPERATION-CAPACITY` after the
  check's response ends, and no longer carries that request's identity.
- Give recovery, the provider and email inboxes, and Kafka consumers their own
  execution capacity, so a burst of API requests no longer refuses their work.
  Recovery and schedule scan failures now log the error class and code, for
  example `Tenant recovery scan failed (CatalogError WV-OPERATION-CAPACITY)`.
- Native connector calls no longer share the two execution work slots of API
  requests, so a burst of requests no longer refuses them. They run up to the
  `capacity` configured for each `WEAVE_NATIVE_EXECUTORS` entry.
- Native connector tasks no longer fail with `HANDLER_FAILED` when Weave refuses
  one of their platform calls for capacity (`WV-OPERATION-CAPACITY` or
  `WV-REQUEST-CAPACITY`). The invocation check before the connector starts is
  sent again while the task's lease is valid; authority and credential checks
  made while the connector runs get up to three attempts within one second.
- A schedule whose run start is refused for capacity stays enabled and fires the
  same occurrence, with the same key, on the next scan, instead of being blocked
  with `WV-SCHEDULE-READINESS`. An occurrence still refused when its minute ends
  is skipped like any other missed minute.
- Provider receipts refused for capacity stay `pending` with the reason
  `transient_failure` and the usual cooldown instead of being blocked.
- `email_receipts.dispatch` answers HTTP 429 (`WV-OPERATION-CAPACITY` or
  `WV-REQUEST-CAPACITY`) when Weave refuses the dispatch for capacity, where it
  used to answer HTTP 200 with state `blocked`. The receipt keeps its state, and
  the next pending scan dispatches it.
- Keep an integration event delivery recoverable when Weave refuses one of its
  database transactions for capacity (`WV-OPERATION-CAPACITY`). The delivery
  stays `leased` instead of moving to `retry` with `DELIVERY_FAILED` or to an
  `AUTHORITY_REVOKED` incident. When the lease expires, the attempt is recorded
  as `ACK_UNKNOWN` and the same event ID is delivered again. The attempt still
  counts, so a delivery refused on every attempt ends in a `DELIVERY_EXHAUSTED`
  incident.

### API

- Publish the run summary, step, and log operations (`run_summaries.list`,
  `runs.steps`, and `runs.logs`) and their schemas so clients can build against
  them. They require `run.read` and validate their queries and cursors; this
  release answers an authorized, valid request with HTTP 501 `WV-UNAVAILABLE`.

### Studio, desktop app, and CLI

- Give Studio one dark theme built from design tokens, the self-hosted Manrope
  typeface, and Lucide icons. The sidebar and pairing pages show the Firefly
  Weave logo; the collapsed sidebar and narrow windows show the Firefly mark,
  and the browser tab the Firefly favicon.
- Remove the Lumi mascot from Studio, the API explorer, exported workflow
  graphs, the README, and the documentation diagrams.
- Say Weave AI throughout Studio: **Ask Weave AI**, **Weave AI settings**,
  **Explain with Weave AI**, the **AI models** settings card, and the role labels
  **Weave AI user** and **Weave AI manager**. Show the product name, Firefly
  Weave, at the top of CLI help instead of ASCII logo art, and say Weave AI in
  CLI prompts and in server and worker messages. API paths, permissions, roles,
  storage names, and error codes keep `lumi`.
- Draw "weave" in the Firefly Weave logo as a wordmark whose w is woven from two
  strands, one amber, instead of typed text, in Studio, on the desktop launch
  page and installer, and in the README banner and social preview.
- Give the desktop app the Firefly icon, a charcoal DMG background as tall as
  the installer window, and a charcoal launch page in a charcoal window that
  opens without a white flash, and draw exported workflow graphs, the API
  explorer, the README banner, shields, and badges with the Firefly identity.
- Keep the values typed into a **Call an action** step's input fields when you
  switch to **Write one expression for the whole input**. Before, the expression
  opened on the input as it was when the step was selected, and the first edit
  replaced the typed values. Switching back to field-by-field input while the
  expression is invalid keeps it in view with the usual message.
- Studio no longer marks a workflow it just opened or created as Unsaved before
  any edit when it is not connected to a platform.
- NOTICE states that the Firefly name, logo, and icon are trademarks of Firefly
  Software Solutions Inc., used with permission and not licensed under the
  Apache License 2.0, and that the Weave name and logo are trademarks of the
  Firefly Software Foundation. It adds the Manrope and Lucide notices.

### Documentation

- Restyle the documentation site in one dark scheme with the Firefly Weave logo,
  the Firefly favicon, and self-hosted Manrope, and recolor every diagram to the
  Firefly light palette on paper panels.
- Call the Studio assistant Weave AI throughout the documentation. Its guide is
  now [Use Weave AI](docs/guides/weave-ai.md); [the previous guide](docs/guides/lumi.md)
  points to it. API paths, permissions, and role names keep `lumi`.
- Add the [Studio editor contracts](docs/reference/studio-editor-contracts.md)
  reference for the step details registry, the test data schema, and the step
  execution bodies that the next Studio editor builds on. Studio does not use
  them yet.

### Development and testing

- Run Studio's unit tests, a cross-platform browser subset, and a portable
  Python subset on macOS and Windows in CI, next to the Linux checks.
- Add acceptance journeys against a real Docker platform with its PostgreSQL and
  Keycloak, the Acme API fixture, and the Studio host (`scripts/acceptance.py`).
  The `Acceptance` workflow runs the `pr` profile on Linux with container egress
  blocked.
- Fail the checks when the Python package, the worker packages, the desktop app,
  the documented install pins, and the upgrade table do not name one release.
- Replay a recorded corpus of compiled artifacts and kernel transitions for
  every example workflow, so a change that alters an existing digest fails the
  checks.

### Versions and schema

- Bump the Python package and browser Studio bundle to `0.1.0a15` and the
  desktop product to `0.1.0-alpha.15`; the native internal installer counter is
  `0.1.15`.
- Pin the Agentic and Files 0.1.7 packages to core 0.1.0a15. The Agentic Weave
  AI gateway now introduces the assistant as Weave AI; Files has a dependency
  update only.
- Schema revision remains `0030_worker_presence`; no new database migration is
  added, and no role is added or changed. See [upgrade a platform safely](docs/operations/upgrades.md#what-changes-in-alpha15)
  for the behavior changes to check.

Verified on macOS (Apple silicon) before tagging: the full local check gate (17
stages, including 5,070 unit and contract tests, the Agentic and Files worker
suites, and building, installing, and checking the 0.1.0a15 core and 0.1.7
worker packages); Studio's type check, 793 unit tests, production build, and
browser suite (934 passed and 6 environment-gated tests skipped; one test that
timed out under full parallel load passed ten times on rerun); 612 integration
tests against a real PostgreSQL 17; and the release acceptance profile against
a real Docker platform with PostgreSQL 17 and Keycloak 26.7.4, where every
enabled bring-up step passed, the secret scan found nothing, and no container,
network, or volume was left behind. The release gate (22 stages) also passed on
the release commit with real Kafka and Keycloak: 646 integration tests, built
release images, 69 process tests including recovery and a restore from a
0.1.0a14 installation, and the queue-load measurements. The acceptance journeys
beyond bring-up, the desktop installers on Windows and Linux, Weave AI and AI
workflows, Developer ID signing, and Apple notarization are **not verified** for
this release. macOS installers remain ad-hoc signed and not notarized, and
Windows installers remain unsigned.

## 0.1.0a14

- Add a detached Docker development platform through `weave platform up`,
  reusing owned databases, identity setup, workspace creation, and user roles.
  Keep the foreground API mode available for existing installations.
- Prepare an explicit Developer ID signing and notarization path for macOS
  releases. Apple credentials are required; ordinary builds remain ad-hoc signed.
- Pin the Agentic and Files 0.1.6 packages to core 0.1.0a14 without changing their
  execution behavior or the database schema.

## 0.1.0a13

- Retry explicit platform capacity rejections while a worker reads its task
  context or credentials, using the current validated lease. Reject late
  responses after cancellation, renewal failure, or expiry; unrelated HTTP
  errors and ambiguous network failures are not replayed.
- Classify an Agentic preparation timeout as not started when its own time
  budget expires before provider execution. Failures after provider execution
  begins retain their ambiguous outcome classification.
- Publish Agentic and Files worker packages 0.1.5 pinned to core 0.1.0a13.
  Files has a dependency update only. The worker protocol and database schema
  remain unchanged from alpha12 (`0030_worker_presence`).

## 0.1.0a12

- Add environment-scoped deployment targets, desired state, observations,
  immutable approval plans, fenced runner jobs, and explicit reconciliation
  for uncertain external effects. Provider credentials remain on an outbound
  runner with local destination and image allowlists.
- Add Operations in Studio and the CLI for reviewed container deployment
  changes. Support trusted Compose deployment and bounded existing Kubernetes
  and Azure Container Apps changes; provisioning cloud clusters is separate.
- Report dated worker contact and capacity, with revision-checked drain and
  resume controls that preserve active leases.
- Let Lumi explain explicitly selected Operations records using bounded,
  permission-checked context. Deployment changes retain their separate plan,
  approval, and application process.
- Reject unsupported component and destination changes before plan approval.
  Mark stopped or stalled compatibility monitors unhealthy so container
  supervisors can replace the affected API process.
- Guide target registration with named application accounts, downloadable
  nonsecret runner setup, observed-resource import, explicit resource capacity,
  and admitted worker release selection.
- Allow reviewed Compose plans to restart stopped workers, checking readiness
  after the restart instead of during the dry run.
- Keep worker lease renewal retrying explicit capacity rejections while the
  current lease remains valid, including while a completed task awaits its
  acknowledgment. Ambiguous transport failures never replay the task handler.
- Keep invalid inspector edits and unapplied AI profile changes visible before
  saving, publishing, or switching views. Edit workflow AI profiles in a dialog,
  with multiline prompts and direct connection-slot selection.
- Organize editor commands into aligned rows and step settings into purposeful
  sections. Keep palette search visible and explain data sources through
  keyboard-accessible information controls. Filter source modes and formulas
  by the receiving field's type while preserving imported expressions for repair.
- Separate reusable project actions from creating a new API action, with a
  direct connection entry point and contextual guidance for local authoring.
- Keep Studio pages and editor assets loading while platform requests are busy,
  and retry temporary session-read capacity rejections within a bounded budget.
- Use standard OpenAI and Anthropic endpoints automatically, retain explicit
  Azure configuration, and group advanced model settings separately. Check
  model-setting combinations with the canonical local validator before applying
  them; configuration validation does not claim live provider readiness.
- Add migrations `0029_deployments` and `0030_worker_presence`, with explicit
  scoped Operations roles. Existing workflow roles gain no deployment authority.
- Publish worker packages 0.1.4 pinned to core 0.1.0a12 and desktop alpha12.
  Keep the verified alpha10 Azure acceptance history separate from this release.

## 0.1.0a11

- Guide AI provider connections, Lumi settings, and workflow AI profiles through
  focused steps with review before creation or application. Keep unfinished
  profile changes local and apply the profile and connection slot atomically.
- Let authors explicitly pass available earlier AI results as structured context
  within one workflow execution, using existing compiler-checked expressions.
- Document the verified alpha10 Azure application-token, AI workflow, Lumi, and
  HTTP acceptance separately from unverified interactive Entra sign-in.
- Correct browser selection tests to wait for the insertion reveal before
  checking pointer reachability.
- Publish worker packages 0.1.3 pinned to core 0.1.0a11. No server behavior or
  database migration changes; the schema remains `0028_files`.

## 0.1.0a10

- Expose Lumi configuration through the paired Studio host and clarify the
  separate administrator, worker-admission, and deployment responsibilities.
- Let explicit canvas selection and structural edits supersede pending initial
  fitting. Keep automatically added empty fields from stealing keyboard focus.
- Use the Lumi mascot in Studio and the illustrated documentation.
- Make compiler scope-parity tests reject inconclusive resource-limit results
  and isolate their timing allowance from production validation budgets.
- Publish worker packages 0.1.2 pinned to core 0.1.0a10. No database migration is
  added; the server still requires schema `0028_files`.

## 0.1.0a9

- Add guided Studio setup for AI provider connections, named connection selection
  for Lumi, and independent workflow model profiles. Keep model credentials in
  operator-managed secret storage and show scoped setup permissions explicitly.
- Keep incomplete LLM profile edits open until required properties are present.
- Add renewable OAuth2 client credentials for independently deployed Agentic
  workers, with bounded token acquisition, expiration-aware caching, scoped API
  origins, cancellation cleanup, and mounted-secret rotation.
- Explain AI workflow execution, Lumi proposal review, and configuration roles
  with step-by-step guides and accessible SVG diagrams.
- Publish worker packages 0.1.1 pinned to core 0.1.0a9. No database migration is
  added; the server still requires schema `0028_files`.

## 0.1.0a8

- Add verified file references, resumable bounded transfers, scoped file roles,
  CLI upload/download commands, Python SDK streaming helpers, worker lease
  checks, and human-task attachments with current claimant/revision checks.
  PostgreSQL stores file chunks separately from run history; retained runs pin
  their files until the run is purged.
- Add independently deployed FTP, FTPS, SFTP, SharePoint/OneDrive and Google
  Drive workers for list, metadata, download, write, move and delete operations.
  Cloud account acceptance remains environment-specific; change-feed triggers
  are not included.
  FTP/FTPS/SFTP require a server-isolated account root. FTP/FTPS write and move
  are disabled by default because the protocol cannot guarantee atomic refusal
  to replace a concurrently created destination; enabling them requires an
  explicit connection policy and a separate worker policy.
- Add contains/not-contains and membership comparisons, versioned decision
  tables, and schema-guided Studio editors for rules and AI steps.
- Add workflow `llmProfiles` and Agentic workers with explicit provider, model,
  reasoning pattern, endpoint policy and budgets. Pin the profile into compiled
  execution, enforce output guards, and reject classified results before values
  or their hashes enter durable state, simulation or reconciliation.
- Add Lumi as a separate environment-configured assistant, using an authenticated
  private gateway. Studio shares only selected context, validates proposed
  definitions, and requires explicit Apply with stale-draft protection and Undo.
- Improve Studio's step configuration, input mapping and human-task authoring;
  render decisions and parallel branches as separate lanes; apply valid field
  edits immediately; keep invalid drafts local; add semantic drag/drop and
  reachable node actions.
- Add schema revisions `0026_decision_tables`, `0027_lumi_configuration`, and
  `0028_files`. Upgrade through the documented maintenance procedure before
  starting the new server. Older grants do not automatically grant file or Lumi
  access.
- Package independent workers separately so Agentic's Python 3.13 requirement
  does not change the server's Python 3.12 support.
- Keep worker completion and failure retries alive after explicit capacity
  rejections while the lease and task deadline remain valid. Direct transport
  calls retain bounded retries; ambiguous outcomes are never retried automatically.

## 0.1.0a7

- Add saved platforms shared by the CLI, Studio, and desktop app: a non-secret
  `profiles.json` in the system configuration folder (`WEAVE_CONFIG_HOME`
  overrides it), written atomically with private permissions. Tokens stay in the
  system credential store, or in an explicit private file.
- Add `weave auth setup SERVER`. It checks the server, shows its sign-in settings
  for review, signs you in through the system browser (PKCE with a loopback
  callback) or a device code, lets you choose a workspace, and says what was
  saved and which command to run next. Explicit flags, `--yes`, and
  `--output json` keep it usable in scripts without prompts.
- Add `weave auth profiles`, `use`, `remove`, `workspace`, and
  `login --switch-account`. `weave auth status` exits 1 when you are not signed in.
- Add the public `GET /api/v1/client-configuration` endpoint, configured with
  `WEAVE_CLIENT_SIGN_IN` and `WEAVE_DISPLAY_NAME`. Clients treat it as a
  proposal: you review it once, the issuer, client, and endpoint origins are
  pinned, and a later change is refused with `WV-PROFILE-CHANGED`.
- Check server addresses before connecting: HTTPS except on loopback, no
  redirects, bounded responses, and no link-local, metadata, NAT64, or 6to4
  destinations.
- Rebuild the Studio connection wizard: work locally or connect to a platform,
  pick a saved platform, check the connection, review the sign-in settings, sign
  in through the system browser or a device code, and choose a workspace. Studio
  reconnects on reopen and never asks for your identity-provider password.
  Local authoring still needs no sign-in.
- Keep desktop pairing across restarts and save Studio exports through the
  system download handler.
- Add no-code REST integrations on the built-in `weave-http@2.0.0` connector:
  `weave connector http-action`, `weave connector import-openapi --target builtin`,
  guided `weave connections create` flags, connector descriptors, and the
  Studio **New API action** builder and OpenAPI import. Publishing now rejects an
  HTTP action whose configuration contradicts its method, with a pointer to
  `/spec/implementation/config`.
- Add `weave platform integrations` and `weave platform secret` so a local
  platform can run built-in HTTP actions with secrets stored by handle.
- Make the **Call an action** step usable end to end in Studio: choose an
  action and a connection, fill inputs through generated forms, map data,
  validate without a platform, save, reopen, and import or export YAML and JSON.
- Redesign the Studio editor: one primary command that follows the lifecycle,
  readable zoom with **Fit all** and **Tidy layout**, Delete with Undo, Decision
  conditions as rule rows with **Otherwise**, problems shown on the steps, an
  inspector with **Apply changes** and **Discard**, and a docked simulation that
  highlights the live path.
- Rework the Studio shell: **Build** and **Operate** navigation, a Home page
  that leads with what needs you, toasts with Undo, drafts kept on this computer
  with autosave, a run detail page for operators, consistent status names, and
  accessibility fixes for contrast, focus, and side panels at narrow widths.
- Register port-free loopback callbacks for the local Keycloak `weave-cli`
  client, and repair existing local platforms on `weave platform start`, so
  browser sign-in works on any loopback port.
- Keep in-process native workers running when they claim work while the
  platform is still opening effects; the dispatcher restarts a failed worker with
  backoff and reports not ready after three consecutive failures.
- Replay in-process native worker claims, renewals, and settlements that the
  platform rejects for capacity, with the same policy as remote workers, so a
  busy database no longer exhausts a task's retries.
- Accept JWKS signing keys that omit `alg`, as Microsoft Entra ID publishes them,
  for the allowed algorithm of their key type only (RSA for `RS256`, P-256 for
  `ES256`).
- Retry Studio reads, and changes that carry an idempotency key, when the
  platform turns them away for capacity (`WV-OPERATION-CAPACITY`,
  `WV-REQUEST-CAPACITY`), honoring `Retry-After` for up to four attempts.
- Show only the logo in the CLI help banner.
- Bump the Python package and browser Studio bundle to `0.1.0a7` and the desktop
  product to `0.1.0-alpha.7`; the native internal installer counter is `0.1.7`.
- Rewrite the documentation around four learning paths, with new guides for
  signing in, no-code REST integrations, and readers coming from BPM tools.

Verified on macOS against a real local Keycloak 26.7.4: CLI sign-in and
workspace selection with a private credential file, a no-code HTTP action
running on a local platform, the Studio sign-in journeys with the macOS
Keychain, and sign-in with a code, reconnection after a restart, and export in
a locally built macOS desktop app. In the desktop app, drafts kept on this
computer last only until the app quits; use **Save to file** to keep a copy.
Microsoft Entra ID and other OpenID
Connect providers are supported through configuration but **not verified**;
Entra-shaped keys are covered by unit tests only.
Credential storage on Windows and Linux is **not verified**. Existing
`--auth-config` connection files keep working. Schema revision remains
`0025_run_lifecycle`; no new database migration is added.

## 0.1.0a6

- Fix the macOS desktop outer application resource seal with explicit ad-hoc
  signing, replacing the damaged alpha5 macOS installers.
- Add strict `codesign` verification of the outer bundle, native executable, and
  frozen host, then repeat it against the application inside the read-only DMG
  before collecting release assets.
- Keep hardened runtime with the PyInstaller library-validation entitlement and
  smoke-test the host after signing, before collecting installers.
- Bump the matching Python host/browser bundle to `0.1.0a6` and desktop product
  to `0.1.0-alpha.6`; the native internal installer counter is `0.1.6`.
- Update installation guidance to use matching alpha6 artifacts and retain the
  browser fallback. Do not use the alpha5 macOS downloads.

macOS bundles are ad-hoc signed, **not Developer ID signed and not notarized**.
Resource-integrity verification does not establish Gatekeeper acceptance or
publisher trust. Manual approval may still be required by macOS and must comply
with local policy; no quarantine-removal workaround is recommended. Windows
publisher signing is also not provided. Schema revision remains
`0025_run_lifecycle`, as introduced by alpha5; no new database migration is added.

## 0.1.0a5

- Add the local Angular Studio with a branded workflow editor, structured canvas,
  source editing, API profiles, run inspection, human tasks, and email conversations.
- Add `weave studio`, browser pairing, a same-origin API bridge, and a versioned,
  checksummed optional asset bundle that runs without Node.js.
- Include the Studio host in the CLI installer's verified dependency closure;
  install the matching browser bundle separately with its published checksum.
- Add native human tasks with scoped assignments, schema-driven decisions, durable
  waits, and explicit operator pause/resume controls.
- Add generic SMTP/IMAP email integration, conversation replies, fenced submissions,
  inbound receipts, and authorized correlation to workflow runs.
- Add identity/workspace discovery and separate task and email permissions.
- Add exact business/correlation-key and status searches across independent runs,
  terminal-run archive/restore, and separately authorized permanent deletion with
  dependency checks, audit receipts, and protection against idempotent recreation.
- Add a guided Studio connection setup with reviewed CIAM configuration, cancellable
  device/PKCE sign-in, connection checks, and authorized workspace selection.
- Add scoped people/access administration through the API, SDK, CLI, and Studio,
  keeping CIAM account management separate from Weave identity links and grants.
- Add typed configuration for every workflow step, recursive expression editing,
  integration action discovery, workflow settings, and a centered Start/End canvas.
- Add a Tauri desktop wrapper, branded drag-to-Applications macOS disk image,
  and native installer build matrix. Desktop artifacts are unsigned.

These changes require the matching server, migrations through 0025, CLI, and Studio
build. Back up the database and follow the upgrade guide. This remains an alpha;
desktop signing/notarization and live CIAM/mail/cloud validation are separate
deployment acceptance steps. Native human tasks use IR v1alpha2; workflows that
do not use them retain the prior IR. The earlier intermittent queue timeout is
not claimed fixed by this release.

## 0.1.0a4

- Add `weave platform` commands for an owned local developer installation:
  prerequisite checks, setup, foreground startup, readiness, first run, token
  refresh, and stop/resume with retained data.
- Show stage progress and terminal-only animation during setup; keep JSON output
  clean and offer `WEAVE_NO_ANIMATION=1` for plain terminal messages.
- Introduce Lumi, the firefly guide, in ASCII CLI help and editable documentation SVGs.
- Export compiled workflow graphs as terminal text, Mermaid, or standalone SVG.
- Add opt-in same-origin Swagger UI at `/docs` and the actual contract at
  `/openapi.json`; authenticated operations retain their existing grants.
- Generate the complete public API reference from the same OpenAPI contract.
- Add step-by-step SDK, YAML/Python workflow, custom inbound/outbound connector,
  API playground, local platform, and remote deployment tutorials.
- Redesign the documentation diagrams with human labels, vector icons, and
  explicit flow explanations; annotate deployment command blocks with their purpose.

This remains an alpha. The local stack is a developer installation, not a
production availability claim. Cloud and live-provider validation remain
specific to the target environment. The earlier intermittent queue timeout
remains unresolved; no retry or runtime scheduling policy is changed here.

## 0.1.0a3

- Render the official woven-W logo as plain ASCII in CLI help, with a compact
  layout for narrow terminals and clean machine-readable output.
- Add `weave init DIRECTORY` for a validated offline starter with a runnable
  simulation and instructions for editing, recompiling, and simulating again.
- Add `weave docs [TOPIC]` and clearer help linking authoring, platform startup,
  worker deployment, and configuration to their guides.
- Publish branded, searchable MkDocs documentation on GitHub Pages with light
  and dark appearances, copyable commands, and complete task-based navigation.
- Explain the platform startup path, service responsibilities, readiness checks,
  first use, shutdown, and restart; distinguish optional container stages.
- Check the documentation site in CI and retain source attribution in stylesheets.

No runtime engine, database migration, authentication, or worker retry policy
changes are introduced. The intermittent queue timeout recorded in 0.1.0a2's
release notes remains an unresolved observation; this release does not claim to fix it.

## 0.1.0a2

- Add a user-local CLI installer with verified release assets, hash-locked client
  dependencies, isolated environments, safe upgrades, and managed uninstall.
- Add ASCII branding, grouped plain-language help, nested command help, and
  `weave --version`, while preserving machine-readable command output.
- Reorganize onboarding around local authoring, an existing API, and running the
  platform; explain authentication, scope, expected results, and recovery steps.
- Add illustrated CLI, Kubernetes, AWS, Azure, and Google Cloud deployment guides.
- Include installer assets, deployment templates, and the complete public
  documentation in the source distribution; verify installation during checks.

No database migrations or workflow-language changes are introduced in this release.

## 0.1.0a1

- Typed workflow schemas, expressions, bounded compilation and canonical artifacts.
- Draft/publication/activation lifecycle, durable PostgreSQL runtime, workers,
  waits, signals, schedules, simulation, history/replay and incident controls.
- Native PyFly API, Python SDK/CLI, provider-neutral identity boundaries and local grants.
- HTTP/webhooks, PostgreSQL, Kafka, integration outbox, connector authoring and provider inbox.
- Teams personal-bot text, WhatsApp Cloud API text/template/status scope, and
  Telegram webhook text integration with local fixture/backend verification.
- OpenAPI 3.1 connector generation with bounded HTTP profiles and offline diagnostics.
- Operational capacity policies, compatibility checks, retention controls and optional telemetry.
- Container packaging, remote worker deployment, and backup/restore guides.
- Editable SVG diagrams and Apache 2.0 source attribution.

See [current capabilities](docs/capabilities.md) for integration scope and
verification boundaries. Messaging connectors have local protocol verification;
live provider account provisioning and message delivery require separate validation.
