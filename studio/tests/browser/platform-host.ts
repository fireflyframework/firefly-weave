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
// A stateful stand-in for the local Studio host's platform connection
// endpoints (design contract sections 5 and 9, snake_case JSON). It never
// issues tokens: sign-in only flips the fake credential state.
import { expect, Page, Route } from "@playwright/test";

export const capabilities = [
  "catalog.read",
  "definition.write",
  "definition.publish",
  "release.activate",
  "run.start",
  "run.read",
  "human_task.read",
  "simulate",
];
export interface Workspace {
  tenant_id: string;
  tenant_name: string;
  project_id: string;
  project_name: string;
  environment_id: string;
  environment_name: string;
  label: string;
}
const uuid = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
export function workspace(
  tenant: [number, string],
  project: [number, string],
  environment: [number, string],
): Workspace {
  return {
    tenant_id: uuid(tenant[0]),
    tenant_name: tenant[1],
    project_id: uuid(project[0]),
    project_name: project[1],
    environment_id: uuid(environment[0]),
    environment_name: environment[1],
    label: `${tenant[1]} / ${project[1]} / ${environment[1]}`,
  };
}
export const acme = [
  workspace([1, "Acme"], [10, "Payments"], [100, "Production"]),
  workspace([1, "Acme"], [10, "Payments"], [101, "Staging"]),
  workspace([1, "Acme"], [11, "Billing"], [110, "Production"]),
];
export const manyWorkspaces = [
  ...acme,
  workspace([2, "Globex"], [20, "Core"], [200, "Development"]),
  workspace([2, "Globex"], [20, "Core"], [201, "Test"]),
  workspace([2, "Globex"], [21, "Edge"], [210, "Production"]),
  workspace([3, "Initech"], [30, "Reports"], [300, "Production"]),
  workspace([3, "Initech"], [30, "Reports"], [301, "Staging"]),
  workspace([3, "Initech"], [31, "Ledger"], [310, "Production"]),
];
export const acmeOption = {
  provider_id: "acme",
  display_name: "Acme (Microsoft Entra ID)",
  issuer: "https://login.acme.example/tenant/v2.0",
  client_id: "weave-studio",
  scopes: ["openid", "profile", "offline_access"],
  trusted_endpoint_origins: ["https://device.acme.example"],
  allow_loopback_http: false,
  flows: ["browser", "device"],
  require_refresh_rotation: true,
  issuer_origin: "https://login.acme.example",
  checks: { browser: true, device: true },
  problem: null,
};
export const partnerOption = {
  ...acmeOption,
  provider_id: "partners",
  display_name: "Partner accounts",
  issuer: "https://partners.example/realms/acme",
  client_id: "studio-partners",
  issuer_origin: "https://partners.example",
  trusted_endpoint_origins: [],
  checks: { browser: false, device: false },
  problem: {
    code: "WV-CONNECT-PROVIDER",
    message:
      "Studio could not reach this sign-in service. Check your network or VPN, or ask your administrator.",
    detail: null,
  },
};
export const acmeDiscovery = {
  server: "https://weave.acme.example",
  display_name: "Acme Weave",
  api_version: "weave/api-v1",
  suggested_name: "Acme Weave",
  existing_profile: null,
  sign_in: [acmeOption],
};
export type Flow = "browser" | "device";
export type Revocation = "confirmed" | "unconfirmed" | "not_requested";
export interface SavedPlatform {
  name: string;
  server: string;
  display_name?: string | null;
  provider_name?: string | null;
  provider_id?: string;
  issuer?: string;
  client_id?: string;
  source?: "server" | "file" | "manual";
  workspace?: Workspace | null;
  account?: {
    subject: string | null;
    display_name: string | null;
    issuer: string;
    provider_id: string;
  } | null;
  /** Sign-in methods the administrator allows (section 9); both by default. */
  flows?: Flow[];
  signedIn?: boolean;
  /** An expired access token the host renews silently on the next check. */
  refreshable?: boolean;
  workspaces?: Workspace[];
}
export const acmePlatform = (
  overrides: Partial<SavedPlatform> = {},
): SavedPlatform => ({
  name: "Acme",
  server: "https://weave.acme.example",
  display_name: "Acme Weave",
  provider_name: acmeOption.display_name,
  provider_id: "acme",
  issuer: acmeOption.issuer,
  client_id: "weave-studio",
  source: "server",
  workspace: acme[0],
  account: {
    subject: "00u1-jane",
    display_name: "jane@acme.example",
    issuer: acmeOption.issuer,
    provider_id: "acme",
  },
  signedIn: true,
  workspaces: acme,
  ...overrides,
});
export const globexPlatform = (
  overrides: Partial<SavedPlatform> = {},
): SavedPlatform => ({
  name: "Globex",
  server: "https://weave.globex.example",
  display_name: "Globex",
  provider_name: "Globex Keycloak",
  provider_id: "globex",
  issuer: "https://sso.globex.example/realms/weave",
  client_id: "weave-studio",
  source: "server",
  workspace: null,
  account: null,
  signedIn: true,
  workspaces: manyWorkspaces.slice(3),
  ...overrides,
});
export interface Reply {
  status?: number;
  json: unknown;
  delay?: number;
}
export interface HostOptions {
  store?: boolean;
  platforms?: SavedPlatform[];
  active?: string | null;
  loginSupported?: boolean;
  /** Discovery answers keyed by the trimmed address the person typed. */
  discover?: Record<string, Reply>;
  /** Replaces the default `/studio/connection/test` answer. */
  test?: (host: PlatformHost) => Reply | undefined;
  /** A pending sign-in the host already holds (for example after a reload). */
  pendingLogin?: { flow: Flow };
  /**
   * Seconds from the start of a sign-in until the host stops waiting, sent as
   * `expires_at`; null sends none (an older host). Defaults to the host's 310.
   */
  loginExpiresIn?: number | null;
  /** Keep the first-run question (`preferences.start` "ask"); by default "local". */
  firstRun?: boolean;
  /** Flows the host accepts when they differ from what the profile view says. */
  enforcedFlows?: Flow[];
  /** What the identity provider answers to revocation on sign-out and remove. */
  revocation?: Revocation;
  /** `POST /studio/preferences` fails with this status. */
  preferencesStatus?: number;
  /** Removing with sign-out leaves the sign-in on this computer (store locked). */
  keepCredentialsOnRemove?: boolean;
}
/** The host's guidance when switching account with a code (section 9). */
export const switchWithCodeNotice =
  "Sign-in with a code cannot ask the sign-in page to offer another account. On the sign-in page in your browser, choose the account you want to use (sign out of the other account first if the page signs you in automatically).";
export interface Call {
  method: string;
  path: string;
  body: unknown;
}
const reply = (route: Route, value: Reply) =>
  route.fulfill({ status: value.status ?? 200, json: value.json });

export class PlatformHost {
  calls: Call[] = [];
  csrfViolations: string[] = [];
  platforms: SavedPlatform[];
  active: string | null;
  store: boolean;
  loginSupported: boolean;
  login: Record<string, unknown> = PlatformHost.idle();
  /** The host's sign-in status after a switch, a sign-out or at startup. */
  static idle(): Record<string, unknown> {
    return {
      id: null,
      state: "idle",
      flow: null,
      verification_uri: null,
      user_code: null,
      authorization_uri: null,
      error_code: null,
      expires_at: null,
      notice: null,
    };
  }
  preferences: { start: "ask" | "local" };
  private outcome: { state: string; error_code?: string } | null = null;
  private loginCount = 0;
  private expiresAt: number | null = null;
  constructor(private options: HostOptions) {
    this.platforms = structuredClone(options.platforms ?? []);
    this.active = options.active ?? null;
    this.store = options.store ?? true;
    this.loginSupported = options.loginSupported ?? true;
    this.preferences = { start: options.firstRun ? "ask" : "local" };
    if (options.pendingLogin) {
      this.loginCount++;
      this.expiresAt = this.expiry();
      this.login = this.awaiting(options.pendingLogin.flow, "login-0");
    }
  }
  /** Epoch seconds when a sign-in started now stops waiting. */
  private expiry() {
    const seconds =
      this.options.loginExpiresIn === undefined
        ? 310
        : this.options.loginExpiresIn;
    return seconds === null ? null : Date.now() / 1000 + seconds;
  }
  flowsOf(p: SavedPlatform | null): Flow[] {
    return p?.flows ?? ["browser", "device"];
  }
  get current() {
    return this.platforms.find((p) => p.name === this.active) ?? null;
  }
  requests(path: string, method = "POST") {
    return this.calls.filter((c) => c.path === path && c.method === method);
  }
  /** The next sign-in poll reports this outcome. */
  finish(state: "authenticated" | "failed" | "cancelled", error_code?: string) {
    this.outcome = { state, error_code };
  }
  session() {
    const p = this.current;
    return {
      paired: true,
      csrfToken: "csrf-1",
      version: "test",
      mode: p ? "connected" : "offline",
      connection: { configured: !!p, login_supported: this.loginSupported },
      profile: p
        ? {
            name: p.name,
            baseUrl: p.server,
            tenantId: p.workspace?.tenant_id ?? null,
            projectId: p.workspace?.project_id ?? null,
            environmentId: p.workspace?.environment_id ?? null,
          }
        : null,
    };
  }
  status() {
    const p = this.current;
    const signedIn = !!p?.signedIn;
    const refreshable = !!p && !signedIn && !!p.refreshable;
    return {
      configured: !!p,
      login_supported: !!p && this.loginSupported,
      store: this.store
        ? {
            available: true,
            location:
              "/Users/test/Library/Application Support/Firefly Weave/profiles.json",
          }
        : { available: false, location: null },
      profile: p
        ? {
            name: p.name,
            server: p.server,
            saved: this.store,
            source: p.source ?? "server",
            display_name: p.display_name ?? null,
            provider_name: p.provider_name ?? null,
            provider_id: p.provider_id ?? null,
            issuer: p.issuer ?? null,
            issuer_origin: p.issuer ? new URL(p.issuer).origin : null,
            client_id: p.client_id ?? null,
            scopes: ["openid", "profile"],
            trusted_endpoint_origins: [],
            flows: this.flowsOf(p),
            workspace: p.workspace ? { ...p.workspace } : null,
            account: p.account ?? null,
            credential_store: "native",
          }
        : null,
      profiles: this.store
        ? this.platforms.map((x) => ({
            name: x.name,
            server: x.server,
            active: x.name === this.active,
            display_name: x.display_name ?? null,
            provider_name: x.provider_name ?? null,
            workspace_label: x.workspace?.label ?? null,
            account_label: x.account?.display_name ?? null,
          }))
        : [],
      authentication: {
        authenticated: signedIn,
        reauthentication_required: !signedIn && !refreshable,
        refresh_available: signedIn || refreshable,
        state: !p
          ? "signed_out"
          : signedIn
            ? "signed_in"
            : refreshable
              ? "expired"
              : "signed_out",
        expires_at: null,
        account: p?.account ?? null,
      },
      login: this.login,
      preferences: { ...this.preferences },
    };
  }
  changed(extra: Record<string, unknown> = {}) {
    return { session: this.session(), connection: this.status(), ...extra };
  }
  identity() {
    const p = this.current;
    const options = p?.workspaces ?? [];
    const tenants = new Map<string, Record<string, unknown>>();
    for (const o of options) {
      const tenant = (tenants.get(o.tenant_id) ?? {
        id: o.tenant_id,
        name: o.tenant_name,
        projects: [],
      }) as { projects: Record<string, unknown>[] };
      tenants.set(o.tenant_id, tenant as Record<string, unknown>);
      let project = tenant.projects.find((x) => x["id"] === o.project_id) as
        | { id?: unknown; name?: unknown; environments: unknown[] }
        | undefined;
      if (!project) {
        project = { id: o.project_id, name: o.project_name, environments: [] };
        tenant.projects.push(project as Record<string, unknown>);
      }
      project.environments.push({
        id: o.environment_id,
        name: o.environment_name,
      });
    }
    return {
      principal_id: "principal-jane",
      kind: "human",
      grants: [...tenants.keys()].map((tenant_id) => ({
        role: "developer",
        scope: { tenant_id },
        resources: [],
        capabilities,
      })),
      workspaces: [...tenants.values()],
      truncated: false,
    };
  }
  private awaiting(flow: string, id: string) {
    return {
      id,
      state: "awaiting_user",
      flow,
      verification_uri:
        flow === "device" ? "https://device.acme.example/activate" : null,
      user_code: flow === "device" ? "WDJB-MJHT" : null,
      authorization_uri:
        flow === "device"
          ? null
          : "https://login.acme.example/authorize?client_id=weave-studio&code_challenge=x&state=y",
      error_code: null,
      expires_at: this.expiresAt,
      notice: this.login["notice"] ?? null,
    };
  }
  private end(state: string, error_code?: string) {
    this.login = {
      ...this.login,
      state,
      error_code: error_code ?? null,
      verification_uri: null,
      user_code: null,
      authorization_uri: null,
      expires_at: null,
    };
  }
  async handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    let body: unknown = null;
    try {
      body = request.postDataJSON();
    } catch {
      body = request.postData();
    }
    this.calls.push({ method, path, body });
    if (method !== "GET" && request.headers()["x-weave-csrf"] !== "csrf-1")
      this.csrfViolations.push(`${method} ${path}`);
    const input = (body ?? {}) as Record<string, unknown>;
    if (path === "/studio/session")
      return reply(route, { json: this.session() });
    if (path === "/studio/connection")
      return reply(route, { json: this.status() });
    if (path === "/studio/preferences") {
      if (this.options.preferencesStatus)
        // The host's answer when its settings folder can't be written.
        return reply(route, {
          status: this.options.preferencesStatus,
          json: {
            status: this.options.preferencesStatus,
            code: "WV-PROFILE-STORE",
            message:
              "Studio could not save this preference in its settings folder. Check that the folder is yours, then try again.",
          },
        });
      const start = input["start"];
      if (start !== "ask" && start !== "local")
        return reply(route, {
          status: 422,
          json: { status: 422, code: "WV-STUDIO-REQUEST", message: "x" },
        });
      this.preferences = { start };
      return reply(route, { json: { preferences: { ...this.preferences } } });
    }
    if (path === "/studio/connection/discover") {
      const typed = String(input["server"] ?? "").trim();
      const answer = this.options.discover?.[typed];
      if (answer?.delay)
        await new Promise((resolve) => setTimeout(resolve, answer.delay));
      return reply(
        route,
        answer ?? {
          status: 502,
          json: {
            status: 502,
            code: "WV-CONNECT-UNREACHABLE",
            message:
              "Studio could not reach that server. Check the address and your network or VPN, then try again.",
          },
        },
      );
    }
    if (path === "/studio/connection/profiles") {
      const name = String(input["name"]);
      if (
        this.platforms.some((p) => p.name.toLowerCase() === name.toLowerCase())
      )
        return reply(route, {
          status: 409,
          json: {
            status: 409,
            code: "WV-PROFILE-EXISTS",
            message:
              "A saved platform already uses that name. Choose another name.",
          },
        });
      const option = [acmeOption, partnerOption].find(
        (o) => o.provider_id === input["provider_id"],
      );
      if (
        !option ||
        option.issuer !== input["issuer"] ||
        option.client_id !== input["client_id"]
      )
        return reply(route, {
          status: 409,
          json: {
            status: 409,
            code: "WV-PROFILE-CHANGED",
            message:
              "The server's sign-in settings changed. Review them again.",
          },
        });
      this.platforms.push({
        name,
        server: String(input["server"]),
        display_name: "Acme Weave",
        provider_name: option.display_name,
        provider_id: option.provider_id,
        issuer: option.issuer,
        client_id: option.client_id,
        workspace: null,
        account: null,
        signedIn: false,
        workspaces: acme,
        flows: option.flows as Flow[],
      });
      this.active = name;
      return reply(route, { json: this.changed() });
    }
    if (path === "/studio/connection/configure") {
      const login = input["login"] as Record<string, unknown>;
      const name = String(input["name"]);
      this.platforms.push({
        name,
        server: String(login["target"]),
        provider_id: String(login["provider_id"]),
        issuer: String(login["issuer"]),
        client_id: String(login["client_id"]),
        source: "file",
        workspace: null,
        account: null,
        signedIn: false,
        workspaces: acme,
      });
      this.active = name;
      return reply(route, { json: this.session() });
    }
    if (path === "/studio/connection/activate") {
      this.active = String(input["name"]);
      this.login = PlatformHost.idle();
      return reply(route, { json: this.changed() });
    }
    if (path === "/studio/connection/disconnect") {
      this.active = null;
      this.login = PlatformHost.idle();
      return reply(route, { json: this.changed() });
    }
    if (path === "/studio/connection/remove") {
      const name = String(input["name"]);
      const removed = this.platforms.find((p) => p.name === name);
      if (!removed)
        return reply(route, {
          status: 404,
          json: {
            status: 404,
            code: "WV-PROFILE-NOT-FOUND",
            message: "No saved platform has that name.",
          },
        });
      this.platforms = this.platforms.filter((p) => p !== removed);
      if (this.active === name) {
        this.active = null;
        this.login = PlatformHost.idle();
      }
      const revoked = removed.signedIn || removed.refreshable;
      return reply(route, {
        json: this.changed({
          sign_out: this.options.keepCredentialsOnRemove
            ? { credentials_removed: false, remote_revocation: "unconfirmed" }
            : {
                credentials_removed: true,
                remote_revocation: revoked
                  ? (this.options.revocation ?? "confirmed")
                  : "not_requested",
              },
        }),
      });
    }
    if (path === "/studio/connection/logout") {
      const p = this.current;
      const revoked = !!(p?.signedIn || p?.refreshable);
      this.login = PlatformHost.idle();
      if (p) {
        p.signedIn = false;
        p.refreshable = false;
        p.account = null;
      }
      return reply(route, {
        json: this.changed({
          remote_revocation: revoked
            ? (this.options.revocation ?? "confirmed")
            : "not_requested",
        }),
      });
    }
    if (path === "/studio/connection/login/start") {
      const flow = String(input["flow"] ?? "browser");
      const allowed = this.options.enforcedFlows ?? this.flowsOf(this.current);
      if (!allowed.includes(flow as Flow))
        return reply(route, {
          status: 409,
          json: {
            status: 409,
            code: "WV-AUTH-FLOW",
            message:
              "This platform does not allow that sign-in method. Use another one.",
          },
        });
      this.outcome = null;
      this.expiresAt = this.expiry();
      this.login = {
        id: `login-${++this.loginCount}`,
        state: "starting",
        flow,
        verification_uri: null,
        user_code: null,
        authorization_uri: null,
        error_code: null,
        expires_at: this.expiresAt,
        // A code can't ask the provider for another account: the host explains.
        notice:
          input["switch_account"] === true && flow === "device"
            ? switchWithCodeNotice
            : null,
      };
      return reply(route, { status: 202, json: this.login });
    }
    const login =
      /^\/studio\/connection\/login\/([^/]+)(\/cancel|\/open)?$/.exec(path);
    if (login) {
      if (login[1] !== this.login["id"])
        return reply(route, {
          status: 404,
          json: {
            status: 404,
            code: "WV-STUDIO-LOGIN",
            message: "Login flow unavailable; refresh connection status",
          },
        });
      if (login[2] === "/cancel") {
        this.end("cancelled");
        return reply(route, { json: this.login });
      }
      if (login[2] === "/open")
        return reply(route, { json: { ...this.login, opened: true } });
      if (this.outcome) {
        const { state, error_code } = this.outcome;
        this.outcome = null;
        if (state === "authenticated" && this.current) {
          this.current.signedIn = true;
          this.current.account = acmePlatform().account!;
        }
        this.end(state, error_code);
      } else if (this.login["state"] === "starting")
        this.login = this.awaiting(
          String(this.login["flow"]),
          String(this.login["id"]),
        );
      return reply(route, { json: this.login });
    }
    if (path === "/studio/connection/test") {
      const custom = this.options.test?.(this);
      if (custom) return reply(route, custom);
      const p = this.current;
      if (!p)
        return reply(route, {
          status: 409,
          json: {
            status: 409,
            code: "WV-STUDIO-CONNECTION",
            message: "Choose a platform before checking the connection.",
          },
        });
      if (!p.signedIn && p.refreshable) {
        // The check renews the expired access token with the refresh token.
        p.signedIn = true;
        p.refreshable = false;
      }
      if (!p.signedIn)
        return reply(route, {
          status: 401,
          json: {
            status: 401,
            code: "WV-AUTH-REQUIRED",
            message: "Your sign-in has ended. Sign in again to continue.",
          },
        });
      return reply(route, {
        json: {
          session: this.session(),
          identity: this.identity(),
          workspaces: p.workspaces ?? [],
          truncated: false,
          workspace_revoked: false,
        },
      });
    }
    if (path === "/studio/scope") {
      const p = this.current!;
      p.workspace =
        (p.workspaces ?? []).find(
          (w) =>
            w.tenant_id === input["tenantId"] &&
            w.project_id === input["projectId"] &&
            w.environment_id === input["environmentId"],
        ) ?? null;
      return reply(route, { json: this.session() });
    }
    if (path === "/studio/api/api/v1/identity")
      return this.current?.signedIn
        ? reply(route, { json: this.identity() })
        : reply(route, {
            status: 401,
            json: { status: 401, code: "WV-AUTH-REQUIRED" },
          });
    if (path.startsWith("/studio/api/"))
      return reply(route, { json: { items: [], next_cursor: null } });
    if (path === "/studio/local/validate")
      return reply(route, {
        json: {
          validationOk: true,
          errorCount: 0,
          diagnostics: [],
          partial: true,
        },
      });
    return reply(route, { status: 404, json: { message: "Not mocked" } });
  }
}

/** Installs the fake host and opens Studio at `path`. */
export async function platformHost(
  page: Page,
  options: HostOptions = {},
  path = "/",
): Promise<PlatformHost> {
  const host = new PlatformHost(options);
  await page.route(
    (url) => url.pathname.startsWith("/studio/"),
    (route) => host.handle(route),
  );
  await page.goto(path);
  return host;
}

/** No horizontal page scroll and no control outside the viewport. */
export async function expectNoOverflow(page: Page, name: string) {
  const result = await page.evaluate(() => {
    const width = window.innerWidth;
    const offscreen = [
      ...document.querySelectorAll<HTMLElement>(
        'button, a[href], select, input:not([type="hidden"]), textarea, output',
      ),
    ]
      .filter((element) => {
        if (element.closest(".canvas")) return false;
        const box = element.getBoundingClientRect();
        if (box.width === 0 || box.height === 0) return false;
        return box.left < -1 || box.right > width + 1;
      })
      .map(
        (element) =>
          `${element.tagName}:${(element.getAttribute("aria-label") || element.textContent || "").trim().slice(0, 40)}`,
      );
    const scrollers = [
      document.scrollingElement!,
      ...document.querySelectorAll<HTMLElement>(".page-content, .main-shell"),
    ].filter((element) => element.scrollWidth > element.clientWidth + 1);
    return {
      width,
      scrollWidth: document.scrollingElement!.scrollWidth,
      offscreen,
      scrollers: scrollers.map((e) => e.className || e.tagName),
    };
  });
  expect(result.scrollWidth, `${name} page scroll width`).toBeLessThanOrEqual(
    result.width,
  );
  expect(result.scrollers, `${name} horizontal scroll containers`).toEqual([]);
  expect(result.offscreen, `${name} controls outside the viewport`).toEqual([]);
}

/** Every form control in the wizard and Settings has an accessible name. */
export async function expectLabelledControls(page: Page) {
  const unlabeled = await page.evaluate(() =>
    [
      ...document.querySelectorAll<HTMLInputElement>(
        "weave-connection-wizard input, weave-connection-wizard select, weave-connection-wizard textarea, weave-platform-list input",
      ),
    ]
      .filter((element) => {
        if (element.getAttribute("aria-label")?.trim()) return false;
        const by = element.getAttribute("aria-labelledby");
        if (by && document.getElementById(by)?.textContent?.trim())
          return false;
        return ![...(element.labels ?? [])].some((label) =>
          label.textContent?.trim(),
        );
      })
      .map((element) => element.outerHTML.slice(0, 100)),
  );
  expect(unlabeled).toEqual([]);
}
