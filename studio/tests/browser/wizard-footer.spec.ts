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
// The connection wizard's Back/Continue bar stays on the bottom edge of the
// window while its step scrolls, with a solid background and nothing showing
// below or through it.
import { test, expect, Page } from "@playwright/test";
import { acmeDiscovery, platformHost } from "./platform-host";

async function reviewStep(page: Page) {
  await platformHost(page, {
    discover: { "weave.acme.example": { json: acmeDiscovery } },
  });
  await page
    .locator("weave-home-dashboard")
    .getByRole("button", { name: "Connect to a platform" })
    .click();
  await page.getByLabel("Server address").fill("weave.acme.example");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.locator("#wizard-heading")).toHaveText("Review and trust");
  await page.getByText("Advanced", { exact: true }).click();
}
/** Where the footer sits, and what a pointer finds just inside its corners. */
const footerState = (page: Page) =>
  page.locator(".wizard-footer").evaluate((footer) => {
    const box = footer.getBoundingClientRect();
    const inside = (x: number, y: number) => {
      const hit = document.elementFromPoint(x, y);
      return !!hit && footer.contains(hit);
    };
    return {
      bottom: box.bottom,
      viewport: window.innerHeight,
      background: getComputedStyle(footer).backgroundColor,
      corners:
        inside(box.left + 2, box.bottom - 2) &&
        inside(box.right - 2, box.bottom - 2),
    };
  });

for (const viewport of [
  { width: 600, height: 500 },
  { width: 360, height: 640 },
])
  test(`the wizard footer stays on the bottom edge at ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await reviewStep(page);
    const scroller = page.locator(".connect-page");
    const overflow = await scroller.evaluate(
      (element) => element.scrollHeight - element.clientHeight,
    );
    expect(overflow).toBeGreaterThan(40);
    for (const position of [0, Math.floor(overflow / 2), overflow]) {
      await scroller.evaluate(
        (element, top) => (element.scrollTop = top),
        position,
      );
      const state = await footerState(page);
      // At the end of the step the footer is the card's bottom edge (inside
      // its 1 px border, with the card's rounded corners).
      expect(
        state.viewport - state.bottom,
        `scrolled ${position}`,
      ).toBeLessThanOrEqual(1);
      expect(state.background).toBe("rgb(255, 255, 255)");
      if (position < overflow)
        expect(state.corners, `corners at ${position}`).toBe(true);
    }
    await scroller.evaluate(
      (element, top) => (element.scrollTop = top),
      Math.floor(overflow / 2),
    );
    await page.screenshot({
      path: `test-results/ui1/wizard-footer-${viewport.width}x${viewport.height}.png`,
    });
    await expect(
      page.getByRole("button", { name: "Continue" }),
    ).toBeInViewport();
  });

test("the wizard footer still closes the card when everything fits", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await reviewStep(page);
  const footer = await page.locator(".wizard-footer").boundingBox();
  const card = await page.locator(".wizard").boundingBox();
  expect(
    Math.abs(footer!.y + footer!.height - (card!.y + card!.height)),
  ).toBeLessThan(2);
});
