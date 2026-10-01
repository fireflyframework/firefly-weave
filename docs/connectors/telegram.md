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

# Telegram webhook text connector

Use this guide to trigger a workflow from a Telegram text message and reply in
the same chat. The offline example first produces definitions without sending a
message. Live setup additionally needs a registered bot, its token, an allowed
chat, an HTTPS webhook, and a running Weave native executor.

![Provider comparison highlighting source-local Telegram update receipts](../diagrams/integrations-messaging.svg)

**How to read this diagram:** Follow the Telegram column: bot and update identity determine the source-local receipt, while the configured chat determines the outbound target. The Teams reference and WhatsApp status mechanisms are separate provider contracts.

## Follow the two directions

You will connect an inbound text message to a Workflow that replies `Received.`:

```text
Telegram update → provider source → durable receipt → Workflow → reply-text Action → Telegram
```

Inbound authentication uses `webhookSecret`; outbound authentication uses
`botToken`. Creating the connection does not register the webhook, and registering
the webhook does not admit a worker that can send replies.
Use an [existing API](../guides/connect-to-api.md) or create one with
[standalone setup](../guides/standalone.md), then arrange
[native worker admission](../guides/workers.md). The
[package-to-activation sequence](authoring.md#from-package-to-an-executable-workflow)
explains each Weave resource and where its ID comes from.

For the offline part, use the source checkout. This fixture imports the native
connector, so select its server dependencies explicitly; a client-only CLI install
is not the Python environment for this script:


```sh
uv run --locked --no-editable --extra server python examples/telegram_text_reply.py > telegram-bundle.json
```

Successful exit produces four keys: `connector`, `action`, `workflow`, and `source`.
The script compiles the Action and Workflow before printing them and sends nothing.
Publish those definitions in dependency order, create the connection below,
admit the exact native release/grants, activate, then replace the source template's
two `REPLACE_WITH_...` UUIDs. The synthetic bot/chat IDs also need replacement.
[Provider sources](../reference/provider-sources.md#create-one-inbound-route)
shows how to build a source request from installed metadata and inspect receipts.

Obtain the bot ID and token from your operator-managed bot registration, and the
numeric chat ID from a verified update for the intended chat. Chat IDs are not
usernames. Store token/secret values in the operator secret provider and use only
handles in `connection.json`. Publish the installed Connector first and replace
this connection request's version UUID with its returned `id`.


Enable `firefly-weave:weave-telegram:firefly_weave.connectors.telegram:package` in `Settings.connector_packages` (`WEAVE_CONNECTOR_PACKAGES`) on server/native-worker installations. The package adds no Telegram SDK. Base/compiler installs do not load it; the native container loads only operator-selected provider services. Distribution version, adapter version and schema digest are separate immutable source pins.

The profile supports original human text messages in explicitly allowed private/group/supergroup chats, `send-text`, and same-chat `reply-text`. It deliberately ignores edits, bot messages, media/service events, channel/business/guest/topic/ephemeral/anonymous messages and unknown update classes. Their durable receipts explain the ignored disposition. Unknown event content is represented by a digest, not stored raw. A malformed or wrong-chat supported event is rejected without acknowledgement.

## Provisioning

Create the bot and HTTPS webhook registration externally under operator control. Weave never calls `setWebhook`, `deleteWebhook`, `getUpdates`, `getMe`, sends a health message, or changes accounts. Polling configuration is rejected. `test_connection` returns `ok=false, code=failed` because remote connection testing is unsupported; this is not a claim that a configured bot was contacted.

Use a connection with these exact nonsecret values and two distinct scoped secret handles:

```json
{
  "name": "telegram",
  "connector_version_id": "00000000-0000-4000-8000-000000000001",
  "config": {"account_id": "123456", "mode": "webhook", "allowed_chat_ids": ["-987"]},
  "secretRef": {"botToken": "telegram-bot-token", "webhookSecret": "telegram-webhook-secret"},
  "allowed_destinations": ["https://api.telegram.org"]
}
```

`account_id` is the configured bot ID. Chat IDs are canonical nonzero signed decimal strings; usernames are unsupported. Lists contain 1–100 distinct IDs. Source `policy` must equal this immutable connection config, including the list and its order. To use a narrower policy, create a separate connection revision. An incoming chat/sender never grants Weave authority or chooses a workflow target.

The bot token is resolved only for sending; it must match the restricted `<configured account_id>:<URL-safe suffix>` profile and be at most 256 characters. This local consistency check is not remote bot-ownership verification. Ingress resolves only `webhookSecret` and requires the configured 1–256 character `[A-Za-z0-9_-]` secret in `X-Telegram-Bot-Api-Secret-Token`. The header uses constant-time comparison, not an HMAC of the body. The Update contains no independent recipient-bot identity: the operator must register the matching unique webhook secret for that bot. Distinct handles do not imply knowledge of unrelated secret values.

Configure Telegram's webhook externally to the existing `/provider-ingress/{source_id}` HTTPS route, with that secret and message-only updates. Do not put the bot token in the Weave ingress URL. Unexpected classes may still arrive and receive explicit ignored receipts. Bot/group permissions and privacy settings determine visibility and require separate live verification. [Telegram webhook setup](https://core.telegram.org/bots/api#setwebhook), [Bot FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

## Trigger and reply example

Run the source-checkout command above with server dependencies installed. It compiles and emits a Connector, Action, Workflow and source request template without making network calls. Publish the definitions, register/authorize the matching native worker capabilities and connection, activate the workflow, then substitute the returned connection-revision and activation UUIDs in the source template. Its package and schema pins are derived from the installed declaration. The example's numeric bot/chat values are synthetic and must be replaced with explicitly authorized values before deployment.

The example maps `/payload` into the workflow, then invokes `reply-text` with the original normalized `chat_id` and `message_id` and the fixed text `Received.`. IDs stay strings across the workflow; the connector converts them to bounded integers on the outgoing wire. The example uses the existing `coalesce` expression for nullable fields in the common event schema. Only original human text events dispatch from this verifier. There is no undocumented expression cast or source-specific runtime path.

Use the existing API, SDK and CLI for administration:

- SDK: `create_provider_source(ProviderSourceRequest(...))`, `read_provider_source(id)`, `list_provider_receipts()`, `read_provider_receipt(id)` and `retry_provider_receipt(id)`.
- CLI: `weave provider-sources create --help`, `weave provider-sources read --help`, `weave provider-receipts list --help`, `weave provider-receipts retry --help` show scoped options and JSON-file input. These use the native OpenAPI operations; ordinary access/error/exit behavior is unchanged.
- Connection/source owners and current grants are rechecked during secret resolution, admission and dispatch. Retrying a receipt cannot restore a disabled source or revoked authority.

Each receipt has kind `telegram-update` and identity `bot:<account_id>:update:<update_id>`. Retries with the same facts deduplicate; changing facts under the same ID conflicts. Arrival order is not a high-water mark, and edits use their own update IDs. Deduplication is source-local: overlapping replacement sources can each dispatch an update. Disable the previous source during a controlled cutover; do not infer global exactly-once delivery. [Telegram Update identity](https://core.telegram.org/bots/api#update).

## Actions and outcomes

`send-text` accepts exactly `chat_id` and `text`. `reply-text` adds `message_id`, a canonical positive decimal string within 1–2147483647. Text is 1–4096 characters. Both actions validate the exact configured chat, current lease authority and credential before one `sendMessage` POST. Replies set `allow_sending_without_reply=false`. Link previews are disabled. Arbitrary hosts, methods, paths, headers, formatting/entity options, attachments, cross-chat replies, paid broadcasts and automatic chat migration are unavailable.

The sole projected result is `{bot_id, chat_id, message_id, acceptance: "accepted"}`. The bounded response must identify the expected bot/chat and a valid message. Accepted is not delivered. The request limit is at most 1 MiB; response limit at most 64 KiB, or the lower action invocation budget. TLS, DNS/peer checks, total deadline and strict JSON/schema checks apply. [sendMessage](https://core.telegram.org/bots/api#sendmessage), [ReplyParameters](https://core.telegram.org/bots/api#replyparameters).

| Result | Outcome |
| --- | --- |
| Invalid input/configuration/chat/secret or unavailable authority before send | `not_started` |
| Proven connection failure before transmission | `not_started`, `TELEGRAM_UNAVAILABLE` |
| Valid explicit 4xx rejection other than 408 | `failed`, `TELEGRAM_REJECTED` |
| Valid 429 rejection | `failed`, `TELEGRAM_RATE_LIMIT` |
| 408/5xx, redirect, malformed/missing/wrong result, response overflow/encoding, possible write timeout/failure | `unknown` |
| Cancellation after transmission may start | Existing worker/operation-ledger cancellation and unknown-outcome handling |

No action retries internally, follows a redirect or invents an idempotency guarantee. A validated `retry_after` never causes a hidden sleep/resend; retry timing must be handled by the operator/workflow. The current failure contract contains only a fixed code and outcome, so it does not expose a structured retry delay. Do not automatically replay unknown sends: the external effect may already have happened. [ResponseParameters](https://core.telegram.org/bots/api#responseparameters).

Bot-token URL paths and raw provider descriptions never enter action output. Owned HTTPX/httpcore diagnostics are contained using a task-local public logging guard through response/client cleanup; unrelated task diagnostics remain enabled. The shared `protected_http_diagnostics()` context also supports owned direct token acquisition. This does not sandbox arbitrary third-party instrumentation or user-installed hooks. Product diagnostics must never dump request objects or traceback locals. Synthetic TLS tests inspect INFO/DEBUG logs, exception chains, response echoes and configured trace-export attributes.

Authentication/payload/size/conflict errors reuse `WV-PROVIDER-AUTH`, `WV-PROVIDER-PAYLOAD`, `WV-PROVIDER-SIZE` and `WV-PROVIDER-CONFLICT`. The source/receipt API is the durable audit surface. Credentials, raw webhook headers, provider response bodies and provider descriptions are not diagnostics.

## Validation scope

Unit, contract and owned local TLS tests exercise synthetic protocol fixtures. Native PostgreSQL tests run only against an explicitly assigned disposable backend and cover concurrent admission, ignored/conflicting facts, commit failure, current authority and source scope. No live bot, recipient permission, delivery, quota, group privacy or production webhook certification follows from these gates. Live sending requires separately authorized bot/chat credentials and actual evidence.
