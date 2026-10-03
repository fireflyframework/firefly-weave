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
// Regression tests for the layout defects the visual tour found: each test
// holds one fix at the size where the defect showed.
import { test, expect, Locator, Page } from "@playwright/test";
import { allCapabilities, connected, newWorkflow, offline } from "./support";
import { DesignerPage } from "./designer-po";
import { acmePlatform, platformHost } from "./platform-host";

/** The element's box, or a failed expectation naming it. */
async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found, `${locator}`).not.toBeNull();
  return found!;
}
/** The element neither scrolls nor overflows sideways. */
async function expectNoSidewaysScroll(locator: Locator) {
  const sizes = await locator.evaluate((e) => [e.scrollWidth, e.clientWidth]);
  expect(sizes[0], `${locator} scroll width`).toBeLessThanOrEqual(sizes[1] + 1);
}
/** A pointer at the element's center reaches the element itself. */
async function expectUncovered(locator: Locator) {
  const hit = await locator.evaluate((element) => {
    const r = element.getBoundingClientRect();
    const found = document.elementFromPoint(
      r.left + r.width / 2,
      r.top + r.height / 2,
    );
    return found === element || element.contains(found)
      ? "self"
      : `${found?.tagName}.${found?.className}`;
  });
  expect(hit, `${locator}`).toBe("self");
}
/** Clicks a palette step, opening the narrow-layout palette when needed. */
async function addStep(page: Page, label: string) {
  const item = page
    .locator(".palette-step")
    .filter({ hasText: new RegExp(`^\\s*${label}`) });
  await expect(async () => {
    if (!(await item.isVisible()))
      await page.getByRole("button", { name: "Insert step" }).click();
    await item.click({ timeout: 2000 });
  }).toPass({ timeout: 20_000 });
}

test.describe("360x740", () => {
  test.use({ viewport: { width: 360, height: 740 } });

  test("a long diagnostic wraps inside the diagnostics area", async ({
    page,
  }) => {
    await page.route("**/studio/session", (r) =>
      r.fulfill({
        json: {
          paired: true,
          csrfToken: "x",
          version: "1",
          mode: "offline",
          profile: null,
        },
      }),
    );
    await page.route("**/studio/local/validate", (r) =>
      r.fulfill({
        json: {
          validationOk: false,
          errorCount: 1,
          partial: true,
          diagnostics: [
            {
              code: "WV-COMP-UNAVAILABLE_REFERENCE",
              severity: "error",
              stage: "semantic",
              path: "/spec/steps/0/value/ref",
              message:
                "Referenced step does not dominate this expression in its lexical scope.",
            },
          ],
        },
      }),
    );
    await page.goto("/");
    await newWorkflow(page);
    await addStep(page, "Transform");
    const close = page.getByRole("button", { name: "Close inspector" });
    if (await close.isVisible()) await close.click();
    const panel = page.getByRole("region", { name: "Compiler diagnostics" });
    const row = panel.locator(".diagnostic-target").first();
    await expect(row).toContainText("hasn't finished");
    await expectNoSidewaysScroll(panel);
    // The severity icon starts the row at its left edge, not in its middle,
    // with the severity word right after it.
    const icon = await box(row.locator(".diagnostic-icon"));
    const severity = await box(row.locator(".diagnostic-severity"));
    const target = await box(row);
    expect(icon.x - target.x).toBeLessThan(12);
    expect(severity.x - (icon.x + icon.width)).toBeLessThan(12);
    const text = await box(row.locator(".diagnostic-text"));
    expect(text.x + text.width).toBeLessThanOrEqual(target.x + target.width);
  });

  test("the detail panel's long check button wraps instead of scrolling", async ({
    page,
  }) => {
    const connection = {
      id: "33333333-3333-4333-8333-333333333333",
      name: "pets",
      connector: "weave-http@2.0.0",
      adapter: "weave-http-v2",
      revision: 2,
      config: {
        baseUrl: "https://api.pets.example",
        auth: { kind: "api-key", header: "X-API-Key" },
      },
      secretRef: { api_key: "pets-api-key" },
      allowed_destinations: ["https://api.pets.example"],
    };
    await connected(page);
    await page.route("**/environments/development/connections?*", (r) =>
      r.fulfill({ json: { items: [connection], next_cursor: null } }),
    );
    await page.route(
      `**/environments/development/connections/${connection.id}`,
      (r) => r.fulfill({ json: connection }),
    );
    await page
      .getByRole("button", { name: "Connections", exact: true })
      .click();
    await page.locator(".resource-row").click();
    const detail = page.locator(".record-detail");
    await expect(
      detail.getByRole("button", {
        name: "Check configuration (no request is sent)",
      }),
    ).toBeVisible();
    await expectNoSidewaysScroll(detail);
  });

  test("detail facts put each label above its value on a phone", async ({
    page,
  }) => {
    const worker = {
      id: "7c9e6679-7425-40de-944b-e07fc1f90ae7",
      release_id: "11111111-1111-4111-8111-111111111111",
      task_types: ["crm-lookup"],
      capacity: 4,
      revoked: false,
    };
    await connected(page);
    await page.route("**/environments/development/workers?*", (r) =>
      r.fulfill({ json: { items: [worker], next_cursor: null } }),
    );
    await page.route(`**/workers/${worker.id}`, (r) =>
      r.fulfill({ json: worker }),
    );
    await page.getByRole("button", { name: "Workers", exact: true }).click();
    await page.locator(".resource-row").click();
    const facts = page.locator(".record-detail .task-metadata").first();
    await expect(facts).toContainText("crm-lookup");
    const label = await box(facts.locator("dt").first());
    const value = await box(facts.locator("dd").first());
    expect(Math.abs(value.x - label.x)).toBeLessThanOrEqual(1);
    expect(value.y).toBeGreaterThan(label.y);
  });

  test("the inspector is a full-height side sheet on a phone", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await addStep(page, "Transform");
    await new DesignerPage(page).selectStep("transform-1");
    const sheet = await box(page.locator(".inspector"));
    // From under the 60 px top bar to the bottom of the window.
    expect(sheet.y).toBe(60);
    expect(sheet.y + sheet.height).toBe(740);
    expect(sheet.x + sheet.width).toBe(360);
  });

  test("a new property's name field keeps its width in the inspector", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await addStep(page, "Call an action");
    await new DesignerPage(page).selectStep("call-action-1");
    const name = new DesignerPage(page).inspector
      .getByRole("textbox", { name: "New property name" })
      .first();
    await name.scrollIntoViewIfNeeded();
    expect((await box(name)).width).toBeGreaterThanOrEqual(100);
  });
});

test.describe("1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Import on Workflows is a keyboard button that opens the file chooser", async ({
    page,
  }) => {
    await offline(page);
    await page.getByRole("button", { name: "Workflows", exact: true }).click();
    const importButton = page.getByRole("button", {
      name: "Import workflow",
      exact: true,
    });
    // The list may still render once more: retry until Enter on the focused
    // button opens the chooser.
    await expect(async () => {
      await importButton.focus();
      await expect(importButton).toBeFocused();
      const chooser = page.waitForEvent("filechooser", { timeout: 2000 });
      await page.keyboard.press("Enter");
      expect((await chooser).isMultiple()).toBe(false);
    }).toPass({ timeout: 15_000 });
  });

  test("an OpenAPI file is chosen with a real button", async ({ page }) => {
    await offline(page);
    await newWorkflow(page);
    const palette = page.locator("weave-palette-integrations");
    await palette.getByRole("button", { name: "New API action" }).click();
    const builder = page.getByRole("dialog", { name: "New API action" });
    await builder.getByRole("tab", { name: "Import OpenAPI" }).click();
    const choose = builder.getByRole("button", { name: "Choose a file" });
    await expect(async () => {
      await choose.focus();
      await expect(choose).toBeFocused();
      const chooser = page.waitForEvent("filechooser", { timeout: 2000 });
      await page.keyboard.press("Enter");
      await chooser;
    }).toPass({ timeout: 15_000 });
  });

  test("a claimed task's form never makes the app shell scroll", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    await connected(page, {
      capabilities: [
        ...allCapabilities,
        "human_task.claim",
        "human_task.complete",
      ],
    });
    let task: Record<string, unknown> = {
      id: "task-1",
      run_id: "run",
      node_id: "review",
      revision: 1,
      status: "ready",
      title: "Review expense 104",
      context: { amount: 125, currency: "EUR" },
      form_schema: {
        type: "object",
        required: ["note"],
        properties: {
          note: { type: "string", title: "Review note" },
          approvedAmount: { type: "number", title: "Approved amount" },
        },
      },
      decisions: ["approve", "reject"],
      claimant_id: null,
    };
    await page.route("**/human-tasks?*", (r) =>
      r.fulfill({ json: { items: [task], next_cursor: null } }),
    );
    await page.route("**/human-tasks/task-1", (r) => r.fulfill({ json: task }));
    await page.route("**/human-tasks/task-1/claim", (r) => {
      task = { ...task, status: "claimed", claimant_id: "human", revision: 2 };
      return r.fulfill({ json: task });
    });
    await page.getByRole("button", { name: "My tasks", exact: true }).click();
    await page.locator(".resource-row").click();
    await page.getByRole("button", { name: "Claim task", exact: true }).click();
    await expect(page.getByLabel("Review note")).toBeVisible();
    // A visually hidden status deep in the form stays inside its pane.
    const overflow = await page.evaluate(
      () => document.scrollingElement!.scrollHeight - window.innerHeight,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("the Assign role panel stacks its fields and keeps its distance", async ({
    page,
  }) => {
    await connected(page, {
      capabilities: [...allCapabilities, "grant.admin", "grant.manage"],
    });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("tab", { name: "People and access" }).click();
    await page
      .locator(".administration .card-heading")
      .getByRole("button", { name: "Assign role" })
      .click();
    const form = page.locator(".member-grant-form");
    const account = await box(form.getByLabel("Account ID"));
    const role = await box(form.getByLabel("Role", { exact: true }));
    const scope = await box(form.getByLabel("Applies to"));
    // One column: every field starts at the same left edge, one per row.
    expect(Math.abs(account.x - role.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(scope.x - role.x)).toBeLessThanOrEqual(1);
    expect(role.y).toBeGreaterThan(account.y + account.height);
    expect(scope.y).toBeGreaterThan(role.y + role.height);
    // The Accounts heading keeps its distance from the form above it.
    const create = await box(
      page.getByRole("button", { name: "Create account" }),
    );
    const heading = await box(page.getByRole("heading", { name: "Accounts" }));
    expect(heading.y - (create.y + create.height)).toBeGreaterThanOrEqual(16);
  });

  test("connection form successes look like the detail panel's", async ({
    page,
  }) => {
    await connected(page);
    await page.route("**/projects/project/capabilities", (r) =>
      r.fulfill({ json: { connectors: ["weave-http-v2"] } }),
    );
    await page.route(
      "**/projects/project/connector-descriptors/weave-http-v2",
      (r) =>
        r.fulfill({
          json: {
            adapter: "weave-http-v2",
            reference: "weave-http@2.0.0",
            digest: "e".repeat(64),
            manifest: {},
            source: "{}",
            implementation_version: "2.0.0",
            capabilities: [],
            bindings: [],
            actions: [],
            connection: { config_schema: {}, auth_schema: {} },
            published_version_id: "7a8cbeef-7d5c-483a-b926-4157ad4286d0",
          },
        }),
    );
    await page.route("**/environments/development/connections", (r) =>
      r.fulfill({
        status: 201,
        json: {
          id: "0f8fad5b-d9cb-469f-a165-70867728950e",
          name: "pets",
          revision: 1,
          connector: "weave-http@2.0.0",
        },
      }),
    );
    await page
      .getByRole("button", { name: "Connections", exact: true })
      .click();
    await page.getByRole("button", { name: "New connection" }).click();
    const dialog = page.getByRole("dialog", { name: "New API connection" });
    await dialog.getByLabel("Connection name", { exact: true }).fill("pets");
    await dialog
      .getByLabel("API address", { exact: true })
      .fill("https://api.pets.example");
    await dialog
      .getByLabel("How the API checks who is calling", { exact: true })
      .selectOption("api-key");
    await dialog.getByLabel("Header name", { exact: true }).fill("X-API-Key");
    await dialog
      .getByLabel("API key handle", { exact: true })
      .fill("pets-api-key");
    await dialog.getByRole("button", { name: "Create connection" }).click();
    const created = dialog.locator(".created-notice");
    await expect(created).toHaveText("Created pets (revision 1).");
    const background = await created.evaluate(
      (e) => getComputedStyle(e).backgroundColor,
    );
    // --success-bg, the success tone, not the caution gold of a plain notice.
    expect(background).toBe("rgb(226, 241, 233)");
  });

  test("a selected row keeps a visible status pill", async ({ page }) => {
    const run = {
      id: "00000000-0000-4000-8000-000000000104",
      business_key: "expense-104",
      state: { status: "waiting", active: [] },
    };
    await connected(page);
    await page.route("**/environments/development/runs?*", (r) =>
      r.fulfill({ json: { items: [run], next_cursor: null } }),
    );
    await page.route(`**/environments/development/runs/${run.id}`, (r) =>
      r.fulfill({ json: run }),
    );
    await page.getByRole("button", { name: "Runs", exact: true }).click();
    const row = page.locator(".resource-row");
    await row.click();
    await expect(row).toHaveClass(/selected/);
    const pill = await row.locator(".status-pill").evaluate((e) => {
      const rgb = (value: string) =>
        (value.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
      const luminance = (value: string) => {
        const [r, g, b] = rgb(value).map((c) => {
          const s = c / 255;
          return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const ratio = (a: string, b: string) => {
        const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
        return (x + 0.05) / (y + 0.05);
      };
      const style = getComputedStyle(e);
      const row = getComputedStyle(e.closest(".resource-row")!);
      return {
        border: ratio(style.borderTopColor, row.backgroundColor),
        text: ratio(style.color, style.backgroundColor),
        width: style.borderTopWidth,
      };
    });
    // Not mist on mist: the pill keeps a tone border the row can't swallow.
    expect(pill.width).toBe("1px");
    expect(pill.border).toBeGreaterThanOrEqual(1.3);
    expect(pill.text).toBeGreaterThanOrEqual(6);
  });

  test("a schema's JSON switch looks like a link, not a box", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    const link = new DesignerPage(page).inspector.getByRole("button", {
      name: "Edit input schema as JSON",
    });
    await expect(link).toBeVisible();
    const look = await link.evaluate((e) => [
      getComputedStyle(e).borderTopWidth,
      getComputedStyle(e).backgroundColor,
      getComputedStyle(e).textDecorationLine,
    ]);
    expect(look).toEqual(["0px", "rgba(0, 0, 0, 0)", "underline"]);
  });

  test("a long dialog keeps its title and close button in view", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 600, height: 500 });
    await offline(page);
    await newWorkflow(page);
    const palette = page.locator("weave-palette-integrations");
    if (!(await palette.isVisible()))
      await page.getByRole("button", { name: "Insert step" }).click();
    await palette.getByRole("button", { name: "New API action" }).click();
    const builder = page.getByRole("dialog", { name: "New API action" });
    await expect(builder).toBeVisible();
    await builder.evaluate((panel) => (panel.scrollTop = panel.scrollHeight));
    const panel = await box(builder);
    const title = await box(
      builder.getByRole("heading", { name: "New API action" }),
    );
    expect(title.y).toBeGreaterThanOrEqual(panel.y);
    expect(title.y + title.height).toBeLessThanOrEqual(panel.y + 80);
    await expectUncovered(
      builder.getByRole("button", { name: "Close the API action builder" }),
    );
  });
});

test("Home's connect button sits with the other ways to start and scrolls with Home", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await platformHost(page, {});
  const actions = page.locator("weave-home-dashboard .home-actions");
  const connect = actions.getByRole("button", {
    name: "Connect to a platform",
  });
  const create = await box(
    actions.getByRole("button", { name: "New workflow" }),
  );
  const button = await box(connect);
  // On the same line as New workflow, after it.
  expect(Math.abs(button.y - create.y)).toBeLessThanOrEqual(1);
  expect(button.x).toBeGreaterThan(create.x);
  await page.setViewportSize({ width: 600, height: 500 });
  const before = (await box(connect)).y;
  await page.locator(".home-view").evaluate((e) => (e.scrollTop = 120));
  await expect.poll(async () => (await box(connect)).y).toBeLessThan(before);
});

test("a focused wizard control is never under its sticky footer", async ({
  page,
}) => {
  await page.setViewportSize({ width: 600, height: 500 });
  await platformHost(page, {
    platforms: [acmePlatform({ workspace: null, workspaces: [] })],
    active: "Acme",
  });
  await page.locator(".platform-indicator").click();
  await page.getByRole("button", { name: "Switch workspace" }).click();
  const details = page.getByLabel("Details for your administrator");
  await expect(details).toBeVisible();
  await details.focus();
  for (const name of ["Copy details", "Check again"]) {
    await page.keyboard.press("Tab");
    const control = page.getByRole("button", { name });
    await expect(control).toBeFocused();
    await expectUncovered(control);
  }
});

for (const viewport of [
  { width: 1440, height: 900 },
  // The overlay inspector is hidden while another step is chosen.
  { width: 600, height: 500 },
])
  test(`another step's inspector starts at the top (${viewport.width}x${viewport.height})`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await offline(page);
    await newWorkflow(page);
    await addStep(page, "Human task");
    await addStep(page, "Transform");
    const designer = new DesignerPage(page);
    await designer.selectStep("approval-1");
    const body = page.locator(".inspector-body");
    await body.evaluate((e) => (e.scrollTop = e.scrollHeight));
    expect(await body.evaluate((e) => e.scrollTop)).toBeGreaterThan(0);
    await designer.selectStep("transform-1");
    await expect.poll(() => body.evaluate((e) => e.scrollTop)).toBe(0);
  });
