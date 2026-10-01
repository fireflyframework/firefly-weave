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

# Mock-only simulation

The simulator executes the pinned compiled artifact through the same pure reducer
as production. It never calls a connector, secret provider, worker transport or
network. It verifies the current imported artifact and shared classified-value
policy before accepting inputs or mocks. It does not establish live readiness or
prove external behavior.

## Offline API and CLI

```python
from datetime import UTC, datetime
from firefly_weave.operations.debug.simulator import Simulator

sim = Simulator(artifact, mocks={"action:echo-action@2.0.0": 8},
                input=3, now=datetime(2026, 1, 1, tzinfo=UTC))
sim.set_breakpoints({"work"})
paused = sim.continue_until_breakpoint()
stepped = sim.next()
saved = sim.serialize()
sim = Simulator.restore(saved)
finished = sim.continue_until_breakpoint()
```

Mock keys are `action:<exact-reference>` or `node:<full-IR-node-id>` for Action
nodes. Node values override action values. Unknown keys and values violating any
pinned Action/implementation output schema are rejected. A JSON list is one
ordinary mocked result. There is no retry-outcome scripting or live fallback.
Missing mocks yield `WV-DEBUG-MISSING_MOCK` and a failed debugger view.

`weave workflow simulate request.json --output json` accepts a JSON object with
`artifact` (the exact compiled envelope), `mocks`, `input`, and timezone-aware
`now`. It continues until a breakpoint, quiescent wait, suspension or termination.
`--commands commands.json` applies a bounded JSON array of commands instead:

```json
[
  {"kind": "breakpoints", "node_ids": ["work"]},
  {"kind": "continue"},
  {"kind": "next"},
  {"kind": "signal", "name": "approved", "payload": true},
  {"kind": "advance_time", "seconds": 30},
  {"kind": "continue"}
]
```

The library exposes `next`, `continue_until_breakpoint`, `signal`, `advance_time`,
`inspect`, and `set_breakpoints`. Constructor and inspection execute no nodes.
Only `advance_time` moves time; signals and time movement return inspection
without reducing the graph. Signal schema checks happen before queuing a receipt.

## Boundaries and state

Each `next` admits one synthetic runtime fact **or** reduces one IR node. Event
admission increments accepted sequence once; nodes never create extra production
receipts. Start, end, branch-output and join nodes are visible labeled boundaries.
Action reduction prepares an isolated task intent. Mock completion is a later
fact boundary, released only after that event's node queue has drained.

`selected_node` identifies the next ready node; `active_nodes` lists asynchronous
waits/tasks. `current_nodes` combines them. Breakpoints use complete IR IDs,
including synthetic IDs, and stop before effects. Continuing a paused breakpoint
consumes that stop once. Ordered ready queues, event IDs, virtual timestamps and
boundary history survive serialization. Server IDs and wall-clock metadata are
outside the comparable `view` trace.

A cursor retains the original event checkpoint separately from speculative local
steps. An evaluation or classified-output incident rolls back every partial node
change and command from that event while retaining its valid prepared `@run`
deadline. Production `transition` drains this exact reducer atomically. There is
no generator stack or second mapping/control-flow implementation.

Due overall timeout wins, followed by terminal signal timeouts, then duration
continuations ordered by due time/node ID. A signal receipt must strictly precede
its signal deadline; equality loses. Receipt selection uses acceptance time/ID.
Duration waits cannot wake early. Suspension preserves the same deferred-fact
barrier as production. Kernel incidents remain `suspended`; missing mocks are a
debugger failure and do not fabricate a production worker incident.

## Server sessions

Routes are project-scoped beneath `/tenants/{tenant}/projects/{project}`:

- `POST /debug/sessions`: the same `{artifact,mocks,input,now}` request; returns
  `{id,revision,created_at,expires_at,view}`, initially revision 1.
- `GET /debug/sessions/{id}`: authorized inspection.
- `POST /debug/sessions/{id}/commands`: one command from the array above;
  requires numeric `If-Match` with the current revision.

Every operation requires the creator and a current project `simulate` capability.
No environment, activation, connection grant or worker release is required.
Tenant RLS and project predicates are independent of creator checks. Each mutation
locks the session row, reloads current grants, checks database-time expiry and
revision, then atomically commits one new revision. A stale revision returns409;
expired sessions return410. The fixed one-hour expiry never moves with virtual
time or inspection, and the application role cannot update its columns.

Native services keep no mutable simulator singleton. A fresh process restores the
serialized row. The exact imported source envelope and its source map are pinned;
filenames are labels and never server filesystem access. Expired rows are retained;
this release does not automatically purge them.

## Resource and data policy

Operator-owned release defaults are 10,000 node reductions, 10,000 synthetic
facts, 10,000 mutation commands, 1,000 pending signal receipts, 10,000 boundaries
per continue command, 366 days of total virtual movement, 3,600 seconds of server
lifetime and 64 MiB per serialized session. The byte ceiling includes the artifact,
mocks, cursor/checkpoint, queues, state and histories. Existing source/compiler
and per-payload ceilings still apply. Authors cannot override limits through the
workflow or HTTP request. Local embedding can supply operator-owned `DebugLimits`.

These are additional debugger limits: a valid production workflow can exceed the
session budget. The measured 80-transform workflow copying a 900,002-byte JSON
input reached 65,767,814 serialized bytes after 37 boundaries; the next command
was rejected by the 67,108,864-byte ceiling. `WV-DEBUG-LIMIT` is a debugger capacity
error, not compiler invalidity. A rejected command preserves its entry checkpoint
and server revision; it never stores partial progress. Continue's internal work
ceiling returns its bounded progress so another command can resume.

Secret-marked ordinary values are rejected at shared admission boundaries; no
masked values are executed. Computed rejection uses the kernel rollback above.
Unannotated ingress/business values remain the author's classification and the
operator's storage/access responsibility. No new classifier, taint engine or
protected secret store exists in the debugger. Structured errors never include
rejected payloads or fingerprints.
