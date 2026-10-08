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
# Receive messaging events with provider sources

A **provider source** is an immutable route from one authenticated messaging
installation (Microsoft Teams, WhatsApp, or Telegram) to one workflow activation
or one waiting signal. When the provider calls Weave, Weave checks the provider's
own authentication, stores each event as a **receipt**, and acknowledges. A
background dispatcher then starts the run or sends the signal.

**Who it is for.** Integration developers and operators who connect a messaging
channel to workflows. For your own system, a [signed webhook](http-and-webhooks.md)
is simpler. **What you need.** A platform that can run the provider's connector
package: a deployment, or the [manual local installation](../guides/standalone.md)
with a native executor. The [local platform](../guides/local-platform.md) from
`weave platform` runs only the built-in HTTP connector, so it cannot run
messaging connectors. You also need the provider's installation guide ([Teams](../connectors/teams.md),
[WhatsApp](../connectors/whatsapp.md), or [Telegram](../connectors/telegram.md));
and, for a workflow that replies, [native worker admission](../guides/workers.md)
and [connector publication and activation](../connectors/authoring.md#from-package-to-an-executable-workflow).
Allow about 30 minutes after the provider side is ready.

![Provider inbox commit followed by background runtime dispatch](../diagrams/integrations-directions.svg)

Follow the middle row. The provider's acknowledgment comes right after the inbox
commit; a later transaction starts or signals the pinned target. After a receipt
shows `dispatched`, read the linked run to see what actually executed.
[Open diagram at full size](../diagrams/integrations-directions.svg)

**The acknowledgment is not completion.** A successful response to the provider
promises that the event is stored durably, not that the workflow ran or that any
reply was delivered.

## Create one inbound route

Run the commands with a saved platform and workspace from `weave auth setup`
(see [how remote commands choose a platform](../guides/connect-to-api.md#how-remote-commands-choose-a-platform)).
Saved platforms are new in 0.1.0a7; with an alpha6 or earlier CLI, these
commands use [explicit mode](../guides/connect-to-api.md#scripts-and-ci-explicit-mode).

**Collect these values first:**

| Value | Where it comes from |
| --- | --- |
| A person with the right grants | `trigger.manage` and `connection.bind` (`deployer`), `connection.manage` (`tenant_admin`), `run.start` or `run.signal` for the target (`operator`), and `run.read` to inspect receipts (`viewer`) |
| `connection_revision_id` | The `id` returned when you create the integration connection |
| `activation_id` | The `id` returned when you activate a compatible workflow |
| Package, version, and schema pins | The exact provider declaration installed on the server, never a version copied from an example |
| `policy` | The integration connection's complete provider `config` |

1. **Create the integration connection.** Save the complete connection request
   from your provider's guide as `connection.json`, then:

    ```sh
    # Create the provider connection; its secret handles were granted by the operator.
    weave connections create --request connection.json --output json
    ```

    Expected: a connection revision whose `id` you save as
    `CONNECTION_REVISION_ID`.

2. **Activate the workflow** that receives the events, and save the activation's
   `id` as `ACTIVATION_ID`.

3. **Generate the source request.** The pins must match the provider declaration
   the server has installed, so run this with a Python environment that has the
   same Weave version as the server installed (`python` below must import
   `firefly_weave`). It reads the declaration and sends nothing. This example is
   for Telegram:

    ```sh
    # Record the two IDs, then generate source.json from the installed declaration.
    export CONNECTION_REVISION_ID='replace-with-returned-connection-uuid'
    export ACTIVATION_ID='replace-with-returned-activation-uuid'
    python - <<'PY_SOURCE' > source.json
    import json
    import os
    from pathlib import Path
    from uuid import UUID
    from firefly_weave.connectors.telegram import package
    from firefly_weave.contracts.providers import ProviderSourceRequest, provider_schema_digest

    metadata = package.metadata.model
    request = ProviderSourceRequest(
        name="telegram-text", provider="telegram", package=metadata.distribution,
        package_version=metadata.distribution_version or metadata.version,
        adapter_version=metadata.version,
        schema_digest=provider_schema_digest(metadata.event_schemas, metadata.dispatch_event_kinds),
        connection_revision_id=UUID(os.environ["CONNECTION_REVISION_ID"]),
        policy=json.loads(Path("connection.json").read_text())["config"],
        kind="run", activation_id=UUID(os.environ["ACTIVATION_ID"]),
        mapping={"ref": "/payload"},
    )
    print(request.model_dump_json(exclude_none=True, indent=2))
    PY_SOURCE
    ```

    Expected: `source.json` with `name`, `provider`, `package`,
    `package_version`, `adapter_version`, `schema_digest`,
    `connection_revision_id`, `policy`, `kind`, `activation_id`, and `mapping`.
    For another provider, change the imported declaration, `name`, and
    `provider`, and use that provider's connection file and a target whose input
    schema accepts its events.

4. **Create the source:**

    ```sh
    # Create the immutable route; the server assigns its ID, scope, and owner.
    weave provider-sources create --request source.json --output json
    ```

    Expected: your request plus `id`, `scope`, `binding_id`, `principal_id`, and
    `"disabled": false`. Save `id` as `SOURCE_ID`.

5. **Point the provider at Weave.** Configure the provider's callback to
   `https://YOUR_PUBLIC_WEAVE_HOST/provider-ingress/SOURCE_ID`. This route is
   outside `/api/v1` and uses the provider's authentication, never your CLI
   bearer token. The random route ID grants no authority on its own.

6. **Send a test event** from the provider, then inspect it as described in the
   next section.

For a signal target, use `"kind": "signal"`, leave out `activation_id`, and
supply the existing `run_id` and the declared `signal` name.

## Inspect admission separately from execution

After an authorized test delivery, find the receipt whose `source_id` and
`event_id` match, then read its run:

```sh
# List receipts in this environment; pass --cursor with next_cursor for the next page.
weave provider-receipts list --limit 20 --output json
# Read one receipt by ID to see its status, reason, and resulting run_id.
weave provider-receipts read RECEIPT_UUID --output json
```

Expected: a receipt with `status` and, once dispatched, the resulting `run_id`
(and `signal_id` for a signal target). Read that run with
`weave runs read RUN_ID --output json`.

| What you see | What to check next |
| --- | --- |
| No receipt, HTTP 401 | The provider signature, secret, and installation policy; the route UUID is not a credential |
| No receipt, HTTP 422 or 413 | A payload or schema rejection, or a size or batch limit; no part of that batch was stored |
| `pending` | A scheduler-enabled dispatcher, database readiness, platform capacity, and the retry cooldown |
| `ignored` | The safe reason; lifecycle and unsupported events may intentionally start nothing |
| `blocked` | The source owner's current grants, the standing binding, and package availability |
| `failed` | The target run or activation and its schema; read the reason before an explicit retry |
| `dispatched` | Read the linked run; it may still be waiting, suspended, or failed |

To try a blocked or failed receipt again:

```sh
# Retry the same event; its identity is preserved and authority is checked again.
weave provider-receipts retry RECEIPT_UUID --output json
```

A retry cannot re-enable a disabled source or make a finished signal target
writable. A replacement source has its own deduplication scope.

## How an event flows

1. The provider sends exact raw bytes and its protocol headers to the native
   verifier.
2. The verifier authenticates them, checks the installation, and normalizes up to
   100 typed events.
3. One transaction locks the source, classifies the whole batch, runs the
   provider's admission hook for new events only, and stores receipts and
   dispatch intents.
4. The provider receives its acknowledgment after that commit.
5. The background dispatcher locks the source and a pending receipt, and
   rechecks authority.
6. It starts the exact activation or signals the exact run.
7. The runtime state and the receipt settle in one transaction.

Provider sources use installed, operator-allowlisted native verifiers. Each of
[Teams](../connectors/teams.md), [WhatsApp](../connectors/whatsapp.md), and
[Telegram](../connectors/telegram.md) implements its own authentication and
installation policy; enable the package explicitly.

## What a source pins

- **The provider declaration.** Package distribution, its exact installed version,
  a concrete adapter implementation version, the provider, and the SHA-256 digest
  of its effective event-schema and dispatch-kind contract. `distribution_version`
  is an exact installed identity (for example `0.1.0a1`), with `version` as the
  fallback for stable packages; `version` is the adapter implementation SemVer. A
  request may omit `adapter_version` only when one declaration matches; the server
  always stores a concrete version. Two providers or adapters in one distribution
  keep distinct verifier identities. Built-in providers use a trusted native
  declaration path, and third-party declarations cannot claim reserved built-in
  adapter names.
- **The connection.** The exact connection revision and installation policy.
  `policy` entries must be exact copies of the connection's validated config keys;
  a key absent from the connection cannot appear as `null`. When the connection
  defines `account_id`, the source must pin that nonempty string.
- **The target.** Either an exact activation, or an exact run plus signal name.
- **The mapping.** `mapping` is a workflow expression; the default
  `{"ref": "/payload"}` passes the normalized payload.

The server assigns the scope, owner, ID, and standing connection binding. To
change anything, create a new source; disabling a source is irreversible for that
ID.

**Checks at creation and dispatch.** Static schema containment is checked when the
source is created, for every dispatchable event kind. Every dispatch payload is
validated and classified for secrets before admission. Other required provider
identity fields are checked before any write by the optional
`ProviderSourceValidator.validate_source(request, connection)` port, which
receives copies and performs no I/O. There is no external schema retrieval,
arbitrary run search, implicit fan-out, sender-to-person mapping, or automatic
attachment download.

## Routes and permissions

Management routes are under
`/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}`:

| Resource | Methods | Required current authority |
| --- | --- | --- |
| `/provider-sources` | POST, GET | Create: `trigger.manage`, standing `connection.manage` and `connection.bind`, and target `run.start` or `run.signal`; list: `run.read` |
| `/provider-sources/{identifier}` | GET | `run.read` |
| `/provider-sources/{identifier}/disable` | POST | `trigger.manage`, `connection.bind`, and the target capability |
| `/provider-receipts` | GET | `run.read` |
| `/provider-receipts/{identifier}` | GET | `run.read` |
| `/provider-receipts/{identifier}/retry` | POST | `run.retry`, the target capability, and the current source owner, binding, package, and target authority |

Lists take `limit` (1 to 100) and an opaque, scope-bound `cursor`, and return
`next_cursor`. Receipt views show identities, times, attempts, safe reason codes,
and the resulting run and signal IDs, never the stored normalized or mapped
payload. Read authority cannot authorize a retry.

## Public ingress and duplicates

`POST /provider-ingress/{identifier}` receives events; provider-specific
challenges use GET on the same route. The private lookup returns only scope
coordinates, never credentials or source policy. Authentication sees the exact
bytes before parsing. Controllers read at most 1 MiB and reject duplicate
headers; normalization allows at most 100 events. A challenge checks its own
protocol credential and never stores a receipt or starts a run.

An event's identity is `(source ID, event kind, provider event ID)`. Its
fingerprint covers typed facts, disposition, and occurrence time, but not the
receive time or retry metadata. An exact replay reuses the stored event and never
repeats the admission hook.

| Response | Meaning |
| --- | --- |
| Provider acknowledgment | Durable admission only, not workflow completion or delivery |
| 401 | Authentication failed (safe response) |
| 409 `WV-PROVIDER-CONFLICT` | An identity conflicts with a stored event; the whole batch is rejected, including its new events |
| 413 | The body or batch is too large |
| 422 `WV-PROVIDER-PAYLOAD` | A schema or secret rejection |
| 500 | An unexpected failure before commit; nothing is acknowledged |

## Receipts, dispatch, and retries

Receipts are `pending`, `dispatched`, `ignored`, `blocked`, or `failed`.

- **Ignored events.** Verified lifecycle or unsupported events need an explicit
  `ignore` disposition and a safe reason.
- **Finished targets.** A finished run keeps its pinned schema for admission:
  lifecycle hooks can still record removal, while new dispatch events become
  visible `failed` receipts without changing that run.
- **Credentials and hooks.** Native verifiers can use `ProviderCredentials` to
  resolve scoped secret handles through the bounded connection resolver, which
  checks source, owner, and binding authority before and after access. A verifier
  may implement `ProviderAdmissionHook.persist(tx, source, events)` for its own
  protocol state; it receives only new events, no raw authentication bytes, and
  performs no network or runtime work. If the hook fails, its state, the
  receipts, and the intents roll back together.

**The dispatcher.** An independent background loop, owned by the API process
when `scheduler_enabled` is on, keeps durable tenant and environment cursors. Per
environment turn, it processes at most ten due receipts, with a five-second total
deadline per dispatch, a four-second SQL limit, and a one-second lock limit.
Transient failures roll back and wait a stored thirty-second cooldown; the
receipt stays `pending` with the reason `transient_failure`. A capacity refusal
(`WV-OPERATION-CAPACITY` or `WV-REQUEST-CAPACITY`) is a transient failure. Missing
authority produces `blocked`; a finished target or schema failure produces
`failed`. Both need an explicit retry, which keeps the event's identity. API-only
replicas can leave pending work for a dispatcher replica.

**Locks and isolation.** Locks are taken in this order: source `FOR UPDATE`,
receipt `FOR UPDATE`, standing binding `FOR SHARE`, the existing runtime locks,
then the outbox project advisory lock. Admission takes the source lock and checks
every duplicate before hooks and inserts. Revoking a binding touches only that
binding; outbox subscriptions use separate `outbox-subscription` bindings.
Provider tables force tenant, project, and environment row-level security, and
each transaction sets all three scope values. No scope survives a transaction.

## Event kinds and the schema digest

A package may declare `dispatch_event_kinds`, a nonempty, unique subset of its
`event_schemas` keys; when omitted, every declared kind can dispatch. Other kinds
may only return `disposition="ignore"`, and their own schema and secret checks
still apply. `firefly_weave.contracts.providers.provider_schema_digest(event_schemas, dispatch_event_kinds)`
computes the pin as the canonical SHA-256 JSON digest of
`{"event_schemas": event_schemas, "dispatch_event_kinds": sorted(effective_kinds)}`.
Reordering a declaration keeps the pin; changing which kinds can dispatch changes
it. There is no dynamic event filter language.

## CLI, SDK, and storage

The CLI families are `weave provider-sources` (`create`, `list`, `read`,
`disable`) and `weave provider-receipts` (`list`, `read`, `retry`). They print
safe JSON. Exit `1` means remote validation or authorization failure, `2` a
local configuration problem, and `3` a server failure; nothing is replayed
automatically. The SDK methods are `create_provider_source`,
`read_provider_source`, `list_provider_sources`, `disable_provider_source`,
`read_provider_receipt`, `list_provider_receipts`, and `retry_provider_receipt`.
Native OpenAPI and the exported schemas include every provider DTO and operation.

Migration `0018_provider_inbox` follows `0017_outbox`. Retention and restore must
keep `provider_sources`, `provider_receipts`, `provider_intents`, connection
source bindings, and provider protocol tables together. Pending receipts and
their runtime effects settle atomically, so a restart can retry without creating
another run. Follow the [retention](../operations/retention.md) and
[backup and restore](../operations/backup-restore.md) procedures to keep these
relationships intact.

## Next steps

- Finish your provider's setup: [Teams](../connectors/teams.md),
  [WhatsApp](../connectors/whatsapp.md), or [Telegram](../connectors/telegram.md).
- [Call HTTP APIs and accept signed webhooks](http-and-webhooks.md): the simpler
  route for your own systems.
- [Incident operations](incident-operations.md): what to do when a dispatched
  run is suspended.

## Troubleshooting

| What you see | Why | What to do |
| --- | --- | --- |
| `provider-sources create` is rejected for `policy` | `policy` differs from the connection's `config` | Copy the connection's `config` exactly, including list order |
| Creation is rejected for the schema digest or version | The pins come from a different Weave version than the server's | Regenerate `source.json` with the server's Weave version |
| The provider reports 401 | Provider authentication or installation policy failed | Check the provider secret handles and installation settings in its guide |
| Receipts stay `pending` | No scheduler-enabled replica is running, the database is not ready, or the platform refused dispatch for capacity (reason `transient_failure`) | Start a replica with the scheduler enabled; check readiness. A capacity refusal is retried after the cooldown |
| A receipt is `blocked` | The source owner lost a grant, or the binding was revoked | Restore the grant, then `weave provider-receipts retry RECEIPT_UUID` |
| A receipt is `failed` | The target run finished, or the payload does not fit the target's schema | Read the reason; fix the target, then retry or create a new source |
