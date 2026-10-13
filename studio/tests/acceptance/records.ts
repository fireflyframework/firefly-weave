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
// Pure helpers of the Studio journey harness: step outcomes, redaction, the
// credential-store guard and the network rules. harness.ts does the I/O.

export const REDACTED = "[redacted]";

export interface StepState {
  applies: boolean;
  missing: string[];
}
export interface SkippedCheck {
  check: string;
  missing: string[];
}
export interface StepRecord {
  id: string;
  status: "passed" | "partial" | "failed" | "skipped" | "not_run";
  viewport: string;
  seconds?: number;
  missing?: string[];
  skipped_checks?: SkippedCheck[];
  error?: string;
  screenshot?: string;
}

/**
 * Values that are secret by their shape, whatever the run knows: credential
 * headers, cookies, bearer and JSON Web Tokens, credential fields in any case
 * style (accessToken, client_secret, secret_value, X-Weave-CSRF), Studio's
 * pairing code and the sign-in's authorization code. Each pattern keeps its
 * name and replaces the value. An ordinary value (a short lowercase word, a
 * small number or a WV- error code) is kept, so "token: expired" and
 * "code=WV-RUN" stay readable.
 */
const ORDINARY = /^(?:[a-z]{1,12}|\d{1,5}|WV-[A-Z0-9-]+)$/;
// An authorization or pairing code can be a plain word: only error codes and numbers are ordinary.
const ORDINARY_CODE = /^(?:\d{1,5}|WV-[A-Z0-9-]+)$/;
const SCHEME = /^(?:bearer|basic|digest|dpop|negotiate|token)\s+/i;
const KEY =
  "(?<![A-Za-z0-9_-])((?:proxy-)?authorization|code|[A-Za-z0-9_-]*?(?:token|secret|password|passwd|api[_-]?key|csrf|code[_-]?verifier|pairing[_-]?code)[A-Za-z0-9_-]*)";
const SEPARATOR = "([\"']?\\s*(?:[:=]|%3[ADad])\\s*)";

function secretValue(value: string, key = ""): boolean {
  const credential = value.replace(SCHEME, "").trim();
  const ordinary = key.toLowerCase() === "code" ? ORDINARY_CODE : ORDINARY;
  return credential.length > 0 && !ordinary.test(credential);
}

const SHAPES: readonly [
  RegExp,
  (match: string, ...groups: string[]) => string,
][] = [
  [
    // A credential header up to the end of its line: Digest and other schemes carry several parameters.
    /\b((?:proxy-)?authorization)(\s*[:=]\s*)(?!["'])([^\r\n]*)/gi,
    (match, name, separator, rest) => {
      const scheme =
        /^((?:bearer|basic|digest|dpop|negotiate|token)\s+)(\S+)/i.exec(rest);
      if (scheme)
        return secretValue(scheme[2])
          ? `${name}${separator}${REDACTED}`
          : match;
      const first = /^\S+/.exec(rest)?.[0] ?? "";
      return secretValue(first)
        ? `${name}${separator}${REDACTED}${rest.slice(first.length)}`
        : match;
    },
  ],
  [
    // Cookies to the end of the header, folded continuation lines included.
    /\b(set-cookie|cookie)(["']?\s*[:=]\s*["']?)[^\n"']+(?:\n[ \t]+[^\n"']*)*/gi,
    (_match, name, separator) => `${name}${separator}${REDACTED}`,
  ],
  [
    // A quoted credential value, spaces and escapes included.
    new RegExp(`${KEY}${SEPARATOR}(["'])((?:\\\\.|(?!\\3)[^\\\\])*)\\3`, "gi"),
    (match, name, separator, quote, value) =>
      secretValue(value, name)
        ? `${name}${separator}${quote}${REDACTED}${quote}`
        : match,
  ],
  [
    // An unquoted credential value: headers, form bodies, query strings and fragments, URL-encoded too.
    new RegExp(`${KEY}${SEPARATOR}((?:[^\\s"'&,;}%]|%(?!26))+)`, "gi"),
    (match, name, separator, value) =>
      secretValue(value, name) ? `${name}${separator}${REDACTED}` : match,
  ],
  [
    /\b(bearer\s+)([A-Za-z0-9._~+/=-]+)/gi,
    (match, prefix, value) =>
      secretValue(value) ? `${prefix}${REDACTED}` : match,
  ],
  [
    /\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*/g,
    () => REDACTED,
  ],
];

/**
 * Every known secret replaced, longest first, so a secret that contains
 * another is replaced whole; then every value that is secret by its shape.
 */
export function redact(text: string, secrets: readonly string[]): string {
  let result = text;
  for (const value of [...secrets]
    .filter((secret) => secret.length > 0)
    .sort((a, b) => b.length - a.length))
    result = result.split(value).join(REDACTED);
  for (const [pattern, replacement] of SHAPES)
    result = result.replace(pattern, replacement);
  return result;
}

/** The first non-empty line of a failure, redacted and at most 300 characters. */
export function errorLine(error: unknown, secrets: readonly string[]): string {
  const text = error instanceof Error ? error.message : String(error);
  const first =
    redact(text, secrets)
      .split("\n")
      .map((line) => line.trim())
      .find((line) => line.length > 0) ?? "failed";
  return first.replace(/^Error: /, "").slice(0, 300);
}

/**
 * The run's canary values, people passwords and secret handles, without empty
 * values.
 */
export function secretsOf(
  canaries: Record<string, string>,
  passwords: readonly (string | undefined)[],
  handles: readonly string[] = [],
): string[] {
  return [...Object.values(canaries), ...passwords, ...handles].filter(
    (value): value is string => typeof value === "string" && value.length > 0,
  );
}

/**
 * The credential-store bindings this run may delete. Without a baseline
 * nothing can tell this run's sign-ins from the person's own, so nothing is
 * deleted.
 */
export function accountsToDelete(
  before: ReadonlySet<string> | undefined,
  now: ReadonlySet<string>,
): string[] {
  if (!before) return [];
  return [...now].filter((account) => !before.has(account)).sort();
}

export function viewportName(
  size: { width: number; height: number } | null,
): string {
  return size ? `${size.width}x${size.height}` : "unknown";
}

/**
 * What a journey's network log must not hold: requests to origins other than
 * the Studio host and the identity provider (the sign-in's loopback callback
 * aside), and mascot artwork.
 */
export function networkProblems(
  urls: readonly string[],
  allowedOrigins: readonly string[],
): string[] {
  const problems: string[] = [];
  for (const url of urls) {
    if (url.startsWith("data:") || url.startsWith("blob:")) continue;
    const parsed = new URL(url);
    if (/\/lumi\.(png|svg)$/i.test(parsed.pathname))
      problems.push(`mascot asset ${parsed.pathname}`);
    const callback =
      parsed.hostname === "127.0.0.1" && parsed.pathname === "/callback";
    if (!callback && !allowedOrigins.includes(parsed.origin))
      problems.push(`request to ${parsed.origin}`);
  }
  return [...new Set(problems)];
}

/**
 * A finished step's status: partial when one of its checks waits for
 * capabilities, and a problem for every enabled check the step never ran.
 */
export function stepOutcome(
  stepId: string,
  steps: Record<string, StepState>,
  ran: ReadonlySet<string>,
  skipped: readonly SkippedCheck[],
): {
  status: "passed" | "partial";
  skippedChecks: SkippedCheck[];
  problems: string[];
} {
  const skippedChecks = [...skipped];
  const problems: string[] = [];
  for (const [id, state] of Object.entries(steps)) {
    if (!id.startsWith(`${stepId}/`) || !state.applies || ran.has(id)) continue;
    if (skippedChecks.some((entry) => entry.check === id)) continue;
    if (state.missing.length)
      skippedChecks.push({ check: id, missing: state.missing });
    else problems.push(`enabled check ${id} has no test`);
  }
  return {
    status: skippedChecks.length ? "partial" : "passed",
    skippedChecks,
    problems,
  };
}
