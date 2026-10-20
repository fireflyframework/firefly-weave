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
// A platform draft's test data and canvas live on the platform: they save
// after the draft, come back on reload, ask before replacing either side on
// a conflict, leave every copy on this computer when the platform refuses a
// secret, and keep entries the platform can't classify yet on this computer.
import { expect, test } from "@playwright/test";
import { CanvasPage } from "./canvas-po";
import {
  COPY,
  PIN,
  edit,
  fresh,
  kept,
  openDraft,
  testData,
} from "./connected-test-data-po";

test("test data and the canvas save with the draft and come back on reload", async ({
  page,
}) => {
  const state = fresh();
  await openDraft(page, state, testData({ customerId: "c-1042" }));
  // A sticky note changes the canvas; the draft saves with it.
  await page.locator('[data-step="check-customer"]').first().click();
  await page.keyboard.press("Shift+S");
  await page.keyboard.press("Escape");
  await expect
    .poll(() => state.saves.map((save) => save.kind), { timeout: 6000 })
    .toEqual(["draft", "test-data", "canvas"]);
  const [, data] = state.saves;
  expect(data.body["draft_revision"]).toBe(2);
  expect(
    (data.body["document"] as { input: { value: unknown } }).input.value,
  ).toEqual({ customerId: "c-1042" });
  // Another note on the platform's canvas, and this computer's copy gone: the platform's come back.
  state.documents.set("canvas", {
    revision: 7,
    document: {
      schemaVersion: 2,
      kind: "weave.studio/canvas",
      notes: [
        {
          id: "note-1a2b3c4d",
          text: "Kept on the platform",
          color: "blue",
          x: 400,
          y: 360,
          width: 240,
          height: 120,
        },
      ],
    },
  });
  await page.evaluate((key) => localStorage.removeItem(key), COPY);
  await page.reload();
  await new CanvasPage(page).ready();
  await expect(page.locator(".sticky-note-body")).toHaveText(
    "Kept on the platform",
  );
  await expect.poll(() => kept(page)).toContain("c-1042");
});

test("a changed document on the platform asks before replacing either side", async ({
  page,
}) => {
  const state = fresh();
  await openDraft(page, state, testData({ customerId: "mine" }));
  // Someone else saved test data for this draft meanwhile: this first save is stale.
  state.refuse.push({
    status: 412,
    json: { status: 412, code: "WV-ETAG", message: "Changed" },
  });
  state.documents.set("test-data", {
    revision: 4,
    document: testData({ customerId: "theirs" }),
  });
  await edit(page);
  const dialog = page.getByRole("dialog", {
    name: "Someone else changed the test data",
  });
  await expect(dialog).toBeVisible({ timeout: 6000 });
  await expect(dialog).toContainText(
    "Keep your version, or use the one saved on the platform.",
  );
  await expect(dialog.getByRole("button", { name: "Keep mine" })).toBeFocused();
  await dialog.getByRole("button", { name: "Use theirs" }).click();
  await expect.poll(() => kept(page)).toContain('"theirs"');
  const toast = page.locator(".toast").filter({
    hasText: "Using the test data saved on the platform.",
  });
  await toast.getByRole("button", { name: "Undo" }).click();
  await expect.poll(() => kept(page)).toContain('"mine"');
});

test("Keep mine saves this computer's test data over the platform's", async ({
  page,
}) => {
  const state = fresh();
  await openDraft(page, state, testData({ customerId: "mine" }));
  state.refuse.push({
    status: 412,
    json: { status: 412, code: "WV-ETAG", message: "Changed" },
  });
  state.documents.set("test-data", {
    revision: 4,
    document: testData({ customerId: "theirs" }),
  });
  await edit(page);
  const dialog = page.getByRole("dialog", {
    name: "Someone else changed the test data",
  });
  await dialog.getByRole("button", { name: "Keep mine" }).click();
  await expect
    .poll(() => JSON.stringify(state.documents.get("test-data")))
    .toContain('"mine"');
});

test("a value the platform refuses as secret leaves every copy on this computer", async ({
  page,
}) => {
  const state = fresh();
  state.refuse.push({
    status: 422,
    json: {
      status: 422,
      code: "WV-STUDIO-DOCUMENT-SECRET",
      message: "Secret",
      result: { paths: ["/input"] },
    },
  });
  await openDraft(page, state, testData({ password: "s3cret-canary" }));
  await expect.poll(() => kept(page)).toContain("s3cret-canary");
  await edit(page);
  await expect(page.locator(".toast")).toContainText(
    "This value is marked secret in the schema. Test data can't keep secrets; leave the field out.",
  );
  await expect
    .poll(() => page.evaluate(() => JSON.stringify(localStorage)))
    .not.toContain("s3cret-canary");
});

test("an entry waiting for publishing stays on this computer and is left out of saves", async ({
  page,
}) => {
  const state = fresh();
  state.refuse.push({
    status: 422,
    json: {
      status: 422,
      code: "WV-STUDIO-DOCUMENT-UNCLASSIFIED",
      message: "Unclassified",
      result: { paths: ["/pins/check-customer"] },
    },
  });
  await openDraft(
    page,
    state,
    testData({ customerId: "c-1" }, { pins: { "check-customer": PIN } }),
  );
  await edit(page);
  await expect
    .poll(
      () => state.saves.filter((save) => save.kind === "test-data").length,
      { timeout: 6000 },
    )
    .toBe(2);
  const [first, second] = state.saves.filter(
    (save) => save.kind === "test-data",
  );
  expect(JSON.stringify(first.body)).toContain("check-customer");
  expect(JSON.stringify(second.body)).not.toContain("check-customer");
  // This computer keeps the entry, and the list of what waits for publishing.
  await expect.poll(() => kept(page)).toContain("c-1");
  expect(
    await page.evaluate((key) => localStorage.getItem(`${key}.local`), COPY),
  ).toBe('["/pins/check-customer"]');
});

test("Use theirs on a canvas Studio can't read keeps theirs on the platform and says so", async ({
  page,
}) => {
  const state = fresh();
  await openDraft(page, state);
  // Someone else saved a canvas this Studio can't read; this first canvas save is stale.
  const theirs = {
    schemaVersion: 2,
    kind: "weave.studio/canvas",
    notes: "not a list",
  };
  state.documents.set("canvas", { revision: 5, document: theirs });
  state.refuseCanvas = [
    { status: 412, json: { status: 412, code: "WV-ETAG", message: "Changed" } },
  ];
  await page.locator('[data-step="check-customer"]').first().click();
  await page.keyboard.press("Shift+S");
  await page.keyboard.press("Escape");
  const dialog = page.getByRole("dialog", {
    name: "Someone else changed the canvas",
  });
  await expect(dialog).toBeVisible({ timeout: 6000 });
  await dialog.getByRole("button", { name: "Use theirs" }).click();
  await expect(
    page.locator(".toast").filter({ hasText: "couldn't read" }),
  ).toBeVisible();
  // Later changes save the draft (at most every 10 s), never this canvas over theirs.
  const canvasSaves = () =>
    state.saves.filter((save) => save.kind === "canvas").length;
  const before = canvasSaves();
  const drafts = state.saves.filter((save) => save.kind === "draft").length;
  await edit(page);
  await expect
    .poll(() => state.saves.filter((save) => save.kind === "draft").length, {
      timeout: 15000,
    })
    .toBeGreaterThan(drafts);
  await page.waitForTimeout(1500);
  expect(canvasSaves()).toBe(before);
  expect(state.documents.get("canvas")).toEqual({
    revision: 5,
    document: theirs,
  });
});
