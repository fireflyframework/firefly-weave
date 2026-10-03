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

# Publish to Kafka and start workflows from Kafka records

Use this guide to publish workflow data to a Kafka topic, or to start a workflow
(or signal a waiting run) from Kafka records. The built-in `weave-kafka@1.0.0`
connector handles both directions. Consumed records get durable receipts, so a
redelivered record reuses its original run instead of starting a second one.

- **Who it is for:** the operator who installs the Kafka support and pins broker
  routes, the broker operator who supplies the cluster facts, and the author who
  publishes Actions and creates triggers.
- **What you need:** an existing broker and an operator-approved topic, a deployed
  platform built with Kafka support (the [local platform](../guides/local-platform.md)
  cannot run it), and a CLI [connected to that platform](../guides/connect-to-api.md).
  Local protocol tests do not verify your broker's access or network
  configuration.
- **How long:** about an hour for a first route, most of it waiting for broker
  facts and an operator restart.

![Kafka record receipt and runtime commit precede manual offset commit](../diagrams/integrations-kafka-receipts.svg)

**How to read this diagram:** Read downward in time. The gap between the database commit and offset commit explains why redelivery must reuse a durable receipt. The two crash cases apply to consumption; publish acknowledgment has its own unknown-outcome rules below.

[Open diagram at full size](../diagrams/integrations-kafka-receipts.svg)

## Choose publish, consume, or both

| Direction | What you create | What it does |
| --- | --- | --- |
| Publish | A **publish Action** used by a workflow step | Sends the step's JSON object to one configured topic |
| Consume | A **broker trigger** | Reads topic records and starts a pinned workflow activation, or signals one existing run |

Both directions share a connection policy but use different execution authority.
Creating a Weave connection does not provision a Kafka cluster or grant broker
ACLs.

## Before you start

| You need | Who provides it |
| --- | --- |
| The cluster identity, topics, advertised broker and coordinator endpoints, the TLS CA, and the permitted numeric addresses | The broker operator |
| A SASL user name and its password behind a secret handle | The broker operator and the Weave operator |
| A server built with Kafka support and a broker policy | The Weave operator |
| The publication sequence: Connector, release, grants, activation | You and the operator; see [From package to an executable workflow](authoring.md#from-package-to-an-executable-workflow) and [worker admission](../guides/workers.md) |

**Install the Kafka support.** The broker family supports Kafka only. Install
`firefly-weave[server,kafka]`, or build the Docker target `kafka-server`, and
enable `WEAVE_BROKER_POLICY`. The locked driver is aiokafka 0.14.0 on CPython
3.12 or 3.13, Linux or macOS, with an owned standard selector loop. Core startup
without the Kafka extra does not import the driver; an enabled but missing or
incompatible driver fails startup. The `weave-kafka` descriptor is registered only
when the broker policy is enabled.

## Provision a connection and its routes

1. **Publish the Connector and an Action, and register the release.** Publish the
    trusted `weave-kafka@1.0.0` descriptor, register its worker release, and
    publish an Action that uses its `publish` action.

2. **Create the connection revision.** Use the published Connector version `id`
    in this request. The names are synthetic; get the actual endpoints and topic
    ACLs from your broker operator.

    ```json
    {
      "name": "orders-kafka",
      "connector_version_id": "00000000-0000-4000-8000-000000000003",
      "config": {
        "driver": "kafka", "cluster_id": "orders-cluster",
        "bootstrap": ["kafka://broker.example:9093"], "topics": ["orders"],
        "security_protocol": "SASL_SSL", "sasl_mechanism": "PLAIN", "username": "weave-orders"
      },
      "secretRef": {"password": "orders-kafka-password"},
      "allowed_destinations": ["kafka://broker.example:9093"]
    }
    ```

    ```sh
    # Create the connection revision from the reviewed request file.
    weave connections create --request orders-kafka.json --output json
    ```

    Expected: a connection revision with an `id`. Save it: route pins and
    triggers use this **connection revision ID**. Creating a revision does not
    open a broker connection.

    `allowed_destinations` must list every advertised broker and coordinator. This
    one-broker list is complete only when the bootstrap, advertised broker, and
    coordinator all use that endpoint. A bootstrap address alone does not
    authorize newly discovered brokers.

3. **Pin the routes and restart (operator).** Route pins are infrastructure
    configuration, not tenant-controlled connection fields. Set
    `WEAVE_BROKER_POLICY` to a JSON object like this one, using the actual
    returned connection revision UUID and verified operator addresses, then
    restart the replicas that publish or consume. Provision the CA file in the
    deployed image or trusted deployment configuration.

    ```json
    {
      "enabled": true,
      "max_clients": 8,
      "routes": [{
        "connection_revision_id": "00000000-0000-4000-8000-000000000001",
        "advertised": "kafka://broker.example:9093",
        "addresses": ["192.0.2.10"],
        "ca_file": "/etc/weave/kafka-ca.pem"
      }]
    }
    ```

    Add one route per advertised destination. Each route lists one to four numeric
    addresses.

4. **Publish a workflow that publishes.** Give the workflow a connection slot and
    activate it with the connection revision and connector release pins. The
    publish Action's configuration is `{"topic":"orders"}`. **The Action input
    object itself is the JSON wire value**: input `{"order_id":"123"}` publishes
    that object, with no payload wrapper. The capability is
    `weave-connector-kafka-publish@1.0.0` and its side effect is
    `non_idempotent`.

5. **Create a broker trigger to consume.** See
    [Start a workflow from a topic](#start-a-workflow-from-a-topic).

6. **Enable consumers (operator).** Consumer replicas set
    `WEAVE_KAFKA_CONSUMER_ENABLED=true` and supply the existing execute-only
    scheduler database URL. API and publish-only replicas leave it `false` and can
    still create durable routes for consumer replicas. `max_clients=1` is
    publish-only; background consumption with that value fails startup.

### Security requirements

- **Production.** Verified TLS and `SASL_SSL` with `PLAIN`, `SCRAM-SHA-256`, or
  `SCRAM-SHA-512`. The user name is configuration; the password is the scoped
  `password` secret reference.
- **Dialing.** Every dial uses the advertised host name for TLS verification and
  SNI, and the operator's numeric address for TCP. Unexpected advertised
  endpoints are denied before dialing or sending credentials.
- **Not supported.** Runtime DNS, arbitrary proxies, OAuth callbacks, GSSAPI,
  client-certificate authentication, and dynamically discovered destination
  permission.
- **Development only.** `PLAINTEXT` additionally requires each operator pin's
  `plaintext: true` and loopback addresses. The isolated `compose.kafka.yaml`
  profile requires an explicitly pinned image and a dedicated port.

## Start a workflow from a topic

A broker trigger pins the connection revision, cluster, topic, run activation or
signal target, payload schema, and an explicit `dead_letter_policy` (`receipt` or
`halt`). The configuring principal must currently hold `trigger.manage`, the
target's `run.start` or `run.signal`, `connection.bind`, and `connection.manage`.
A connection-owned source binding is created atomically with the route.

This complete run-trigger request belongs at
`POST /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/broker-triggers`:

```json
{
  "name": "order-events", "driver": "kafka",
  "connection_revision_id": "00000000-0000-4000-8000-000000000001",
  "cluster_id": "orders-cluster", "topic": "orders", "kind": "run",
  "activation_id": "00000000-0000-4000-8000-000000000002",
  "payload_schema": {
    "type": "object", "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"], "additionalProperties": false
  },
  "dead_letter_policy": "halt"
}
```

```sh
# Create the trigger in the workspace of your saved platform.
weave broker-triggers create --request order-events.json
```

Replace the UUIDs with the connection revision ID and the workflow activation ID.
The target workflow's input schema must accept this payload. A signal trigger
uses `"kind": "signal"` with `run_id` and `signal` instead of `activation_id`.

Expected: a trigger with its `id` and `binding_id`; save both. A record whose value
is `{"order_id":"123"}` and that carries exactly one `weave-event-id` UUID header
then starts one run. Arbitrary existing Kafka records are not automatically valid
Weave events; see [Wire metadata](#bounds-recovery-and-operational-limits).

**Manage routes and bindings.** `weave broker-triggers` also reads, lists,
disables, and retries routes and lists metadata-only incidents. Revoke a source
binding with `weave source-bindings revoke`. The same operations exist in the API
and the typed SDK.

## Know what success looks like

- **Publish.** A successful publish returns `topic`, `partition`, `offset`, and
  `event_id`.
- **Consume.** Consumer acceptance records a receipt containing `run_id`, and
  `signal_id` for a signal target.
- **Connection test.** `weave connections test` deliberately reports failure for
  Kafka: a metadata-only test has no standing or worker authority to create a
  credential-bearing client. Use an authorized publish or trigger for a live
  test.

## Troubleshoot

| What you see | Why | What to do |
| --- | --- | --- |
| Nothing starts from new records | Consumers are disabled, the scheduler identity is missing, a route pin is missing, or broker ACLs deny access | Check `WEAVE_KAFKA_CONSUMER_ENABLED`, the scheduler database URL, the operator route pins, and the broker ACLs, then the trigger incidents |
| A route is blocked | `halt` stopped at a rejected record | Fix the source record or policy, then retry with authority; retry does not skip the record |
| A publish ends `unknown` | The acknowledgment was lost after sending | Reconcile with [incident operations](../reference/incident-operations.md) before publishing again |
| Startup fails after enabling the broker | The Kafka driver is missing or incompatible, or `max_clients=1` with consumers enabled | Install `firefly-weave[server,kafka]` on a supported platform; raise `max_clients` |
| The connection test fails | It always does for Kafka | Use an authorized publish or trigger instead |

## Delivery and failure semantics

**Publishing.** Publishing requires current worker lease, release, and connection
authority. Credentials are resolved under that authority. Checks happen before and
after provider I/O, before sending, every second while awaiting acknowledgment,
and after acknowledgment. The producer uses `acks=all`, producer-session
idempotence, and a stable UUID event header derived from the operation key. These
do not deduplicate effects across client lifetimes. A lost acknowledgment
produces an **unknown** native task outcome and an incident, with no hidden
new-client retry. Already-sent effects cannot be recalled after revocation.

**Consuming.** Consumers use manual commits. One database transaction records the
accepted transport receipt and starts the run or delivers its signal. Only after
that transaction commits may the accepted offset prefix advance:

- a crash before the database commit replays without a partial run;
- a crash after the database commit but before the Kafka commit replays the same
  receipt and run.

This is local durable deduplication with at-least-once broker delivery, not an
atomic database and Kafka transaction.

**Identity.** Transport identity includes the tenant, project, trigger, configured
cluster, topic, partition, and offset. Semantic identity is the valid event UUID
within the source. The same event and admitted payload at a new offset references
the original run; a changed payload is rejected. Rejected records have a typed
rejection receipt and incident, never an invented run ID. Invalid,
secret-classified, or conflicting rejected content is omitted from persisted
evidence, including raw payload hashes and unsafe event identifiers. Rejected
events do not reserve semantic identity. Current authority is checked even for
receipt replay.

**Rejected records.** `receipt` durably records the rejection before allowing the
offset to advance. `halt` durably blocks the route and leaves the offset
uncommitted. An explicit authorized retry keeps the route pins and the original
offset; it does not skip the poison record, which blocks again if still invalid.
Disabling the trigger and revoking the binding prevent new admission. Operators can
inspect the metadata-only incident and fix the source or provision a replacement
route through normal authorization.

## Bounds, recovery, and operational limits

| Boundary | Limit or behavior |
| --- | --- |
| JSON value | 1,048,576 canonical serialized bytes; publish input must be an object; admitted consumer values may be any valid JSON |
| Wire metadata | No key; exactly one `weave-event-id` header containing a canonical 36-byte UUID; no extra headers or compression |
| Producer | 30-second maximum send window, with cleanup plus one second reserved inside the Action deadline for reporting; 5-second requests, 1 MiB + 4,096 request bytes, 1 MiB + 1,024 batch bytes |
| Broker configuration | Permit value plus record and request overhead; the supplied fixture uses 1 MiB + 4,096 message and fetch allowance |
| Scope capacity | 64 active routes per environment; 16 bootstrap endpoints and 64 topics per connection |
| Local and process capacity | At most 8 client owners process-wide; one publication slot reserved, at most 7 consumers; lower per-app caps also apply |
| Consumer delivery | At most 32 fetched records per batch and 32 assigned partitions; contiguous delivered-prefix commit frontier |
| Credentials and client lifetime | At most 60 seconds; opaque provider-version ownership; no cross-lifetime credential cache |
| Discovery | Independent durable tenant and environment cursors, one scoped route admission per turn, least-recent admission ordering |
| Claim and retry | 65-second database claim, no indefinite renewal; 60-second consumer turn; 5-second failed-start backoff |
| Revocation | Current authority checked at credential, fetch, receipt, and commit boundaries and each second while idle; checks bounded to 5 seconds |
| Shutdown and rebalance | 5-second drain and cleanup budgets; admission closes and transports abort before a graceful producer stop |

Discovery re-reads durable routes after a restart; in-memory clients are not
configuration authority. Replicas contend on database claims. Kafka assignment
generation independently fences offset commits after a rebalance. A database
check and a later broker commit cannot be atomically fenced across these systems.
Fairness depends on finite eligible routes, available databases, and cooperative
cleanup; it is not a latency SLA. Periodic client retirement causes rebalances and
a bounded credential rotation delay.

Kafka fetch limits are soft protocol limits: brokers may return a larger first
batch. Oversized values are rejected on admission, but this is not a hard
process-memory bound against a malicious broker. Python cannot forcibly kill a
noncooperative thread; stuck owners remain counted rather than allowing unbounded
replacements. Driver logs from owned threads are reduced to controlled codes,
including exception context; unrelated applications' threads keep normal
logging. This reduces low-level diagnostics and avoids leaking SASL user names,
secrets, or broker-controlled errors.

## What has been verified

Local integration tests exercise SASL `PLAIN` over verified TLS. `SCRAM`
configuration follows the driver contract but has not been independently verified
against a live authenticated broker in this release.

## Next steps

- Branch on the event payload or wait for a follow-up signal:
  [Author workflows](../guides/workflow-authoring.md).
- Receive events over HTTP instead: [signed webhooks](../reference/http-and-webhooks.md#signed-ingress).
- Notify external systems about run changes: [integration events](../reference/integration-events.md).
