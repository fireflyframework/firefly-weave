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
// The model behind the weave-http@2.0.0 connection form: field names exactly
// as the platform's AuthProfile and ConnectionRequest, client checks that
// mirror the server (contracts/http_profiles.py AuthProfile and
// fixed_server, contracts/http_profile_checks.py connection_issues and
// connections/diagnostics.py), WV-CONNECTION diagnostics mapped to form
// fields, plain copy for this context, and the administrator hand-off. Pure.
//
// Secrets: the form only ever holds secret handle NAMES. Values that look
// like secrets are refused before anything is sent or shown for hand-off.
import type { ScopeProfile } from "./readiness";

export type AuthKind =
  | "none"
  | "api-key"
  | "basic"
  | "bearer"
  | "machine-token";
export type ClientAuthentication = "client_secret_post" | "client_secret_basic";

export const authKinds: { value: AuthKind; label: string; hint: string }[] = [
  {
    value: "none",
    label: "No authentication",
    hint: "The API is public; no secret is needed.",
  },
  {
    value: "api-key",
    label: "API key in a header",
    hint: "The key is sent in a header you name, such as X-API-Key.",
  },
  {
    value: "basic",
    label: "Username and password",
    hint: "HTTP basic authentication with two stored secrets.",
  },
  {
    value: "bearer",
    label: "Bearer token",
    hint: "A stored token is sent as Authorization: Bearer.",
  },
  {
    value: "machine-token",
    label: "OAuth client credentials",
    hint: "The platform gets a token from your identity provider with a stored client secret.",
  },
];
/** Secret slots per authentication kind, as AuthProfile.slots(). */
export const authSlots: Record<AuthKind, string[]> = {
  none: [],
  "api-key": ["api_key"],
  basic: ["username", "password"],
  bearer: ["token"],
  "machine-token": ["client_secret"],
};
export const slotLabels: Record<string, string> = {
  api_key: "API key",
  username: "Username",
  password: "Password",
  token: "Bearer token",
  client_secret: "Client secret",
};

export interface ConnectionDraft {
  name: string;
  /** The API's HTTPS or HTTP origin, such as https://api.example.com. */
  origin: string;
  auth: AuthKind;
  header: string;
  clientId: string;
  endpoint: string;
  /** Scopes separated by spaces or commas. */
  scopes: string;
  authentication: ClientAuthentication;
  /** Secret handle NAME per slot; never a value. */
  secrets: Record<string, string>;
  /** Further literal HTTPS or HTTP origins the connection may reach. */
  extraDestinations: string[];
}
/** The JSON body of connections.create (ConnectionRequest by alias). */
export interface ConnectionRequestBody {
  name: string;
  connector_version_id: string;
  config: { baseUrl: string; auth: Record<string, unknown> };
  secretRef: Record<string, string>;
  allowed_destinations: string[];
}
/** A problem attached to a form field: "name", "origin", "auth", "header", "clientId",
 * "endpoint", "scopes", "authentication", "secret.<slot>", "secrets",
 * "destination.<i>", "destinations", "connector" or "general". */
export interface FieldProblem {
  field: string;
  message: string;
  code?: string;
}

const protectedHeaders = new Set([
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
const headerName = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
const resourceName = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const localHandle = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const scopePattern = /^[\x21\x23-\x5b\x5d-\x7e]{1,256}$/;
const pointerPattern = /^(\/([^~/]|~[01])*)*$/;

export function emptyDraft(
  prefill: Partial<ConnectionDraft> = {},
): ConnectionDraft {
  return {
    name: "",
    origin: "",
    auth: "none",
    header: "",
    clientId: "",
    endpoint: "",
    scopes: "",
    authentication: "client_secret_post",
    secrets: {},
    extraDestinations: [],
    ...prefill,
  };
}

/** What the API action builder hands over for the connection: names and the origin only. */
export interface BuilderConnection {
  name?: string | null;
  baseUrl?: string | null;
  auth?: { kind: AuthKind; header?: string | null } | null;
}

/** Form prefill from the API action builder's hand-off. */
export function prefillFromBuilder(
  connection: BuilderConnection | null | undefined,
): Partial<ConnectionDraft> {
  if (!connection) return {};
  const prefill: Partial<ConnectionDraft> = {};
  if (connection.name) prefill.name = connection.name;
  if (connection.baseUrl) prefill.origin = connection.baseUrl;
  const kind = connection.auth?.kind;
  if (kind && kind in authSlots) prefill.auth = kind;
  if (kind === "api-key" && connection.auth?.header)
    prefill.header = connection.auth.header;
  return prefill;
}

/** A lowercase service slug for a connection name, such as "pets" from https://api.pets.example. */
export function suggestedName(origin: string): string {
  try {
    const parts = new URL(origin).hostname
      .split(".")
      .filter(
        (part) => !["api", "www", "com", "net", "org", "io"].includes(part),
      );
    const slug = (parts[0] ?? "")
      .toLowerCase()
      .replace(/[^a-z0-9_.-]/g, "-")
      .replace(/^[^a-z0-9]+/, "");
    return slug.slice(0, 64);
  } catch {
    return "";
  }
}

/**
 * The origin of a fixed server address (fixed_server): no credentials,
 * query, fragment, percent or backslash, no trailing-dot host, port not 0.
 * HTTPS, or HTTP when plainHttp is set (the platform's egress check decides
 * whether that address is reachable). Returns the origin and path, or null.
 */
function fixedServer(
  value: string,
  { plainHttp = false }: { plainHttp?: boolean } = {},
): { origin: string; path: string } | null {
  const raw = value.trim();
  if (
    !raw ||
    raw.includes("%") ||
    raw.includes("\\") ||
    /\/\.{1,2}(\/|$)/.test(raw.replace(/^https?:\/\//i, "")) ||
    [...raw].some((c) => c.charCodeAt(0) <= 32 || c.charCodeAt(0) >= 127)
  )
    return null;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (
    (url.protocol !== "https:" && !(plainHttp && url.protocol === "http:")) ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    raw.includes("?") ||
    raw.includes("#") ||
    url.hostname.endsWith(".") ||
    url.port === "0" ||
    /^https?:\/\/[^/]*@/i.test(raw)
  )
    return null;
  const path = url.pathname.replace(/\/+$/, "");
  if (
    path
      .split("/")
      .slice(1)
      .some((part) => ["", ".", ".."].includes(part)) ||
    path.includes("{") ||
    path.includes("}")
  )
    return null;
  return { origin: `${url.protocol}//${url.host}`, path };
}

/** The origin when the value is a fixed server address, else null; token endpoints pass plainHttp: false. */
export function originOf(
  value: string,
  { plainHttp = true }: { plainHttp?: boolean } = {},
): string | null {
  return fixedServer(value, { plainHttp })?.origin ?? null;
}

/**
 * The non-blocking "Not encrypted" notice for an http:// API address, else
 * null. Plain HTTP works (public addresses as they are, private ones only
 * where the platform operator approved the origin); the form says so and
 * never refuses it.
 */
export function plainHttpNotice(value: string): string | null {
  const origin = originOf(value);
  return origin?.startsWith("http://")
    ? `Not encrypted: requests to ${origin} travel in plain text.`
    : null;
}

const scopesOf = (draft: ConnectionDraft) =>
  draft.scopes.split(/[\s,]+/).filter(Boolean);

/** The destinations the profile requires: the API origin and, for OAuth, the token endpoint origin. */
export function requiredDestinations(draft: ConnectionDraft): string[] {
  const result: string[] = [];
  const origin = originOf(draft.origin);
  if (origin) result.push(origin);
  if (draft.auth === "machine-token") {
    const endpoint = originOf(draft.endpoint, { plainHttp: false });
    if (endpoint && !result.includes(endpoint)) result.push(endpoint);
  }
  return result;
}

/** One allowed destination as the form shows it; `index` is its position in the request, -1 when repeated. */
export interface DestinationEntry {
  value: string;
  index: number;
  source: "api" | "token" | "extra";
  /** Position in extraDestinations for added entries, else -1. */
  extra: number;
}

/** Required origins first (locked), then the added origins; repeats are dropped from the request. */
export function destinationEntries(draft: ConnectionDraft): DestinationEntry[] {
  const entries: DestinationEntry[] = [];
  const seen = new Set<string>();
  const origin = originOf(draft.origin);
  if (origin) {
    entries.push({ value: origin, index: 0, source: "api", extra: -1 });
    seen.add(origin);
  }
  if (draft.auth === "machine-token") {
    const endpoint = originOf(draft.endpoint, { plainHttp: false });
    if (endpoint && !seen.has(endpoint)) {
      entries.push({
        value: endpoint,
        index: entries.length,
        source: "token",
        extra: -1,
      });
      seen.add(endpoint);
    }
  }
  let index = entries.length;
  draft.extraDestinations.forEach((raw, extra) => {
    const server = fixedServer(raw, { plainHttp: true });
    const value = server && !server.path ? server.origin : raw.trim();
    const repeated = !value || seen.has(value);
    entries.push({
      value: raw,
      index: repeated ? -1 : index++,
      source: "extra",
      extra,
    });
    if (!repeated) seen.add(value);
  });
  return entries;
}

/** The allowed_destinations sent: required origins first, then the extra origins, once each. */
export function destinationsOf(draft: ConnectionDraft): string[] {
  return destinationEntries(draft)
    .filter((entry) => entry.index >= 0)
    .map((entry) => {
      const server = fixedServer(entry.value, { plainHttp: true });
      return server && !server.path ? server.origin : entry.value.trim();
    });
}

/**
 * True when a handle field holds what looks like a secret value: well-known
 * token prefixes, a JSON web token, an authorization header value, or a
 * random-looking run of characters. Handle names are words joined by '-',
 * '_' or '.', so each joined piece is judged on its own: long dashed handles
 * pass, while hex and base64url keys (which may contain '-' or '_') do not.
 */
export function looksLikeSecretValue(value: string): boolean {
  const text = value.trim();
  if (!text) return false;
  if (
    /^(sk|pk|rk)[-_][A-Za-z0-9]/.test(text) ||
    /^gh[pousr]_[A-Za-z0-9]/.test(text) ||
    /^github_pat_/.test(text) ||
    /^xox[abposr]-/.test(text) ||
    /^AKIA[0-9A-Z]{12,}$/.test(text) ||
    /^AIza[0-9A-Za-z_-]{20,}$/.test(text) ||
    /^glpat-/.test(text) ||
    /^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+/.test(text) ||
    /^(bearer|basic)\s+\S/i.test(text)
  )
    return true;
  const mixed = (piece: string) =>
    /[A-Z]/.test(piece) && /[a-z]/.test(piece) && /\d/.test(piece);
  if (text.length >= 20 && !/[-_.]/.test(text) && mixed(text)) return true;
  if (
    text
      .split(/[-_.]/)
      .some(
        (piece) =>
          (piece.length >= 16 && mixed(piece)) ||
          (piece.length >= 24 && /\d/.test(piece) && /[A-Za-z]/.test(piece)),
      )
  )
    return true;
  return text.length >= 64;
}

/** Client checks before anything is sent; the server re-checks everything. */
export function checkDraft(
  draft: ConnectionDraft,
  options: { local?: boolean } = {},
): FieldProblem[] {
  const problems: FieldProblem[] = [];
  const add = (field: string, message: string) =>
    problems.push({ field, message });
  const name = draft.name.trim();
  if (!name) add("name", "Enter a connection name, such as pets.");
  else if (!resourceName.test(name) || name.length > 128)
    add(
      "name",
      "Use letters, digits, '.', '_' or '-', starting with a letter or digit (128 characters at most).",
    );
  const server = fixedServer(draft.origin, { plainHttp: true });
  if (!draft.origin.trim())
    add("origin", "Enter the API address, such as https://api.example.com.");
  else if (!server)
    add(
      "origin",
      "Use an HTTPS or HTTP address such as https://api.example.com, without a user name, password, query or fragment.",
    );
  else if (server.path)
    add(
      "origin",
      `Use only the origin, ${server.origin}. The base path (${server.path}) belongs in each action's path.`,
    );
  if (draft.auth === "api-key") {
    const header = draft.header.trim();
    if (!header)
      add(
        "header",
        "Enter the header that carries the API key, such as X-API-Key.",
      );
    else if (!headerName.test(header) || header.length > 128)
      add(
        "header",
        "Use a header name made of letters, digits and '-', such as X-API-Key.",
      );
    else if (
      protectedHeaders.has(header.toLowerCase()) ||
      /^(proxy-|x-forwarded-)/i.test(header)
    )
      add(
        "header",
        `${header} is reserved. Use a header such as X-API-Key; bearer tokens have their own option.`,
      );
  }
  if (draft.auth === "machine-token") {
    const clientId = draft.clientId.trim();
    if (!clientId)
      add("clientId", "Enter the OAuth client ID. It isn't a secret.");
    else if (clientId.length > 256)
      add("clientId", "Use a client ID of 256 characters at most.");
    if (!draft.endpoint.trim())
      add(
        "endpoint",
        "Enter the token endpoint, such as https://login.example.com/oauth2/token.",
      );
    else if (!fixedServer(draft.endpoint))
      add(
        "endpoint",
        "Use the HTTPS token endpoint address, without a user name, password, query or fragment.",
      );
    const scopes = scopesOf(draft);
    if (scopes.length > 100) add("scopes", "Use at most 100 scopes.");
    else if (scopes.some((scope) => !scopePattern.test(scope)))
      add(
        "scopes",
        "Separate scopes with spaces. A scope can't contain quotes or backslashes.",
      );
    else if (new Set(scopes).size !== scopes.length)
      add("scopes", "List each scope once.");
  }
  for (const slot of authSlots[draft.auth]) {
    const handle = (draft.secrets[slot] ?? "").trim();
    const field = `secret.${slot}`;
    const label = slotLabels[slot] ?? slot;
    if (!handle)
      add(
        field,
        `Enter the handle name of the stored ${label.toLowerCase()}, such as pets-${slot.replace("_", "-")}.`,
      );
    else if (looksLikeSecretValue(handle))
      add(
        field,
        "This looks like a secret value. Enter the handle name your platform operator gave you instead; never paste the secret itself.",
      );
    else if (!resourceName.test(handle) || handle.length > 128)
      add(
        field,
        "A handle is a name such as pets-api-key: letters, digits, '.', '_' or '-'.",
      );
    else if (options.local && !localHandle.test(handle))
      add(
        field,
        "Handles on this computer use lowercase letters, digits, '.', '_' or '-' (64 characters at most).",
      );
  }
  const destinations = destinationsOf(draft);
  destinations.forEach((destination, index) => {
    if (
      !originOf(destination) ||
      fixedServer(destination, { plainHttp: true })?.path
    )
      add(
        `destination.${index}`,
        "List only an HTTP or HTTPS origin such as https://api.example.com: no path, query, credentials or wildcards.",
      );
  });
  return problems;
}

/** The connections.create body: AuthProfile fields as the platform names them, handles only. */
export function connectionRequest(
  draft: ConnectionDraft,
  connectorVersionId: string,
): ConnectionRequestBody {
  const auth: Record<string, unknown> = { kind: draft.auth };
  if (draft.auth === "api-key") auth["header"] = draft.header.trim();
  if (draft.auth === "machine-token") {
    auth["client_id"] = draft.clientId.trim();
    auth["endpoint"] = draft.endpoint.trim();
    const scopes = scopesOf(draft);
    if (scopes.length) auth["scopes"] = scopes;
    if (draft.authentication !== "client_secret_post")
      auth["authentication"] = draft.authentication;
  }
  const secretRef: Record<string, string> = {};
  for (const slot of authSlots[draft.auth])
    secretRef[slot] = (draft.secrets[slot] ?? "").trim();
  return {
    name: draft.name.trim(),
    connector_version_id: connectorVersionId,
    config: { baseUrl: originOf(draft.origin) ?? draft.origin.trim(), auth },
    secretRef,
    allowed_destinations: destinationsOf(draft),
  };
}

const stable = (value: unknown): string =>
  Array.isArray(value)
    ? `[${value.map(stable).join(",")}]`
    : value !== null && typeof value === "object"
      ? `{${Object.keys(value as object)
          .sort()
          .map(
            (key) =>
              `${JSON.stringify(key)}:${stable((value as Record<string, unknown>)[key])}`,
          )
          .join(",")}}`
      : JSON.stringify(value);

/** True when a listed revision carries exactly this request (for re-listing after a lost answer). */
export function sameRequest(
  revision: Record<string, unknown>,
  request: ConnectionRequestBody,
): boolean {
  return (
    revision["name"] === request.name &&
    String(revision["connector_version_id"] ?? "") ===
      request.connector_version_id &&
    stable(revision["config"] ?? {}) === stable(request.config) &&
    stable(revision["secretRef"] ?? {}) === stable(request.secretRef) &&
    stable(revision["allowed_destinations"] ?? []) ===
      stable(request.allowed_destinations)
  );
}

const unescape = (segment: string) =>
  segment.replace(/~1/g, "/").replace(/~0/g, "~");

/** The form field a WV-CONNECTION diagnostic pointer belongs to. */
export function fieldForPointer(pointer: string): string {
  if (!pointerPattern.test(pointer)) return "general";
  const parts = pointer.split("/").slice(1).map(unescape);
  const [head, second, third] = parts;
  if (head === "name") return "name";
  if (head === "connector_version_id") return "connector";
  if (head === "secretRef") return second ? `secret.${second}` : "secrets";
  if (head === "allowed_destinations")
    return second !== undefined && /^\d+$/.test(second)
      ? `destination.${second}`
      : "destinations";
  if (head === "config") {
    if (second === "baseUrl") return "origin";
    if (second === "auth") {
      if (!third || third === "kind") return "auth";
      if (third === "header") return "header";
      if (third === "client_id") return "clientId";
      if (third === "endpoint") return "endpoint";
      if (third === "scopes") return "scopes";
      if (third === "authentication") return "authentication";
      return "auth";
    }
  }
  return "general";
}

/** Plain fallback text per diagnostic code, for this connection form. */
export const connectionCodeCopy: Record<string, string> = {
  "WV-CONNECTION-CONNECTOR":
    "The weave-http@2.0.0 connector isn't published or installed for this project.",
  "WV-CONNECTION-CONFIG": "The platform doesn't accept this setting.",
  "WV-CONNECTION-AUTH":
    "The platform doesn't accept these authentication settings.",
  "WV-CONNECTION-SECRET":
    "This secret handle isn't available in this environment.",
  "WV-CONNECTION-DESTINATION":
    "Allowed destinations must be HTTPS or HTTP origins such as https://api.example.com.",
};
const secretHint = "Check the handle name with your platform operator.";

function readable(text: unknown): text is string {
  return (
    typeof text === "string" &&
    text.trim().length > 0 &&
    text.length <= 240 &&
    !/[{}[\]<>]/.test(text)
  );
}

/** A server sentence in this form's words: field names become the labels people see. */
export function plainServerMessage(message: string): string {
  return message
    .replace(/\ballowed_destinations\b/g, "the allowed destinations")
    .replace(/\bsecretRef\b/g, "secret handles")
    .replace(/\bbaseUrl\b/g, "API address")
    .replace(/\bthe the\b/g, "the");
}

const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

/**
 * Field problems from a 422 WV-CONNECTION problem body. The diagnostics are
 * read from `diagnostics` or `result.diagnostics`; each keeps its code as a
 * support code and lands on the field its pointer names.
 */
export function connectionProblems(detail: unknown): FieldProblem[] {
  const body = record(detail);
  const items = Array.isArray(body["diagnostics"])
    ? body["diagnostics"]
    : record(body["result"])["diagnostics"];
  if (!Array.isArray(items)) return [];
  return items.slice(0, 100).map((value) => {
    const item = record(value);
    const code = /^WV-CONNECTION-[A-Z]+$/.test(String(item["code"] ?? ""))
      ? String(item["code"])
      : "";
    const pointer = typeof item["path"] === "string" ? item["path"] : "";
    const base = readable(item["message"])
      ? plainServerMessage(item["message"])
      : (connectionCodeCopy[code] ?? "The platform rejected this setting.");
    return {
      field: fieldForPointer(pointer),
      message: code === "WV-CONNECTION-SECRET" ? `${base} ${secretHint}` : base,
      code: code || undefined,
    };
  });
}

/** Plain copy for a failed create or check, in this form's context. */
export function connectionFailureCopy(
  status: number,
  code: string,
  action: "create" | "check",
): string {
  if (status === 403 || code === "WV-DENIED" || code === "WV-FORBIDDEN")
    return action === "create"
      ? "Your account can't create connections in this environment. Hand the request below to an administrator."
      : "Your account can't check connections in this environment.";
  if (code === "WV-CONNECTION")
    return action === "create"
      ? "The platform didn't accept this connection. Review the highlighted settings."
      : "The platform can't use this saved connection any more. Review the highlighted settings and create a new revision.";
  if (code === "WV-VALIDATION" || status === 400)
    return "The platform couldn't read this connection request. Review each setting and try again.";
  if (code === "WV-IDEMPOTENCY-CONFLICT")
    return "This request changed after the first attempt. Try again to send it as a new request.";
  if (status === 404)
    return action === "create"
      ? "This environment isn't available. Choose another workspace in Settings."
      : "That connection revision no longer exists.";
  return "";
}

export const checkCopy = {
  label: "Check configuration (no request is sent)",
  running: "Checking the configuration… This can take up to a minute.",
  ok: "The platform accepted this configuration. No request was sent to the API, so the address and credentials are confirmed on the first run.",
  failed:
    "The platform can't use this connection as configured. Review the settings and the secret handle names with your platform operator.",
};

const quote = (value: string) =>
  /^[A-Za-z0-9_./:=@%+-]+$/.test(value)
    ? value
    : `'${value.replace(/'/g, `'\\''`)}'`;

/** Workspace flags so the administrator's CLI targets this exact environment. */
function scopeFlags(profile: ScopeProfile | null): string[] {
  const flags: string[] = [];
  if (profile?.tenantId) flags.push("--tenant", quote(profile.tenantId));
  if (profile?.projectId) flags.push("--project", quote(profile.projectId));
  if (profile?.environmentId)
    flags.push("--environment", quote(profile.environmentId));
  return flags;
}

/** The request an administrator can submit; secret handles only, never values. */
export function handoffRequest(
  draft: ConnectionDraft,
  connectorVersionId: string,
): ConnectionRequestBody {
  return connectionRequest(draft, connectorVersionId || "CONNECTOR_VERSION_ID");
}

/**
 * The CLI command for the hand-off. With a known connector version (or
 * settings the guided flags can't express) it submits the saved request
 * file; otherwise it uses the guided flags, which resolve the published
 * weave-http@2.0.0 version themselves.
 */
export function handoffCommand(
  draft: ConnectionDraft,
  connectorVersionId: string,
  profile: ScopeProfile | null,
  file = "connection.json",
): string {
  const base = ["weave", "connections", "create", ...scopeFlags(profile)];
  if (connectorVersionId || draft.authentication !== "client_secret_post")
    return [...base, "--request", quote(file)].join(" ");
  const request = connectionRequest(draft, "");
  const parts = [
    ...base,
    "--name",
    quote(request.name),
    "--api-url",
    quote(request.config.baseUrl),
    "--auth",
    draft.auth,
  ];
  if (draft.auth === "api-key")
    parts.push("--auth-header", quote(draft.header.trim()));
  if (draft.auth === "machine-token") {
    parts.push("--client-id", quote(draft.clientId.trim()));
    parts.push("--token-endpoint", quote(draft.endpoint.trim()));
    for (const scope of scopesOf(draft)) parts.push("--scope", quote(scope));
  }
  for (const [slot, handle] of Object.entries(request.secretRef))
    parts.push("--secret", quote(`${slot}=${handle}`));
  const required = requiredDestinations(draft);
  for (const destination of request.allowed_destinations)
    if (!required.includes(destination))
      parts.push("--allow", quote(destination));
  return parts.join(" ");
}
