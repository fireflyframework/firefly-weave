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
import { test, expect } from "@playwright/test";
import { connected, lookupAction, newWorkflow, insertStep } from "./support";
import { chooseAction } from "./integrations-po";

for (const viewport of [
  { width: 1280, height: 720 },
  { width: 600, height: 500 },
]) {
  test(`focused action inputs remain reachable after scrolling and tabbing backward at ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const action = structuredClone(lookupAction);
    action.spec.inputSchema = {
      type: "object",
      required: Array.from({ length: 12 }, (_, index) => `field${index + 1}`),
      properties: Object.fromEntries(
        Array.from({ length: 12 }, (_, index) => [
          `field${index + 1}`,
          { type: "string", title: `Field ${index + 1}` },
        ]),
      ),
    } as typeof action.spec.inputSchema;
    await connected(page, {
      catalog: [{ id: "long-action", document: action }],
    });
    await newWorkflow(page);
    await insertStep(page, "Call an action");
    if (viewport.width < 768)
      await page.locator('[data-step="call-action-1"] .node-body').click();
    await chooseAction(page, "sql.lookup@1.0.0");
    const last = page.getByRole("textbox", { name: "Field 12", exact: true });
    await last.click();
    const body = page.locator(".inspector-body");
    const initialScroll = await body.evaluate((element) => element.scrollTop);
    await last.hover();
    await page.mouse.wheel(0, -180);
    await expect
      .poll(() => body.evaluate((element) => element.scrollTop))
      .toBeLessThan(initialScroll);
    await expect(last).toBeFocused();
    let reachedFirst = false;
    for (let index = 0; index < 50; index++) {
      await page.keyboard.press("Shift+Tab");
      const focused = page.locator("input:focus");
      if (await focused.count()) {
        await expect
          .poll(() =>
            focused.evaluate((element) => {
              const box = element.getBoundingClientRect();
              const hit = document.elementFromPoint(
                box.left + box.width / 2,
                box.top + box.height / 2,
              );
              return hit === element || element.contains(hit)
                ? "self"
                : hit?.tagName + "." + hit?.className;
            }),
          )
          .toBe("self");
        if (
          await page
            .getByRole("textbox", { name: "Field 1", exact: true })
            .evaluate((element) => element === document.activeElement)
        ) {
          reachedFirst = true;
          break;
        }
      }
    }
    expect(reachedFirst).toBe(true);
  });
}
