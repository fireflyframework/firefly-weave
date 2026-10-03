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
// platform and its real Keycloak: no route mocks, real sign-in pages, real
// tokens in the system credential store. Skipped unless both variables are set:
//
//   WEAVE_E2E_PLATFORM_DIR  The local platform directory created by
//                           `weave platform --directory DIR setup` and started
//                           with `weave platform --directory DIR start`. Its
//                           platform.json gives the API and Keycloak ports, and
//                           identity.env the Keycloak admin client secret (read,
//                           never printed). Built-in HTTP connector integrations
//                           must be enabled (`... integrations enable`).
//   WEAVE_E2E_PERSON_FILE   The JSON printed by `weave platform --directory DIR
//                           user --username NAME --role tenant_admin --role
//                           developer --role deployer --role operator --role
//                           viewer --role task_participant --output json`
//                           (fields username, password, subject, grants). Keep
//                           it private (0600); the password is never printed.
//
// Optional:
//   WEAVE_E2E_SHOTS         A folder that also receives a copy of every step
//                           screenshot (they always go to
//                           test-results/real-platform/).
//
// Build the app first (`npm run build`): the host serves
// studio/dist/studio/browser. Every test starts its own host with a fresh
// WEAVE_CONFIG_HOME, so the developer's saved platforms are never read.
//
// The wizard saves sign-ins in the native credential store (on macOS the login
// Keychain, service "firefly-weave"). Every test removes its platform through
// the UI, which signs out and deletes the credential, and the suite checks that
// no credential created by this run is left behind (leftovers are deleted by
// their account binding, and only those this run created).
//
// Keycloak changes (a 20-second access token lifetime, ended sessions, an
// extra unlinked person) are made with the realm's admin API; the access
// token lifetime is always restored and the extra person deleted.
//
// The quick-integration test calls https://jsonplaceholder.typicode.com from
// the platform's worker, and runs the operator's
// `weave platform --directory DIR integrations grant` from this source tree.
// That command refuses to run once the source checkout differs from the one
// the platform was set up from: set the platform up again after changing src/.
import { test, expect, Page, TestInfo } from "@playwright/test";
import {
  ChildProcess,
  execFileSync,
  spawn,
  spawnSync,
} from "node:child_process";
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
import { platform as os, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { DesignerPage } from "./designer-po";
import { openYaml } from "./integrations-po";
import { command } from "./support";

const platformDir = process.env["WEAVE_E2E_PLATFORM_DIR"]?.trim() ?? "";
const personFile = process.env["WEAVE_E2E_PERSON_FILE"]?.trim() ?? "";
test.skip(
  !platformDir || !personFile,
  "Set WEAVE_E2E_PLATFORM_DIR and WEAVE_E2E_PERSON_FILE to run against a real local platform.",
);

const repository = resolve("..");
const python = resolve(repository, ".venv/bin/python");
const shots = resolve("test-results/real-platform");
const service = "firefly-weave";

interface Person {
  username: string;
  password: string;
  subject: string;
  grants: { role: string; scope: Record<string, string> }[];
}
interface Setup {
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
function setup(): Setup {
  if (cached) return cached;
  const state = JSON.parse(
    readFileSync(join(platformDir, "platform.json"), "utf8"),
  ) as { ports: Record<string, number> };
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
    api: `http://127.0.0.1:${state.ports["api"]}`,
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
async function shot(page: Page, name: string) {
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

function freePort(): Promise<number> {
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

interface Host {
  child: ChildProcess;
  origin: string;
  output: () => string;
}

/** The parent environment without Weave settings that could point elsewhere. */
function hostEnvironment(configHome: string): NodeJS.ProcessEnv {
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

async function startHost(configHome: string): Promise<Host> {
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

async function stopHost(host: Host | null) {
  if (!host) return;
  if (host.child.exitCode !== null || host.child.signalCode !== null) return;
  const exited = new Promise((done) => host.child.once("exit", done));
  host.child.kill("SIGTERM");
  const late = setTimeout(() => host.child.kill("SIGKILL"), 15_000);
  await exited;
  clearTimeout(late);
}

/** Waits for the host and pairs `page` with the code it printed. */
async function pair(page: Page, host: Host, name: string) {
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

/** Account bindings of this service's generic passwords (attributes only). */
function credentialAccounts(): Set<string> {
  const accounts = new Set<string>();
  if (os() !== "darwin") return accounts;
  const dump = execFileSync("security", ["dump-keychain"], {
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
    stdio: ["ignore", "pipe", "ignore"],
  });
  for (const block of dump.split(/^keychain: /m)) {
    if (!block.includes(`"svce"<blob>="${service}"`)) continue;
    const account = block.match(/"acct"<blob>="([^"]*)"/)?.[1];
    if (account) accounts.add(account);
  }
  return accounts;
}

/** True when `security find-generic-password` still finds this binding. */
function credentialExists(account: string) {
  if (os() !== "darwin") return false;
  return (
    spawnSync(
      "security",
      ["find-generic-password", "-s", service, "-a", account],
      { stdio: "ignore" },
    ).status === 0
  );
}

let baseline = new Set<string>();
/** Bindings this run created that are still stored. */
function leftovers() {
  return [...credentialAccounts()].filter((a) => !baseline.has(a));
}

/**
 * What the running test must undo (hosts, configuration homes, Keycloak
 * accounts). `afterEach` runs them even when the test timed out, which never
 * reaches a `finally` block.
 */
const cleanups: ((info: TestInfo) => Promise<unknown> | unknown)[] = [];

// --- Keycloak admin --------------------------------------------------------------

async function admin() {
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

async function restoreAccessTokenLifespan() {
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
async function withAccessTokenLifespan(
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
async function endSessions(subject: string) {
  const kc = await admin();
  await kc(`/users/${subject}/logout`, { method: "POST" });
}

/** A Keycloak account the platform has never linked to a Weave person. */
async function unlinkedPerson() {
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
function platformCommand(...args: string[]) {
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

const heading = (page: Page) => page.locator("#wizard-heading");
const indicator = (page: Page) => page.locator(".platform-indicator");
const banner = (page: Page) => page.locator(".platform-banner");

/** First-run choice (when asked), server address, review and trust, save. */
async function addPlatform(
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
async function completeSignIn(
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
async function startWorking(page: Page, prefix?: string) {
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
async function connectAndSignIn(page: Page, name: string, person: Person) {
  await addPlatform(page, name, { firstRun: true });
  await page.getByRole("button", { name: "Sign in with your browser" }).click();
  await completeSignIn(page, person);
  await startWorking(page);
  await expect(indicator(page)).toContainText("Signed in", {
    timeout: 30_000,
  });
}

async function openSidebar(page: Page, label: string) {
  await page
    .locator(".sidebar nav")
    .getByRole("button", { name: label, exact: true })
    .click();
}

/** Settings → Platforms → ⋯ → Remove: signs out and deletes the credential. */
async function removePlatform(page: Page, name: string, prefix?: string) {
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
function expectNoCredentialsLeft() {
  const left = leftovers();
  expect(left, "credentials this run left in the system store").toEqual([]);
  for (const account of left) expect(credentialExists(account)).toBe(false);
}

interface Session {
  host: Host | null;
  configHome: string;
  errors: string[];
}

/** A fresh configuration home and a running host for one test. */
async function session(page: Page): Promise<Session> {
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

const uniqueName = (prefix: string) =>
  `${prefix}-${randomBytes(3).toString("hex")}`;

/** The run the platform's first-run demo left in the workspace. */
function demoRun() {
  return (
    JSON.parse(readFileSync(join(platformDir, "first-run.json"), "utf8")) as {
      run_id: string;
    }
  ).run_id;
}

/**
 * Runs lists the demo run: real data through the signed-in host. The platform
 * answers this visit's own request, so earlier rows can't satisfy the check.
 */
async function expectRunsLoad(page: Page) {
  await openSidebar(page, "Home");
  await expect(page.locator("weave-home-dashboard")).not.toContainText(
    "Loading your",
    { timeout: 30_000 },
  );
  const listed = page.waitForResponse(
    (r) =>
      r.request().method() === "GET" &&
      new URL(r.url()).pathname.endsWith("/runs"),
    { timeout: 30_000 },
  );
  await openSidebar(page, "Runs");
  const response = await listed;
  expect(response.status(), "the platform's run list").toBe(200);
  const listing = (await response.json()) as { items: { id: string }[] };
  expect(listing.items.map((item) => item.id)).toContain(demoRun());
  // Rows read as workflows and keys; the ID shows in the run's detail.
  await expect(page.locator(".resource-row").first()).toBeVisible({
    timeout: 30_000,
  });
}

// --- the suite ----------------------------------------------------------------------

test.beforeAll(() => {
  rmSync(shots, { recursive: true, force: true });
  mkdirSync(shots, { recursive: true });
  expect(existsSync(python), "the repository's .venv").toBe(true);
  expect(
    existsSync(join(repository, "studio/dist/studio/browser/index.html")),
    "run `npm run build` first",
  ).toBe(true);
  baseline = credentialAccounts();
});

test.afterEach(async ({}, info) => {
  await restoreAccessTokenLifespan();
  while (cleanups.length)
    try {
      await cleanups.pop()!(info);
    } catch (error) {
      console.error(error);
    }
});

test.afterAll(() => {
  // Delete only bindings this run created, then fail if any had to be.
  const left = leftovers();
  for (const account of left)
    spawnSync(
      "security",
      ["delete-generic-password", "-s", service, "-a", account],
      { stdio: "ignore" },
    );
  expect(left, "credentials this run left in the system store").toEqual([]);
});

// A step that can't act fails with its own message instead of the test timeout.
test.use({
  viewport: { width: 1440, height: 900 },
  actionTimeout: 30_000,
  navigationTimeout: 30_000,
});

test("1-4: first run, browser sign-in, real runs, silent recovery after a restart", async ({
  page,
}) => {
  test.setTimeout(300_000);
  const { person, environment } = setup();
  const s = await session(page);
  const name = uniqueName("e2e");

  // 1. First run: pairing, the choice, the real server, review and trust.
  await pair(page, s.host!, "01-pairing");
  await addPlatform(page, name, { firstRun: true, shots: "01" });
  await shot(page, "01-saved-sign-in-step");

  // 2. Browser sign-in through the real Keycloak and the loopback callback.
  await page.getByRole("button", { name: "Sign in with your browser" }).click();
  await expect(
    page.getByText("Waiting for you to finish signing in…"),
  ).toBeVisible();
  await shot(page, "02-waiting");
  const tested = page.waitForResponse(
    (r) => r.url().endsWith("/studio/connection/test") && r.status() === 200,
    { timeout: 120_000 },
  );
  const href = await completeSignIn(page, person, { shots: "02" });
  expect(new URL(href).searchParams.get("code_challenge_method")).toBe("S256");
  const { identity } = (await (await tested).json()) as {
    identity: {
      workspaces: {
        id: string;
        name: string;
        projects: {
          id: string;
          name: string;
          environments: { id: string; name: string }[];
        }[];
      }[];
    };
  };
  const tenant = identity.workspaces.find(
    (w) => w.id === environment.tenant_id,
  )!;
  const project = tenant.projects.find((p) => p.id === environment.project_id)!;
  const stage = project.environments.find(
    (e) => e.id === environment.environment_id,
  )!;
  await expect(heading(page)).toHaveText("Choose a workspace", {
    timeout: 60_000,
  });
  const picker = page.locator("weave-workspace-picker");
  await expect(picker).toContainText(tenant.name);
  await expect(picker).toContainText(project.name);
  await expect(picker).toContainText(stage.name);
  await startWorking(page, "02");
  // The top bar names the platform, the account and the workspace.
  await expect(indicator(page)).toContainText(name);
  await expect(indicator(page)).toContainText(
    `${project.name} / ${stage.name} · Signed in as ${person.username}`,
  );
  await shot(page, "02-topbar");
  expect(leftovers()).toHaveLength(1);

  // 3. Real data: the demo run is listed in Runs, workflows in Workflows.
  await expectRunsLoad(page);
  await expect(page.locator(".error-banner")).toHaveCount(0);
  await shot(page, "03-runs");
  await openSidebar(page, "Workflows");
  await expect(page.locator(".resource-row").first()).toContainText(
    /Published|Active/,
    { timeout: 30_000 },
  );
  await shot(page, "03-workflows");

  // 4. Restart: a new host on the same configuration reconnects silently.
  await stopHost(s.host);
  s.host = await startHost(s.configHome);
  await pair(page, s.host, "04-pairing-again");
  await expect(indicator(page)).toContainText(
    `${project.name} / ${stage.name} · Signed in as ${person.username}`,
    { timeout: 30_000 },
  );
  await expect(page.locator("weave-connection-wizard")).toHaveCount(0);
  await expect(banner(page)).toHaveCount(0);
  await expectRunsLoad(page);
  await shot(page, "04-runs-after-restart");
  expect(s.errors).toEqual([]);

  await removePlatform(page, name, "04");
  expectNoCredentialsLeft();
});

test("5-6: silent refresh after expiry, then an ended session keeps local work", async ({
  page,
}) => {
  test.setTimeout(360_000);
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-expiry");
  await pair(page, s.host!, "05-pairing");
  await withAccessTokenLifespan(20, async () => {
    // 5. Access tokens live 20 seconds: outlive one, then use the platform.
    await connectAndSignIn(page, name, person);
    await expectRunsLoad(page);
    await shot(page, "05-signed-in");
    await page.waitForTimeout(25_000);
    await expectRunsLoad(page);
    await expect(banner(page)).toHaveCount(0);
    await expect(page.locator(".error-banner")).toHaveCount(0);
    await expect(indicator(page)).toContainText(
      `Signed in as ${person.username}`,
    );
    await shot(page, "05-renewed-silently");

    // 6. The identity provider ends the session: renewal fails cleanly.
    await endSessions(person.subject);
    await page.waitForTimeout(25_000);
    await openSidebar(page, "Home");
    await openSidebar(page, "Runs");
    await expect(banner(page)).toContainText(
      `Your session for ${name} expired`,
      { timeout: 30_000 },
    );
    await expect(page.locator(".error-banner")).toHaveCount(0);
    await expect(indicator(page)).toContainText("Session expired");
    await shot(page, "06-session-expired");

    // Local authoring keeps working: draw a step and validate it locally.
    await openSidebar(page, "Home");
    await page
      .getByRole("button", { name: "New workflow", exact: true })
      .click();
    await page
      .locator(".palette-step")
      .filter({ hasText: "Transform" })
      .click();
    await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
    await page.getByRole("button", { name: "Validate", exact: true }).click();
    const diagnostics = page.locator(".diagnostics");
    await expect(diagnostics).toContainText("No problems found", {
      timeout: 30_000,
    });
    await expect(diagnostics).toContainText(
      "Sign in again to check actions and connections against the project.",
    );
    await expect(banner(page)).toContainText(
      `Your session for ${name} expired`,
    );
    await shot(page, "06-local-authoring");

    // Sign in again from the notice; the local draft stays open.
    await banner(page).getByRole("button", { name: "Sign in again" }).click();
    await expect(heading(page)).toHaveText(`Sign in to ${name}`);
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await completeSignIn(page, person, { shots: "06-again" });
    await startWorking(page);
    await expect(indicator(page)).toContainText(
      `Signed in as ${person.username}`,
    );
    await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
    await expect(banner(page)).toHaveCount(0);
    await openSidebar(page, "Runs");
    await page
      .getByRole("dialog", { name: "Leave the designer?" })
      .getByRole("button", { name: "Leave designer" })
      .click();
    // Rows show the short run ID; the full ID is in the run's detail.
    await expect(page.locator(".resource-table")).toContainText(
      `Run ${demoRun().slice(0, 8)}`,
      { timeout: 30_000 },
    );
    await shot(page, "06-signed-in-again");
  });
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});

test("7: switch account to a person the platform does not know", async ({
  page,
  context,
}) => {
  test.setTimeout(240_000);
  const { person } = setup();
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const s = await session(page);
  const name = uniqueName("e2e-switch");
  const bob = await unlinkedPerson();
  await pair(page, s.host!, "07-pairing");
  await connectAndSignIn(page, name, person);
  await openSidebar(page, "Settings");
  await page.getByRole("button", { name: `Switch account on ${name}` }).click();
  await expect(heading(page)).toHaveText(`Sign in to ${name}`);
  await expect(
    page.getByText("Choose a different account on the sign-in page."),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign in with your browser" }).click();
  // Alice still has a Keycloak session, yet the sign-in page asks again.
  const href = await completeSignIn(page, bob, { shots: "07" });
  expect(new URL(href).searchParams.get("prompt")).toBe("login");
  await expect(
    page.getByRole("heading", {
      name: "You signed in, but this platform doesn't recognize your account yet",
    }),
  ).toBeVisible({ timeout: 60_000 });
  const details = page.locator("#wizard-access-details");
  await expect(details).toHaveValue(new RegExp(`Subject: ${bob.subject}`));
  // The top bar no longer claims the previous account.
  await expect(indicator(page)).toContainText("Account not recognized");
  await expect(indicator(page)).not.toContainText("Signed in");
  await page.getByRole("button", { name: "Copy details" }).click();
  await expect
    .poll(() => page.evaluate(() => navigator.clipboard.readText()))
    .toContain(bob.subject);
  await shot(page, "07-not-recognized");
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});

test("8-9: an unreachable server, then a cancelled sign-in and a retry", async ({
  page,
}) => {
  test.setTimeout(240_000);
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-cancel");
  await pair(page, s.host!, "09-pairing");

  // 9. Unreachable server: a plain-language explanation.
  await expect(heading(page)).toHaveText("How do you want to work?");
  await page.getByRole("button", { name: "Connect to a platform" }).click();
  await page.getByLabel("Server address").fill("http://127.0.0.1:1");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  const problem = page.locator("#wizard-server-problem");
  await expect(problem).toBeVisible({ timeout: 45_000 });
  await expect(problem.locator("strong")).toHaveText(
    "Studio couldn't reach 127.0.0.1:1.",
  );
  await expect(problem).toContainText("Check the address and your network");
  await shot(page, "09-unreachable");

  // 8. Cancel a sign-in, see the cancelled state, try again.
  await addPlatform(page, name, { firstRun: false });
  await page.getByRole("button", { name: "Sign in with your browser" }).click();
  await expect(
    page.getByRole("link", { name: "Open sign-in page" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Cancel sign-in" }).click();
  const cancelled = page.locator("#wizard-sign-in-problem");
  await expect(cancelled).toContainText("Sign-in canceled", {
    timeout: 30_000,
  });
  // While the wizard is open the top bar reads "Connecting…", not an error.
  await expect(indicator(page)).toContainText("Connecting…");
  await shot(page, "08-cancelled");
  await page.getByRole("button", { name: "Try again" }).click();
  await completeSignIn(page, person);
  await startWorking(page, "08");
  await expect(indicator(page)).toContainText(
    `Signed in as ${person.username}`,
  );
  await shot(page, "08-retried");
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});

test("10: quick integration from Studio runs against the real platform", async ({
  page,
}) => {
  test.setTimeout(420_000);
  const { person } = setup();
  const s = await session(page);
  const suffix = randomBytes(3).toString("hex");
  const name = `e2e-integration-${suffix}`;
  const action = `get-todo-${suffix}`;
  await pair(page, s.host!, "10-pairing");
  await connectAndSignIn(page, name, person);
  const designer = new DesignerPage(page);

  // Describe GET /todos/{id} in the API action builder, from a new workflow.
  await openSidebar(page, "Home");
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  // At this width the palette is beside the canvas; its catalog loads first.
  const palette = page.locator("weave-palette-integrations");
  await expect(palette).toBeVisible({ timeout: 30_000 });
  await palette.getByRole("button", { name: "New API action" }).click();
  const builder = page.getByRole("dialog", { name: "New API action" });
  await expect(builder.getByLabel("Name", { exact: true })).toBeFocused();
  await page.keyboard.type(action);
  await builder
    .getByLabel("API address")
    .fill("https://jsonplaceholder.typicode.com");
  await builder.getByLabel("Path", { exact: true }).fill("/todos/{id}");
  await builder.getByLabel("Type of id").selectOption("integer");
  await builder
    .getByLabel("Example response")
    .fill(
      '{"userId": 1, "id": 1, "title": "sample title", "completed": false}',
    );
  // Readiness is one line: ready, or how many things to set up.
  await expect(builder.locator(".readiness-line")).toContainText(
    /Ready to publish|to set up|couldn't check/,
  );
  await openYaml(page);
  const preview = page.getByRole("region", { name: /Action YAML for/ });
  await expect(preview).toContainText("sideEffect: read_only");
  await expect(preview).toContainText("path: /todos/{id}");
  // The origin stays with the connection; sample values are never kept.
  await expect(preview).not.toContainText("jsonplaceholder");
  await expect(preview).not.toContainText("sample title");
  await shot(page, "10-describe");

  // Publish the action, then insert it with its connection slot.
  await builder.getByRole("button", { name: "Publish action" }).click();
  await page
    .getByRole("dialog", { name: `Publish ${action} 1.0.0?` })
    .getByRole("button", { name: "Publish action" })
    .click();
  await expect(builder).toContainText(`Published ${action}@1.0.0.`, {
    timeout: 60_000,
  });
  await shot(page, "10-published");
  await builder.getByRole("button", { name: "Insert into workflow" }).click();
  await expect(builder).toHaveCount(0);
  await expect(designer.node("call-action-1")).toBeVisible();

  // The workflow: a unique name and one required input field, "id".
  await designer.deselect();
  await designer.inspectorField("Name").fill(`todo-reader-${suffix}`);
  const inputSchema = designer.inspector.locator(
    '[data-field="spec/inputSchema"]',
  );
  await inputSchema.getByRole("button", { name: "Add field" }).click();
  await inputSchema.getByLabel("Field name").fill("id");
  await inputSchema.getByLabel("Type of “id”").selectOption("integer");
  await inputSchema.getByRole("checkbox", { name: "Required: “id”" }).check();
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();

  // The action reads its path parameter from the workflow input, and its
  // answer becomes the workflow's result.
  await designer.selectStep("call-action-1");
  const form = page.locator(".action-input-form");
  await form
    .locator('[data-path="path"] .schema-field')
    .getByRole("button", { name: "Data" })
    .click();
  await form.getByRole("combobox", { name: /^ID/ }).fill("/input/id");
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await designer.inspector
    .getByRole("button", { name: "Use action output as workflow result" })
    .click();
  await shot(page, "10-mapped");
  const source = await designer.source();
  expect(source).toContain(`uses: ${action}@1.0.0`);
  expect(source).toContain("connection: jsonplaceholder");
  expect(source).toMatch(/id:\s+ref: \/input\/id/);
  expect(source).toMatch(
    /connections:\s+jsonplaceholder:\s+connector: weave-http@2\.0\.0/,
  );

  // The connection for the slot, from the inspector: origin only, no secret.
  await designer.selectStep("call-action-1");
  await designer.inspector
    .getByRole("button", { name: "Create a connection for this API" })
    .click();
  const connect = page.getByRole("dialog", { name: "New API connection" });
  await expect(
    connect.getByLabel("Connection name", { exact: true }),
  ).toHaveValue("jsonplaceholder");
  await expect(connect.getByLabel("API address", { exact: true })).toHaveValue(
    "https://jsonplaceholder.typicode.com",
  );
  const created = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname.endsWith("/connections"),
  );
  await connect.getByRole("button", { name: "Create connection" }).click();
  const revision = String(
    ((await (await created).json()) as Record<string, unknown>)["id"],
  );
  await expect(connect).toContainText("Created jsonplaceholder (revision");
  // The last step names the real revision ID, ready to run on this computer.
  await expect(connect.locator("code.grant")).toHaveText(
    `weave platform integrations grant --connection ${revision} --access read`,
  );
  // Letting this computer's release use the connection is an operator step:
  // Studio's readiness steps name the command, and the operator runs it.
  await connect.getByRole("button", { name: "Show steps" }).click();
  await expect(connect).toContainText(
    "weave platform integrations grant --connection REVISION_ID --access read",
  );
  await shot(page, "10-connection");
  const granted = platformCommand(
    "integrations",
    "grant",
    "--connection",
    revision,
    "--access",
    "read",
    "--output",
    "json",
  );
  expect(granted.status, granted.stdout + granted.stderr).toBe(0);
  await page.keyboard.press("Escape");
  await expect(connect).toHaveCount(0);

  // Publish, then activate: the slot's connection and the release are pinned.
  await command(page, "Publish…");
  const publish = page.getByRole("dialog", {
    name: `Publish todo-reader-${suffix} 1.0.0?`,
  });
  await shot(page, "10-publish");
  await publish.getByRole("button", { name: "Publish version" }).click();
  // Published: Activate… becomes the one primary command.
  const activateButton = page
    .getByRole("toolbar", { name: "Workflow commands" })
    .getByRole("button", { name: "Activate…", exact: true });
  await expect(activateButton).toBeVisible({ timeout: 60_000 });
  await activateButton.click();
  const activate = page.getByRole("dialog");
  await expect(activate).toContainText(/Activate \S+ \d+\.\d+\.\d+/);
  const integrations = JSON.parse(
    readFileSync(join(platformDir, "integrations.json"), "utf8"),
  ) as { release_id: string };
  await expect(activate.getByLabel(/^weave-http@2\.0\.0/)).toHaveValue(
    integrations.release_id,
    { timeout: 30_000 },
  );
  await expect(activate.getByLabel(/^jsonplaceholder/)).toHaveValue(revision);
  await shot(page, "10-activate");
  const submit = activate.getByRole("button", { name: "Activate version" });
  await submit.scrollIntoViewIfNeeded();
  await submit.click();
  await expect(activate).toHaveCount(0, { timeout: 30_000 });

  // Start a run from the form built from the input schema.
  await command(page, "Start run…");
  const run = page.getByRole("dialog", { name: "Start a run" });
  await expect(
    run.getByLabel("Version to run", { exact: true }),
  ).not.toHaveValue("");
  await run.getByRole("spinbutton", { name: /^ID/ }).fill("1");
  await shot(page, "10-start-run");
  const started = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname.endsWith("/runs"),
  );
  await run.getByRole("button", { name: "Start run" }).click();
  const runId = String(
    ((await (await started).json()) as Record<string, unknown>)["id"],
  );
  await expect(run).toHaveCount(0, { timeout: 30_000 });

  // The toast opens the new run (the published version holds the edits, so
  // there is nothing to keep in the designer); it succeeds with the real
  // API's answer.
  await page.locator(".toast").getByRole("button", { name: "View" }).click();
  await expect(
    page.getByRole("dialog", { name: "Leave the designer?" }),
  ).toHaveCount(0);
  const detail = page.locator(".record-detail");
  await expect(detail.locator(".run-id .tag")).toHaveText(runId.slice(0, 8), {
    timeout: 30_000,
  });
  await expect
    .poll(
      async () => {
        const status = await detail.locator(".status-pill").first().innerText();
        if (status !== "Succeeded")
          await detail.getByRole("button", { name: "Refresh" }).click();
        return status;
      },
      { timeout: 120_000, intervals: [2_000] },
    )
    .toBe("Succeeded");
  await detail.getByText("Technical details").click();
  await expect(detail).toContainText("delectus aut autem");
  await shot(page, "10-run-succeeded");
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});

test("11: sign out with revocation, then remove the platform", async ({
  page,
}) => {
  test.setTimeout(240_000);
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-signout");
  await pair(page, s.host!, "11-pairing");
  await connectAndSignIn(page, name, person);
  const stored = leftovers();
  expect(stored).toHaveLength(1);
  expect(credentialExists(stored[0])).toBe(true);
  await indicator(page).click();
  await page
    .locator("#platform-menu")
    .getByRole("button", { name: "Sign out" })
    .click();
  const dialog = page.getByRole("dialog", { name: `Sign out of ${name}?` });
  await expect(dialog).toBeVisible();
  await shot(page, "11-sign-out-confirm");
  const logout = page.waitForResponse((r) =>
    r.url().endsWith("/studio/connection/logout"),
  );
  await dialog.getByRole("button", { name: "Sign out" }).click();
  const result = (await (await logout).json()) as {
    remote_revocation?: string;
  };
  // The identity provider confirmed it ended the session.
  expect(result.remote_revocation).toBe("confirmed");
  await expect(indicator(page)).toContainText("Signed out", {
    timeout: 30_000,
  });
  // The sign-in left this computer's credential store.
  expect(credentialExists(stored[0])).toBe(false);
  await shot(page, "11-signed-out");
  // The platform now refuses calls; Studio asks to sign in and stays
  // "Signed out" (nothing expired).
  await openSidebar(page, "Runs");
  await expect(
    page.getByRole("heading", { name: "Sign in to see runs" }),
  ).toBeVisible({ timeout: 30_000 });
  await expect(indicator(page)).toContainText("Signed out");
  await expect(banner(page)).toHaveCount(0);
  await expect(page.locator(".error-banner")).toHaveCount(0);
  await shot(page, "11-runs-signed-out");
  await removePlatform(page, name, "11");
  expectNoCredentialsLeft();
  expect(s.errors).toEqual([]);
});
