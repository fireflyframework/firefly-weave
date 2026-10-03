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

# Inspect run history and replay it offline

**History** is the ordered record of facts Weave accepted for a run: it started,
an action completed, a signal arrived, a timer elapsed. **Replay** feeds those
facts back through the same pure runtime kernel to check that they tell a
consistent story. Replay sends no messages, executes no actions, and does not
resume the run. To start a new execution instead, see
[run retry](incident-operations.md).

**Who it is for.** Operators investigating a run, and developers who audit or
debug executions. **What you need.** Studio or the CLI connected to a platform
([connect the CLI](../guides/connect-to-api.md)), the run's `id` (from its start
response, or a trigger or provider receipt), and `run.read` in that environment
(the `viewer` role). For offline replay, also the exact compiled artifact the run
used. Allow 10 minutes.

![Offline consistency verification separated from operations that change execution](../diagrams/integrations-incidents-replay.svg)

Follow the top row left to right for read-only replay and its result. A
consistent report describes the retained evidence of a finished run; it does not
independently prove delivery or past authorization. The lower row is a separate,
authorized decision that changes execution.
[Open diagram at full size](../diagrams/integrations-incidents-replay.svg)

## Read a run's history

**In Studio**, open **Runs**, select a run, and read its **Timeline**: the
first page of its accepted facts (up to 100) as plain sentences, such as "Run
started". **View raw event** shows a fact as it was recorded. The run's **Now**
line explains why it waits, for example "Waiting for a signal at STEP", where
STEP is the step's ID. A run blocked by an incident shows "This run stopped at
STEP." instead.

**From the CLI**, run the commands with a saved platform and workspace from
`weave auth setup` (see
[how remote commands choose a platform](../guides/connect-to-api.md#how-remote-commands-choose-a-platform)).
Saved platforms are new in 0.1.0a7; with an alpha6 or earlier CLI, these
commands use [explicit mode](../guides/connect-to-api.md#scripts-and-ci-explicit-mode).

```sh
# Read the first page of accepted facts for one run.
weave runs history RUN_ID --limit 100 --output json
```

Expected: an event page with `run_id`, `scope`, `high_water_sequence`, `events`,
and `next_cursor`. When `next_cursor` is not `null`, pass it unchanged with
`--cursor` to read the next page. Do not compute sequence offsets yourself.

## Check that the history is consistent

Choose where the check runs.

**On the platform.** The server replays the run with the artifact it pinned. It
replays the same bounded evidence as an export (at most 1,000 facts, or fewer
with `--limit`):

```sh
# Ask the platform to replay the run's recorded facts with its pinned artifact.
weave runs replay RUN_ID --output json
```

Expected: a replay report (described below). It needs only `run.read`.

**Offline, on your computer.** Use this when you want an independent check with
an artifact you keep:

1. **Export the run's evidence:**

    ```sh
    # Export up to 1,000 accepted facts with source references and completeness metadata.
    weave runs export RUN_ID --output json > history.json
    ```

    Expected: a JSON document with `version: weave/history-v1`, `events`,
    `artifact_digest`, and `omissions`. The export never includes the artifact
    itself.

2. **Get the exact artifact the run used.** Use the `compiled-artifact.json` you
   kept when you compiled and published that version, or read the published
   version. Its `VERSION_ID` is `activation.request.version_id` in
   `weave runs read RUN_ID --output json`:

    ```sh
    # Save the compiled artifact of the published version the run is pinned to.
    weave definitions export VERSION_ID --collection workflows --output json \
      | python3 -c 'import json,sys; json.dump(json.load(sys.stdin)["artifact"], sys.stdout)' \
      > pinned-artifact.json
    ```

    Then compare the artifact's `digest` with the export's `artifact_digest`;
    they must be equal. A current source file with the same name is not a
    substitute.

3. **Replay locally:**

    ```sh
    # Replay the exported facts with the pinned artifact; no network, database, or executor is used.
    weave run replay --artifact pinned-artifact.json --events history.json
    ```

    Expected: a replay report and exit code `0` when it is `consistent`, `1` when
    it is `incomplete` or `inconsistent`. A nonzero exit is a verdict about the
    evidence, not a sign that the live run failed.

Release tooling should keep each published artifact with its release evidence, so
offline replay is always possible.

## Read the replay report

The report has `status`, `diagnostics` (each with `code` and `sequence`),
`last_verified_sequence`, `final_state`, `omissions`, and
`authorization_verified`, which is always `false`.

| Status | Meaning | What to do next |
| --- | --- | --- |
| `consistent` | A fully evidenced, finished stream whose transitions agree | Nothing; this proves internal consistency, not external delivery or past authorization |
| `incomplete` | The run is still open, the export was bounded, or evidence is missing or withheld | Check whether the run is still running or the export hit its limit; keep the verified prefix |
| `inconsistent` | The artifact, the event stream, or a transition does not match | Confirm you used the exact artifact and stream, then read the first diagnostic; never edit history to make it agree |

`last_verified_sequence` and `final_state` describe only what replay could
verify. Sequence gaps and unavailable operands stop at the verified prefix. These
make a report `inconsistent`: the wrong artifact, lock, or source identity;
duplicate accepted receipt identities; invalid event envelopes; invalid signal
arbitration; or a transition that differs from the recorded one. A run suspended
by a legitimate kernel incident remains an inspectable, verified prefix.

## Routes, pagination, and limits

All routes are under
`/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{id}`
and need current `run.read`:

| Route | CLI | Returns |
| --- | --- | --- |
| `GET .../history?limit=1..100&cursor=…` | `weave runs history` | One `EventPage`; default limit 100 |
| `GET .../export?limit=1..1000` | `weave runs export` | One `HistoryExport`; default limit 1,000 |
| `GET .../replay?limit=1..1000` | `weave runs replay` | One `ReplayReport` from the server's pinned artifact, over the same bounded evidence as the export; default limit 1,000 |

**Stable pages.** A history cursor binds the full scope, the run, the last
sequence, and the high-water sequence of the first page, so facts appended later
do not enter that traversal. Every page reloads your current `run.read`; knowing
a cursor grants nothing.

**Late completions.** An export also lists up to 100 ignored late completion
receipts in a separate, ordered `auxiliary_receipts` section. The run lock and
`auxiliary_high_water` define that snapshot, and `auxiliary_complete: false` says
it was truncated; it has no pagination. Ignored receipts never enter the accepted
sequence.

**Source references.** An export names the original scoped version, source hash,
source format, source map, and the IR node paths. Republishing the same
executable with new formatting does not change that reference. Missing or
ambiguous source provenance makes the export explicitly incomplete. Filenames in
a source map are labels: the service never opens them and never includes source
text or an artifact.

**Size limits.** Inspection limits do not cap how long a run may live.

- Pure replay accepts at most 10,000 events and 64 MiB of artifact plus events.
  It checks each transition's size before hashing a reconstruction larger than
  64 MiB and returns an incomplete verified prefix.
- Export streams rows and bounds the whole output, including maps, auxiliary
  receipts, and replay state, to 64 MiB, measured field by field. An oversized
  final state is withheld with a `resource_limit` omission, an incomplete status,
  and the last verified sequence kept.
- These are input and output bounds, not limits on kernel CPU or process memory;
  runtime state and transitions have their own budgets.

`replay(artifact, tuple(events))` in Python calls the same public kernel
transition as production and never regenerates outcomes. The strict
`event-page`, `history-export`, `recorded-evidence`, and `replay-report` schemas
are written by `weave schema export --directory DIR`. The older
`weave run history` and `weave run export` commands still work with
`--environment-url` (or `WEAVE_ENVIRONMENT_URL`) and `WEAVE_ACCESS_TOKEN`.

## What the evidence contains

New facts carry optional `weave/recorded-v1` evidence in an append-only, scoped
sidecar, committed in the same transaction as the fact:

- The start fact keeps the safe initial input. Every fact binds the run, the
  scope, the executable and dependency identity, and the exact source reference.
- Newly issued task and wait UUIDs map to kernel command nodes and to pinned
  action, release, and deadline facts. Receipts bind the generation and the
  selected wait, and bounded signal metadata records how signals were
  arbitrated.
- The expected transition is the SHA-256 of a canonical `classified-v1` safe
  projection of the kernel state, identified as `sha256-rfc8785-v1`; no
  duplicate snapshot is stored.
- Lease tokens, output fingerprints, and free-form operator reasons or evidence
  strings are never exported. Typed omissions distinguish a withheld value from a
  genuine `null`.

The [secret classification policy](schema-profile.md#durable-secret-classification)
still governs values and artifacts, including its limit on unmarked business
data.

**Task timing.** Task receipts can carry timing evidence:

- `live_admission` records the single database `checked_at` time of the final
  locked lease check and that attempt's `effective_deadline`. The issued task
  keeps its original deadline; claiming may shorten the effective deadline to the
  overall run deadline. Replay requires admission strictly before the effective
  deadline and checks that neither issued bound was extended. A fact's own
  timestamp is not its admission time.
- `recovery_decision` records when recovery decided. A scheduled retry must be at
  or after that time and strictly before the effective deadline; a recovery
  incident may be recorded after the deadline. Recovery retires its generation:
  only the next generation can supply an outcome, never before the recorded retry
  time. A manual safe retry also allows only its successor.
- Missing timing in older sidecars makes replay incomplete; contradictory timing
  or lifecycle facts make it inconsistent.

**What replay cannot prove.** Existing facts are never rewritten. Missing initial
input, identity, or issuance provenance stays incomplete; this version does not
fill it from snapshots. A policy-blocked run shows only safe metadata and explicit
omissions. A sidecar's presence does not prove completeness. Recorded facts
establish internal consistency, not independent proof of historical leases or
identity-provider authorization, which is why `authorization_verified` is always
`false`.

**Oversized finished runs.** A terminal control on an oversized run can keep its
full transition in PostgreSQL while omitting the optional expected-transition
digest with reason `resource_limit`. Replay then stays incomplete: a committed
cancellation or timeout does not by itself prove a fully replayable stream, and
earlier facts are never rewritten to fill the gap.

## Next steps

- [Incident operations](incident-operations.md): decide what to do when history
  shows an uncertain action.
- [Implement and operate a worker](../guides/workers.md): leases, completions, and
  recovery.
- [Manage executions and business cases](../guides/execution-management.md):
  find, archive, and purge runs.
- [Simulate a workflow run](simulation.md): try the same workflow with mocks
  before it runs for real.

## Troubleshooting

| What you see | Why | What to do |
| --- | --- | --- |
| `inconsistent` with an artifact or lock diagnostic | You replayed with a different artifact | Use the artifact whose `digest` equals the export's `artifact_digest` |
| `incomplete` for a run that is still running | Open runs are never fully evidenced | Replay again after the run finishes |
| `incomplete` with `WV-REPLAY-BOUNDED-PREFIX` | The run has more accepted facts than the export or replay limit (at most 1,000); the platform replay has the same bound | Keep the verified prefix: it covers the facts up to `last_verified_sequence` |
| `incomplete` with `WV-REPLAY-LIMIT` | The export reached its 64 MiB size budget, or one fact was too large to include | Keep the verified prefix; the withheld facts are not part of the check |
| `WV-REPLAY-INPUT: bounded artifact and recorded events required` | A file is not valid JSON, is too large, or has more than 10,000 events | Check both files and their sizes |
| HTTP 403 `WV-FORBIDDEN` | You lack `run.read` in that environment | Ask for the `viewer` role there; `operator` alone does not include it |
| HTTP 404 `WV-NOT-FOUND` | The run belongs to another environment than the selected workspace | Check the `Workspace:` line of `weave auth status`, or pass `--environment` |
| `auxiliary_complete: false` | More than 100 late completions were ignored | Expected; ignored receipts never change the run |
