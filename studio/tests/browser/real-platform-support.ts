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
// The real Studio host from this source tree against a REAL running local
// platform: setup, sign-in, the system credential store audit and the pages
// the real-platform suites share. See real-platform.spec.ts for the variables
// these suites read and how they keep the credential store clean.
import { test, expect, Page, TestInfo } from "@playwright/test";
import { ChildProcess, spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import {
  credentialAccounts as storedAccounts,
  credentialExists as stored,
  deleteCredential,
} from "../credential-store";
import { selectChoice } from "./support";
import { python, pythonAvailable, repository } from "../python-path";

export const platformDir = process.env["WEAVE_E2E_PLATFORM_DIR"]?.trim() ?? "";
export const personFile = process.env["WEAVE_E2E_PERSON_FILE"]?.trim() ?? "";
export const acme = process.env["WEAVE_E2E_ACME_ORIGIN"]?.trim() ?? "";
export const acmeControl = process.env["WEAVE_E2E_ACME_CONTROL"]?.trim() ?? "";

let shots = resolve("test-results/real-platform");
const service = "firefly-weave";

export interface Person {
  username: string;
  password: string;
  subject: string;
  grants: { role: string; scope: Record<string, string> }[];
}
export interface Setup {
  api: string;
  keycloak: string;
  keycloakHost: string;
  adminSecret: string;
  person: Person;
  environment: {
    tenant_id: string;
    project_id: string;
    environment_id: string;
  };
}

let cached: Setup | null = null;
/** Reads the platform and person files once, only when the suite runs. */
export function setup(): Setup {
  if (cached) return cached;
  const state = JSON.parse(
    readFileSync(join(platformDir, "platform.json"), "utf8"),
  ) as { mode?: "host" | "docker"; ports: Record<string, number> };
  const identity = readFileSync(join(platformDir, "identity.env"), "utf8");
  const secret = identity
    .split("\n")
    .map((line) => line.replace(/^export\s+/, "").trim())
    .find((line) => line.startsWith("WEAVE_KC_ADMIN_SECRET="))
    ?.slice("WEAVE_KC_ADMIN_SECRET=".length)
    .replace(/^['"]|['"]$/g, "");
  if (!secret) throw Error("identity.env has no WEAVE_KC_ADMIN_SECRET");
  const person = JSON.parse(readFileSync(personFile, "utf8")) as Person;
  const environment = person.grants.find((g) => g.scope["environment_id"])
    ?.scope as Setup["environment"] | undefined;
  if (!environment) throw Error("The person has no environment grant");
  cached = {
    api: `http://127.0.0.1:${state.ports[state.mode === "docker" ? "container_api" : "api"]}`,
    keycloak: `http://localhost:${state.ports["keycloak"]}`,
    keycloakHost: `localhost:${state.ports["keycloak"]}`,
    adminSecret: secret,
    person,
    environment,
  };
  return cached;
}

// --- screenshots ---------------------------------------------------------------

/** Saves a step screenshot to test-results/real-platform/ (and WEAVE_E2E_SHOTS). */
export async function shot(page: Page, name: string) {
  mkdirSync(shots, { recursive: true });
  const file = join(shots, `${name}.png`);
  await page.screenshot({ path: file });
  const copies = process.env["WEAVE_E2E_SHOTS"]?.trim();
  if (copies) {
    mkdirSync(copies, { recursive: true });
    copyFileSync(file, join(copies, `${name}.png`));
  }
}

// --- the real Studio host --------------------------------------------------------

export function freePort(): Promise<number> {
  return new Promise((done, fail) => {
    const server = createServer();
    server.once("error", fail);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => done(port));
    });
  });
}

export interface Host {
  child: ChildProcess;
  origin: string;
  output: () => string;
}

/** The parent environment without Weave settings that could point elsewhere. */
export function hostEnvironment(configHome: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(process.env))
    if (!key.startsWith("WEAVE_") && !key.startsWith("PYFLY_"))
      env[key] = value;
  return {
    ...env,
    PYTHONPATH: join(repository, "src"),
    WEAVE_CONFIG_HOME: configHome,
  };
}

export async function startHost(configHome: string): Promise<Host> {
  const port = await freePort();
  let output = "";
  const child = spawn(
    python,
    [
      "-m",
      "firefly_weave.cli.main",
      "studio",
      "--assets",
      "studio/dist/studio/browser",
      "--port",
      String(port),
      "--no-browser",
    ],
    {
      cwd: repository,
      env: hostEnvironment(configHome),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout?.on("data", (data) => (output += String(data)));
  child.stderr?.on("data", (data) => (output += String(data)));
  return { child, origin: `http://127.0.0.1:${port}`, output: () => output };
}

export async function stopHost(host: Host | null) {
  if (!host) return;
  if (host.child.exitCode !== null || host.child.signalCode !== null) return;
  const exited = new Promise((done) => host.child.once("exit", done));
  host.child.kill("SIGTERM");
  const late = setTimeout(() => host.child.kill("SIGKILL"), 15_000);
  await exited;
  clearTimeout(late);
}

/** Waits for the host and pairs `page` with the code it printed. */
export async function pair(page: Page, host: Host, name: string) {
  await expect
    .poll(() => host.output().match(/Pairing code: (\S+)/)?.[1], {
      timeout: 30_000,
    })
    .toBeTruthy();
  const code = host.output().match(/Pairing code: (\S+)/)![1];
  await expect
    .poll(
      async () => {
        try {
          return (
            await page.request.get(`${host.origin}/studio/session`)
          ).status();
        } catch {
          return 0;
        }
      },
      { timeout: 30_000 },
    )
    .toBe(200);
  await page.goto(host.origin);
  await shot(page, name);
  await page.getByLabel("Pairing code").fill(code);
  await page.getByRole("button", { name: "Pair browser" }).click();
  await expect(page.locator(".shell")).toBeVisible({ timeout: 30_000 });
}

// --- the system credential store -------------------------------------------------

/** Account bindings of this service in the system credential store (attributes only). */
export function credentialAccounts(): Set<string> {
  return storedAccounts(service);
}

/** True when the system credential store still holds this binding. */
export function credentialExists(account: string) {
  return stored(service, account);
}

/**
 * Bindings already stored when the suite started. Unknown until `beforeAll`
 * has read the store: without it nothing can tell this run's bindings from the
 * person's own, so nothing is deleted.
 */
let baseline: Set<string> | undefined;
/** Bindings this run created that are still stored. */
export function leftovers() {
  const before = baseline;
  if (!before) return [];
  return [...credentialAccounts()].filter((a) => !before.has(a));
}

/**
 * What the running test must undo (hosts, configuration homes, Keycloak
 * accounts). `afterEach` runs them even when the test timed out, which never
 * reaches a `finally` block.
 */
export const cleanups: ((info: TestInfo) => Promise<unknown> | unknown)[] = [];

// --- Keycloak admin --------------------------------------------------------------

export async function admin() {
  const { keycloak, adminSecret } = setup();
  const reply = await fetch(
    `${keycloak}/realms/master/protocol/openid-connect/token`,
    {
      method: "POST",
      body: new URLSearchParams({
        grant_type: "client_credentials",
        client_id: "weave-bootstrap",
        client_secret: adminSecret,
      }),
    },
  );
  expect(reply.status, "Keycloak admin token").toBe(200);
  const token = ((await reply.json()) as { access_token: string }).access_token;
  return async (path: string, init: RequestInit = {}) => {
    const response = await fetch(`${keycloak}/admin/realms/weave${path}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    });
    if (!response.ok)
      throw Error(`Keycloak admin ${path}: HTTP ${response.status}`);
    return response;
  };
}

/**
 * The realm's access token lifetime before this run shortened it. A test that
 * times out never reaches its `finally`, so `afterEach` restores it too.
 */
let lifespanToRestore: number | null = null;

export async function restoreAccessTokenLifespan() {
  if (lifespanToRestore === null) return;
  const kc = await admin();
  const realm = (await (await kc("")).json()) as Record<string, unknown>;
  await kc("", {
    method: "PUT",
    body: JSON.stringify({ ...realm, accessTokenLifespan: lifespanToRestore }),
  });
  lifespanToRestore = null;
}

/** Runs `body` with the realm's access token lifetime set, then restores it. */
export async function withAccessTokenLifespan(
  seconds: number,
  body: () => Promise<void>,
) {
  const kc = await admin();
  const realm = (await (await kc("")).json()) as Record<string, unknown>;
  const original = Number(realm["accessTokenLifespan"] ?? 300);
  // A value this short is a leftover of an interrupted run, not the setting.
  expect(original, "the realm's access token lifetime").toBeGreaterThan(
    seconds,
  );
  lifespanToRestore = original;
  await kc("", {
    method: "PUT",
    body: JSON.stringify({ ...realm, accessTokenLifespan: seconds }),
  });
  try {
    await body();
  } finally {
    await restoreAccessTokenLifespan();
  }
}

/** Ends every Keycloak session of the person (an administrator sign-out). */
export async function endSessions(subject: string) {
  const kc = await admin();
  await kc(`/users/${subject}/logout`, { method: "POST" });
}

/** A Keycloak account the platform has never linked to a Weave person. */
export async function unlinkedPerson() {
  const kc = await admin();
  const username = `bob-${randomBytes(3).toString("hex")}`;
  const password = randomBytes(18).toString("base64url");
  await kc("/users", {
    method: "POST",
    body: JSON.stringify({
      username,
      enabled: true,
      emailVerified: true,
      email: `${username}@example.invalid`,
      firstName: "Bob",
      lastName: "Tester",
      requiredActions: [],
      credentials: [{ type: "password", value: password, temporary: false }],
    }),
  });
  const found = (await (
    await kc(`/users?username=${username}&exact=true`)
  ).json()) as { id: string }[];
  const subject = found[0].id;
  cleanups.push(async () => {
    const remove = await admin();
    await remove(`/users/${subject}`, { method: "DELETE" });
  });
  return { username, password, subject };
}

// --- the operator's command line --------------------------------------------------

/** `weave platform --directory DIR ...` from this source tree. */
export function platformCommand(...args: string[]) {
  const home = mkdtempSync(join(tmpdir(), "weave-studio-e2e-operator-"));
  const env = hostEnvironment(home);
  const result = spawnSync(
    python,
    [
      "-m",
      "firefly_weave.cli.main",
      "platform",
      "--directory",
      platformDir,
      ...args,
    ],
    { cwd: repository, env, encoding: "utf8", timeout: 300_000 },
  );
  rmSync(home, { recursive: true, force: true });
  return {
    status: result.status,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
  };
}

// --- Studio pages -----------------------------------------------------------------

export const heading = (page: Page) => page.locator("#wizard-heading");
export const indicator = (page: Page) => page.locator(".platform-indicator");
export const banner = (page: Page) => page.locator(".platform-banner");

/** First-run choice (when asked), server address, review and trust, save. */
export async function addPlatform(
  page: Page,
  name: string,
  options: { firstRun: boolean; shots?: string } = { firstRun: true },
) {
  const { api, keycloakHost } = setup();
  const prefix = options.shots;
  if (options.firstRun) {
    await expect(heading(page)).toHaveText("How do you want to work?");
    if (prefix) await shot(page, `${prefix}-choice`);
    await page.getByRole("button", { name: "Connect to a platform" }).click();
  }
  await expect(heading(page)).toHaveText("Connect to a platform");
  await page.getByLabel("Server address").fill(api);
  if (prefix) await shot(page, `${prefix}-server`);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(heading(page)).toHaveText("Review and trust", {
    timeout: 30_000,
  });
  const review = page.locator(".review-list").first();
  await expect(review).toContainText(api);
  await expect(review).toContainText("Local Keycloak (development)");
  await expect(review).toContainText(keycloakHost);
  await page.getByLabel("Name", { exact: true }).fill(name);
  await page
    .getByRole("checkbox", {
      name: "I trust this server and identity provider",
    })
    .check();
  if (prefix) await shot(page, `${prefix}-review`);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(heading(page)).toHaveText(`Sign in to ${name}`, {
    timeout: 30_000,
  });
}

/**
 * Follows the sign-in page in a new tab like a person would: the real
 * Keycloak form (when it asks), then the host's loopback callback page.
 */
export async function completeSignIn(
  page: Page,
  account: { username: string; password: string },
  options: { form?: boolean; shots?: string } = {},
) {
  const link = page.getByRole("link", { name: "Open sign-in page" });
  await expect(link).toBeVisible({ timeout: 30_000 });
  await expect(link).toHaveAttribute("target", "_blank");
  const href = (await link.getAttribute("href")) ?? "";
  const opened = page.context().waitForEvent("page");
  await link.click();
  const popup = await opened;
  try {
    await popup.waitForLoadState();
    const callback = /^http:\/\/127\.0\.0\.1:\d+\/callback\?/;
    if (options.form !== false) {
      const username = popup.locator("#username");
      // With an open session, prompt=login asks the signed-in person for
      // their password again; "Restart login" lets another person sign in.
      const restart = popup.locator("#reset-login");
      await expect(username.or(restart)).toBeVisible({ timeout: 30_000 });
      if (options.shots) await shot(popup, `${options.shots}-idp-form`);
      if (!(await username.isVisible())) {
        await restart.click();
        await expect(username).toBeVisible();
        if (options.shots) await shot(popup, `${options.shots}-idp-restart`);
      }
      await username.fill(account.username);
      await popup.locator("#password").fill(account.password);
      await popup.locator("#kc-login").click();
    }
    await popup.waitForURL(callback, { timeout: 30_000 });
    await expect(popup.locator("body")).toHaveText(
      "Login complete. Close this window.",
    );
    if (options.shots) await shot(popup, `${options.shots}-callback`);
  } finally {
    await popup.close();
  }
  return href;
}

/** Workspace step: the person's environment preselected, then Start working. */
export async function startWorking(page: Page, prefix?: string) {
  const { environment } = setup();
  await expect(heading(page)).toHaveText("Choose a workspace", {
    timeout: 60_000,
  });
  const radio = page.locator(
    `weave-workspace-picker input[type="radio"][value*="${environment.environment_id}"]`,
  );
  await expect(radio).toHaveCount(1);
  if (!(await radio.isChecked())) await radio.check();
  if (prefix) await shot(page, `${prefix}-workspace`);
  await page.getByRole("button", { name: "Start working" }).click();
  await expect(page.locator("weave-connection-wizard")).toHaveCount(0, {
    timeout: 30_000,
  });
}

/** Adds the platform and signs in as `person` with every default. */
export async function connectAndSignIn(
  page: Page,
  name: string,
  person: Person,
) {
  await addPlatform(page, name, { firstRun: true });
  await page.getByRole("button", { name: "Sign in with your browser" }).click();
  await completeSignIn(page, person);
  await startWorking(page);
  await expect(indicator(page)).toContainText("Signed in", {
    timeout: 30_000,
  });
}

export async function openSidebar(page: Page, label: string) {
  await page
    .locator(".sidebar nav")
    .getByRole("button", { name: label, exact: true })
    .click();
}

/** Settings → Platforms → ⋯ → Remove: signs out and deletes the credential. */
export async function removePlatform(
  page: Page,
  name: string,
  prefix?: string,
) {
  await openSidebar(page, "Settings");
  const more = page.getByRole("button", { name: `More actions for ${name}` });
  await expect(more).toBeVisible();
  await more.click();
  await page.getByRole("menuitem", { name: `Remove ${name}` }).click();
  const dialog = page.getByRole("dialog", { name: `Remove ${name}?` });
  await expect(dialog).toBeVisible();
  if (prefix) await shot(page, `${prefix}-remove-confirm`);
  await dialog.getByRole("button", { name: "Remove platform" }).click();
  await expect(
    page.getByRole("button", { name: `More actions for ${name}` }),
  ).toHaveCount(0, { timeout: 30_000 });
  await expect(indicator(page)).toContainText("Local authoring");
  if (prefix) await shot(page, `${prefix}-removed`);
}

/** Records the bindings stored by this test so the suite can verify cleanup. */
export function expectNoCredentialsLeft() {
  const left = leftovers();
  expect(left, "credentials this run left in the system store").toEqual([]);
  for (const account of left) expect(credentialExists(account)).toBe(false);
}

export interface Session {
  host: Host | null;
  configHome: string;
  errors: string[];
}

/** A fresh configuration home and a running host for one test. */
export async function session(page: Page): Promise<Session> {
  const configHome = realpathSync(
    mkdtempSync(join(tmpdir(), "weave-studio-real-platform-")),
  );
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && m.text().includes("Content Security Policy"))
      errors.push(m.text());
  });
  const s: Session = { host: await startHost(configHome), configHome, errors };
  cleanups.push(async (info: TestInfo) => {
    if (info.status !== info.expectedStatus && s.host)
      console.error(s.host.output().slice(-6000));
    await stopHost(s.host);
    rmSync(configHome, { recursive: true, force: true });
  });
  return s;
}

export const uniqueName = (prefix: string) =>
  `${prefix}-${randomBytes(3).toString("hex")}`;

/** The run the platform's first-run demo left in the workspace. */
export function demoRun() {
  return (
    JSON.parse(readFileSync(join(platformDir, "first-run.json"), "utf8")) as {
      run_id: string;
    }
  ).run_id;
}

/** The platform's next answer to Operate › Runs' list (run_summaries.list). */
export function runSummaries(page: Page) {
  return page.waitForResponse(
    (r) =>
      r.request().method() === "GET" &&
      new URL(r.url()).pathname.endsWith("/run-summaries"),
    { timeout: 30_000 },
  );
}

/**
 * Shows the platform's oldest runs of the last 30 days, the first-run demo run
 * first among them, and checks that the platform's own answer lists it and
 * that its row shows its short ID in the Run column.
 */
export async function showDemoRun(page: Page) {
  const oldest = page.waitForResponse(
    (r) =>
      r.request().method() === "GET" &&
      new URL(r.url()).pathname.endsWith("/run-summaries") &&
      new URL(r.url()).searchParams.get("order") === "started_asc",
    { timeout: 30_000 },
  );
  await selectChoice(page.getByLabel("Time range", { exact: true }), "30d");
  const more = page.getByRole("button", { name: "More filters" });
  if ((await more.getAttribute("aria-expanded")) !== "true") await more.click();
  await selectChoice(page.getByLabel("Order", { exact: true }), "started_asc");
  const response = await oldest;
  expect(response.status(), "the platform's run summaries").toBe(200);
  const listing = (await response.json()) as { items: { id: string }[] };
  expect(listing.items.map((item) => item.id)).toContain(demoRun());
  await expect(
    page.locator(`.runs-table .cell-run code[title="${demoRun()}"]`),
  ).toHaveText(demoRun().slice(0, 8), { timeout: 30_000 });
}

/**
 * Runs lists real data through the signed-in host, and the demo run among it.
 * The platform answers this visit's own requests, so earlier rows can't
 * satisfy the check.
 */
export async function expectRunsLoad(page: Page) {
  await openSidebar(page, "Home");
  await expect(page.locator("weave-home-dashboard")).not.toContainText(
    "Loading your",
    { timeout: 30_000 },
  );
  const listed = runSummaries(page);
  await openSidebar(page, "Runs");
  const response = await listed;
  expect(response.status(), "the platform's run summaries").toBe(200);
  await showDemoRun(page);
}

/**
 * Starts a run of the first-run demo through the host's API bridge, as any
 * other client of the platform would; Studio's own commands play no part.
 */
export async function startDemoRun(page: Page): Promise<string> {
  const { environment } = setup();
  const receipt = JSON.parse(
    readFileSync(join(platformDir, "first-run.json"), "utf8"),
  ) as { activation_id: string };
  const path = `/studio/api/api/v1/tenants/${environment.tenant_id}/projects/${environment.project_id}/environments/${environment.environment_id}/runs`;
  const answer = await page.evaluate(
    async ({ path, activation }) => {
      const session = (await (
        await fetch("/studio/session", { credentials: "same-origin" })
      ).json()) as { csrfToken?: string };
      const response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Weave-CSRF": session.csrfToken ?? "",
          "Idempotency-Key": crypto.randomUUID(),
        },
        body: JSON.stringify({
          activation_id: activation,
          input: { message: "Started after Runs opened" },
        }),
      });
      return {
        status: response.status,
        body: (await response.json()) as Record<string, unknown>,
      };
    },
    { path, activation: receipt.activation_id },
  );
  expect(answer.status, JSON.stringify(answer.body)).toBe(201);
  return String(answer.body["id"]);
}

// --- the suite ----------------------------------------------------------------------

/** Bindings already stored when the running test started, once known. */
let storedBefore: Set<string> | undefined;

/**
 * Registers a real-platform suite in the calling spec: it runs only with
 * WEAVE_E2E_PLATFORM_DIR and WEAVE_E2E_PERSON_FILE, keeps its screenshots in
 * test-results/`folder`, and audits the system credential store around every
 * test.
 */
export function useRealPlatformSuite(folder = "real-platform") {
  test.skip(
    !platformDir || !personFile,
    "Set WEAVE_E2E_PLATFORM_DIR and WEAVE_E2E_PERSON_FILE to run against a real local platform.",
  );

  test.beforeAll(() => {
    shots = resolve("test-results", folder);
    rmSync(shots, { recursive: true, force: true });
    mkdirSync(shots, { recursive: true });
    expect(pythonAvailable(), "the repository's Python environment").toBe(true);
    expect(
      existsSync(join(repository, "studio/dist/studio/browser/index.html")),
      "run `npm run build` first",
    ).toBe(true);
    baseline = credentialAccounts();
  });

  test.beforeEach(() => {
    storedBefore = undefined;
    storedBefore = credentialAccounts();
  });

  test.afterEach(async ({}, info) => {
    await restoreAccessTokenLifespan();
    while (cleanups.length)
      try {
        await cleanups.pop()!(info);
      } catch (error) {
        console.error(error);
      }
    // A test that failed before removing its platform never signed out: delete
    // the bindings it stored, so the suite's audit reports only what a passing
    // test left behind instead of repeating this failure.
    const before = storedBefore;
    if (before && info.status !== info.expectedStatus)
      for (const account of credentialAccounts())
        if (!before.has(account)) deleteCredential(service, account);
  });

  test.afterAll(() => {
    // Delete only bindings this run created, then fail if any had to be.
    const left = leftovers();
    for (const account of left) deleteCredential(service, account);
    expect(left, "credentials this run left in the system store").toEqual([]);
  });

  // A step that can't act fails with its own message instead of the test timeout.
  test.use({
    viewport: { width: 1440, height: 900 },
    actionTimeout: 30_000,
    navigationTimeout: 30_000,
  });
}
