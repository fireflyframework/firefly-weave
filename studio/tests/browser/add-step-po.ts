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
// Page object for the Add a step panel. It drives the panel the way a
// person does: real keys and clicks, never `force`.
import { expect, type Locator, type Page } from "@playwright/test";

/** The panel's search field, wherever the panel is open. */
export const addStepSearch = (page: Page): Locator =>
  page.locator(".add-step-panel").getByRole("combobox");

export class AddStepPanel {
  constructor(readonly page: Page) {}
  get root(): Locator {
    return this.page.locator(".add-step-panel");
  }
  get title(): Locator {
    return this.root.locator(".add-step-title");
  }
  get search(): Locator {
    return addStepSearch(this.page);
  }
  get list(): Locator {
    return this.root.getByRole("listbox");
  }
  /** Waits until the panel's slide-in has finished, so its box is where it rests. */
  async settled() {
    await expect(this.root).toBeVisible();
    await this.root.evaluate((element) =>
      Promise.all(
        element.getAnimations().map((animation) => animation.finished),
      ),
    );
  }
  option(label: string): Locator {
    return this.list.getByRole("option", { name: label, exact: true });
  }
  async labels(): Promise<string[]> {
    return this.list.locator(".entry-label").allTextContents();
  }
  /** Group captions as written (CSS shows them in capitals). */
  async headings(): Promise<string[]> {
    return (await this.list.locator(".add-step-group").allTextContents()).map(
      (text) => text.trim(),
    );
  }
  /**
   * The label of the option the search field's aria-activedescendant names.
   * The attribute and the option are read in one step: typing redraws the
   * list, so an ID read on its own can name an option that is already gone.
   */
  async activeLabel(): Promise<string> {
    return this.search.evaluate((search) => {
      const id = search.getAttribute("aria-activedescendant");
      const option = id ? document.getElementById(id) : null;
      return option?.querySelector(".entry-label")?.textContent ?? "";
    });
  }
  /** Finds an entry by its exact label through the search, and chooses it. */
  async choose(label: string) {
    // The panel focuses its search once it is drawn with its inputs; a fill
    // before that would land in a field that isn't bound yet.
    await expect(this.search).toBeFocused();
    await this.search.fill(label);
    await this.option(label).first().click();
    await expect(this.root).toHaveCount(0);
  }
}

/** What the target runs, for the panel's reasons: features, and kinds or fields Studio can't edit yet. */
export async function languageFeatures(
  page: Page,
  features: string[],
  pending: string[] = ["forEach", "callWorkflow", "callable"],
) {
  const manifest = {
    version: "weave/language-manifest-v1",
    language_version: "1",
    ir_versions: [],
    features,
    limits: {},
    operators: [],
    workflow_fields: [
      {
        name: "callable",
        feature: "flow.callWorkflow",
        studio: pending.includes("callable") ? "pending" : "ready",
      },
    ],
    step_kinds: ["forEach", "callWorkflow"].map((kind) => ({
      kind,
      feature: `flow.${kind}`,
      schema_ref: "",
      group: "flow",
      label: kind,
      blocks: [],
      scope_roots: [],
      studio: pending.includes(kind) ? "pending" : "ready",
    })),
  };
  await page.route(
    /\/studio\/(?:contracts\/language|api\/api\/v1\/tenants\/[^/]+\/projects\/[^/]+\/language)$/,
    (route) => route.fulfill({ json: manifest }),
  );
}
