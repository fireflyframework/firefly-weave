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
// Execute workflow plays a transaction's undo steps: the frame counts them,
// each step lights as it is undone, the edge into it drawn back, then it says
// how it ended; the run log lists them in the order they ran. The platform is
// mocked; the Studio host that simulates is the repository's own Python.
import { expect, test, type Page } from "@playwright/test";
import { CanvasPage } from "./canvas-po";
import { hasPython } from "./local-authoring";
import type { StepDetailsPage } from "./step-details-po";
import { outputPane } from "./test-data-po";
import {
  ADD_STEP,
  executeWorkflow,
  HOLD_STEP,
  moveWorkflow,
  NOTIFY_STEP,
  openInside,
  openMoveToExecute,
  SUBMIT_STEP,
} from "./transaction-po";

test.skip(!hasPython, "Needs the repository's Python environment.");
// The host simulates with the repository's Python, which a busy machine starts slowly.
test.setTimeout(90_000);
const ANSWER = { timeout: 30_000 };

const STEPS =
  HOLD_STEP() + ADD_STEP + SUBMIT_STEP + NOTIFY_STEP + "  irreversible: true\n";
const SUMMARIZE = `- id: summarize
  kind: transform
  value: {object: {done: {literal: true}}}
`;
const MOVE = moveWorkflow(STEPS);
const REFUSED = { code: "REQUEST_REFUSED", outcome: "failed", effect: "none" };
const FAILED = { errors: { submit: REFUSED } };
const STUCK = {
  errors: {
    submit: REFUSED,
    "add#undo": { code: "UNAVAILABLE", outcome: "failed", attempts: 10 },
  },
};

const frameState = (canvas: CanvasPage) =>
  canvas.tileBody("move").locator(".frame-state");
const word = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".tile-output");
const badge = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".tile-badge");
const runLog = (page: Page) => page.getByRole("region", { name: "Run log" });
/** The run log's rows as they read, in order. */
const logged = async (page: Page) =>
  (await runLog(page).locator(".run-log-entry").allInnerTexts()).map((text) =>
    text.replace(/\s+/g, " ").trim(),
  );
/** Keeps what the canvas shows at each moment of the playback. */
const watch = (page: Page) =>
  page.evaluate(() => {
    interface Moment {
      frame: string;
      live: string[];
      back: string[];
      arrows: string[];
      log: string[];
    }
    const moments: Moment[] = [];
    Object.assign(window, { moments });
    const canvas = document.querySelector(".canvas-v2")!;
    const names = (selector: string, attribute: string) =>
      [...canvas.querySelectorAll(selector)].map(
        (element) =>
          element.closest(`[${attribute}]`)!.getAttribute(attribute) ?? "",
      );
    new MutationObserver(() => {
      const back = [...canvas.querySelectorAll(".edge.undoing")];
      const moment: Moment = {
        frame: canvas.querySelector(".frame-state")?.textContent?.trim() ?? "",
        live: names(".tile-shape.live", "data-tile"),
        back: back.map((edge) => edge.getAttribute("data-edge-line") ?? ""),
        arrows: back.map(
          (edge) =>
            `${edge.getAttribute("marker-start")} ${edge.getAttribute("marker-end")}`,
        ),
        log: [
          ...document.querySelectorAll(".run-log-transaction, .run-log-phase"),
        ].map((row) => row.getAttribute("aria-label") ?? ""),
      };
      if (JSON.stringify(moment) !== JSON.stringify(moments.at(-1)))
        moments.push(moment);
    }).observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    });
  });
const moments = (page: Page) =>
  page.evaluate(
    () =>
      (
        window as unknown as {
          moments: {
            frame: string;
            live: string[];
            back: string[];
            arrows: string[];
            log: string[];
          }[];
        }
      ).moments,
  );
/** Simulate a failure… of a step, from its Output. */
async function simulate(page: Page, details: StepDetailsPage, id: string) {
  await openInside(details, id);
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "More output actions" }).click();
  await page.getByRole("menuitem", { name: "Simulate a failure…" }).click();
  return page.getByRole("dialog", { name: `Simulate a failure of ${id}` });
}

test.describe("a simulated run's undo steps at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a step that can't be undone fails: what's done is undone, in order", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("console", (message) => {
      // Not a resource the mocked platform doesn't have.
      if (
        message.type() === "error" &&
        !message.text().startsWith("Failed to load")
      )
        errors.push(message.text());
    });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const { details } = await openMoveToExecute(page, MOVE);
    const dialog = await simulate(page, details, "submit");
    await dialog.getByLabel("Code").fill("REQUEST_REFUSED");
    // submit's action doesn't say this code changes nothing.
    await dialog.getByRole("radio", { name: /^It changed nothing/ }).check();
    await dialog.getByRole("button", { name: "Simulate failure" }).click();
    await details.close();
    const canvas = new CanvasPage(page);
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText("Undone", ANSWER);
    await expect(frameState(canvas)).toHaveAttribute("data-tone", "warning");
    await expect(word(canvas, "add")).toHaveText("Undone");
    await expect(word(canvas, "hold")).toHaveText("Released");
    await expect(word(canvas, "submit")).toHaveText("Failed");
    for (const id of ["add", "hold"])
      await expect(badge(canvas, id)).toHaveAttribute("data-badge", "undone");
    await expect(badge(canvas, "submit")).toHaveAttribute(
      "data-badge",
      "failed",
    );
    await expect(canvas.tileBody("submit")).toHaveAccessibleName(
      /Failed: REQUEST_REFUSED\. The transaction was undone\./,
    );
    await expect(canvas.tileBody("move")).toHaveAccessibleName(
      "move, Transaction, All of these succeed, or what's done is undone, Undone, step 1 of 1 in Main sequence",
    );
    await expect(canvas.tileBody("hold")).toHaveAccessibleName(/, Released,/);
    // The block's own failure is in its header: no badge on the corner, no error border.
    await expect(badge(canvas, "move")).toHaveCount(0);
    await expect(
      canvas.root.locator('.loop-frame[data-frame="move"]'),
    ).not.toHaveClass(/\berror\b/);
    // The point of no return stays drawn, and how each step is undone doesn't.
    await expect(
      canvas.root.getByRole("img", {
        name: "Point of no return, after submit",
      }),
    ).toBeVisible();
    await expect(canvas.root.locator(".undo-badge")).toHaveCount(0);

    const log = runLog(page);
    await expect(log.locator(".run-log-outcome")).toHaveText(
      "The simulated run failed: the transaction move was undone.",
    );
    expect(await logged(page)).toEqual([
      "hold 1 field",
      "add 1 field",
      "submit REQUEST_REFUSED",
      "Transaction move Undone",
      "Undo of add Undone",
      "Release of hold Released",
    ]);
    // Its undo steps are further in, as a list of the transaction's.
    const undo = log.getByRole("button", { name: "Undo of add, undone" });
    const own = log.getByRole("button", { name: "Transaction move, undone" });
    const left = (row: typeof undo) =>
      row
        .locator(".run-log-icon")
        .evaluate((icon) => icon.getBoundingClientRect().left);
    expect((await left(undo)) - (await left(own))).toBe(24);
    expect((await own.boundingBox())!.height).toBe(36);
    await expect(
      log.getByRole("list", { name: "Transaction move" }),
    ).toHaveCount(1);
    // A row opens the step it is about.
    await undo.click();
    await expect(details.dialogFor("add")).toBeVisible();
    await details.close();
    expect(errors).toEqual([]);
  });

  test("plays them back: the frame counts, the step lights and its edge points back", async ({
    page,
  }) => {
    await openMoveToExecute(page, MOVE, FAILED);
    const canvas = new CanvasPage(page);
    await watch(page);
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText("Undone", ANSWER);
    const played = await moments(page);
    const states = [...new Set(played.map(({ frame }) => frame))];
    expect(states).toEqual([
      "",
      "Waiting for submit, which can't be undone",
      "Undoing · 1 of 2",
      "Undoing · 2 of 2",
      "Undone",
    ]);
    const at = (state: string) =>
      played.filter(({ frame }) => frame === state).at(-1)!;
    expect(at("Undoing · 1 of 2")).toMatchObject({
      live: ["move", "add"],
      back: ["hold>add"],
      arrows: ["url(#canvas-arrow-undoing) null"],
    });
    expect(at("Undoing · 1 of 2").log).toEqual([
      "Transaction move, undoing, 1 of 2",
      "Undo of add, running",
    ]);
    expect(at("Undoing · 2 of 2").live).toEqual(["move", "hold"]);
    expect(at("Undoing · 2 of 2").back).toHaveLength(1);
    expect(at("Undone")).toMatchObject({
      live: [],
      back: [],
      log: [
        "Transaction move, undone",
        "Undo of add, undone",
        "Release of hold, released",
      ],
    });
    await expect(canvas.root.locator(".edge.undoing")).toHaveCount(0);
    // The canvas says how it ended.
    await expect(
      canvas.root.locator('[role="alert"], [aria-live]'),
    ).toContainText([
      "The simulated run failed: the transaction move was undone.",
    ]);
  });

  test("shows how it ended at once with reduced motion", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openMoveToExecute(page, MOVE, FAILED);
    const canvas = new CanvasPage(page);
    await watch(page);
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText("Undone", ANSWER);
    const states = new Set((await moments(page)).map(({ frame }) => frame));
    expect([...states].filter(Boolean)).toEqual(["Undone"]);
  });

  test("an undo that keeps failing leaves it in doubt, with what is still applied", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const { details } = await openMoveToExecute(page, MOVE, FAILED);
    const dialog = await simulate(page, details, "add");
    await dialog.getByRole("radio", { name: "Its undo" }).check();
    await dialog.getByRole("button", { name: "Simulate failure" }).click();
    await details.close();
    const canvas = new CanvasPage(page);
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText(
      "In doubt · action needed",
      ANSWER,
    );
    await expect(frameState(canvas)).toHaveAttribute("data-tone", "danger");
    await expect(
      canvas.root.locator('.loop-frame[data-frame="move"]'),
    ).toHaveClass(/\berror\b/);
    await expect(word(canvas, "hold")).toHaveText("Still reserved");
    await expect(word(canvas, "hold")).toHaveAttribute("data-tone", "warning");
    await expect(badge(canvas, "hold")).toHaveAttribute("data-tone", "warning");
    await expect(badge(canvas, "hold").locator("weave-icon")).toHaveAttribute(
      "data-icon",
      "lock",
    );
    await expect(word(canvas, "add")).toHaveText("Undo failed");
    await expect(word(canvas, "submit")).toHaveText("Failed");
    // Who acts on it, in the header's tooltip.
    await canvas.tileBody("move").hover();
    await expect(canvas.root.locator(".frame-tip")).toContainText(
      "In doubt · action needed. In a real run, someone resolves this in Operate.",
    );
    const log = runLog(page);
    await expect(log.locator(".run-log-outcome p")).toHaveText([
      "The transaction move is in doubt, so the simulated run stopped.",
      "WV-TXN-UNDO-STUCK: A step's undo couldn't finish, so the transaction is in doubt.",
    ]);
    await expect(log.locator(".run-log-status")).toHaveText(
      "Stopped · 2 steps",
    );
    expect((await logged(page)).slice(2)).toEqual([
      "submit REQUEST_REFUSED",
      "Transaction move In doubt · action needed",
      "Undo of add Failed after 10 tries",
    ]);
    await expect(
      log.getByRole("button", { name: "Undo of add, failed after 10 tries" }),
    ).toBeVisible();
  });

  test("a failure that changed nothing has nothing to undo", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const { details } = await openMoveToExecute(page, MOVE);
    const dialog = await simulate(page, details, "add");
    await expect(dialog.getByLabel("Code")).toHaveValue("VALIDATION");
    await expect(
      dialog.getByRole("radio", { name: /^Didn't happen/ }),
    ).toBeChecked();
    await expect(
      dialog.getByRole("radio", { name: /^It changed nothing/ }),
    ).toBeChecked();
    await dialog.getByRole("button", { name: "Simulate failure" }).click();
    await details.close();
    const canvas = new CanvasPage(page);
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText("Canceled", ANSWER);
    await expect(word(canvas, "add")).toHaveText("Failed");
    await expect(word(canvas, "hold")).toHaveText("Released");
    // Never reached: only its pinned data shows.
    await expect(word(canvas, "submit")).toHaveText("1 field");
    expect(await logged(page)).toEqual([
      "hold 1 field",
      "add VALIDATION",
      "Transaction move Canceled",
      "Release of hold Released",
    ]);
  });

  test("one that finishes confirms what it reserved", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openMoveToExecute(page, moveWorkflow(STEPS, { after: SUMMARIZE }));
    const canvas = new CanvasPage(page);
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText("Completed", ANSWER);
    await expect(frameState(canvas)).toHaveAttribute("data-tone", "success");
    await expect(word(canvas, "hold")).toHaveText("Confirmed");
    for (const id of ["add", "submit"])
      await expect(word(canvas, id)).toHaveText("Done");
    await expect(word(canvas, "hold")).toHaveAttribute("data-tone", "success");
    const log = runLog(page);
    await expect(log.locator(".run-log-outcome")).toHaveCount(0);
    expect((await logged(page)).slice(2)).toEqual([
      "submit 1 field",
      "notify 0 fields",
      "Transaction move Completed",
      "Confirm of hold Confirmed",
      "summarize 1 field",
    ]);
  });

  test("one that continues on error is undone, and the run goes on", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openMoveToExecute(
      page,
      moveWorkflow(STEPS, { after: SUMMARIZE, extra: "onError: continue\n" }),
      FAILED,
    );
    const canvas = new CanvasPage(page);
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText("Undone", ANSWER);
    await expect(badge(canvas, "move")).toHaveAttribute(
      "data-badge",
      "handled",
    );
    await expect(canvas.tileBody("move")).toHaveAccessibleName(
      /failed and continued, WV-TXN-ROLLED-BACK, Undone/,
    );
    await expect(badge(canvas, "submit")).toHaveAttribute(
      "data-badge",
      "failed",
    );
    await expect(badge(canvas, "summarize")).toHaveAttribute(
      "data-badge",
      "done",
    );
    const log = runLog(page);
    await expect(log.locator(".run-log-outcome")).toHaveText(
      "The simulated run succeeded with 1 handled error.",
    );
    expect((await logged(page)).slice(2)).toEqual([
      "submit",
      "Transaction move Undone",
      "Undo of add Undone",
      "Release of hold Released",
      "move Failed, continued WV-TXN-ROLLED-BACK",
      "summarize 1 field",
    ]);
  });
});

test.describe("a simulated run's undo steps on a phone", () => {
  test.use({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
  });

  test("the run log lists them in rows a finger can take, none cut", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openMoveToExecute(page, MOVE, STUCK);
    const canvas = new CanvasPage(page);
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText(
      "In doubt · action needed",
      ANSWER,
    );
    const log = runLog(page);
    if (!(await log.count()))
      await page.getByRole("button", { name: "Run log" }).first().click();
    const rows = log.locator(".run-log-transaction, .run-log-phase");
    await expect(rows).toHaveCount(2);
    for (const row of await rows.all()) {
      expect((await row.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      for (const part of await row.locator("strong, span").all())
        expect(
          await part.evaluate(
            (element) => element.scrollWidth - element.clientWidth,
          ),
        ).toBeLessThanOrEqual(0);
    }
    expect(
      await page.evaluate(
        () =>
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      ),
    ).toBe(0);
  });
});
