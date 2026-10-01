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

# WhatsApp Cloud API

## What the integration builds

There are two independent paths: authenticated webhook events start/signal a
Workflow; `send-text` or `send-template` Actions send to an explicitly allowed
recipient. Delivery-status webhooks report later provider facts. An outbound
`accepted` result is not a delivery-status result.

Complete [standalone setup](../guides/standalone.md) and
[native worker admission](../guides/workers.md). Use the
[publication and activation sequence](authoring.md#from-package-to-an-executable-workflow)
for Weave IDs. The operator setup below covers the separate provider assets.

| Connection field | Required source of the value |
| --- | --- |
| `connector_version_id` | Published `weave-whatsapp` Connector response `id` |
| `app_id` | Operator's Meta application |
| `account_id` | WhatsApp Business Account (WABA) identity |
| `phone_number_id` | Registered business phone asset ID, distinct from its phone number |
| `business_phone_number` | That asset's canonical digit-only phone number |
| `graph_version` | Version independently reviewed for this deployment; fixture value is provisional |
| `recipient_allowlist` | Explicitly authorized digit-only recipient numbers |
| `approved_templates` | Account owner's reviewed name, locale and body-parameter count |

[`connection.json`](../../examples/connectors/whatsapp/connection.json) is the
complete creation-request shape, including all three distinct secret handles.
Copy it into an operator-reviewed working file, replace its synthetic IDs and
allowlist, and create the connection through the environment API. Save the
returned revision `id`. No script here provisions provider assets.

For inbound processing, publish/activate a Workflow accepting the complete
[`event-target.schema.json`](../../examples/connectors/whatsapp/event-target.schema.json).
Branch on `event_type` before treating `text` as a message: status events have null
text. Create the source using [the source request recipe](../reference/provider-sources.md#create-one-inbound-route),
selecting the WhatsApp declaration/provider and copying this exact connection config.
Save its `id` before configuring the provider callback.

For outbound processing, publish Actions selecting `send-text` or `send-template`
with `config: {}`, the installed descriptor schemas, `sideEffect: non_idempotent`,
and the descriptor timeout. Bind the connection and admitted release at activation.
These are complete **Action inputs**, not API requests to start a run:

```json
{"recipient":"15550000001","text":"Appointment reminder"}
```

```json
{"recipient":"15550000001","name":"appointment","locale":"en_US","parameters":["Tuesday"]}
```

Expected successful output has the shape
`{"message_id":"PROVIDER_MESSAGE_ID","acceptance":"accepted"}`. Save that message
ID for status inspection below. If a webhook is acknowledged but no reply occurs,
inspect its provider receipt and linked run separately. If sending ends in an
unknown outcome, use [incident reconciliation](../reference/incident-operations.md)
before another send. A missing status fact alone does not prove no send occurred.


This connector admits authenticated incoming text and delivery-status facts and
sends text or configured approved templates. It uses native provider sources,
receipts, current connection authority and the existing operation ledger.

The checked-in profile and fixtures establish offline wire behavior. They do not
certify current Meta account eligibility, template approval, commercial policy or
Graph-version compatibility. No real message, account or subscription was used.
Teams remains the first provider delivery in this release.

## Operator setup

1. Provision your Meta app, WABA and registered business phone outside Weave using
   current Meta instructions. Confirm webhook subscriptions and asset permissions
   with the account owner. Sending requires an appropriate access token; the
   historical Meta collection names `whatsapp_business_messaging`. Recheck current
   requirements, customer-service window and template rules before live use.
2. Configure distinct secret handles for `accessToken`, `appSecret`, `verifyToken`.
   The verification token is operator-selected webhook verification material; it
   is not an app secret or access token. GET resolves only verifyToken, POST only
   appSecret, and sends only accessToken. Each resolution remains subject to
   current scope, source, binding and connection authority.
3. Select the exact installed native declaration in `connector_packages`:
   `firefly-weave:weave-whatsapp:firefly_weave.connectors.whatsapp:package`.
   There is no Meta SDK extra. Disabled providers are not loaded by core scanning.
4. Publish the selected package's Connector manifest, create a connection revision
   using the example profile, and create a provider source against an admitted
   activation or waiting signal. Use the package's actual distribution version,
   adapter version and shared provider schema digest; do not substitute one for
   another. The source policy must equal the immutable connection configuration.
5. Configure Meta's callback as `/provider-ingress/{source_id}` on your public
   HTTPS ingress, with the configured verification token. Weave echoes a valid
   GET challenge and admits authenticated POSTs only after the entire transaction
   commits. Do not include query strings or auth headers in reverse-proxy/access
   logs. Weave's native request logger uses the URL path, not challenge query data.
6. Before live acceptance, verify current normative Meta documentation for the
   selected Graph version and account profile, then use explicitly authorized
   test recipients. `test_connection` currently reports failed without network
   access because remote asset/token validation is unsupported. Creation validates
   the local configuration; a token's presence proves no provider-side access.

Graph version is mandatory and fixed per connection. `v26.0` in synthetic examples
is provisional; no current WhatsApp compatibility or lifecycle date is asserted.
Only `https://graph.facebook.com/{graph_version}/{phone_number_id}/messages` is
permitted. Connection egress must contain exactly `https://graph.facebook.com`.
The app/WABA/phone mapping cannot come from workflow input.

## Ingress and workflow mapping

POST signatures cover exact raw bytes with HMAC-SHA256 and appSecret before JSON
parsing. Missing, malformed and repeated signatures fail; rewriting whitespace
invalidates a signature. Raw bodies are limited to 1 MiB, normalized batches to
100 events. All entries, changes, messages and statuses are inspected. A mixed
WABA/phone batch, schema conflict or failed commit leaves no partial receipts or
status projection and receives no success ACK. Provider timestamps are occurrence
facts, not a freshness authentication mechanism.

`whatsapp-message` and `whatsapp-status` are both dispatchable. Their required
common envelope includes event_type, installation_id, message_id, phone_number_id,
peer_id, timestamp, text, status, error_codes and facts_digest. For messages, text
is populated and status is null; statuses have null text. Map `/payload` to the
shared example target schema, then branch on event_type. Source creation checks
both schemas. A text-only mapping/target cannot silently discard status events.

Recognized statuses are sent, delivered, read, failed and deleted in this offline
compatibility profile. Unsupported message types/status names with a valid stable
identity produce durable ignored receipts and a safe reason. Unsupported envelope
categories or malformed required structures reject the whole batch. Self messages
from the configured business number are ignored. No sender identity conveys
Weave authorization or adds an outbound recipient to the allowlist.

## Status history

Each status identity includes installation, message ID, status and provider
timestamp. Immutable facts never overwrite one another. Progress is monotonic
only along sent → delivered → read; failed_seen and deleted_seen are separate
flags. Read, sent, delivered creates three facts and ends with progress read.
A failed fact after read leaves progress read and sets failed_seen. Deleted does
not delete data or imply that it follows read in an ordering.

Installation identity binds tenant/project/environment plus app, WABA and phone.
Credential rotation, Graph version changes and replacement sources retain that
history. A repeated fact across two sources creates one fact and two source
observations, but each source can dispatch its own workflow receipt. Disable the
old source during cutover when duplicate workflow effects are undesirable.
This is source-local at-least-once delivery, not global exactly-once execution.

Unknown message IDs may receive statuses even if Weave did not originate the
send. A stored status is not a claim that Weave accepted the outbound operation.
Accepted sends return only message_id and acceptance=accepted; delivery remains
separate provider evidence.

Read operations require current scoped run.read and the corresponding native
package selected. Disabled sources remain readable; an unselected/unavailable
package fails safely. Installation history can contain facts first observed via
another source in the same scope. IDs/cursors never grant cross-scope authority.

```text
GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-sources/{source}/whatsapp-statuses?message_id={wamid}
GET /api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/provider-sources/{source}/whatsapp-statuses/{state_id}/facts
```

SDK: `whatsapp_delivery_state(source_id, message_id)` and
`list_whatsapp_status_facts(source_id, state_id, limit=50, cursor=None)`.
Facts use scope/source/state-bound cursors and a maximum page size of 100.

```sh
weave whatsapp-statuses read --source SOURCE_UUID --message-id wamid.out \
  --base-url https://weave.example --tenant TENANT_UUID --project PROJECT_UUID --environment ENV_UUID
weave whatsapp-statuses facts --source SOURCE_UUID --state-id STATE_UUID --limit 50 \
  --base-url https://weave.example --tenant TENANT_UUID --project PROJECT_UUID --environment ENV_UUID
```

Use the existing CLI credential/session mechanism. JSON stdout and exit codes
follow shared CLI behavior: 1 for API denial/error, 2 for local usage/configuration,
3 for server/transport failure. Source/receipt lifecycle remains under
`weave provider-sources` and `weave provider-receipts`.

## Sends and failure handling

`send-text` accepts only recipient/text. `send-template` accepts recipient,
configured name/locale and ordered body-text parameters. Recipients must exactly
match the canonical digit-only allowlist. Templates must exactly match the
configured approval assertion and parameter count. No arbitrary components,
media, buttons, URL headers, account/subscription APIs or template creation exist.

Local limits: 100 recipients/templates, 20 parameters, 4096 text characters,
1024 characters per parameter, encoded request at most 1 MiB and response at most
64 KiB, subject to tighter invocation budgets. These are local conservative
bounds, not assertions about current provider quotas. The total invocation
deadline includes authorization and secret resolution.

The adapter makes one POST, with no redirects, proxies or automatic retries.
Explicit provider 4xx rejection is failed; 429 is failed with WHATSAPP_RATE_LIMIT.
Timeout/cancellation after transmission may begin, 408/5xx, redirects, response
limits and malformed success remain unknown. Credentials, text, provider error
bodies and diagnostic response headers are never ordinary outputs. An operation
key is not provider idempotency. Retry decisions remain explicit and governed by
the operation ledger; never automatically replay an unknown POST.

Status facts/observations and dedup evidence are retained by default. Disabling a
source does not purge them. Ordinary roles have no delete privilege.
The [retention policy](../operations/retention.md) governs coordinated cleanup
and preserves active, unknown-outcome, and deduplication references.

## Evidence boundary

Wire shapes derive from Meta's official sample pinned to
[14703a3e1fdba9bcf75b2360b00817b6fcc9f79b](https://github.com/fbsamples/business-messaging-sample-tech-provider-app/tree/14703a3e1fdba9bcf75b2360b00817b6fcc9f79b)
and its [official Postman webhook reference](https://www.postman.com/meta/whatsapp-business-platform/folder/vzaxn16/webhook-payload-reference).
The Postman text includes historical beta wording. Direct current normative Meta
pages returned 429 during September 30 research. Current version/lifecycle,
commercial rules, account permissions and provider retry behavior remain live
acceptance prerequisites. Local tests cannot certify those facts.
