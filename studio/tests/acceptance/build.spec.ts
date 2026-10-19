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
// J1: build the reference workflow in Studio as the builder. J1.1 signs in
// through the real host and identity provider and checks the brand lockup at
// each size; the later steps wait for the editor and language capabilities
// (journeys.toml), and the change that enables them adds their bodies here.
import { expect } from "@playwright/test";
import { journey, useJourneys } from "./harness";

useJourneys();

journey("J1", "build the reference workflow in Studio", async (j) => {
  await j.step("J1.1", async () => {
    const page = await j.signIn("builder");
    const indicator = page.locator(".platform-indicator");
    await expect(indicator).toContainText(j.platformName("builder"));
    await expect(indicator).toContainText("Signed in as builder");
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    const wide = j.viewport === "1440x900";
    await expect(page.locator(".brand-lockup")).toBeVisible({ visible: wide });
    await expect(page.locator(".brand-mark")).toBeVisible({ visible: !wide });
    await expect(page.locator('img[src*="lumi" i]')).toHaveCount(0);
    await j.shot(page, "home");
  });
});
