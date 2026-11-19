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
import { connected, sourceText } from "./support";
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

/** order-intake, connected to a project whose catalog lock is the corpus's. */
async function open(page: Page) {
  const asked: string[] = [];
  const details = await openStepFixture(
    page,
    yamlFile("order-intake.yaml", workflow),
    async (p) => {
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
    const { details, asked } = await open(page);
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
    await list.getByRole("option", { name: "notify-customer 1.0.0" }).click();
    await expect(tile(page, "call-workflow-1")).toContainText(
      "notify-customer 1.0.0",
    );
    await expect(details.field("workflow")).toContainText(
      "In this project · callable by order-intake",
    );
    // The project's lock is read once for the window.
    expect(asked).toHaveLength(1);
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
    await expect(tile(page, "call-workflow-1")).toContainText(
      "notify-customer 1.0.0 · Doesn't wait",
    );
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
