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
// Publishing a workflow whose Call an API and email steps call actions the
// workflow owns: Studio publishes those actions first, then the workflow,
// and Simulate publishes only the actions. The platform here is a small
// catalog kept by the test; its checks are the repository's own compiler.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse, stringify } from "yaml";
import {
  builtinActions,
  hasPython,
  localAuthoring,
  platformCompile,
  stopLocalAuthoring,
} from "./local-authoring";
import {
  addCommonStep,
  openStepFixture,
  sizes,
  yamlFile,
} from "./step-details-po";
import {
  box,
  boxes,
  clippedText,
  density,
  expectAligned,
} from "./step-geometry";
import {
  allCapabilities,
  closeSheet,
  command,
  connected,
  sourceText,
  tokenColor,
} from "./support";

test.skip(!hasPython, "Needs the repository's Python environment.");
test.afterAll(() => stopLocalAuthoring());

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const API = "order-intake.call-api@1.0.0";
const EMAIL = "order-intake.send-email@1.0.0";
const base = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-intake, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      customerId: {type: string, title: Customer ID}
  outputSchema: {type: object}
  connections:
    crm: {connector: weave-http@2.0.0, required: true}
    mail: {connector: weave-email@1.0.0, required: true}
  steps:
    - id: summary
      kind: transform
      value: {literal: {done: true}}
  output: {literal: {}}
`;

type Definition = Record<string, unknown> & {
  metadata: { name: string; version: string };
};
interface PlatformCall {
  path: "actions" | "workflows";
  name: string;
  version: string;
  key: string;
  /** What a workflow publish sent. */
  source?: string;
}
interface PlatformOptions {
  capabilities?: string[];
  /** Versions already published by someone else. */
  taken?: Definition[];
  /** An action name whose first publish answers 503. */
  failOnce?: string;
  /** The URL typed into Call an API ("" leaves it empty). */
  url?: string;
  /** Holds every action publish until the test releases it. */
  hold?: boolean;
}

/** JSON with sorted keys: two documents with the same content read the same. */
const canonical = (value: unknown): string =>
  JSON.stringify(value, (_key, item: unknown) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? Object.fromEntries(
          Object.entries(item as Record<string, unknown>).sort(([a], [b]) =>
            a < b ? -1 : a > b ? 1 : 0,
          ),
        )
      : item,
  );

/**
 * A connected platform with a catalog in memory. Compiles go to the
 * repository's compiler with what was published; a publish keeps the
 * version, answers the same content with the version it has, and refuses
 * other content under a taken version, as the platform does.
 */
async function ownedPlatform(page: Page, options: PlatformOptions) {
  const catalog = new Map<string, Definition>(
    (options.taken ?? []).map((definition) => [
      `${definition.metadata.name}@${definition.metadata.version}`,
      definition,
    ]),
  );
  const calls: PlatformCall[] = [];
  let failed = false;
  let release = () => undefined as void;
  const held = options.hold
    ? new Promise<void>((done) => (release = done))
    : null;
  await localAuthoring(page);
  await page.route("**/studio/contracts/connector-actions", (route) =>
    route.fulfill({ json: builtinActions() }),
  );
  await connected(page, {
    capabilities: options.capabilities ?? [...allCapabilities, "compile"],
    catalog: [],
  });
  const compile = (source: unknown, format: string) =>
    platformCompile({ source, format, definitions: [...catalog.values()] });
  await page.route(`${project}/compiler/compile`, async (route) => {
    const body = route.request().postDataJSON() as {
      source: unknown;
      format: string;
    };
    await route.fulfill({ json: await compile(body.source, body.format) });
  });
  await page.route(`${project}/actions`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const definition = parse(
      (route.request().postDataJSON() as { source: string }).source,
    ) as Definition;
    const { name, version } = definition.metadata;
    calls.push({
      path: "actions",
      name,
      version,
      key: route.request().headers()["idempotency-key"] ?? "",
    });
    if (held) await held;
    if (options.failOnce === name && !failed) {
      failed = true;
      return route.fulfill({
        status: 503,
        json: { code: "WV-UNAVAILABLE", message: "The platform is busy." },
      });
    }
    const existing = catalog.get(`${name}@${version}`);
    if (existing && canonical(existing) !== canonical(definition))
      return route.fulfill({
        status: 409,
        json: {
          code: "WV-VERSION-CONFLICT",
          message: "An immutable version already has different content",
        },
      });
    const result = (await compile(definition, "object")) as { ok: boolean };
    if (!result.ok)
      return route.fulfill({
        status: 422,
        json: {
          code: "WV-COMPILE",
          message: "Definition compilation failed",
          result,
        },
      });
    catalog.set(`${name}@${version}`, existing ?? definition);
    await route.fulfill({
      status: 201,
      json: { id: `action-${catalog.size}`, kind: "Action", name, version },
    });
  });
  await page.route(`${project}/workflows`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = route.request().postDataJSON() as {
      source: string;
      format: string;
    };
    const { name, version } = (parse(body.source) as Definition).metadata;
    calls.push({
      path: "workflows",
      name,
      version,
      key: route.request().headers()["idempotency-key"] ?? "",
      source: body.source,
    });
    const result = (await compile(body.source, body.format)) as { ok: boolean };
    if (!result.ok)
      return route.fulfill({
        status: 422,
        json: {
          code: "WV-COMPILE",
          message: "Definition compilation failed",
          result,
        },
      });
    await route.fulfill({
      status: 201,
      json: {
        id: "11111111-1111-4111-8111-111111111111",
        kind: "Workflow",
        name,
        version,
        digest: "0".repeat(64),
      },
    });
  });
  return { calls, catalog, release: () => release() };
}

/** Replaces the workflow source in Source and applies it. */
async function applySource(page: Page, text: string) {
  await closeSheet(page);
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await page.getByRole("textbox", { name: "Workflow source" }).fill(text);
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
}
const stepOf = async (page: Page, id: string) =>
  (parse(await sourceText(page)).spec.steps as Record<string, unknown>[]).find(
    (step) => step["id"] === id,
  )!;

/**
 * order-intake with Send email and Call an API (GET /orders) inserted from
 * the step picker, so the workflow owns both actions; the email's fields
 * come from Source.
 */
async function withOwnedSteps(page: Page, options: PlatformOptions = {}) {
  let platform!: Awaited<ReturnType<typeof ownedPlatform>>;
  const details = await openStepFixture(
    page,
    yamlFile("order-intake.yaml", base),
    async (p) => {
      platform = await ownedPlatform(p, options);
    },
  );
  await addCommonStep(page, "Send email");
  await expect(details.dialogFor("send-email")).toBeVisible();
  await details.close();
  await addCommonStep(page, "Call an API");
  await expect(details.dialogFor("call-api")).toBeVisible();
  if (options.url !== "") {
    const url = details.field("url").getByRole("textbox", { name: "URL" });
    await url.fill(options.url ?? "/orders");
    await url.press("Tab");
  }
  await details.close();
  const document = parse(await sourceText(page));
  const email = (document.spec.steps as Record<string, unknown>[]).find(
    (step) => step["id"] === "send-email",
  )!;
  email["with"] = {
    literal: {
      to: ["ana@example.com"],
      subject: "Your order",
      text: "Thanks for your order.",
    },
  };
  await applySource(page, stringify(document));
  return { details, platform };
}
const publishDialog = (page: Page) =>
  page.getByRole("dialog", { name: "Publish order-intake 1.0.0?" });

/**
 * The publish dialog's design: one left and one right edge for its content
 * (heading, message and list; close button, list and main button), one left
 * edge for the rows' text and one right edge for their states, each row's
 * text and state centered on the row without touching, rows at least as
 * tall as the density's row, no clipped text, the main button inside the
 * window and nothing scrolling sideways.
 */
async function expectTidy(page: Page, dialog: Locator, height: number) {
  const list = await box(dialog.locator(".owned-list"));
  const rows = await boxes(dialog.locator(".owned-row"));
  const texts = await boxes(dialog.locator(".owned-text"));
  const states = await boxes(dialog.locator(".owned-state"));
  const primary = await box(dialog.locator(".dialog-footer .primary"));
  const close = await box(dialog.locator(".modal-header button"));
  expectAligned(
    [
      (await box(dialog.getByRole("heading"))).x,
      (await box(dialog.locator(".dialog-message"))).x,
      list.x,
      (await box(dialog.locator(".owned-summary"))).x,
    ],
    "content left edge",
  );
  expectAligned([close.right, list.right, primary.right], "content right edge");
  expectAligned(
    rows.map((row) => row.x),
    "row left edges",
  );
  expectAligned(
    rows.map((row) => row.right),
    "row right edges",
  );
  expectAligned(
    states.map((state) => state.right),
    "state right edges",
  );
  expectAligned(
    [
      ...(await boxes(dialog.locator(".owned-title"))),
      ...(await boxes(dialog.locator(".owned-meta"))),
      ...(await boxes(dialog.locator(".owned-note"))),
    ].map((text) => text.x),
    "text left edge",
  );
  const rhythm = await density(page);
  rows.forEach((row, index) => {
    expect(row.height).toBeGreaterThanOrEqual(rhythm.row - 0.5);
    expectAligned(
      [row.middle, texts[index].middle, states[index].middle],
      `row ${index + 1} centers`,
    );
    expect(texts[index].right).toBeLessThanOrEqual(states[index].x + 0.5);
  });
  expect(await clippedText(dialog, "")).toEqual([]);
  expect(primary.y).toBeGreaterThanOrEqual(0);
  expect(primary.bottom).toBeLessThanOrEqual(height);
  expect(
    await dialog.evaluate(
      (panel) =>
        panel.scrollWidth <= panel.clientWidth &&
        document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
}
/**
 * Waits until the workflow is published, and closes the toast that says
 * so, which would otherwise sit over step details' fields on small screens.
 */
async function workflowPublished(page: Page) {
  const toast = page.locator(".toast-region");
  await expect(toast).toContainText("Published order-intake 1.0.0.");
  await toast.getByRole("button", { name: "Dismiss notification" }).click();
}
const posted = (calls: PlatformCall[]) =>
  calls.map((call) =>
    call.path === "actions" ? `${call.name}@${call.version}` : call.path,
  );

for (const size of sizes)
  test.describe(`publishing a workflow's own actions at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Publish… publishes the step actions first, then the workflow, and Save to file says what it leaves out", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page);
      await command(page, "Save to file");
      await expect(page.locator(".toast-region")).toContainText(
        "These files don't keep the settings of 2 actions of this workflow's Call an API, email or file steps yet. Publish the workflow to keep them in the project.",
      );
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      const rows = dialog.locator(".owned-row");
      await expect(dialog.locator(".owned-state")).toHaveText([
        "Ready",
        "Ready",
      ]);
      await expect(rows.nth(0)).toContainText("order-intake.call-api · 1.0.0");
      await expect(rows.nth(0)).toContainText("Used by call-api");
      await expect(rows.nth(1)).toContainText(
        "order-intake.send-email · 1.0.0",
      );
      await dialog
        .getByRole("button", { name: "Publish 2 actions and the workflow" })
        .click();
      await expect(dialog).toHaveCount(0);
      await expect(page.locator(".toast-region")).toContainText(
        "Published order-intake 1.0.0.",
      );
      expect(posted(platform.calls)).toEqual([API, EMAIL, "workflows"]);
      expect(new Set(platform.calls.map((call) => call.key)).size).toBe(3);
    });

    test("Validate says the step actions publish first, and step details count no issue for them", async ({
      page,
    }) => {
      const { details, platform } = await withOwnedSteps(page);
      await command(page, "Validate");
      await expect(page.locator("#main")).toContainText(
        "No other problems found. Steps that call this workflow's own actions are checked again when Publish… or Simulate publishes those actions.",
      );
      await details.open("call-api");
      await expect(details.dialog.locator(".sd-issues-pill")).toHaveCount(0);
      expect(platform.calls).toEqual([]);
    });

    test("a change to a published action starts its next version, and Undo returns to it", async ({
      page,
    }) => {
      const { details, platform } = await withOwnedSteps(page);
      await command(page, "Publish…");
      await publishDialog(page)
        .getByRole("button", { name: "Publish 2 actions and the workflow" })
        .click();
      await expect(publishDialog(page)).toHaveCount(0);
      await workflowPublished(page);
      await details.open("call-api");
      await details
        .field("method")
        .getByRole("combobox", { name: "Method" })
        .click();
      await page.getByRole("option", { name: /^POST/ }).click();
      await expect(page.locator(".toast-region")).toContainText(
        "order-intake.call-api 1.0.0 is published, so this change starts version 1.0.1.",
      );
      await details.close();
      expect((await stepOf(page, "call-api"))["uses"]).toBe(
        "order-intake.call-api@1.0.1",
      );
      expect((await stepOf(page, "send-email"))["uses"]).toBe(EMAIL);
      await command(page, "Undo");
      expect((await stepOf(page, "call-api"))["uses"]).toBe(API);
      await command(page, "Redo");
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await expect(dialog.locator(".owned-row")).toHaveCount(1);
      await expect(dialog.locator(".owned-row")).toContainText(
        "order-intake.call-api · 1.0.1",
      );
      await expect(dialog.locator(".owned-note")).toHaveText(
        "Changed after version 1.0.0 was published.",
      );
      await expect(
        dialog.getByRole("button", {
          name: "Publish 1 action and the workflow",
        }),
      ).toBeVisible();
      await dialog.getByRole("button", { name: "Keep editing" }).click();
      await expect(dialog).toHaveCount(0);
      expect(posted(platform.calls)).toEqual([API, EMAIL, "workflows"]);
    });

    test("a failed action publish stops before the workflow, and Try again resends the same request", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page, {
        failOnce: "order-intake.send-email",
      });
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await dialog
        .getByRole("button", { name: "Publish 2 actions and the workflow" })
        .click();
      await expect(dialog.locator(".owned-state")).toHaveText([
        "Published",
        "Not confirmed",
      ]);
      await expect(dialog.getByRole("status")).toHaveText(
        "1 of 2 actions published. The workflow wasn't published. What was published stays as it is. Try again publishes the rest.",
      );
      expect(
        await dialog
          .locator(".owned-state")
          .first()
          .evaluate((element) => getComputedStyle(element).color),
      ).toBe(await tokenColor(page, "--success-ink"));
      expect(
        await dialog
          .locator(".owned-state")
          .nth(1)
          .evaluate((element) => getComputedStyle(element).color),
      ).toBe(await tokenColor(page, "--warning-ink"));
      await expect(
        dialog.getByRole("button", { name: "Try again" }),
      ).toBeFocused();
      await expectTidy(page, dialog, size.height);
      await dialog.getByRole("button", { name: "Try again" }).click();
      await expect(dialog).toHaveCount(0);
      expect(posted(platform.calls)).toEqual([API, EMAIL, EMAIL, "workflows"]);
      const keys = platform.calls.map((call) => call.key);
      expect(keys[2]).toBe(keys[1]);
      expect(keys[0]).not.toBe(keys[1]);
    });

    test("an action name taken by other content is published under a free name", async ({
      page,
    }) => {
      const send = (
        builtinActions() as {
          actions: {
            connector: string;
            action: string;
            document: Definition;
          }[];
        }
      ).actions.find(
        (entry) =>
          entry.connector === "weave-email@1.0.0" && entry.action === "send",
      )!.document;
      const taken = {
        ...structuredClone(send),
        metadata: { name: "order-intake.send-email", version: "1.0.0" },
      };
      (taken["spec"] as Record<string, unknown>)["timeoutSeconds"] = 10;
      const { platform } = await withOwnedSteps(page, { taken: [taken] });
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      const row = dialog.locator(".owned-row", {
        hasText: "order-intake.send-email",
      });
      await expect(row.locator(".owned-state")).toHaveText("Taken");
      await expect(row).toContainText(
        "Version 1.0.0 of order-intake.send-email is already published with different content. Studio publishes this one as order-intake.send-email-2 1.0.0.",
      );
      await dialog
        .getByRole("button", { name: "Publish 2 actions and the workflow" })
        .click();
      await expect(dialog).toHaveCount(0);
      expect((await stepOf(page, "send-email"))["uses"]).toBe(
        "order-intake.send-email-2@1.0.0",
      );
      expect(posted(platform.calls)).toEqual([
        API,
        "order-intake.send-email-2@1.0.0",
        "workflows",
      ]);
    });

    test("Simulate publishes the step actions first and opens the simulation setup, without publishing the workflow", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page);
      await command(page, "Simulate");
      const dialog = page.getByRole("dialog", {
        name: "Publish actions to simulate order-intake?",
      });
      await expect(dialog.locator(".owned-state")).toHaveText([
        "Ready",
        "Ready",
      ]);
      await dialog
        .getByRole("button", { name: "Publish 2 actions and simulate" })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Simulate this workflow" }),
      ).toBeVisible();
      expect(posted(platform.calls)).toEqual([API, EMAIL]);
    });

    test("without permission to publish, Simulate says why and sends nothing", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page, {
        capabilities: [
          ...allCapabilities.filter(
            (capability) => capability !== "definition.publish",
          ),
          "compile",
        ],
      });
      await command(page, "Simulate");
      await expect(
        page.getByText(
          "Simulating this workflow publishes the actions of its Call an API, email and file steps first, and your account can't publish actions in this workspace.",
        ),
      ).toBeVisible();
      expect(platform.calls).toEqual([]);
    });

    test("an action that can't be built yet stops before anything is published", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page, { url: "" });
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      const row = dialog.locator(".owned-row", {
        hasText: "order-intake.call-api",
      });
      await expect(row.locator(".owned-state")).toHaveText("Can't publish");
      await expect(row).toContainText(
        "Fill in the URL of step call-api first.",
      );
      await expect(dialog.getByRole("status")).toHaveText(
        "1 of 2 actions can't be published yet. Nothing was published.",
      );
      await dialog
        .locator(".dialog-footer")
        .getByRole("button", { name: "Close" })
        .click();
      await expect(dialog).toHaveCount(0);
      await expect(
        page.getByText(
          "1 of 2 actions can't be published yet. Nothing was published.",
        ),
      ).toBeVisible();
      expect(platform.calls).toEqual([]);
    });

    test("the publish dialog lines up its rows and fits the window", async ({
      page,
    }) => {
      await withOwnedSteps(page);
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await expect(dialog.locator(".owned-state")).toHaveText([
        "Ready",
        "Ready",
      ]);
      await expectTidy(page, dialog, size.height);
    });

    test("while the actions publish, focus stays in the dialog, and Undo, Escape and Tab change nothing", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page, { hold: true });
      // One more edit after the steps: the summary's value.
      const document = parse(await sourceText(page));
      const summary = (document.spec.steps as Record<string, unknown>[]).find(
        (step) => step["id"] === "summary",
      )!;
      summary["value"] = { literal: { done: false, edited: "last" } };
      await applySource(page, stringify(document));
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      const primary = dialog.locator(".dialog-footer .primary");
      await expect(primary).toHaveText("Publish 2 actions and the workflow");
      await expect(primary).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(
        dialog.locator(".modal-header").getByRole("button", { name: "Close" }),
      ).toBeFocused();
      await page.keyboard.press("Shift+Tab");
      await expect(primary).toBeFocused();
      await primary.click();
      await expect(dialog.getByRole("status")).toHaveText("Publishing 1 of 2…");
      await expect(primary).toBeFocused();
      await expect(primary).toHaveAttribute("aria-disabled", "true");
      const focusInDialog = () =>
        page.evaluate(() => !!document.activeElement?.closest(".modal-panel"));
      for (const key of ["Control+z", "Escape", "Tab", "Shift+Tab", "Tab"]) {
        await page.keyboard.press(key);
        expect(await focusInDialog(), `focus after ${key}`).toBe(true);
      }
      await expect(dialog).toBeVisible();
      // Even with focus lost, the shell leaves its keys alone while the dialog is open.
      await page.evaluate(() =>
        (document.activeElement as HTMLElement | null)?.blur(),
      );
      await page.keyboard.press("Control+z");
      await page.keyboard.press("Escape");
      await expect(dialog).toBeVisible();
      platform.release();
      await expect(dialog).toHaveCount(0);
      await workflowPublished(page);
      expect(posted(platform.calls)).toEqual([API, EMAIL, "workflows"]);
      expect(platform.calls.at(-1)!.source).toContain("edited: last");
      expect(await sourceText(page)).toContain("edited: last");
    });

    test("while a suggested name is checked, the dialog can't be left or started again", async ({
      page,
    }) => {
      const send = (
        builtinActions() as {
          actions: {
            connector: string;
            action: string;
            document: Definition;
          }[];
        }
      ).actions.find(
        (entry) =>
          entry.connector === "weave-email@1.0.0" && entry.action === "send",
      )!.document;
      const taken = {
        ...structuredClone(send),
        metadata: { name: "order-intake.send-email", version: "1.0.0" },
      };
      (taken["spec"] as Record<string, unknown>)["timeoutSeconds"] = 10;
      const { platform } = await withOwnedSteps(page, { taken: [taken] });
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await expect(
        dialog
          .locator(".owned-row", { hasText: "order-intake.send-email" })
          .locator(".owned-state"),
      ).toHaveText("Taken");
      // The check of the suggested name waits until the test lets it answer.
      let answer = () => undefined as void;
      const held = new Promise<void>((done) => (answer = done));
      await page.route(`${project}/compiler/compile`, async (route) => {
        await held;
        await route.fallback();
      });
      const primary = dialog.locator(".dialog-footer .primary");
      await primary.click();
      await expect(dialog.getByRole("status")).toHaveText(
        "Checking the suggested names…",
      );
      await expect(primary).toBeFocused();
      await expect(primary).toHaveAttribute("aria-disabled", "true");
      await expect(
        dialog.getByRole("button", { name: "Keep editing" }),
      ).toHaveCount(0);
      await page.keyboard.press("Escape");
      // A second press of the main button starts nothing.
      const at = await box(primary);
      await page.mouse.click(at.x + at.width / 2, at.y + at.height / 2);
      await expect(dialog).toBeVisible();
      answer();
      await expect(dialog).toHaveCount(0);
      expect(posted(platform.calls)).toEqual([
        API,
        "order-intake.send-email-2@1.0.0",
        "workflows",
      ]);
    });

    test("Escape keeps editing, and nothing is published", async ({ page }) => {
      const { platform } = await withOwnedSteps(page);
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await expect(
        dialog.getByRole("button", {
          name: "Publish 2 actions and the workflow",
        }),
      ).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      expect(platform.calls).toEqual([]);
    });

    test("a published action changed in Source starts its next version when publishing", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page);
      await command(page, "Publish…");
      await publishDialog(page)
        .getByRole("button", { name: "Publish 2 actions and the workflow" })
        .click();
      await expect(publishDialog(page)).toHaveCount(0);
      await workflowPublished(page);
      const document = parse(await sourceText(page));
      const api = (document.spec.steps as Record<string, unknown>[]).find(
        (step) => step["id"] === "call-api",
      )!;
      api["with"] = {
        object: { headers: { object: { "X-Trace": { literal: "1" } } } },
      };
      await applySource(page, stringify(document));
      await command(page, "Publish…");
      await expect(page.locator(".toast-region")).toContainText(
        "order-intake.call-api 1.0.0 is published, so this change starts version 1.0.1.",
      );
      const dialog = publishDialog(page);
      await expect(dialog.locator(".owned-row")).toHaveCount(1);
      await expect(dialog.locator(".owned-row")).toContainText(
        "order-intake.call-api · 1.0.1",
      );
      await expect(dialog.locator(".owned-note")).toHaveText(
        "Changed after version 1.0.0 was published.",
      );
      await dialog
        .getByRole("button", { name: "Publish 1 action and the workflow" })
        .click();
      await expect(dialog).toHaveCount(0);
      expect((await stepOf(page, "call-api"))["uses"]).toBe(
        "order-intake.call-api@1.0.1",
      );
      expect(posted(platform.calls)).toEqual([
        API,
        EMAIL,
        "workflows",
        "order-intake.call-api@1.0.1",
        "workflows",
      ]);
    });
  });

// The dialog's design at the narrowest supported phone width and a short
// landscape window, ready to confirm and after a stop partway.
for (const size of [
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
])
  test.describe(`the publish dialog at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("lines up its rows and fits the window, ready and after a stop partway", async ({
      page,
    }) => {
      await withOwnedSteps(page, { failOnce: "order-intake.send-email" });
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await expect(dialog.locator(".owned-state")).toHaveText([
        "Ready",
        "Ready",
      ]);
      await expectTidy(page, dialog, size.height);
      await dialog
        .getByRole("button", { name: "Publish 2 actions and the workflow" })
        .click();
      await expect(dialog.locator(".owned-state")).toHaveText([
        "Published",
        "Not confirmed",
      ]);
      await expect(
        dialog.getByRole("button", { name: "Try again" }),
      ).toBeFocused();
      await expectTidy(page, dialog, size.height);
    });
  });
