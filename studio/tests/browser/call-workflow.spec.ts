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
// A step that calls another workflow: added from Add a step, its workflow
// chosen from the project's published versions (each name's versions under
// it, a version that doesn't allow this workflow disabled with the
// compiler's sentence), its input from the called workflow's input, and
// whether it waits. The platform is mocked; its catalog lock is the shared
// corpus the platform's own tests check.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { allCapabilities, connected, offline, sourceText } from "./support";
import {
  addCommonStep,
  openStepFixture,
  StepDetailsPage,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";

const corpus = JSON.parse(
  readFileSync(resolve("tests/fixtures/callable-corpus.json"), "utf8"),
) as { lists: { lock: unknown }[] };
const workflow = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.2.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: { customerId: { type: "string" } },
    },
    outputSchema: { type: "object" },
    steps: [{ id: "fetch", kind: "transform", value: { ref: "/input" } }],
    output: { literal: {} },
  },
});

/** A callable draft kept on this computer: lookup-customer 0.1.0. */
const draft = {
  entry: {
    id: "d1",
    name: "lookup-customer",
    version: "0.1.0",
    savedAt: "2026-11-18T10:00:00Z",
  },
  document: {
    source: stringify({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "lookup-customer", version: "0.1.0" },
      spec: {
        inputSchema: { type: "object" },
        outputSchema: { type: "object" },
        callable: {},
        steps: [],
        output: { literal: {} },
      },
    }),
    format: "yaml",
    layout: null,
    savedAt: "2026-11-18T10:00:00Z",
  },
};

/** order-intake, connected to a project whose catalog lock is the corpus's; with `drafts`, lookup-customer is kept on this computer. */
async function open(page: Page, options: { drafts?: boolean } = {}) {
  const asked: string[] = [];
  const details = await openStepFixture(
    page,
    yamlFile("order-intake.yaml", workflow),
    async (p) => {
      if (options.drafts)
        await p.addInitScript((kept) => {
          if (localStorage.getItem("local:weave.localDrafts.v1")) return;
          localStorage.setItem(
            "local:weave.localDrafts.v1",
            JSON.stringify([kept.entry]),
          );
          localStorage.setItem(
            `local:weave.localDraft.${kept.entry.id}`,
            JSON.stringify(kept.document),
          );
        }, draft);
      const result = await connected(p);
      await withLanguageFeatures(p, ["text.concat", "flow.callWorkflow"]);
      await p.route(/\/projects\/[^/]+\/catalog$/, (route) => {
        asked.push(route.request().url());
        return route.fulfill({ json: corpus.lists[0].lock });
      });
      return result;
    },
  );
  return { details, asked };
}
/** Adds Call a workflow after fetch and returns its step details. */
async function addCall(page: Page, details: StepDetailsPage) {
  await addCommonStep(page, "Call a workflow", "Add a step after fetch");
  await expect(details.dialogFor("call-workflow-1")).toBeVisible();
  return details.dialogFor("call-workflow-1");
}
const tile = (page: Page, id: string) =>
  page.locator(`.tile-node[data-tile="${id}"]`);

test.describe("calling a workflow from a step", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Add a step adds the call and opens it, with Execute workflow and no Execute step", async ({
    page,
  }) => {
    const { details } = await open(page);
    const dialog = await addCall(page, details);
    const primary = dialog
      .locator(".sd-actions")
      .getByRole("button", { name: "Execute workflow", exact: true });
    await expect(primary).toHaveAttribute(
      "title",
      "A call runs only when the whole workflow executes.",
    );
    await expect(
      dialog.getByRole("button", { name: "Execute step", exact: true }),
    ).toHaveCount(0);
    // A new call needs its workflow; the tile says so, and nothing opens yet.
    await expect(
      details.field("workflow").getByRole("button", { name: "Open workflow" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", {
        name: /^call-workflow-1, Call a workflow, Setup needed: choose a workflow to call,/,
      }),
    ).toBeVisible();
  });

  test("lists each workflow's versions under its name, the ones it can't call disabled with why", async ({
    page,
  }) => {
    const { details, asked } = await open(page, { drafts: true });
    await addCall(page, details);
    await details.field("workflow").getByRole("combobox").click();
    const list = page.getByRole("listbox", { name: "Workflow options" });
    await expect(list.locator(".choice-group").first()).toHaveText(
      "In this project",
    );
    await expect(
      list.locator(".choice-heading", { hasText: "notify-customer" }),
    ).toBeVisible();
    const refused = list.getByRole("option", { name: "notify-customer 1.9.0" });
    await expect(refused).toHaveAttribute("aria-disabled", "true");
    await expect(refused).toContainText(
      "This workflow version does not allow calls from order-intake; add order-intake to its callable.allowedCallers in a new version.",
    );
    // A version it can't call is in the disabled ink, its sentence lined up under it.
    const ink = await page.evaluate(() => {
      const probe = document.createElement("span");
      probe.style.color = "var(--disabled-ink)";
      document.body.append(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      return color;
    });
    await expect(refused.locator(".choice-line")).toHaveCSS("color", ink);
    const version = await refused.locator(".choice-line").boundingBox();
    const sentence = await refused.locator("small").boundingBox();
    expect(sentence!.x).toBe(version!.x);
    // A draft on this computer says so at the end of its row.
    const kept = list.getByRole("option", { name: "lookup-customer 0.1.0" });
    await expect(kept.locator(".choice-aside")).toHaveText("Draft");
    await list.getByRole("option", { name: "notify-customer 1.0.0" }).click();
    await expect(tile(page, "call-workflow-1")).toContainText(
      "notify-customer 1.0.0",
    );
    await expect(details.dialog.locator(".sd-subtitle")).toHaveText(
      "Call a workflow · notify-customer 1.0.0",
    );
    await expect(details.field("workflow")).toContainText(
      "In this project · callable by order-intake",
    );
    // Open workflow sits beside the select, centred on it.
    const select = await details
      .field("workflow")
      .getByRole("combobox")
      .boundingBox();
    const openButton = await details
      .field("workflow")
      .getByRole("button", { name: "Open workflow" })
      .boundingBox();
    expect(openButton!.x).toBeGreaterThan(select!.x + select!.width);
    expect(openButton!.y + openButton!.height / 2).toBeCloseTo(
      select!.y + select!.height / 2,
      0,
    );
    // The project's lock is read once for the window.
    expect(asked).toHaveLength(1);
  });

  test("on a phone, Open workflow follows the hint", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 780 });
    const { details } = await open(page);
    await addCall(page, details);
    await details.field("workflow").getByRole("combobox").click();
    await page
      .getByRole("listbox", { name: "Workflow options" })
      .getByRole("option", { name: "notify-customer 1.0.0" })
      .click();
    const field = details.field("workflow");
    const hint = await field
      .getByText("In this project · callable by order-intake")
      .boundingBox();
    const openButton = await field
      .getByRole("button", { name: "Open workflow" })
      .boundingBox();
    expect(openButton!.y).toBeGreaterThanOrEqual(hint!.y + hint!.height);
  });

  test("maps the called workflow's input, and a call that doesn't wait loses If it fails", async ({
    page,
  }) => {
    const { details } = await open(page);
    await addCall(page, details);
    await details.field("workflow").getByRole("combobox").click();
    await page
      .getByRole("listbox", { name: "Workflow options" })
      .getByRole("option", { name: "notify-customer 1.0.0" })
      .click();
    await expect(details.field("input")).toContainText(
      "From notify-customer's input",
    );
    await expect(details.field("input.customerId")).toBeVisible();
    const mode = details.field("mode").getByRole("radiogroup");
    await expect(
      mode.getByRole("radio", { name: "Wait for its result" }),
    ).toHaveAttribute("aria-checked", "true");
    await details
      .field("onFailure")
      .getByRole("radio", { name: "Continue with the result status" })
      .click();
    await mode.getByRole("radio", { name: "Start it and continue" }).click();
    await expect(details.field("onFailure")).toHaveCount(0);
    // The tile names the version, and says on a line of its own, in the info tone, that it doesn't wait.
    const called = tile(page, "call-workflow-1");
    await expect(called.locator(".tile-output")).toHaveText("Doesn't wait");
    await expect(called.locator(".tile-output")).toHaveAttribute(
      "data-tone",
      "info",
    );
    await expect(
      page.getByRole("button", {
        name: /^call-workflow-1, Call a workflow, notify-customer 1\.0\.0, doesn't wait,/,
      }),
    ).toBeVisible();
    await mode.getByRole("radio", { name: "Wait for its result" }).click();
    await expect(
      details
        .field("onFailure")
        .getByRole("radio", { name: "Continue with the result status" }),
    ).toHaveAttribute("aria-checked", "false");
  });

  test("a business key cleared again is removed, not kept empty", async ({
    page,
  }) => {
    const { details } = await open(page);
    const dialog = await addCall(page, details);
    await dialog.getByRole("button", { name: "Add option" }).click();
    await dialog
      .getByRole("menu", { name: "Add option" })
      .getByRole("menuitem", { name: /^Business key/ })
      .click();
    const key = details.field("businessKey").getByRole("textbox").first();
    await key.fill("A-1042");
    await key.press("Tab");
    await details.close();
    expect(await sourceText(page)).toContain("A-1042");
    await details.open("call-workflow-1");
    const again = details.field("businessKey").getByRole("textbox").first();
    await again.fill("");
    await again.press("Tab");
    await details.close();
    // The compiler refuses an empty key: clearing it removes the field.
    expect(await sourceText(page)).not.toContain("businessKey");
  });

  test("a call that doesn't wait can't be pinned", async ({ page }) => {
    const { details } = await open(page);
    await addCall(page, details);
    await details.field("workflow").getByRole("combobox").click();
    await page
      .getByRole("listbox", { name: "Workflow options" })
      .getByRole("option", { name: "notify-customer 1.0.0" })
      .click();
    await details
      .field("mode")
      .getByRole("radio", { name: "Start it and continue" })
      .click();
    await details.close();
    await tile(page, "call-workflow-1")
      .locator(".tile-body")
      .click({ button: "right" });
    const item = page.getByRole("menuitem", { name: /^Pin output/ });
    await expect(item).toHaveAttribute("aria-disabled", "true");
    await expect(item).toContainText("This step's output can't be pinned.");
  });
});

test.describe("when the project's workflows can't be listed", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  /** order-intake with lookup-customer kept on this computer, offline or connected without catalog read. */
  async function openWithout(page: Page, how: "offline" | "no catalog read") {
    const details = await openStepFixture(
      page,
      yamlFile("order-intake.yaml", workflow),
      async (p) => {
        await p.addInitScript((kept) => {
          if (localStorage.getItem("local:weave.localDrafts.v1")) return;
          localStorage.setItem(
            "local:weave.localDrafts.v1",
            JSON.stringify([kept.entry]),
          );
          localStorage.setItem(
            `local:weave.localDraft.${kept.entry.id}`,
            JSON.stringify(kept.document),
          );
        }, draft);
        const result =
          how === "offline"
            ? await offline(p)
            : await connected(p, {
                capabilities: allCapabilities.filter(
                  (capability) => capability !== "catalog.read",
                ),
              });
        await withLanguageFeatures(p, ["text.concat", "flow.callWorkflow"]);
        return result;
      },
    );
    return { details, dialog: await addCall(page, details) };
  }

  for (const [how, reason] of [
    ["offline", "Connect to a platform to call published workflows."],
    [
      "no catalog read",
      "You can't read this project's published workflows here.",
    ],
  ] as const)
    test(`${how}: the list starts with why, and the field keeps saying it`, async ({
      page,
    }) => {
      const { details } = await openWithout(page, how);
      const field = details.field("workflow");
      const line = field.locator(".param-line");
      await expect(line).toHaveText(reason);
      await field.getByRole("combobox").click();
      const list = page.getByRole("listbox", { name: "Workflow options" });
      // First, where "In this project" would be, and never a choice.
      const first = list.locator(":scope > *").first();
      await expect(first).toHaveText(reason);
      await expect(first).not.toHaveAttribute("role", "option");
      await expect(list.getByRole("option")).toHaveCount(1);
      await list.getByRole("option", { name: /^lookup-customer/ }).click();
      await expect(field.getByRole("combobox")).toHaveValue(
        /lookup-customer 0\.1\.0/,
      );
      await expect(line).toHaveText(reason);
    });
});
