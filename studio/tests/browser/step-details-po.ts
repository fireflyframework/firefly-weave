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
// Page object for step details. It drives Studio the way a person does:
// real clicks, double-clicks and keys, never `force`.
import { expect, type Locator, type Page } from "@playwright/test";
import { resolve } from "node:path";
import { connected, offline } from "./support";

export const stepFixture = resolve("tests/fixtures/step-details.yaml");
/** Each journey runs on a desktop and at the smallest supported editor size. */
export const sizes = [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
];

/**
 * The new editor, as everyone gets it by default: removes any stored choice
 * before every page this test opens, so the spec proves the default.
 */
export async function withNewEditor(page: Page) {
  await page.addInitScript(() =>
    localStorage.removeItem("ui:weave.editorNext"),
  );
}

/** A workflow file on disk, or its text. */
export type WorkflowFile =
  | string
  | { name: string; mimeType: string; buffer: Buffer };
/** Workflow text as a file to open. */
export const yamlFile = (name: string, text: string): WorkflowFile => ({
  name,
  mimeType: "application/yaml",
  buffer: Buffer.from(text),
});

/** Opens a workflow file with the new editor on, showing its canvas. */
export async function openStepFixture(
  page: Page,
  file: WorkflowFile = stepFixture,
  start: (page: Page) => Promise<unknown> = offline,
): Promise<StepDetailsPage> {
  await withNewEditor(page);
  await start(page);
  await page.getByLabel("Choose a workflow file").setInputFiles(file);
  await expect(page.locator(".editor-bar")).toBeVisible();
  const canvas = page.getByLabel("Workflow canvas", { exact: true });
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  await expect(canvas.or(show).first()).toBeVisible();
  if (await show.isVisible()) await show.click();
  await expect(canvas).toBeVisible();
  return new StepDetailsPage(page);
}

/**
 * The "+" at the start of the workflow, on the designer or on the new canvas
 * ("Insert a step before" its first step once it has steps).
 */
export const START_PLUS =
  /^(?:Add (?:a step here, at the start|a step at the start|your first step|first step)|Insert a step before \S+)$/;

/**
 * Shows the whole workflow: the designer's Fit all or the new canvas's Fit
 * view, from its View menu where the toolbar is too narrow for the button.
 */
export async function fitCanvas(page: Page) {
  const fit = page
    .getByRole("button", { name: /^Fit (?:all|view)\b/ })
    .filter({ visible: true });
  if (await fit.count()) return fit.first().click();
  const view = page
    .getByRole("button", { name: "Canvas view" })
    .filter({ visible: true });
  if (!(await view.count())) return;
  await view.first().click();
  await page.getByRole("menuitem", { name: "Fit view" }).click();
}

/**
 * Chooses a step (Call an API, Send email…) in Add a step, opened from a "+":
 * the one at the start of the workflow unless another is named. Several file
 * servers offer the file steps: `app` names the one to use (SFTP, Google
 * Drive…); without it, the first found.
 */
export async function addCommonStep(
  page: Page,
  label: string,
  plus: string | RegExp = START_PLUS,
  app?: string,
) {
  await fitCanvas(page);
  await page
    .getByRole("button", {
      name: plus,
      exact: typeof plus === "string",
    })
    .filter({ visible: true })
    .first()
    .click();
  const panel = page.locator(".add-step-panel");
  // The panel focuses its search once it is drawn and bound.
  await expect(panel.getByRole("combobox")).toBeFocused();
  await panel.getByRole("combobox").fill(label);
  const option = panel.getByRole("option", { name: label, exact: true });
  await (
    app
      ? option.filter({
          has: page.locator(".entry-description", {
            hasText: new RegExp(`^${app} · `),
          }),
        })
      : option
  )
    .first()
    .click();
}

/** Makes the platform report these language features (templates need text.concat). */
export async function withLanguageFeatures(
  page: Page,
  features = ["text.concat", "text.join"],
) {
  await page.route(
    /\/studio\/(?:contracts\/language|api\/api\/v1\/tenants\/[^/]+\/projects\/[^/]+\/language)$/,
    (route) => route.fulfill({ json: { features } }),
  );
}

/** The editor bar's gear that opens workflow settings (with the new editor on). */
export const settingsGear = (page: Page): Locator =>
  page
    .locator(".editor-identity")
    .getByRole("button", { name: /workflow settings$/i });

export class StepDetailsPage {
  constructor(readonly page: Page) {}
  get dialog(): Locator {
    return this.page.getByRole("dialog", { name: /^Step details: / });
  }
  dialogFor(name: string): Locator {
    return this.page.getByRole("dialog", {
      name: `Step details: ${name}`,
      exact: true,
    });
  }
  /** A step's card: the designer's node, or the new canvas's tile once it is there. */
  node(id: string): Locator {
    return this.page.locator(
      `[data-step="${id}"] .node-body, .tile-node[data-tile="${id}"] .tile-body`,
    );
  }
  /** The workflow's start: the designer's Start, or the new canvas's trigger tile. */
  trigger(): Locator {
    return this.page
      .locator(
        'button[aria-label="Start — workflow settings"], .tile-node[data-tile^="$trigger"] .tile-body',
      )
      .first();
  }
  /** A field of the visible tab by its stable ID. */
  field(id: string): Locator {
    return this.dialog
      .locator(`.sd-tabpanel:not([hidden]) [data-param="${id}"]`)
      .first();
  }
  /** The top-level fields of the visible tab. */
  topFields(): Locator {
    return this.dialog.locator(
      ".sd-tabpanel:not([hidden]) .param-form > [data-param]",
    );
  }
  /** A tab of the Parameters pane (not the panes' own tabs below 768 px). */
  tab(name: "Parameters" | "Settings"): Locator {
    return this.dialog
      .getByRole("tablist", { name: "Step details sections" })
      .getByRole("tab", { name: new RegExp(`^${name}`) });
  }
  async open(id: string) {
    const node = this.node(id);
    await node.scrollIntoViewIfNeeded();
    await node.dblclick();
    await expect(this.dialogFor(id)).toBeVisible();
  }
  async openWithKeyboard(id: string) {
    await this.node(id).focus();
    await this.page.keyboard.press("Enter");
    await expect(this.dialogFor(id)).toBeVisible();
  }
  async close() {
    await this.dialog
      .getByRole("button", { name: "Close", exact: true })
      .click();
    await expect(this.dialog).toHaveCount(0);
  }
}

/** Open the step fixture with the platform catalog available. */
export const openConnectedFixture = (
  page: Page,
  file: WorkflowFile = stepFixture,
) =>
  openStepFixture(page, file, async (p) => {
    const result = await connected(p);
    await withLanguageFeatures(p);
    return result;
  });
