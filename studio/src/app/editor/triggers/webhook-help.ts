/*
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
*/
// Help for sending a webhook a signed request. The platform accepts a body
// `{"eventId": …, "payload": …}` signed with HMAC-SHA256 over
// "<timestamp>.<body>" (X-Weave-Timestamp, X-Weave-Signature, and
// X-Weave-Event-Id when sent, which must match the body). Studio never has
// the secret's value: the command reads it from $WEAVE_WEBHOOK_SECRET.
import type { Json } from "../../forms/core/json";

const HANDLE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

/** A shell word that keeps every character as it is. */
const quoted = (value: string) => `'${value.replace(/'/g, `'\\''`)}'`;

/**
 * A POSIX shell command that signs and sends a test request to `url` with
 * `payloadSample` as the payload. Each run uses a new event ID and the
 * current time.
 */
export function signedCurl(options: {
  url: string;
  payloadSample?: Json;
}): string {
  const payload = JSON.stringify(options.payloadSample ?? {});
  return [
    "# Set WEAVE_WEBHOOK_SECRET to the secret's value first; Studio never sees it.",
    `PAYLOAD=${quoted(payload)}`,
    "TS=$(date +%s)",
    'EVENT_ID="studio-$TS"',
    `BODY=$(printf '{"eventId":"%s","payload":%s}' "$EVENT_ID" "$PAYLOAD")`,
    `SIGNATURE=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$WEAVE_WEBHOOK_SECRET" | awk '{print $NF}')`,
    `curl -sS -X POST ${quoted(options.url)} \\`,
    "  -H 'Content-Type: application/json' \\",
    '  -H "X-Weave-Timestamp: $TS" \\',
    '  -H "X-Weave-Event-Id: $EVENT_ID" \\',
    '  -H "X-Weave-Signature: $SIGNATURE" \\',
    '  --data-binary "$BODY"',
  ].join("\n");
}

/** The command that stores a secret handle's value on a local platform, or null for a name it would refuse. */
export function secretCommand(handle: string): string | null {
  return HANDLE.test(handle)
    ? `weave platform secret set --handle ${handle}`
    : null;
}

export interface WebhookUrl {
  label: string;
  url: string;
  hint: string;
}

/** The webhook's addresses, each labeled: the production URL first, then the test URL while listening. */
export function webhookUrls(urls: {
  production?: string;
  test?: string;
}): WebhookUrl[] {
  const lines: WebhookUrl[] = [];
  if (urls.production)
    lines.push({
      label: "Production URL",
      url: urls.production,
      hint: "Starts runs of the active version.",
    });
  if (urls.test)
    lines.push({
      label: "Test URL",
      url: urls.test,
      hint: "Works while Studio listens for a test event; its runs are marked Test.",
    });
  return lines;
}
