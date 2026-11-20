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
// A loop in a recorded run on the Runs tab: the item chip above its frame
// says which item the steps inside show, the failed one first; Previous,
// Next and the item list choose another; a finished loop says how many
// items it had under its frame.
import { expect, test, type Page } from "@playwright/test";
import type { StepFact } from "../../src/app/operate/run-contracts";
import {
  openRunsTab,
  runFixture,
  RUN_IDS,
  type RunFixture,
  type RunsPlatform,
} from "./runs-po";
import { StepDetailsPage } from "./step-details-po";
import { tokenColor } from "./support";

const ROWS = ["Ada", "Bo", "Cy", "Di", "Ed"].map((name) => ({ name }));
/** invoice-approval with a loop: fetch, then send for each row of its output. */
const loopWorkflow = (collect = "all") => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "invoice-approval", version: "1.3.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      { id: "fetch", kind: "transform", value: { literal: { rows: ROWS } } },
      {
        id: "orders",
        kind: "forEach",
        items: { ref: "/steps/fetch/output/rows" },
        concurrency: 1,
        maxItems: 1000,
        collect,
        body: {
          steps: [
            { id: "check", kind: "transform", value: { ref: "/item" } },
            {
              id: "send",
              kind: "transform",
              value: { object: { to: { ref: "/item/name" } } },
            },
          ],
          output: { ref: "/steps/send/output" },
        },
      },
    ],
    output: { literal: {} },
  },
});

function fact(key: string, status: StepFact["status"]): StepFact {
  return {
    node_id: key.replace(/\[.*$/, ""),
    instance_key: key,
    iteration: [...key.matchAll(/\[(\d+)\]/g)].map((match) => Number(match[1])),
    kind: "transform",
    status,
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: null,
    error_code: status === "failed" ? "SMTP_REJECTED" : null,
    child_run_id: null,
    log_entries: 0,
  };
}
const items = (upTo: number, last: StepFact["status"]) =>
  Array.from({ length: upTo }, (_, i) => [
    fact(`check[${i}]`, "succeeded"),
    fact(`send[${i}]`, i === upTo - 1 ? last : "succeeded"),
  ]).flat();

/** The loop run: items 1 to 3 sent, item 4 failed at send, item 5 never ran. */
function failedRun(): RunFixture {
  const base = runFixture("loop");
  const state = base.view["state"] as Record<string, unknown>;
  return {
    ...base,
    summary: {
      ...base.summary,
      failed_step: {
        node_id: "send",
        instance_key: "send[3]",
        error_code: "SMTP_REJECTED",
      },
    },
    view: {
      ...base.view,
      state: {
        ...state,
        steps: { fetch: { output: { rows: ROWS } } },
        loops: { orders: { owner: "orders", count: 5 } },
        incidents: {
          "send[3]:0": { node_id: "send[3]", code: "SMTP_REJECTED" },
        },
      },
    },
    facts: [
      fact("fetch", "succeeded"),
      ...items(4, "failed"),
      fact("orders", "failed"),
    ],
  };
}
/** The same loop finished, Skip empty results leaving two of five out. */
function finishedRun(): RunFixture {
  const base = runFixture("loop");
  const state = base.view["state"] as Record<string, unknown>;
  return {
    ...base,
    summary: { ...base.summary, status: "succeeded", failed_step: null },
    view: {
      ...base.view,
      state: {
        ...state,
        status: "succeeded",
        steps: {
          fetch: { output: { rows: ROWS } },
          orders: { output: [{ to: "Ada" }, { to: "Cy" }, { to: "Ed" }] },
        },
        loops: { orders: { owner: "orders", count: 5 } },
        incidents: {},
      },
    },
    facts: [
      fact("fetch", "succeeded"),
      ...items(5, "succeeded"),
      fact("orders", "succeeded"),
    ],
  };
}

/** The loop cancelled while items 2 and 3 ran: they were cut off. */
function cancelledRun(): RunFixture {
  const base = runFixture("loop");
  const state = base.view["state"] as Record<string, unknown>;
  return {
    ...base,
    summary: { ...base.summary, status: "cancelled", failed_step: null },
    view: {
      ...base.view,
      state: {
        ...state,
        status: "cancelled",
        steps: { fetch: { output: { rows: ROWS } } },
        loops: { orders: { owner: "orders", count: 5 } },
        incidents: {},
      },
    },
    facts: [
      fact("fetch", "succeeded"),
      ...items(1, "succeeded"),
      fact("check[1]", "succeeded"),
      fact("send[1]", "cancelled"),
      fact("check[2]", "cancelled"),
      fact("orders", "cancelled"),
    ],
  };
}

/** The Runs tab on the loop run, its version answered as the loop workflow. */
async function openLoopRun(
  page: Page,
  run: RunFixture,
  collect = "all",
  workflow: object = loopWorkflow(collect),
) {
  const opened = await openRunsTab(page, {
    runs: [run],
    workflow,
    before: async (platform: RunsPlatform) => {
      await page.route("**/projects/*/workflows/*/export", (route) => {
        platform.requests.push(new URL(route.request().url()));
        return route.fulfill({
          json: { id: run.versionId, document: workflow },
        });
      });
    },
  });
  await opened.runs.row(RUN_IDS.loop).click();
  const chip = page.locator('.item-chip-spot[data-chip="orders"]');
  return { ...opened, chip };
}
/** A step's corner badge on the canvas ("failed", "done"), or null without one. */
const tileState = (page: Page, id: string) =>
  page
    .locator(`.canvas-v2 .tile-node[data-tile="${id}"]`)
    .evaluate(
      (node) =>
        node.querySelector(".tile-badge")?.getAttribute("data-badge") ?? null,
    );

test.describe("A recorded loop at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the chip opens on the failed item; Previous and Next show the others in the steps inside", async ({
    page,
  }) => {
    const { chip } = await openLoopRun(page, failedRun());
    const item = chip.getByRole("button", { name: /Choose an item$/ });
    await expect(item).toHaveAccessibleName(
      "Item 4 of 5, Failed. Choose an item",
    );
    await expect.poll(() => tileState(page, "send")).toBe("failed");
    // The run's header and its row in the list name the item.
    await expect(
      page.getByText("Failed at send, item 4: SMTP_REJECTED"),
    ).toBeVisible();
    await expect(
      page.locator(`.runs-list [data-run="${RUN_IDS.loop}"]`),
    ).toContainText("at send, item 4");
    // Above the frame's top right, at screen size.
    const frame = page.locator('.loop-frame[data-frame="orders"]');
    const spot = (await chip.locator(".item-chip").boundingBox())!;
    const box = (await frame.boundingBox())!;
    expect(Math.abs(spot.x + spot.width - (box.x + box.width))).toBeLessThan(2);
    expect(spot.y + spot.height).toBeLessThanOrEqual(box.y);
    expect(Math.round(spot.height)).toBe(34);

    await chip.getByRole("button", { name: "Previous item" }).click();
    await expect(item).toHaveAccessibleName(
      "Item 3 of 5, Finished. Choose an item",
    );
    await expect.poll(() => tileState(page, "send")).toBe("done");
    // The frame keeps the run's state, and its header the item it failed at.
    await expect(frame).toHaveClass(/\berror\b/);
    const head = page.locator('.canvas-v2 .tile-node[data-tile="orders"]');
    await expect(head.locator(".frame-run")).toHaveText("· failed at item 4");
    // The code shows on the failed step, not under the frame.
    await expect(head.locator(".tile-output")).toHaveCount(0);
    await chip.getByRole("button", { name: "Next item" }).click();
    await chip.getByRole("button", { name: "Next item" }).click();
    await expect(item).toHaveAccessibleName(
      "Item 5 of 5, Not run. Choose an item",
    );
    await expect.poll(() => tileState(page, "send")).toBeNull();
    await expect(
      chip.getByRole("button", { name: "Next item" }),
    ).toHaveAttribute("aria-disabled", "true");
  });

  test("the list goes to an item by number or by row, the failed ones first, from the keyboard", async ({
    page,
  }) => {
    const { chip } = await openLoopRun(page, failedRun());
    const item = chip.getByRole("button", { name: /Choose an item$/ });
    await item.focus();
    await page.keyboard.press("Enter");
    const list = page.getByRole("dialog", { name: "Items of orders" });
    await expect(list).toBeVisible();
    // Focus starts on the chosen item, under Failed.
    const options = list.getByRole("option");
    await expect(options.first()).toBeFocused();
    await expect(options.first()).toContainText("Item 4");
    await expect(options.first()).toContainText("SMTP_REJECTED");
    await expect(options.first()).toContainText("Failed");
    await expect(options).toHaveCount(6);
    // Go to item is 64 × 32, as the list's other controls.
    const field = (await list.getByRole("textbox").boundingBox())!;
    expect([Math.round(field.width), Math.round(field.height)]).toEqual([
      64, 32,
    ]);
    await page.keyboard.press("ArrowDown");
    await expect(options.nth(1)).toBeFocused();
    await expect(options.nth(1)).toContainText("Finished");
    await page.keyboard.press("Escape");
    await expect(list).toBeHidden();
    await expect(item).toBeFocused();
    // A digit goes to Go to item.
    await page.keyboard.press("ArrowDown");
    await expect(options.first()).toBeFocused();
    await page.keyboard.press("2");
    await page.keyboard.press("Enter");
    await expect(list).toBeHidden();
    await expect(item).toHaveAccessibleName(
      "Item 2 of 5, Finished. Choose an item",
    );
    await expect(item).toBeFocused();
    // A row chooses its item.
    await item.click();
    await list.getByRole("option", { name: /Item 5/ }).click();
    await expect(item).toHaveAccessibleName(
      "Item 5 of 5, Not run. Choose an item",
    );
  });

  test("a finished loop says how many items it had and how many results it left out", async ({
    page,
  }) => {
    await openLoopRun(page, finishedRun(), "nonNull");
    const tile = page.locator('.canvas-v2 .tile-node[data-tile="orders"]');
    await expect(tile.locator(".tile-output")).toHaveText("5 items, 2 skipped");
    // Its header counts the items, as Execute does, with no badge of its own.
    await expect(tile.locator(".frame-run")).toHaveText("· 5 of 5");
    await expect(tile.locator(".frame-run")).toHaveAttribute(
      "data-tone",
      "done",
    );
    await expect(tile.locator(".tile-badge")).toHaveCount(0);
  });
});

test.describe("A recorded loop's step details at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("recompute what the failed item's step received from the run", async ({
    page,
  }) => {
    // send maps its input from the current item, as an action does.
    const workflow = JSON.parse(
      JSON.stringify(loopWorkflow()).replace(
        '"id":"send","kind":"transform","value":',
        '"id":"send","kind":"action","uses":"mail.send@1.0.0","with":',
      ),
    );
    await openLoopRun(page, failedRun(), "all", workflow);
    const details = new StepDetailsPage(page);
    await details.open("send");
    // Item 4 failed at send: its input, from the run's own data.
    await expect(
      details.dialog.locator("weave-input-pane .pane-received"),
    ).toContainText('"to": "Di"');
  });
});

test.describe("A recorded loop's item in the chip and in step details at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the chip and step details show one item: each follows the other", async ({
    page,
  }) => {
    // Each item's send kept what it returned.
    const run = failedRun();
    run.facts = run.facts.map((each) =>
      each.node_id === "send" && each.status === "succeeded"
        ? { ...each, output: { to: ROWS[each.iteration[0]].name } }
        : each,
    );
    const { chip } = await openLoopRun(page, run);
    const item = chip.getByRole("button", { name: /Choose an item$/ });
    await chip.getByRole("button", { name: "Previous item" }).click();
    await expect(item).toHaveAccessibleName(
      "Item 3 of 5, Finished. Choose an item",
    );
    const details = new StepDetailsPage(page);
    await details.open("send");
    // The run's Item select, beside the run chip.
    const select = details.dialog.getByRole("combobox", { name: /^Item\b/ });
    await expect(select.locator("option:checked")).toHaveText(
      "Item 3 of 5 · Finished",
    );
    const output = details.dialog.locator("weave-output-pane");
    await output.getByRole("radio", { name: "JSON" }).click();
    await expect(output.locator(".pane-json")).toContainText('"Cy"');
    // Another item chosen in step details moves the chip.
    await select.selectOption({ label: "Item 1 of 5 · Finished" });
    await expect(output.locator(".pane-json")).toContainText('"Ada"');
    await expect(item).toHaveAccessibleName(
      "Item 1 of 5, Finished. Choose an item",
    );
    // The step before it in the loop opens on the same item.
    await details.close();
    await details.open("check");
    await expect(select.locator("option:checked")).toHaveText(
      "Item 1 of 5 · Finished",
    );
  });
});

test.describe("A cancelled loop at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the items the run cut off read Stopped, and nothing reads Failed", async ({
    page,
  }) => {
    const { chip } = await openLoopRun(page, cancelledRun());
    // The chip opens on the first item the run cut off.
    const item = chip.getByRole("button", { name: /Choose an item$/ });
    await expect(item).toHaveAccessibleName(
      "Item 2 of 5, Stopped. Choose an item",
    );
    // The header says where the run stopped, quietly: it wasn't a failure.
    const run = page.locator(
      '.canvas-v2 .tile-node[data-tile="orders"] .frame-run',
    );
    await expect(run).toHaveText("· canceled at item 2");
    expect(
      await run.evaluate((element) => getComputedStyle(element).color),
    ).toBe(await tokenColor(page, "--muted"));
    await item.click();
    const list = page.getByRole("dialog", { name: "Items of orders" });
    const options = list.getByRole("option");
    await expect(options).toHaveText([
      /Item 1\s*Finished/,
      /Item 2\s*Stopped/,
      /Item 3\s*Stopped/,
      /Item 4\s*Not run/,
      /Item 5\s*Not run/,
    ]);
    await expect(list.getByText("Failed", { exact: true })).toHaveCount(0);
    // Not run is the dashed circle; Stopped keeps the cancelled run's icon; both neutral.
    const icon = (n: number) => options.nth(n).locator(".item-status");
    await expect(icon(1)).toHaveAttribute("data-icon", "off");
    await expect(icon(3)).toHaveAttribute("data-icon", "typeAny");
    const neutral = await tokenColor(page, "--neutral-ink");
    for (const n of [1, 3])
      expect(
        await icon(n).evaluate((element) => getComputedStyle(element).color),
      ).toBe(neutral);
  });
});

test.describe("A recorded loop on a phone", () => {
  test.use({ viewport: { width: 360, height: 780 } });

  test("step details put the run and its Item select on their own row, readable", async ({
    page,
  }) => {
    const { chip } = await openLoopRun(page, failedRun());
    // Item 3 from the chip's sheet, then send's step details on it.
    await chip.getByRole("button", { name: /Choose an item$/ }).click();
    await page
      .getByRole("dialog", { name: "Items of orders" })
      .getByRole("option", { name: /Item 3/ })
      .click();
    const details = new StepDetailsPage(page);
    await details.openWithKeyboard("send");
    const select = details.dialog.getByRole("combobox", { name: /^Item\b/ });
    await expect(select.locator("option:checked")).toHaveText(
      "Item 3 of 5 · Finished",
    );
    const shown = (await select.boundingBox())!;
    const close = (await details.dialog
      .getByRole("button", { name: "Close", exact: true })
      .boundingBox())!;
    // At least 12em wide and a touch target tall, under the step arrows and Close.
    expect(shown.width).toBeGreaterThanOrEqual(150);
    expect(shown.height).toBeGreaterThanOrEqual(44);
    expect(shown.y).toBeGreaterThanOrEqual(close.y + close.height);
    // Whole on the screen, its label included.
    expect(shown.x + shown.width).toBeLessThanOrEqual(360);
    expect(
      await select.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
    ).toBe(true);
  });

  test("the chip is one button that opens the items as a sheet", async ({
    page,
  }) => {
    const { chip } = await openLoopRun(page, failedRun());
    await expect(
      chip.getByRole("button", { name: "Previous item" }),
    ).toBeHidden();
    const item = chip.getByRole("button", { name: /Choose an item$/ });
    expect(Math.round((await item.boundingBox())!.height)).toBe(44);
    await item.click();
    const sheet = page.getByRole("dialog", { name: "Items of orders" });
    await expect(sheet).toBeVisible();
    await expect(
      sheet.getByText("Items of orders", { exact: true }),
    ).toBeVisible();
    const box = (await sheet.boundingBox())!;
    expect(Math.round(box.width)).toBe(360);
    await sheet.getByRole("button", { name: "Close", exact: true }).click();
    await expect(sheet).toBeHidden();
    await expect(item).toBeFocused();
  });
});
