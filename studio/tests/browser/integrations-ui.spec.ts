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
// Where the quick-integration UI is mounted: the inspector's searchable action
// picker, the palette's Integrations section, the step picker, the
// Connections view (New integration, New connection, Check configuration),
// templates on Home and in the "New ▾" menu, and the builder offline.
import { test, expect, Page, Request } from "@playwright/test";
import {
  allCapabilities,
  canUndo,
  command,
  connected,
  insertStep,
  lookupAction,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";
import {
  actionPicker,
  chooseAction,
  openPaletteIntegrations,
  openYaml,
  shot,
} from "./integrations-po";
import {
  hasPython,
  localAuthoring,
  stopLocalAuthoring,
} from "./local-authoring";

test.afterAll(() => stopLocalAuthoring());

const environment = "**/environments/development";
const connection = {
  id: "33333333-3333-4333-8333-333333333333",
  name: "pets",
  connector: "weave-http@2.0.0",
  connector_digest: "d".repeat(64),
  adapter: "weave-http-v2",
  revision: 2,
  config: {
    baseUrl: "https://api.pets.example",
    auth: { kind: "api-key", header: "X-API-Key" },
  },
  secretRef: { api_key: "pets-api-key" },
  allowed_destinations: ["https://api.pets.example"],
};

/** The Connections view with one weave-http connection revision. */
async function connectionsView(
  page: Page,
  capabilities = allCapabilities,
  identityDelay = 0,
) {
  const tests: Request[] = [];
  let answer: { status: number; json: unknown } = {
    status: 200,
    json: { ok: true, code: "ok" },
  };
  await connected(page, { capabilities, identityDelay });
  await page.route(`${environment}/connections?*`, (r) =>
    r.fulfill({ json: { items: [connection], next_cursor: null } }),
  );
  await page.route(`${environment}/connections/${connection.id}`, (r) =>
    r.fulfill({ json: connection }),
  );
  await page.route(`${environment}/connections/${connection.id}/test`, (r) => {
    tests.push(r.request());
    return r.fulfill(answer);
  });
  await page.getByRole("button", { name: "Connections", exact: true }).click();
  return {
    tests,
    answer: (status: number, json: unknown) => (answer = { status, json }),
  };
}

for (const [width, height] of [
  [1440, 900],
  [600, 500],
] as const)
  test.describe(`${width}x${height}`, () => {
    test.use({ viewport: { width, height } });

    test("the inspector's action picker searches by keyboard and binds the only compatible slot", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: lookup
  version: 1.0.0
spec:
  inputSchema:
    type: object
  outputSchema:
    type: object
  connections:
    db:
      connector: weave-postgresql@1.0.0
  steps:
    - id: action-1
      kind: action
      uses: your-action@1.0.0
      with:
        literal: {}
  output:
    literal: {}
`);
      await designer.selectStep("action-1");
      const picker = actionPicker(page);
      await picker.click();
      await expect(picker).toHaveAttribute("aria-expanded", "true");
      // The field shows the step's action; typing replaces it. Both catalog
      // actions match "lookup"; "sql" leaves one.
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.type("lookup");
      await expect(page.locator('[role="option"][data-uses]')).toHaveCount(2);
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.type("sql");
      await expect(page.locator('[role="option"][data-uses]')).toHaveCount(1);
      await page.keyboard.press("Enter");
      await expect(picker).toHaveAttribute("aria-expanded", "false");
      await expect(picker).toBeFocused();
      await expect(picker).toHaveValue("sql.lookup@1.0.0");
      // The workflow's only weave-postgresql slot is chosen for the step.
      await expect(
        page.getByLabel("Connection slot", { exact: true }),
      ).toHaveValue("db");
      await page.getByLabel(/^Customer ID/).fill("c-1");
      await page.locator(".inspector-header h2").click();
      const source = await sourceText(page);
      expect(source).toContain("uses: sql.lookup@1.0.0");
      expect(source).toContain("connection: db");
    });

    test("the picker's New API action opens the builder for the selected step", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      await insertStep(page, "Call an action");
      await new DesignerPage(page).selectStep("call-action-1");
      const picker = actionPicker(page);
      await picker.click();
      await new DesignerPage(page).inspector
        .getByRole("button", { name: "New API action", exact: true })
        .click();
      const builder = page.getByRole("dialog", { name: "New API action" });
      await expect(builder).toBeVisible();
      await expect(builder.getByLabel("Name", { exact: true })).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(builder).toHaveCount(0);
      await expect(
        new DesignerPage(page).inspector.getByRole("button", {
          name: "New API action",
          exact: true,
        }),
      ).toBeFocused();
    });

    test("the palette lists published actions and inserts one with its slot as one undo step", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      const palette = await openPaletteIntegrations(page);
      await expect(
        palette.getByRole("button", { name: /^Insert / }),
      ).toHaveCount(2);
      await page.getByLabel("Search steps").fill("sql");
      await expect(
        palette.getByRole("button", { name: /^Insert / }),
      ).toHaveCount(1);
      await palette
        .getByRole("button", { name: /^Insert sql\.lookup@1\.0\.0/ })
        .click();
      const designer = new DesignerPage(page);
      await expect(designer.node("call-action-1")).toBeVisible();
      await expect(
        designer.node("call-action-1").locator(".node-body"),
      ).toBeFocused();
      let source = await sourceText(page);
      expect(source).toContain("uses: sql.lookup@1.0.0");
      expect(source).toContain("connection: weave-postgresql");
      expect(source).toMatch(
        /connections:\s+weave-postgresql:\s+connector: weave-postgresql@1\.0\.0\s+required: true/,
      );
      await command(page, "Undo");
      source = await sourceText(page);
      expect(source).not.toContain("sql.lookup");
      expect(source).not.toContain("weave-postgresql");
    });

    test("the step picker inserts a published action into the existing compatible slot", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: lookup
  version: 1.0.0
spec:
  inputSchema:
    type: object
  outputSchema:
    type: object
  connections:
    crm-db:
      connector: weave-postgresql@1.0.0
  steps: []
  output:
    literal: {}
`);
      await designer.fit();
      await designer.target("Add a step here, at the start").click();
      await page.locator('[data-option="action:sql.lookup@1.0.0"]').click();
      await expect(designer.node("call-action-1")).toBeVisible();
      const source = await sourceText(page);
      expect(source).toContain("uses: sql.lookup@1.0.0");
      expect(source).toContain("connection: crm-db");
      expect(source.match(/connector: weave-postgresql/g)).toHaveLength(1);
    });

    test("without publish access the picker and palette ask a developer instead", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: allCapabilities.filter((c) => c !== "definition.publish"),
      });
      // Neither "New ▾" nor the Connections view offers the API action builder.
      await page
        .getByRole("button", { name: "Workflows", exact: true })
        .click();
      await page.getByRole("button", { name: "More ways to start" }).click();
      const menu = page.getByRole("menu", { name: "More ways to start" });
      await expect(
        menu.getByRole("menuitem", { name: /^From a template/ }),
      ).toBeVisible();
      await expect(
        menu.getByRole("menuitem", { name: /^New API action/ }),
      ).toHaveCount(0);
      await page.keyboard.press("Escape");
      await page
        .getByRole("button", { name: "Connections", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: "New connection" }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "New integration" }),
      ).toHaveCount(0);
      await page.getByRole("button", { name: "Home", exact: true }).click();
      await newWorkflow(page);
      const palette = await openPaletteIntegrations(page);
      await expect(palette).toContainText(
        "To call another API, ask a developer to publish an action.",
      );
      await expect(
        palette.getByRole("button", { name: "New API action" }),
      ).toHaveCount(0);
      await insertStep(page, "Call an action");
      await new DesignerPage(page).selectStep("call-action-1");
      await expect(actionPicker(page)).toBeVisible();
      await expect(
        page.getByRole("button", {
          name: "New API action",
        }),
      ).toHaveCount(0);
      await actionPicker(page).click();
      await expect(
        page.getByRole("option", { name: "New API action" }),
      ).toHaveCount(0);
      await chooseAction(page, "crm.lookup@2.0.0");
      await expect(actionPicker(page)).toHaveValue("crm.lookup@2.0.0");
    });

    test("choosing an action replaces a slot that doesn't fit it with the only one that does", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: lookup
  version: 1.0.0
spec:
  inputSchema:
    type: object
  outputSchema:
    type: object
  connections:
    crm:
      connector: crm@1.0.0
    db:
      connector: weave-postgresql@1.0.0
  steps:
    - id: action-1
      kind: action
      uses: your-action@1.0.0
      connection: crm
      with:
        literal: {}
  output:
    literal: {}
`);
      await designer.selectStep("action-1");
      await chooseAction(page, "sql.lookup@1.0.0");
      await expect(
        page.getByLabel("Connection slot", { exact: true }),
      ).toHaveValue("db");
    });

    test("a published action whose details load after another workflow opened is not inserted there", async ({
      page,
    }) => {
      await connected(page);
      let release = () => {};
      const held = new Promise<void>((done) => (release = done));
      // Registered last, so it wins over connected()'s export route.
      await page.route("**/projects/project/actions/a1/export", async (r) => {
        await held;
        await r
          .fulfill({ json: { id: "a1", document: lookupAction } })
          .catch(() => undefined);
      });
      await newWorkflow(page);
      const palette = await openPaletteIntegrations(page);
      await palette
        .getByRole("button", { name: /^Insert sql\.lookup@1\.0\.0/ })
        .click();
      // While the action's details load, open a template instead.
      await page.getByRole("button", { name: "Home", exact: true }).click();
      await page
        .getByRole("button", { name: "Use template: Approval, then branch" })
        .click();
      const designer = new DesignerPage(page);
      await expect(designer.node("review")).toBeVisible();
      release();
      await expect(page.locator("[data-step]")).toHaveCount(4);
      await page.waitForTimeout(300);
      await expect(page.locator("[data-step]")).toHaveCount(4);
      const source = await sourceText(page);
      expect(source).not.toContain("sql.lookup");
      expect(source).not.toContain("weave-postgresql");
      expect(await canUndo(page)).toBe(false);
    });

    test("Connections: the detail panel shows handle names and checks the configuration", async ({
      page,
    }, info) => {
      const view = await connectionsView(page);
      await page.locator(".resource-row").click();
      const detail = page.locator(".record-detail");
      await expect(detail).toContainText("https://api.pets.example");
      await expect(detail).toContainText("API key in a header (X-API-Key)");
      await expect(detail).toContainText("API key: pets-api-key");
      const check = detail.getByRole("button", {
        name: "Check configuration (no request is sent)",
      });
      await check.click();
      await expect(detail).toContainText(
        "The platform accepted this configuration.",
      );
      expect(view.tests).toHaveLength(1);
      await expect(check).toBeFocused();
      await shot(page, info, `connections-${width}-detail`);
      // A rejected configuration explains each problem in plain words.
      view.answer(422, {
        code: "WV-CONNECTION",
        message: "Connection requirements are unavailable or incompatible",
        diagnostics: [
          {
            code: "WV-CONNECTION-SECRET",
            path: "/secretRef/api_key",
            message: "This secret handle is not available in this environment.",
          },
        ],
      });
      await check.press("Enter");
      await expect(detail.getByRole("alert")).toContainText(
        "This secret handle is not available in this environment.",
      );
      await expect(detail.getByRole("alert")).toContainText(
        "Support code: WV-CONNECTION",
      );
      expect(view.tests).toHaveLength(2);
    });

    test("Connections: without connection.manage there is no check, and New connection prepares a request for an administrator", async ({
      page,
    }) => {
      await connectionsView(
        page,
        allCapabilities.filter((c) => c !== "connection.manage"),
      );
      // API actions live with workflows: Connections offers connections only.
      await expect(
        page.getByRole("button", { name: "New integration" }),
      ).toHaveCount(0);
      await page.getByRole("button", { name: "New connection" }).click();
      const dialog = page.getByRole("dialog", { name: "New API connection" });
      await expect(dialog).toContainText(
        "An administrator creates the connection",
      );
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "New connection" }),
      ).toBeFocused();
      await page.locator(".resource-row").click();
      await expect(page.locator(".record-detail")).toContainText(
        "pets-api-key",
      );
      await expect(
        page.getByRole("button", {
          name: "Check configuration (no request is sent)",
        }),
      ).toHaveCount(0);
    });

    test("connection guidance updates when identity arrives after the dialog opens", async ({
      page,
    }) => {
      await connectionsView(
        page,
        allCapabilities.filter((c) => c !== "connection.manage"),
        2500,
      );
      await page.getByRole("button", { name: "New connection" }).click();
      const dialog = page.getByRole("dialog", { name: "New API connection" });
      await expect(dialog).toContainText(
        "Sign in to this platform to check your access.",
      );
      await expect(
        dialog.getByRole("button", {
          name: "Prepare the request for an administrator",
        }),
      ).toBeVisible();
      await expect(dialog).toContainText(
        "An administrator creates the connection",
      );
      await expect(dialog).not.toContainText(
        "Sign in to this platform to check your access.",
      );
    });

    test("Workflows: New API action opens the builder without insert buttons", async ({
      page,
    }) => {
      await connectionsView(page);
      await page
        .getByRole("button", { name: "Workflows", exact: true })
        .click();
      await page.getByRole("button", { name: "More ways to start" }).click();
      await page.getByRole("menuitem", { name: /^New API action/ }).click();
      const builder = page.getByRole("dialog", { name: "New API action" });
      await expect(builder).toBeVisible();
      await expect(builder.getByLabel("Name", { exact: true })).toBeFocused();
      await expect(
        builder.getByRole("button", { name: /Insert|Use in this step/ }),
      ).toHaveCount(0);
      await page.keyboard.press("Escape");
      await expect(builder).toHaveCount(0);
    });

    test("Home lists the templates and opens one as a new draft", async ({
      page,
    }, info) => {
      await offline(page);
      const gallery = page.locator("weave-template-gallery");
      await expect(gallery.locator("[data-template]")).toHaveCount(5);
      await gallery.scrollIntoViewIfNeeded();
      await shot(page, info, `templates-${width}-home`);
      const overflow = await page.evaluate(
        () =>
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth,
      );
      expect(overflow).toBe(false);
      await page
        .getByRole("button", { name: "Use template: Approval, then branch" })
        .click();
      const designer = new DesignerPage(page);
      await expect(designer.node("review")).toBeVisible();
      // Focus moves to the new workflow's first step, not to the page.
      await expect(designer.node("review").locator(".node-body")).toBeFocused();
      // Nothing is kept yet: the template opens as an untouched new draft.
      await expect(page.locator(".editor-identity .status-chip")).toHaveCount(
        0,
      );
      // A template starts its own undo history.
      expect(await canUndo(page)).toBe(false);
      expect(await sourceText(page)).toContain("name: approval-then-branch");
    });

    test("the New menu offers templates and works from the keyboard", async ({
      page,
    }, info) => {
      await offline(page);
      await page
        .getByRole("button", { name: "Workflows", exact: true })
        .click();
      const toggle = page.getByRole("button", { name: "More ways to start" });
      await toggle.focus();
      await page.keyboard.press("ArrowDown");
      const menu = page.getByRole("menu", { name: "More ways to start" });
      await expect(menu).toBeVisible();
      await expect(
        menu.getByRole("menuitem", { name: /^Blank workflow/ }),
      ).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await expect(
        menu.getByRole("menuitem", { name: /^From a template/ }),
      ).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0);
      await expect(toggle).toBeFocused();
      await toggle.click();
      await menu.getByRole("menuitem", { name: /^From a template/ }).click();
      const dialog = page.getByRole("dialog", {
        name: "New workflow from a template",
      });
      await expect(dialog.locator("[data-template]")).toHaveCount(5);
      await shot(page, info, `templates-${width}-menu`);
      await dialog
        .getByRole("button", {
          name: "Use template: Wait for a signal with a time limit",
        })
        .click();
      await expect(dialog).toHaveCount(0);
      const designer = new DesignerPage(page);
      await expect(page.locator("[data-step]").first()).toBeVisible();
      await expect(
        designer.node("confirmation").locator(".node-body"),
      ).toBeFocused();
      expect(await designer.source()).toContain("kind: signal");
    });

    test("offline, the builder inserts a placeholder action with its slot", async ({
      page,
    }) => {
      test.skip(
        !hasPython && !process.env.CI,
        "Needs the repository's Python environment.",
      );
      await offline(page);
      await localAuthoring(page);
      await newWorkflow(page);
      const palette = await openPaletteIntegrations(page);
      await expect(palette).toContainText(
        "Connect to browse your project's actions.",
      );
      await palette.getByRole("button", { name: "New API action" }).click();
      const builder = page.getByRole("dialog", { name: "New API action" });
      await expect(builder.getByLabel("Name", { exact: true })).toBeFocused();
      await page.keyboard.type("list-pets");
      await builder.getByLabel("API address").fill("https://api.pets.example");
      await builder.getByLabel("Path", { exact: true }).fill("/v1/pets");
      await openYaml(page);
      await expect(
        page.getByRole("region", { name: /Action YAML for/ }),
      ).toContainText("sideEffect: read_only");
      await expect(builder).toContainText(
        "Connect to a platform to check and publish this action.",
      );
      await builder
        .getByRole("button", { name: "Insert as placeholder" })
        .click();
      await expect(builder).toHaveCount(0);
      // Focus lands on the inserted step, not back on the builder's opener.
      await expect(
        new DesignerPage(page).node("call-action-1").locator(".node-body"),
      ).toBeFocused();
      const source = await sourceText(page);
      expect(source).toContain("uses: list-pets@1.0.0");
      expect(source).toContain("connection: pets");
      expect(source).toMatch(
        /connections:\s+pets:\s+connector: weave-http@2\.0\.0/,
      );
      // Offline there is no platform to create a connection on.
      await new DesignerPage(page).selectStep("call-action-1");
      await expect(
        page.getByRole("button", { name: "Create a connection for this API" }),
      ).toHaveCount(0);
    });
  });

test("at 360 px the templates, the New menu and the Connections tools fit without sideways scrolling", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 640 });
  const sideways = () =>
    page.evaluate(
      () =>
        document.documentElement.scrollWidth >
        document.documentElement.clientWidth,
    );
  await connectionsView(page);
  for (const name of ["New connection", "Refresh"]) {
    const tool = await page
      .getByRole("button", { name, exact: true })
      .boundingBox();
    expect(tool!.x).toBeGreaterThanOrEqual(0);
    expect(tool!.x + tool!.width).toBeLessThanOrEqual(360);
  }
  expect(await sideways()).toBe(false);
  await page.getByRole("button", { name: "Home", exact: true }).click();
  const gallery = page.locator("weave-template-gallery");
  await expect(gallery.locator("[data-template]")).toHaveCount(5);
  await gallery.locator("[data-template]").last().scrollIntoViewIfNeeded();
  expect(await sideways()).toBe(false);
  const card = await gallery.locator("[data-template]").first().boundingBox();
  expect(card!.x).toBeGreaterThanOrEqual(0);
  expect(card!.x + card!.width).toBeLessThanOrEqual(360);
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  await page.getByRole("button", { name: "More ways to start" }).click();
  const menu = page.getByRole("menu", { name: "More ways to start" });
  await expect(menu).toBeVisible();
  const box = await menu.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(360);
  expect(await sideways()).toBe(false);
});

test("at 600x500 the New menu stays under its button when the page scrolls", async ({
  page,
}) => {
  await page.setViewportSize({ width: 600, height: 500 });
  await offline(page);
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  const toggle = page.getByRole("button", { name: "More ways to start" });
  await toggle.click();
  const menu = page.getByRole("menu", { name: "More ways to start" });
  await expect(menu).toBeVisible();
  const scroller = await toggle.evaluate((element) => {
    for (let node = element.parentElement; node; node = node.parentElement)
      if (node.scrollHeight > node.clientHeight + 20) {
        const style = getComputedStyle(node);
        if (/(auto|scroll)/.test(style.overflowY)) return true;
      }
    return document.documentElement.scrollHeight > innerHeight;
  });
  expect(scroller).toBe(true);
  // Scroll with the pointer beside the menu, over the page.
  await page.mouse.move(590, 470);
  await page.mouse.wheel(0, 120);
  await expect
    .poll(async () => (await toggle.boundingBox())!.y)
    .toBeLessThan(150);
  const button = (await toggle.boundingBox())!;
  await expect
    .poll(async () => Math.round((await menu.boundingBox())!.y))
    .toBe(Math.round(button.y + button.height + 6));
  // Focus stays in the menu, so the keyboard keeps working.
  await expect(
    menu.getByRole("menuitem", { name: /^Blank workflow/ }),
  ).toBeFocused();
});
