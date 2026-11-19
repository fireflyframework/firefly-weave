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
// The Called by a workflow trigger: who may call this workflow
// (spec.callable), what callers send and read, the published workflows that
// call it (from the project's catalog lock, mocked here), and the question
// before it goes, from the canvas or from the workflow settings.
import { expect, test, type Page, type Route } from "@playwright/test";
import { parse, stringify } from "yaml";
import { connected, sourceText } from "./support";
import {
  addCommonStep,
  openStepFixture,
  settingsGear,
  StepDetailsPage,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";

/** notify-customer 1.0.0; `callable` null leaves it out. */
const workflow = (callable: unknown) =>
  stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "notify-customer", version: "1.0.0" },
    spec: {
      inputSchema: {
        type: "object",
        required: ["customerId"],
        properties: {
          customerId: { type: "string" },
          message: { type: "string" },
        },
      },
      outputSchema: {
        type: "object",
        properties: {
          messageId: { type: "string" },
          accepted: { type: "boolean" },
        },
      },
      ...(callable === null ? {} : { callable }),
      steps: [{ id: "send", kind: "transform", value: { ref: "/input" } }],
      output: { ref: "/steps/send/output" },
    },
  });
const ALLOWED = ["order-intake", "billing-close", "support-desk"];
const call = (id: string, mode = "wait") => ({
  id,
  kind: "callWorkflow",
  uses: "notify-customer@1.0.0",
  mode,
  with: { literal: {} },
});
const published = (name: string, version: string, steps: unknown[]) => ({
  document: {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name, version },
    spec: {
      inputSchema: { type: "object" },
      outputSchema: { type: "object" },
      steps,
      output: { literal: {} },
    },
  },
  digest: "0".repeat(64),
});
const LOCK = {
  definitions: [
    published("order-intake", "1.2.0", [call("notify")]),
    published("billing-close", "2.0.0", [call("notify-owner", "detach")]),
    published("support-desk", "1.1.0", [
      {
        id: "assistant",
        kind: "agent",
        tools: [{ name: "notify", workflow: "notify-customer@1.0.0" }],
      },
    ]),
    published("returns-desk", "1.0.0", [call("tell-customer")]),
  ],
  tasks: [],
  adapters: [],
  schemas: {},
};

/** notify-customer, connected to a project whose published workflows call it; `callable` null for none. */
async function open(
  page: Page,
  callable: unknown = { allowedCallers: ALLOWED },
  catalog: (route: Route) => Promise<void> = (route) =>
    route.fulfill({ json: LOCK }),
) {
  return openStepFixture(
    page,
    yamlFile("notify-customer.yaml", workflow(callable)),
    async (p) => {
      const result = await connected(p);
      await withLanguageFeatures(p, ["flow.callWorkflow"]);
      await p.route(/\/projects\/[^/]+\/catalog$/, catalog);
      // The published versions, for Open on a caller.
      await p.route(/\/projects\/[^/]+\/workflows\?/, (route) =>
        route.fulfill({
          json: {
            items: LOCK.definitions.map((locked, index) => ({
              id: `v-${index}`,
              name: locked.document.metadata.name,
              version: locked.document.metadata.version,
            })),
            next_cursor: null,
          },
        }),
      );
      await p.route(
        /\/projects\/[^/]+\/workflows\/v-(\d+)\/export$/,
        (route) => {
          const index = Number(
            /v-(\d+)\/export$/.exec(route.request().url())![1],
          );
          return route.fulfill({
            json: {
              id: `v-${index}`,
              document: LOCK.definitions[index].document,
            },
          });
        },
      );
      return result;
    },
  );
}
const calledTile = (page: Page) =>
  page.locator('.tile-node[data-tile^="$trigger"]').filter({
    hasText: "Called by a workflow",
  });
/** Opens the trigger's step details on Parameters. */
async function openTrigger(page: Page, details: StepDetailsPage) {
  await calledTile(page).locator(".tile-body").dblclick();
  await expect(details.dialog).toBeVisible();
  return details.dialog;
}
const callable = async (page: Page) =>
  (parse(await sourceText(page)) as { spec: { callable?: unknown } }).spec
    .callable;

test.describe("the Called by a workflow trigger", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Add step adds it now that Studio edits callable workflows", async ({
    page,
  }) => {
    const details = await open(page, null);
    await addCommonStep(page, "Called by a workflow", "Add a step after send");
    await expect(calledTile(page)).toBeAttached();
    // The new trigger opens in step details, on who may call it.
    await expect(
      details.dialog.getByRole("radio", {
        name: "Any workflow in this project",
      }),
    ).toBeChecked();
    await details.close();
    expect(await callable(page)).toEqual({});
  });

  test("shows who may call it, what callers send and read, and the published callers, the one not allowed last", async ({
    page,
  }) => {
    const details = await open(page);
    const dialog = await openTrigger(page, details);
    await expect(
      dialog.getByRole("radio", { name: "Only these workflows" }),
    ).toBeChecked();
    await expect(
      dialog
        .getByRole("list", { name: "Workflows that can call this one" })
        .locator(".cb-chip"),
    ).toHaveText(ALLOWED);
    const input = dialog.getByRole("group", { name: "Input", exact: true });
    await expect(input).toContainText("2 fields");
    await expect(input.locator("li").first()).toContainText(
      /customerId\s*Text\s*Required/,
    );
    await expect(
      dialog.getByRole("group", { name: "Result", exact: true }),
    ).toContainText("accepted");
    // The radio stays at its card's start with its words beside it, with the
    // schema designer open and after Done.
    const radioAtStart = async () => {
      for (const name of [
        "Any workflow in this project",
        "Only these workflows",
      ]) {
        const card = dialog
          .locator(".cb-card")
          .filter({ has: page.getByRole("radio", { name }) });
        const box = (await card.boundingBox())!;
        const radio = (await card.getByRole("radio").boundingBox())!;
        const words = (await card.locator(".cb-card-text").boundingBox())!;
        expect(radio.x - box.x).toBeLessThanOrEqual(16);
        expect(radio.width).toBeLessThanOrEqual(20);
        expect(words.x - (radio.x + radio.width)).toBeLessThanOrEqual(16);
      }
    };
    const edit = dialog.getByRole("button", { name: "Edit the input" });
    await edit.click();
    await expect(edit).toHaveAttribute("aria-expanded", "true");
    await radioAtStart();
    await edit.click();
    await expect(edit).toHaveText("Edit");
    await radioAtStart();
    const callers = dialog.locator(".cb-caller");
    await expect(dialog.locator(".cb-section").last()).toContainText(
      "4 published workflows",
    );
    await expect(callers.locator(".cb-caller-name")).toHaveText([
      "billing-close 2.0.0",
      "order-intake 1.2.0",
      "support-desk 1.1.0",
      "returns-desk 1.0.0",
    ]);
    await expect(callers.locator(".cb-wide")).toHaveText([
      "Step notify-owner · doesn't wait",
      "Step notify · waits for the result",
      "AI agent assistant · workflow tool",
      "Step tell-customer calls this workflow, but returns-desk isn't one of the workflows allowed. Its next activation will fail.",
    ]);
    // Allow it adds the caller in one step, and focus moves to its chip.
    await callers.last().getByRole("button", { name: "Allow it" }).click();
    await expect(
      dialog.getByRole("button", { name: "Remove returns-desk" }),
    ).toBeFocused();
    await expect(dialog.locator(".cb-caller.is-refused")).toHaveCount(0);
    await details.close();
    expect(await callable(page)).toEqual({
      allowedCallers: [...ALLOWED, "returns-desk"],
    });
  });

  test("Who can call it writes spec.callable: any workflow, or names added one by one", async ({
    page,
  }) => {
    const details = await open(page, {});
    const dialog = await openTrigger(page, details);
    // The tile's subtitle says what spec.callable allows while step details stay open.
    const subtitle = calledTile(page);
    const any = dialog.getByRole("radio", {
      name: "Any workflow in this project",
    });
    await expect(any).toBeChecked();
    await dialog.getByRole("radio", { name: "Only these workflows" }).check();
    const field = dialog.getByRole("combobox", { name: "Add a workflow" });
    await expect(field).toBeFocused();
    // Nothing is written until the first name: an empty list would be refused.
    await expect(subtitle).toContainText("Any workflow");
    await field.fill("-intake");
    await field.press("Enter");
    await expect(dialog.getByRole("alert")).toHaveText(
      "Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.",
    );
    await field.fill("order-intake");
    await field.press("Enter");
    await expect(subtitle).toContainText("Only 1 workflow");
    await field.fill("order-intake");
    await field.press("Enter");
    await expect(dialog.getByRole("alert")).toHaveText(
      "order-intake can already call this workflow.",
    );
    await field.fill("");
    await field.press("Backspace");
    const remove = dialog.getByRole("button", { name: "Remove order-intake" });
    await expect(remove).toBeFocused();
    // The last name stays: without it any workflow could call this one.
    await remove.click();
    await expect(dialog.getByRole("alert")).toHaveText(
      "Keep at least one workflow here, or choose Any workflow in this project.",
    );
    await details.close();
    expect(await callable(page)).toEqual({ allowedCallers: ["order-intake"] });
    await openTrigger(page, details);
    await any.check();
    // From a list of names, Any workflow asks first.
    await page
      .getByRole("dialog", {
        name: "Let any workflow in this project call notify-customer?",
      })
      .getByRole("button", { name: "Let any workflow call it", exact: true })
      .click();
    await expect(subtitle).toContainText("Any workflow");
    await details.close();
    expect(await callable(page)).toEqual({});
  });

  test("an arrow key from Only these workflows asks before any workflow may call, and the names come back", async ({
    page,
  }) => {
    const details = await open(page);
    const dialog = await openTrigger(page, details);
    const only = dialog.getByRole("radio", { name: "Only these workflows" });
    const chips = dialog.locator(".cb-chip");
    await only.focus();
    await page.keyboard.press("ArrowUp");
    const question = page.getByRole("dialog", {
      name: "Let any workflow in this project call notify-customer?",
    });
    await expect(
      question.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeFocused();
    await question.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(only).toBeChecked();
    await expect(only).toBeFocused();
    await expect(chips).toHaveText(ALLOWED);
    await expect(calledTile(page)).toContainText("Only 3 workflows");
    await only.focus();
    await page.keyboard.press("ArrowUp");
    await question
      .getByRole("button", { name: "Let any workflow call it", exact: true })
      .click();
    await expect(calledTile(page)).toContainText("Any workflow");
    await expect(chips).toHaveCount(0);
    // Only these workflows brings back the names it had in this session.
    await only.check();
    await expect(chips).toHaveText(ALLOWED);
    await expect(calledTile(page)).toContainText("Only 3 workflows");
    await details.close();
    expect(await callable(page)).toEqual({ allowedCallers: ALLOWED });
  });

  test("at 100 names, Allow it is off and says why", async ({ page }) => {
    const hundred = [
      ...ALLOWED,
      ...Array.from({ length: 97 }, (_, n) => `workflow-${n}`),
    ];
    const details = await open(page, { allowedCallers: hundred });
    const dialog = await openTrigger(page, details);
    const refused = dialog.locator(".cb-caller.is-refused");
    await expect(
      refused.getByRole("button", { name: "Allow it" }),
    ).toBeDisabled();
    await expect(refused).toContainText(
      "Up to 100 workflows can be named here.",
    );
  });

  test("removing it names the callers and their steps, starting on Cancel", async ({
    page,
  }) => {
    await open(page);
    await calledTile(page).locator(".tile-body").focus();
    await page.keyboard.press("Delete");
    const question = page.getByRole("dialog", {
      name: "Remove Called by a workflow?",
    });
    await expect(question).toContainText(
      "billing-close, order-intake, support-desk and returns-desk call this workflow. Their next activations will fail until they stop calling it.",
    );
    await expect(question).toContainText(
      "support-desk 1.1.0 · AI agent assistant",
    );
    await expect(question).toContainText(
      "Callable by other workflows in the workflow settings turns off too.",
    );
    await expect(
      question.getByRole("button", { name: "Cancel", exact: true }),
    ).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(question).toHaveCount(0);
    expect(await callable(page)).toEqual({ allowedCallers: ALLOWED });
    await calledTile(page).locator(".tile-body").focus();
    await page.keyboard.press("Delete");
    await question.getByRole("button", { name: "Remove", exact: true }).click();
    await expect(calledTile(page)).toHaveCount(0);
    expect(await callable(page)).toBeUndefined();
  });

  test("turning Callable off in the workflow settings asks the same, and removes the trigger with it", async ({
    page,
  }) => {
    await open(page);
    await settingsGear(page).click();
    const settings = page.getByRole("dialog", { name: "Workflow settings" });
    const callableSwitch = settings.getByRole("switch", {
      name: /^Callable by other workflows/,
    });
    await callableSwitch.click();
    const question = page.getByRole("dialog", {
      name: "Remove Called by a workflow?",
    });
    await expect(question).toContainText("billing-close, order-intake");
    await question.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(callableSwitch).toHaveAttribute("aria-checked", "true");
    await expect(calledTile(page)).toHaveCount(1);
    await callableSwitch.click();
    await question.getByRole("button", { name: "Remove", exact: true }).click();
    await expect(calledTile(page)).toHaveCount(0);
    await settings.getByRole("button", { name: "Close", exact: true }).click();
    expect(await callable(page)).toBeUndefined();
  });
});

test.describe("opening a caller", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Open on a caller opens its published version", async ({ page }) => {
    const details = await open(page);
    const dialog = await openTrigger(page, details);
    await dialog.getByRole("button", { name: "Open order-intake" }).click();
    await expect(
      page.getByText(
        "Opened order-intake 1.2.0 from this project. Your changes start a new draft.",
      ),
    ).toBeVisible();
    await expect(page.locator(".editor-identity")).toContainText(
      "order-intake",
    );
  });
});

test.describe("when the callers can't show", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("not connected, it says where callers come from", async ({ page }) => {
    const details = await openStepFixture(
      page,
      yamlFile("notify-customer.yaml", workflow({})),
    );
    const dialog = await openTrigger(page, details);
    await expect(dialog.locator(".cb-none")).toContainText(
      "Connect to a platform to see which workflows call this one.Studio reads the callers from the project's published workflows.",
    );
  });

  test("a failed read offers Try again, which reads the callers", async ({
    page,
  }) => {
    let answers = 0;
    const details = await open(page, { allowedCallers: ALLOWED }, (route) =>
      ++answers === 1
        ? route.fulfill({ status: 500, json: { title: "Server error" } })
        : route.fulfill({ json: LOCK }),
    );
    const dialog = await openTrigger(page, details);
    await expect(dialog.locator(".cb-none-title")).toHaveText(
      "Studio couldn't read this project's published workflows.",
    );
    await dialog.getByRole("button", { name: "Try again" }).click();
    await expect(dialog.locator(".cb-caller")).toHaveCount(4);
  });
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 360, height: 780 } });

  test("callers' lines are shorter", async ({ page }) => {
    const details = await open(page);
    await calledTile(page).locator(".tile-body").dblclick();
    await expect(details.dialog).toBeVisible();
    const tab = details.dialog.getByRole("tab", { name: "Parameters" });
    if (await tab.count()) await tab.first().click();
    const callers = details.dialog.locator(".cb-caller");
    await expect(callers.locator(".cb-narrow").first()).toHaveText(
      "Step notify-owner · doesn't wait",
    );
    await expect(callers.locator(".cb-narrow").last()).toHaveText(
      "Calls this workflow but isn't allowed. Its next activation will fail.",
    );
    await expect(callers.locator(".cb-wide").first()).toBeHidden();
    // Open is an icon named for its workflow, 44 px square.
    const openCaller = details.dialog.getByRole("button", {
      name: "Open order-intake",
    });
    await expect(openCaller.locator(".cb-open-text")).toBeHidden();
    const box = (await openCaller.boundingBox())!;
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
  });
});
