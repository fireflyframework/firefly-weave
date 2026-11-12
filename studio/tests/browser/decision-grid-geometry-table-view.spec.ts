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
// Table view, measured: Edit table keeps Input, Parameters and Output; Input
// holds the test values and Output the rule that matched and the result; the
// grid fills the Parameters card with stable columns; one primary button;
// the status, the row states and the problems as the approved design draws
// them. Desktop sizes, 1280 px and wider, plus the two-pane layout below.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  DecisionGridPage,
  draftOrdersWorkflow,
  keepTableDraft,
  openDraftGrid,
  openPaymentGrid,
  paymentPolicy,
  TablePanes,
  twelveRules,
  withoutOtherwise,
  type TableDocumentFixture,
} from "./decision-grid-po";
import { setTestInput } from "./formula-po";
import { hasPython, localAuthoring } from "./local-authoring";
import { openStepFixture, type StepDetailsPage } from "./step-details-po";
import { box, boxes, expectAligned, type Box } from "./step-geometry";

test.skip(!hasPython, "Weave's check runs the repository's Python.");

const DESKTOP = [
  { tag: "1440x900", width: 1440, height: 900, card: 780 },
  { tag: "1920x1080", width: 1920, height: 1080, card: 1060 },
];
const DRAFT_BANNER =
  "Draft 1.2.1 is saved on this computer, and test runs use it. Publish it before publishing this workflow.";
const PUBLISHED_BANNER =
  "payment-policy 1.2.0 is published. Your first change starts the next version on this computer.";

const clamp = (low: number, value: number, high: number) =>
  Math.min(high, Math.max(low, value));

/** The panes' content width, as step details measures it for their widths. */
const panesWidth = (details: StepDetailsPage) =>
  details.dialog.locator(".sd-panes").evaluate((panes) => {
    const s = getComputedStyle(panes);
    return (
      panes.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight)
    );
  });

/** The Parameters card's content box: its inner width less 20 px on each side. */
const cardContent = (details: StepDetailsPage) =>
  details.dialog.locator(".sd-main").evaluate((main) => {
    const r = main.getBoundingClientRect();
    const s = getComputedStyle(main);
    return {
      left: r.left + parseFloat(s.borderLeftWidth) + 20,
      right: r.right - parseFloat(s.borderRightWidth) - 20,
    };
  });

/** A token's color as computed styles report colors. */
const tokenColor = (page: Page, token: string) =>
  page.evaluate((name) => {
    const probe = document.createElement("div");
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    return value;
  }, token);

/** A token's font family as computed styles report it. */
const tokenFamily = (page: Page, token: string) =>
  page.evaluate((name) => {
    const probe = document.createElement("div");
    probe.style.font = `var(${name})`;
    document.body.append(probe);
    const value = getComputedStyle(probe).fontFamily;
    probe.remove();
    return value;
  }, token);

const style = (locator: Locator, property: string) =>
  locator.evaluate(
    (element, name) => getComputedStyle(element).getPropertyValue(name),
    property,
  );
/** A style of an element's ::after, where the cursor and a problem are drawn. */
const after = (locator: Locator, property: string) =>
  locator.evaluate(
    (element, name) =>
      getComputedStyle(element, "::after").getPropertyValue(name),
    property,
  );

/** A rule's row: its # cell and every cell it draws (row 0 is rule 1). */
const row = (grid: DecisionGridPage, index: number) =>
  grid.root.locator(`.dg-row[aria-rowindex="${index + 2}"]`);

/** payment-policy with a fourth rule whose formula is longer than two lines. */
function withLongFormula(version: string): TableDocumentFixture {
  const base = paymentPolicy(version);
  const tier = (name: string) => ({
    op: {
      name: "eq",
      args: [{ ref: "/input/tier" }, { literal: name }],
    },
  });
  const spec = base.spec as { rules: unknown[] };
  return {
    ...base,
    spec: {
      ...base.spec,
      rules: [
        ...spec.rules,
        {
          id: "named-tiers",
          when: {
            op: {
              name: "or",
              args: [
                tier("diamond-reserve"),
                tier("platinum-preferred"),
                tier("black-card-holder"),
                tier("founding-partner"),
                tier("strategic-account"),
                tier("internal-staff"),
              ],
            },
          },
          output: { literal: { approve: true, limit: 9000 } },
        },
      ],
    },
  };
}

/** Every visible control in `scope` measures at least `target` each way. */
async function expectTargets(scope: Locator, target: number, what: string) {
  const controls = await scope.evaluate((root) =>
    [
      ...root.querySelectorAll<HTMLElement>(
        'button, [role="gridcell"][tabindex="0"]',
      ),
    ]
      .filter((e) => e.getClientRects().length > 0)
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

/** Twelve rules with four problems: the grid keeps five rows, the list scrolls. */
async function expectProblemsBeside(grid: DecisionGridPage) {
  const problems = grid.problems();
  await expect(problems.locator("li")).toHaveCount(4);
  const box_ = await box(grid.root.locator(".dg-box"));
  expect(box_.height, "the grid box").toBeGreaterThanOrEqual(287.5);
  const scroller = await box(grid.root.locator(".dg-scroll"));
  const shown = (
    await boxes(grid.root.locator(".dg-row:not(.dg-header) > .dg-mark-cell"))
  ).filter(
    (cell) => cell.y >= scroller.y + 63 && cell.bottom <= scroller.bottom + 0.5,
  );
  expect(shown.length, "body rows in view").toBeGreaterThanOrEqual(5);
  // The list holds its head and its first three problems as drawn, and
  // scrolls to the fourth.
  const list = await box(problems);
  const head = await box(problems.locator(".dg-problems-head"));
  const items = await boxes(problems.locator("li"));
  const three =
    head.height + items.slice(0, 3).reduce((sum, item) => sum + item.height, 0);
  expect(
    list.height,
    "the list: its head and three rows",
  ).toBeGreaterThanOrEqual(three);
  expect(list.height, "the list: its head and three rows").toBeLessThanOrEqual(
    three + 3.5,
  );
  expect(
    await problems.evaluate((e) => e.scrollHeight > e.clientHeight),
    "the list scrolls",
  ).toBe(true);
  for (const item of await problems.locator("li").all()) {
    const [line, go] = [
      await box(item),
      await box(item.getByRole("button", { name: "Go to cell" })),
    ];
    expect(go.y - line.y, "Go to cell on the first line").toBeLessThan(8);
  }
}

for (const size of DESKTOP)
  test.describe(`Table view at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Edit table shows Input, Parameters and Output at the table's widths", async ({
      page,
    }) => {
      const { details } = await openDraftGrid(page);
      const dialog = details.dialog;
      await expect(dialog.locator(".sd-rail")).toHaveCount(0);
      for (const pane of [".sd-input", ".sd-main", ".sd-output"])
        await expect(dialog.locator(pane)).toBeVisible();
      const total = await panesWidth(details);
      const [input, main, output] = [
        await box(dialog.locator(".sd-input")),
        await box(dialog.locator(".sd-main")),
        await box(dialog.locator(".sd-output")),
      ];
      expect(
        Math.abs(input.width - clamp(240, 0.2 * total, 360)),
        "Input's width",
      ).toBeLessThanOrEqual(1);
      expect(
        Math.abs(output.width - clamp(280, 0.24 * total, 440)),
        "Output's width",
      ).toBeLessThanOrEqual(1);
      expect(main.width, "the Parameters card").toBeGreaterThanOrEqual(
        size.card - 0.5,
      );
      for (const pane of [input, output])
        expect(pane.width, "a side pane").toBeGreaterThanOrEqual(239.5);
    });

    test("the grid box spans the card's content box", async ({ page }) => {
      const { details, grid } = await openDraftGrid(page);
      const content = await cardContent(details);
      const grid_ = await box(grid.root.locator(".dg-box"));
      expect(Math.abs(grid_.x - content.left), "left edge").toBeLessThanOrEqual(
        1,
      );
      expect(
        Math.abs(grid_.right - content.right),
        "right edge",
      ).toBeLessThanOrEqual(1);
    });

    test("the tracks keep their minimums, and editing a cell moves no column", async ({
      page,
    }) => {
      const { grid } = await openDraftGrid(page);
      const headers = grid.root.locator(".dg-header > .dg-th");
      const before = await boxes(headers);
      const [mark, name, amount, tier, approve, limit] = before;
      expect(mark.width, "#").toBeCloseTo(48, 0);
      expect(name.width, "Rule name").toBeGreaterThanOrEqual(159.5);
      for (const input of [amount, tier])
        expect(input.width, "an input column").toBeGreaterThanOrEqual(139.5);
      for (const result of [approve, limit])
        expect(result.width, "a result column").toBeGreaterThanOrEqual(119.5);

      const edges = (all: Box[]) => all.map((b) => [b.x, b.width]);
      await grid.cell(1, 1).dblclick();
      await expect(grid.root.locator(".dg-cell-input")).toBeFocused();
      await grid.root.locator(".dg-cell-input").fill("<= 1000000000000");
      expect(edges(await boxes(headers)), "while editing").toEqual(
        edges(before),
      );
      await page.keyboard.press("Escape");
      await expect(grid.cell(1, 1)).toBeFocused();
      expect(edges(await boxes(headers)), "after editing").toEqual(
        edges(before),
      );
    });

    test("rows, header, group row and footer keep their heights, and cell text is 14 px sans", async ({
      page,
    }) => {
      const { grid } = await openDraftGrid(page);
      for (const cell of await boxes(
        grid.root.locator(".dg-row:not(.dg-header) > .dg-td"),
      ))
        expect(cell.height, "a body row").toBeGreaterThanOrEqual(35.5);
      for (const cell of await boxes(grid.root.locator(".dg-header > .dg-th")))
        expect(cell.height, "the header row").toBeCloseTo(36, 0);
      for (const cell of await boxes(grid.root.locator(".dg-group")))
        expect(cell.height, "the group row").toBeCloseTo(28, 0);
      expect(
        (await box(grid.root.locator(".dg-foot"))).height,
        "the footer",
      ).toBeCloseTo(44, 0);

      const mono = await tokenFamily(page, "--type-mono");
      const sans = await tokenFamily(page, "--type-body");
      const texts = await grid.root.locator(".dg-grid").evaluate((root) =>
        [...root.querySelectorAll<HTMLElement>("*")]
          .filter((e) =>
            [...e.childNodes].some(
              (n) => n.nodeType === Node.TEXT_NODE && n.textContent!.trim(),
            ),
          )
          .map((e) => {
            const s = getComputedStyle(e);
            return {
              text: e.textContent!.trim().slice(0, 24),
              small: !!e.closest(".dg-group, .dg-number, .sr-only"),
              size: parseFloat(s.fontSize),
              family: s.fontFamily,
            };
          }),
      );
      expect(texts.length).toBeGreaterThan(10);
      for (const t of texts) {
        expect(t.family, `"${t.text}" font`).not.toBe(mono);
        if (!t.small)
          expect(t.size, `"${t.text}" size`).toBeGreaterThanOrEqual(13);
      }
      for (const cell of await grid.root
        .locator(".dg-td .dg-cell-text, .dg-td .dg-formula")
        .all()) {
        expect(await style(cell, "font-size"), "cell text").toBe("14px");
        expect(await style(cell, "font-family"), "cell text").toBe(sans);
      }
    });

    test("a formula shows in full on two lines, and a longer one stops at two", async ({
      page,
    }) => {
      const { grid } = await openDraftGrid(page, withLongFormula("1.2.1"));
      const formulas = grid.root.locator(".dg-td.is-formula .dg-formula");
      await expect(formulas).toHaveCount(2);
      const sample = formulas.nth(0);
      await expect(sample).toHaveText(
        'input.amount > 5000 or input.tier == "platinum"',
      );
      if (size.width === 1440) {
        const fits = await sample.evaluate(
          (e) => e.scrollHeight <= e.clientHeight + 0.5,
        );
        expect(fits, "the sample formula shows in full").toBe(true);
      }
      const line = parseFloat(await style(formulas.nth(1), "line-height"));
      expect(
        (await box(formulas.nth(1))).height,
        "the longer formula's lines",
      ).toBeLessThanOrEqual(2 * line + 0.5);
      // The row grows with its formula.
      const long = await box(grid.cell(3, 0));
      expect(long.height).toBeGreaterThanOrEqual(2 * line);
    });

    test("a draft's only primary button is Publish table, and Execute step is again once the table closes", async ({
      page,
    }) => {
      const { details, grid } = await openDraftGrid(page);
      const primary = details.dialog.locator("button.primary:visible");
      await expect(primary).toHaveCount(1);
      await expect(primary).toHaveText("Publish table");
      await expect(
        details.dialog.getByRole("button", { name: "Execute step" }),
      ).toBeVisible();
      await grid.back();
      await expect(primary).toHaveCount(1);
      await expect(primary).toHaveText("Execute step");
    });

    test("Add rule and the add-column commands appear once, in the grid's footer", async ({
      page,
    }) => {
      const { details, grid } = await openDraftGrid(page);
      for (const name of [
        "Add rule",
        "Add input column",
        "Add result column",
      ]) {
        const found = details.dialog.getByRole("button", { name, exact: true });
        await expect(found, name).toHaveCount(1);
        await expect(
          grid.root
            .locator(".dg-foot")
            .getByRole("button", { name, exact: true }),
        ).toBeVisible();
      }
      await expect(grid.root.locator(".dg-foot")).toContainText("3 rules");
      await expect(
        details.dialog.getByRole("button", { name: "Try an input" }),
      ).toHaveCount(0);
    });

    test("a matched row is green, refused rows are red, and the cursor outlines one cell", async ({
      page,
    }) => {
      const { grid, panes } = await openDraftGrid(page);
      const [success, successBg, danger, dangerBg, focus] = await Promise.all(
        ["--success", "--success-bg", "--danger", "--danger-bg", "--focus"].map(
          (token) => tokenColor(page, token),
        ),
      );
      await grid.cell(1, 1).click();
      await panes.type({ amount: "2000", tier: "gold" });
      await expect(row(grid, 0).locator(".dg-mark")).toHaveAttribute(
        "data-mark",
        "match",
      );
      for (const cell of await row(grid, 0).locator(".dg-td").all())
        expect(await style(cell, "background-color"), "a matched cell").toBe(
          successBg,
        );
      const bar = await style(
        row(grid, 0).locator(".dg-mark-cell"),
        "box-shadow",
      );
      expect(bar).toContain(success);
      expect(bar).toContain("3px 0px 0px 0px inset");

      // The cursor: one outlined cell, even while typing in Input, and its
      // row keeps the color of any other row.
      await expect(grid.root.locator(".dg-td.is-cursor")).toHaveCount(1);
      const cursor = await after(grid.cell(1, 1), "box-shadow");
      expect(cursor).toContain(focus);
      expect(cursor).toContain("0px 0px 0px 2px inset");
      const plain = await style(
        row(grid, 2).locator(".dg-td").nth(4),
        "background-color",
      );
      for (const cell of await row(grid, 1)
        .locator(".dg-td:not(.is-cursor)")
        .all())
        expect(await style(cell, "background-color"), "the cursor's row").toBe(
          plain,
        );

      await grid.policy("Exactly one");
      await panes.type({ amount: "6000" });
      for (const index of [0, 2]) {
        await expect(row(grid, index).locator(".dg-mark")).toHaveAttribute(
          "data-mark",
          "refused",
        );
        for (const cell of await row(grid, index).locator(".dg-td").all())
          expect(await style(cell, "background-color"), "a refused cell").toBe(
            dangerBg,
          );
        const refused = await style(
          row(grid, index).locator(".dg-mark-cell"),
          "box-shadow",
        );
        expect(refused).toContain(danger);
        expect(refused).toContain("3px 0px 0px 0px inset");
      }
    });

    test("a draft shows the Draft pill and says what the draft does", async ({
      page,
    }) => {
      const { grid } = await openDraftGrid(page);
      await expect(grid.root.locator(".dg-bar .pill")).toHaveText("Draft");
      await expect(grid.root.locator(".dg-banner")).toHaveText(DRAFT_BANNER);
    });

    test("a published table shows the Published pill and its banner, and Execute step stays the primary button", async ({
      page,
    }) => {
      const { details, grid } = await openPaymentGrid(page);
      await expect(grid.root.locator(".dg-bar .pill")).toHaveText("Published");
      await expect(grid.root.locator(".dg-banner")).toHaveText(
        PUBLISHED_BANNER,
      );
      const primary = details.dialog.locator("button.primary:visible");
      await expect(primary).toHaveCount(1);
      await expect(primary).toHaveText("Execute step");
    });

    test("each problem names its cell, and errors turn Publish table off", async ({
      page,
    }) => {
      const { grid } = await openDraftGrid(page);
      await grid.edit(0, 1, ">> 1000");
      await grid.edit(1, 0, "big-gold");
      const items = grid.problems().locator("li");
      await expect(items).toHaveCount(2);
      await expect(items.locator(".pill")).toHaveText([
        "Rule 1 · amount",
        "Rule 2 · Rule name",
      ]);
      for (const item of await items.all()) {
        await expect(
          item.locator('weave-icon[data-icon="failCircle"]'),
        ).toBeVisible();
        await expect(
          item.getByRole("button", { name: "Go to cell" }),
        ).toBeVisible();
      }
      await expect(grid.problems()).toContainText(
        "Publish table is off until they're fixed.",
      );
      const publish = grid.root.getByRole("button", { name: "Publish table" });
      await expect(publish).toHaveAttribute("aria-disabled", "true");
      await expect(publish).toHaveAccessibleDescription(
        "Fix the 2 problems to publish",
      );
      // Off, it keeps an edge on the card (once its color has settled).
      const edge = await tokenColor(page, "--border");
      await expect.poll(() => style(publish, "border-top-color")).toBe(edge);
      // A cell's problem is its underline only, not an invalid field's bar.
      expect(await style(grid.cell(0, 1), "box-shadow")).toBe("none");
      expect(await after(grid.cell(0, 1), "box-shadow")).toContain(
        await tokenColor(page, "--danger"),
      );
      // A duplicate name marks its cell, and the mark shows inside the cursor.
      const name = grid.cell(1, 0);
      await expect(name).toHaveAttribute("aria-invalid", "true");
      await expect(name).toHaveAccessibleDescription(/must be unique/);
      await name.click();
      await expect(name).toHaveClass(/is-cursor/);
      const drawn = await after(name, "box-shadow");
      expect(drawn).toContain(await tokenColor(page, "--focus"));
      expect(drawn).toContain(await tokenColor(page, "--danger"));
    });

    test("the menus never paint over the cursor or the grid's lines", async ({
      page,
    }) => {
      const { grid } = await openDraftGrid(page);
      const focus = await tokenColor(page, "--focus");
      const name = grid.cell(1, 0);
      await name.click();
      await expect(name).toHaveClass(/is-cursor/);
      expect(await after(name, "box-shadow")).toContain(focus);
      expect(await after(name, "z-index")).toBe("1");
      for (const [toggle, cell] of [
        [name.locator(".row-menu-toggle"), name],
        [
          grid.root
            .locator(".dg-header > .dg-th")
            .nth(2)
            .locator(".row-menu-toggle"),
          grid.root.locator(".dg-header > .dg-th").nth(2),
        ],
      ] as const) {
        await grid.cell(1, 0).click();
        await page.mouse.move(0, 0);
        await expect
          .poll(() => style(toggle, "background-color"), { message: "at rest" })
          .toBe("rgba(0, 0, 0, 0)");
        const [inner, outer] = [await box(toggle), await box(cell)];
        expect(inner.y, "inside the cell's top").toBeGreaterThanOrEqual(
          outer.y,
        );
        expect(inner.bottom, "clear of the cell's line").toBeLessThanOrEqual(
          outer.bottom - 1,
        );
        expect(inner.right, "inside the cell").toBeLessThanOrEqual(outer.right);
      }
    });

    test("the result view's segments keep their padding", async ({ page }) => {
      const { panes } = await openDraftGrid(page);
      for (const radio of await panes.output.getByRole("radio").all())
        expect(
          parseFloat(await style(radio, "padding-left")),
          "segment padding",
        ).toBeGreaterThanOrEqual(7.5);
    });

    test("with no test values, Input shows the fields and Output says there is no result yet", async ({
      page,
    }) => {
      const { panes } = await openDraftGrid(page);
      await expect(panes.field("amount")).toBeVisible();
      await expect(panes.field("tier")).toBeVisible();
      await expect(panes.input).toContainText(
        "Change a value to see which rule matches. These values are for trying the table; they aren't saved with it.",
      );
      await expect(panes.output).toContainText("No result yet");
      await expect(panes.output).toContainText(
        "Type a value in Input to see which rule matches.",
      );
      await expect(panes.result).toHaveCount(0);
      await expect(panes.count).toHaveText("Not checked");
      // The hints balance their last line.
      for (const hint of [
        panes.input.locator(".dg-try-hint"),
        page.locator("#dg-policy-hint"),
      ])
        expect(await style(hint, "text-wrap-style")).toBe("pretty");
    });
  });

test.describe("Table view at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the step's test data fills Input, and the matched rule and the result are in view", async ({
    page,
  }) => {
    await keepTableDraft(page);
    const details = await openStepFixture(page, draftOrdersWorkflow);
    await localAuthoring(page);
    await setTestInput(page, details, {
      Amount: "2000",
      "Tier (optional)": "gold",
    });
    await details.open("score");
    const grid = new DecisionGridPage(page);
    await grid.open();
    const panes = new TablePanes(page);
    await expect(panes.input.locator(".pill")).toHaveText("From test data");
    await expect(panes.field("amount")).toHaveValue("2000");
    await expect(panes.cards).toHaveCount(1);
    await expect(panes.cards.first()).toContainText("Rule 1 · big-gold");
    await expect(panes.count).toHaveText("Rule 1 matched");
    await expect(panes.result).toContainText("5000");
    await expect(panes.checked).toBeVisible();
    const pane = await box(details.dialog.locator(".sd-output"));
    const last = await box(panes.result.locator("tr").last());
    expect(last.bottom, "the result's last row").toBeLessThanOrEqual(
      pane.bottom,
    );
    expect(
      await panes.output.locator(".sd-pane-body").evaluate((e) => e.scrollTop),
    ).toBe(0);

    // A keystroke: the grid's marks and Output follow before the next frame.
    const started = Date.now();
    const seen = await panes.field("amount").evaluate(async (field) => {
      const input = field as HTMLInputElement;
      input.value = "500";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      await new Promise((done) => requestAnimationFrame(() => done(null)));
      const dialog = input.closest(".step-details")!;
      return {
        count: dialog.querySelector(".dg-try-count")?.textContent?.trim(),
        mark: dialog
          .querySelector('.dg-row[aria-rowindex="3"] .dg-mark')
          ?.getAttribute("data-mark"),
      };
    });
    expect(seen).toEqual({ count: "Rule 2 matched", mark: "match" });
    await expect(panes.input.locator(".pill")).toHaveText("Edited");
    await expect(panes.checked).toBeVisible();
    expect(Date.now() - started, "Weave's check").toBeLessThan(5000);
    await panes.input
      .getByRole("button", { name: "Use the step's test data" })
      .click();
    await expect(panes.field("amount")).toHaveValue("2000");
    await expect(panes.input.locator(".pill")).toHaveText("From test data");
  });

  test("a problem in the table withholds the result, and an overlap warning doesn't", async ({
    page,
  }) => {
    const { grid, panes } = await openDraftGrid(page);
    await panes.type({ amount: "2000", tier: "gold" });
    await expect(panes.result).toContainText("5000");
    await grid.edit(0, 1, ">> 1000");
    await expect(panes.output.locator(".dg-callout")).toContainText(
      "Rule 1 has a problem, so Studio can't check this input.",
    );
    await expect(panes.result).toHaveCount(0);
    await expect(panes.output).toContainText("Not checked");
    await page.keyboard.press("Escape");
    await grid.cell(0, 1).focus();
    await page.keyboard.press("Escape");
    await expect(grid.problems()).toHaveCount(0);

    // Exactly one with rules that overlap: a warning only.
    await grid.edit(1, 1, "> 500");
    await grid.policy("Exactly one");
    await expect(grid.problems()).toContainText("can both match");
    await panes.type({ amount: "700", tier: "silver" });
    await expect(panes.count).toHaveText("Rule 2 matched");
    await expect(panes.result).toContainText("1000");
  });

  test("a problems list never squeezes the grid below five rows", async ({
    page,
  }) => {
    const { grid } = await openDraftGrid(page, twelveRules("1.2.1"));
    for (const row of [0, 1, 2, 3]) await grid.edit(row, 2, ">> x");
    await expectProblemsBeside(grid);
  });

  test("while a cell is edited, its text box is its only frame", async ({
    page,
  }) => {
    const { grid } = await openDraftGrid(page);
    const cell = grid.cell(1, 1);
    await cell.click();
    await expect(cell).toHaveClass(/is-cursor/);
    expect(await after(cell, "box-shadow")).toContain(
      await tokenColor(page, "--focus"),
    );
    await page.keyboard.press("Enter");
    await expect(grid.root.locator(".dg-cell-input")).toBeFocused();
    await expect(cell).toHaveClass(/is-editing/);
    expect(await after(cell, "box-shadow"), "the cursor's outline").toBe(
      "none",
    );
    // A cell with a problem: neither the outline nor the underline.
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type(">> 1");
    await page.keyboard.press("Enter");
    await expect(cell).toHaveClass(/is-flagged/);
    await cell.click();
    await page.keyboard.press("Enter");
    await expect(cell).toHaveClass(/is-editing/);
    expect(await after(cell, "box-shadow"), "a flagged cell").toBe("none");
  });

  test("a required test value left blank says so in Input, and the grid shows no marks", async ({
    page,
  }) => {
    const { grid, panes } = await openDraftGrid(page);
    const danger = await tokenColor(page, "--danger");
    await panes.type({ amount: "2000", tier: "gold" });
    await expect(row(grid, 0).locator(".dg-mark")).toHaveAttribute(
      "data-mark",
      "match",
    );
    const amount = panes.field("amount");
    await amount.fill("");
    await expect(amount).toHaveAttribute("aria-invalid", "true");
    await expect(panes.input.locator("p.error")).toHaveText([
      "Enter a number.",
    ]);
    expect(await style(amount, "border-top-color")).toBe(danger);
    await expect(grid.root.locator(".dg-mark")).toHaveCount(0);
    await expect(panes.output.locator(".dg-callout")).toContainText(
      "The input doesn't fit the table's input fields.",
    );
    // Weave still checks it and says what doesn't fit.
    await expect(panes.checked).toHaveAttribute("data-tone", "neutral");
    await expect(grid.root.locator(".dg-mark")).toHaveCount(0);
    await expect(panes.result).toHaveCount(0);
    // A number again: the error goes and the marks come back.
    await amount.fill("500");
    await expect(amount).not.toHaveAttribute("aria-invalid", "true");
    await expect(panes.input.locator("p.error")).toHaveCount(0);
    await expect(row(grid, 1).locator(".dg-mark")).toHaveAttribute(
      "data-mark",
      "match",
    );
  });

  test("with no Otherwise result, the Otherwise row's results read No result in the subtle color", async ({
    page,
  }) => {
    const { grid } = await openDraftGrid(page, withoutOtherwise("1.2.1"));
    const subtle = await tokenColor(page, "--subtle");
    await expect(grid.cell(3, 1)).toHaveText("When no rule matches");
    for (const col of [3, 4]) {
      await expect(grid.cell(3, col)).toHaveText("No result");
      expect(
        await style(grid.cell(3, col).locator(".dg-cell-text"), "color"),
        "No result",
      ).toBe(subtle);
    }
  });

  test("when no rule matches, the Otherwise row is the matched row", async ({
    page,
  }) => {
    const { grid, panes } = await openDraftGrid(page);
    const [success, successBg] = await Promise.all(
      ["--success", "--success-bg"].map((token) => tokenColor(page, token)),
    );
    await panes.type({ amount: "3000", tier: "silver" });
    await expect(panes.count).toHaveText("No rule matched");
    const other = row(grid, 3);
    await expect(other).toHaveClass(/dg-otherwise/);
    await expect(other.locator(".dg-mark")).toHaveAttribute(
      "data-mark",
      "match",
    );
    await expect(other.locator(".dg-mark weave-icon")).toHaveCount(1);
    for (const cell of await other.locator(".dg-td").all())
      expect(await style(cell, "background-color"), "an Otherwise cell").toBe(
        successBg,
      );
    expect(await style(other.locator(".dg-mark-cell"), "box-shadow")).toContain(
      success,
    );
    for (const index of [0, 1, 2])
      await expect(row(grid, index).locator(".dg-mark")).toHaveAttribute(
        "data-mark",
        "no-match",
      );
    // A rule matches again: Otherwise goes back to its own color.
    await panes.type({ amount: "500" });
    await expect(other.locator(".dg-mark")).toHaveCount(0);
    await expect(other).not.toHaveClass(/is-match/);
  });

  test("rows stay 36 px while a cell is edited, a one-line formula and Otherwise included", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1920, height: 1080 });
    const { grid } = await openDraftGrid(page);
    await grid.cell(1, 1).dblclick();
    await expect(grid.root.locator(".dg-cell-input")).toBeFocused();
    for (const cell of await boxes(
      grid.root.locator(".dg-row:not(.dg-header) > .dg-mark-cell"),
    ))
      expect(cell.height, "a row while editing").toBeCloseTo(36, 0);
  });

  test("widths set in table view stay apart from the step form's", async ({
    page,
  }) => {
    const { details, grid } = await openDraftGrid(page);
    const dialog = details.dialog;
    const total = await panesWidth(details);
    const tableInput = clamp(240, 0.2 * total, 360);
    const resize = dialog.getByRole("separator", { name: "Resize Input" });
    await resize.focus();
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowRight");
    await expect
      .poll(async () =>
        Math.abs(
          (await box(dialog.locator(".sd-input"))).width - tableInput - 32,
        ),
      )
      .toBeLessThanOrEqual(1);
    const widened = (await box(dialog.locator(".sd-input"))).width;

    await grid.back();
    const form = (await box(dialog.locator(".sd-input"))).width;
    expect(Math.abs(form - Math.round(total * 0.28))).toBeLessThanOrEqual(1);
    await resize.focus();
    await page.keyboard.press("ArrowLeft");
    await expect
      .poll(async () => (await box(dialog.locator(".sd-input"))).width)
      .toBeCloseTo(form - 16, 0);

    await grid.open();
    expect(
      Math.abs((await box(dialog.locator(".sd-input"))).width - widened),
    ).toBeLessThanOrEqual(1);
  });

  test("a pane that opens again never squeezes the grid below 640 px", async ({
    page,
  }) => {
    const { details } = await openDraftGrid(page);
    const dialog = details.dialog;
    await dialog.getByRole("button", { name: "Fold Input" }).click();
    await expect(
      dialog.getByRole("button", { name: "Show Input" }),
    ).toBeVisible();
    const resize = dialog.getByRole("separator", { name: "Resize Output" });
    await resize.focus();
    await page.keyboard.press("End");
    // The separator's maximum is where End stops: Output beside the rail.
    const total = await panesWidth(details);
    const widest = String(Math.round(total - 640 - 48));
    await expect(resize).toHaveAttribute("aria-valuenow", widest);
    await expect(resize).toHaveAttribute("aria-valuemax", widest);
    await dialog.getByRole("button", { name: "Show Input" }).click();
    await expect(dialog.locator(".sd-input")).toBeVisible();
    const [main, output, panes] = [
      await box(dialog.locator(".sd-main")),
      await box(dialog.locator(".sd-output")),
      await box(dialog.locator(".sd-panes")),
    ];
    expect(main.width, "the grid's card").toBeGreaterThanOrEqual(639.5);
    expect(output.right, "Output inside the panes").toBeLessThanOrEqual(
      panes.right + 0.5,
    );
  });

  test("the problems head counts what Publish table counts", async ({
    page,
  }) => {
    const { grid } = await openDraftGrid(page);
    await grid.edit(1, 1, "> 500");
    await grid.policy("Exactly one");
    await grid.edit(0, 1, ">> 1");
    const head = grid.problems().locator(".dg-problems-head");
    await expect(head).toContainText("1 problem, 1 warning");
    await expect(head).toContainText("Publish table is off until it's fixed.");
    await expect(
      grid.root.getByRole("button", { name: "Publish table" }),
    ).toHaveAccessibleDescription("Fix the problem to publish");
  });

  test("Fold folds a pane to its rail, and the choice is remembered", async ({
    page,
  }) => {
    const { details, grid } = await openDraftGrid(page);
    const dialog = details.dialog;
    const before = (await box(dialog.locator(".sd-main"))).width;
    await dialog.getByRole("button", { name: "Fold Input" }).click();
    const rail = dialog.locator(".sd-rail-input");
    await expect(rail).toBeVisible();
    expect((await box(rail)).width).toBeCloseTo(48, 0);
    await expect(dialog.locator(".sd-input")).toBeHidden();
    expect((await box(dialog.locator(".sd-main"))).width).toBeGreaterThan(
      before,
    );
    await grid.back();
    await grid.open();
    await expect(rail).toBeVisible();
    await rail.click();
    await expect(dialog.locator(".sd-input")).toBeVisible();
    await expect(rail).toHaveCount(0);
  });
});

test.describe("Table view on touch at 1440x900", () => {
  test.use({
    viewport: { width: 1440, height: 900 },
    hasTouch: true,
    isMobile: true,
  });

  test("every grid control and Fold is at least 44 px each way, and rows are 44 px", async ({
    page,
  }) => {
    const { details, grid } = await openDraftGrid(page);
    for (const cell of await boxes(
      grid.root.locator(".dg-row:not(.dg-header) > .dg-td"),
    ))
      expect(cell.height, "a body row").toBeGreaterThanOrEqual(43.5);
    // The column menus always show on touch.
    const menus = grid.root.locator(".dg-th-menu");
    await expect(menus).toHaveCount(4);
    for (const menu of await menus.all())
      expect(await style(menu, "opacity")).toBe("1");
    await expectTargets(grid.root.locator(".dg-box"), 44, "the grid");
    for (const name of ["Fold Input", "Fold Output"]) {
      const fold = await box(details.dialog.getByRole("button", { name }));
      expect(fold.width, name).toBeGreaterThanOrEqual(43.5);
      expect(fold.height, name).toBeGreaterThanOrEqual(43.5);
    }
  });
});

test.describe("Table view at 1279x800", () => {
  test.use({ viewport: { width: 1279, height: 800 } });

  test("Parameters and the Data pane show, with Output selected, and the grid fills its card", async ({
    page,
  }) => {
    const { details, grid, panes } = await openDraftGrid(page);
    const dialog = details.dialog;
    await expect(dialog.locator(".sd-rail")).toHaveCount(0);
    await expect(dialog.locator(".sd-main")).toBeVisible();
    await expect(
      dialog
        .getByRole("tablist", { name: "Data" })
        .getByRole("tab", { name: "Output" }),
    ).toHaveAttribute("aria-selected", "true");
    await expect(panes.output).toBeVisible();
    await expect(dialog.locator(".sd-input")).toBeHidden();
    const content = await cardContent(details);
    const grid_ = await box(grid.root.locator(".dg-box"));
    expectAligned([grid_.x, content.left], "the grid's left edge", 1);
    expectAligned([grid_.right, content.right], "the grid's right edge", 1);
  });
});

for (const size of [
  { tag: "1024x768", width: 1024, height: 768 },
  { tag: "768x1024", width: 768, height: 1024 },
])
  test.describe(`Table view at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the Data pane starts folded, and the grid's compact columns fill the card", async ({
      page,
    }) => {
      const { details, grid, panes } = await openDraftGrid(page);
      const dialog = details.dialog;
      const rail = dialog.getByRole("button", { name: "Show data" });
      await expect(rail).toBeVisible();
      expect((await box(rail)).width).toBeCloseTo(48, 0);
      await expect(dialog.locator(".sd-output")).toBeHidden();
      await expect(dialog.getByRole("tablist", { name: "Data" })).toHaveCount(
        0,
      );
      const content = await cardContent(details);
      const grid_ = await box(grid.root.locator(".dg-box"));
      expectAligned([grid_.x, content.left], "the grid's left edge", 1);
      expectAligned([grid_.right, content.right], "the grid's right edge", 1);
      const [mark, name, ...columns] = await boxes(
        grid.root.locator(".dg-header > .dg-th"),
      );
      expect(mark.width).toBeCloseTo(48, 0);
      expect(name.width, "Rule name").toBeGreaterThanOrEqual(127.5);
      for (const input of columns.slice(0, 2))
        expect(input.width, "an input column").toBeGreaterThanOrEqual(127.5);
      for (const result of columns.slice(2))
        expect(result.width, "a result column").toBeGreaterThanOrEqual(111.5);
      if (size.width >= 1024)
        expect(
          await grid.root
            .locator(".dg-scroll")
            .evaluate((e) => e.scrollWidth - e.clientWidth),
          "sideways room",
        ).toBeLessThanOrEqual(0);
      // The rail opens the Data pane at Output, and the choice is kept.
      await rail.click();
      await expect(
        dialog
          .getByRole("tablist", { name: "Data" })
          .getByRole("tab", { name: "Output" }),
      ).toHaveAttribute("aria-selected", "true");
      await expect(panes.output).toBeVisible();
      await grid.back();
      await grid.open();
      await expect(panes.output).toBeVisible();
      await expect(rail).toHaveCount(0);
    });

    test("the add-column commands stay together at the footer's right", async ({
      page,
    }) => {
      const { grid } = await openDraftGrid(page);
      const foot = await box(grid.root.locator(".dg-foot"));
      const [input, result] = [
        await box(grid.root.getByRole("button", { name: "Add input column" })),
        await box(grid.root.getByRole("button", { name: "Add result column" })),
      ];
      expectAligned([input.y, result.y], "one row", 0.5);
      expect(foot.right - result.right, "at the right").toBeLessThanOrEqual(
        12.5,
      );
    });

    test("a problems list never squeezes the grid below five rows", async ({
      page,
    }) => {
      const { grid } = await openDraftGrid(page, twelveRules("1.2.1"));
      for (const row of [0, 1, 2, 3]) await grid.edit(row, 2, ">> x");
      await expectProblemsBeside(grid);
    });

    test("beside the Data pane, a short table's rows and both of its problems show in full", async ({
      page,
    }) => {
      const { grid, panes } = await openDraftGrid(page);
      await panes.show("Output");
      await expect(panes.output).toBeVisible();
      await grid.edit(0, 1, ">> 1000");
      await grid.edit(1, 0, "big-gold");
      const problems = grid.problems();
      await expect(problems.locator("li")).toHaveCount(2);
      const scroller = grid.root.locator(".dg-scroll");
      const view = await box(scroller);
      const rows = await boxes(
        grid.root.locator(".dg-row:not(.dg-header) > .dg-mark-cell"),
      );
      expect(rows.length, "three rules and Otherwise").toBe(4);
      for (const [index, cell] of rows.entries()) {
        expect(cell.y, `row ${index + 1}'s top`).toBeGreaterThanOrEqual(view.y);
        expect(cell.bottom, `row ${index + 1}'s bottom`).toBeLessThanOrEqual(
          view.bottom + 0.5,
        );
      }
      expect(
        await scroller.evaluate((e) => e.scrollHeight - e.clientHeight),
        "the grid scrolls",
      ).toBeLessThanOrEqual(1);
      const list = await box(problems);
      for (const item of await boxes(problems.locator("li")))
        expect(item.bottom, "a problem").toBeLessThanOrEqual(list.bottom + 0.5);
      expect(
        await problems.evaluate((e) => e.scrollHeight - e.clientHeight),
        "the list scrolls",
      ).toBeLessThanOrEqual(1);
    });

    test("the table bar's divider shows only while its items share one line", async ({
      page,
    }) => {
      const { grid, panes } = await openDraftGrid(page);
      const bar = grid.root.locator(".dg-bar");
      const divider = bar.locator(".dg-divider");
      const oneLine = async () => {
        const [back, ident, publish] = [
          await box(bar.locator(".dg-back")),
          await box(bar.locator(".dg-ident")),
          await box(bar.locator(".dg-publish")),
        ];
        return ident.y < back.bottom && publish.y < back.bottom;
      };
      // The Data pane folded: one line, with the divider.
      expect(await oneLine(), "folded").toBe(true);
      await expect(divider).toBeVisible();
      // The Data pane open: at 768 the bar wraps and the divider goes.
      await panes.show("Output");
      await expect(panes.output).toBeVisible();
      const shared = await oneLine();
      if (size.width === 768)
        expect(shared, "beside the Data pane").toBe(false);
      if (shared) await expect(divider).toBeVisible();
      else await expect(divider).toBeHidden();
    });
  });
