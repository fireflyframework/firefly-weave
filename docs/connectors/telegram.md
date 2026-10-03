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

# Start a workflow from a Telegram message and reply

Use this guide to trigger a workflow from a Telegram text message and reply in
the same chat. The `weave-telegram@1.0.0` connector receives webhook updates for
one bot and sends text only to chats you allowed explicitly.

- **Who it is for:** the operator who registers the bot and its webhook, and the
  author who builds the reply workflow.
- **What you need:** for the offline example, the source checkout. For live use, a
  registered bot and its token, an allowed chat, a public HTTPS webhook, a deployed
  platform with a native executor (the [local platform](../guides/local-platform.md)
  never enables connector packages), and a CLI
  [connected to that platform](../guides/connect-to-api.md).
- **How long:** about 10 minutes for the offline example; about an hour for a
  live route once the bot exists.

![Provider comparison highlighting source-local Telegram update receipts](../diagrams/integrations-messaging.svg)

**How to read this diagram:** Follow the bottom Telegram row from authenticated update to source-local receipt and reply. Bot and update identity determine the receipt; the configured chat determines the outbound target. The Teams reference and WhatsApp status rows describe separate provider contracts.

[Open diagram at full size](../diagrams/integrations-messaging.svg)

## How the two directions connect

You will connect an inbound text message to a workflow that replies `Received.`:

```text
Telegram update → provider source → durable receipt → Workflow → reply-text Action → Telegram
```

Each direction has its own secret and its own setup:

| Direction | Authenticated with | Set up by |
| --- | --- | --- |
| Inbound updates | `webhookSecret`, sent by Telegram in `X-Telegram-Bot-Api-Secret-Token` | A provider source, plus the webhook you register with Telegram |
| Outbound replies | `botToken` | A connection, a published Action, and an admitted native release |

Creating the connection does not register the webhook, and registering the
webhook does not admit an executor that can send replies.

**What the profile supports.** Original human text messages in explicitly allowed
private, group, and supergroup chats; `send-text`; and same-chat `reply-text`.
It deliberately ignores edits, bot messages, media and service events, channel,
business, guest, topic, ephemeral, and anonymous messages, and unknown update
classes. Their durable receipts explain the ignored disposition; unknown event
content is represented by a digest, not stored raw. A malformed or wrong-chat
supported event is rejected without acknowledgment.

## Try the offline example

The example builds every definition you need without sending a message. It
imports the native connector, so select its server dependencies explicitly; a
client-only CLI install is not the Python environment for this script:

```sh
# Compile and print the Connector, Action, Workflow, and source template; nothing is sent.
uv run --locked --no-editable --extra server python examples/telegram_text_reply.py > telegram-bundle.json
```

Expected: a successful exit and a JSON object with four keys: `connector`,
`action`, `workflow`, and `source`. The script compiles the Action and Workflow
before printing them. The source template's package and schema pins come from the
installed declaration, and its numeric bot and chat values are synthetic.

The workflow maps `/payload` into its input, then calls `reply-text` with the
original normalized `chat_id` and `message_id` and the fixed text `Received.`. IDs
stay strings across the workflow; the connector converts them to bounded integers
on the outgoing wire. The example uses the existing `coalesce` expression for
nullable fields in the common event schema. Only original human text events
dispatch from this verifier.

## Set up a live route

1. **Register the bot and collect its facts (operator, outside Weave).** Create
    the bot under operator control and obtain its bot ID and token. Get the
    numeric chat ID from a verified update for the intended chat; chat IDs are not
    user names. Store the token and a webhook secret in the operator secret
    provider, behind two distinct handles.

2. **Enable the package (operator).** Select
    `firefly-weave:weave-telegram:firefly_weave.connectors.telegram:package` in
    `WEAVE_CONNECTOR_PACKAGES` on server and native-executor installations. The
    package adds no Telegram SDK. Base and compiler installs do not load it; the
    native container loads only operator-selected provider services.

3. **Publish the definitions and create the connection.** Publish the bundle's
    `connector`, `action`, and `workflow` in that dependency order, then create the
    connection with the request in [Connection](#connection), using the returned
    Connector version ID:

    ```sh
    # Create the connection revision from the reviewed request file.
    weave connections create --request telegram-connection.json --output json
    ```

    Expected: a connection revision; save its `id`. The
    [package-to-activation sequence](authoring.md#from-package-to-an-executable-workflow)
    explains each Weave resource and where its ID comes from.

4. **Admit, grant, and activate.** Register the native release with the exact
    capabilities, grant it the connection's credentials (`weave workers grant`),
    and activate the workflow with the connection and release pins. Save the
    activation `id`.

5. **Create the provider source.** Replace the source template's two
    `REPLACE_WITH_...` UUIDs with the connection revision and activation IDs, and
    replace the synthetic bot and chat IDs with the real ones. The source `policy`
    must equal the connection config. Then:

    ```sh
    # Create the inbound route from the completed template.
    weave provider-sources create --request telegram-source.json
    ```

    Expected: a source with its `id`.
    [Provider sources](../reference/provider-sources.md#create-one-inbound-route)
    shows how to build a source request from installed metadata.

6. **Register the webhook with Telegram (operator).** Point the bot's webhook at
    the existing `/provider-ingress/{source_id}` HTTPS route, with the configured
    webhook secret and message-only updates. Do not put the bot token in the Weave
    ingress URL. Weave never calls `setWebhook`, `deleteWebhook`, `getUpdates`, or
    `getMe`, sends a health message, or changes accounts; polling configuration is
    rejected. See [Telegram webhook setup](https://core.telegram.org/bots/api#setwebhook)
    and the [Bot FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get).

7. **Send a test message.** From the allowed chat, send a text message, then read
    the receipt and its run with `weave provider-receipts list` and `weave runs
    read`. Expected: a receipt of kind `telegram-update` linked to a run, and the
    reply `Received.` in the chat.

Bot and group permissions and privacy settings determine which messages the bot
sees, and need separate live verification. Unexpected update classes may still
arrive and receive explicit ignored receipts.

## Connection

The connection has these exact nonsecret values and two distinct scoped secret
handles:

```json
{
  "name": "telegram",
  "connector_version_id": "00000000-0000-4000-8000-000000000001",
  "config": {"account_id": "123456", "mode": "webhook", "allowed_chat_ids": ["-987"]},
  "secretRef": {"botToken": "telegram-bot-token", "webhookSecret": "telegram-webhook-secret"},
  "allowed_destinations": ["https://api.telegram.org"]
}
```

- **IDs.** `account_id` is the configured bot ID. Chat IDs are canonical nonzero
  signed decimal strings; user names are unsupported. The list has 1 to 100
  distinct IDs.
- **Source policy.** The source `policy` must equal this immutable connection
  config, including the list and its order. For a narrower policy, create a
  separate connection revision. An incoming chat or sender never grants Weave
  authority or chooses a workflow target.
- **Bot token.** Resolved only for sending. It must match the restricted
  `<configured account_id>:<URL-safe suffix>` profile and be at most 256
  characters. This local consistency check is not remote bot-ownership
  verification.
- **Webhook secret.** Ingress resolves only `webhookSecret` and requires the
  configured 1–256 character `[A-Za-z0-9_-]` secret in
  `X-Telegram-Bot-Api-Secret-Token`. The header uses constant-time comparison,
  not an HMAC of the body. The update contains no independent recipient-bot
  identity, so the operator must register the matching unique webhook secret for
  that bot. Distinct handles do not imply knowledge of unrelated secret values.
- **Connection test.** `weave connections test` returns `ok=false, code=failed`
  because remote connection testing is unsupported; this is not a claim that a
  configured bot was contacted.

## Receipts and administration

Each receipt has kind `telegram-update` and identity
`bot:<account_id>:update:<update_id>`. Retries with the same facts deduplicate;
changing facts under the same ID conflicts. Arrival order is not a high-water
mark, and edits use their own update IDs. Deduplication is source-local:
overlapping replacement sources can each dispatch an update, so disable the
previous source during a controlled cutover and do not infer global exactly-once
delivery. See [Telegram Update identity](https://core.telegram.org/bots/api#update).

Use the existing API, SDK, and CLI to administer sources and receipts:

- **CLI.** `weave provider-sources create`, `read`, `list`, and `disable`, and
  `weave provider-receipts list`, `read`, and `retry`. Each takes `--help` for
  its scoped options and JSON-file input.
- **SDK.** `create_provider_source(ProviderSourceRequest(...))`,
  `read_provider_source(id)`, `list_provider_receipts()`,
  `read_provider_receipt(id)`, and `retry_provider_receipt(id)`.

Connection and source owners and current grants are rechecked during secret
resolution, admission, and dispatch. Retrying a receipt cannot restore a disabled
source or revoked authority.

## Actions and outcomes

`send-text` accepts exactly `chat_id` and `text`. `reply-text` adds `message_id`,
a canonical positive decimal string within 1–2147483647. Text is 1–4096
characters. Both actions validate the exact configured chat, current lease
authority, and the credential before one `sendMessage` POST. Replies set
`allow_sending_without_reply=false`, and link previews are disabled. Arbitrary
hosts, methods, paths, headers, formatting or entity options, attachments,
cross-chat replies, paid broadcasts, and automatic chat migration are
unavailable. See [sendMessage](https://core.telegram.org/bots/api#sendmessage)
and [ReplyParameters](https://core.telegram.org/bots/api#replyparameters).

The only projected result is `{bot_id, chat_id, message_id, acceptance:
"accepted"}`. The bounded response must identify the expected bot and chat and a
valid message. Accepted is not delivered. The request is at most 1 MiB and the
response at most 64 KiB, or the lower action invocation budget. TLS, DNS and peer
checks, a total deadline, and strict JSON and schema checks apply.

| Result | Outcome |
| --- | --- |
| Invalid input, configuration, chat, or secret, or unavailable authority before sending | `not_started` |
| Proven connection failure before transmission | `not_started`, `TELEGRAM_UNAVAILABLE` |
| Valid explicit 4xx rejection other than 408 | `failed`, `TELEGRAM_REJECTED` |
| Valid 429 rejection | `failed`, `TELEGRAM_RATE_LIMIT` |
| 408 or 5xx, redirect, malformed, missing, or wrong result, response overflow or encoding, possible write timeout or failure | `unknown` |
| Cancellation after transmission may have started | Existing worker and operation-ledger cancellation and unknown-outcome handling |

No action retries internally, follows a redirect, or invents an idempotency
guarantee. A validated `retry_after` never causes a hidden sleep or resend; retry
timing belongs to the operator or the workflow. The current failure contract
contains only a fixed code and outcome, so it does not expose a structured retry
delay. Do not replay unknown sends automatically: the external effect may already
have happened. See [ResponseParameters](https://core.telegram.org/bots/api#responseparameters).

**Diagnostics stay value-free.** Bot-token URL paths and raw provider descriptions
never enter action output. Owned HTTPX and httpcore diagnostics are contained by a
task-local public logging guard through response and client cleanup; unrelated
task diagnostics stay enabled. The shared `protected_http_diagnostics()` context
also covers owned direct token acquisition. This does not sandbox arbitrary
third-party instrumentation or user-installed hooks; product diagnostics must
never dump request objects or traceback locals. Synthetic TLS tests inspect INFO
and DEBUG logs, exception chains, response echoes, and configured trace-export
attributes.

Authentication, payload, size, and conflict errors at ingress reuse
`WV-PROVIDER-AUTH`, `WV-PROVIDER-PAYLOAD`, `WV-PROVIDER-SIZE`, and
`WV-PROVIDER-CONFLICT`. The source and receipt API is the durable audit surface.
Credentials, raw webhook headers, provider response bodies, and provider
descriptions are not diagnostics.

## What has been verified

Unit, contract, and owned local TLS tests exercise synthetic protocol fixtures.
Native PostgreSQL tests run only against an explicitly assigned disposable backend
and cover concurrent admission, ignored and conflicting facts, commit failure,
current authority, and source scope. No live bot, recipient permission, delivery,
quota, group privacy, or production webhook certification follows from these
gates. Live sending requires separately authorized bot and chat credentials and
actual evidence.

## Troubleshoot

| What you see | Why | What to do |
| --- | --- | --- |
| `WV-PROVIDER-AUTH` at the webhook | The `X-Telegram-Bot-Api-Secret-Token` header does not match the stored webhook secret | Register the webhook with the same secret as the `webhookSecret` handle |
| The receipt is ignored | The update is not an original human text message, such as an edit, a bot message, or media | Send plain text; only supported updates dispatch |
| The webhook is not acknowledged | A supported message came from a chat outside `allowed_chat_ids`, or the update is malformed | Check the chat ID; for a narrower or wider list, create a new connection revision and source |
| A receipt is dispatched but there is no reply | The run or the reply Action failed | Read the linked run and its incidents |
| `TELEGRAM_RATE_LIMIT` | Telegram returned 429 | Wait before sending again; Weave never retries silently |
| A send ends `unknown` | The message may have been sent | Check the chat before sending again; see [incident operations](../reference/incident-operations.md) |

## Next steps

- Wait for a person before replying: [Human tasks and approvals](../guides/human-tasks.md).
- Understand receipts and dispatch: [Authenticated provider sources](../reference/provider-sources.md).
- Compare with the other messaging connectors: [Teams](teams.md) and [WhatsApp](whatsapp.md).
