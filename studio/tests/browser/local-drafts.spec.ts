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
// Local work is never silently lost: autosave to this browser, a reload
// brings the workflow back at the same address, Home offers "Continue
// editing", Workflows lists "On this computer", and a browser that refuses
// storage says so.
import { test, expect, Page } from "@playwright/test";
import { connected, insertStep, newWorkflow, offline } from "./support";
import { platformHost } from "./platform-host";

const steps = (page: Page) => page.locator("[data-step]");

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(
    `${viewport.width}x${viewport.height}`,
    { tag: "@xplat" },
    () => {
      test.use({ viewport });

      test("a local workflow survives a reload at the same address", async ({
        page,
      }) => {
        await offline(page);
        await newWorkflow(page);
        for (const step of ["Transform", "Wait for time", "Human task"])
          await insertStep(page, step);
        await expect(steps(page)).toHaveCount(3);
        const address = page.url();
        expect(address).toMatch(/\/workflows\/[^/]+\/designer$/);
        // The autosave waits 800 ms after the last change.
        await expect
          .poll(() =>
            page.evaluate(() => localStorage.getItem("weave.localDrafts.v1")),
          )
          .toContain("untitled-workflow");
        await page.reload();
        await expect(steps(page)).toHaveCount(3);
        expect(page.url()).toBe(address);
        await expect(page).toHaveTitle(
          "untitled-workflow | Designer | Firefly Weave Studio",
        );
      });

      test("Home continues the last workflow; Workflows lists it on this computer", async ({
        page,
      }) => {
        await offline(page);
        await newWorkflow(page);
        await insertStep(page, "Transform");
        await page.getByRole("button", { name: "Home", exact: true }).click();
        // Leaving says where the workflow is kept.
        const leave = page.getByRole("dialog", {
          name: "Leave untitled-workflow?",
        });
        await expect(leave).toContainText(
          "You'll find it under Workflows › On this computer.",
        );
        await leave.getByRole("button", { name: "Leave", exact: true }).click();
        const resume = page.getByRole("button", { name: /^Continue editing/ });
        await expect(resume).toContainText(
          "Continue editing untitled-workflow, edited",
        );
        // The first row of Home.
        const first = await page
          .locator("weave-home-dashboard button")
          .evaluateAll(
            (buttons) =>
              buttons.find(
                (b) =>
                  !b.closest(".home-header") &&
                  b.getBoundingClientRect().height,
              )?.textContent ?? "",
          );
        expect(first).toContain("Continue editing");
        await resume.click();
        await expect(steps(page)).toHaveCount(1);
        await page
          .getByRole("button", { name: "Workflows", exact: true })
          .click();
        await page.getByRole("button", { name: "Leave", exact: true }).click();
        await expect(
          page.getByRole("button", { name: "On this computer" }),
        ).toHaveAttribute("aria-pressed", "true");
        const row = page.locator(".resource-row").filter({
          hasText: "untitled-workflow",
        });
        await expect(row).toContainText("Kept on this computer");
        await expect(row).toContainText("1.0.0");
        await expect(row).toContainText(/Edited (just now|\d+ min ago)/);
        await row
          .getByRole("button", {
            name: "Delete untitled-workflow from this computer",
          })
          .click();
        const confirm = page.getByRole("dialog", {
          name: "Delete untitled-workflow from this computer?",
        });
        await confirm
          .getByRole("button", { name: "Delete", exact: true })
          .click();
        await expect(row).toHaveCount(0);
        const toast = page.locator(".toast");
        await expect(toast).toContainText(
          "Deleted untitled-workflow from this computer.",
        );
        await toast.getByRole("button", { name: "Undo" }).click();
        await expect(row).toHaveCount(1);
        await expect(toast).toContainText("Restored untitled-workflow.");
      });

      test("a new workflow keeps the previous one", async ({ page }) => {
        await offline(page);
        await newWorkflow(page);
        await insertStep(page, "Transform");
        await page
          .getByRole("button", { name: "Workflows", exact: true })
          .click();
        await page.getByRole("button", { name: "Leave", exact: true }).click();
        await page
          .getByRole("button", { name: "New workflow", exact: true })
          .click();
        await insertStep(page, "Wait for time");
        await page
          .getByRole("button", { name: "Workflows", exact: true })
          .click();
        await page.getByRole("button", { name: "Leave", exact: true }).click();
        await expect(page.locator(".resource-row")).toHaveCount(2);
        await expect(page.locator(".count")).toHaveText("2 workflows");
      });

      test("storage that refuses drafts is explained once and nothing breaks", async ({
        page,
      }) => {
        await page.addInitScript(() => {
          const refuse = () => {
            throw new DOMException("Storage is blocked", "SecurityError");
          };
          Storage.prototype.setItem = refuse;
          Storage.prototype.getItem = refuse;
        });
        await offline(page);
        await newWorkflow(page);
        await insertStep(page, "Transform");
        const notice = page.getByRole("alert").filter({
          hasText: "Studio can't keep drafts in this browser.",
        });
        await expect(notice).toContainText(
          "Use Save to file to keep your work.",
        );
        await insertStep(page, "Wait for time");
        await expect(steps(page)).toHaveCount(2);
        await expect(notice).toHaveCount(1);
        // Leaving says the truth: nothing is kept here.
        await page.getByRole("button", { name: "Home", exact: true }).click();
        await expect(page.getByRole("dialog")).toContainText(
          "Studio can't keep drafts in this browser.",
        );
      });
    },
  );

test("the desktop app says local drafts last until it quits", async ({
  page,
}) => {
  // The desktop window keeps no browser data after the app quits.
  await page.addInitScript(() => {
    (window as unknown as Record<string, unknown>)["__TAURI_INTERNALS__"] = {};
  });
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  await expect(page.locator(".status-chip")).toContainText(
    /Draft saved \d{2}:\d{2}/,
    { timeout: 10_000 },
  );
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  const leave = page.getByRole("dialog", { name: "Leave untitled-workflow?" });
  await expect(leave).toContainText(
    "until you quit Firefly Weave Studio. Save to file to keep a copy.",
  );
  await leave.getByRole("button", { name: "Leave", exact: true }).click();
  await page.getByRole("button", { name: "On this computer" }).click();
  await expect(page.locator(".local-quit-notice")).toContainText(
    "The desktop app keeps these drafts only until you quit it.",
  );
  await expect(
    page.locator(".resource-row").filter({ hasText: "untitled-workflow" }),
  ).toContainText("Kept until you quit");
});

test("a link to a workflow that isn't here explains and opens Workflows", async ({
  page,
}) => {
  await platformHost(page, {}, "/workflows/does-not-exist/designer");
  const toast = page.getByRole("alert").locator(".toast");
  await expect(toast).toHaveAttribute("data-tone", "danger");
  await expect(toast).toContainText(
    "That workflow isn't available. It may have been deleted, or it belongs to another workspace.",
  );
  await expect(page).toHaveURL(/\/workflows$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Workflows");
});

test("the back button returns to the open workflow", async ({ page }) => {
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  await page.getByRole("button", { name: "Leave", exact: true }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Workflows");
  await page.goBack();
  await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
});

test("the reload guard holds only edits that aren't kept anywhere", async ({
  page,
}) => {
  // What the browser's "Leave site?" question depends on: whether Studio
  // cancels beforeunload.
  const guarded = () =>
    page.evaluate(() => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    });
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  // Kept on this computer (the guard saves a waiting autosave first).
  expect(await guarded()).toBe(false);
  await expect
    .poll(() =>
      page.evaluate(() => localStorage.getItem("weave.localDrafts.v1")),
    )
    .toContain("untitled-workflow");
  // A browser that refuses storage keeps nothing: the guard asks.
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new DOMException("full", "QuotaExceededError");
    };
  });
  await insertStep(page, "Wait for time");
  expect(await guarded()).toBe(true);
});

test("connected, unsaved designer edits keep the reload guard on", async ({
  page,
}) => {
  await connected(page);
  await newWorkflow(page);
  expect(
    await page.evaluate(() => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    }),
  ).toBe(false);
  await insertStep(page, "Transform");
  expect(
    await page.evaluate(() => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    }),
  ).toBe(true);
});

test("a draft whose notes and settings can't be read opens without them and says so", async ({
  page,
}) => {
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  await expect(page.locator(".status-chip")).toContainText(
    /Draft saved \d{2}:\d{2}/,
    { timeout: 10_000 },
  );
  const toast = page.getByRole("alert").locator(".toast");
  // A draft whose notes and settings read fine opens without a word.
  await page.reload();
  await expect(steps(page)).toHaveCount(1);
  await expect(toast).toHaveCount(0);
  // One saved by a newer Studio can't be read: the workflow still opens.
  await page.evaluate(() => {
    const [entry] = JSON.parse(localStorage.getItem("weave.localDrafts.v1")!);
    const key = `weave.localDraft.${entry.id}`;
    const draft = JSON.parse(localStorage.getItem(key)!);
    draft.canvas = { schemaVersion: 3, kind: "weave.studio/canvas" };
    localStorage.setItem(key, JSON.stringify(draft));
  });
  await page.reload();
  await expect(steps(page)).toHaveCount(1);
  await expect(toast).toHaveAttribute("data-tone", "danger");
  await expect(toast).toContainText(
    "Studio couldn't read the notes and settings saved with this workflow, so it opened without them.",
  );
});
