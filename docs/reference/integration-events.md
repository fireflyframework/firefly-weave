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

# Notify your application about workflow events

Weave can tell your application when something changes: a run moves to a new
status, a definition is published, or an activation is activated or retired. A
**subscription** selects which of these future events to send and to which fixed
HTTP destination. A **delivery** is the durable record of sending one event to
one subscription revision, with every attempt.

**Who it is for.** Integration developers who build the receiving endpoint, and
operators who keep deliveries healthy. This page covers outbound notifications
only; to bring business events into Weave, use
[signed webhooks](http-and-webhooks.md) or [provider sources](provider-sources.md).
**What you need.** A platform from the [standalone setup](../guides/standalone.md)
or the [local platform](../guides/local-platform.md), a receiving HTTPS endpoint
you control, and an operator who can grant a signing secret handle. Allow about
30 minutes.

![Outbound lifecycle transaction, outbox dispatcher, and durable receiver acknowledgment](../diagrams/integrations-directions.svg)

Follow the bottom row from the recorded change to the receiver's committed effect
and acknowledgment. The change and its delivery intent are stored in the same
transaction; the outbox dispatcher then sends a bounded attempt. The upper rows
are inbound routes with different acknowledgment rules.
[Open diagram at full size](../diagrams/integrations-directions.svg)

## What Weave sends

Events are delivered **at least once**. Each notification carries controlled
identity, status, sequence, revision, and emission metadata, and never business
input or output, source documents, free-form errors, your correlation strings, or
workflow evidence.

| Event type | Sent when | Scope |
| --- | --- | --- |
| `run.transition` | A run's accepted status changes | The run's environment |
| `definition.published` | A definition version is published | The project; the environment is `null` |
| `activation.activated` | A version is activated in an environment | That environment |
| `activation.retired` | An activation is retired | That environment |

The event's `correlation_id` is the stable UUID of the source resource. The
change, the immutable event, and every matching delivery intent commit in the
same transaction, so a failed append rolls back the change itself. Replaying a
change keeps its event ID. New subscriptions never receive historical events.

## Set up a subscription

Run the commands with a saved platform and workspace from `weave auth setup`
(see [how remote commands choose a platform](../guides/connect-to-api.md#how-remote-commands-choose-a-platform)).
Saved platforms are new in 0.1.0a7; with an alpha6 or earlier CLI, these
commands use [explicit mode](../guides/connect-to-api.md#scripts-and-ci-explicit-mode).

1. **Build the receiver first.** It must verify the signature and timestamp,
   store `eventId` durably to discard duplicates, commit its own effect, and only
   then return a configured success status. See
   [Receive and acknowledge](#receive-and-acknowledge).
   [`examples/idempotent_receiver.py`](../../examples/idempotent_receiver.py) shows
   a durable effect ledger for a different request shape; it does not implement
   this envelope or its HMAC, so it is not a drop-in receiver.

2. **Get a signing secret handle.** The operator grants a handle, such as
   `release-receiver-key`, to your environment in `WEAVE_SECRET_GRANTS`. Its value
   must be at least 16 bytes of high-entropy, destination-specific data that uses
   only the bearer-token alphabet `[A-Za-z0-9._~+/-]` (with optional trailing
   `=`). The same value authenticates the bearer request and the HMAC signature,
   so give your receiver the same value.

3. **Create an integration connection on `weave-http@1.0.0`.** Subscriptions send
   through the original HTTP connector, not the no-code `weave-http@2.0.0`. If
   `weave-http@1.0.0` is not yet published in your project, see
   [the original HTTP connector](http-and-webhooks.md#the-original-http-connector-weave-http100).
   Save this as `receiver-connection.json`, using the connector version ID of the
   published `weave-http@1.0.0` and your receiver's origin. Since 0.1.0a7,
   `weave connector descriptor weave-http --output json` shows that ID as
   `published_version_id`:

    ```json
    {
      "name": "release-receiver",
      "connector_version_id": "00000000-0000-4000-8000-000000000009",
      "config": {"baseUrl": "https://receiver.example.com", "auth": "bearer"},
      "secretRef": {"token": "release-receiver-key"},
      "allowed_destinations": ["https://receiver.example.com"]
    }
    ```

    ```sh
    # Create the bearer connection; the request names the handle, never the value.
    weave connections create --request receiver-connection.json --output json
    ```

    Expected: a connection revision. Save its `id` as the connection revision ID.
    A private destination also needs the operator's private-network policy.

4. **Create the subscription.** Save this as `subscription.json`, replacing the
   connection UUID:

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

    ```sh
    # Save the subscription; saving the same name later creates a new revision.
    weave subscriptions save --request subscription.json --output json
    ```

    Expected: your request plus `id`, `revision`, `binding_id`, `principal_id`,
    and `"active": true`. Save `id`, `revision`, and `binding_id`.

5. **Keep a dispatcher running.** A scheduler-enabled replica runs the outbox
   dispatcher. A native action worker does not send these notifications.

6. **Cause one matching event**, such as publishing a definition, then inspect the
   delivery:

    ```sh
    # List deliveries and find the one whose subscription_id matches yours.
    weave deliveries list --output json
    # Show every attempt for that delivery, with timing and outcome codes.
    weave deliveries history DELIVERY_ID --output json
    ```

    Expected: a delivery with status `delivered` once your receiver returned a
    configured success status. Confirm the business effect in the receiver's own
    store as a separate check.

## Configure a destination

The connection must be an admitted, immutable `weave-http` (v1) revision with a
reviewed origin, explicit allowed destinations, and an operator-approved
credential handle. The v1 contract provides one `token` slot with `auth: bearer`,
so `signing_slot` is `token`.

**Who may configure.** The configuring person needs `subscription.manage`,
`connection.manage`, `connection.bind` on that exact connection, and read
authority for the events: `run.read` for run events, `catalog.read` for catalog
events. Publication events need project-wide catalog authority; an
environment-only grant cannot export project metadata. Administration alone
grants no business read permission. A request cannot choose its principal or
carry secret bytes.

| Role | Can |
| --- | --- |
| `deployer` | Manage subscriptions (`subscription.manage`) |
| `operator` | Read deliveries (`delivery.read`) and retry them (`delivery.retry`) |
| `viewer` | Read deliveries (`delivery.read`) |

**Revisions.** Saving the same name creates a new immutable revision pinned to the
person who saved it. Only future events match the new revision; existing
deliveries keep their original connection, path, event types, owner, signing
handle, and statuses. Old revisions stay readable.

**Stopping deliveries.** Disabling a revision (`weave subscriptions disable`)
stops new matches. Revoking its connection source binding (`weave source-bindings
revoke`) also stops pending deliveries. A project can have at most 64 active
subscriptions; one more is rejected rather than silently dropping fan-out.

**Key rotation.** The value behind the handle may rotate. Each attempt resolves
the current bytes and records the provider's opaque version when there is one;
no value or digest is stored. Coordinate rotation with the receiver. The
environment-variable provider has no version, and Weave cannot retrieve old keys.

## Receive and acknowledge

**The body** is a stable JSON envelope with exactly `eventId` and `payload`. The
payload is a versioned `IntegrationEvent` (`version: weave/integration/v1`) whose
`event_id` equals `eventId`. The body and event ID stay identical across
attempts, and the delivery ID stays the same for one event and subscription
revision.

**The headers** are `Content-Type: application/json`, `Idempotency-Key` and
`X-Weave-Event-Id` (both equal to `eventId`), `X-Weave-Delivery-Id`,
`X-Weave-Timestamp`, `X-Weave-Signature`, and the bearer `Authorization`. Each
attempt uses a fresh integer Unix timestamp and a lowercase hexadecimal
HMAC-SHA256 over the ASCII timestamp, a period, and the exact body bytes: the
same scheme as [signed webhooks](http-and-webhooks.md#signed-ingress), so Weave's
own webhook verifier accepts this envelope.

**Your receiver must:**

1. Authenticate the timestamp and signature before parsing, and reject stale
   timestamps and disagreeing event IDs.
2. Store `eventId` durably and discard duplicates; an in-memory cache is not
   enough.
3. Commit its effect, then return one of the configured success statuses.

If the receiver commits and Weave stops before recording the acknowledgment, a
new attempt sends the same event again. Weave does not promise exactly-once
external effects.

**What counts as success.** Only a configured 2xx status acknowledges delivery.
Redirects are not followed, and there are no transport-level retries. The HTTP
transport checks the destination, DNS pin, TLS hostname, and peer address.
Response bytes are capped at 1 KiB, and response bodies, headers, and exception
details are never stored.

## Retry and inspect

A delivery's status is `pending`, `leased`, `retry`, `delivered`, or `incident`.

- **Automatic attempts.** Three in total. After a failed first attempt Weave waits
  5 seconds; after later ones, 30 seconds. An expired lease is recorded as
  `ACK_UNKNOWN` before the next attempt. Attempts accumulate and are never
  erased.
- **Incidents.** When attempts run out or authority is revoked, the delivery
  becomes an operations incident with a controlled code and an audit record. It
  does not create or suspend a run.
- **Manual retry.** `weave deliveries retry DELIVERY_ID`
  (`POST .../deliveries/{identifier}/retry`) grants exactly one more attempt and
  keeps every ID and pin. Concurrent retries cannot add more than one. Retry
  rechecks the original owner's current authority and cannot restore a revoked
  binding. Audit records name the person who retried; automatic incidents name the
  pinned configuring principal and the outbox dispatcher.

`weave deliveries list` and `read` show bounded metadata, and
`weave deliveries history ID` (`GET .../deliveries/{identifier}/attempts`) shows
attempts with provider versions. The full CLI families are `weave subscriptions`
(`save`, `list`, `read`, `disable`) and `weave deliveries` (`list`, `read`,
`history`, `retry`). The SDK methods are `save_subscription`, `read_subscription`,
`list_subscriptions`, `disable_subscription`, `read_delivery`, `list_deliveries`,
`delivery_attempts`, and `retry_delivery`. Native OpenAPI describes the same
operations.

## How the outbox dispatcher works

These details matter when you size replicas or investigate slow deliveries.

**Scheduling.** A separate loop owned by the API process polls every second, using
execute-only scheduler catalog authority and durable tenant and environment
cursors. Recovery and schedule loops keep their own pace. API-only replicas
disable the scheduler. No in-memory queue owns pending deliveries.

**Capacity.** At most four sends are in flight per process. Capacity is reserved
before a cursor advances or a delivery is claimed, so a full process does not
consume another turn. One dispatch call examines at most ten candidates and
claims no more than the free send slots; the automatic loop makes one claim
decision per environment turn. Selection filters due work before `LIMIT`, uses
`SKIP LOCKED`, and leaves incidents out of ordinary scanning.

**Time budget.** A lease lasts 30 seconds. One attempt has a 15-second deadline
covering credential resolution, authority checks, HTTP, and settlement; credential
resolution may take at most 5 seconds and HTTP at most 5 seconds, leaving time to
settle. Slow receivers are called outside database transactions.

**Credential resolution.** Four unfinished standing-source credential resolutions
are allowed per process, shared by Kafka, outbox, and provider inbox bindings;
worker credentials use a separate path. A cancelled resolution keeps its slot
until the provider's function actually returns. A provider that never returns can
hold a slot indefinitely, but it cannot create an unbounded queue or authorize a
stale send; process shutdown cannot interrupt arbitrary synchronous provider code.

**Authority and fencing.** Current source owner grants, destination grants, the
binding fingerprint, and the lease are checked before and after credential
resolution, and again just before sending. A revoked binding cannot authorize
another request, but revocation cannot recall bytes already sent. Every
acknowledgment is fenced by its token and database-time expiry. Shutdown stops
claiming and cancels active network work before closing database resources;
unfinished leases remain recoverable.

**Locks.** Source, receipt, and run rows are locked first, then the project
fan-out advisory lock. Saving a subscription takes that project lock and creates
a fresh outbox binding, never touching an existing source binding or runtime row.
Dispatch locks the delivery, then the binding, without the project lock; revoking
a binding locks only that binding. Source events and subscription revisions are
append-only for the application role, protected by composite scope keys and forced
row-level security.

**Migration.** Forward migration `0017_outbox` follows `0016_broker_receipts`;
earlier migrations are unchanged. The pure compiler and runtime kernel remain
offline.

## Next steps

- [Integrate your product](../guides/host-integration.md): use these events in an
  application.
- [Call HTTP APIs and accept signed webhooks](http-and-webhooks.md): the inbound
  direction and the HMAC scheme.
- [Observability](../operations/observability.md): watch delivery health.

## Troubleshooting

| What you see | Why | What to do |
| --- | --- | --- |
| `subscriptions save` fails with `WV-DELIVERY-CONFIG` | The connection is not a `weave-http` v1 revision, `signing_slot` is not one of its secret slots, or its `baseUrl` origin is missing from `allowed_destinations` | Create a v1 bearer connection whose `allowed_destinations` include the `baseUrl` origin, and use `"signing_slot": "token"` |
| No delivery appears | The event type does not match, the event happened before the subscription, or you lack read authority for it | Compare `event_types`, the subscription revision, and your `run.read` or `catalog.read` grant |
| Deliveries stay `pending` | No scheduler-enabled replica is running, or the database is not ready | Start a replica with the scheduler enabled; check readiness |
| Status `retry` | The receiver did not return a configured success status | Check the receiver's logs and its status code |
| Status `incident` | Attempts ran out, or authority or the binding was revoked | Read the `code` and `weave deliveries history`, fix the cause, then `weave deliveries retry` |
| The receiver sees the same event twice | Delivery is at least once | Deduplicate durably on `eventId` |
| The receiver rejects the signature | The receiver's key, clock, or body handling differs | Verify over the exact raw bytes with the same key, and allow for clock skew |
