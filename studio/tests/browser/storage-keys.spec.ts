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
// After a journey that keeps a local draft, changes pane views, sets a test
// input and opens step details, every key in browser storage starts with
// local:, ui: or a platform's profile prefix; keys from before prefixes
// move once and old run inputs go.
import { expect, test } from "@playwright/test";
import { openStepFixture, withNewEditor } from "./step-details-po";
import { setTestInput } from "./formula-po";
import { offline } from "./support";

const PREFIXED = /^(local:|ui:|p[0-9a-f]{16}:)/;

test("every key Studio keeps on this computer is prefixed", async ({
  page,
}) => {
  const details = await openStepFixture(page);
  await setTestInput(page, details, {
    "Customer ID": "c-1042",
    Email: "ada@example.com",
    Amount: "1500",
  });
  await details.openWithKeyboard("lookup");
  await details.dialog.getByRole("radio", { name: "JSON" }).first().click();
  await details.close();
  const keys = await page.evaluate(() => Object.keys(localStorage));
  // The editor flag and the pane views at least; test data joins when step details keep it.
  expect(keys).toEqual(
    expect.arrayContaining(["ui:weave.editorNext", "ui:weave.ndv.views.v1"]),
  );
  for (const key of keys) expect(key, key).toMatch(PREFIXED);
});

test("keys from before prefixes move once, and old run inputs go", async ({
  page,
}) => {
  await page.addInitScript(() => {
    if (sessionStorage.getItem("seeded")) return;
    sessionStorage.setItem("seeded", "1");
    localStorage.setItem("weave.localDrafts.v1", "[]");
    localStorage.setItem("weave.studio.inspectorWidth", "420");
    localStorage.setItem("weave-studio-run-input:v-1", '{"amount":1}');
  });
  await withNewEditor(page);
  await offline(page);
  await expect(
    page.locator(".editor-bar, weave-home-dashboard").first(),
  ).toBeVisible();
  const keys = await page.evaluate(() => Object.keys(localStorage).sort());
  expect(keys).toContain("local:weave.localDrafts.v1");
  expect(keys).toContain("ui:weave.studio.inspectorWidth");
  expect(keys.some((key) => key.startsWith("weave-studio-run-input:"))).toBe(
    false,
  );
  expect(keys.some((key) => key.startsWith("weave."))).toBe(false);
});
