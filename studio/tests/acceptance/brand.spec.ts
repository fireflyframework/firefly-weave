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
// J8: brand and naming in Studio against the real platform: the home link and
// the Weave AI entry point by name, no "Lumi" in the audited pages, Manrope and
// the dark color scheme. The harness audits every journey's requests (only the
// Studio host and the identity provider; no mascot artwork).
import { expect, journey, openSidebar, useJourneys } from "./harness";

useJourneys();

journey("J8", "brand and naming", async (j) => {
  await j.step("J8.1", async () => {
    const page = await j.signIn("builder");
    await expect(
      page.getByRole("link", { name: "Firefly Weave Studio home" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Ask Weave AI" }).first(),
    ).toBeVisible();
    for (const label of ["Home", "Workflows", "Settings"]) {
      await openSidebar(page, label);
      await expect(page.getByText(/\bLumi\b/)).toHaveCount(0);
      await j.shot(page, label.toLowerCase());
    }
    // read-only audit: the font and color scheme the page computed, and
    // whether a Manrope face actually loaded (check() is also true when the
    // page declares no Manrope at all)
    const computed = await page.evaluate(async () => {
      await document.fonts.ready;
      return {
        manrope: document.fonts.check("14px Manrope"),
        loaded: [...document.fonts].some(
          (face) =>
            face.family.replace(/["']/g, "") === "Manrope" &&
            face.status === "loaded",
        ),
        family: getComputedStyle(document.body).fontFamily,
        scheme: getComputedStyle(document.documentElement).colorScheme,
      };
    });
    expect(computed.manrope).toBe(true);
    expect(computed.loaded).toBe(true);
    expect(computed.family.replace(/["']/g, "")).toMatch(/^Manrope\b/);
    expect(computed.scheme).toBe("dark");
  });
});
