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
// J9: what each person can use in Studio. The builder can publish and activate
// the demo workflow; the viewer, in a separate browser context, cannot: the
// primary command is disabled with its reason, and the menu names why Publish…
// can't run. A blocked menu item is checked by its reason only, whether or not
// it is also disabled. The server refuses the same commands regardless:
// tests/acceptance/test_j9_security.py sends them as the viewer.
import { expect, Locator, Page } from "@playwright/test";
import { openSidebar } from "../browser/real-platform-support";
import { journey, useJourneys } from "./harness";

useJourneys();

/**
 * Opens the first-run demo workflow, a published version, from Workflows. It
 * is found by name: J0 also publishes the child workflow notify-customer.
 */
async function openDemoWorkflow(page: Page): Promise<void> {
  await openSidebar(page, "Workflows");
  const row = page.locator(".resource-row").filter({ hasText: "first-run" });
  await expect(row).toContainText(/Published|Active/, { timeout: 30_000 });
  await row.locator(".row-open").click();
  await expect(page.locator(".shell.in-designer")).toBeVisible({
    timeout: 30_000,
  });
}

/** The designer's lifecycle commands: the primary button and the More menu. */
function lifecycle(page: Page): { primary: Locator; more: Locator } {
  const group = page.getByRole("group", { name: "Save and run" });
  return {
    primary: group.locator("button.primary"),
    more: group.getByRole("button", { name: "More", exact: true }),
  };
}

/** The reason a menu item shows under its label; empty when it can run. */
function reason(item: Locator): Locator {
  return item.locator("small");
}

/** The Publish… item of the open More menu. */
async function publishItem(page: Page): Promise<Locator> {
  const { more } = lifecycle(page);
  await more.click();
  const item = page.getByRole("menuitem", { name: /^Publish…/ });
  await expect(item).toBeVisible();
  return item;
}

journey("J9", "security and negative paths", async (j) => {
  await j.step("J9.2", async () => {
    const builder = await j.signIn("builder");
    await openDemoWorkflow(builder);
    const own = lifecycle(builder);
    await expect(own.primary).toHaveText("Activate…");
    await expect(own.primary).toBeEnabled();
    const publish = await publishItem(builder);
    await expect(reason(publish)).toHaveCount(0);
    await j.shot(builder, "builder-can-publish");
    await builder.keyboard.press("Escape");

    const viewer = await j.signIn("viewer");
    await openDemoWorkflow(viewer);
    const theirs = lifecycle(viewer);
    await expect(theirs.primary).toHaveText("Activate…");
    await expect(theirs.primary).toBeDisabled();
    await expect(viewer.locator("#primary-blocked")).toHaveText(
      "Your account can't activate versions in this workspace.",
    );
    await expect(reason(await publishItem(viewer))).toHaveText(
      "Your account can't publish workflows in this workspace.",
    );
    await j.shot(viewer, "viewer-cannot-publish");
    await viewer.keyboard.press("Escape");
  });
});
