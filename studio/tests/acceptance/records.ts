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
import { createHash } from "node:crypto";
// Pure helpers of the Studio journey harness: step outcomes, redaction, the
// credential-store guard and the network rules. harness.ts does the I/O.

const REDACTED = "[redacted]";

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
// The exemptions are deliberately narrow; do not widen them. A hand-chosen
// secret that is a lowercase word of at most 12 letters, a number of at most
// 5 digits or a literal WV- code survives the shape layer (password=letmein),
// but no secret this stack generates has those shapes: pairing codes, CSRF
// tokens, platform passwords and identity-provider values are long random
// strings, and canaries are wv-canary-<hex>. Known secrets are replaced
// before the shapes, whatever their form.
const ORDINARY = /^(?:[a-z]{1,12}|\d{1,5}|WV-[A-Z0-9-]+)$/;
// An authorization or pairing code can be a plain word: only error codes and numbers are ordinary.
const ORDINARY_CODE = /^(?:\d{1,5}|WV-[A-Z0-9-]+)$/;
const SCHEME = /^(?:bearer|basic|digest|dpop|negotiate|token)\s+/i;
const KEY =
  "(?<![A-Za-z0-9_-])((?:proxy-)?authorization|code|[A-Za-z0-9_-]*?(?:token|secret|password|passwd|api[_-]?key|csrf|code[_-]?verifier|pairing[_-]?code)[A-Za-z0-9_-]*)";
const SEPARATOR = "([\"']?\\s*(?:[:=]|%3[ADad])\\s*)";
// A quote inside a JSON string is escaped: \" or \'.
const QUOTE = "\\\\?[\"']";
const SECRET_NAME = new RegExp(`^(?:${KEY}|(?:set-)?cookie)$`, "i");

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
    // A private key block up to its end marker, or to the end of a cut-short text: SFTP and TLS keys.
    /(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)/g,
    (_match, begin, end) => `${begin}${REDACTED}${end}`,
  ],
  [
    // A name/value pair, as HAR headers and cookies hold them: {"name":"X-Weave-CSRF","value":"…"}.
    new RegExp(
      `(${QUOTE}name${QUOTE}\\s*:\\s*${QUOTE})([A-Za-z0-9_-]+)(${QUOTE}\\s*,\\s*${QUOTE}value${QUOTE}\\s*:\\s*${QUOTE})((?:(?!${QUOTE})[^\\r\\n])*)`,
      "gi",
    ),
    (match, open, name, separator, value) =>
      SECRET_NAME.test(name) &&
      (/cookie$/i.test(name) || secretValue(value, name))
        ? `${open}${name}${separator}${REDACTED}`
        : match,
  ],
  [
    // A credential field of JSON escaped inside a string: {\"accessToken\":\"…\"}.
    new RegExp(
      `${KEY}(\\\\["']\\s*:\\s*\\\\["'])((?:(?!\\\\["'])[^\\r\\n])*)`,
      "gi",
    ),
    (match, name, separator, value) =>
      secretValue(value, name) ? `${name}${separator}${REDACTED}` : match,
  ],
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
 * The credential-store bindings to delete: the ones this run can prove are
 * its own (`own`, computed from each platform it added) that are still
 * stored. Any other binding, including one another process stores during
 * the run, is never deleted.
 */
export function accountsToDelete(
  own: ReadonlySet<string>,
  now: ReadonlySet<string>,
): string[] {
  return [...now].filter((account) => own.has(account)).sort();
}

/** The login fields a sign-in's credential-store binding is derived from. */
interface Login {
  provider_id: string;
  issuer: string;
  client_id: string;
  target: string;
  account: string;
}

/** A sign-in's binding, as `firefly_weave.sdk.auth.LoginConfig.binding` computes it. */
export function loginBinding(login: Login): string {
  const material = [
    login.provider_id,
    login.issuer,
    login.client_id,
    login.target,
    login.account,
  ];
  return createHash("sha256").update(JSON.stringify(material)).digest("hex");
}

/** The binding of `platform` in a host's saved platforms (`profiles.json`), if it is there. */
export function platformBinding(
  document: unknown,
  platform: string,
): string | undefined {
  const profiles = (
    document as { profiles?: Record<string, { login?: Login }> }
  )?.profiles;
  const login =
    profiles && Object.hasOwn(profiles, platform)
      ? profiles[platform]?.login
      : undefined;
  return login ? loginBinding(login) : undefined;
}

export function viewportName(
  size: { width: number; height: number } | null,
): string {
  return size ? `${size.width}x${size.height}` : "unknown";
}

/**
 * The browser-storage dump of a person's `ordinal`-th sign-in in one journey
 * and viewport: `<journey>-<viewport>-<person>` for the first, with `-<n>`
 * after it for later ones, so no sign-in overwrites another's dump.
 */
export function dumpName(
  journey: string,
  viewport: string,
  person: string,
  ordinal: number,
): string {
  const base = `${journey}-${viewport}-${person}`;
  return ordinal > 1 ? `${base}-${ordinal}` : base;
}

/**
 * Whether the scan checks Studio's storage keys in this run (`check` applies
 * and none of its capabilities is missing). Only then does each dump also list
 * every IndexedDB database with its stores, empty ones included, so the check
 * covers IndexedDB as well as localStorage and sessionStorage.
 */
export function storageKeyCheckOn(
  steps: Readonly<Record<string, StepState>>,
  check: string,
): boolean {
  const state = steps[check];
  return state !== undefined && state.applies && state.missing.length === 0;
}

// scripts/acceptance_evidence.py ACCOUNT_KEY: <profileId>:<subjectHash>:<tenantId>:<projectId>:
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const ACCOUNT_KEY = new RegExp(
  `^p[0-9a-f]{16}:[0-9a-f]{16}:${UUID}:${UUID}:`,
  "i",
);

/** The account keys a storage dump still holds: Web Storage keys, IndexedDB database names and record keys. */
export function storageAccountKeys(dump: {
  local: Record<string, string>;
  session: Record<string, string>;
  indexed_db: { database: string; key: string }[];
  indexed_db_databases?: { name: string }[];
}): number {
  const recordKey = (key: string): string => {
    const value: unknown = JSON.parse(key);
    return typeof value === "string" ? value : "";
  };
  return [
    ...Object.keys(dump.local),
    ...Object.keys(dump.session),
    ...(dump.indexed_db_databases ?? []).map((database) => database.name),
    ...dump.indexed_db.map((record) => recordKey(record.key)),
  ].filter((key) => ACCOUNT_KEY.test(key)).length;
}

/** `value`, frozen all the way down: what a run reads stays what the orchestrator wrote. */
export function deepFreeze<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const item of Object.values(value)) deepFreeze(item);
  }
  return value;
}

/**
 * Why a journey may not record `stepId`, or "" when it may: the step is
 * declared, belongs to the journey and is recorded in the run stage (the scan
 * records its own step).
 */
export function stepRefusal(
  stepId: string,
  journeyId: string,
  steps: Readonly<Record<string, StepState>>,
  stages: Readonly<Record<string, string>>,
): string {
  if (!Object.hasOwn(steps, stepId))
    return `${stepId} is not declared in tests/acceptance/journeys.toml`;
  if (!stepId.startsWith(`${journeyId}.`) || stepId.includes("/"))
    return `${stepId} is not a step of ${journeyId}`;
  if (stages[stepId] !== "run")
    return `${stepId} is recorded by the ${stages[stepId] ?? "unknown"} stage, not by a journey`;
  return "";
}

/** A step screenshot's file name: `<nn>-<slug>.png`, with a slug of lower-case letters, digits and dashes only. */
export function shotName(ordinal: number, slug: string): string {
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug))
    throw Error(`A screenshot slug uses a-z, 0-9 and dashes, not "${slug}"`);
  return `${String(ordinal).padStart(2, "0")}-${slug}.png`;
}

/**
 * Where a person's pages must not have gone: a page that is not http(s)
 * (a data:, blob: or file: page replaces Studio as setContent would), or an
 * origin other than the Studio host and the identity provider (the sign-in's
 * loopback callback aside). The blank page a new tab starts on is fine.
 */
export function navigationProblems(
  urls: readonly string[],
  allowedOrigins: readonly string[],
): string[] {
  const problems: string[] = [];
  for (const url of urls) {
    if (url === "about:blank") continue;
    const parsed = new URL(url);
    const callback =
      parsed.protocol === "http:" &&
      parsed.hostname === "127.0.0.1" &&
      parsed.pathname === "/callback";
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:")
      problems.push(`navigation to ${parsed.protocol}`);
    else if (!callback && !allowedOrigins.includes(parsed.origin))
      problems.push(`navigation to ${parsed.origin}`);
  }
  return [...new Set(problems)];
}

/** A platform's binding from the text of a host's `profiles.json`, or why there is none. */
export function savedBinding(
  text: string | undefined,
  platform: string,
): { binding: string } | { problem: string } {
  if (text === undefined)
    return { problem: "the Studio host saved no platforms file" };
  let document: unknown;
  try {
    document = JSON.parse(text);
  } catch {
    return { problem: "the Studio host's platforms file is unreadable" };
  }
  const binding = platformBinding(document, platform);
  return binding
    ? { binding }
    : { problem: `the Studio host's platforms file has no ${platform}` };
}

/** Why a dump is not the person's Studio storage, or "" when it is. */
export function dumpOriginProblem(origin: string, host: string): string {
  if (!host) return "storage was read before the Studio host started";
  return origin === host
    ? ""
    : `storage was read on ${origin}, not the Studio host ${host}`;
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
      parsed.protocol === "http:" &&
      parsed.hostname === "127.0.0.1" &&
      parsed.pathname === "/callback";
    if (!callback && !allowedOrigins.includes(parsed.origin))
      problems.push(`request to ${parsed.origin}`);
  }
  return [...new Set(problems)];
}

/**
 * A finished step's status: partial when one of its checks waits for
 * capabilities, and a problem for every enabled check the step never ran or
 * recorded as skipped.
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
  const skippedChecks: SkippedCheck[] = [];
  const problems: string[] = [];
  for (const entry of skipped) {
    const state = steps[entry.check] as StepState | undefined;
    if (!entry.missing.length)
      problems.push(
        `check ${entry.check} was skipped without naming what it waits for`,
      );
    else if (state && state.applies && !state.missing.length)
      problems.push(`enabled check ${entry.check} was skipped`);
    else skippedChecks.push(entry);
  }
  for (const [id, state] of Object.entries(steps)) {
    if (!id.startsWith(`${stepId}/`) || !state.applies || ran.has(id)) continue;
    if (skipped.some((entry) => entry.check === id)) continue;
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
