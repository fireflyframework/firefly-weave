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
import { describe, it, expect, vi, afterEach } from "vitest";
import { ApiError, StudioApi } from "../src/app/api";
import {
  ConnectionClient,
  LoginEvent,
  LoginStatus,
  LoginWatcher,
  accessRequestText,
  accountHint,
  allowedFlows,
  connectProblem,
  existingProfileName,
  filterWorkspaces,
  groupWorkspaces,
  hostOf,
  isConnectionStatus,
  loginDeadline,
  loginOutcome,
  nextPollDelay,
  optionsFromIdentity,
  parseConnectionFile,
  platformState,
  profileNameError,
  problemText,
  safeSignInUrl,
  remainingTimeText,
  serverAddressError,
  sessionUsable,
  signInMethods,
  signOutReport,
  signedInText,
  stateLabel,
  workspaceKey,
  workspaceLabel,
  WorkspaceOption,
} from "../src/app/connection";

const option = (
  tenant: string,
  project: string,
  environment: string,
): WorkspaceOption => ({
  tenant_id: `t-${tenant}`,
  tenant_name: tenant,
  project_id: `p-${tenant}-${project}`,
  project_name: project,
  environment_id: `e-${tenant}-${project}-${environment}`,
  environment_name: environment,
  label: `${tenant} / ${project} / ${environment}`,
});

describe("platform names", () => {
  it("accepts the profile name grammar", () => {
    expect(profileNameError("Acme production")).toBe("");
    expect(profileNameError("acme_prod-2.eu")).toBe("");
    expect(profileNameError("a".repeat(64))).toBe("");
  });
  it("explains invalid names in plain language", () => {
    expect(profileNameError("")).toContain("Enter a name");
    expect(profileNameError("   ")).toContain("Enter a name");
    expect(profileNameError("a".repeat(65))).toContain("64 characters");
    expect(profileNameError(" leading")).toContain("letter or digit");
    expect(profileNameError("trailing ")).toContain("letter or digit");
    expect(profileNameError("acme/prod")).toContain("letter or digit");
    expect(profileNameError("café")).toContain("letter or digit");
  });
});

describe("server addresses", () => {
  it("requires a value without spaces", () => {
    expect(serverAddressError("")).toContain("Enter the server address");
    expect(serverAddressError("weave example.com")).toContain("spaces");
    expect(serverAddressError(" weave.example.com ")).toBe("");
    expect(
      serverAddressError(`weave.example.com/${"a".repeat(2048)}`),
    ).toContain("too long");
  });
  it("finds the host of an address", () => {
    expect(hostOf("https://weave.example.com")).toBe("weave.example.com");
    expect(hostOf("https://login.example.com:8443/realms/a")).toBe(
      "login.example.com:8443",
    );
    expect(hostOf("weave.example.com")).toBe("weave.example.com");
    expect(hostOf("")).toBe("");
  });
});

describe("connection problems", () => {
  it("never makes the support code the primary text", () => {
    for (const code of [
      "WV-CONNECT-ADDRESS",
      "WV-CONNECT-INSECURE",
      "WV-CONNECT-BLOCKED",
      "WV-CONNECT-UNREACHABLE",
      "WV-CONNECT-TLS",
      "WV-CONNECT-TIMEOUT",
      "WV-CONNECT-REDIRECT",
      "WV-CONNECT-NOT-WEAVE",
      "WV-CONNECT-INCOMPATIBLE",
      "WV-CONNECT-NO-SIGN-IN",
      "WV-CONNECT-PROVIDER",
      "WV-SOMETHING-NEW",
    ]) {
      const problem = connectProblem(code, "weave.example.com");
      expect(problem.title).not.toContain("WV-");
      expect(problem.hint).not.toContain("WV-");
      expect(problem.code).toBe(code);
    }
  });
  it("suggests HTTPS for an insecure address", () => {
    const problem = connectProblem(
      "WV-CONNECT-INSECURE",
      "http://weave.example.com",
    );
    expect(problem.action).toEqual({
      kind: "address",
      label: "Use https://weave.example.com",
      value: "https://weave.example.com",
    });
  });
  it("offers a redirect target only when it is an HTTPS origin", () => {
    expect(
      connectProblem(
        "WV-CONNECT-REDIRECT",
        "weave.example.com",
        "https://api.weave.example.com",
      ).action,
    ).toEqual({
      kind: "address",
      label: "Use https://api.weave.example.com",
      value: "https://api.weave.example.com",
    });
    expect(
      connectProblem(
        "WV-CONNECT-REDIRECT",
        "weave.example.com",
        "http://evil.example.com",
      ).action,
    ).toBeUndefined();
    expect(
      connectProblem(
        "WV-CONNECT-REDIRECT",
        "weave.example.com",
        "https://api.example.com/login?x=1",
      ).action,
    ).toBeUndefined();
  });
  it("mentions the VPN when a server is unreachable", () => {
    expect(
      connectProblem("WV-CONNECT-UNREACHABLE", "weave.example.com").hint,
    ).toContain("VPN");
  });
  it("points to a connection file when no sign-in is published", () => {
    const problem = connectProblem(
      "WV-CONNECT-NO-SIGN-IN",
      "weave.example.com",
    );
    expect(problem.hint).toContain(
      "Ask your administrator for a connection file",
    );
    expect(problem.action?.kind).toBe("connection-file");
  });
  it("asks to update for an incompatible server", () => {
    expect(
      connectProblem("WV-CONNECT-INCOMPATIBLE", "weave.example.com").hint,
    ).toContain("Update");
  });
});

describe("sign-in outcomes", () => {
  it("explains each outcome without raw codes", () => {
    const cases: [string, string, string][] = [
      ["cancelled", "", "Sign-in canceled"],
      ["failed", "WV-AUTH-EXPIRED", "Sign-in timed out"],
      ["timeout", "", "Sign-in timed out"],
      ["failed", "WV-AUTH-DENIED", "Sign-in was declined"],
      ["failed", "WV-AUTH-STORE", "couldn't save your sign-in"],
      ["failed", "WV-AUTH-OFFLINE", "couldn't reach the identity provider"],
      ["failed", "WV-AUTH-PROVIDER", "Sign-in didn't work"],
      ["lost", "WV-STUDIO-LOGIN", "no longer active"],
      [
        "failed",
        "WV-AUTH-DEVICE-UNAVAILABLE",
        "doesn't offer sign-in with a code",
      ],
      ["failed", "WV-AUTH-SUPERSEDED", "Another sign-in finished first"],
      ["failed", "WV-AUTH-CALLBACK", "unexpected answer"],
    ];
    for (const [state, code, title] of cases) {
      const outcome = loginOutcome(state, code);
      expect(outcome.title).toContain(title);
      expect(outcome.title + outcome.hint).not.toContain("WV-");
    }
  });
  it("backs off polling from 1.5 to at most 5 seconds", () => {
    const delays = [1500];
    for (let i = 0; i < 6; i++) delays.push(nextPollDelay(delays.at(-1)!));
    expect(delays[0]).toBe(1500);
    expect(delays[1]).toBeGreaterThan(1500);
    expect(Math.max(...delays)).toBe(5000);
  });
  it("caps waiting at the host's expiry, or 15 minutes without one", () => {
    const now = Date.UTC(2026, 9, 1, 12, 0, 0);
    expect(loginDeadline({}, now)).toBe(now + 15 * 60 * 1000);
    expect(loginDeadline({ expires_at: null }, now)).toBe(now + 15 * 60 * 1000);
    // The host stops waiting at expires_at; Studio gives it a moment to say so.
    const expiry = now / 1000 + 310.5;
    expect(loginDeadline({ expires_at: expiry }, now)).toBe(
      expiry * 1000 + 2000,
    );
    // Nonsense values fall back to the default cap.
    expect(loginDeadline({ expires_at: -5 }, now)).toBe(now + 15 * 60 * 1000);
    expect(loginDeadline({ expires_at: now }, now)).toBe(now + 15 * 60 * 1000);
  });
  it("states the time left only when the host gives an expiry", () => {
    const now = Date.UTC(2026, 9, 1, 12, 0, 0);
    const at = (seconds: number) => ({ expires_at: now / 1000 + seconds });
    expect(remainingTimeText({}, now)).toBe("");
    expect(remainingTimeText({ expires_at: null }, now)).toBe("");
    expect(remainingTimeText(at(310), now)).toBe("About 5 minutes left");
    expect(remainingTimeText(at(600), now)).toBe("About 10 minutes left");
    expect(remainingTimeText(at(80), now)).toBe("About 1 minute left");
    expect(remainingTimeText(at(45), now)).toBe("Less than a minute left");
    expect(remainingTimeText(at(-3), now)).toBe("Less than a minute left");
  });
  it("offers only the sign-in methods the administrator allows", () => {
    expect(allowedFlows(undefined)).toEqual(["browser", "device"]);
    expect(allowedFlows(null)).toEqual(["browser", "device"]);
    expect(allowedFlows(["device"])).toEqual(["device"]);
    expect(allowedFlows(["browser", "device"], ["device"])).toEqual([
      "browser",
    ]);
    expect(allowedFlows([])).toEqual([]);
  });
  it("explains a refused sign-in method", () => {
    const outcome = loginOutcome("failed", "WV-AUTH-FLOW");
    expect(outcome.title).toBe(
      "Your administrator doesn't allow this sign-in method",
    );
    expect(outcome.title + outcome.hint).not.toContain("WV-");
  });
  it("only links to HTTPS or loopback sign-in pages", () => {
    expect(safeSignInUrl("https://login.example.com/device")).toBe(
      "https://login.example.com/device",
    );
    expect(safeSignInUrl("http://localhost:18080/realms/x")).toBe(
      "http://localhost:18080/realms/x",
    );
    expect(safeSignInUrl("http://login.example.com")).toBe("");
    expect(safeSignInUrl("javascript:alert(1)")).toBe("");
    expect(safeSignInUrl(null)).toBe("");
  });
  it("lists the methods an option allows", () => {
    expect(
      signInMethods({ flows: ["browser", "device"], checks: undefined }),
    ).toEqual(["browser", "device"]);
    expect(
      signInMethods({
        flows: ["browser", "device"],
        checks: { browser: true, device: false },
      }),
    ).toEqual(["browser"]);
    expect(problemText({ code: "WV-CONNECT-PROVIDER", message: "" })).toContain(
      "identity provider",
    );
    expect(problemText("The provider has no device endpoint.")).toBe(
      "The provider has no device endpoint.",
    );
    expect(problemText(null)).toBe("");
  });
});

describe("connection status", () => {
  it("recognizes host payloads and rejects other documents", () => {
    expect(isConnectionStatus({ configured: false, profiles: [] })).toBe(true);
    expect(
      isConnectionStatus({ configured: true, login_supported: true }),
    ).toBe(true);
    expect(isConnectionStatus({ message: "HTTP 200" })).toBe(false);
    expect(isConnectionStatus(null)).toBe(false);
    expect(isConnectionStatus([])).toBe(false);
  });
  it("labels platform states", () => {
    expect(stateLabel("signed_in")).toBe("Signed in");
    expect(stateLabel("expired")).toBe("Session expired");
    expect(stateLabel("signed_out")).toBe("Signed out");
    expect(stateLabel(null)).toBe("Not signed in");
    const status = {
      profile: { name: "Acme", server: "https://a" },
      profiles: [
        { name: "Acme", server: "https://a" },
        { name: "Other", server: "https://o", state: "expired" as const },
        { name: "Third", server: "https://t" },
      ],
      authentication: { state: "signed_in" as const },
    };
    expect(platformState(status.profiles[0], status)).toBe("signed_in");
    expect(platformState(status.profiles[1], status)).toBe("expired");
    expect(platformState(status.profiles[2], status)).toBeNull();
  });
  it("treats a renewable expired session as usable", () => {
    expect(sessionUsable({ state: "signed_in" })).toBe(true);
    expect(sessionUsable({ state: "expired", refresh_available: true })).toBe(
      true,
    );
    expect(sessionUsable({ state: "expired", refresh_available: false })).toBe(
      false,
    );
    expect(
      sessionUsable({ state: "signed_out", refresh_available: true }),
    ).toBe(false);
    expect(sessionUsable({ state: "in_progress" })).toBe(false);
    // Older hosts report only `authenticated`.
    expect(sessionUsable({ authenticated: true })).toBe(true);
    expect(sessionUsable({ authenticated: false })).toBe(false);
    expect(sessionUsable(null)).toBe(false);
  });
  it("names the signed-in account only with a name the provider shared", () => {
    const hint = (display_name: string | null) => ({
      subject: "34a7eb09-217e-4e7d-8877-8e1b01c2b3c5",
      display_name,
      issuer: "https://sso.example/realms/weave",
      provider_id: "sso",
    });
    expect(signedInText("Signed in", hint("alice"))).toBe("Signed in as alice");
    // A bare subject is an ID, not a name: the top bar keeps it out.
    expect(signedInText("Signed in", hint(null))).toBe("Signed in");
    expect(signedInText("Signed in", hint("  "))).toBe("Signed in");
    expect(signedInText("Signed in", null)).toBe("Signed in");
    expect(signedInText("Session expired", hint("alice"))).toBe(
      "Session expired",
    );
  });
  it("reads an existing profile name from either shape", () => {
    expect(existingProfileName("Acme")).toBe("Acme");
    expect(existingProfileName({ name: "Acme" })).toBe("Acme");
    expect(existingProfileName(null)).toBe("");
  });
});

describe("workspaces", () => {
  const options = [
    option("Acme", "Payments", "Production"),
    option("Acme", "Payments", "Staging"),
    option("Acme", "Billing", "Production"),
    option("Globex", "Core", "Development"),
  ];
  it("groups tenant > project > environment in order", () => {
    const groups = groupWorkspaces(options);
    expect(groups.map((g) => g.tenant_name)).toEqual(["Acme", "Globex"]);
    expect(groups[0].projects.map((p) => p.project_name)).toEqual([
      "Payments",
      "Billing",
    ]);
    expect(
      groups[0].projects[0].environments.map((e) => e.environment_name),
    ).toEqual(["Production", "Staging"]);
  });
  it("searches across every level", () => {
    expect(filterWorkspaces(options, "glob").length).toBe(1);
    expect(filterWorkspaces(options, "PRODUCTION").length).toBe(2);
    expect(filterWorkspaces(options, "billing prod").length).toBe(1);
    expect(filterWorkspaces(options, "").length).toBe(4);
  });
  it("labels a saved selection", () => {
    expect(
      workspaceLabel({
        tenant_id: "t",
        project_id: "p",
        environment_id: "e",
        tenant_name: "Acme",
        project_name: "Payments",
        environment_name: "Production",
      }),
    ).toBe("Payments / Production");
    expect(
      workspaceLabel({ tenant_id: "t", project_id: "p", environment_id: "e" }),
    ).toBe("Workspace selected");
    expect(workspaceLabel(null)).toBe("");
    expect(workspaceKey(options[0])).toBe(
      "t-Acme/p-Acme-Payments/e-Acme-Payments-Production",
    );
  });
  it("derives options from an identity for older hosts", () => {
    const derived = optionsFromIdentity({
      principal_id: "p",
      kind: "human",
      grants: [],
      truncated: false,
      workspaces: [
        {
          id: "t",
          name: "Acme",
          projects: [
            {
              id: "p",
              name: "Payments",
              environments: [{ id: "e", name: "Production" }],
            },
          ],
        },
      ],
    });
    expect(derived).toEqual([
      {
        tenant_id: "t",
        tenant_name: "Acme",
        project_id: "p",
        project_name: "Payments",
        environment_id: "e",
        environment_name: "Production",
        label: "Acme / Payments / Production",
      },
    ]);
    expect(optionsFromIdentity(null)).toEqual([]);
  });
  it("writes access request details without secrets", () => {
    const text = accessRequestText({
      platform: "Acme",
      server: "https://weave.acme.example",
      provider: "Acme (Microsoft Entra ID)",
      issuer: "https://login.acme.example/tenant/v2.0",
      account: {
        subject: "00u1",
        display_name: "jane@acme.example",
        issuer: "https://login.acme.example/tenant/v2.0",
        provider_id: "acme",
      },
    });
    expect(text).toContain("Platform: Acme (https://weave.acme.example)");
    expect(text).toContain("Account: jane@acme.example");
    expect(text).toContain("Subject: 00u1");
    expect(text).toContain("Identity provider ID: acme");
    expect(text.toLowerCase()).not.toContain("token");
  });
  it("says when the identity provider shares no subject", () => {
    const text = accessRequestText({
      platform: "Acme",
      server: "https://weave.acme.example",
      account: {
        subject: null,
        display_name: null,
        issuer: "https://login.acme.example/tenant/v2.0",
        provider_id: "acme",
      },
    });
    expect(text).toContain("Subject: Not shared by your identity provider");
    expect(text).toContain("Issuer: https://login.acme.example/tenant/v2.0");
    expect(
      accessRequestText({ platform: "Acme", server: "", account: null }),
    ).not.toContain("Subject");
  });
  it("reads an account hint whose subject may be missing", () => {
    expect(
      accountHint({
        subject: null,
        display_name: null,
        issuer: "https://login.acme.example",
        provider_id: "acme",
      }),
    ).toEqual({
      subject: null,
      display_name: null,
      issuer: "https://login.acme.example",
      provider_id: "acme",
    });
    expect(accountHint({ subject: "00u1" })?.subject).toBe("00u1");
    expect(accountHint(null)).toBeNull();
    expect(accountHint({})).toBeNull();
    expect(accountHint({ subject: 42 })).toBeNull();
  });
});

describe("connection files", () => {
  const login = {
    provider_id: "acme",
    issuer: "https://login.acme.example",
    client_id: "studio",
    target: "https://weave.acme.example",
    account: "operator",
  };
  it("reads a bare login configuration or a named one", () => {
    expect(parseConnectionFile(JSON.stringify(login), "acme.json")).toEqual({
      name: "acme",
      login: { scopes: ["openid", "profile", "email"], ...login },
    });
    const named = parseConnectionFile(
      JSON.stringify({ name: "Acme prod", login: { ...login, scopes: ["x"] } }),
      "file.json",
    );
    expect(named.name).toBe("Acme prod");
    expect(named.login["scopes"]).toEqual(["x"]);
  });
  it("rejects tokens, secrets and unknown keys", () => {
    expect(() =>
      parseConnectionFile(
        JSON.stringify({ access_token: "never-import" }),
        "x.json",
      ),
    ).toThrow("Tokens, secrets, and credential file paths are not accepted.");
    expect(() =>
      parseConnectionFile(
        JSON.stringify({ name: "x", login, refresh_token: "r" }),
        "x.json",
      ),
    ).toThrow("Tokens and secrets are not accepted.");
    expect(() =>
      parseConnectionFile(
        JSON.stringify({ ...login, client_secret: "s" }),
        "x.json",
      ),
    ).toThrow("not accepted");
  });
  it("explains invalid files", () => {
    expect(() => parseConnectionFile("{", "x.json")).toThrow("valid JSON");
    expect(() => parseConnectionFile("[]", "x.json")).toThrow(
      "isn't a connection file",
    );
    expect(() =>
      parseConnectionFile(JSON.stringify({ provider_id: "acme" }), "x.json"),
    ).toThrow("missing");
  });
});

describe("connection client", () => {
  it("gives slow platform checks more time than ordinary requests", async () => {
    const calls: { path: string; timeout: number | undefined }[] = [];
    const api = new StudioApi();
    api.request = async <T>(
      path: string,
      _method?: string,
      _body?: unknown,
      _headers?: Record<string, string>,
      timeout?: number,
    ) => {
      calls.push({ path, timeout });
      return {} as T;
    };
    const client = new ConnectionClient(api);
    await client.status();
    await client.discover("weave.example.com");
    await client.test();
    await client.logout();
    await client.remove("Acme", true);
    await client.selectWorkspace({
      tenant_id: "t",
      project_id: "p",
      environment_id: "e",
    });
    await client.login("login-1");
    const slow = calls.filter(
      (c) => c.path !== "/studio/connection/login/login-1",
    );
    expect(slow.every((c) => (c.timeout ?? 0) >= 45000)).toBe(true);
    // Sign-in polls stay short: the host answers them from memory.
    expect(calls.at(-1)?.timeout).toBeUndefined();
  });
  it("saves the start preference on the host", async () => {
    const calls: { path: string; method?: string; body?: unknown }[] = [];
    const api = new StudioApi();
    api.request = async <T>(path: string, method?: string, body?: unknown) => {
      calls.push({ path, method, body });
      return { preferences: { start: "local" } } as T;
    };
    const result = await new ConnectionClient(api).savePreferences("local");
    expect(calls).toEqual([
      {
        path: "/studio/preferences",
        method: "POST",
        body: { start: "local" },
      },
    ]);
    expect(result.preferences.start).toBe("local");
  });
  it("reports a sign-out the identity provider did not confirm", () => {
    expect(signOutReport({ remote_revocation: "confirmed" })).toBe("complete");
    expect(signOutReport({ remote_revocation: "not_requested" })).toBe(
      "complete",
    );
    expect(signOutReport({ remote_revocation: "unconfirmed" })).toBe(
      "unconfirmed",
    );
    expect(
      signOutReport({
        sign_out: { credentials_removed: true, remote_revocation: "confirmed" },
      }),
    ).toBe("complete");
    expect(
      signOutReport({
        sign_out: {
          credentials_removed: true,
          remote_revocation: "unconfirmed",
        },
      }),
    ).toBe("unconfirmed");
    expect(
      signOutReport({
        sign_out: {
          credentials_removed: false,
          remote_revocation: "unconfirmed",
        },
      }),
    ).toBe("incomplete");
    expect(signOutReport({})).toBe("none");
    expect(signOutReport(null)).toBe("none");
  });
});

describe("login watcher", () => {
  afterEach(() => {
    vi.useRealTimers();
  });
  const pendingLogin = (extra: Partial<LoginStatus> = {}): LoginStatus => ({
    id: "login-1",
    state: "awaiting_user",
    flow: "browser",
    authorization_uri: "https://login.example.com/authorize",
    ...extra,
  });
  function setup(answers: (LoginStatus | Error)[]) {
    vi.useFakeTimers();
    vi.setSystemTime(Date.UTC(2026, 9, 1, 12, 0, 0));
    const polls: number[] = [];
    const cancels: string[] = [];
    const client = {
      login: async (id: string) => {
        polls.push(Date.now());
        expect(id).toBe("login-1");
        const answer = answers.shift() ?? pendingLogin();
        if (answer instanceof Error) throw answer;
        return answer;
      },
      cancelLogin: async (id: string) => {
        cancels.push(id);
        return { id, state: "cancelled" };
      },
    };
    const watcher = new LoginWatcher(client);
    const events: LoginEvent[] = [];
    watcher.subscribe((event) => events.push(event));
    return { watcher, events, polls, cancels };
  }

  it("polls with backoff until the sign-in finishes", async () => {
    const { watcher, events, polls } = setup([
      pendingLogin(),
      pendingLogin(),
      { id: "login-1", state: "authenticated" },
    ]);
    const start = Date.now();
    watcher.watch(pendingLogin());
    expect(watcher.pending).toBe(true);
    expect(events.map((e) => e.kind)).toEqual(["waiting"]);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(polls.map((t) => t - start)).toEqual([1500, 3750, 7125]);
    expect(events.map((e) => e.kind)).toEqual([
      "waiting",
      "waiting",
      "waiting",
      "authenticated",
    ]);
    expect(watcher.pending).toBe(false);
  });

  it("keeps the backoff and expiry when the same sign-in is watched again", async () => {
    const { watcher, polls } = setup([]);
    const start = Date.now();
    watcher.watch(pendingLogin({ expires_at: start / 1000 + 300 }));
    await vi.advanceTimersByTimeAsync(4000);
    const before = watcher.deadline;
    watcher.watch(pendingLogin({ expires_at: start / 1000 + 300 }));
    expect(watcher.deadline).toBe(before);
    await vi.advanceTimersByTimeAsync(3500);
    // 1500, 3750 and then 7125: the resumed watch did not restart at 1.5 s.
    expect(polls.map((t) => t - start)).toEqual([1500, 3750, 7125]);
    watcher.stop();
  });

  it("stops at the host's expiry and cancels the host flow", async () => {
    const { watcher, events, cancels } = setup([]);
    watcher.watch(pendingLogin({ expires_at: Date.now() / 1000 + 4 }));
    expect(watcher.remaining).toBe("Less than a minute left");
    await vi.advanceTimersByTimeAsync(20_000);
    const last = events.at(-1)!;
    expect(last).toEqual({ kind: "ended", state: "timeout", code: "" });
    expect(cancels).toEqual(["login-1"]);
    expect(watcher.pending).toBe(false);
  });

  it("reports declined, lost and transient failures", async () => {
    const failed = setup([
      new ApiError(503, { code: "WV-STUDIO-BUSY" }),
      { id: "login-1", state: "failed", error_code: "WV-AUTH-DENIED" },
    ]);
    failed.watcher.watch(pendingLogin());
    await vi.advanceTimersByTimeAsync(10_000);
    expect(failed.events.at(-1)).toEqual({
      kind: "ended",
      state: "failed",
      code: "WV-AUTH-DENIED",
    });
    vi.useRealTimers();
    const lost = setup([
      new ApiError(404, { code: "WV-STUDIO-LOGIN", status: 404 }),
    ]);
    lost.watcher.watch(pendingLogin());
    await vi.advanceTimersByTimeAsync(2000);
    expect(lost.events.at(-1)).toEqual({
      kind: "ended",
      state: "lost",
      code: "WV-STUDIO-LOGIN",
    });
  });

  it("stop ends polling without reporting an outcome", async () => {
    const { watcher, events, polls, cancels } = setup([]);
    watcher.watch(pendingLogin());
    watcher.stop();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(polls).toEqual([]);
    expect(cancels).toEqual([]);
    expect(watcher.pending).toBe(false);
    expect(events.map((e) => e.kind)).toEqual(["waiting"]);
  });
});
