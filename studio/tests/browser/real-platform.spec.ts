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
//   WEAVE_E2E_ACME_ORIGIN   The Acme fixture origin the platform approved with
//                           `weave platform up --allow-private-origin`
//                           (http://acme.acceptance.test:8080). The
//                           quick-integration test needs it.
//   WEAVE_E2E_ACME_CONTROL  The fixture's loopback control URL; when set, the
//                           test also reads the fixture's request journal.
//   WEAVE_E2E_AI_MODEL      The model weave platform ai enable approved on this
//                           platform (qwen3:4b). Test 13 needs it.
//
// Build the app first (`npm run build`): the host serves
// studio/dist/studio/browser. Every test starts its own host with a fresh
// WEAVE_CONFIG_HOME, so the developer's saved platforms are never read.
//
// The wizard saves sign-ins in the native credential store, service
// "firefly-weave": the login Keychain on macOS, the Secret Service on Linux and
// the Credential Manager on Windows (../credential-store.ts). Every test
// removes its platform through the UI, which signs out and deletes the
// credential, and the suite checks that no credential created by this run is
// left behind (leftovers are deleted by their account binding, and only those
// this run created). A test that fails first has the bindings it stored
// deleted after it, so its failure is reported once. The audit fails when the
// store is unavailable.
//
// Keycloak changes (a 20-second access token lifetime, ended sessions, an
// extra unlinked person) are made with the realm's admin API; the access
// token lifetime is always restored and the extra person deleted.
//
// The quick-integration test calls the Acme API fixture from the platform's
// native executor over plain HTTP, which only the platform's private-origin
// policy allows, and runs the operator's
// `weave platform --directory DIR integrations grant` from this source tree.
// That command refuses to run once the source checkout differs from the one
// the platform was set up from: set the platform up again after changing src/.
import { test, expect } from "@playwright/test";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { DesignerPage } from "./designer-po";
import { openYaml } from "./integrations-po";
import { command, selectChoice } from "./support";
import {
  acme,
  acmeControl,
  addPlatform,
  banner,
  completeSignIn,
  connectAndSignIn,
  credentialExists,
  endSessions,
  expectNoCredentialsLeft,
  expectRunsLoad,
  heading,
  indicator,
  leftovers,
  openSidebar,
  pair,
  platformCommand,
  platformDir,
  removePlatform,
  runSummaries,
  session,
  setup,
  shot,
  showDemoRun,
  startDemoRun,
  startHost,
  startWorking,
  stopHost,
  uniqueName,
  unlinkedPerson,
  withAccessTokenLifespan,
  useRealPlatformSuite,
} from "./real-platform-support";

useRealPlatformSuite();

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
    await expect(diagnostics).toContainText("Checked locally", {
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
    const listed = runSummaries(page);
    await openSidebar(page, "Runs");
    await page
      .getByRole("dialog", { name: "Leave the designer?" })
      .getByRole("button", { name: "Leave designer" })
      .click();
    expect((await listed).status(), "the platform's run summaries").toBe(200);
    // The Run column shows the demo run's short ID; the full ID is in its detail.
    await showDemoRun(page);
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
  if (process.env.CI) expect(acme, "WEAVE_E2E_ACME_ORIGIN").not.toBe("");
  test.skip(
    !acme,
    "Set WEAVE_E2E_ACME_ORIGIN to the Acme fixture origin this platform approved; scripts/acceptance.py does.",
  );
  const { person } = setup();
  const s = await session(page);
  const suffix = randomBytes(3).toString("hex");
  const name = `e2e-integration-${suffix}`;
  const action = `get-order-status-${suffix}`;
  await pair(page, s.host!, "10-pairing");
  await connectAndSignIn(page, name, person);
  const designer = new DesignerPage(page);

  // Describe GET /orders/{id}/status of the Acme fixture in the API action
  // builder, from a new workflow.
  await openSidebar(page, "Home");
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  // At this width the palette is beside the canvas; its catalog loads first.
  const palette = page.locator("weave-palette-integrations");
  await expect(palette).toBeVisible({ timeout: 30_000 });
  await palette.getByRole("button", { name: "New API action" }).click();
  const builder = page.getByRole("dialog", { name: "New API action" });
  await expect(builder.getByLabel("Name", { exact: true })).toBeFocused();
  await page.keyboard.type(action);
  await builder.getByLabel("API address").fill(acme);
  await builder.getByLabel("Path", { exact: true }).fill("/orders/{id}/status");
  await builder
    .getByLabel("Example response")
    .fill('{"orderId": "O-1", "status": "shipped"}');
  // Readiness is one line: ready, or how many things to set up.
  await expect(builder.locator(".readiness-line")).toContainText(
    /Platform ready for API actions|to set up|couldn't check/,
  );
  // The preview names the action once this test's freshly started host has
  // analyzed the request; the host allows one analysis 30 seconds
  // (LOCAL_WORK_SECONDS in src/firefly_weave/studio/host.py).
  await expect(
    builder.getByRole("region", { name: "Action preview", exact: true }),
  ).toContainText(`${action}@1.0.0`, { timeout: 30_000 });
  await openYaml(page);
  const preview = page.getByRole("region", { name: /Action YAML for/ });
  await expect(preview).toContainText("sideEffect: read_only");
  await expect(preview).toContainText("path: /orders/{id}/status");
  // The origin stays with the connection; sample values are never kept.
  await expect(preview).not.toContainText("acme.acceptance.test");
  await expect(preview).not.toContainText("shipped");
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

  // The workflow: a unique name and one required text field, "id".
  await designer.deselect();
  await designer.inspectorField("Name").fill(`order-status-${suffix}`);
  const inputSchema = designer.inspector.locator(
    '[data-field="spec/inputSchema"]',
  );
  await inputSchema.getByRole("button", { name: "Add field" }).click();
  await inputSchema.getByLabel("Field name").fill("id");
  await selectChoice(
    inputSchema.getByLabel("Type of “id”", { exact: true }),
    "string",
  );
  await inputSchema.getByRole("checkbox", { name: "Required: “id”" }).check();
  await page.locator(".inspector-header h2").click();

  // The action reads its path parameter from the workflow input, and its
  // answer becomes the workflow's result.
  await designer.selectStep("call-action-1");
  const form = page.locator(".action-input-form");
  await form
    .locator('[data-path="path"] .schema-field')
    .getByRole("button", { name: "Use data", exact: true })
    .click();
  await form.getByRole("combobox", { name: /^ID/ }).fill("/input/id");
  await page.locator(".inspector-header h2").click();
  await designer.inspector
    .getByRole("button", { name: "Use action output as workflow result" })
    .click();
  await page
    .getByRole("dialog", { name: "Replace the workflow result?" })
    .getByRole("button", { name: "Replace result", exact: true })
    .click();
  await shot(page, "10-mapped");
  const source = await designer.source();
  expect(source).toContain(`uses: ${action}@1.0.0`);
  expect(source).toContain("connection: acme");
  expect(source).toMatch(/id:\s+ref: \/input\/id/);
  expect(source).toMatch(
    /connections:\s+acme:\s+connector: weave-http@2\.0\.0/,
  );

  // The connection for the slot, from the inspector: origin only, no secret.
  // The platform accepts its plain-HTTP origin because the private-origin
  // policy approved it.
  await designer.selectStep("call-action-1");
  await designer.inspector
    .getByRole("button", { name: "Create a connection for this API" })
    .click();
  const connect = page.getByRole("dialog", { name: "New API connection" });
  await expect(
    connect.getByLabel("Connection name", { exact: true }),
  ).toHaveValue("acme");
  await expect(connect.getByLabel("API address", { exact: true })).toHaveValue(
    acme,
  );
  // Create needs the connector version the dialog's own check finds; a click
  // while it's still checking only says so (http-connection.spec.ts), and sends
  // nothing.
  await expect(connect.locator(".readiness .hint[role=status]")).toHaveText(
    /^Looks ready\./,
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
  await expect(connect).toContainText("Created acme (revision");
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
    name: `Publish order-status-${suffix} 1.0.0?`,
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
  await expect(activate).toContainText("picked automatically");
  await activate.getByRole("button", { name: "Review", exact: true }).click();
  await expect(activate.getByLabel(/^weave-http@2\.0\.0/)).toHaveValue(
    integrations.release_id,
    { timeout: 30_000 },
  );
  await expect(activate.getByLabel(/^acme/)).toHaveValue(revision);
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
  await run.getByRole("textbox", { name: /^ID/ }).fill("O-1");
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

  // The toast opens the new run; it succeeds with the fixture's answer.
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
  await expect(detail).toContainText("shipped");
  await shot(page, "10-run-succeeded");
  // The fixture saw exactly one call for the order, without an API key.
  if (acmeControl) {
    const journal = (await (
      await fetch(`${acmeControl}/_control/journal`)
    ).json()) as {
      entries: { method: string; path: string; key_sha256: string | null }[];
    };
    expect(
      journal.entries.filter((entry) => entry.path === "/orders/O-1/status"),
    ).toEqual([expect.objectContaining({ method: "GET", key_sha256: null })]);
  }
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

test("12: Refresh finds a run started after Runs opened", async ({ page }) => {
  test.setTimeout(240_000);
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-new-run");
  await pair(page, s.host!, "12-pairing");
  await connectAndSignIn(page, name, person);
  const opened = runSummaries(page);
  await openSidebar(page, "Runs");
  const first = await opened;
  expect(first.status(), "the platform's run summaries").toBe(200);
  const before = new URL(first.url()).searchParams;
  // Only Refresh reads the list from here on.
  await page.getByRole("switch", { name: "Auto-refresh" }).uncheck();
  await shot(page, "12-runs-opened");

  // Another client starts a run on the platform after the page opened.
  const runId = await startDemoRun(page);
  const refreshed = runSummaries(page);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  const second = await refreshed;
  expect(second.status(), "the platform's run summaries").toBe(200);
  const query = new URL(second.url()).searchParams;
  expect(query.has("cursor")).toBe(false);
  const listing = (await second.json()) as {
    items: { id: string; started_at?: string }[];
  };
  expect(listing.items[0]?.id, "the newest run").toBe(runId);
  // The refreshed window ends after the run started, later than the first one.
  const startedBefore = Date.parse(query.get("started_before")!);
  expect(startedBefore).toBeGreaterThan(
    Date.parse(listing.items[0].started_at!),
  );
  expect(startedBefore).toBeGreaterThan(
    Date.parse(before.get("started_before")!),
  );
  await expect(
    page.locator(".runs-table .run-row").first().locator(".cell-run code"),
  ).toHaveText(runId.slice(0, 8), { timeout: 30_000 });
  await shot(page, "12-refreshed");
  expect(s.errors).toEqual([]);
  await removePlatform(page, name, "12");
  expectNoCredentialsLeft();
});

test("13: the AI models screen reaches ready on a local model", async ({
  page,
}) => {
  test.setTimeout(480_000);
  const model = process.env["WEAVE_E2E_AI_MODEL"]?.trim() ?? "";
  test.skip(
    !model,
    "Set WEAVE_E2E_AI_MODEL after weave platform ai enable on this platform.",
  );
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-ai-models");
  await pair(page, s.host!, "13-pairing");
  await connectAndSignIn(page, name, person);
  await openSidebar(page, "Settings");
  await page.getByRole("tab", { name: "AI models" }).click();
  await expect(page).toHaveURL(/\/settings\/ai$/);
  const rows = page.locator(".readiness-row");
  await expect(rows).toHaveCount(8, { timeout: 30_000 });
  await shot(page, "13-ai-models");

  // Test the local model from Studio; a cold model may take up to two minutes.
  await page.getByRole("button", { name: "Test ollama-local" }).click();
  const form = page.getByRole("dialog", { name: "ollama-local" });
  await expect(form.getByLabel("Model", { exact: true })).toHaveValue(model, {
    timeout: 30_000,
  });
  await form.getByRole("button", { name: "Test connection" }).click();
  await expect(form.locator(".test-status")).toContainText(
    `Connected. ${model} answered`,
    { timeout: 170_000 },
  );
  await shot(page, "13-ai-connection-tested");
  await form.getByRole("button", { name: "Close AI connection" }).click();

  // With only `weave platform ai enable` run, every item but Weave AI is ready.
  await page.getByRole("button", { name: "Recheck" }).click();
  await expect(rows.nth(6)).toHaveAttribute("data-status", "done", {
    timeout: 30_000,
  });
  const statuses = await rows.evaluateAll((items) =>
    items.map((item) => [
      item.querySelector(".readiness-label")?.textContent ?? "",
      item.getAttribute("data-status") ?? "",
    ]),
  );
  expect(
    statuses
      .filter(([label]) => label !== "Weave AI")
      .every(([, status]) => status === "done" || status === "not_needed"),
    JSON.stringify(statuses),
  ).toBe(true);
  await expect(rows.nth(7)).toHaveAttribute("data-status", "action");
  await shot(page, "13-ai-models-ready");
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});
