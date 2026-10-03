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
import { test, expect, type Page } from "@playwright/test";
import { resolve } from "node:path";
import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { offline, expectHitTarget, sourceText, newWorkflow } from "./support";

const fixture = resolve("tests/fixtures/vendor-payment-approval.yaml");
async function openFixture(page: Page, incomplete = false) {
  await offline(page);
  await page.getByLabel("Choose a workflow file").setInputFiles(
    incomplete
      ? {
          name: "vendor-payment-approval.yaml",
          mimeType: "application/yaml",
          buffer: Buffer.from(
            readFileSync(fixture, "utf8").replace(
              "uses: sql.lookup@1.0.0",
              "uses: your-action@1.0.0",
            ),
          ),
        }
      : fixture,
  );
  const showCanvas = page.getByRole("button", { name: "Show canvas" });
  if (await showCanvas.isVisible()) await showCanvas.click();
  await expect(page.locator('[data-step="record-result"]')).toBeAttached();
}

test.describe("Canvas editing at 1280x720", () => {
  test.use({ viewport: { width: 1280, height: 720 } });
  test("Fit all includes End and tidy reports an unchanged layout", async ({
    page,
  }) => {
    await openFixture(page);
    await page.getByRole("button", { name: "Fit all", exact: true }).click();
    await expect
      .poll(async () => {
        const canvas = await page.locator(".canvas").boundingBox();
        const end = await page
          .getByRole("img", { name: "Workflow end", exact: true })
          .boundingBox();
        return end!.y + end!.height <= canvas!.y + canvas!.height;
      })
      .toBe(true);
    await page
      .getByRole("button", { name: "Tidy layout", exact: true })
      .click();
    await expect(
      page.getByText("Layout is already tidy", { exact: true }),
    ).toBeVisible();
  });
  test("tiny overview avoids overlapping insertion controls and slash opens a usable slot", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 640, height: 360 });
    await openFixture(page);
    await page.getByRole("button", { name: "Fit all", exact: true }).click();
    await expect(page.locator(".canvas")).toHaveClass(/tiny-overview/);
    await expect(page.locator(".insertion-target").first()).toBeHidden();
    await expect(
      page.getByText("Overview · zoom in to insert", { exact: true }),
    ).toBeVisible();
    await page.locator('[data-step="prepare-request"] .node-body').focus();
    await page.keyboard.press("/");
    await expect(
      page.getByRole("dialog", { name: /after prepare-request/ }),
    ).toBeVisible();
    await expect(page.locator(".canvas")).not.toHaveClass(/tiny-overview/);
  });
  test("dragging to a slot changes sequence and offers Undo", async ({
    page,
  }) => {
    await openFixture(page);
    await page.getByRole("button", { name: "Fit all", exact: true }).click();
    const before = parse(await sourceText(page));
    await page.getByRole("button", { name: /^Zoom to 100%/ }).click();
    for (let i = 0; i < 4; i++)
      await page.getByRole("button", { name: "Zoom out", exact: true }).click();
    const node = page.locator('[data-step="prepare-request"] .node-body');
    await node.focus();
    await expectHitTarget(node);
    const slot = page.locator(
      '.insertion-target[data-owner="root"][data-index="2"]',
    );
    await expect(slot).toBeVisible();
    const from = await node.boundingBox(),
      to = await slot.boundingBox();
    await page.mouse.move(
      from!.x + from!.width / 2,
      from!.y + from!.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(to!.x + to!.width / 2, to!.y + to!.height / 2, {
      steps: 12,
    });
    await page.mouse.up();
    await expect(
      page.getByText("Moved prepare-request.", { exact: true }),
    ).toBeVisible();
    const undo = page.getByRole("button", { name: "Undo", exact: true }).last();
    await expect(undo).toBeVisible();
    const after = parse(await sourceText(page));
    expect(after.spec.steps.map((step: { id: string }) => step.id)).toEqual([
      "approval",
      "prepare-request",
      "route",
      "record-result",
    ]);
    await undo.click();
    expect(parse(await sourceText(page))).toEqual(before);
    await page.getByRole("button", { name: "Fit all", exact: true }).click();
    await node.focus();
    await expectHitTarget(node);
    const restored = await node.boundingBox(),
      canvas = await page.locator(".canvas").boundingBox();
    await page.mouse.move(
      restored!.x + restored!.width / 2,
      restored!.y + restored!.height / 2,
    );
    await page.mouse.down();
    await page.mouse.move(canvas!.x + 24, canvas!.y + 100, { steps: 10 });
    await page.mouse.up();
    await expect(
      page.getByText("Drop on a highlighted + to move this step.", {
        exact: true,
      }),
    ).toBeVisible();
    expect(parse(await sourceText(page))).toEqual(before);
  });
  test("node context actions offer duplication, deletion and answer paths", async ({
    page,
  }) => {
    await openFixture(page);
    const node = page.locator('[data-step="approval"] .node-body');
    await node.focus();
    await node.click({ button: "right" });
    await expect(
      page.getByRole("menuitem", { name: "Duplicate", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("menuitem", { name: "Delete step", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("menuitem", {
        name: "Add paths for answers",
        exact: true,
      }),
    ).toBeVisible();
    await page
      .getByRole("menuitem", { name: "Duplicate", exact: true })
      .click();
    const copy = page.locator('[data-step="approval-1"] .node-body');
    await expect(copy).toBeVisible();
    await copy.focus();
    await copy.click({ button: "right" });
    await page
      .getByRole("menuitem", { name: "Delete step", exact: true })
      .click();
    await expect(copy).toHaveCount(0);
    await page
      .getByRole("button", { name: "Undo", exact: true })
      .last()
      .click();
    await expect(copy).toBeVisible();
  });
});

for (const viewport of [
  { width: 1280, height: 720 },
  { width: 768, height: 1024 },
]) {
  test.describe(`Structural reveal at ${viewport.width}`, () => {
    test.use({ viewport });
    test("answer ghost creates a fully visible group beside the inspector", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const item = page
        .locator(".palette")
        .getByRole("button", { name: "Human task", exact: true });
      if (!(await item.isVisible()))
        await page
          .getByRole("button", { name: "Insert step", exact: true })
          .click();
      await item.click();
      await expect(page.locator(".palette.popover-visible")).toHaveCount(0);
      const ghost = page.getByRole("button", {
        name: "Branch on the answer (approve / reject)",
        exact: true,
      });
      await expectHitTarget(ghost);
      await ghost.click();
      await expect(
        page.locator('.lane-header[data-owner^="decision-1/"]'),
      ).toHaveCount(3);
      for (const lane of await page
        .locator('.lane-header[data-owner^="decision-1/"]')
        .all()) {
        await expect
          .poll(async () => {
            const box = await lane.boundingBox(),
              canvas = await page.locator(".canvas").boundingBox();
            return (
              !!box &&
              !!canvas &&
              box.x >= canvas.x &&
              box.x + box.width <= canvas.x + canvas.width &&
              box.y >= canvas.y &&
              box.y + box.height <= canvas.y + canvas.height
            );
          })
          .toBe(true);
        await expectHitTarget(lane);
      }
      await expect(
        page.locator('.lane-header[data-owner="decision-1/case 1"]'),
      ).toHaveText("Approve");
    });
  });
}

test.describe("Zoomed canvas editing", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test("40% retains readable lanes, problem dots and working insertion controls", async ({
    page,
  }) => {
    await openFixture(page, true);
    await page.getByRole("button", { name: /^Zoom to 100%, now/ }).click();
    for (let index = 0; index < 5; index++)
      await page.getByRole("button", { name: "Zoom out", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "Zoom to 100%, now 40%", exact: true }),
    ).toBeVisible();
    const target = page.getByRole("button", {
      name: "Add a step after post-ledger-entry, in ledger",
      exact: true,
    });
    await target.focus();
    await expectHitTarget(target);
    const box = await target.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(23.9);
    await expect(
      page.locator('.lane-header[data-owner="route/case 1"]'),
    ).toHaveText("Approve");
    const readable = await page
      .locator('.lane-header[data-owner="pay-and-notify/ledger"]')
      .evaluate(
        (element) =>
          (parseFloat(getComputedStyle(element).fontSize) *
            element.getBoundingClientRect().width) /
          (element as HTMLElement).offsetWidth,
      );
    expect(readable).toBeGreaterThanOrEqual(10.9);
    await expect(
      page.locator('[data-step="pay-vendor"] .node-chip'),
    ).toBeVisible();
    await target.click();
    await expect(
      page.getByRole("dialog", { name: /after post-ledger-entry, in ledger/i }),
    ).toBeVisible();
    await page.screenshot({
      path: "../.superpowers/editor-ux/after/canvas-40-percent-1440.png",
    });
    await page.keyboard.press("Escape");
    await expect(
      page.getByRole("dialog", { name: /after post-ledger-entry, in ledger/i }),
    ).toHaveCount(0);
    await page.locator('[data-step="post-ledger-entry"] .node-body').focus();
    await page.keyboard.press("/");
    await expect(
      page.getByRole("dialog", { name: /after post-ledger-entry, in ledger/i }),
    ).toBeVisible();
  });
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`Editor lanes at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("Decision and Parallel siblings share a row and Fail ends its path", async ({
      page,
    }) => {
      await openFixture(page);
      await page.getByRole("button", { name: "Fit all", exact: true }).click();
      for (const owners of [
        ["route/case 1", "route/case 2"],
        ["pay-and-notify/ledger", "pay-and-notify/email"],
      ]) {
        const a = page.locator(`.lane-header[data-owner="${owners[0]}"]`);
        const b = page.locator(`.lane-header[data-owner="${owners[1]}"]`);
        await expect(a).toBeVisible();
        await expect(b).toBeVisible();
        const [first, second] = await page.locator(".lane-header").evaluateAll(
          (headers, owners) =>
            owners.map((owner) => {
              const box = headers
                .find((header) => header.getAttribute("data-owner") === owner)!
                .getBoundingClientRect();
              return { x: box.x, y: box.y, width: box.width };
            }),
          owners,
        );
        expect(Math.abs(first.y - second.y)).toBeLessThanOrEqual(2);
        expect(first.x + first.width).toBeLessThanOrEqual(second.x);
      }
      await expect(page.locator('[data-terminal="rejected"]')).toBeVisible();
      await expect(page.locator('.edge[data-from="rejected"]')).toHaveCount(0);
      await expect(
        page.locator(
          '.insertion-target[data-owner="route/case 2"][data-index="1"]',
        ),
      ).toHaveCount(0);
    });

    test("the last ledger plus inserts into ledger, preserving email", async ({
      page,
    }) => {
      await openFixture(page);
      await page.getByRole("button", { name: /^Zoom to 100%, now/ }).click();
      const target = page.getByRole("button", {
        name: "Add a step after post-ledger-entry, in ledger",
        exact: true,
      });
      await target.focus();
      await expectHitTarget(target);
      await target.click();
      const search = page.getByRole("combobox", {
        name: "Search steps and actions",
      });
      await search.fill("Wait for time");
      await search.press("Enter");
      const source = parse(await sourceText(page));
      const parallel = source.spec.steps[2].cases[0].steps[1];
      expect(
        parallel.branches.ledger.steps.map((step: { id: string }) => step.id),
      ).toEqual(["post-ledger-entry", "wait-1"]);
      expect(
        parallel.branches.email.steps.map((step: { id: string }) => step.id),
      ).toEqual(["send-confirmation"]);
    });
  });
}
