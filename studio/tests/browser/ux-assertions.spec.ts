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
import {
  expectInViewport,
  expectNoOverlap,
  expectNotClippedBy,
  tabStops,
} from "./ux-assertions";

test("geometry helpers catch clipping and overlap while respecting the top layer", async ({
  page,
}) => {
  await page.setContent(
    `<main style="width:100px;height:50px;overflow:hidden"><div id="clipped" style="height:100px;background:white">Content</div><div id="popup" popover style="width:200px;height:100px">Popup</div></main><button popovertarget="popup">Open</button>`,
  );
  await expect(
    expectNotClippedBy(page.locator("#clipped"), page.locator("main")),
  ).rejects.toThrow();
  await page.getByRole("button", { name: "Open" }).click();
  await expectInViewport(page.locator("#popup"), 8);
  await expectNotClippedBy(page.locator("#popup"), page.locator("main"));
  await expect(
    expectNoOverlap(page.locator("main, #clipped")),
  ).rejects.toThrow();
  await page.keyboard.press("Escape");
  await expectNoOverlap(page.locator("main, button"));
});

test("keyboard audit counts only reachable stops", async ({ page }) => {
  await page.setContent(
    `<section><button>One</button><button disabled>Disabled</button><button tabindex="-1">Roving inactive</button><input aria-label="Two"><button>Three</button></section><button>Outside</button>`,
  );
  await page.getByRole("button", { name: "One", exact: true }).focus();
  expect(await tabStops(page, page.locator("section"))).toBe(3);
  await expect(page.getByRole("button", { name: "Outside" })).toBeFocused();
});
