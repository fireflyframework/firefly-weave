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

# Human tasks

Human tasks and Studio are included in alpha5. Install the matching client/server
and follow the [Studio setup guide](studio.md); alpha4 predates these commands.
The examples below run from the matching alpha5 source checkout with its
development CLI (`.venv/bin/weave`). With an installed alpha5 CLI, use `weave`
in place of that executable path; obtain the referenced example files from the
same tag. It uses saved human OAuth credentials, never an application
or worker token. See [identity setup](../operations/identity-and-secrets.md) first.

A `humanTask` waits for one authenticated person's decision. It creates no worker
lease. Generic signals and worker completions cannot decide it. Rejection is a
normal business output; a subsequent switch can branch on the decision.

## 1. Choose identities and scope

An administrator provisions active local human principals and grants their roles.
Provider role names alone grant no Weave authority.

| Role | What it permits here |
|---|---|
| `developer` at project scope | Publish the workflow |
| `deployer` at environment scope | Activate a published version |
| `operator` at environment scope | Start, pause, resume or cancel executions |
| `task_participant` at environment scope | Read eligible tasks, claim, release and complete owned tasks |
| `task_manager` at environment scope | Manage groups/bindings and reassign tasks; does not itself grant completion |
| `viewer` | Inspect runs; does not grant approval |
| `worker` | Execute worker actions; cannot act as a human reviewer |

For the entire walkthrough, the same human needs `developer`, `deployer`,
`operator`, `task_manager`, `task_participant` and `viewer` in their appropriate
scopes. `operator` alone does not grant run-read authority. In production, separate
administrator, deployer and reviewer identities as appropriate. Candidates need
both an active **human** principal and current task-participant authority.

Set the actual scope UUIDs and API address in your shell. The UUIDs below are
illustrative; replace every placeholder with IDs from your platform.

```bash
export WEAVE_BASE_URL="https://your-weave-api.example"
export WEAVE_TENANT_ID="YOUR_TENANT_UUID"
export WEAVE_PROJECT_ID="YOUR_PROJECT_UUID"
export WEAVE_ENVIRONMENT_ID="YOUR_ENVIRONMENT_UUID"
```

Every command below inherits these scope variables. `human-auth.json` is the
non-secret OAuth configuration used for a completed `weave auth login`; credentials
remain in the configured credential store. Commands take the resource UUID as a
**positional argument**, as shown by `human-tasks claim --help` and `runs pause --help`.

## 2. Create a reviewer group and assignment

Save this as `group.json`, substituting the reviewer's actual principal UUID:

```json
{
  "name": "expense-reviewers",
  "member_ids": ["YOUR_REVIEWER_PRINCIPAL_UUID"],
  "expected_revision": 0
}
```

Run as a task manager:

```bash
.venv/bin/weave human-groups put --auth-config human-auth.json \
  --request group.json --idempotency-key expense-group-create-1
```

Copy the returned group's `id` into `assignment.json`:

```json
{
  "name": "expense-reviewers",
  "principal_ids": [],
  "group_ids": ["RETURNED_GROUP_UUID"],
  "enabled": true,
  "expected_revision": 0
}
```

```bash
.venv/bin/weave human-assignments put --auth-config human-auth.json \
  --request assignment.json --idempotency-key expense-assignment-create-1
.venv/bin/weave human-assignments list --auth-config human-auth.json
```

Keep the returned `binding_id`. For direct assignment, set `principal_ids` to the
reviewer UUIDs and `group_ids` to an empty list instead. Zero means creation; edits
must supply the current binding/group revision. Each new mutation uses a new
idempotency key; an exact retry reuses its original key and unchanged JSON.

## 3. Publish and activate a complete workflow

The complete [expense workflow](../../examples/human_tasks/workflow.yaml) contains
input/output schemas, a human node and the output expression. Its reviewer node is:

```yaml
- id: review
  kind: humanTask
  assignment: expense-reviewers
  title: {literal: Review expense}
  context: {ref: /input}
  formSchema:
    type: object
    properties: {note: {type: string}}
    required: [note]
    additionalProperties: false
  decisions: [approve, reject]
  dueSeconds: 300
  expirySeconds: 1800
```

Title must evaluate to a nonempty string of at most 512 characters; context must be
an object. Forms use the compiler's bounded JSON Schema profile. Due time marks
overdue work. Explicit expiry times out the entire run; omitted expiry creates no
task deadline. Overall workflow timeout still applies, including during pause.

Build the publication request from the file rather than escaping YAML manually:

```bash
.venv/bin/python - <<'PYTHON'
import json
from pathlib import Path
Path("publish.json").write_text(json.dumps({
    "format": "yaml",
    "source": Path("examples/human_tasks/workflow.yaml").read_text(),
}))
PYTHON
.venv/bin/weave definitions publish --collection workflows \
  --auth-config human-auth.json --request publish.json \
  --idempotency-key expense-publish-1
```

Copy the returned version `id` and `digest`, then create `activation.json`:

```json
{
  "version_id": "RETURNED_VERSION_UUID",
  "artifact_digest": "RETURNED_ARTIFACT_DIGEST",
  "scope": {
    "tenant_id": "YOUR_TENANT_UUID",
    "project_id": "YOUR_PROJECT_UUID",
    "environment_id": "YOUR_ENVIRONMENT_UUID"
  },
  "assignment_binding_ids": {
    "expense-reviewers": "RETURNED_BINDING_UUID"
  }
}
```

```bash
.venv/bin/weave definitions activations create --auth-config human-auth.json \
  --request activation.json --idempotency-key expense-activate-1
```

Activation pins the assignment binding revision and candidate IDs. Workflow source
contains the logical assignment name, not provider usernames or CIAM roles.
Binding edits do not rewrite existing activation/run history; current group
membership and grants still govern each decision.

## 4. Start and find the task

Save `start.json` using the returned activation `id`:

```json
{
  "activation_id": "RETURNED_ACTIVATION_UUID",
  "input": {"amount": 125, "description": "Travel expense"},
  "business_key": "expense-2026-0042"
}
```

```bash
.venv/bin/weave runs start --auth-config human-auth.json \
  --request start.json --idempotency-key expense-run-1
.venv/bin/weave human-tasks list --auth-config human-auth.json --limit 50
.venv/bin/weave human-tasks read TASK_UUID --auth-config human-auth.json
```

Select the task whose `run_id` matches your start response. The task includes its
business context, form schema, decisions, current revision, owner, due/expiry times
and audit history. The list shows eligible work; a manager may inspect all tasks in
its authorized scope. A read does not claim work.

## 5. Claim, then decide

Save `claim.json` with the task's **current** revision (one for a fresh task):

```json
{"expected_revision": 1}
```

```bash
.venv/bin/weave human-tasks claim TASK_UUID --auth-config human-auth.json \
  --request claim.json --idempotency-key expense-claim-1
.venv/bin/weave human-tasks read TASK_UUID --auth-config human-auth.json
```

Use the claim response's revision in `decision.json`; the following assumes it is two:

```json
{"expected_revision": 2, "decision": "approve", "data": {"note": "Reviewed receipt"}}
```

```bash
.venv/bin/weave human-tasks complete TASK_UUID --auth-config human-auth.json \
  --request decision.json --idempotency-key expense-decision-1
.venv/bin/weave runs read RUN_UUID --auth-config human-auth.json
```

Completion requires current ownership, candidate membership and grants, an allowed
decision and schema-valid data. The node output is
`{"decision":"approve","data":{"note":"Reviewed receipt"}}`. Replace approve
with reject to record rejection. A stale revision conflicts; reread before making
a new decision. Exact retries return the original receipt only after current
authorization checks. Changed payloads under the same key conflict.

Release uses `human-tasks release TASK_UUID` with current `expected_revision` and a
new idempotency key. Manager reassignment additionally requires `principal_id` and
an audit `reason`; the target must remain eligible. Cancellation closes unfinished
tasks. Viewer, operator and worker authority never grants human approval.

## 6. Pause and resume when operationally necessary

Run controls use their own control revision, separate from the human task revision.
For a new run, save `pause.json`:

```json
{"expected_revision": 0, "reason": "Wait for finance clarification"}
```

```bash
.venv/bin/weave runs pause RUN_UUID --auth-config human-auth.json \
  --request pause.json --idempotency-key expense-pause-1
```

Use the returned run state's `control_revision` in `resume.json`, for example:

```json
{"expected_revision": 1, "reason": "Finance clarification received"}
```

```bash
.venv/bin/weave runs resume RUN_UUID --auth-config human-auth.json \
  --request resume.json --idempotency-key expense-resume-1
```

These commands require operator authority. Pause fences new claims/admissions and
continuation. Already leased external work may finish; an already claimed human
task can record a valid decision, with continuation deferred. Resume removes the
manual barrier; unresolved incidents still block execution. Deadlines continue.

## SDK, simulation and execution management

The runnable [Python example](../../examples/human_tasks/approval.py) publishes,
activates, starts, claims and completes a native task using environment-provided
human credentials. `HumanTaskStep` works with `WorkflowBuilder.add_step` for offline
authoring. SDK task and run controls use the same backend checks as Studio and CLI.

The debugger's `human_decision` takes the node and `{decision, data}` payload. It is
a simulation fact, not an authoritative human receipt. Human workflows compile as
`weave/ir-v1alpha2`; workflows without this feature retain legacy alpha1 semantics.

Business keys permit multiple executions; idempotency keys distinguish retries
from new starts. Discover by exact business/correlation/status filters, then signal
a specific run UUID rather than selecting ambiguously by business key. Continue to
[Manage executions and business cases](execution-management.md) for archive and
permanent deletion semantics.
