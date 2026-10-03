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
// Platform connection contract of the local Studio host (snake_case JSON) and
// the plain-language helpers the wizard, Settings and shell share. Nothing here
// handles tokens: credentials stay in the host's credential store.
import { Session, StudioApi } from "./api";
import { describeError } from "./errors";

export interface Identity {
  principal_id: string;
  kind: string;
  grants: {
    role: string;
    scope: {
      tenant_id: string;
      project_id?: string | null;
      environment_id?: string | null;
    } | null;
    resources: string[];
    capabilities: string[];
  }[];
  workspaces: {
    id: string;
    name: string;
    projects: {
      id: string;
      name: string;
      environments: { id: string; name: string }[];
    }[];
  }[];
  truncated: boolean;
}
export type AuthState = "signed_in" | "expired" | "signed_out" | "in_progress";
export type SignInFlow = "browser" | "device";
export interface WorkspaceSelection {
  tenant_id: string;
  project_id: string;
  environment_id: string;
  tenant_name?: string | null;
  project_name?: string | null;
  environment_name?: string | null;
}
/** The last signed-in account; the subject is null when the provider shared none. */
export interface AccountHint {
  subject: string | null;
  display_name?: string | null;
  issuer?: string | null;
  provider_id?: string | null;
}
/**
 * A saved platform as the host lists it; never carries credentials. The
 * active profile view adds sign-in settings, the workspace and the account
 * hint; list entries carry labels only.
 */
export interface PlatformView {
  name: string;
  server?: string | null;
  saved?: boolean;
  source?: string | null;
  display_name?: string | null;
  provider_name?: string | null;
  provider_id?: string | null;
  issuer?: string | null;
  issuer_origin?: string | null;
  client_id?: string | null;
  scopes?: string[] | null;
  trusted_endpoint_origins?: string[] | null;
  login?: { target?: string; issuer?: string; provider_id?: string } | null;
  workspace?: (WorkspaceSelection & { label?: string | null }) | null;
  workspace_label?: string | null;
  account?: AccountHint | null;
  account_label?: string | null;
  active?: boolean;
  state?: AuthState | null;
  authentication?: { state?: AuthState | null } | null;
  flows?: SignInFlow[] | null;
}
export interface Authentication {
  authenticated?: boolean;
  reauthentication_required?: boolean;
  refresh_available?: boolean;
  state?: AuthState | null;
  expires_at?: string | number | null;
  error_code?: string | null;
  account?: AccountHint | null;
}
export interface LoginStatus {
  id?: string | null;
  state?: string | null;
  flow?: string | null;
  verification_uri?: string | null;
  user_code?: string | null;
  authorization_uri?: string | null;
  error_code?: string | null;
  /** Epoch seconds when the host stops waiting; null when idle or finished. */
  expires_at?: number | null;
  /** Plain guidance from the host, for example how to switch account with a code. */
  notice?: string | null;
}
export type StartPreference = "ask" | "local";
/** How Studio starts; the host keeps it next to the saved platforms. */
export interface StudioPreferences {
  start: StartPreference;
}
export interface ConnectionStatus {
  configured?: boolean;
  login_supported?: boolean;
  store?: { available?: boolean; location?: string | null } | null;
  profile?: PlatformView | null;
  profiles?: PlatformView[] | null;
  authentication?: Authentication | null;
  login?: LoginStatus | null;
  preferences?: StudioPreferences | null;
}
export interface SignInChoice {
  provider_id: string;
  display_name: string;
  issuer: string;
  client_id: string;
  scopes: string[];
  trusted_endpoint_origins: string[];
  allow_loopback_http?: boolean;
  flows: SignInFlow[];
  require_refresh_rotation?: boolean;
  issuer_origin?: string;
  checks?: { browser?: boolean; device?: boolean } | null;
  problem?: string | { code?: string; message?: string } | null;
}
export interface Discovery {
  server: string;
  display_name?: string | null;
  api_version?: string;
  suggested_name?: string | null;
  existing_profile?: string | { name?: string } | null;
  sign_in: SignInChoice[];
}
export interface WorkspaceOption {
  tenant_id: string;
  tenant_name: string;
  project_id: string;
  project_name: string;
  environment_id: string;
  environment_name: string;
  label: string;
}
export type Revocation = "confirmed" | "unconfirmed" | "not_requested";
export interface ConnectionResult {
  session: Session;
  connection: ConnectionStatus;
  /** Sign out: whether the identity provider confirmed ending the sign-in. */
  remote_revocation?: Revocation | null;
  /** Remove: what happened to the platform's sign-in on this computer. */
  sign_out?: {
    credentials_removed?: boolean;
    remote_revocation?: Revocation | null;
  } | null;
}
export interface TestResult {
  session: Session;
  identity: Identity;
  workspaces?: WorkspaceOption[];
  truncated?: boolean;
  /** The saved workspace is no longer authorized and was cleared. */
  workspace_revoked?: boolean;
}
export type WizardStep =
  | "choice"
  | "server"
  | "review"
  | "sign-in"
  | "workspace";
export type SwitchKind = "platform" | "workspace";
export type PlatformAction =
  | "use"
  | "sign-in"
  | "switch-account"
  | "sign-out"
  | "workspace"
  | "remove";
export interface VerifiedPlatform {
  session: Session | null;
  identity: Identity;
  workspaces: WorkspaceOption[];
}
export interface WorkspaceChange {
  session: Session;
  identity: Identity | null;
}
export interface ProfileRequest {
  name: string;
  server: string;
  provider_id: string;
  issuer: string;
  client_id: string;
  trust_confirmed: true;
}

/**
 * How long Studio waits for calls that may reach the platform server or the
 * identity provider (discovery, sign-in checks, revocation) or a credential
 * store prompt. The host bounds each of them below this (at most 40 seconds).
 */
export const platformCallTimeout = 60_000;

/** Thin, typed calls to the paired local host. */
export class ConnectionClient {
  constructor(private api: StudioApi) {}
  private slow<T>(path: string, method = "GET", body?: unknown) {
    return this.api.request<T>(path, method, body, {}, platformCallTimeout);
  }
  status() {
    return this.slow<ConnectionStatus>("/studio/connection");
  }
  discover(server: string) {
    return this.slow<Discovery>("/studio/connection/discover", "POST", {
      server,
    });
  }
  createProfile(body: ProfileRequest) {
    return this.slow<ConnectionResult>(
      "/studio/connection/profiles",
      "POST",
      body,
    );
  }
  /** Legacy reviewed connection file; the host answers with a session payload. */
  configure(name: string, login: Record<string, unknown>) {
    return this.slow<Session>("/studio/connection/configure", "POST", {
      name,
      login,
      trust_confirmed: true,
    });
  }
  activate(name: string) {
    return this.slow<ConnectionResult>("/studio/connection/activate", "POST", {
      name,
    });
  }
  disconnect() {
    return this.slow<ConnectionResult>(
      "/studio/connection/disconnect",
      "POST",
      {},
    );
  }
  remove(name: string, signOut: boolean) {
    return this.slow<ConnectionResult>("/studio/connection/remove", "POST", {
      name,
      sign_out: signOut,
    });
  }
  logout(revoke = true) {
    return this.slow<ConnectionResult>("/studio/connection/logout", "POST", {
      revoke,
    });
  }
  startLogin(flow: SignInFlow | "auto", switchAccount: boolean) {
    return this.slow<LoginStatus>("/studio/connection/login/start", "POST", {
      flow,
      switch_account: switchAccount,
    });
  }
  login(id: string) {
    return this.api.request<LoginStatus>(
      `/studio/connection/login/${encodeURIComponent(id)}`,
    );
  }
  cancelLogin(id: string) {
    return this.api.request<LoginStatus>(
      `/studio/connection/login/${encodeURIComponent(id)}/cancel`,
      "POST",
      {},
    );
  }
  /** Desktop only: the host opens the system browser at the current sign-in page. */
  openLogin(id: string) {
    return this.api.request<unknown>(
      `/studio/connection/login/${encodeURIComponent(id)}/open`,
      "POST",
      {},
    );
  }
  test() {
    return this.slow<TestResult>("/studio/connection/test", "POST");
  }
  /** Saves how Studio starts; the host stores it next to the saved platforms. */
  savePreferences(start: StartPreference) {
    return this.api.request<{ preferences: StudioPreferences }>(
      "/studio/preferences",
      "POST",
      { start },
    );
  }
  /** Persists the workspace to the active platform; the host re-checks access. */
  selectWorkspace(option: WorkspaceSelection) {
    return this.slow<Session>("/studio/scope", "POST", {
      tenantId: option.tenant_id,
      projectId: option.project_id,
      environmentId: option.environment_id,
    });
  }
}

const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

/** True for a host connection payload (not, for example, an HTML fallback). */
export function isConnectionStatus(value: unknown): value is ConnectionStatus {
  const body = record(value);
  return (
    !!body &&
    (typeof body["configured"] === "boolean" ||
      Array.isArray(body["profiles"]) ||
      record(body["store"]) !== null)
  );
}

const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9 ._-]*$/;
/** Plain-language problem with a platform name, or an empty string. */
export function profileNameError(value: string): string {
  if (!value.trim()) return "Enter a name for this platform.";
  if (value.length > 64) return "Use 64 characters or fewer.";
  if (!PROFILE_NAME.test(value) || value.endsWith(" "))
    return "Start with a letter or digit, and use only letters, digits, spaces, periods, underscores, or hyphens.";
  return "";
}
/** Light client-side check; the host normalizes and validates the address. */
export function serverAddressError(value: string): string {
  const text = value.trim();
  if (!text) return "Enter the server address.";
  if (/\s/.test(text)) return "Remove the spaces from the address.";
  if (text.length > 2048)
    return "This address is too long. Enter only the server address, for example https://weave.example.com.";
  return "";
}
export function hostOf(value: string | null | undefined): string {
  const text = (value ?? "").trim();
  if (!text) return "";
  try {
    return new URL(
      /^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `https://${text}`,
    ).host;
  } catch {
    return text;
  }
}
function httpsOrigin(value: string | null | undefined): string {
  if (!value) return "";
  try {
    const url = new URL(value);
    return url.protocol === "https:" &&
      !url.username &&
      !url.password &&
      `${url.protocol}//${url.host}` === value
      ? value
      : "";
  } catch {
    return "";
  }
}

export interface ConnectProblem {
  title: string;
  hint: string;
  code: string;
  action?: {
    kind: "address" | "connection-file";
    label: string;
    value?: string;
  };
}
/** Explains a discovery or profile failure for the address the person typed. */
export function connectProblem(
  code: string,
  address: string,
  detail?: unknown,
  fallback = "",
): ConnectProblem {
  const host = hostOf(address) || "the server";
  switch (code) {
    case "WV-CONNECT-ADDRESS":
      return {
        code,
        title: "That doesn't look like a server address.",
        hint: "Enter only the address your administrator gave you, for example https://weave.example.com. Leave out paths such as /login.",
      };
    case "WV-CONNECT-INSECURE": {
      const value = `https://${hostOf(address)}`;
      return {
        code,
        title: "This address isn't secure.",
        hint: "Studio connects to remote servers only over HTTPS. Plain http:// works only for a server on this computer.",
        action: hostOf(address)
          ? { kind: "address", label: `Use ${value}`, value }
          : undefined,
      };
    }
    case "WV-CONNECT-BLOCKED":
      return {
        code,
        title: "Studio can't connect to that address.",
        hint: "Addresses such as 169.254.x.x or 0.0.0.0 are reserved and never belong to a Weave server. Check the address with your administrator.",
      };
    case "WV-CONNECT-UNREACHABLE":
      return {
        code,
        title: `Studio couldn't reach ${host}.`,
        hint: "Check the address and your network connection. If the server is on your company network, connect to your VPN and try again.",
      };
    case "WV-CONNECT-TLS":
      return {
        code,
        title: `Studio couldn't set up a secure connection to ${host}.`,
        hint: "This computer doesn't trust the server's certificate. If your company uses its own certificates, ask your administrator to install them, or check that you're on the right network.",
      };
    case "WV-CONNECT-TIMEOUT":
      return {
        code,
        title: `${host} didn't answer in time.`,
        hint: "Check your network or VPN connection, then try again.",
      };
    case "WV-CONNECT-REDIRECT": {
      const target = httpsOrigin(typeof detail === "string" ? detail : "");
      return {
        code,
        title: "This address forwards to another location.",
        hint: target
          ? `The server sent Studio to ${target}. If that's your Weave server, use that address instead.`
          : "Ask your administrator for the exact address of the Weave server.",
        action: target
          ? { kind: "address", label: `Use ${target}`, value: target }
          : undefined,
      };
    }
    case "WV-CONNECT-NOT-WEAVE":
      return {
        code,
        title: "This doesn't look like a Firefly Weave server.",
        hint: "Check the address. Use the address of the Weave server itself, not a sign-in page or a website.",
      };
    case "WV-CONNECT-INCOMPATIBLE":
      return {
        code,
        title:
          "This server runs a version of Firefly Weave that this Studio can't use.",
        hint: "Update Firefly Weave Studio, or ask your administrator which version to use with this server.",
      };
    case "WV-CONNECT-NO-SIGN-IN":
      return {
        code,
        title: "This server doesn't publish its sign-in settings.",
        hint: "Ask your administrator for a connection file, then use it here.",
        action: { kind: "connection-file", label: "Use a connection file" },
      };
    case "WV-CONNECT-PROVIDER":
      return {
        code,
        title: "Studio couldn't check the identity provider for this server.",
        hint: "The sign-in service didn't answer as expected. Try again, and if it keeps failing, ask your administrator to check the platform's sign-in settings.",
      };
    case "WV-PROFILE-CHANGED":
      return {
        code,
        title: "The server's sign-in settings changed.",
        hint: "Review the new settings before you continue.",
      };
    case "WV-PROFILE-STORE":
      return {
        code,
        title: "Studio couldn't save platforms on this computer.",
        hint: "Check that your user configuration folder is writable, then try again.",
      };
    default:
      return {
        code,
        title: fallback || "Studio couldn't connect to this server.",
        hint: "Check the address and try again.",
      };
  }
}

export interface LoginOutcome {
  title: string;
  hint: string;
  /** Not a problem (the person canceled): shown as a neutral note. */
  calm?: boolean;
}
/** Plain explanation of a finished sign-in that did not succeed. */
export function loginOutcome(
  state: string,
  code?: string | null,
): LoginOutcome {
  if (state === "cancelled")
    return {
      title: "Sign-in canceled.",
      hint: "Nothing changed.",
      calm: true,
    };
  if (state === "timeout" || code === "WV-AUTH-EXPIRED")
    return {
      title: "Sign-in timed out",
      hint: "The sign-in request expired before it was finished. Start again, then finish signing in within a few minutes.",
    };
  if (state === "lost" || code === "WV-STUDIO-LOGIN")
    return {
      title: "This sign-in is no longer active",
      hint: "Studio restarted or another sign-in replaced this one. Start again.",
    };
  switch (code) {
    case "WV-AUTH-DENIED":
      return {
        title: "Sign-in was declined",
        hint: "The request was declined in the browser or by the identity provider. Try again. If it keeps happening, ask your administrator whether your account can use Weave Studio.",
      };
    case "WV-AUTH-STORE":
      return {
        title: "Studio couldn't save your sign-in",
        hint: "Your system's credential store is locked or unavailable. Unlock your keychain or credential manager, then try again.",
      };
    case "WV-AUTH-OFFLINE":
      return {
        title: "Studio couldn't reach the identity provider",
        hint: "Check your network or VPN connection, then try again.",
      };
    case "WV-AUTH-DEVICE-UNAVAILABLE":
      return {
        title: "This identity provider doesn't offer sign-in with a code",
        hint: "Sign in with your web browser instead.",
      };
    case "WV-AUTH-SUPERSEDED":
      return {
        title: "Another sign-in finished first",
        hint: "A different sign-in for this platform replaced this one. Check whether you're signed in, or start again.",
      };
    case "WV-AUTH-CALLBACK":
      return {
        title: "The sign-in page sent back an unexpected answer",
        hint: "Start again from Studio and finish signing in on the page it opens. If it keeps failing, ask your administrator to check the platform's sign-in settings.",
      };
    case "WV-AUTH-TRUST":
      return {
        title: "The sign-in page isn't on an address you reviewed",
        hint: "Studio stopped for your safety. Ask your administrator to check the platform's sign-in settings.",
      };
    case "WV-AUTH-FLOW":
      return {
        title: "Your administrator doesn't allow this sign-in method",
        hint: "Sign in with the other method Studio offers. If none works, ask your administrator to check the platform's sign-in settings.",
      };
    case "WV-STUDIO-CONNECTION":
      return {
        title: "This platform isn't ready for sign-in",
        hint: "Review the platform in Settings, then try again.",
      };
    default:
      return {
        title: "Sign-in didn't work",
        hint: "The identity provider returned an error. Try again. If it keeps failing, ask your administrator to check the platform's sign-in settings.",
      };
  }
}
export const firstPollDelay = 1500;
export function nextPollDelay(previous: number): number {
  return Math.min(5000, Math.round(previous * 1.5));
}
/** How long Studio waits for a sign-in when the host states no expiry. */
export const defaultLoginWait = 15 * 60 * 1000;
// The host reports its own timeout at expires_at; Studio gives it a moment.
const expiryGrace = 2000;
// An expiry further away than this is not a sign-in limit Studio can trust.
const longestLoginWait = 60 * 60 * 1000;
/** The host's sign-in expiry in epoch milliseconds, or 0 when it states none. */
function loginExpiry(login: LoginStatus | null | undefined, now: number) {
  const seconds = Number(login?.expires_at);
  if (!Number.isFinite(seconds) || seconds <= 0) return 0;
  const expiry = seconds * 1000;
  return expiry - now > longestLoginWait ? 0 : expiry;
}
/** When Studio stops waiting: the host's expiry, otherwise 15 minutes after `started`. */
export function loginDeadline(
  login: LoginStatus | null | undefined,
  started: number,
): number {
  const expiry = loginExpiry(login, started);
  return expiry ? expiry + expiryGrace : started + defaultLoginWait;
}
/** "About N minutes left" from the host's expiry; empty when it states none. */
export function remainingTimeText(
  login: LoginStatus | null | undefined,
  now: number,
): string {
  const expiry = loginExpiry(login, now);
  if (!expiry) return "";
  const left = expiry - now;
  if (left <= 60_000) return "Less than a minute left";
  const minutes = Math.round(left / 60_000);
  return minutes === 1
    ? "About 1 minute left"
    : `About ${minutes} minutes left`;
}
/** A sign-in page Studio may link to: HTTPS, or HTTP on this computer only. */
export function safeSignInUrl(value: unknown): string {
  if (typeof value !== "string" || value.length > 8192) return "";
  try {
    const url = new URL(value);
    return url.protocol === "https:" ||
      (url.protocol === "http:" &&
        ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))
      ? value
      : "";
  } catch {
    return "";
  }
}
export function signInMethods(
  option: Pick<SignInChoice, "flows" | "checks">,
): SignInFlow[] {
  return (option.flows ?? ["browser", "device"]).filter(
    (flow) => option.checks?.[flow] !== false,
  );
}
/**
 * The sign-in methods to offer: the ones the administrator allows (both when
 * an older host does not say), minus any the host or provider refused since.
 */
export function allowedFlows(
  flows: readonly SignInFlow[] | null | undefined,
  refused: Iterable<SignInFlow> = [],
): SignInFlow[] {
  const blocked = new Set(refused);
  return (flows ?? ["browser", "device"]).filter(
    (flow) => (flow === "browser" || flow === "device") && !blocked.has(flow),
  );
}
export function problemText(problem: SignInChoice["problem"]): string {
  if (!problem) return "";
  if (typeof problem === "string") return problem;
  if (problem.message) return problem.message;
  return connectProblem(problem.code ?? "", "").title.replace(
    "this server",
    "this option",
  );
}

export function stateLabel(state: AuthState | null | undefined): string {
  return state === "signed_in"
    ? "Signed in"
    : state === "expired"
      ? "Session expired"
      : state === "signed_out"
        ? "Signed out"
        : state === "in_progress"
          ? "Signing in"
          : "Not signed in";
}
/**
 * True when the active platform's sign-in works without asking again: signed
 * in, or an expired access token the host renews with its refresh token on
 * the next platform check.
 */
export function sessionUsable(
  auth: Authentication | null | undefined,
): boolean {
  if (!auth) return false;
  if (auth.state)
    return (
      auth.state === "signed_in" ||
      (auth.state === "expired" && auth.refresh_available === true)
    );
  return !!auth.authenticated;
}
/** The sign-in state of a saved platform; only the active one is checked live. */
export function platformState(
  platform: PlatformView,
  status: ConnectionStatus | null,
): AuthState | null {
  if (status?.profile?.name === platform.name) {
    const auth = status.authentication;
    return (
      auth?.state ??
      (auth?.authenticated
        ? "signed_in"
        : auth?.reauthentication_required
          ? "expired"
          : null)
    );
  }
  return platform.state ?? platform.authentication?.state ?? null;
}
export function platformServer(platform: PlatformView | null | undefined) {
  return platform?.server ?? platform?.login?.target ?? "";
}
export function platformIssuer(platform: PlatformView | null | undefined) {
  return platform?.issuer ?? platform?.login?.issuer ?? "";
}
export function existingProfileName(value: Discovery["existing_profile"]) {
  if (!value) return "";
  return typeof value === "string" ? value : (value.name ?? "");
}
export function accountName(account: AccountHint | null | undefined) {
  return account?.display_name || account?.subject || "";
}
/**
 * The top bar's sign-in state, naming the account ("Signed in as alice") when
 * the identity provider shared a name. A bare subject is an ID, so it stays out.
 */
export function signedInText(
  state: string,
  account: AccountHint | null | undefined,
) {
  const name = account?.display_name?.trim();
  return state === "Signed in" && name ? `Signed in as ${name}` : state;
}
/** An account hint from a host answer; any field may be missing or null. */
export function accountHint(value: unknown): AccountHint | null {
  const body = record(value);
  if (!body) return null;
  const text = (key: string) =>
    typeof body[key] === "string" && body[key] ? (body[key] as string) : null;
  if (
    body["subject"] !== undefined &&
    body["subject"] !== null &&
    !text("subject")
  )
    return null;
  const hint: AccountHint = {
    subject: text("subject"),
    display_name: text("display_name"),
    issuer: text("issuer"),
    provider_id: text("provider_id"),
  };
  return hint.subject || hint.display_name || hint.issuer || hint.provider_id
    ? hint
    : null;
}
/** Shown when the identity provider did not confirm a sign-out. */
export const revocationNote =
  "Signed out on this computer. Your identity provider did not confirm the sign-out.";
/**
 * What a sign-out or removal did: "incomplete" when the sign-in stayed on
 * this computer, "unconfirmed" when only the identity provider did not
 * confirm, "none" when the answer reports no sign-out.
 */
export function signOutReport(
  result: unknown,
): "complete" | "unconfirmed" | "incomplete" | "none" {
  const body = record(result);
  if (!body) return "none";
  const removal = record(body["sign_out"]);
  if (removal) {
    if (removal["credentials_removed"] === false) return "incomplete";
    return removal["remote_revocation"] === "unconfirmed"
      ? "unconfirmed"
      : "complete";
  }
  if (!("remote_revocation" in body)) return "none";
  return body["remote_revocation"] === "unconfirmed"
    ? "unconfirmed"
    : "complete";
}

export const workspaceKey = (o: {
  tenant_id: string;
  project_id: string;
  environment_id: string;
}) => `${o.tenant_id}/${o.project_id}/${o.environment_id}`;
export function workspaceLabel(
  selection:
    | (WorkspaceSelection & { label?: string | null })
    | null
    | undefined,
): string {
  if (!selection) return "";
  return selection.project_name && selection.environment_name
    ? `${selection.project_name} / ${selection.environment_name}`
    : "Workspace selected";
}
export interface WorkspaceGroup {
  tenant_id: string;
  tenant_name: string;
  projects: {
    project_id: string;
    project_name: string;
    environments: WorkspaceOption[];
  }[];
}
/** Groups options tenant > project > environment, keeping the host's order. */
export function groupWorkspaces(options: WorkspaceOption[]): WorkspaceGroup[] {
  const tenants = new Map<string, WorkspaceGroup>();
  for (const option of options) {
    let tenant = tenants.get(option.tenant_id);
    if (!tenant) {
      tenant = {
        tenant_id: option.tenant_id,
        tenant_name: option.tenant_name,
        projects: [],
      };
      tenants.set(option.tenant_id, tenant);
    }
    let project = tenant.projects.find(
      (p) => p.project_id === option.project_id,
    );
    if (!project) {
      project = {
        project_id: option.project_id,
        project_name: option.project_name,
        environments: [],
      };
      tenant.projects.push(project);
    }
    project.environments.push(option);
  }
  return [...tenants.values()];
}
export function filterWorkspaces(
  options: WorkspaceOption[],
  query: string,
): WorkspaceOption[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return options;
  return options.filter((o) => {
    const text =
      `${o.tenant_name} ${o.project_name} ${o.environment_name} ${o.label}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
}
/** Workspace options from the identity document, for hosts that do not list them. */
export function optionsFromIdentity(
  identity: Identity | null | undefined,
): WorkspaceOption[] {
  return (
    identity?.workspaces.flatMap((t) =>
      t.projects.flatMap((p) =>
        p.environments.map((e) => ({
          tenant_id: t.id,
          tenant_name: t.name,
          project_id: p.id,
          project_name: p.name,
          environment_id: e.id,
          environment_name: e.name,
          label: `${t.name} / ${p.name} / ${e.name}`,
        })),
      ),
    ) ?? []
  );
}
/** Details an administrator needs to grant access; identifiers only, never credentials. */
export function accessRequestText(details: {
  platform: string;
  server: string;
  provider?: string | null;
  issuer?: string | null;
  account?: AccountHint | null;
}): string {
  const account = details.account;
  return [
    "Firefly Weave access request",
    `Platform: ${details.platform}${details.server ? ` (${details.server})` : ""}`,
    details.provider ? `Identity provider: ${details.provider}` : "",
    account?.provider_id ? `Identity provider ID: ${account.provider_id}` : "",
    account?.issuer || details.issuer
      ? `Issuer: ${account?.issuer || details.issuer}`
      : "",
    account?.display_name ? `Account: ${account.display_name}` : "",
    account
      ? `Subject: ${account.subject || "Not shared by your identity provider"}`
      : "",
  ]
    .filter(Boolean)
    .join("\n");
}

export type LoginEvent =
  | { kind: "waiting"; login: LoginStatus }
  | { kind: "authenticated" }
  | {
      kind: "ended";
      state: "cancelled" | "failed" | "timeout" | "lost";
      code: string;
    }
  | { kind: "stopped" };
const pendingState = (state: unknown) =>
  state === "starting" || state === "awaiting_user";

/**
 * Follows the host's pending sign-in with one backoff and one time limit.
 * The wizard shows it while open; the shell keeps following it after the
 * wizard closes, so a sign-in finished in the browser is still noticed.
 */
export class LoginWatcher {
  /** The latest status of the sign-in being followed. */
  login: LoginStatus | null = null;
  /** The view showing this sign-in; while set, other views only reflect it. */
  attendant: object | null = null;
  private started = 0;
  private delay = firstPollDelay;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private generation = 0;
  private listeners = new Set<(event: LoginEvent) => void>();
  constructor(
    private client: Pick<ConnectionClient, "login" | "cancelLogin">,
    private clock: () => number = () => Date.now(),
  ) {}
  get pending() {
    return pendingState(this.login?.state);
  }
  /** When Studio stops waiting for the sign-in being followed. */
  get deadline() {
    return loginDeadline(this.login, this.started);
  }
  get remaining() {
    return remainingTimeText(this.login, this.clock());
  }
  subscribe(listener: (event: LoginEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  /**
   * Follows `login`, a status from starting, polling or the connection
   * status. The sign-in already followed keeps its backoff and start time.
   */
  watch(login: LoginStatus | null | undefined) {
    if (!login) return;
    if (
      login.id &&
      login.id === this.login?.id &&
      this.pending &&
      pendingState(login.state)
    )
      return;
    this.stop();
    this.started = this.clock();
    this.delay = firstPollDelay;
    this.accept(login, this.generation);
  }
  /** Stops following; the host's sign-in is left as it is. */
  stop() {
    this.generation++;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.login = null;
  }
  private emit(event: LoginEvent) {
    for (const listener of [...this.listeners]) listener(event);
  }
  private accept(login: LoginStatus, generation: number) {
    const state = String(login.state ?? "");
    if (pendingState(state)) {
      // A status that omits the expiry keeps the one already stated.
      const expiry = login.expires_at ?? this.login?.expires_at ?? null;
      this.login =
        login.id === this.login?.id ? { ...login, expires_at: expiry } : login;
      this.schedule(String(login.id ?? ""), generation);
      this.emit({ kind: "waiting", login: this.login });
      return;
    }
    this.stop();
    if (state === "authenticated") this.emit({ kind: "authenticated" });
    else if (state === "cancelled" || state === "failed")
      this.emit({ kind: "ended", state, code: login.error_code ?? "" });
    else this.emit({ kind: "stopped" });
  }
  private schedule(id: string, generation: number) {
    if (this.timer) clearTimeout(this.timer);
    const delay = this.delay;
    this.delay = nextPollDelay(delay);
    this.timer = setTimeout(() => void this.poll(id, generation), delay);
  }
  private async poll(id: string, generation: number) {
    if (generation !== this.generation) return;
    this.timer = null;
    if (this.clock() > this.deadline) {
      this.stop();
      if (id) void this.client.cancelLogin(id).catch(() => undefined);
      this.emit({ kind: "ended", state: "timeout", code: "" });
      return;
    }
    try {
      const login = await this.client.login(id);
      if (generation !== this.generation) return;
      this.accept(login, generation);
    } catch (error) {
      if (generation !== this.generation) return;
      const plain = describeError(error);
      if (plain.code === "WV-STUDIO-SESSION") {
        this.stop();
        this.emit({ kind: "stopped" });
        return;
      }
      if ([0, 429, 502, 503, 504].includes(plain.status)) {
        // A busy or briefly unreachable host: keep waiting within the limit.
        this.schedule(id, generation);
        return;
      }
      this.stop();
      this.emit(
        plain.status === 404 || plain.code === "WV-STUDIO-LOGIN"
          ? { kind: "ended", state: "lost", code: plain.code }
          : { kind: "ended", state: "failed", code: plain.code },
      );
    }
  }
}

const connectionFileKeys = [
  "provider_id",
  "issuer",
  "client_id",
  "target",
  "account",
  "scopes",
  "trusted_endpoint_origins",
  "allow_loopback_http",
  "timeout",
  "login_timeout",
  "require_refresh_rotation",
];
/** Reads a non-secret connection file from an administrator; throws a plain Error. */
export function parseConnectionFile(
  text: string,
  filename: string,
): { name: string; login: Record<string, unknown> } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw Error(
      "This file isn't valid JSON. Ask your administrator for the connection file again.",
    );
  }
  const body = record(parsed);
  if (!body)
    throw Error(
      "This file isn't a connection file. Choose the JSON file your administrator gave you.",
    );
  if (
    "login" in body &&
    Object.keys(body).some((key) => !["name", "login"].includes(key))
  )
    throw Error(
      "Connection files may contain only a name and sign-in settings. Tokens and secrets are not accepted.",
    );
  const login = record(body["login"] ?? body);
  if (!login || Object.keys(login).some((k) => !connectionFileKeys.includes(k)))
    throw Error(
      "Choose a connection file with sign-in settings only. Tokens, secrets, and credential file paths are not accepted.",
    );
  const missing = ["provider_id", "issuer", "client_id", "target"].filter(
    (key) => typeof login[key] !== "string" || !String(login[key]).trim(),
  );
  if (missing.length)
    throw Error(
      `This connection file is missing ${missing.join(", ")}. Ask your administrator for a complete file.`,
    );
  const name =
    typeof body["name"] === "string" && "login" in body
      ? body["name"]
      : filename.replace(/\.json$/i, "");
  return {
    name,
    login: {
      scopes: ["openid", "profile", "email"],
      ...structuredClone(login),
    },
  };
}

/** Copies text; false when neither the clipboard API nor the legacy command works. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // WKWebView and insecure contexts can refuse the asynchronous clipboard.
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    return copied;
  } catch {
    return false;
  }
}
