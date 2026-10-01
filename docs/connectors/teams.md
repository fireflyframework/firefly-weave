<!--
Copyright 2026 Firefly Software Foundation.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Teams personal bot

## Build an inbound message and reply flow

The inbound **provider source** authenticates a Bot Framework activity and stores
a conversation reference. The outbound **reply Action** uses that stored reference
to address a message. A reference is a server-issued UUID plus a generation; it
is not the provider's conversation ID, and callers cannot invent one to send.

Complete [standalone setup](../guides/standalone.md), then
[native worker admission](../guides/workers.md). The
[publication sequence](authoring.md#from-package-to-an-executable-workflow) explains
Connector version, connection revision, release and activation IDs. Teams also
requires provider assets and an existing personal installation, described below.

Use these checkpoints in order:

1. Gather the bot client/registration tenant UUIDs from the operator's app
   registration and the customer tenant UUID from the intended installation.
   Obtain conversation, bot, user and service URL facts from a separately
   authenticated installation record. This profile cannot discover an installation
   from an email address or bootstrap unknown conversation IDs.
2. Enable the package, publish its Connector, and create the connection using the
   exact policy fields below and a scoped `client_secret` handle. Save the returned
   connection revision `id`; confirm operator-approved service/token origins.
3. Publish a `teams-reply@1.0.0` Action as described below, then
   [`reply.workflow.yaml`](../../examples/teams/reply.workflow.yaml). Admit the
   native worker release and credential grants, bind the connection slot `teams`,
   and save the activation `id`.
4. Create a Teams provider source with that connection revision and activation;
   [the source recipe](../reference/provider-sources.md#create-one-inbound-route)
   shows all request fields. Use the Teams declaration, provider `teams`, and
   copy every connection config field into `policy`.
5. Set the bot messaging endpoint to the returned source's public
   `/provider-ingress/SOURCE_ID` URL. After an authorized real message, inspect the
   provider receipt and its linked run. A 202 response confirms inbound commit,
   not that the reply Action succeeded.

A reply Action's complete input shape is:

```json
{
  "reference_id": "00000000-0000-4000-8000-000000000001",
  "generation": 1,
  "activity_id": "ORIGINAL_ACTIVITY_ID",
  "text": "Message received."
}
```

Use `reference_id`, `generation`, and `activity_id` from the authenticated
normalized event; the UUID above is only a shape example. For `send`, omit
`activity_id` and use a still-authorized stored reference. Success returns
`{"id":"PROVIDER_ACTIVITY_ID","status":"accepted"}`. If the reference is revoked
or its generation is stale, use the explicit reference lifecycle below; resending
the same input cannot recreate authority. For other failures, inspect
[provider receipts](../reference/provider-sources.md#inspect-admission-separately-from-execution)
and [run incidents](../reference/incident-operations.md).


The optional `teams` extra implements public-cloud Bot Connector text messages for one explicitly configured personal installation per immutable connection/source. It supports reply and proactive text to a stored authenticated reference. It does not create conversations, provision accounts, send Graph messages, or implement channels/groups, SSO, skills, invoke, streaming, attachments or cards. Fixture and local PostgreSQL verification is separate from live Azure/Teams certification.

Install the exact reviewed Weave wheel with `server,teams` extras. Operator configuration must select `firefly-weave:weave-teams:firefly_weave.connectors.teams:package` in `connector_packages`; installing the optional SDK alone does not enable it. Missing extras fail startup when selected. The installed distribution version is `0.1.0a1`; adapter/task behavior is separately pinned at `1.0.0`. Native services share the application's existing PyFly container.

## Provision and configure

An operator must provision an Azure Bot resource with a supported single-tenant Entra registration, enable the Teams channel, set its HTTPS messaging endpoint to `/provider-ingress/{source_id}`, publish a Teams app manifest with personal scope and the bot app ID, and install it under tenant policy. See Microsoft's [manual Bot provisioning](https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/provision-azure-bot-service-manually) and [proactive messaging requirements](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages). No automation here creates those resources.

Publish the installed `package.descriptor.manifest`, then create a connection with the following exact policy fields. IDs are external installation facts obtained by the operator; they never become Weave principal grants.

| Field | Meaning |
| --- | --- |
| `cloud` | `public` (default) |
| `account_id` | Canonical bot app/client UUID |
| `registration_tenant` | Canonical Entra registration tenant UUID |
| `tenant_id`, `allowed_tenants` | Exact Teams customer tenant and explicit permitted tenant UUID list |
| `conversation_id` | One installed personal conversation ID |
| `bot_id`, `user_id` | Exact bot and personal-user channel account IDs |
| `installation_generation` | Positive integer, initially 1 |

A complete connection creation request has this shape. All UUIDs and channel IDs
below are illustrative. Replace them with the published Connector version ID and
authenticated installation facts before submission; replace the service origin
with the one approved for that installation.

```json
{
  "name": "teams-personal",
  "connector_version_id": "00000000-0000-4000-8000-000000000001",
  "config": {
    "cloud": "public",
    "account_id": "00000000-0000-4000-8000-000000000002",
    "registration_tenant": "00000000-0000-4000-8000-000000000003",
    "tenant_id": "00000000-0000-4000-8000-000000000004",
    "allowed_tenants": ["00000000-0000-4000-8000-000000000004"],
    "conversation_id": "INSTALLATION_CONVERSATION_ID",
    "bot_id": "INSTALLATION_BOT_CHANNEL_ID",
    "user_id": "INSTALLATION_USER_CHANNEL_ID",
    "installation_generation": 1
  },
  "secretRef": {"client_secret": "teams-client-secret"},
  "allowed_destinations": ["https://login.microsoftonline.com", "https://connector.example.test"]
}
```

`connector.example.test` is a non-routable documentation placeholder, not a
Microsoft service recommendation. The complete authenticated service URL is later
stored in the reference; this connection allowlist contains its origin only.

Connection `secretRef.client_secret` names an operator-granted secret handle. `allowed_destinations` contains literal HTTPS origins for `login.microsoftonline.com` and the selected Bot Connector service. Do not place secrets in config, source policy, action input or workflow output. `test_connection` validates configuration only; success does not certify credentials, installation, or delivery.

The provider source copies all policy fields exactly from that immutable connection revision, selects provider `teams`, package `firefly-weave`, actual package version and adapter version, and pins `provider_schema_digest(package.metadata.model.event_schemas, package.metadata.model.dispatch_event_kinds)`. Only `message` is dispatchable. Lifecycle events are durably ignored by workflow dispatch while updating references transactionally. Mapping is checked against every dispatchable schema. A source requires current source-owner, connection-binding and target authority.

## Message to workflow to reply

`examples/teams/reply.workflow.yaml` demonstrates the input mapping. Publish a connector Action for `weave-teams@1.0.0` action `reply` using the installed descriptor's input/output schema, `config: {}`, `sideEffect: non_idempotent`, and a 30-second timeout. Register a native worker release containing the exact descriptor capabilities/bindings, explicitly grant its credential capability against the selected connection revision, bind that revision at workflow activation, then bind the provider source to that activation. No chat-user identity supplies execution authority.

A verified message produces `reference_id`, `generation`, `activity_id`, `text`, and bounded protocol facts. The native reply receives only those first four fields. A proactive `send` accepts `reference_id`, `generation`, and `text` without `activity_id`. Both restore the scoped reference and check current lease, connection, source and reference authority before token acquisition and again before the single POST. No workflow input can choose a service URL, token endpoint, tenant, audience, scope or credential handle.

The authenticated `serviceurl` claim must exactly match the activity `serviceUrl`, including its base path. IDs are encoded as individual URL components. Ingress uses fixed Microsoft JWKS metadata and public PyFly async validation; it makes no OAuth grant or send. HTTP 202 acknowledgment follows the reference/receipt transaction commit, and commit failure is not acknowledged.

## Revocation and explicit reactivation

References remain fenced across source replacements and connection revisions. A bot removal/uninstall revokes the current generation. Late message/add events cannot reactivate it and receive a typed denial; exact already committed duplicates may safely ACK. Provider timestamps do not establish ordering. A delayed previously unseen removal may conservatively revoke a reactivated generation.

Use the native API, typed SDK methods `read_teams_reference`, `list_teams_references`, `revoke_teams_reference`, `reactivate_teams_reference`, or CLI `weave teams-references read|list|revoke|reactivate`. Canonical API paths are `/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}/teams-references` and `/{identifier}` with `/revoke` or `/reactivate` for POST transitions. Core routes remain documented when Teams is disabled and return unavailable (409). Optional Teams implementation modules and SDKs are loaded only for the exact selected entry point.

Revoke request: `expected_generation`. Reactivate request: `expected_generation`, `source_id`, `request_id` (UUID idempotency identity). Both require current `trigger.manage`, `connection.manage`, and `connection.bind`. Revoke reduces authority and remains possible after disabling the old source. Reactivation requires a separately created immutable connection/source pinning exactly the next generation, full current owner/binding/target authority, and the unchanged previously authenticated address. Conflicting expected generation or idempotency content fails closed. Reactivation neither installs a bot nor verifies a new provider account.

Once a final reference check authorizes transmission, a concurrent revocation cannot recall already authorized/in-flight bytes. No cross-system atomic revocation guarantee is made. Connection/source disablement and grant revocation also deny new sends.

## Authentication and outcome limits

The single-tenant client-secret profile uses one public `OAuth2Client.client_credentials` acquisition per invocation, tenant-fixed token endpoint, explicit POST client authentication and scope `https://api.botframework.com/.default`. There is no cross-invocation token cache or refresh-token grant. Each OAuth client owns and closes its transport; JWKS uses application-owned borrowed async fetchers with bounded per-audience validators (maximum 128), exact endpoint equality and deadlines. Both use bounded egress without ambient proxies or redirects.

The send result is only `{id, status: accepted}`; it is provider acceptance/correlation, not delivery. Timeout/disconnect after a POST may be unknown and is not automatically retried. HTTP 429 is a rejected outcome; this implementation performs no hidden retries. SDK errors, token responses and provider bodies are never copied into task errors/output. Remote worker credentials alone do not grant Teams reference access; the initial connector requires the native lease-bound reference callback.

PyFly is pinned to published `26.9.15`, wheel SHA256 `c712b314cbaaaaec9aa7ec7f256fb8a31e6faf228e3bd52715356fd689c8943b`. Microsoft Agents hosting core/activity are pinned to `1.7.0`. Migration `0019_teams_references` follows `0018_provider_inbox`; backups must preserve the reference fence, lifecycle evidence, administrative receipts and provider inbox together.

Live acceptance still requires an explicitly authorized test tenant, bot, personal installation and destination. Local wire fixtures do not establish Azure provisioning, tenant policy, real service URL/token shapes, delivery, throttle timing or production restart behavior.
