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
// the secret's value: the command reads it from $WEAVE_WEBHOOK_SECRET
// inside Perl's core Digest::SHA, so it is on no command line (OpenSSL
// takes an HMAC key as an argument, which the process list shows).
import type { Json } from "../../forms/core/json";
import type { TriggerIntent } from "../state/canvas-sidecar";

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
    "# Export WEAVE_WEBHOOK_SECRET with the secret's value first; no command line carries it, and Studio never sees it.",
    `PAYLOAD=${quoted(payload)}`,
    "TS=$(date +%s)",
    // Unique within a second too: a reused event ID answers the earlier receipt or a conflict.
    'EVENT_ID="studio-$TS-$(openssl rand -hex 4)"',
    `BODY=$(printf '{"eventId":"%s","payload":%s}' "$EVENT_ID" "$PAYLOAD")`,
    `SIGNATURE=$(printf '%s.%s' "$TS" "$BODY" | perl -MDigest::SHA=hmac_sha256_hex -0777 -ne 'print hmac_sha256_hex($_, $ENV{WEAVE_WEBHOOK_SECRET})')`,
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

export interface SecretCommand {
  label: string;
  command: string;
}

/**
 * The commands that store a webhook's secrets: the production secret, and
 * the test secret its test URL checks, which is never the same handle.
 */
export function secretCommands(trigger: TriggerIntent): SecretCommand[] {
  const commands: SecretCommand[] = [];
  const production = trigger["secretRef"];
  const test = trigger["testSecretRef"];
  const add = (label: string, handle: unknown) => {
    const command = typeof handle === "string" ? secretCommand(handle) : null;
    if (command) commands.push({ label, command });
  };
  add("Production secret", production);
  if (test !== production) add("Test secret", test);
  return commands;
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
