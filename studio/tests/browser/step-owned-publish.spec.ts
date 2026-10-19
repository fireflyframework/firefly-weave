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
  stopLocalAuthoring,
} from "./local-authoring";
import {
  addOwnedSteps,
  applySource,
  ownedPlatform,
  ownedWorkflow,
  publishDialog,
  withOwnedSteps,
  type Definition,
  type PlatformCall,
} from "./owned-actions";
import { openStepFixture, sizes, yamlFile } from "./step-details-po";
import {
  box,
  boxes,
  clippedText,
  density,
  expectAligned,
  intersects,
  showParameters,
} from "./step-geometry";
import {
  allCapabilities,
  canUndo,
  command,
  offline,
  sourceText,
  tokenColor,
} from "./support";

test.skip(!hasPython, "Needs the repository's Python environment.");
test.afterAll(() => stopLocalAuthoring());

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const API = "order-intake.call-api@1.0.0";
const EMAIL = "order-intake.send-email@1.0.0";
const stepOf = async (page: Page, id: string) =>
  (parse(await sourceText(page)).spec.steps as Record<string, unknown>[]).find(
    (step) => step["id"] === id,
  )!;

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
  // The footer's buttons: in a row on the right edge, or stacked full width
  // on a phone, the main one last.
  const footer = await boxes(dialog.locator(".dialog-footer button"));
  expectAligned(
    [...footer.map((button) => button.height), rhythm.control],
    "footer button heights",
  );
  if ((page.viewportSize()?.width ?? 1440) <= 480) {
    expectAligned(
      [list.x, ...footer.map((button) => button.x)],
      "stacked footer left edge",
    );
    expectAligned(
      [list.right, ...footer.map((button) => button.right)],
      "stacked footer right edge",
    );
    for (let i = 1; i < footer.length; i++)
      expectAligned(
        [footer[i].y - footer[i - 1].bottom, 8],
        "stacked footer gap",
      );
  } else
    expectAligned(
      footer.map((button) => button.middle),
      "footer buttons on one row",
    );
  expect(footer.at(-1)).toEqual(primary);
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
/** Send email's action as someone else published it, with other content. */
function takenEmail(): Definition {
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
  return taken;
}
/** Whether a compile request checks an action (not the workflow). */
function checksAction(body: { source?: unknown }): boolean {
  const source = body.source;
  return typeof source === "string"
    ? /^kind: Action$/m.test(source)
    : (source as { kind?: string } | null)?.kind === "Action";
}
const posted = (calls: PlatformCall[]) =>
  calls.map((call) =>
    call.path === "actions" ? `${call.name}@${call.version}` : call.path,
  );

for (const size of sizes)
  test.describe(`publishing a workflow's own actions at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Publish… publishes the step actions first, then the workflow, and Save to file keeps their settings in the canvas file", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page);
      const saved: string[] = [];
      page.on("download", (download) =>
        saved.push(download.suggestedFilename()),
      );
      await command(page, "Save to file");
      await expect
        .poll(() => [...saved].sort())
        .toEqual(["order-intake.canvas.json", "order-intake.yaml"]);
      await expect(page.locator(".toast-region")).not.toContainText(
        "don't keep the settings",
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
      const { platform } = await withOwnedSteps(page, {
        taken: [takenEmail()],
      });
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

    test("a taken action's new name is announced", async ({ page }) => {
      await withOwnedSteps(page, { taken: [takenEmail()] });
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await expect(
        dialog
          .locator(".owned-row", { hasText: "order-intake.send-email" })
          .locator(".owned-state"),
      ).toHaveText("Taken");
      await expect(dialog.locator('.sr-only[aria-live="polite"]')).toHaveText(
        "Version 1.0.0 of order-intake.send-email is already published with different content. Studio publishes this one as order-intake.send-email-2 1.0.0.",
      );
    });

    test("closing the dialog while the actions are checked sends nothing more", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page);
      let checks = 0;
      let release = () => undefined as void;
      const held = new Promise<void>((done) => (release = done));
      await page.route(`${project}/compiler/compile`, async (route) => {
        if (checksAction(route.request().postDataJSON())) {
          checks++;
          await held;
        }
        await route.fallback();
      });
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await expect(dialog.locator(".dialog-footer .primary")).toHaveText(
        "Checking…",
      );
      await expect.poll(() => checks).toBe(1);
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      const answered = page.waitForResponse(/\/compiler\/compile$/);
      release();
      await answered;
      // The second action would be checked right after: nothing goes out.
      await page.waitForTimeout(1000);
      expect(checks).toBe(1);
      expect(platform.calls).toEqual([]);
    });

    test("the designer going away while a suggested name is checked sends nothing", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page, {
        taken: [takenEmail()],
      });
      await command(page, "Publish…");
      const dialog = publishDialog(page);
      await expect(
        dialog
          .locator(".owned-row", { hasText: "order-intake.send-email" })
          .locator(".owned-state"),
      ).toHaveText("Taken");
      let release = () => undefined as void;
      const held = new Promise<void>((done) => (release = done));
      await page.route(`${project}/compiler/compile`, async (route) => {
        await held;
        await route.fallback();
      });
      await dialog.locator(".dialog-footer .primary").click();
      await expect(dialog.getByRole("status")).toHaveText(
        "Checking the suggested names…",
      );
      // The browser's Back leaves the designer, and the dialog with it.
      await page.evaluate(() => history.back());
      await expect(page.locator(".editor-bar")).toHaveCount(0);
      await expect(dialog).toHaveCount(0);
      const answered = page.waitForResponse(/\/compiler\/compile$/);
      release();
      await answered;
      // Publishing would go on right after the check: nothing goes out.
      await page.waitForTimeout(1000);
      expect(platform.calls).toEqual([]);
    });

    test("while a suggested name is checked, the dialog can't be left or started again", async ({
      page,
    }) => {
      const { platform } = await withOwnedSteps(page, {
        taken: [takenEmail()],
      });
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

    test("the version toast clears step details' sticky header and the focused control", async ({
      page,
    }) => {
      const { details } = await withOwnedSteps(page);
      await command(page, "Publish…");
      await publishDialog(page)
        .getByRole("button", { name: "Publish 2 actions and the workflow" })
        .click();
      await expect(publishDialog(page)).toHaveCount(0);
      await workflowPublished(page);
      await details.openWithKeyboard("call-api");
      await showParameters(page, details.dialog);
      await details
        .field("method")
        .getByRole("combobox", { name: "Method" })
        .click();
      await page.getByRole("option", { name: /^POST/ }).click();
      const toast = page.locator(".toast-region .toast");
      await expect(toast).toContainText(
        "order-intake.call-api 1.0.0 is published, so this change starts version 1.0.1.",
      );
      const shown = await box(toast);
      const bars = await boxes(
        details.dialog.locator(
          ".sd-panes > .sd-segments, .sd-panes > .sd-main > .sd-head",
        ),
      );
      expect(bars.length).toBeGreaterThan(0);
      for (const bar of bars) expect(intersects(shown, bar)).toBe(false);
      expect(intersects(shown, await box(page.locator(":focus")))).toBe(false);
      expect(shown.bottom).toBeLessThanOrEqual(size.height);
    });
  });

// A platform draft holds the workflow only: the requests of its Call an API,
// email and file steps whose actions aren't published stay while it is open.
test.describe("saving a draft on the platform", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Save draft says the unpublished actions' settings stay only while it's open", async ({
    page,
  }) => {
    await withOwnedSteps(page);
    let saved: { document: Definition } | null = null;
    await page.route(`${project}/drafts/*`, (route) => {
      saved = route.request().postDataJSON() as { document: Definition };
      return route.fulfill({
        json: { id: "draft", revision: 1, document: saved.document },
      });
    });
    await command(page, "Save draft");
    await expect.poll(() => saved).not.toBeNull();
    await expect(page.locator(".toast-region")).toContainText(
      "Draft saved. This draft keeps the settings of 2 actions of this workflow's Call an API, email or file steps only while it's open. Publish the workflow to keep them in the project.",
    );
  });

  test("Save draft adds no note when every action is published", async ({
    page,
  }) => {
    await withOwnedSteps(page);
    await command(page, "Publish…");
    await publishDialog(page)
      .getByRole("button", { name: "Publish 2 actions and the workflow" })
      .click();
    await expect(publishDialog(page)).toHaveCount(0);
    await workflowPublished(page);
    await page.route(`${project}/drafts/*`, (route) =>
      route.fulfill({
        json: {
          id: "draft",
          revision: 1,
          document: (route.request().postDataJSON() as { document: unknown })
            .document,
        },
      }),
    );
    await command(page, "Save draft");
    const toast = page.locator(".toast-region");
    await expect(toast).toContainText("Draft saved.");
    await expect(toast).not.toContainText("only while it's open");
  });
});

// Publish… and Simulate load the code that publishes the workflow's own
// actions on first use; when it can't load, they stop and say so.
test.describe("when the publishing code can't load", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  for (const [name, purpose] of [
    ["Publish…", "Publish…"],
    ["Simulate", "Simulate"],
  ])
    test(`${name} stops and says so`, async ({ page }) => {
      const { platform } = await withOwnedSteps(page);
      let refused = 0;
      await page.route(/\.m?js(\?.*)?$/, async (route) => {
        const response = await route.fetch();
        const body = await response.text();
        if (body.includes("Turn on Try the new editor in Settings")) {
          refused++;
          return route.abort();
        }
        return route.fulfill({ response, body });
      });
      await command(page, name);
      await expect(page.locator("#main")).toContainText(
        `Studio couldn't load what ${purpose} needs. Check that Weave Studio is still running, reload the page and try again.`,
      );
      expect(refused).toBeGreaterThan(0);
      await expect(publishDialog(page)).toHaveCount(0);
      expect(platform.calls).toEqual([]);
    });
});

// The publishing code may arrive after the click has been drawn: what it
// decides still shows at once, with nothing else on the page changing.
for (const size of [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
])
  test.describe(`when the publishing code loads late at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Simulate's refusal shows as soon as the code arrives", async ({
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
      let delayed = 0;
      await page.route(/\.m?js(\?.*)?$/, async (route) => {
        const response = await route.fetch();
        const body = await response.text();
        if (body.includes("Turn on Try the new editor in Settings")) {
          delayed++;
          await new Promise((done) => setTimeout(done, 1500));
        }
        return route.fulfill({ response, body });
      });
      await command(page, "Simulate");
      await expect(
        page.getByText(
          "Simulating this workflow publishes the actions of its Call an API, email and file steps first, and your account can't publish actions in this workspace.",
        ),
      ).toBeVisible({ timeout: 5000 });
      expect(delayed).toBeGreaterThan(0);
      expect(platform.calls).toEqual([]);
    });
  });

// Publishing records into the last undo step. A workflow opened again has
// none: the record then adds no undo step of its own, which would change
// nothing a person sees.
test.describe("a workflow kept on this computer, opened again once connected", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("publishing its actions adds no undo step", async ({ page }) => {
    const details = await openStepFixture(
      page,
      yamlFile("order-intake.yaml", ownedWorkflow),
      async (p) => {
        await localAuthoring(p);
        await p.route("**/studio/contracts/connector-actions", (route) =>
          route.fulfill({ json: builtinActions() }),
        );
        await offline(p);
      },
    );
    await addOwnedSteps(page, details);
    const designer = page.url();
    // Connected now; the workflow comes back from this computer.
    const platform = await ownedPlatform(page, {});
    await page.goto(designer);
    await expect(page.locator(".editor-bar")).toBeVisible();
    expect(await canUndo(page)).toBe(false);
    await command(page, "Publish…");
    await publishDialog(page)
      .getByRole("button", { name: "Publish 2 actions and the workflow" })
      .click();
    await expect(publishDialog(page)).toHaveCount(0);
    await workflowPublished(page);
    expect(posted(platform.calls)).toEqual([API, EMAIL, "workflows"]);
    expect(await canUndo(page)).toBe(false);
  });
});
