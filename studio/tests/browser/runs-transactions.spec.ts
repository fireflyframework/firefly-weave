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
// A recorded run's transactions on the Runs tab, from runs.transactions on a
// mocked platform: the frame's state and each step's word, the point of no
// return, the header and list captions of a run in doubt, its incident in
// Operate, and never a business key.
import { expect, test, type Locator } from "@playwright/test";
import { StepDetailsPage } from "./step-details-po";
import { outputPane } from "./test-data-po";
import {
  BUSINESS_KEY,
  badge,
  chooseRun as choose,
  divider,
  frameState,
  IDS,
  INCIDENT,
  openTransactionRuns as openTab,
  word,
} from "./transaction-runs-po";
import type { RunsPlatform } from "./runs-po";

const transactionReads = (platform: RunsPlatform, id: string) =>
  platform.requests.filter((url) =>
    url.pathname.endsWith(`/runs/${id}/transactions`),
  );
/** The run header's caption parts that a line break splits in two. */
const splitParts = (header: Locator) =>
  header
    .locator(".run-caption > span:not(.run-in-doubt), .run-in-doubt-part")
    .evaluateAll((parts) =>
      parts
        .filter((part) => part.getClientRects().length > 1)
        .map((part) => part.textContent?.trim()),
    );
/** Where the caption's first in-doubt part sits: at the start of a line of
 * its own below "Following this run", or after something on its line. */
const firstDoubtPart = (header: Locator) =>
  header.locator(".run-caption").evaluate((caption) => {
    const first = caption.querySelector(".run-in-doubt-part")!;
    const follow = caption.querySelector(".run-follow-caption")!;
    const part = first.getBoundingClientRect();
    return {
      indent: Math.round(part.left - caption.getBoundingClientRect().left),
      belowFollow: part.top >= follow.getBoundingClientRect().bottom - 1,
    };
  });

test.describe("a recorded run's transactions at 1440x900", () => {
  test.use({
    viewport: { width: 1440, height: 900 },
    contextOptions: { reducedMotion: "reduce" },
  });

  test("a recorded rollback reads Undone, each step's word and the point of no return", async ({
    page,
  }) => {
    const { runs, platform, errors } = await openTab(page);
    const canvas = await choose(page, runs, IDS.undone);
    await expect(frameState(canvas)).toHaveText("Undone");
    await expect(frameState(canvas)).toHaveAttribute("data-tone", "warning");
    await expect(word(canvas, "hold")).toHaveText("Released");
    await expect(word(canvas, "add")).toHaveText("Undone");
    await expect(word(canvas, "submit")).toHaveText("Failed");
    await expect(badge(canvas, "submit")).toHaveAttribute(
      "data-badge",
      "failed",
    );
    await expect(divider(canvas)).toHaveCount(1);
    // One page of 100, and the header says more follow.
    expect(
      transactionReads(platform, IDS.undone).map((url) => url.search),
    ).toEqual(["?limit=100"]);
    await expect(runs.canvas.locator(".run-transactions-note")).toHaveText(
      "Showing the first 100 transactions.",
    );
    // Its step details say the step was undone.
    const details = new StepDetailsPage(page);
    await details.open("add");
    const pane = await outputPane(details);
    await expect(pane).toContainText(
      "This step was undone after the transaction failed (try 10 of 10).",
    );
    expect(await page.locator("body").innerText()).not.toContain(BUSINESS_KEY);
    expect(errors).toEqual([]);
  });

  test("a run in doubt names the step and the transaction, keeps what is reserved, and opens its incident", async ({
    page,
  }) => {
    const { runs, errors } = await openTab(page);
    // A run in doubt whose transactions Studio hasn't read.
    await expect(runs.row(IDS.earlier).locator(".run-caption-line")).toHaveText(
      "Transaction in doubt",
    );
    const canvas = await choose(page, runs, IDS.doubt);
    const header = runs.canvas.locator(".run-header");
    await expect(header.locator(".run-pill")).toHaveText("On hold");
    await expect(header.locator(".run-in-doubt")).toHaveText(
      "Failed at submit: REQUEST_REFUSED · transaction move is in doubt · WV-TXN-UNDO-STUCK",
    );
    // The caption wraps only between whole parts: when it started and how
    // long it ran never split.
    expect(await splitParts(header)).toEqual([]);
    await expect(runs.row(IDS.doubt).locator(".run-caption-line")).toHaveText(
      "Failed at submit · move in doubt",
    );
    await expect(runs.row(IDS.earlier).locator(".run-caption-line")).toHaveText(
      "Transaction in doubt",
    );
    await expect(frameState(canvas)).toHaveText("In doubt · action needed");
    await expect(frameState(canvas)).toHaveAttribute("data-tone", "danger");
    // Its header says how it stands: nothing more under the frame.
    await expect(word(canvas, "move")).toHaveCount(0);
    await expect(word(canvas, "hold")).toHaveText("Still reserved");
    await expect(word(canvas, "hold")).toHaveAttribute("data-tone", "warning");
    // Never the success check: the hold is still applied.
    await expect(badge(canvas, "hold")).toHaveAttribute("data-tone", "warning");
    await expect(badge(canvas, "hold").locator("weave-icon")).toHaveAttribute(
      "data-icon",
      "lock",
    );
    await expect(word(canvas, "add")).toHaveText("Undo failed");
    await expect(divider(canvas)).toHaveCount(1);

    const details = new StepDetailsPage(page);
    await details.open("move");
    const pane = await outputPane(details);
    await expect(pane.locator(".pill").first()).toHaveText(
      "In doubt · action needed",
    );
    const banner = pane.locator(".pane-in-doubt");
    await expect(banner).toContainText(
      "This transaction is in doubt. Someone must resolve it in Operate.",
    );
    await expect(banner).toContainText(
      "Undo of add failed after 10 tries: UNAVAILABLE. Weave stopped undoing, so hold is still reserved.",
    );
    await expect(pane).toContainText(
      "No result: the run stopped while move was in doubt.",
    );
    // Settings: the transaction's own, read only, Give up undoing after among them.
    await details.dialog.getByRole("tab", { name: "Settings" }).click();
    const settings = details.dialog.locator(".settings-form");
    for (const label of [
      "Business key",
      "Hold reservations for",
      "Undo retries",
      "Give up undoing after",
    ])
      await expect(settings).toContainText(label);
    await expect(
      settings.locator(
        "input:not([disabled]):not([readonly]), select:not([disabled]), textarea:not([disabled]):not([readonly])",
      ),
    ).toHaveCount(0);
    // The business key's mapping, never the value it read in this run.
    const key = settings.locator(".param", { hasText: "Business key" }).first();
    await expect(key).toContainText("moveId");
    const shown = await key.evaluate((element) =>
      [
        element.textContent ?? "",
        ...[...element.querySelectorAll("*")].flatMap((node) => [
          node.getAttribute("title") ?? "",
          node.getAttribute("aria-label") ?? "",
          node.getAttribute("aria-description") ?? "",
        ]),
      ].join("\n"),
    );
    expect(shown).not.toContain("m-0193");
    expect(await page.locator("body").innerText()).not.toContain(BUSINESS_KEY);

    await details.dialog.getByRole("tab", { name: "Parameters" }).click();
    await (await outputPane(details))
      .getByRole("button", { name: "Open in Operate" })
      .click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/incidents\\?incident=${INCIDENT}$`),
    );
    expect(errors).toEqual([]);
  });

  test("a run whose transactions are refused says so, and draws no transaction words", async ({
    page,
  }) => {
    const { runs, errors } = await openTab(page);
    const canvas = await choose(page, runs, IDS.refused);
    await expect(runs.canvas.locator(".run-transactions-note")).toHaveText(
      "You can't see this run's transactions here.",
    );
    await expect(frameState(canvas)).toHaveCount(0);
    await expect(canvas.tile("hold")).not.toContainText("Released");
    await expect(canvas.tile("add")).not.toContainText("Undone");
    // The line comes from the version, so it stays.
    await expect(divider(canvas)).toHaveCount(1);
    expect(errors).toEqual([]);
  });
});

test.describe("a recorded run in doubt on a phone", () => {
  test.use({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
    contextOptions: { reducedMotion: "reduce" },
  });

  test("its captions wrap whole, never cut", async ({ page }) => {
    const { runs, errors } = await openTab(page);
    await runs.row(IDS.doubt).click();
    const parts = runs.canvas.locator(".run-in-doubt-part");
    await expect(parts).toHaveCount(3);
    for (const part of await parts.all()) {
      const box = (await part.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(390);
    }
    const header = runs.canvas.locator(".run-header");
    expect(await splitParts(header)).toEqual([]);
    // "Following this run" has a line of its own, so the next line starts
    // with the step that failed, never with a separator.
    expect(await firstDoubtPart(header)).toEqual({
      indent: 0,
      belowFollow: true,
    });
    // Also where both would fit on one line.
    await page.setViewportSize({ width: 470, height: 844 });
    expect(await firstDoubtPart(header)).toEqual({
      indent: 0,
      belowFollow: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await runs.canvas.getByRole("button", { name: "Back to runs" }).click();
    const caption = runs.row(IDS.doubt).locator(".run-caption-line");
    await expect(caption).toHaveText("Failed at submit · move in doubt");
    const cut = await caption.evaluate(
      (element) => element.scrollWidth > element.clientWidth,
    );
    expect(cut).toBe(false);
    const row = (await runs.row(IDS.doubt).boundingBox())!;
    expect(row.height).toBeGreaterThanOrEqual(56);
    expect(errors).toEqual([]);
  });
});
