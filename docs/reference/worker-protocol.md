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

# Remote worker protocol

A **worker** is your own process that asks Weave for tasks, runs your business
code, and reports each outcome. This reference describes the HTTP protocol
between a worker and the platform: the endpoints, the lease rules, and what
happens on retries, cancellation, and lost responses. It is for integration
developers who write their own task transport or diagnose lease and completion
behavior.

**Building your first worker?** Follow the [worker guide](../guides/workers.md)
instead: it explains the example handler, the roles involved, and the
deployment order. The Python SDK's `Worker` and `WorkerTransport` implement
everything on this page for you. Before adding workers, complete the
[standalone tutorial](../guides/standalone.md) or the
[local platform](../guides/local-platform.md) setup.

**Need to call a REST API?** You may not need a worker at all: the built-in
HTTP connector calls it [without code](../connectors/http-without-code.md).

## The task lifecycle

| Term | Meaning |
| --- | --- |
| Release | An immutable build identity (an image digest) and the capabilities it declares, admitted by a deployer |
| Instance | One registered running process, owned by the authenticated worker principal |
| Task | One external operation that a workflow's Call an action step prepares |
| Lease | Time-limited permission for one instance to attempt that task |
| Generation | The attempt number; recovery increments it so a stale attempt loses authority |
| Operation key | A stable idempotency key for the external system, the same across attempts of one task |
| Completion ID | Your identifier for one submitted outcome and its replayable receipt |
| Receipt | The platform's durable acknowledgment that it accepted that outcome |

The normal order is **admit release → register instance → claim → heartbeat
while working → complete or fail**. Registering creates no task. A claim
returns an empty list while no eligible work exists. When the handler needs a
secret, it asks for a separate, authorized credential lease.

![Admission, registration, claim, heartbeat, and outcome lifecycle](../diagrams/authoring-worker-lifecycle.svg)

Read down from admission and registration through claim, heartbeat, and outcome.
The dashed path at the bottom is recovery: it creates a new generation and
lease proof but keeps the operation key, so the external target must use that
key to avoid repeating an effect.
[Open diagram at full size](../diagrams/authoring-worker-lifecycle.svg).

## Authentication and endpoints

The [worker example](../../examples/worker/main.py) imports only the worker SDK
and an HTTP client. It receives no database or administrator credentials.

- A separately provisioned identity-provider client obtains an access token for
  the platform's API audience (`weave-api` in the local tutorial). The API
  verifies it like any other token and looks up the linked Weave principal.
- That principal needs the `worker` role in the environment. The role grants
  no authoring rights, and identity-provider roles alone grant nothing.

Every path below is relative to
`/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}`, and
every request needs a bearer access token plus a current grant in that scope.

| Operation | POST path | Body and result |
| --- | --- | --- |
| Admit an immutable release | `/worker-releases` | A deployer sends `image_digest`, the exact `capabilities`, and optionally `credential_capabilities` and trusted `connector_bindings`; returns the release ID |
| Register an instance | `/workers` | `release_id`, the exact `task_types`, and `capacity`; returns a worker ID owned by the authenticated principal |
| Claim | `/tasks/claim` | `worker_id` and `limit` (1 to 100); returns zero or more `TaskLease` values |
| Renew | `/tasks/heartbeat` | The current `LeaseProof`; returns the lease with its new expiry |
| Complete | `/tasks/complete` | `lease`, a unique `completion_id`, and an `output` that matches the schema; returns the committed receipt |
| Fail | `/tasks/fail` | `lease` and an `error` with `completion_id`, a bounded `code`, and an `outcome` (`not_started`, `failed`, or `unknown`) |
| Credentials | `/tasks/credentials` | A live `lease`, the bound `connection_revision_id`, and a `slot`; returns a short-lived lease with `Cache-Control: no-store` |

For example, after a deployer admits the example release, registration sends:

```json
{
  "release_id": "<the admitted release UUID>",
  "task_types": ["example-record@1.0.0"],
  "capacity": 1
}
```

Replace the placeholder with the release ID that admission returned. Save the
registration result's `id` as the worker ID; a claim body is then
`{"worker_id": "<that instance UUID>", "limit": 1}`. Never build a lease proof
yourself: send back the exact proof from the claim response for renewals and
outcomes.

**Keep the lease proof private.** `LeaseProof` holds the task ID, the
generation, the owner instance ID, and a secret token; never log it or store it
in ordinary telemetry. `TaskLease` also carries the immutable `input`, the
stable `operation_key`, the `capability`, the admitted `worker_release_id`, the
lease expiry (`expires_at`), and the absolute `deadline`. Built-in connectors
follow the same rules, but their invocation details are private to the platform.

## SDK runner and token lifetime

- **Runner.** Use `WorkerTransport` with an authenticated `httpx.AsyncClient`,
  and `Worker` with handlers keyed by exact `taskType@taskVersion`. The runner
  limits concurrency, renews leases, and cancels a handler when authority or
  the deadline is lost.
- **Shutdown.** SIGTERM asks the worker to drain. Supervise the process with an
  outer time limit for shutdown.
- **Tokens.** `WorkerTransport` does not renew tokens. The example acquires one
  token at startup and is deliberately finite: it stops claiming 210 seconds
  before the token expires, which reserves its 180-second task contract plus a
  30-second margin, and a final 15-second margin bounds the whole invocation. A
  token too short for that policy fails before any claim. A supervisor then
  starts a new invocation with fresh credentials. This policy fits the
  example's declared task timeout, not every handler.

## Retries and lost responses

**Delivery is at least once.** Pass `operation_key` to the external target, and
let the target keep its own durable idempotency receipt. The key stays the same
when recovery creates a new generation.

- A stale generation or proof cannot complete a newer attempt.
- Sending an accepted `completion_id` again with the same body replays its
  receipt; changing the output conflicts (HTTP 409, `WV-COMPLETION-CONFLICT`).
- A lost completion response is ambiguous. Do not run the handler again to
  compensate.

**Example.** The target records a customer, and the worker crashes before Weave
accepts the completion. Recovery issues generation 2 of the same task. Its
handler sends the same `operation_key`, so the target returns its original
receipt. Lease fencing stops generation 1 from advancing generation 2's work;
only the target's own deduplication prevents a duplicate customer.

**Capacity rejections are the only automatic retries.** HTTP 429 with exactly
`WV-REQUEST-CAPACITY` or `WV-OPERATION-CAPACITY` means the platform rejected
the request before admitting it:

| Request | What the SDK does |
| --- | --- |
| Claim | Returns no leases; the worker polls again later without cancelling active handlers |
| Heartbeat through `Worker` | Retries until the current lease expires, waiting 50, 100, and 200 ms, then 250 ms each time; renewal continues while completion awaits acknowledgment |
| Direct `WorkerTransport.heartbeat` | At most 3 attempts in total, waiting 50 and then 100 ms, within one second of the first rejection |
| Complete or fail through `Worker` | Retries within the task deadline while the owning worker's lease watchdog remains active, waiting 50, 100, and 200 ms, then 250 ms each time |
| Direct `WorkerTransport.complete` or `.fail` | At most 48 attempts within ten seconds of the first rejection, using the same backoff; the initial request uses the caller's HTTP timeout |

The lease and deadline watchdog can stop these retries sooner. Every retry
sends identical bytes (proof, completion ID, and payload), and the handler never
runs again. Any other 429, authorization errors, server errors, disconnects,
timeouts, and malformed successful answers fail without automatic retry.
Credential requests are never retried. These retries cannot resolve an
ambiguous outcome or renew expired authority.
The extended settlement window belongs to the exact lease proof and execution
task. The worker's renewal task owns a separate window that ends at the current
lease expiry. Unrelated calls and other child tasks retain the direct transport
limits. A successful heartbeat received before expiry can extend the current
lease, but never the task deadline.

**Effects and incidents.** Only effects declared safe are retried, within the
pinned attempt and deadline policy. An ambiguous non-idempotent effect raises an
incident instead. Weave does not promise exactly-once external effects. In the
local acceptance test, the target's in-memory receipt map survives API and
worker restarts; a production target must keep receipts for its whole replay
window.

**Credential leases need every authorization at once:** the release's explicit
opt-in, the administrator's release, connection, and capability grants, the
connection bound in the activation, a live lease, and the operator's grant from
the secret handle to its provider. Nothing a host or worker sends can choose a
secret-provider locator, a destination policy, or native executor authority.

**How the crash scenario is tested.** The crash launcher lives under
`tests/e2e/support/` and is copied only into its own test container. It records
a private lease proof after the receiver accepts the effect, then exits before
completion. Production source and images have no fault switch, and ordinary SDK
handlers have no such hook.

## Cancellation and suspended branches

**A failed branch cancels its siblings.** When a structured branch fails, the
platform cancels pending work and revokes the sibling leases already issued, in
the same run transaction.

- A valid proof for the exact generation that this control failure revoked, if
  it is still live, can submit one immutable `ignored` receipt. It records the
  delivery without accepting output or advancing the run. Identical retries
  replay it; a different payload fails.
- Expired attempts, forged proofs, unrelated stale generations, and timeout
  revocations do not get this exception. Heartbeats and credential requests
  stay denied after revocation.
- The SDK treats `ignored` as a committed receipt and never reruns the handler.

**A recoverable failure suspends the whole run.** Other attempts already in
flight can still record authenticated completion receipts before their expiry
and deadline. Their outputs wait until every blocking incident is resolved:
joins, new branch starts, and retry claims cannot cross this barrier. Resolving
one branch never clears a sibling's incident. Parallel concurrency counts every
started, unfinished direct branch, including nested work, signal waits, and
retries, and survives restarts.

## Classified output rejection

**Secret output is rejected, not stored.** When an ordinary output contains a
value for a field marked `x-secret` or `writeOnly`, the platform opens one
controlled incident and returns a `rejected` receipt.

- The receipt keeps the task, generation, completion ID, time, and
  `reason_code`; its `accepted_output_hash` is null.
- No raw output, response representation, submitted key, or digest of the raw
  output is stored. Built-in connectors use the same boundary, and a classified
  late output gets the same safe rejection without advancing a finished run.
- Every retry of that receipt, after the current authority and original proof
  ownership are checked, returns the same rejection, even with different
  content. It cannot become accepted, and the platform does not compare
  rejected payloads.
- A new valid result needs a new operation allowed by the incident and lease
  rules. Accepted `completed`, `failed`, and `ignored` receipts keep their exact
  payload comparison.
- The SDK treats `rejected` as a delivered outcome and never reruns the
  handler because its output was rejected.

**Older runs with uncertain secret handling are blocked.** Such runs cannot
issue new work, resume, lease credentials, or accept ordinary outcomes. Trusted,
bounded scanners record only immutable `run_policy_blocks` entries and fence
eligible attempts as `policy_blocked`, atomically.

- Candidate searches skip these entries before applying their limits, so later
  valid work keeps moving across restarts.
- Public reads and rejected requests never repair data. Blocks are never
  cleared automatically, grant nothing, and do not prove that historical data
  was repaired.
- A later cancellation cannot turn a policy fence into an `ignored` receipt.

## Historical receipts with unavailable payload evidence

After policy blocking, the original proof owner, with current authority, can
still replay an existing receipt for a task, generation, and completion ID:

- An earlier `completed`, `failed`, or `ignored` receipt returns
  `UnavailableCompletionReceipt`: the original identity, accepted time, and
  status, `unavailable: true`, `payload_match: "unavailable"`, and an explicit
  omission of `/accepted_output_hash`. It contains no output or hash.
- This acknowledges the historical receipt only. It neither accepts nor
  compares the newly submitted body; a changed body gets the same answer and is
  neither hashed nor stored.
- An earlier `rejected` receipt is already safe and returns unchanged, with its
  original reason.
- A new completion ID stays denied, and revoked grants, a revoked worker, or
  another owner still deny the replay. Receipts and events are never rewritten.

`CompletionAcknowledgment` is the public union of both receipt types. The SDK
keeps the concrete type and the `unavailable` flag, and completes delivery
without rerunning the handler.

## Troubleshooting

| What you see | Why | What to do |
| --- | --- | --- |
| Claims always return `[]` | No activated workflow has a task for this release and its task types yet, or all tasks are taken | Check that the activation pins this release and that a run reached the Call an action step |
| HTTP 401, `WV-UNAUTHENTICATED` | The worker's token expired, has the wrong audience, or its identity is not linked | Get a fresh token; ask an administrator to link the worker's identity |
| HTTP 403, `WV-FORBIDDEN` | The principal lacks the grant for this operation in this environment, or the worker ID belongs to another principal | Ask for the `worker` role; use a worker ID that this principal registered |
| HTTP 409, `WV-LEASE` | The lease expired, a newer generation exists, or the worker instance is unknown or revoked | Stop the handler and never resend with another proof; recovery issues a new attempt. After a revocation, register a new instance |
| HTTP 409, `WV-COMPLETION-CONFLICT` | A `completion_id` was reused with a different body | Resend the exact original body, or use a new completion ID only for a genuinely new outcome |
| HTTP 409, `WV-RELEASE-CONFLICT` | Admission reused an image digest with a different release request | Build a new image, or resend the exact original request, which returns the existing release |
| HTTP 429, `WV-REQUEST-CAPACITY` or `WV-OPERATION-CAPACITY` | The platform is at capacity | Nothing; the SDK retries within the limits above |
| A `rejected` receipt and a new incident | The output held a value for a field marked `x-secret` or `writeOnly` | Change the handler so it never returns those values, then resolve the incident; see [classified output rejection](#classified-output-rejection) |

## Next steps

- Build, deploy, and observe a worker with the [worker guide](../guides/workers.md).
- See how leases and retries fit the whole platform in
  [concepts](../concepts.md#tasks-leases-and-retries).
- Resolve the incidents that a failed task opens with
  [incident operations](incident-operations.md).
- Look up each worker operation's schema in the [full API reference](api-explorer.md).
