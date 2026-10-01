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

# Python SDK

The base wheel includes the pure compiler, canonical models and `WorkflowBuilder`. Remote host clients and CLI login use the optional `client` extra. In this source workspace use `uv sync --locked --no-editable --extra client`; the lock uses the verified published PyFly 26.9.15 wheel URL and SHA256. Server/database packages are not client dependencies.

```python
from firefly_weave.contracts.access import Scope
from firefly_weave.sdk.client import WeaveClient

# The host owns these UUIDs and a callable that returns a current access token.
async def compile_remote(api_origin, token_provider, tenant_id, project_id, text):
    scope = Scope(tenant_id=tenant_id, project_id=project_id)
    async with WeaveClient(api_origin, token_provider, scope) as client:
        catalog = await client.catalog()
        return await client.compile(source=text, format="yaml", catalog=catalog,
                                    filename="editor.yaml")
```

`WeaveClient` is asynchronous; use `async with` and await operations. Synchronous context use fails clearly. The client owns its HTTPX client **and any supplied transport** and closes them on exit. It cannot be reused after closing. The synchronous credential callable runs once per request, so host-driven rotation does not require a new client. An explicit `AsyncTokenProvider.get_access_token(target)` port supports asynchronous acquisition/rotation; it receives the exact API origin. Supplying an async function through the synchronous callable port is rejected.

The base URL is an exact HTTPS origin, with HTTP allowed only for local loopback development. Requests have bounded timeouts/response sizes, no redirect following, ambient proxy inheritance, compression acceptance or automatic retries. Mutation transport errors explicitly leave the outcome unknown. The SDK always emits canonical versioned resource paths and quoted revisions, validates exact request/response DTOs and rejects unsupported wire/artifact versions.

`WeaveError` exposes `status`, `code`, `request_id`, `diagnostics` and the safe `problem`; HTTP412 is `PreconditionFailed`. `TransportError` and `ContractError` distinguish uncertain delivery and unsupported responses. Exception text omits provider/transport bodies. Unavailable run/task/resource unions remain typed instead of being coerced to successful payloads.

## Operation families

| Methods | Purpose |
| --- | --- |
| `compile`, `validate`, `catalog`, `schemas`, `capabilities` | Canonical authoring and supported language/IR/limits/connectors |
| `save_draft`, `read_draft`, `export_draft`, `delete_draft`, `list_definitions` | Append-only draft editing, retirement and catalog discovery |
| `publish`, `read_definition`, `export_definition`, `retire_definition` | Immutable workflow, Action and Connector versions |
| `activate`, `read_activation`, `export_activation`, `retire_activation`, `list_activations` | Environment pins and lifecycle |
| `create_connection`, `read_connection`, `test_connection`, `list_connections` | Immutable reference-only connections and explicit health checks |
| `start_run`, `read_run`, `signal`, `cancel`, `retry`, `list_runs` | Durable runtime operations |
| `history`, `export_run`, `replay`, `run_incidents`, `list_incidents`, `resolve_incident` | Bounded trace/evidence and audited recovery |
| `register_release`, `read_release`, `list_releases`, `register_worker`, `read_worker`, `list_workers`, `revoke_worker`, `grant_connection` | Trusted worker capabilities and current status |
| `claim_tasks`, `heartbeat`, `complete_task`, `fail_task`, `request_credentials` | Typed worker protocol, with existing `WorkerTransport` also supported |
| `create_trigger`, `read_trigger`, `list_triggers`, `disable_trigger` | Immutable signed ingress revisions |
| `save_schedule`, `read_schedule`, `list_schedules`, `schedule_history`, `change_schedule` | UTC schedules with revisions and bounded occurrence pages |
| `create_debug`, `inspect_debug`, `command_debug` | Effect-free project sessions and virtual time |
| `create_tenant`, `create_project`, `create_environment`, `read_environment`, `grant` | Existing separately authorized provisioning roots |

`invoke(operation_id, ...)` is the registry-validated adapter used by the CLI; host code should generally use the typed methods. Every paginated method returns a typed `Page` except run history, whose `EventPage` also carries its high-water mark. Resource IDs and cursor scope/filter must match the client's scope. Secret lease results use the exclusion-preserving `CredentialLease` model; ordinary serialization omits the resolved value.

## Device login, PKCE and stores

`OAuthSession(LoginConfig(...), store)` is independent of server Settings, database access and domain authorization. Discovery verifies the exact issuer and explicitly trusted endpoint origins. The binding includes provider, issuer, client ID, target API origin and local account selector. That selector is a local storage identity, not an unverified assertion about an ID token. The client uses access tokens; ID tokens remain opaque and are never used for API authentication.

`login(flow="auto")` selects device authorization when advertised, otherwise browser authorization with S256. Device login explicitly opts into the public framework's S256 extension; it does not downgrade on denial. Generic device polling/code exchange remain in PyFly. Browser login binds an ephemeral loopback listener before launching the browser, verifies the exact callback host/path/state and issuer where advertised, rejects repeated/ambiguous callbacks, uses the same redirect URI for exchange and closes accepted connections on cancellation/timeout. Noninteractive hosts can supply a browser/instruction callback to their UI. All login waits are bounded; denial and expiry do not save credentials.

Native storage uses an approved operating-system keyring backend and refuses an insecure or unknown backend. There is no silent plaintext fallback. An explicit `FileCredentialStore(absolute_path)` supports POSIX only: the existing parent must be owned by the caller with mode0700, and files/locks must be regular, singly linked, caller-owned0600 files. Descriptor-relative no-follow traversal rejects symlinks, including ancestors. Credential replacement uses an exclusive temporary sibling, file fsync, atomic rename and directory fsync. Windows file fallback is refused; approved native vault storage uses a per-record, non-reentrant in-process lock before the per-user named Windows mutex. This excludes async tasks across store instances and event-loop threads, since the OS mutex alone reenters on one thread. Both acquisition stages share one 60-second deadline, cancellation is preserved, and native mutex acquisition/release stay on the owning thread. The registry retains the same lock while any holder or waiter uses it. POSIX cooperating processes use a shared lock file that is never replaced or unlinked during use.

Each login first saves a token-free `authenticating` record with a fresh UUID generation. Completion saves tokens only if that exact generation and pending state still exist under the same lock. Logout, record deletion or a newer login prevents a late save; explicit new login replaces prior credentials, so cancellation can require reauthentication.

Refresh holds that same per-record lock. Before any external refresh exchange it atomically saves a fresh token-free `refreshing` fence. Only the process's local memory then holds the old refresh token. Failure to save the fence makes zero token requests. A successful rotating response must replace the active record before access is returned. Crash, ambiguous exchange or final-save failure leaves a fence that subsequent processes treat as requiring login; old tokens are never restored. Scope escalation or missing rotation is rejected. Durability means the chosen store's acknowledged atomic write; broader power-loss/OS guarantees are not claimed.

Logout saves a token-free logged-out generation and removes the local record under the lock. Deletion failure is an error. Optional revocation reports `confirmed`, `unconfirmed` or `not_requested` separately from local removal. Revocation cannot undo an already issued access token's lifetime or external effects. Status is local metadata and performs no network refresh. Tokens and grant verifiers never enter ordinary repr, CLI JSON, lock files or errors.

The pinned Keycloak provider's stale-refresh comparison has whole-second token timestamps. The client fence prevents cooperating processes from reusing the old record regardless of this provider granularity; do not infer stronger server-side replay guarantees from a rotated string alone.
