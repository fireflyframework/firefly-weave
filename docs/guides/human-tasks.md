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

# Add human approvals with human tasks

A **human task** is a workflow step that waits for one authenticated person to
make a decision, such as approving or rejecting an expense. In BPM terms it is a
*user task*. This walkthrough builds a small expense approval end to end: you
decide who may review, publish and activate the workflow, start a run, and then
claim and complete the task.

**Who this is for:** process designers who add approvals, task managers who
decide who reviews, and developers who automate the same steps. It takes about
30 minutes. **What you need:** a running platform (your team's, or the
[local platform](local-platform.md)), the `weave` CLI, and an account with the
roles in step 1. Read [People and access](people-and-access.md) first if you
administer the platform.

**Release status.** Human tasks and every `human-*`, `definitions`, and `runs`
command here are also part of alpha6. Connecting with `weave auth setup`, which
this page uses, and `weave platform user` are new in 0.1.0a7.
The examples run from a source checkout with its development CLI,
`.venv/bin/weave`, because they read `examples/human_tasks/` from that
checkout. With an installed CLI, type `weave` instead. The Studio controls named
here, such as **Due after**, **Human task assignments**, and **Add a path for
each answer**, are also new in 0.1.0a7; an alpha6 or earlier Studio asks for
activation bindings as JSON.

## How a human task works

1. **The run reaches the step.** Weave creates a task with a title, the business
   context, a form, and the allowed decisions. No worker waits, and nothing is
   leased.
2. **Eligible people see it.** The task's *assignment* is a logical name in the
   workflow. When you activate a version, each environment binds that name to
   an *assignment binding*: a list of people, groups, or both. Only those
   candidates, with current task permission, can act.
3. **One person claims it.** A claim gives that person exclusive ownership. They
   can release it for others.
4. **The owner completes it.** They choose one allowed decision and submit form
   data that matches the form schema.
5. **The run continues.** The step's output is `{"decision": ..., "data": ...}`.
   A rejection is a normal business result, not an error; a following
   **Decision** step can branch on it.

Generic signals and worker completions cannot decide a human task, and viewer,
operator, or worker authority never counts as an approval.

**Choose where you do each part:**

| What you do | In Studio | With the CLI, API, or SDK |
| --- | --- | --- |
| Add the task to a workflow | A **Human task** step; see the [step reference](studio-step-reference.md#human-task) | A `humanTask` step in YAML (step 3) |
| Decide who may review | Not available in Studio | `weave human-groups` and `weave human-assignments` (step 2) |
| Activate with the reviewers | **Activate…** in the designer → **Human task assignments** | `weave definitions activations create` (step 3) |
| Claim and decide | **My tasks**; see [Complete human work](studio.md#complete-human-work) | `weave human-tasks claim` and `complete` (step 5) |
| Pause or resume the run | **Runs** → **Pause run** or **Resume run** | `weave runs pause` and `resume` (step 6) |

## 1. Choose identities and scope

**Why:** Weave decides who may act from its own role bindings, never from role
names in your identity provider. An administrator links each person and grants
roles, as described in [People and access](people-and-access.md).

| Role | What it permits here |
|---|---|
| `developer` at project scope | Publish the workflow |
| `deployer` at environment scope | Activate a published version |
| `operator` at environment scope | Start, pause, resume, or cancel runs |
| `viewer` | Read runs; does not grant approval |
| `task_participant` at environment scope | Read eligible tasks; claim, release, and complete your own tasks |
| `task_manager` at environment scope | Manage groups and assignment bindings, and reassign tasks; does not by itself allow completing a task |
| `worker` | Run worker actions; can never act as a human reviewer |

For this walkthrough, one person needs `developer`, `deployer`, `operator`,
`viewer`, `task_participant`, and `task_manager`. `operator` alone cannot read
runs. In production, give administration, deployment, and review to different
people. A reviewer must be an active **human** principal with current
`task_participant` permission.

**On the local platform, one command creates such a person:**

```sh
# Development only: create alice with every role this walkthrough needs in the demo workspace.
.venv/bin/weave platform user --username alice --role developer --role deployer \
  --role operator --role viewer --role task_participant --role task_manager
```

Expected: `Username: alice`, a generated password that is shown once, the
granted roles, and the `weave auth setup` command to run next. See
[Create a person who can sign in](local-platform.md#5-create-a-person-who-can-sign-in).
**Will you also call a REST API from this workflow?** Add `--role tenant_admin`
to the same command now, because creating integration connections needs it and
an existing username is never changed.

**Connect once with your own account** and choose the workspace this walkthrough
uses; [Connect the CLI to a platform](connect-to-api.md) explains each prompt:

```sh
# Save the platform, sign in as yourself, and pick the tenant, project, and environment.
.venv/bin/weave auth setup https://your-weave-api.example
# Confirm the signed-in account and the workspace before changing anything.
.venv/bin/weave auth status --check
```

Expected: a `Sign-in:` line that starts with `signed in as YOUR_ACCOUNT,
verified with the platform`, and the workspace you chose. Every command below
then uses that saved platform, your sign-in, and its workspace, so it needs no
address, token, or scope flags. Credentials stay in your credential store.

- **Your principal UUID**, which step 2 needs for a solo test, is
  `identity.principal_id` in `.venv/bin/weave auth status --check --output json`.
- **Your workspace's UUIDs**, which step 3 needs, are in the `workspace` object
  of `.venv/bin/weave auth status --output json`.
- **Unset scope variables first.** An exported `WEAVE_BASE_URL` switches
  commands to explicit mode, and exported `WEAVE_TENANT_ID`, `WEAVE_PROJECT_ID`,
  or `WEAVE_ENVIRONMENT_ID` values replace the saved workspace.

**To use explicit mode instead**, for example with an alpha6 or earlier CLI, set
the workspace IDs and sign in with the operator's non-secret connection file,
`human-auth.json`:

```sh
# Explicit mode: the address and workspace come from these variables, not from a saved platform.
export WEAVE_BASE_URL="https://your-weave-api.example"
export WEAVE_TENANT_ID="YOUR_TENANT_UUID"
export WEAVE_PROJECT_ID="YOUR_PROJECT_UUID"
export WEAVE_ENVIRONMENT_ID="YOUR_ENVIRONMENT_UUID"
# Sign in with the connection file; tokens go to your credential store.
.venv/bin/weave auth login --auth-config human-auth.json
```

Expected: one JSON document with `"authenticated": true`. Then add
`--auth-config human-auth.json` to every command below.

**Every command prints JSON and takes the task or run UUID as a positional
argument**, as `human-tasks claim --help` and `runs pause --help` show.

## 2. Create a reviewer group and assignment

**Why:** the assignment binding is where you say who may review in this
environment, without changing the workflow. Groups let you change membership
later in one place. Run these commands as a task manager.

Save `group.json`, with the reviewer's principal UUID (yours, for a solo test):

```json
{
  "name": "expense-reviewers",
  "member_ids": ["YOUR_REVIEWER_PRINCIPAL_UUID"],
  "expected_revision": 0
}
```

```sh
# Create the reviewer group; the key makes a retry of this exact request safe.
.venv/bin/weave human-groups put \
  --request group.json --idempotency-key expense-group-create-1
```

Expected: the group as one JSON object with its `id`, `name`, `revision` (1 for
a new group), and `member_ids`. Copy its `id` into `assignment.json`:

```json
{
  "name": "expense-reviewers",
  "principal_ids": [],
  "group_ids": ["RETURNED_GROUP_UUID"],
  "enabled": true,
  "expected_revision": 0
}
```

```sh
# Bind the assignment name to the group, then list the bindings in this environment.
.venv/bin/weave human-assignments put \
  --request assignment.json --idempotency-key expense-assignment-create-1
.venv/bin/weave human-assignments list
```

Expected: the binding with its `binding_id`, `name`, `revision`, `group_ids`,
and `enabled`, and then a list whose `items` include it. Keep the `binding_id`
for step 3.

- **Direct assignment:** set `principal_ids` to the reviewers' UUIDs and
  `group_ids` to an empty list instead.
- **Revisions:** `expected_revision` 0 creates; an edit must send the current
  revision of the group or binding.
- **Idempotency keys:** each new change needs a new key. An exact retry reuses
  its original key with unchanged JSON.

## 3. Publish and activate a complete workflow

**Why:** a workflow names only the logical assignment, `expense-reviewers`.
Activation connects that name to the binding you just created, for one
environment.

The complete [expense workflow](../../examples/human_tasks/workflow.yaml) has
input and output schemas, one human task, and an output expression. Its
reviewer step is:

```yaml
# The human task: who may review, what they see, and what they can decide.
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

- **`title`** must produce a non-empty string of at most 512 characters, and
  **`context`** must produce an object; reviewers see both.
- **`formSchema`** is the form the reviewer fills in, using the compiler's
  bounded JSON Schema profile.
- **`dueSeconds`** marks the task overdue after 5 minutes; it decides nothing.
- **`expirySeconds`** times out the whole run after 30 minutes if nobody
  decides. Without it, the task has no deadline of its own. The workflow's
  overall timeout still applies, also while the run is paused.

In Studio, the same fields are the **Human task** step's **Assignment binding**,
**Title**, **Context**, **Decisions**, **Due after**, **Expires after**, and
**Form schema**.

Build the publication request from the file instead of escaping YAML by hand:

```sh
# Wrap the YAML source in a publication request.
.venv/bin/python - <<'PYTHON'
import json
from pathlib import Path
Path("publish.json").write_text(json.dumps({
    "format": "yaml",
    "source": Path("examples/human_tasks/workflow.yaml").read_text(),
}))
PYTHON
# Publish an immutable version of the workflow in your project.
.venv/bin/weave definitions publish --collection workflows \
  --request publish.json \
  --idempotency-key expense-publish-1
```

Expected: the published version with its `id`, `name` (`expense-approval`),
`version` (`1.0.0`), and `digest`. Copy the `id` and `digest` into
`activation.json`, with your workspace's UUIDs from step 1:

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

```sh
# Activate the version in your environment with the reviewer binding.
.venv/bin/weave definitions activations create \
  --request activation.json --idempotency-key expense-activate-1
```

Expected: the activation with its `id` and an `assignment_pins` entry for
`expense-reviewers`. **In Studio**, select **Activate…** in the designer of the
published version. The dialog, titled with the workflow and version, such as
"Activate expense-approval 1.0.0", shows the same choice under **Human task
assignments**: for `expense-reviewers`, pick the binding in **Choose an
assignment binding**. When only one enabled binding has that name, Studio
selects it for you. If none exists, Studio says "No enabled assignment binding
named expense-reviewers exists here. A task manager creates one." Then select
**Activate version**.

**Activation pins the binding.** It records the binding's revision and
candidates. Later edits to the binding do not rewrite existing activations or
run history, but current group membership and grants still decide each claim
and completion.

## 4. Start a run and find the task

Save `start.json` with the activation's `id`:

```json
{
  "activation_id": "RETURNED_ACTIVATION_UUID",
  "input": {"amount": 125, "description": "Travel expense"},
  "business_key": "expense-2026-0042"
}
```

```sh
# Start one run, then list the tasks you are eligible for.
.venv/bin/weave runs start \
  --request start.json --idempotency-key expense-run-1
.venv/bin/weave human-tasks list --limit 50
# Read one task in full; reading does not claim it.
.venv/bin/weave human-tasks read TASK_UUID
```

Expected: `runs start` prints the run with its `id`. Within moments, the task
list includes a task whose `run_id` is that run, with `"status": "ready"` and
`"revision": 1`. The full task shows its business context, form schema,
decisions, current revision, owner, due and expiry times, and audit history.

The list shows tasks you are eligible for; a task manager also sees the other
tasks in their authorized workspace. The [business key](execution-management.md#1-choose-identifiers-with-different-jobs)
groups runs for one case; it is not unique.

**In Studio**, open **My tasks**: the task shows the status "Ready to claim".
The status filter next to the search box, **All tasks** by default, also offers
**Ready to claim**, **Claimed**, **Completed**, **Expired**, and **Canceled**.

## 5. Claim, then decide

**Why:** claiming first stops two reviewers from deciding the same task.

Save `claim.json` with the task's **current** revision, which is 1 for a new
task:

```json
{"expected_revision": 1}
```

```sh
# Claim the task for yourself, then read it to confirm.
.venv/bin/weave human-tasks claim TASK_UUID \
  --request claim.json --idempotency-key expense-claim-1
.venv/bin/weave human-tasks read TASK_UUID
```

Expected: the task with `"status": "claimed"`, your principal UUID as
`claimant_id`, and a new `revision`. Put that revision in `decision.json`; this
example assumes it is 2:

```json
{"expected_revision": 2, "decision": "approve", "data": {"note": "Reviewed receipt"}}
```

```sh
# Record the decision, then read the run to see it continue.
.venv/bin/weave human-tasks complete TASK_UUID \
  --request decision.json --idempotency-key expense-decision-1
.venv/bin/weave runs read RUN_ID
```

Expected: the task with `"status": "completed"` and the output
`{"decision":"approve","data":{"note":"Reviewed receipt"}}`. This workflow
returns that output as its result, so the run finishes. Use `reject` instead of
`approve` to record a rejection.

**In Studio**, do the same in **My tasks**:

1. Open the task and select **Claim task**. Expected: "Task claimed. Fill in
   the form, then choose a decision." and the status "Claimed by you".
2. Fill in the form under **Your decision** and select **Approve**. Studio asks
   once, "Approve Review expense? You can't change this later."; select
   **Approve** again, or **Back** to return to the form.

Expected: "Approved Review expense." Studio then opens the next task that is
ready to claim, if there is one; the decided task keeps its **Recorded
outcome**. **Release task** gives a claimed task back instead: "Task released.
Others can claim it now." If someone else acted first, Studio keeps your answers
and says "This task changed or you lost access to it. Your answers are still
here. Refresh before you try again." Select **Refresh** in the task's detail to
read its current state before you act again.

**Completion is checked every time.** It needs current ownership, current
candidate membership and grants, an allowed decision, and form data that matches
the schema. A stale revision is refused: read the task again before deciding.
An exact retry with the same key returns the original receipt, after the same
authorization checks; a changed request under the same key is refused.

- **Release:** `human-tasks release TASK_UUID` with the current
  `expected_revision` and a new idempotency key returns the task to the other
  candidates.
- **Reassign:** a task manager uses `human-tasks reassign TASK_UUID` with
  `expected_revision`, the new `principal_id`, and an audit `reason`. The new
  owner must be eligible.
- **Cancel:** cancelling the run closes its unfinished tasks.

## 6. Pause and resume when operationally necessary

**Why:** an operator can hold a run, for example while finance clarifies an
expense. Run controls have their own *control revision*, separate from the task
revision. For a run that was never paused it is 0. Save `pause.json`:

```json
{"expected_revision": 0, "reason": "Wait for finance clarification"}
```

```sh
# Pause the run at its next safe point; the reason goes to the audit log.
.venv/bin/weave runs pause RUN_ID \
  --request pause.json --idempotency-key expense-pause-1
```

Expected: the run with `state.manual_paused` set to `true` and
`state.control_revision` set to 1. Use that value in `resume.json`:

```json
{"expected_revision": 1, "reason": "Finance clarification received"}
```

```sh
# Remove the manual barrier so the run can continue.
.venv/bin/weave runs resume RUN_ID \
  --request resume.json --idempotency-key expense-resume-1
```

Both commands need `operator`. In Studio, **Pause run** and **Resume run** in
the run's detail ask for a **Reason** and do the same.

- **What a pause stops:** new claims and admissions, and the run's
  continuation.
- **What it lets finish:** external work that was already leased. An already
  claimed task can still record a valid decision; the run continues after
  resume.
- **What it does not stop:** deadlines, including task expiry and the workflow
  timeout. Unresolved incidents still block the run after resume.

## Use the SDK and the simulator

The runnable [Python example](../../examples/human_tasks/approval.py) publishes,
activates, starts, claims, and completes a task with human credentials from its
environment (`WEAVE_BASE_URL`, `WEAVE_ACCESS_TOKEN`, the three scope IDs, and
`WEAVE_PRINCIPAL_ID`). For local authoring, `HumanTaskStep` works with
`WorkflowBuilder.add_step`. The SDK's task and run controls go through the same
server checks as Studio and the CLI.

In a [simulation](../reference/simulation.md), the debugger's `human_decision`
command takes the step ID and a `{decision, data}` payload. It is a simulated
fact, not a real human receipt. Studio's **Simulate** offers the same with
**Submit decision**. A workflow with human tasks compiles to
`weave/ir-v1alpha2` or higher, because the version is the highest level any
construct in the workflow needs; see the
[IR version](../reference/compiler.md#executable-and-source-envelope) rules.

## If something goes wrong

| What you see | Why | What to do |
| --- | --- | --- |
| `WV-ASSIGNMENT` (HTTP 422) when you activate | The activation does not bind every human task's assignment, or the binding is missing, disabled, or in another environment | List bindings with `human-assignments list` and pass an enabled one for each assignment name |
| `WV-ASSIGNMENT` "Assignment requires candidates" | The binding has no people and no groups | Add `principal_ids` or `group_ids` |
| `WV-GROUP-REVISION` or `WV-ASSIGNMENT-REVISION` (HTTP 409) | Someone changed the group or the binding since you read it, or `expected_revision` is 0 for one that already exists | Read it again and send its current revision |
| The task list is empty | The run has not reached the task yet, you are not a candidate, or you lack `task_participant` | Read the run; check the binding's members and your grants |
| `WV-HUMAN-TASK-REVISION` (HTTP 409), or Studio says "This task changed or you lost access to it." | The task changed since you read it | Run `human-tasks read` and use the current revision; in Studio, select **Refresh** in the task's detail |
| `WV-HUMAN-TASK-CLAIM` (HTTP 409) | The task is no longer available to claim | Read it: someone else may own it |
| `WV-HUMAN-TASK-OWNER` (HTTP 409) | You are completing or releasing a task you have not claimed | Claim it first, or ask its owner |
| `WV-HUMAN-TASK-FORM` (HTTP 422) | The decision is not allowed, or the data does not match the form | Compare with the task's `decisions` and `form_schema` |
| `WV-HUMAN-TASK-EXPIRED` (HTTP 409) | The task's expiry or the workflow's deadline passed | The run has timed out; start a new run if needed |
| `WV-HUMAN-TASK-TERMINAL` (HTTP 409) | The task or its run has already finished | Nothing to decide; read the run |

## Next steps

- [Complete tasks in Studio](studio.md#complete-human-work) with **My tasks**.
- [Branch on the decision](studio-step-reference.md#human-task) with **Add a
  path for each answer** in the human task's inspector, such as "Add a path for
  each answer (approve, reject)".
- [Manage runs and business cases](execution-management.md): find runs by
  business key, archive them, and delete them permanently.
- [Give people the right access](people-and-access.md) to add reviewers.
