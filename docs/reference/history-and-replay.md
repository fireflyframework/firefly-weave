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

# Recorded history and offline replay

![Offline consistency verification separated from operations that change execution](../diagrams/integrations-incidents-replay.svg)

**How to read this diagram:** Take the left branch to replay. A consistent report describes the retained terminal evidence; it does not independently prove delivery or historical authorization. The right branch requires a separate authorized operations decision.

## Inspect what happened without repeating it

**History** is the ordered record of accepted run facts. **Replay** feeds those
facts through the pure runtime kernel to check consistency. It sends no messages,
executes no Actions, and does not resume a run. To create a new execution, use
[run retry](incident-operations.md) instead.

After [standalone setup](../guides/standalone.md), obtain the run `id` from its
start response or trigger/provider receipt and a credential with scoped `run.read`.
Keep the exact compiled artifact retained for that run's activation. A current
source file with the same name is not a substitute: compare its digest with the
run/export `artifact_digest` before offline replay. Publication/deployment tooling
should retain this artifact alongside the release evidence.

1. Set `WEAVE_ENVIRONMENT_URL` to the complete scoped environment URL and use the
   protected `WEAVE_ACCESS_TOKEN` configuration described in [CLI login](cli.md#login-and-secure-persistence).
2. Read history using the commands below. Save `next_cursor` unchanged for the next
   page; do not compute sequence offsets yourself.
3. Export the safe evidence, then replay locally with the matching artifact.
   Export does not automatically include that artifact. If you do not have it,
   `GET .../runs/RUN_UUID/replay` provides the server's report under the same read authority.
4. Inspect `status`, `diagnostics`, `last_verified_sequence`, and `omissions`.
   The CLI exits 0 for `consistent`, 1 for `incomplete` or `inconsistent`; a
   nonzero replay exit is not itself evidence that a live run failed.

| Report | What to do next |
| --- | --- |
| `consistent` | The evidenced terminal transitions agree; external delivery and historical authorization are not independently proven |
| `incomplete` | Check whether the run is open, export is bounded, or evidence was omitted/unavailable; retain the verified prefix |
| `inconsistent` | Confirm the exact artifact and event stream, then inspect the first diagnostic; do not edit history to force agreement |

See [worker lifecycle](../guides/workers.md) for leases and completions, and
[incident operations](incident-operations.md) when inspection requires an operator
decision. Replay itself changes no durable state.

## Routes, pagination, and evidence


`GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/runs/{id}/history`
returns an `EventPage`. `limit` is 1–100 (default 100). Pass `next_cursor` unchanged
for the next page. The cursor binds the full scope, run, last sequence and the
initial high-water sequence, so later appends do not enter that traversal. Every
page reloads current `run.read` authority; knowing a cursor grants no access.

The sibling `/export` and `/replay` routes require the same current authority.
Export reads at most 1,000 accepted events (configurable `limit` 1–1,000). It also
returns up to 100 ignored late completion receipts in a separate ordered section.
The run lock and `auxiliary_high_water` timestamp define that one auxiliary
snapshot; `auxiliary_complete=false` discloses truncation. Auxiliary pagination is
not offered. Ignored receipts never enter the accepted kernel sequence.

An export identifies the original scoped version, source hash, source format,
source map and IR node paths. Publishing the same executable with new formatting
does not replace that reference. Missing/ambiguous original source provenance is
explicitly incomplete. Filenames in a source map are labels; the service neither
opens them nor automatically includes source literals or an executable artifact.
Use the separately authorized pinned artifact when replaying offline.

```sh
weave run history RUN_UUID --environment-url ENVIRONMENT_URL --limit 100
weave run export RUN_UUID --environment-url ENVIRONMENT_URL > history.json
weave run replay --artifact pinned-artifact.json --events history.json
```

The authenticated commands read `WEAVE_ACCESS_TOKEN`. The replay command is local
and accepts an export or a JSON event array. It has no database, catalog, executor,
secret-provider or network fallback. `replay(artifact, tuple(events))` calls the
same public pure kernel transition as production; it never regenerates outcomes.
Strict `event-page`, `history-export`, `recorded-evidence` and `replay-report`
schemas are available through `weave schema export`.

`consistent` means a fully evidenced terminal stream. A valid open run, bounded
prefix, missing provenance or required redaction is `incomplete`. Sequence gaps
and unavailable operands stop at the verified prefix. Wrong artifact/lock/source
identity, duplicate accepted receipt identities, invalid event envelopes, invalid
arbitration or unequal expected transitions are `inconsistent`. Legitimate kernel
incidents remain inspectable verified suspended prefixes. `last_verified_sequence`
and `final_state` describe only verified reconstruction.

New facts have optional `weave/recorded-v1` evidence in an append-only scoped
sidecar, committed atomically with the accepted event. Its start fact retains safe
initial input; every fact binds run/scope, executable/dependency identity and exact
source reference. Newly issued task/wait UUIDs map to kernel command nodes and
pinned action/release/deadline facts. Receipts bind generation and selected wait,
and bounded signal metadata records arbitration evidence. The transition
expectation is SHA-256 of a canonical `classified-v1` safe kernel projection,
identified by `sha256-rfc8785-v1`. It contains no duplicate transition snapshot.
Lease tokens, output fingerprints and freeform operator reason/evidence strings
are not exported. Typed omissions distinguish withheld operands from genuine null.
The shared classification/admission policy still governs values and artifacts;
unannotated business data retains that policy's documented limitation.

Task receipts also carry optional timing evidence. `live_admission` records the
single database `checked_at` used by the final locked lease check, plus that
attempt's actual `effective_deadline`. The issued TaskFact retains its original
deadline; claiming may cap the effective deadline to the overall run deadline.
Replay requires admission strictly before the effective deadline and checks that
it does not extend either issued bound. Event creation can occur after the
admission and even after the deadline; its timestamp is not the admission time.
`recovery_decision` records the actual recovery decision time: a scheduled retry
must be at or after that time and strictly before the effective deadline. A
recovery incident may legitimately be recorded after the deadline. Recovery
retires its generation; only the next generation can supply a subsequent outcome,
and it cannot be admitted before the recorded retry time. Explicit failed
attempts may be followed by recovery for that same generation; manual safe retry
also permits only its successor. Missing timing in older sidecars is incomplete,
while contradictory supplied timing or lifecycle facts are inconsistent. No
historical lease expiry/token authorization is inferred from these safe facts.

Existing historical events are never rewritten. Missing initial input, identity or issuance
provenance remains incomplete; this version does not supplement it from snapshots.
A policy-blocked run exposes only safe metadata and explicit omissions. Sidecar
presence alone is not proof of completeness. Recorded task/release/arbitration
facts establish internal consistency, not independent historical lease or identity
provider authorization; every replay report states `authorization_verified=false`.

Inspection limits do not cap durable run lifetime. Pure replay accepts at most
10,000 events and 64 MiB of serialized artifact-plus-events input. It checks a
transition's wire size before hashing a reconstruction exceeding 64 MiB and returns
an incomplete verified prefix. Export streams rows before accumulating them; the
complete output envelope, including maps, auxiliary receipts and replay state, is
bounded to 64 MiB. A field-wise UTF-8 size check avoids constructing a full dump to
check that limit. Oversized final state is withheld with a `resource_limit` omission,
incomplete status and the retained last verified sequence. This is an inspection
input/output bound, not a bound on kernel evaluation CPU or peak process memory:
runtime state and transition admission have separate operational budgets.
A serialized byte ceiling is not a process-memory guarantee.

A terminal control for an oversized historical run can preserve its full durable
transition inside PostgreSQL while omitting the optional expected-transition
digest from recorded evidence with reason `resource_limit`. That omission keeps
replay incomplete; a committed cancellation or timeout does not by itself prove
a fully replayable stream. Earlier events and evidence are never rewritten to
fill that gap.
