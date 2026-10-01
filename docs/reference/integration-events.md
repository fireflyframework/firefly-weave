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

# Outbound integration events

![Outbound lifecycle transaction, outbox dispatcher, and durable receiver acknowledgment](../diagrams/integrations-directions.svg)

**How to read this diagram:** Follow the bottom row from the recorded lifecycle change to the receiver’s committed effect and acknowledgment. The source transaction and outbox attempt are described in the left card. The upper rows are inbound contracts with different receipt semantics.

## Send lifecycle notifications to your application

A **subscription** selects future Weave lifecycle events and a fixed HTTP
destination. A **delivery** is the durable attempt record for one event and one
subscription revision. This is outbound notification; to receive business events
into Weave, use [signed webhooks](http-and-webhooks.md) or
[provider sources](provider-sources.md).

After [standalone setup](../guides/standalone.md), configure your receiving
application to authenticate and durably deduplicate the envelope described below.
[`examples/idempotent_receiver.py`](../../examples/idempotent_receiver.py) demonstrates
a durable effect ledger for a different request shape. It does not implement
this integration-event envelope or HMAC authentication; it is not a drop-in
subscription receiver.
Then perform these steps:

1. Publish the installed HTTP v1 Connector and create an environment connection
   to your receiver with `auth: bearer` and `secretRef.token` pointing to the
   operator-granted signing handle. Retain the connection response's revision `id`.
2. Create the subscription with the complete body below, replacing its connection
   UUID. Save the returned subscription `id`, `revision`, and `binding_id`.
3. Keep a scheduler-enabled replica running the outbox dispatcher. A native Action
   worker is not the sender for these notifications; the outbox owns delivery.
4. Perform one authorized operation matching `event_types` after subscription
   creation. Existing historical events do not backfill.
5. List deliveries, identify the returned `subscription_id`, and read its attempts.
   `delivered` means the receiver returned a configured success status. Confirm
   the business effect in the receiver's durable store as a separate check.

If no delivery exists, compare the event filter, subscription revision, and source
read authority. If it remains pending, inspect scheduler/database readiness. If
it reaches `incident`, inspect the fixed `code` and attempt history, correct the
receiver or authority problem, then request an explicit retry. Retry preserves
IDs, so the receiver must continue deduplicating. It cannot restore a revoked
standing connection binding. See [host integration](../guides/host-integration.md)
for using these events in a product and the exact authority rules below.


Weave sends **at least once** notifications for accepted run transitions,
definition publication, and environment activation or retirement. The notification
contains controlled identity, status, sequence, revision, and emission metadata.
It never contains business input/output, source documents, freeform errors,
user-supplied correlation strings, or workflow evidence. The correlation ID is the
stable source resource UUID. Publication has project scope and a null environment;
run and activation events have their exact source environment.

Publication, activation, accepted runtime transitions, the immutable integration
event, and all matching delivery intents commit in the same transaction.
Replaying a source mutation retains its logical event ID. New subscriptions do
not replay historical events. An append failure rolls back the source mutation.

## Configure a destination

Use an admitted immutable `weave-http` connection revision with a reviewed origin,
explicit allowed destinations, and an operator-approved credential reference.
The current HTTP contract provides a `token` reference with `auth: bearer`;
select `signing_slot: token`. Its current bytes authenticate both the bearer
request and the HMAC signature. Use a destination-specific high-entropy key of
at least 16 bytes, with the HTTP bearer-safe alphabet. Private destinations also
require the operator's explicit private-network policy.

`POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/subscriptions`
accepts this body:

```json
{
  "name": "release-notifications",
  "connection_revision_id": "00000000-0000-4000-8000-000000000001",
  "signing_slot": "token",
  "path": "/weave/events",
  "event_types": ["definition.published", "activation.activated", "activation.retired"],
  "statuses": [200, 202, 204]
}
```

The configuring principal needs `subscription.manage`, `connection.manage`, exact
destination `connection.bind`, and source read authority: `run.read` for run events
or `catalog.read` for catalog events. Project publication requires project-wide
catalog authority; an environment-only grant cannot export project metadata.
Administration alone supplies no business read permission. A subscription request
cannot choose its principal or supply secret bytes. Metadata inspection needs
`delivery.read`; the viewer and operator roles have it. The operator has
`delivery.retry`; the deployer has `subscription.manage`.

Saving the same name creates a new immutable revision pinned to the modifier.
Only future events match that revision. Existing deliveries retain their original
connection, path, event filters, owner, signing reference, and success statuses.
Disabling a revision stops new matching; revoking its connection source binding
also stops pending delivery. Use the existing connection-source-binding revoke
operation to withdraw that exact standing authorization. Old revisions remain
inspectable. The admission cap is 64 active subscriptions per project; exceeding
it is rejected rather than silently truncating fan-out.

The underlying key may rotate behind its pinned handle. Each attempt resolves
current bytes and records the provider's opaque version when available; no value
or value digest is stored. Coordinate rotation with the receiver. Environment
providers may have no version. Weave does not offer historical key retrieval or
claim immutable credential bytes.

## Receive and acknowledge

The body is a stable JSON envelope with exactly `eventId` and `payload`. The
payload is a versioned `IntegrationEvent`; its `event_id` equals `eventId`.
The event ID and body remain identical across attempts. The delivery ID remains
stable for that event/subscription revision pair.

Headers include `Content-Type: application/json`, `Idempotency-Key`,
`X-Weave-Event-Id`, `X-Weave-Delivery-Id`, `X-Weave-Timestamp`, and
`X-Weave-Signature`. Each attempt computes a fresh integer Unix timestamp and
hexadecimal HMAC-SHA256 over ASCII timestamp, a period, and the exact transmitted
body bytes. Authenticate the timestamp and signature before parsing. Reject stale
timestamps and disagreeing event identities. The existing Weave webhook verifier
accepts this envelope and signing scheme.

The receiver must durably deduplicate `eventId` and commit its effect before
returning a configured successful status. Weave does not promise exactly-once
external effects. If the receiver commits and Weave crashes before recording the
acknowledgment, a new leased attempt sends the same event again. A receiver's
in-memory cache is insufficient for durable deduplication.

Only the pinned configured 2xx status acknowledges delivery. Redirects are not
followed, and there are no transport retries. The existing bounded HTTP transport
checks destination, DNS pin, TLS hostname, and peer address. Response bytes are
bounded to 1 KiB; response bodies, headers, and exception details are never stored.

## Retry and inspect

Delivery status is `pending`, `leased`, `retry`, `delivered`, or `incident`.
There are three automatic attempts. Failed acknowledgments wait 5 seconds after
the first attempt and 30 seconds after subsequent attempts. An expired lease is
recorded as `ACK_UNKNOWN` before another attempt. Attempts are cumulative and
never erased. Exhaustion or revoked authority creates an operations-owned delivery
incident with a controlled code and audit; it does not create or suspend a run.

Use the `deliveries` list/read routes and
`GET .../deliveries/{identifier}/attempts` for bounded metadata and provider-version
history. `POST .../deliveries/{identifier}/retry` grants exactly one additional
attempt, preserving all IDs and pins. Concurrent retries cannot add multiple
budgets. Retry rechecks the original owner's current authority and cannot restore
a revoked binding. Audit records identify the retry operator; automatic incidents
identify the pinned configuration principal and the outbox dispatcher initiator.

The typed SDK exposes `save_subscription`, `read_subscription`,
`list_subscriptions`, `disable_subscription`, `read_delivery`, `list_deliveries`,
`delivery_attempts`, and `retry_delivery`. CLI families are `weave subscriptions`
and `weave deliveries`, with the same operation names and generated request-body,
paging, JSON, and error behavior. Native OpenAPI describes the same DTOs/routes.

## Scheduling and transaction ownership

A separate lifespan-owned outbox loop polls every second through execute-only
scheduler catalog authority and durable tenant/environment cursors. Recovery and
schedule loops retain their own quanta. API-only replicas explicitly disable the
scheduler. No memory queue owns pending deliveries.

At most four sends are in flight per process across application instances.
Capacity is reserved before cursor advancement or durable claim; a full process
does not consume another discovery turn. A dispatch call examines at most ten
candidates and claims no more than available send slots. The automatic loop uses
one claim decision per environment turn. Selection filters due work before LIMIT,
uses `SKIP LOCKED`, and removes incidents from ordinary scanning.

A lease lasts 30 seconds. The attempt has one cooperative 15-second deadline,
including credential resolution, current-authority checks, HTTP, and fenced
settlement. Provider resolution has a nested maximum of 5 seconds; HTTP has at
most 5 seconds and reserves remaining settlement time. Slow receiver I/O occurs
outside database transactions. Four process-wide unfinished standing-source
provider resolutions are allowed (Kafka, outbox, and provider inbox bindings);
worker credential resolution is a separate path. Cancellation keeps a provider's
slot until its actual synchronous function exits. Each accepted resolution owns
one thread; a startup gate prevents provider entry when thread creation or
startup fails, and rejected starts release their reservation without queued work. A non-cooperative provider can occupy a slot indefinitely, but
cannot create an unbounded queue or authorize a stale send. Process shutdown
cannot forcibly interrupt arbitrary synchronous provider code.

Current source owner/grants, destination grants, binding fingerprint and lease
are checked before and after provider I/O, then immediately before send. A revoked
binding cannot authorize another request, but revocation cannot recall bytes
already sent. Confirmed acceptance may settle under its still-live lease.
Every acknowledgment is fenced by token and database-time expiry. Shutdown stops
claiming and cancels active network work before closing database resources;
unfinished durable leases remain recoverable.

Lock order is authoritative source/receipt/run rows, then the project fan-out
advisory lock. Subscription configuration takes that project lock and connection
creation of a fresh outbox binding, never an existing source binding or runtime/
delivery row. Inbound Kafka already checks its existing binding before runtime
admission and the project lock; configuration does not acquire that binding in
reverse order. Dispatch takes delivery then binding locks, without a project lock;
binding revoke takes only its binding row. Immutable source events and
subscription revisions are append-only to the application database role; composite
scope keys, the project-event/destination check, and FORCE RLS protect relationships.

Apply forward migration `0017_outbox`, parent `0016_broker_receipts`. Existing
migrations are unchanged. Source transactions use the injected operations append
service; transaction-local runtime repositories require that port before persisting
an authoritative transition. The pure compiler and runtime kernel remain offline.
