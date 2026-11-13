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
// A run chosen in the Runs tab, drawn on the read-only canvas: each step's
// state in the run, the path it took, the header that names the run, the
// banner for a run of another version, a run followed while it runs, and
// no way to change the workflow from there.
import { expect, test, type Page } from "@playwright/test";
import { CanvasPage } from "./canvas-po";
import {
  listedKinds,
  openRunsTab,
  RUN_IDS,
  runFixture,
  type RunsPage,
} from "./runs-po";
import { sizes, StepDetailsPage } from "./step-details-po";
import { tokenColor } from "./support";

/** The Runs tab with `id` chosen and drawn. */
async function chooseRun(page: Page, runs: RunsPage, id: string) {
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  await runs.row(id).click();
  const canvas = new CanvasPage(page);
  await expect(canvas.root).toBeVisible();
  await expect(runs.canvas.locator(".run-header")).toContainText(
    `Run ${id.slice(0, 8)}`,
  );
  // The run is drawn once its data has loaded.
  await expect(
    canvas.root.locator('[data-badge="done"]').first(),
  ).toBeVisible();
  return canvas;
}
const sourceOf = (page: Page) =>
  page.getByRole("textbox", { name: "Workflow source" }).inputValue();

for (const size of sizes)
  test.describe(`a run on the canvas at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      contextOptions: { reducedMotion: "reduce" },
    });

    test("choosing a failed run draws it on the canvas", async ({ page }) => {
      const { runs } = await openRunsTab(page);
      const canvas = await chooseRun(page, runs, RUN_IDS.failed);
      for (const id of ["fetch", "check", "approve", "wait-for-reply"])
        await expect(canvas.tile(id).locator(".tile-badge")).toHaveAttribute(
          "data-badge",
          "done",
        );
      const send = canvas.tile("send");
      await expect(send.locator(".tile-badge")).toHaveAttribute(
        "data-badge",
        "failed",
      );
      await expect(send.locator(".tile-body")).toHaveAttribute(
        "aria-label",
        /Failed: SMTP_REJECTED/,
      );
      // The failure's code reads under the step, not only in a tooltip.
      await expect(send.locator(".tile-output")).toHaveText("SMTP_REJECTED");
      const success = await tokenColor(page, "--success");
      await expect(canvas.edgeLine("wait-for-reply>send")).toHaveClass(
        /\btaken\b/,
      );
      await expect(canvas.edgeLine("wait-for-reply>send")).toHaveCSS(
        "stroke",
        success,
      );
      // Past the failure the run went nowhere: plain, not "not taken".
      await expect(canvas.edgeLine("send>$end")).toHaveClass(/\bidle\b/);
      // A run that happened isn't the draft's to-do list: no setup or pin badges.
      await expect(canvas.root.locator('[data-badge="setup"]')).toHaveCount(0);
      await expect(canvas.root.locator(".tile-issue")).toHaveCount(0);
      // Read-only: no handles, no "+", no editing tools, and a pill that says so.
      await expect(canvas.root.locator(".handle:visible")).toHaveCount(0);
      await expect(canvas.root.locator(".insert-plus")).toHaveCount(0);
      await expect(
        canvas.root.getByRole("button", { name: "Add a sticky note" }),
      ).toHaveCount(0);
      await expect(canvas.root.getByText("Read-only")).toBeVisible();
    });

    test("the header names the run and its failure", async ({ page }) => {
      const { runs } = await openRunsTab(page);
      await chooseRun(page, runs, RUN_IDS.failed);
      const header = runs.canvas.locator(".run-header");
      await expect(header.getByRole("heading")).toHaveText("Run 1c0d7f8a");
      await expect(header.locator(".run-pill")).toHaveText("Failed");
      await expect(header).toContainText("Failed at send: SMTP_REJECTED");
      await expect(header.locator(".run-version")).toHaveText("1.3.0");
      await expect(header).toContainText("took 4 s");
      await expect(
        header.getByRole("button", { name: "Debug in editor" }),
      ).toBeVisible();
      await expect(
        header.getByRole("button", { name: "Copy to editor" }),
      ).toHaveCount(0);
      // Below 768 px, Open in Operate waits in More.
      if (size.width >= 768)
        await header.getByRole("button", { name: "Open in Operate" }).click();
      else {
        await header.getByRole("button", { name: "More" }).click();
        await page.getByRole("menuitem", { name: "Open in Operate" }).click();
      }
      await expect(page).toHaveURL(new RegExp(`/runs/${RUN_IDS.failed}$`));
    });

    test("a run on an older version shows the banner and what changed", async ({
      page,
    }) => {
      const { runs } = await openRunsTab(page);
      const canvas = await chooseRun(page, runs, RUN_IDS.older);
      const banner = runs.canvas.locator(".run-banner");
      await expect(banner).toContainText(
        "This run used 1.2.0; you're editing the draft of 1.3.0.",
      );
      await expect(banner).toContainText("Changed since this run: send.");
      await expect(banner).toContainText("New in this draft: check.");
      await expect(banner).toContainText("Not in this draft: log.");
      // On a phone the banner first reads in one line.
      if (size.width < 768)
        await expect(banner.locator("summary")).toHaveText(
          "This run used 1.2.0. 3 steps differ.Details",
        );
      await expect(canvas.tile("send").locator(".tile-badge")).toHaveAttribute(
        "data-badge",
        "changed",
      );
      await expect(canvas.tile("send").locator(".tile-output")).toHaveText(
        "Changed since this run",
      );
      await expect(canvas.tile("check").locator(".tile-badge")).toHaveAttribute(
        "data-badge",
        "added",
      );
      await expect(
        runs.canvas.locator(".run-header .run-version"),
      ).toHaveAttribute("data-older", "");
      // A run that didn't fail is copied, not debugged.
      await expect(
        runs.canvas.getByRole("button", { name: "Copy to editor" }),
      ).toBeVisible();
    });

    test("a test run says it used a draft", async ({ page }) => {
      const { runs } = await openRunsTab(page);
      await chooseRun(page, runs, RUN_IDS.test);
      await expect(runs.canvas.locator(".run-banner")).toHaveText(
        "This test run used a draft of 1.3.0.",
      );
      await expect(runs.canvas.locator(".run-header")).toContainText("Test");
    });

    test("nothing changes the workflow in the Runs tab", async ({ page }) => {
      const { runs, platform } = await openRunsTab(page);
      const executions: string[] = [];
      page.on("request", (request) => {
        if (request.url().includes("/studio/local/debug/"))
          executions.push(request.url());
      });
      await runs.tabs.getByRole("tab", { name: "Source" }).click();
      const before = await sourceOf(page);
      await runs.tabs.getByRole("tab", { name: "Runs" }).click();
      const canvas = await chooseRun(page, runs, RUN_IDS.failed);
      // The run is framed on the step that failed.
      await canvas.tileBody("send").click();
      for (const key of [
        "Delete",
        "Backspace",
        "ControlOrMeta+d",
        "ControlOrMeta+c",
        "ControlOrMeta+v",
        "ControlOrMeta+x",
        "ControlOrMeta+z",
        "ControlOrMeta+Enter",
      ])
        await page.keyboard.press(key);
      await expect(
        page.getByRole("button", { name: "Add a step", exact: true }),
      ).toHaveCount(0);
      // Nor the workflow settings, which edit.
      await expect(
        page.getByRole("button", { name: "Open workflow settings" }),
      ).toHaveCount(0);
      await expect(canvas.root.locator("[data-toolbar-for]")).toHaveCount(0);
      // Nor step details: no pin, unpin or output to edit.
      const details = new StepDetailsPage(page);
      await details.openWithKeyboard("fetch");
      await expect(
        details.dialog.getByRole("button", {
          name: /^(Pin output|Unpin output|Edit output|Keep pinned|Unpin)$/,
        }),
      ).toHaveCount(0);
      await details.close();
      await runs.tabs.getByRole("tab", { name: "Source" }).click();
      expect(await sourceOf(page)).toBe(before);
      expect(executions).toEqual([]);
      // The run was read, never written.
      expect(
        platform.requests.every((url) => !url.pathname.endsWith("/execute")),
      ).toBe(true);
    });

    test("Back to editing returns to the canvas with the workflow editable", async ({
      page,
    }) => {
      const { runs } = await openRunsTab(page);
      const canvas = await chooseRun(page, runs, RUN_IDS.failed);
      const back = runs.canvas.getByRole("button", { name: "Back to editing" });
      if (!(await back.isVisible()))
        await runs.canvas.getByRole("button", { name: "More" }).click();
      await page
        .getByRole("button", { name: "Back to editing" })
        .or(page.getByRole("menuitem", { name: "Back to editing" }))
        .first()
        .click();
      await expect(
        runs.tabs.getByRole("tab", { name: "Designer" }),
      ).toHaveAttribute("aria-selected", "true");
      // The draft is drawn without the run, and editable again.
      await expect(canvas.root.locator('[data-badge="failed"]')).toHaveCount(0);
      await canvas.tileBody("check").click();
      await page.keyboard.press("Delete");
      await expect(canvas.tile("check")).toHaveCount(0);
      await page.keyboard.press("ControlOrMeta+z");
      await expect(canvas.tile("check")).toHaveCount(1);
    });
  });

test.describe("a run followed while it runs", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a running run is followed until it ends", async ({ page }) => {
    const { runs, platform } = await openRunsTab(page);
    // Opening the tab chose the newest run: the running one.
    const header = runs.canvas.locator(".run-header");
    await expect(header).toContainText("Following this run");
    const canvas = new CanvasPage(page);
    await expect(canvas.tile("check").locator(".tile-shape")).toHaveClass(
      /\blive\b/,
    );
    // Without reduced motion the running step pulses.
    await expect(canvas.tile("check").locator(".tile-shape")).toHaveClass(
      /\bpulse\b/,
    );
    // How long it has been at work reads under it.
    await expect(canvas.tile("check").locator(".tile-output")).toHaveText(
      /^Running for \d+ s$/,
    );
    const reads = () =>
      platform.requests.filter((url) =>
        url.pathname.endsWith(`/runs/${RUN_IDS.running}`),
      ).length;
    const first = reads();
    // The run moves on: check finished, then the run ends.
    const running = platform.runs.find(
      (run) => run.summary.id === RUN_IDS.running,
    )!;
    const done = runFixture("succeeded");
    running.view = {
      ...done.view,
      id: RUN_IDS.running,
    };
    running.facts = done.facts;
    await page.clock.runFor(2_000);
    await expect.poll(reads).toBeGreaterThan(first);
    await expect(canvas.tile("check").locator(".tile-badge")).toHaveAttribute(
      "data-badge",
      "done",
    );
    await expect(header).not.toContainText("Following this run");
    const ended = reads();
    await page.clock.runFor(10_000);
    expect(reads()).toBe(ended);
  });

  test("Escape on the canvas returns to the run's row", async ({ page }) => {
    const { runs } = await openRunsTab(page);
    const canvas = await chooseRun(page, runs, RUN_IDS.failed);
    await canvas.tileBody("fetch").focus();
    await page.keyboard.press("Escape");
    await expect(runs.row(RUN_IDS.failed)).toBeFocused();
  });

  test("with no run chosen, the draft stays drawn behind a note", async ({
    page,
  }) => {
    // Only a run the platform can't show: nothing to choose on opening.
    const { runs } = await openRunsTab(page, { runs: [] });
    await expect(runs.rows).toHaveCount(1);
    const note = runs.canvas.getByRole("note");
    await expect(note).toContainText("Choose a run to see it on the canvas.");
    await expect(note).toContainText("Use the list, or ↑ and ↓ then Enter.");
    const canvas = new CanvasPage(page);
    await expect(canvas.tile("send")).toBeVisible();
    await expect(canvas.root.locator(".tile-badge")).toHaveCount(0);
    await expect(runs.canvas.locator(".run-header")).toHaveCount(0);
  });
});

for (const size of [
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
])
  test.describe(`the run's framing at ${size.width}x${size.height}`, () => {
    test.use({
      viewport: size,
      contextOptions: { reducedMotion: "reduce" },
    });

    test("the run is framed on what is drawn, with the failed step in view", async ({
      page,
    }) => {
      const { runs } = await openRunsTab(page);
      const canvas = await chooseRun(page, runs, RUN_IDS.failed);
      await expect(canvas.tile("send").locator(".tile-badge")).toHaveAttribute(
        "data-badge",
        "failed",
      );
      // The frame settles after the run is drawn.
      await expect
        .poll(async () =>
          canvas.root.evaluate((root) => {
            const frame = root.getBoundingClientRect();
            const drawn = [
              ...root.querySelectorAll(
                ".tile-node .tile-body, .tile-node .tile-label, .tile-node .tile-badge",
              ),
            ].map((element) => element.getBoundingClientRect());
            const left = Math.min(...drawn.map((rect) => rect.left));
            const right = Math.max(...drawn.map((rect) => rect.right));
            const send = root
              .querySelector('.tile-node[data-tile="send"] .tile-body')!
              .getBoundingClientRect();
            return {
              sendInView: send.left >= frame.left && send.right <= frame.right,
              // Room left empty on either side while the path goes on past
              // the other edge, or how far off center a path that fits sits.
              emptyRight: Math.round(frame.right - right),
              emptyLeft: Math.round(left - frame.left),
              cutLeft: Math.round(Math.max(0, frame.left - left)),
            };
          }),
        )
        .toMatchObject({ sendInView: true });
      const measured = await canvas.root.evaluate((root) => {
        const frame = root.getBoundingClientRect();
        const drawn = [
          ...root.querySelectorAll(
            ".tile-node .tile-body, .tile-node .tile-label, .tile-node .tile-badge",
          ),
        ].map((element) => element.getBoundingClientRect());
        const tiles = [...root.querySelectorAll(".tile-node .tile-body")].map(
          (element) => element.getBoundingClientRect(),
        );
        return {
          left: Math.min(...drawn.map((rect) => rect.left)) - frame.left,
          right: frame.right - Math.max(...drawn.map((rect) => rect.right)),
          // How much of the first tile is past the left edge.
          tileCut: frame.left - Math.min(...tiles.map((rect) => rect.left)),
        };
      });
      if (size.width === 1440) {
        // Wider than the column at 60%: nothing left empty on the right,
        // and only a little of the path's start is cut off.
        expect(measured.right).toBeLessThanOrEqual(24);
        expect(measured.tileCut).toBeLessThanOrEqual(60);
      } else {
        // It fits: centered across.
        expect(Math.abs(measured.left - measured.right)).toBeLessThanOrEqual(
          24,
        );
        expect(measured.left).toBeGreaterThan(0);
      }
    });
  });
