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
// Alignment of the decision table grid, measured: its density and columns,
// the scroll box and what stays put in it, Try an input beside the grid or
// under it, its controls' targets and the rails beside it, at desktop and
// narrow sizes and on touch.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { connected } from "./support";
import {
  openStepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import {
  DecisionGridPage,
  ordersWorkflow,
  paymentPolicy,
  routeTables,
  type TableDocumentFixture,
} from "./decision-grid-po";
import { hasPython, localAuthoring } from "./local-authoring";
import {
  box,
  boxes,
  clippedText,
  density,
  expectAligned,
  intersects,
  type Box,
} from "./step-geometry";

const SIZES = [
  { tag: "1440x900", use: { viewport: { width: 1440, height: 900 } } },
  { tag: "600x500", use: { viewport: { width: 600, height: 500 } } },
  { tag: "360x640", use: { viewport: { width: 360, height: 640 } } },
  {
    tag: "600x500 touch",
    use: {
      viewport: { width: 600, height: 500 },
      hasTouch: true,
      isMobile: true,
    },
  },
];

const TEXT = [
  "tier",
  "region",
  "channel",
  "country",
  "segment",
  "language",
  "device",
  "partner",
  "currency",
];

/** payment-policy with twelve input columns, four results and thirty rules: wider and taller than its box. */
function wideTable(version: string): TableDocumentFixture {
  const base = paymentPolicy(version);
  const properties: Record<string, unknown> = {
    amount: { type: "number" },
    score: { type: "integer" },
    verified: { type: "boolean" },
  };
  for (const name of TEXT) properties[name] = { type: "string" };
  const result = (i: number) => ({
    approve: i % 2 === 0,
    limit: i * 100,
    reason: `region ${i}`,
    review: false,
  });
  return {
    ...base,
    spec: {
      ...base.spec,
      inputSchema: { type: "object", properties, required: ["amount"] },
      outputSchema: {
        type: "object",
        properties: {
          approve: { type: "boolean" },
          limit: { type: "number" },
          reason: { type: "string" },
          review: { type: "boolean" },
        },
        required: ["approve"],
      },
      rules: Array.from({ length: 30 }, (_, i) => ({
        id: `region-${i + 1}`,
        when: {
          op: {
            name: "eq",
            args: [{ ref: "/input/region" }, { literal: `r${i + 1}` }],
          },
        },
        output: { literal: result(i + 1) },
      })),
      defaultOutput: { literal: { ...result(0), review: true } },
    },
  };
}

/** The orders workflow connected, its step's details, and the grid on `table` (version 1.2.0). */
async function openGrid(page: Page, table = paymentPolicy("1.2.0")) {
  const details = await openStepFixture(page, ordersWorkflow, async (p) => {
    const result = await connected(p);
    await withLanguageFeatures(p);
    return result;
  });
  await routeTables(page, [table]);
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  return { details, grid };
}

/** The wide table as a draft on this computer, offline, with Try an input open and checked by the host. */
async function openTry(page: Page) {
  const document = wideTable("1.2.1");
  await page.addInitScript(
    (value) =>
      localStorage.setItem("local:weave.tableDraft.payment-policy", value),
    JSON.stringify({
      kind: "draft",
      name: "payment-policy",
      version: "1.2.1",
      base: "1.2.0",
      document,
      savedAt: "2026-10-27T08:00:00.000Z",
    }),
  );
  const workflow = yamlFile(
    "orders.yaml",
    ordersWorkflow.buffer.toString().replace("@1.2.0", "@1.2.1"),
  );
  const details = await openStepFixture(page, workflow);
  await localAuthoring(page);
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  await grid.root.getByRole("button", { name: "Try an input" }).click();
  const panel = grid.root.getByRole("region", { name: "Try an input" });
  await expect(panel).toBeVisible();
  await panel.getByLabel("amount").fill("100");
  await panel.getByLabel("region").fill("r2");
  await expect(
    panel.locator(".pill", { hasText: "Checked by Weave" }),
  ).toBeVisible();
  return { details, grid, panel };
}

/** The page never scrolls sideways. */
async function expectNoPageScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [
    document.scrollingElement!.scrollWidth,
    innerWidth,
  ]);
  expect(scroll, "the page's width").toBeLessThanOrEqual(width);
}

/**
 * Every shown control in `scope` is at least `target` wide and tall. A choice
 * field counts as one target: its text box and its chevron both open it.
 */
async function expectTargets(scope: Locator, target: number, what: string) {
  const controls = await scope.evaluate((root) =>
    [
      ...root.querySelectorAll<HTMLElement>(
        'button, input, .choice-field, [role="gridcell"][tabindex="0"]',
      ),
    ]
      .filter(
        (e) =>
          e.getClientRects().length > 0 &&
          !e.parentElement?.closest(".choice-field"),
      )
      .map((e) => {
        const r = e.getBoundingClientRect();
        const name =
          e.getAttribute("aria-label") || e.innerText.trim() || e.tagName;
        return { name: name.slice(0, 40), width: r.width, height: r.height };
      }),
  );
  expect(controls.length, `${what} has controls`).toBeGreaterThan(0);
  for (const c of controls) {
    expect(c.width, `${what} "${c.name}" width`).toBeGreaterThanOrEqual(
      target - 0.5,
    );
    expect(c.height, `${what} "${c.name}" height`).toBeGreaterThanOrEqual(
      target - 0.5,
    );
  }
}

const phone = (page: Page) => page.viewportSize()!.width < 768;

for (const size of SIZES)
  test.describe(`The decision grid at ${size.tag}`, () => {
    test.use(size.use);

    test("rows, cells and toolbar controls follow the density, and every column lines up", async ({
      page,
    }) => {
      const { grid } = await openGrid(page);
      const { row, control } = await density(page);
      for (const cell of await boxes(grid.root.locator(".dg-th, .dg-td")))
        expect(cell.height, "cell height").toBeCloseTo(row, 0);
      const tools = await boxes(
        grid.root.locator("button.dg-tool, .dg-policy .choice-field"),
      );
      expect(tools.length, "toolbar controls").toBe(5);
      for (const tool of tools)
        expect(tool.height, "toolbar control height").toBeCloseTo(control, 0);
      // A tap anywhere inside the policy's field opens it: its text box
      // fills the field inside the border.
      const field = await box(grid.root.locator(".dg-policy .choice-field"));
      const text = await box(grid.root.locator('.dg-policy [role="combobox"]'));
      expect(text.height, "the policy's text box").toBeGreaterThanOrEqual(
        field.height - 2.5,
      );

      const headers = await boxes(grid.root.locator(".dg-header > .dg-th"));
      expectAligned(
        headers.map((h) => h.y),
        "header tops",
      );
      const [mark, name, ...columns] = headers;
      expect(mark.width, "# column").toBeCloseTo(56, 0);
      expect(name.width, "Rule name column").toBeGreaterThanOrEqual(159.5);
      expect(name.width, "Rule name column").toBeLessThanOrEqual(
        phone(page) ? 160.5 : 240.5,
      );
      for (const column of columns) {
        expect(column.width, "column width").toBeGreaterThanOrEqual(119.5);
        expect(column.width, "column width").toBeLessThanOrEqual(320.5);
      }
      for (let index = 1; index <= headers.length; index++)
        expectAligned(
          (await boxes(grid.root.locator(`[aria-colindex="${index}"]`))).map(
            (cell) => cell.x,
          ),
          `column ${index}'s left edges`,
        );

      // Two input columns, then two result columns.
      const [inputs, results] = await boxes(
        grid.root.locator(".dg-group:not(.dg-group-start)"),
      );
      expectAligned([inputs.x, columns[0].x], "Inputs starts at amount");
      expectAligned([inputs.right, columns[1].right], "Inputs ends at tier");
      expectAligned([results.x, columns[2].x], "Results starts at approve");
      expectAligned([results.right, columns[3].right], "Results ends at limit");
    });

    test("the grid scrolls inside its box, never the page, and its header and rule numbers stay put", async ({
      page,
    }) => {
      const { grid } = await openGrid(page, wideTable("1.2.0"));
      const scroller = grid.root.locator(".dg-scroll");
      const room = await scroller.evaluate((e) => ({
        x: e.scrollWidth - e.clientWidth,
        y: e.scrollHeight - e.clientHeight,
      }));
      expect(room.x, "room to scroll sideways").toBeGreaterThanOrEqual(400);
      expect(room.y, "room to scroll down").toBeGreaterThanOrEqual(300);
      await expectNoPageScroll(page);

      const corner = grid.root.locator(".dg-header > .dg-mark-cell");
      const heading = grid.root.locator('.dg-header > [aria-colindex="4"]');
      const number = grid.root.locator(
        '.dg-row[aria-rowindex="2"] > .dg-mark-cell',
      );
      const name = grid.cell(0, 0);
      const before = {
        corner: await box(corner),
        heading: await box(heading),
        number: await box(number),
        name: await box(name),
      };
      await scroller.evaluate((e) => {
        e.scrollLeft = 400;
        e.scrollTop = 300;
      });
      await expect
        .poll(() => scroller.evaluate((e) => [e.scrollLeft, e.scrollTop]))
        .toEqual([400, 300]);
      const after = {
        corner: await box(corner),
        heading: await box(heading),
        number: await box(number),
        name: await box(name),
      };
      expectAligned([after.corner.x, before.corner.x], "# stays left", 0.5);
      expectAligned([after.corner.y, before.corner.y], "# stays on top", 0.5);
      expectAligned([after.heading.y, before.heading.y], "header stays", 0.5);
      expectAligned(
        [after.heading.x, before.heading.x - 400],
        "a column's header scrolls sideways",
        0.5,
      );
      expectAligned(
        [after.number.x, before.number.x],
        "rule numbers stay",
        0.5,
      );
      expectAligned(
        [after.number.y, before.number.y - 300],
        "rule numbers scroll down",
        0.5,
      );
      // On a phone only the numbers stay, so the cell being edited keeps the room.
      expectAligned(
        [after.name.x, before.name.x - (phone(page) ? 400 : 0)],
        "rule names",
        0.5,
      );
      await expectNoPageScroll(page);
    });

    test("Try an input sits beside the grid on a desktop and under it on a phone", async ({
      page,
    }) => {
      test.skip(!hasPython, "Create the Python development environment first.");
      const { grid, panel } = await openTry(page);
      const [scroll, tried, toolbar] = [
        await box(grid.root.locator(".dg-scroll")),
        await box(panel),
        await box(grid.root.getByRole("toolbar", { name: "Table commands" })),
      ];
      expect(intersects(scroll, tried), "Try an input covers the grid").toBe(
        false,
      );
      if (phone(page)) {
        expect(tried.y, "under the grid").toBeGreaterThanOrEqual(
          scroll.bottom - 0.5,
        );
        expectAligned([tried.x, toolbar.x], "left edges");
        expectAligned([tried.right, toolbar.right], "right edges");
      } else {
        expect(tried.x, "beside the grid").toBeGreaterThanOrEqual(
          scroll.right - 0.5,
        );
        expectAligned([tried.right, toolbar.right], "right edges");
      }
      expect(await clippedText(panel, "")).toEqual([]);
      const { target } = await density(page);
      await expectTargets(panel, target, "Try an input");
      await expectNoPageScroll(page);
    });

    test("the header, the problems line and every control keep their text whole and their targets", async ({
      page,
    }) => {
      const { grid } = await openGrid(page);
      await grid.edit(0, 1, ">> 1000");
      const problems = grid.root.locator(".dg-problems");
      await expect(problems).toContainText(">> isn't a test");
      expect(await clippedText(grid.root, ".dg-header")).toEqual([]);
      expect(await clippedText(grid.root, ".dg-problems")).toEqual([]);
      expect(await clippedText(grid.root, ".dg-toolbar")).toEqual([]);
      const { target } = await density(page);
      await expectTargets(grid.root, target, "the grid");
    });
  });

test.describe("The rails beside the grid at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("each rail runs the full height of the pane it folds", async ({
    page,
  }) => {
    const details = await openStepFixture(page, ordersWorkflow, async (p) => {
      const result = await connected(p);
      await withLanguageFeatures(p);
      return result;
    });
    await routeTables(page, [paymentPolicy("1.2.0")]);
    await details.open("score");
    const panes = [
      await box(details.dialog.locator(".sd-input")),
      await box(details.dialog.locator(".sd-output")),
    ];
    await new DecisionGridPage(page).open();
    const rails: Box[] = [
      await box(details.dialog.locator(".sd-rail-input")),
      await box(details.dialog.locator(".sd-rail-output")),
    ];
    const main = await box(details.dialog.locator(".sd-main"));
    rails.forEach((rail, index) => {
      expect(Math.abs(rail.width - 48), "rail width").toBeLessThanOrEqual(0.5);
      expectAligned([rail.y, panes[index].y], "rail top");
      expectAligned([rail.bottom, panes[index].bottom], "rail bottom");
      expect(intersects(rail, main), "rail and the grid's pane").toBe(false);
    });
  });
});
