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
# Authenticated provider sources

![Provider inbox commit followed by background runtime dispatch](../diagrams/integrations-directions.svg)

**How to read this diagram:** Follow the middle lane. The provider ACK follows the inbox commit, while a later transaction starts or signals the pinned target. Read the linked run after a dispatched receipt to inspect actual execution.

## Create one inbound route

A **provider source** is an immutable route from one authenticated provider
installation to one Workflow activation or waiting signal. A **receipt** records
what happened to one provider event. The provider's HTTP acknowledgment means the
receipt is durable; the background dispatcher may still need to start the run.

Begin with [standalone setup](../guides/standalone.md), then the relevant
[Teams](../connectors/teams.md), [WhatsApp](../connectors/whatsapp.md), or
[Telegram](../connectors/telegram.md) installation guide. For a Workflow that sends
a reply, also complete [native worker admission](../guides/workers.md) and
[Connector publication/activation](../connectors/authoring.md#from-package-to-an-executable-workflow).

Collect these values before creating the source:

| Value | Where it comes from |
| --- | --- |
| Scoped environment and bearer credential | Standalone bootstrap/login, with the capabilities listed below |
| `connection_revision_id` | The environment connection creation response's `id` |
| `activation_id` | The compatible Workflow activation response's `id` |
| Package/version/schema pins | The exact installed provider declaration, not a version guessed from an example |
| `policy` | The immutable connection's complete provider config |

For example, create `connection.json` using the complete Telegram request in its
connector guide. After publication and activation, run this local generator with
those two returned UUIDs. It reads the declaration but sends no request:

```sh
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
weave provider-sources create --request source.json
```

For another provider, change the imported declaration, `name`, and `provider`, and
use that provider's connection file and compatible target input schema. This is a
complete `ProviderSourceRequest`; the server supplies `id`, `scope`, `binding_id`,
`principal_id`, and `disabled`. Save its `id`, then configure the provider's callback
to `https://YOUR_PUBLIC_WEAVE_HOST/provider-ingress/SOURCE_ID`. The callback is
outside `/api/v1`. It uses provider authentication, not your CLI bearer token.
For a signal target, use `kind: signal`, omit `activation_id`, and supply the exact
existing `run_id` and declared `signal` name instead.

## Inspect admission separately from execution

After an authorized test delivery, list receipts and read the one whose
`source_id` and `event_id` match. Use its resulting `run_id` to read run state/history.

| Observation | Next check |
| --- | --- |
| No receipt, HTTP 401 | Provider signature/secret and installation policy; the route UUID is not a credential |
| No receipt, HTTP 422 or 413 | Payload/schema or size/batch rejection; do not assume part of the batch committed |
| `pending` | Scheduler-enabled dispatcher, database readiness, and cooldown |
| `ignored` | Safe reason; lifecycle/unsupported events may intentionally start no Workflow |
| `blocked` | Current source owner, grants, standing binding, and package availability |
| `failed` | Target run/activation and schema; inspect the reason before explicit retry |
| `dispatched` | Read the linked run; it may still be waiting, suspended, or failed |

`weave provider-receipts retry RECEIPT_UUID` preserves event identity and rechecks
authority. It cannot enable an irreversibly disabled source or make a terminal
signal target writable. A replacement source has a different deduplication scope.


Provider sources use installed, operator-allowlisted native verifier services.
[Teams](../connectors/teams.md), [WhatsApp](../connectors/whatsapp.md), and
[Telegram](../connectors/telegram.md) implement their own authentication and
installation policies. Enable the corresponding package explicitly. Generic
[signed webhooks](http-and-webhooks.md) provide a separate ingress contract.

```mermaid
sequenceDiagram
    participant P as Provider
    participant V as Native verifier
    participant DB as Scoped inbox transaction
    participant D as Background dispatcher
    participant R as Existing runtime
    P->>V: Exact raw bytes and protocol headers
    V->>V: Authenticate, installation check, normalize
    V->>DB: Up to 100 typed events
    DB->>DB: Lock source, classify whole batch
    DB->>DB: Hook only new events; store receipts and intents
    DB-->>P: Provider ACK after commit
    D->>DB: Lock source and pending receipt; recheck authority
    D->>R: Start exact activation or signal exact run
    R->>DB: Runtime state and receipt settlement in one transaction
```

A source pins package distribution, its exact installed version, a concrete adapter implementation version, provider and the SHA-256 canonical digest of its effective event-schema and dispatch-kind contract, exact connection revision, installation policy and target. The server assigns its scope, owner, ID and standing connection binding. The package metadata `distribution_version` is an exact bounded ASCII installed identity (for example `0.1.0a1`), with the old `version` fallback retained for stable packages. `version` remains the adapter implementation SemVer and does not change connector binding semantics. A source request may omit `adapter_version` only when declaration selection is unique; the server always persists a concrete SemVer. Two providers or adapter implementations in one installed distribution retain distinct verifier identities. First-party providers must use an explicit trusted native declaration path; third-party declarations cannot claim reserved built-in adapter names.

The target is either an exact activation or an exact run plus signal name. The `mapping` field is the compiler's expression AST; the default `{ "ref": "/payload" }` maps the normalized payload. Static schema containment is checked at source creation for every dispatchable kind; canonical runtime validation and secret classification guard every dispatch payload before admission. No external schema retrieval, arbitrary run search, implicit fanout, sender-to-principal mapping or automatic attachment download occurs. Policy entries must be exact copies of existing validated connection configuration keys; an absent key with null is rejected. When the connection defines `account_id`, the source must explicitly pin that nonempty string. Other required provider identity fields are checked before writes by the optional synchronous `ProviderSourceValidator.validate_source(request, connection)` port, which receives defensive copies and performs no I/O. Source changes require a new source; disable is irreversible for that source ID.

Management routes are under `/api/v1/tenants/{tenant}/projects/{project}/environments/{environment}`:

| Resource | Methods | Required current authority |
| --- | --- | --- |
| `/provider-sources` | POST, GET | Create: `trigger.manage`, standing `connection.manage` + `connection.bind`, target `run.start`/`run.signal`; list: `run.read` |
| `/provider-sources/{identifier}` | GET | `run.read` |
| `/provider-sources/{identifier}/disable` | POST | `trigger.manage`, `connection.bind`, target capability |
| `/provider-receipts` | GET | `run.read` |
| `/provider-receipts/{identifier}` | GET | `run.read` |
| `/provider-receipts/{identifier}/retry` | POST | `run.retry`, target capability and current source-owner, binding, package and target authority |

List operations use the standard bounded `limit` (1–100), opaque scope-bound `cursor` and `next_cursor`. Receipt views expose identities, times, attempts, safe reason codes and resulting run/signal IDs. They never expose the stored normalized or mapped payload. Read authority cannot authorize a retry.

Public ingress is `POST /provider-ingress/{identifier}`; provider-specific challenges use GET on that route. The random route ID grants no authority. The private lookup function returns only scope coordinates, never credentials or source policy. Authentication receives exact bytes before parsing. Controllers stream at most 1 MiB and reject duplicate headers; normalization permits at most 100 events. Challenges verify their separate protocol credential and never admit receipts or start a run.

Semantic identity is `(source ID, event kind, provider event ID)`. The fingerprint includes typed semantic facts, disposition and occurrence time; it excludes ingress receipt time and retry-envelope metadata. An exact replay reuses the durable event and never repeats an admission hook. Conflicting identities reject the whole batch with `WV-PROVIDER-CONFLICT` (409), including any new events in that batch. Provider schema/secret rejection is `WV-PROVIDER-PAYLOAD` (422); authentication is a safe 401; body/batch overflow is 413. Unexpected precommit failure is 500 and must not acknowledge. A successful ACK promises durable admission, not workflow completion or external delivery.

Receipts are `pending`, `dispatched`, `ignored`, `blocked` or `failed`. Verified lifecycle/unsupported events require an explicit `ignore` disposition and safe reason. A terminal run retains its pinned schema for admission: lifecycle hooks can still record removal, while new dispatch events become visible failed receipts without mutating that terminal run. Native verifiers can inject `ProviderCredentials` to resolve scoped secret handles through the connections-owned bounded resolver, which checks source/owner/binding authority before and after secret access. The verifier may implement `ProviderAdmissionHook.persist(tx, source, events)` for local protocol state; it receives only new events, no raw authentication bytes, and performs no network or runtime operations. Hook failure rolls back protocol state, receipts and intents together.

The independent lifespan-owned dispatcher has its own durable tenant/environment cursors. It processes at most ten due receipts per environment turn with a five-second total deadline per dispatch, four-second SQL and one-second lock limits. Transient failures roll back and receive a thirty-second persisted cooldown; unavailable authority produces visible blocked state, and terminal/schema failure produces failed state. Blocked/failed receipts require an explicit retry that retains semantic identity. The `scheduler_enabled` setting controls background execution; API-only replicas can leave durable pending work for a dispatcher replica.

Locks follow source `FOR UPDATE` → receipt `FOR UPDATE` → standing binding `FOR SHARE` → existing runtime locks → outbox project advisory lock. Admission takes the source lock and checks all duplicates before hooks and inserts. Binding revocation touches only its binding; outbox subscription administration uses separate `outbox-subscription` bindings. Provider tables force tenant, project and environment RLS, with each transaction setting all three scope values, including empty absent dimensions. No cross-request pool scope survives a transaction.

SDK methods are `create_provider_source`, `read_provider_source`, `list_provider_sources`, `disable_provider_source`, `read_provider_receipt`, `list_provider_receipts`, and `retry_provider_receipt`. CLI commands mirror operation names:

```sh
weave provider-sources create --request source.json
weave provider-sources list --limit 20
weave provider-receipts read RECEIPT_UUID
weave provider-receipts retry RECEIPT_UUID
```

Supply the standard `WEAVE_BASE_URL`, tenant/project/environment IDs and bearer credential configuration. The shared CLI emits safe machine JSON; exit 1 means remote validation/authorization failure, exit 2 local configuration, and exit 3 server failure. No automatic transport replay occurs. Native OpenAPI and offline schemas include every provider administration DTO and operation.

Migration `0018_provider_inbox` follows `0017_outbox`. Retention and restore must preserve `provider_sources`, `provider_receipts`, `provider_intents`, connection source bindings, and provider protocol tables together. Pending receipts and dispatched runtime effects share atomic settlement, so restart can retry without creating another run. Follow the [retention](../operations/retention.md) and
[backup and restore](../operations/backup-restore.md) procedures to preserve these relationships.


Package metadata may declare `dispatch_event_kinds`, a nonempty unique subset of `event_schemas` keys. Omission means all declared event kinds can dispatch. Other kinds may only return `disposition="ignore"`; their own schema and secret classification still apply. `firefly_weave.contracts.providers.provider_schema_digest(event_schemas, dispatch_event_kinds)` computes the source pin as the canonical SHA-256 JSON digest of `{"event_schemas": event_schemas, "dispatch_event_kinds": sorted(effective_kinds)}`. Reordering a declaration leaves the pin stable; changing its effective dispatch capability changes the pin. There is no dynamic event filter language.
