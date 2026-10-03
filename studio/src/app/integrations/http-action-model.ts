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
// Pure model for the API action builder. The local Studio host is the only
// authority that turns a request description into an Action (the Python
// builder shared with the CLI); this module only shapes that description,
// explains obvious mistakes before a round trip, and maps the host's
// diagnostics back to form rows. Checks here mirror the Python rules and are
// kept honest by a parity test that runs them side by side.
import { parse } from "yaml";
import { validVersion } from "../forms/core/identifiers";
import { ApiError } from "../api";
import { describeError } from "../errors";
import { describeDiagnostic, type Severity } from "../designer/diagnostic-copy";

/** The built-in HTTP connector every builder action runs on. */
export const HTTP_CONNECTOR = "weave-http@2.0.0";
/** Adapter name of the built-in HTTP connector descriptor. */
export const HTTP_ADAPTER = "weave-http-v2";

export type Method = "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE";
export const METHODS: readonly Method[] = [
  "GET",
  "POST",
  "PUT",
  "PATCH",
  "DELETE",
  "HEAD",
];
export type ParamLocation = "path" | "query" | "header";
export type ParamType = "string" | "integer" | "boolean";
export type AuthKind =
  | "none"
  | "api-key"
  | "basic"
  | "bearer"
  | "machine-token";

export interface MethodEffect {
  action: "read" | "write";
  sideEffect: "read_only" | "non_idempotent";
  label: string;
  /** What the label means for retries, in plain words. */
  detail: string;
  explanation: string;
}

/** The effect follows the method; there is deliberately no way to choose it. */
export function methodEffect(method: Method): MethodEffect {
  const read = method === "GET" || method === "HEAD";
  const label = read ? "Reads data" : "Changes data";
  const detail = read
    ? `${method === "HEAD" ? "The response has no body, only the status. " : ""}Weave may repeat it after a failure, because reading again changes nothing.`
    : "Weave sends it once and never retries it automatically, because a repeated request could apply the change twice.";
  return {
    action: read ? "read" : "write",
    sideEffect: read ? "read_only" : "non_idempotent",
    label,
    detail,
    explanation: `${label}. ${detail}`,
  };
}
export const isRead = (method: Method) => method === "GET" || method === "HEAD";

// Mirrors of the Python contract patterns (contracts/definitions.py and
// contracts/http_profiles.py); the parity test compares them case by case.

const RESOURCE_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const PARAMETER_NAME = /^[A-Za-z_][A-Za-z0-9_-]{0,127}$/;
const HEADER_NAME = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
const PLACEHOLDER = /^\{[A-Za-z_][A-Za-z0-9_-]{0,127}\}$/;
const HOST =
  /^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?|[0-9A-Fa-f:.]{2,45})$/;
const PROTECTED = new Set([
  "authorization",
  "proxy-authorization",
  "host",
  "cookie",
  "set-cookie",
  "content-length",
  "content-type",
  "accept",
  "connection",
  "transfer-encoding",
  "te",
  "trailer",
  "upgrade",
  "forwarded",
  "via",
]);

export const validName = (name: string) =>
  name.length <= 128 && RESOURCE_NAME.test(name);
export { validVersion } from "../forms/core/identifiers";
/** Headers the executor sets itself or that would carry credentials. */
export function isProtectedHeader(name: string) {
  const lower = name.toLowerCase();
  return (
    PROTECTED.has(lower) ||
    lower.startsWith("proxy-") ||
    lower.startsWith("x-forwarded-")
  );
}
const invisible = (text: string) =>
  [...text].some((c) => c.charCodeAt(0) <= 32 || c.charCodeAt(0) >= 127);

/** Placeholder names of a path template, or why the profile rejects it. */
export function pathPlaceholders(
  path: string,
): { names: string[] } | { error: string } {
  if (!path.startsWith("/") || path.startsWith("//"))
    return {
      error:
        "The path must start with a single /, for example /v1/pets/{petId}.",
    };
  if (path.length > 4096)
    return { error: "The path is longer than 4,096 characters." };
  if (/[%?#\\]/.test(path))
    return {
      error:
        "The path can't contain %, ?, # or \\. Add query values as query parameters below.",
    };
  if (invisible(path))
    return { error: "The path can only use visible ASCII characters." };
  const names: string[] = [];
  for (const part of path.slice(1).split("/")) {
    if (part === "." || part === "..")
      return { error: "The path can't contain . or .. segments." };
    if (part.includes("{") || part.includes("}")) {
      if (!PLACEHOLDER.test(part))
        return {
          error:
            "Write each placeholder as a whole segment, such as {petId}: letters, digits, _ or -, starting with a letter or _.",
        };
      names.push(part.slice(1, -1));
    }
  }
  if (new Set(names).size !== names.length)
    return { error: "Each placeholder can appear only once." };
  return { names };
}

/**
 * The HTTPS origin a connection keeps, plus any base path the person typed
 * (which belongs in the action's path). Mirrors fixed_server and the
 * connection destination check.
 */
export function parseApiAddress(
  input: string,
): { origin: string; basePath: string } | { error: string } {
  const value = input.trim();
  const fail = (error: string) => ({ error });
  if (!value)
    return fail("Enter the API address, for example https://api.example.com.");
  const scheme = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(value);
  if (!scheme || scheme[1].toLowerCase() !== "https")
    return fail(
      "Use an https:// address. Weave only calls APIs over HTTPS, so credentials never travel unencrypted.",
    );
  if (/[%\\]/.test(value) || invisible(value))
    return fail(
      "The address can only use visible ASCII characters, without % or \\.",
    );
  let rest = value.slice(scheme[0].length);
  const hash = rest.indexOf("#");
  const fragment = hash >= 0 ? rest.slice(hash + 1) : "";
  if (hash >= 0) rest = rest.slice(0, hash);
  const mark = rest.indexOf("?");
  const query = mark >= 0 ? rest.slice(mark + 1) : "";
  if (mark >= 0) rest = rest.slice(0, mark);
  if (query || fragment)
    return fail(
      "Remove the ? or # part. Query values become query parameters of the action.",
    );
  if (!rest.startsWith("//"))
    return fail(
      "Enter the address as https://host, for example https://api.example.com.",
    );
  rest = rest.slice(2);
  const slash = rest.indexOf("/");
  const netloc = slash >= 0 ? rest.slice(0, slash) : rest;
  const path = slash >= 0 ? rest.slice(slash) : "";
  if (netloc.includes("@"))
    return fail(
      "Remove the user name or password from the address. Credentials belong to the connection.",
    );
  let host = netloc;
  let port: string | null = null;
  if (netloc.startsWith("[")) {
    const close = netloc.indexOf("]");
    if (close < 0) return fail("The address has an unclosed [ in its host.");
    host = netloc.slice(1, close);
    const after = netloc.slice(close + 1);
    if (after && !after.startsWith(":"))
      return fail("The address host is not valid.");
    port = after ? after.slice(1) : null;
  } else {
    const colon = netloc.indexOf(":");
    if (colon >= 0) {
      host = netloc.slice(0, colon);
      port = netloc.slice(colon + 1);
    }
  }
  if (!host || host.endsWith(".") || !HOST.test(host))
    return fail(
      "Enter a host name such as api.example.com, without wildcards or a trailing dot.",
    );
  if (port) {
    if (!/^[0-9]+$/.test(port) || Number(port) > 65535 || Number(port) === 0)
      return fail("The port must be a number from 1 to 65535.");
  }
  const basePath = path.replace(/\/+$/, "");
  if (
    basePath
      .split("/")
      .slice(1)
      .some((part) => part === "" || part === "." || part === "..") ||
    /[{}]/.test(basePath)
  )
    return fail(
      "The address path has empty, . or .. segments or a placeholder. Put the path in the Path field instead.",
    );
  return { origin: `https://${netloc}`, basePath };
}

export interface ParamRow {
  id: string;
  location: ParamLocation;
  name: string;
  type: ParamType;
  /** Query parameters only: the value is a list (sent as repeated keys). */
  list: boolean;
  required: boolean;
}
export interface StatusRow {
  id: string;
  code: string;
  /** Chosen "no response body"; 204, 205 and HEAD are always empty. */
  empty: boolean;
}
export interface HttpActionDraft {
  name: string;
  version: string;
  description: string;
  method: Method;
  /** API address; only its origin is kept, for the connection. */
  address: string;
  path: string;
  parameters: ParamRow[];
  body: {
    enabled: boolean;
    mode: "sample" | "schema";
    text: string;
    required: boolean;
  };
  response: { mode: "sample" | "schema" | "none"; text: string };
  statuses: StatusRow[];
  timeout: string;
  stringMaxLength: string;
  /** Connection authentication, used to block the API-key header and to prefill the connection. */
  auth: { kind: AuthKind; header: string };
}

export function blankDraft(newId: () => string): HttpActionDraft {
  return {
    name: "",
    version: "1.0.0",
    description: "",
    method: "GET",
    address: "",
    path: "",
    parameters: [],
    body: { enabled: false, mode: "sample", text: "", required: true },
    response: { mode: "sample", text: "" },
    statuses: [{ id: newId(), code: "200", empty: false }],
    timeout: "30",
    stringMaxLength: "256",
    auth: { kind: "none", header: "" },
  };
}

/**
 * Keeps one required path row per placeholder, in path order, ahead of the
 * query and header rows. Returns the same array when nothing changes.
 */
export function syncPathRows(
  rows: readonly ParamRow[],
  names: readonly string[],
  newId: () => string,
): ParamRow[] {
  const current = rows.filter((r) => r.location === "path");
  const others = rows.filter((r) => r.location !== "path");
  const next = names.map(
    (name) =>
      current.find((r) => r.name === name) ?? {
        id: newId(),
        location: "path" as const,
        name,
        type: "string" as const,
        list: false,
        required: true,
      },
  );
  const same =
    next.length === current.length &&
    next.every((r, i) => r === current[i]) &&
    rows.slice(0, current.length).every((r) => r.location === "path");
  return same ? (rows as ParamRow[]) : [...next, ...others];
}

export function emptyStatusFor(
  method: Method,
  code: number,
  chosen: boolean,
): { empty: boolean; forced: boolean } {
  const forced = method === "HEAD" || code === 204 || code === 205;
  return { empty: forced || chosen, forced };
}

export type FieldKey =
  | "name"
  | "version"
  | "description"
  | "method"
  | "address"
  | "path"
  | "parameters"
  | "body"
  | "response"
  | "statuses"
  | "timeout"
  | "stringMaxLength"
  | "auth"
  | "general";
/** FieldIssue.code of an empty required value. */
export const REQUIRED = "required";
export interface FieldIssue {
  field: FieldKey;
  row?: string;
  severity: Severity;
  message: string;
  hint?: string;
  code?: string;
}
/** Request array index → form row id, so diagnostics land on the right row. */
export interface RowMap {
  parameters: string[];
  statuses: string[];
  empty: string[];
}
export interface RequestBuild {
  /** The host request, or null while an error blocks it. */
  request: Record<string, unknown> | null;
  issues: FieldIssue[];
  map: RowMap;
}

const SAMPLE_LIMIT = 256 * 1024;
/** Most parameters one action can declare (the host's HttpActionRequest bound). */
export const PARAMETER_LIMIT = 64;

/** Parses a pasted JSON or YAML value; YAML aliases and non-JSON values are refused. */
export function parseSample(
  text: string,
): { value: unknown } | { error: string } {
  if (!text.trim()) return { error: "Paste a JSON or YAML value." };
  if (text.length > SAMPLE_LIMIT)
    return {
      error: "This is larger than 256 KB. Paste a shorter example.",
    };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    try {
      value = parse(text, { maxAliasCount: 0, prettyErrors: true });
    } catch (e) {
      const line = (e as { linePos?: { line: number }[] }).linePos?.[0]?.line;
      return {
        error: `This isn't valid JSON or YAML${line ? ` (line ${line})` : ""}.`,
      };
    }
  }
  return plainJson(value, 0)
    ? { value }
    : {
        error:
          "This contains values JSON can't represent, such as dates, aliases or infinite numbers.",
      };
}
function plainJson(value: unknown, depth: number): boolean {
  if (depth > 64) return false;
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return true;
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every((v) => plainJson(v, depth + 1));
  if (
    typeof value === "object" &&
    Object.getPrototypeOf(value) === Object.prototype
  )
    return Object.values(value as object).every((v) => plainJson(v, depth + 1));
  return false;
}
const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

function wholeNumber(text: string, low: number, high: number): number | null {
  const value = Number(String(text).trim());
  return /^\s*\d+\s*$/.test(String(text)) && value >= low && value <= high
    ? value
    : null;
}

/** Shapes the host request; errors found here are explained without a round trip. */
export function buildRequest(draft: HttpActionDraft): RequestBuild {
  const issues: FieldIssue[] = [];
  const error = (field: FieldKey, message: string, row?: string) =>
    issues.push({ field, message, severity: "error", ...(row ? { row } : {}) });
  /** An empty required value: forms show it only once the person has left the field. */
  const missing = (field: FieldKey, message: string, row?: string) =>
    issues.push({
      field,
      message,
      severity: "error",
      code: REQUIRED,
      ...(row ? { row } : {}),
    });
  const name = draft.name.trim();
  if (!name) missing("name", "Enter a name, for example pets.get-pet.");
  else if (!validName(name))
    error(
      "name",
      "Use letters, digits, dots, hyphens or underscores, starting with a letter or digit.",
    );
  const version = draft.version.trim();
  if (!validVersion(version)) error("version", "Use a version such as 1.0.0.");
  const description = draft.description.trim();
  if (description.length > 1024)
    error("description", "Keep the description under 1,024 characters.");
  const path = draft.path.trim();
  const placeholders = path ? pathPlaceholders(path) : null;
  if (!placeholders)
    missing("path", "Enter the path, for example /v1/pets/{petId}.");
  else if ("error" in placeholders) error("path", placeholders.error);
  const authHeader =
    draft.auth.kind === "api-key" ? draft.auth.header.trim().toLowerCase() : "";
  const ordered = [
    ...draft.parameters.filter((r) => r.location === "path"),
    ...draft.parameters.filter((r) => r.location !== "path"),
  ];
  const seen = new Set<string>();
  const parameters: Record<string, unknown>[] = [];
  const map: RowMap = { parameters: [], statuses: [], empty: [] };
  for (const row of ordered) {
    const rowName = row.name.trim();
    const key = `${row.location}:${row.location === "header" ? rowName.toLowerCase() : rowName}`;
    if (!rowName) {
      missing("parameters", "Enter a name for this parameter.", row.id);
      continue;
    }
    if (row.location === "header") {
      if (rowName.length > 128 || !HEADER_NAME.test(rowName)) {
        error(
          "parameters",
          "Header names use letters, digits and - only, such as X-Request-Id.",
          row.id,
        );
        continue;
      }
      if (isProtectedHeader(rowName)) {
        error(
          "parameters",
          "Weave sets this header itself or takes it from the connection's sign-in. Remove it.",
          row.id,
        );
        continue;
      }
      if (authHeader && rowName.toLowerCase() === authHeader) {
        error(
          "parameters",
          "This header carries the API key from the connection. Remove it.",
          row.id,
        );
        continue;
      }
    } else if (!PARAMETER_NAME.test(rowName)) {
      error(
        "parameters",
        "Use letters, digits, _ or -, starting with a letter or _.",
        row.id,
      );
      continue;
    }
    if (seen.has(key)) {
      error(
        "parameters",
        "This name is already used by another parameter.",
        row.id,
      );
      continue;
    }
    seen.add(key);
    parameters.push({
      name: rowName,
      location: row.location,
      type: row.type,
      array: row.location === "query" && row.list,
      required: row.location === "path" || row.required,
    });
    map.parameters.push(row.id);
  }
  if (parameters.length > PARAMETER_LIMIT)
    error(
      "parameters",
      `An action can have at most ${PARAMETER_LIMIT} parameters. Remove some, or split the request into two actions.`,
    );
  const request: Record<string, unknown> = {
    name,
    version,
    method: draft.method,
    pathTemplate: path,
    parameters,
  };
  if (description) request["description"] = description;
  if (!isRead(draft.method) && draft.body.enabled) {
    const sample = parseSample(draft.body.text);
    if (!draft.body.text.trim())
      missing(
        "body",
        "Paste an example body or a JSON Schema, or turn off the body.",
      );
    else if ("error" in sample) error("body", sample.error);
    else if (draft.body.mode === "schema" && !isObject(sample.value))
      error("body", 'A JSON Schema is an object, such as {"type": "object"}.');
    else {
      request[draft.body.mode === "schema" ? "bodySchema" : "bodySample"] =
        sample.value;
      request["bodyRequired"] = draft.body.required;
    }
  }
  if (draft.method !== "HEAD" && draft.response.mode !== "none") {
    const text = draft.response.text;
    if (text.trim()) {
      const sample = parseSample(text);
      if ("error" in sample) error("response", sample.error);
      else if (draft.response.mode === "schema" && !isObject(sample.value))
        error(
          "response",
          'A JSON Schema is an object, such as {"type": "object"}.',
        );
      else
        request[
          draft.response.mode === "schema" ? "responseSchema" : "responseSample"
        ] = sample.value;
    }
  }
  const statuses: number[] = [];
  const empty: number[] = [];
  for (const row of draft.statuses) {
    const code = wholeNumber(row.code, 200, 299);
    if (code === null) {
      error("statuses", "Use a success status from 200 to 299.", row.id);
      continue;
    }
    if (statuses.includes(code)) {
      error("statuses", "This status is listed twice.", row.id);
      continue;
    }
    statuses.push(code);
    map.statuses.push(row.id);
    if (emptyStatusFor(draft.method, code, row.empty).empty) {
      empty.push(code);
      map.empty.push(row.id);
    }
  }
  if (!draft.statuses.length)
    error("statuses", "Add at least one success status, such as 200.");
  request["statuses"] = statuses;
  request["emptyStatuses"] = empty;
  const timeout = wholeNumber(draft.timeout, 1, 30);
  if (timeout === null) error("timeout", "Choose from 1 to 30 seconds.");
  else request["timeoutSeconds"] = timeout;
  const longest = wholeNumber(draft.stringMaxLength, 1, 4096);
  if (longest === null)
    error("stringMaxLength", "Choose from 1 to 4,096 characters.");
  else if (longest !== 256) request["stringMaxLength"] = longest;
  if (
    draft.auth.kind === "api-key" &&
    apiKeyHeaderIssue(draft.auth.header) === ""
  )
    request["auth"] = { kind: "api-key", header: draft.auth.header.trim() };
  const blocked = issues.some((i) => i.severity === "error");
  return { request: blocked ? null : request, issues, map };
}

function apiKeyHeaderIssue(header: string) {
  const value = header.trim();
  if (!value)
    return "Enter the header that carries the API key, such as X-API-Key.";
  if (value.length > 128 || !HEADER_NAME.test(value))
    return "Header names use letters, digits and - only, such as X-API-Key.";
  if (isProtectedHeader(value))
    return "This header can't carry an API key. Choose a custom header such as X-API-Key.";
  return "";
}

/** Address and authentication problems; they never block the action itself. */
export function connectionIssues(draft: HttpActionDraft): FieldIssue[] {
  const issues: FieldIssue[] = [];
  if (draft.address.trim()) {
    const parsed = parseApiAddress(draft.address);
    if ("error" in parsed)
      issues.push({
        field: "address",
        severity: "error",
        message: parsed.error,
      });
    else if (parsed.basePath)
      issues.push({
        field: "address",
        severity: "warning",
        message: `The connection keeps only ${parsed.origin}. Move ${parsed.basePath} to the start of the path.`,
      });
  }
  if (draft.auth.kind === "api-key") {
    const problem = apiKeyHeaderIssue(draft.auth.header);
    if (problem)
      issues.push({ field: "auth", severity: "error", message: problem });
  }
  return issues;
}

const unescapePointer = (part: string) =>
  part.replace(/~1/g, "/").replace(/~0/g, "~");

/** Where a request or Action pointer belongs in the form. */
export function locateDiagnostic(
  pointer: string,
  map: RowMap,
  params: readonly ParamRow[] = [],
): { field: FieldKey; row?: string } {
  const parts = pointer.split("/").slice(1).map(unescapePointer);
  const at = (rows: string[], index: string | undefined) => {
    const i = Number(index);
    return Number.isInteger(i) && i >= 0 && i < rows.length
      ? rows[i]
      : undefined;
  };
  const withRow = (field: FieldKey, row: string | undefined) =>
    row ? { field, row } : { field };
  const [first, second, third, fourth, fifth, sixth] = parts;
  switch (first) {
    case "name":
    case "version":
    case "description":
    case "method":
      return { field: first };
    case "pathTemplate":
      return { field: "path" };
    case "parameters":
      return withRow("parameters", at(map.parameters, second));
    case "bodySample":
    case "bodySchema":
    case "bodyRequired":
      return { field: "body" };
    case "responseSample":
    case "responseSchema":
      return { field: "response" };
    case "statuses":
      return withRow("statuses", at(map.statuses, second));
    case "emptyStatuses":
      return withRow("statuses", at(map.empty, second));
    case "timeoutSeconds":
      return { field: "timeout" };
    case "stringMaxLength":
      return { field: "stringMaxLength" };
    case "auth":
      return { field: "auth" };
    case "metadata":
      return second === "name" || second === "version"
        ? { field: second }
        : { field: "general" };
    case "spec":
      break;
    default:
      return { field: "general" };
  }
  switch (second) {
    case "timeoutSeconds":
      return { field: "timeout" };
    case "sideEffect":
    case "retry":
      return { field: "method" };
    case "outputSchema":
      return { field: "response" };
    case "inputSchema": {
      if (third !== "properties" || !fourth) return { field: "parameters" };
      if (fourth === "body") return { field: "body" };
      const location =
        fourth === "headers" ? "header" : fourth === "path" ? "path" : "query";
      const row =
        fifth === "properties" && sixth
          ? params.find(
              (p) =>
                p.location === location &&
                (location === "header"
                  ? p.name.toLowerCase() === sixth.toLowerCase()
                  : p.name === sixth),
            )
          : undefined;
      return withRow("parameters", row?.id);
    }
    case "implementation":
      if (third === "action") return { field: "method" };
      if (third !== "config") return { field: "general" };
      switch (fourth) {
        case "path":
          return { field: "path" };
        case "method":
        case "sideEffect":
          return { field: "method" };
        case "parameters":
          return withRow("parameters", at(map.parameters, fifth));
        case "statuses":
          return withRow("statuses", at(map.statuses, fifth));
        case "emptyStatuses":
          return withRow("statuses", at(map.empty, fifth));
        default:
          return { field: "general" };
      }
    default:
      return { field: "general" };
  }
}

export interface DiagnosticLike {
  code?: unknown;
  message?: unknown;
  severity?: unknown;
  hint?: unknown;
  path?: unknown;
}
export interface Explained {
  severity: Severity;
  text: string;
  hint?: string;
  code: string;
  technical: string;
}
const CONTRACT = /^WV-COMP-[A-Z_]+_CONTRACT$/;
const SPECIFIC = /^WV-(HTTP-ACTION|HTTP-CONNECTION|IMPORT)-/;

/**
 * Plain explanation of a host or platform diagnostic. Builder and importer
 * messages are written for authors and are value-free, so they are shown as
 * they are; other compiler codes use Studio's plain copy.
 */
export function explainDiagnostic(diagnostic: DiagnosticLike): Explained {
  const code = typeof diagnostic.code === "string" ? diagnostic.code : "";
  const message =
    typeof diagnostic.message === "string" ? diagnostic.message.trim() : "";
  const hint =
    typeof diagnostic.hint === "string" && diagnostic.hint.trim()
      ? plainRelaxationText(diagnostic.hint.trim())
      : undefined;
  const severity: Severity =
    diagnostic.severity === "warning" || diagnostic.severity === "info"
      ? diagnostic.severity
      : "error";
  if (message && (SPECIFIC.test(code) || CONTRACT.test(code)))
    return {
      severity,
      text: plainRelaxationText(message),
      code,
      technical: message,
      ...(hint ? { hint } : {}),
    };
  const plain = describeDiagnostic(diagnostic);
  const shownHint = hint ?? plain.hint;
  return {
    severity,
    text: plainRelaxationText(plain.text),
    code,
    technical: message,
    ...(shownHint ? { hint: shownHint } : {}),
  };
}

/** Next free patch version, used when a version already exists with other content. */
export function nextPatchVersion(
  version: string,
  taken: Iterable<string>,
): string {
  const used = new Set(taken);
  const core = /^(\d+)\.(\d+)\.(\d+)/.exec(version);
  let [major, minor, patch] = core
    ? [Number(core[1]), Number(core[2]), Number(core[3]) + 1]
    : [1, 0, 1];
  while (used.has(`${major}.${minor}.${patch}`)) patch++;
  return `${major}.${minor}.${patch}`;
}

/** A free patch version for this action, given the name@version references already published. */
export function suggestVersion(
  name: string,
  version: string,
  published: readonly string[],
): string {
  const taken = published
    .filter((ref) => ref.startsWith(`${name}@`))
    .map((ref) => ref.slice(name.length + 1));
  return nextPatchVersion(version, [...taken, version]);
}

/** The platform already holds this name@version with different content. */
export const IMMUTABLE_VERSION = "WV-COMP-IMMUTABLE_VERSION";
const CONNECTOR_CODES = new Set([
  "WV-COMP-UNKNOWN_CONNECTOR",
  "WV-COMP-UNKNOWN_ADAPTER",
]);
export const versionTaken = (diagnostics: readonly DiagnosticLike[]) =>
  diagnostics.some((d) => d.code === IMMUTABLE_VERSION);
/** The project can't resolve the built-in HTTP connector yet: show readiness, not compiler text. */
export const needsConnector = (diagnostics: readonly DiagnosticLike[]) =>
  diagnostics.some((d) => CONNECTOR_CODES.has(String(d.code)));

/** The Studio host's request limit (studio/service.py BODY_LIMIT). */
export const HOST_BODY_LIMIT = 2 * 1024 * 1024;
/** Room kept for the rest of an OpenAPI request: format, options, selection and name. */
const ENVELOPE_BYTES = 64 * 1024;
/**
 * Whether OpenAPI text still fits the host's request once it is encoded as a
 * JSON string in UTF-8: quotes, backslashes, newlines and non-ASCII
 * characters take more room on the wire than in the text box.
 */
export function fitsHostRequest(text: string): boolean {
  const room = HOST_BODY_LIMIT - ENVELOPE_BYTES;
  // One UTF-16 unit never takes more than 6 bytes (\u0001), so short text always fits.
  if (text.length * 6 <= room) return true;
  return new TextEncoder().encode(JSON.stringify(text)).length <= room;
}

const slugify = (text: string) =>
  text
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 63)
    .replace(/-$/, "");

/** Connection slot name for a service: its host name, else the action's first name segment. */
export function serviceSlug(origin: string, fallbackName: string): string {
  const parsed = origin ? parseApiAddress(origin) : null;
  if (parsed && "origin" in parsed) {
    const host = parsed.origin
      .slice("https://".length)
      .replace(/:\d*$/, "")
      .toLowerCase();
    if (!host.startsWith("[") && !/^[0-9.]+$/.test(host)) {
      const labels = host.split(".").filter(Boolean);
      while (labels.length > 1 && (labels[0] === "www" || labels[0] === "api"))
        labels.shift();
      const slug = slugify(labels[0] ?? "");
      if (slug) return slug;
    }
  }
  return slugify(fallbackName.split(".")[0] ?? "") || "api";
}

/** OpenAPI text format from the file name, else from the first character. */
export function detectFormat(fileName: string, text: string): "json" | "yaml" {
  const lower = fileName.toLowerCase();
  if (lower.endsWith(".json")) return "json";
  if (lower.endsWith(".yaml") || lower.endsWith(".yml")) return "yaml";
  return /^\s*[{[]/.test(text) ? "json" : "yaml";
}

export interface Relaxations {
  defaultStringMaxLength?: number;
  numericFormats?: boolean;
  ignoreResponseHeaders?: boolean;
  jsonMediaOnly?: boolean;
  upgradeOpenapi30?: boolean;
}
export type RelaxationKey = keyof Relaxations;
/** Opt-in departures from the strict import; each one applied shows as a warning. */
export const RELAXATIONS: Record<
  RelaxationKey,
  { label: string; description: string }
> = {
  defaultStringMaxLength: {
    label: "Give text values without a length limit a default limit",
    description:
      "Weave needs a maximum length for text parameters. Values without one get the limit you choose.",
  },
  numericFormats: {
    label: "Turn int32 and int64 formats into number limits",
    description:
      "They become minimum and maximum values. float and double formats are dropped.",
  },
  ignoreResponseHeaders: {
    label: "Ignore response headers",
    description: "Actions return the status and the body only.",
  },
  jsonMediaOnly: {
    label: "Use only JSON when other content types are listed",
    description:
      "Other content types, such as XML, are dropped. The operation must offer application/json.",
  },
  upgradeOpenapi30: {
    label: "Read OpenAPI 3.0 documents as 3.1",
    description:
      "Rewrites nullable, exclusive limits and examples to their 3.1 form.",
  },
};
export const RELAXATION_KEYS = Object.keys(RELAXATIONS) as RelaxationKey[];

export function relaxationPayload(state: Relaxations): Relaxations | undefined {
  const out: Relaxations = {};
  for (const key of RELAXATION_KEYS) {
    const value = state[key];
    if (key === "defaultStringMaxLength") {
      if (typeof value === "number") out.defaultStringMaxLength = value;
    } else if (value === true) (out as Record<string, unknown>)[key] = true;
  }
  return Object.keys(out).length ? out : undefined;
}

/** Relaxations a reason's text points to, in order of appearance. */
export function suggestedRelaxations(
  reasons: readonly DiagnosticLike[],
): RelaxationKey[] {
  const found: RelaxationKey[] = [];
  for (const reason of reasons)
    for (const text of [reason.hint, reason.message])
      if (typeof text === "string")
        for (const match of text.matchAll(/relaxations\.(\w+)/g)) {
          const key = match[1] as RelaxationKey;
          if (key in RELAXATIONS && !found.includes(key)) found.push(key);
        }
  return found;
}

/** Replaces policy keys such as relaxations.jsonMediaOnly with the option's label. */
export function plainRelaxationText(text: string): string {
  const label = (key: string) =>
    key in RELAXATIONS ? `“${RELAXATIONS[key as RelaxationKey].label}”` : key;
  return text
    .replace(
      /\b([Ss])et relaxations\.(\w+)/g,
      (_, s: string, key: string) =>
        `${s === "S" ? "Turn" : "turn"} on ${label(key)}`,
    )
    .replace(/relaxations\.(\w+)/g, (_, key: string) => label(key));
}

const METHOD_KEYS = new Set([
  "get",
  "put",
  "post",
  "delete",
  "options",
  "head",
  "patch",
  "trace",
]);
/** "GET /pets/{petId}" for a pointer into an OpenAPI operation, else null. */
export function operationForPointer(pointer: string): string | null {
  const parts = pointer.split("/").slice(1).map(unescapePointer);
  if (parts[0] !== "paths" || parts.length < 3 || !METHOD_KEYS.has(parts[2]))
    return null;
  return `${parts[2].toUpperCase()} ${parts[1]}`;
}

export interface OperationLike {
  key: string;
  operationId?: string | null;
  method: string;
  path: string;
  summary?: string | null;
  tags?: readonly string[];
}
/** Operations whose key, method, path, summary or tags contain every word. */
export function filterOperations<T extends OperationLike>(
  operations: readonly T[],
  query: string,
): T[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [...operations];
  return operations.filter((op) => {
    const text = [
      op.key,
      op.operationId ?? "",
      op.method,
      op.path,
      op.summary ?? "",
      ...(op.tags ?? []),
    ]
      .join(" ")
      .toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

export type PublishFailureKind =
  | "version"
  | "key"
  | "compile"
  | "denied"
  | "unknown"
  | "rejected";
export interface PublishFailure {
  kind: PublishFailureKind;
  message: string;
  diagnostics?: DiagnosticLike[];
  code?: string;
}

/**
 * Sorts a publish failure. "unknown" means the request may have reached the
 * platform: the caller keeps its idempotency key and offers to check again.
 */
export function classifyPublishError(error: unknown): PublishFailure {
  if (error instanceof ApiError) {
    const code = error.code;
    if (error.status === 409 && code === "WV-VERSION-CONFLICT")
      return {
        kind: "version",
        code,
        message:
          "This version is already published with different content. Published versions can't change.",
      };
    if (error.status === 409 && code === "WV-IDEMPOTENCY-CONFLICT")
      return {
        kind: "key",
        code,
        message:
          "The platform saw this request before with different content. Publish again to send it as a new request.",
      };
    if (error.status === 422 && code === "WV-COMPILE") {
      const detail = isObject(error.detail) ? error.detail : {};
      const result = isObject(detail["result"]) ? detail["result"] : {};
      const diagnostics = Array.isArray(result["diagnostics"])
        ? (result["diagnostics"] as DiagnosticLike[])
        : [];
      return {
        kind: "compile",
        code,
        diagnostics,
        message: "The platform found problems in this action.",
      };
    }
    if (error.status === 403)
      return {
        kind: "denied",
        code,
        message:
          "Your account can't publish actions in this workspace. Download the file and ask a developer to publish it.",
      };
    if (error.status >= 500 || error.status === 408)
      return { kind: "unknown", code, message: unknownMessage };
    return { kind: "rejected", code, message: error.plain.message };
  }
  if (
    (error instanceof DOMException &&
      (error.name === "TimeoutError" || error.name === "AbortError")) ||
    (error instanceof TypeError && /fetch|network/i.test(error.message))
  )
    return { kind: "unknown", message: unknownMessage };
  return { kind: "rejected", message: describeError(error).message };
}
const unknownMessage =
  "Studio couldn't confirm whether this was published. Check again: Studio resends the same request, so nothing is published twice.";
