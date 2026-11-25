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
// How transactions lay out in Operate: the runs list's column, a record's
// three layouts on its own width, the inbox's bands and cases, the drawer, the
// decisions and the Overview panel, at five window sizes and on touch. Nothing
// scrolls sideways, no operation key or word is cut, every target is full
// size, and what a person must see first is in view.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { caller, ids, loop, runs } from "../transaction-incidents-fixture";
import { environment } from "./incidents-po";
import {
  operator,
  reconciler,
  transactionsPlatform,
} from "./operate-transactions-po";

const sizes = [
  { width: 1440, height: 900, touch: false },
  { width: 1920, height: 1080, touch: false },
  { width: 768, height: 1024, touch: false },
  { width: 600, height: 500, touch: false },
  { width: 360, height: 640, touch: true },
  { width: 1440, height: 900, touch: true },
] as const;
type Size = (typeof sizes)[number];
const tagOf = (size: Size) =>
  `${size.width}x${size.height}${size.touch ? " touch" : ""}`;
/** The least height of a control, and of an icon button. */
const large = (size: Size) => size.touch || size.width < 768;
const control = (size: Size) => (large(size) ? 44 : 36);
const icon = (size: Size) => (large(size) ? 44 : 32);

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
  right: number;
  bottom: number;
}
/** The border box of an element. */
const box = async (locator: Locator): Promise<Box> => {
  const { x, y, width, height } = (await locator.boundingBox())!;
  return { x, y, width, height, right: x + width, bottom: y + height };
};
const boxes = (locator: Locator): Promise<Box[]> =>
  locator.evaluateAll((elements) =>
    elements
      .filter((element) => element.getClientRects().length > 0)
      .map((element) => {
        const r = element.getBoundingClientRect();
        return {
          x: r.x,
          y: r.y,
          width: r.width,
          height: r.height,
          right: r.right,
          bottom: r.bottom,
        };
      }),
  );
/** Nothing is wider than the window, nor than the element that holds it. */
async function expectNoSideways(page: Page, ...holders: Locator[]) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    ),
  ).toBeLessThanOrEqual(0);
  for (const holder of holders)
    for (const element of await holder.all())
      expect(
        await element.evaluate((node) => node.scrollWidth - node.clientWidth),
        String(holder),
      ).toBeLessThanOrEqual(0);
}
/** Text that its own box, or its cell's, cuts off. */
const cutText = (scope: Locator, selector: string): Promise<string[]> =>
  scope.evaluate((root, within) => {
    const cut: string[] = [];
    for (const element of root.querySelectorAll<HTMLElement>(within)) {
      if (!element.getClientRects().length) continue;
      const range = document.createRange();
      range.selectNodeContents(element);
      const cell = (
        element.closest("[role=cell], [role=columnheader], dd, li, p, label") ??
        element
      ).getBoundingClientRect();
      for (const part of range.getClientRects())
        if (
          part.width > 1 &&
          (part.right > cell.right + 1 || part.left < cell.left - 1)
        )
          cut.push(element.innerText.trim().slice(0, 60));
      if (
        element.scrollWidth > element.clientWidth + 1 &&
        element.clientWidth > 1
      )
        cut.push(element.innerText.trim().slice(0, 60));
    }
    return cut;
  }, selector);
/** Each control is at least `height` tall, and an icon button as wide. */
async function expectTargets(
  scope: Locator,
  selector: string,
  height: number,
  width = 0,
) {
  const found = await boxes(scope.locator(selector));
  expect(found.length, selector).toBeGreaterThan(0);
  for (const target of found) {
    expect(target.height, selector).toBeGreaterThanOrEqual(height - 0.5);
    expect(target.width, selector).toBeGreaterThanOrEqual(width - 0.5);
  }
}
/** Whole in the window, without scrolling. */
async function expectInView(page: Page, ...parts: Locator[]) {
  const { height } = page.viewportSize()!;
  for (const part of parts) {
    const { y, bottom } = await box(part);
    expect(y, String(part)).toBeGreaterThanOrEqual(-0.5);
    expect(bottom, String(part)).toBeLessThanOrEqual(height + 0.5);
  }
}

const transactionsOf = (page: Page) =>
  page.getByRole("region", { name: "Transactions" });
const block = (page: Page, key: string) =>
  transactionsOf(page).getByRole("article", { name: `Transaction ${key}` });
const high = (page: Page) =>
  page.getByRole("region", { name: "High priority" });
const other = (page: Page) =>
  page.getByRole("region", { name: "Other incidents" });
const drawer = (page: Page) => page.locator("weave-incident-drawer");
const sheet = (page: Page) => page.locator(".record-detail");
const offers = (page: Page) =>
  drawer(page).getByRole("region", { name: "Resolve this incident" });
const offer = (page: Page, name: string) =>
  offers(page).getByRole("button", { name, exact: true });
const dialog = (page: Page, name: string) =>
  page.getByRole("dialog", { name, exact: true });
const openRunDetail = async (page: Page, id: string) => {
  await page.goto(`/operate/runs/${id}`);
  await expect(transactionsOf(page).locator(".txn-summary")).toBeVisible();
};
const openIncident = async (page: Page, id: string) => {
  await page.goto(`/operate/incidents?incident=${id}`);
  await expect(drawer(page).locator(".event-lines li").first()).toBeVisible();
};
/** The columns a record's row draws: five, four, or two for a step row. */
const columnsOf = (row: Locator) =>
  row.evaluate(
    (element) =>
      getComputedStyle(element).gridTemplateColumns.split(" ").length,
  );

for (const size of sizes)
  test.describe(tagOf(size), () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
      timezoneId: "UTC",
    });

    test("the runs list holds its pills, and its filters are full targets", async ({
      page,
    }) => {
      await transactionsPlatform(page);
      await page.goto("/operate/runs");
      const rows = page
        .getByRole("table", { name: "Runs" })
        .locator(".run-row");
      await expect(rows).toHaveCount(11);
      await expectNoSideways(page);
      // In its own column, or on the row's line of details where there is none.
      const own = size.width >= 1440 ? ".cell-transaction" : ".cell-meta";
      const words =
        /^(Open|In doubt|Undoing|Undone|Completed|Force ended|Reconciled)$/;
      const shown = rows
        .locator(`${own} .status-pill`)
        .filter({ hasText: words });
      const pills = await boxes(shown);
      expect(pills).toHaveLength(10);
      expect(
        await cutText(page.locator("#main"), `${own} .status-pill`),
      ).toEqual([]);
      // A name too long for its column keeps its start, and is whole in its title.
      const names = await rows.locator(".row-open").evaluateAll((buttons) =>
        buttons.map((button) => {
          const range = document.createRange();
          range.selectNodeContents(button);
          return {
            start:
              range.getBoundingClientRect().left -
              button.getBoundingClientRect().left,
            whole: button.getAttribute("title") === button.textContent?.trim(),
          };
        }),
      );
      expect(names).toHaveLength(11);
      for (const { start, whole } of names) {
        expect(start).toBeGreaterThanOrEqual(-0.5);
        expect(whole).toBe(true);
      }
      const heads = page
        .getByRole("table", { name: "Runs" })
        .getByRole("columnheader");
      if (size.width >= 1440) {
        // Its own column, 104 px, after Incidents; a run without one is blank.
        const head = await box(heads.filter({ hasText: "Transaction" }));
        const before = await box(heads.filter({ hasText: "Incidents" }));
        expect(head.width).toBeCloseTo(104, 0);
        expect(head.x).toBeGreaterThan(before.x);
        for (const pill of pills) {
          expect(pill.x).toBeGreaterThanOrEqual(head.x - 0.5);
          expect(pill.right).toBeLessThanOrEqual(head.right + 0.5);
        }
      }
      const more = page.getByRole("button", { name: "More filters" });
      if (!(await more.isVisible()))
        await page
          .getByRole("button", { name: /^Filters/ })
          .first()
          .click();
      await more.click();
      const group = page.getByRole("group", { name: "Transaction" });
      await expect(group.getByRole("button")).toHaveCount(10);
      await expectTargets(group, "button", control(size));
      const needs = page
        .locator("label.checkbox-field")
        .filter({ hasText: "Needs attention" });
      expect((await box(needs)).height).toBeGreaterThanOrEqual(
        (large(size) ? 44 : 24) - 0.5,
      );
      await expectNoSideways(page);
    });

    test("a run's records take the layout their own width allows, with every key whole", async ({
      page,
    }) => {
      await transactionsPlatform(page, { capabilities: reconciler });
      await openRunDetail(page, runs.loop);
      await page
        .getByRole("button", { name: "Show transaction move[1]" })
        .click();
      const record = block(page, "move[4]").locator(".txn-record");
      const inner = await record.evaluate((element) => element.clientWidth);
      const expected = inner >= 720 ? 5 : inner >= 560 ? 4 : 2;
      // Beside the runs list from 1440 px, under it and narrower below.
      expect(expected).toBe(size.width >= 1440 ? 5 : 2);
      const rows = transactionsOf(page).locator(".txn-step:not(.txn-head)");
      await expect(rows).toHaveCount(13);
      for (const row of await rows.all())
        expect(await columnsOf(row)).toBe(expected);
      await expectNoSideways(
        page,
        transactionsOf(page).locator(".txn-record"),
        rows,
      );
      expect(
        await cutText(
          transactionsOf(page),
          ".c-step strong, .c-how, .step-word, .step-note, .c-key code, .txn-meta span, .txn-cause span, .txn-resolution dd, .status-pill",
        ),
      ).toEqual([]);
      // The whole run ID is in each key, which wraps inside its cell.
      const key = block(page, "move[3]").locator(".txn-step.stuck .c-key code");
      await expect(key).toHaveText(`${runs.loop}:hold[3]#cancel`);
      const cell = await box(
        block(page, "move[3]").locator(".txn-step.stuck .c-key"),
      );
      const text = await box(key);
      expect(text.right).toBeLessThanOrEqual(cell.right + 0.5);
      await expectTargets(
        transactionsOf(page),
        ".copy-key",
        icon(size),
        icon(size),
      );
      // Blocks: 16 px of padding, 12 px in a narrow section; folded, one line.
      const narrow = (await box(transactionsOf(page))).width < 560;
      await expect(block(page, "move[2]")).toHaveCSS(
        "padding",
        narrow ? "12px" : "16px",
      );
      await page
        .getByRole("button", { name: "Hide transaction move[1]" })
        .click();
      await expect(block(page, "move[1]")).toHaveCSS("padding", "8px 12px");
    });

    test("a run in doubt shows its notice and its actions first", async ({
      page,
    }) => {
      await transactionsPlatform(page, { capabilities: reconciler });
      await openRunDetail(page, runs.called);
      const parts = [
        page.locator("#record-detail-title"),
        page.locator(".notice.incident"),
        page.locator(".run-controls"),
      ];
      const blocked = page.locator(".cancel-blocked");
      await expect(blocked).toContainText(
        "Cancel without undoing: a transaction is in doubt. Resolve it from its incident first.",
      );
      if (size.width >= 1440 || size.width === 360)
        await expectInView(page, ...parts);
      if (size.width >= 1440) await expectInView(page, blocked);
      await expectTargets(
        page.locator(".run-controls"),
        "button",
        control(size),
      );
      await expectNoSideways(page);
      await transactionsOf(page)
        .getByRole("button", { name: "1 in doubt" })
        .click();
      await expect(
        block(page, "apply").getByRole("heading", {
          name: /Transaction apply/,
        }),
      ).toBeFocused();
    });

    test("the inbox shows the band in doubt first, its case on the list's own columns", async ({
      page,
    }) => {
      await transactionsPlatform(page, { capabilities: reconciler });
      await page.goto("/operate/incidents");
      await expect(high(page).locator(".band-count")).toHaveText("4 incidents");
      await expect(other(page).locator(".resource-row")).toHaveCount(4);
      await expectNoSideways(page);
      expect(
        await cutText(
          high(page),
          ".row-open, .row-text small, .business-key, .case-head strong, .case-head span, .tag, .status-pill, .band-count",
        ),
      ).toEqual([]);
      // 24 px between the bands.
      expect(
        (await box(other(page))).y - (await box(high(page))).bottom,
      ).toBeCloseTo(24, 0);
      // A case's members keep the edges of the list's columns, within 1 px.
      const edges = await high(page)
        .getByRole("table")
        .evaluate((table) => {
          const lefts = (row: Element) =>
            [...row.children]
              .filter((cell) => cell.getClientRects().length)
              .map((cell) => cell.getBoundingClientRect().left);
          return {
            rows: [...table.querySelectorAll(".resource-row")].map((row) => ({
              member: !!row.closest(".case"),
              lefts: lefts(row),
            })),
          };
        });
      const plain = edges.rows.find((row) => !row.member)!.lefts;
      const members = edges.rows.filter((row) => row.member);
      expect(members).toHaveLength(2);
      for (const { lefts } of members) {
        expect(lefts).toHaveLength(plain.length);
        for (const [column, left] of lefts.entries())
          expect(
            Math.abs(left - plain[column]),
            `column ${column}`,
          ).toBeLessThanOrEqual(1);
      }
      // Six columns from 900 px of list, two below 680 px.
      const columns = await columnsOf(
        high(page).locator(".resource-row").first(),
      );
      expect(columns).toBe(size.width >= 1440 ? 6 : 2);
      const first = high(page).locator(".resource-row").first();
      if (size.width >= 1440 && !size.touch)
        await expectInView(
          page,
          high(page),
          other(page).locator(".resource-row").nth(0),
          other(page).locator(".resource-row").nth(1),
        );
      if (size.width === 360)
        await expectInView(
          page,
          high(page).getByRole("heading", { level: 2 }),
          high(page).getByText("Transactions in doubt. Other systems"),
          first,
        );
      // Its whole row opens an incident.
      await expectTargets(high(page), ".resource-row", control(size));
    });

    test("the drawer keeps the record in one row per step and its offers in reach", async ({
      page,
    }) => {
      await transactionsPlatform(page, { capabilities: operator });
      await openIncident(page, ids.caller);
      const detail = await box(sheet(page));
      if (size.width < 768)
        expect(detail).toMatchObject({
          x: 0,
          y: 0,
          width: size.width,
          height: size.height,
        });
      else {
        expect(detail.width).toBeGreaterThanOrEqual(300);
        expect(detail.width).toBeLessThanOrEqual(420.5);
      }
      const waitsOn = drawer(page).getByRole("region", { name: "Waits on" });
      if (size.width === 360 || size.width >= 1440)
        await expectInView(
          page,
          drawer(page).locator("#incident-detail-title"),
          drawer(page).locator('.notice[data-tone="danger"]'),
          waitsOn,
        );
      const rows = drawer(page).locator(".txn-step:not(.txn-head)");
      await expect(rows).toHaveCount(3);
      for (const row of await rows.all()) expect(await columnsOf(row)).toBe(2);
      await expectNoSideways(page, sheet(page), rows);
      expect(
        await cutText(
          drawer(page),
          ".c-step strong, .c-how, .step-word, .c-key code, .txn-facts dd, .txn-links code, .offers p",
        ),
      ).toEqual([]);
      await expectTargets(drawer(page), ".copy-key", icon(size), icon(size));
      await expectTargets(offers(page), "button", control(size));
      await expectTargets(waitsOn, "button", large(size) ? 44 : 32);
      // The parts come in the order a person decides by.
      const order = await drawer(page).evaluate((element) =>
        [
          "#incident-detail-title",
          '.notice[data-tone="danger"]',
          ".txn-links",
          ".txn-facts",
          ".txn-record",
          ".offers",
          ".recent-events",
        ].map((selector) =>
          Math.round(
            element.querySelector(selector)!.getBoundingClientRect().top,
          ),
        ),
      );
      expect(order).toEqual([...order].sort((a, b) => a - b));
    });

    test("each decision's dialog keeps its title and its buttons in view", async ({
      page,
    }) => {
      const platform = await transactionsPlatform(page, {
        capabilities: reconciler,
      });
      for (const [id, names] of [
        [ids.called, ["Retry the undo", "Mark as reconciled", "Force end"]],
        [ids.unknown, ["It happened", "It didn't happen"]],
        [ids.caller, ["Wait again"]],
      ] as const) {
        await openIncident(page, id);
        for (const name of names) {
          await offer(page, name).click();
          const asked = dialog(page, name);
          await expect(asked).toBeVisible();
          if (name === "Mark as reconciled")
            await asked.getByRole("radio", { name: /^Finished/ }).check();
          const panel = await box(asked);
          expect(panel.width).toBeCloseTo(Math.min(520, size.width - 16), 0);
          // 40 px above and 16 px below at most; on a phone, 8 px beside.
          expect(panel.height).toBeLessThanOrEqual(size.height - 16 + 0.5);
          await expectInView(
            page,
            asked.getByRole("heading", { name }),
            asked.getByRole("button", { name: "Cancel", exact: true }),
            asked.getByRole("button", { name, exact: true }),
          );
          await expectNoSideways(page, asked);
          expect(
            await cutText(
              asked,
              ".tick code, .tick span, .stays li, .confirm-reason, label, p",
            ),
            name,
          ).toEqual([]);
          if (name === "Mark as reconciled")
            await expectTargets(asked, ".tick", large(size) ? 44 : 32);
          await expectTargets(
            asked.locator(".dialog-footer"),
            "button",
            control(size),
          );
          await asked
            .getByRole("button", { name: "Cancel", exact: true })
            .click();
          await expect(asked).toHaveCount(0);
        }
      }
      expect(platform.sent).toEqual([]);
    });

    test("the Overview panel sits last and holds its counts and link", async ({
      page,
    }) => {
      await transactionsPlatform(page);
      await page.goto("/operate/overview");
      const panel = page.getByRole("article", {
        name: "Transactions",
        exact: true,
      });
      await expect(panel.locator("dd")).toHaveText(["4", "1"]);
      await expectNoSideways(page, panel);
      expect(await cutText(panel, "dt, dd, a, h3")).toEqual([]);
      await expectTargets(panel, "a", large(size) ? 44 : 32);
    });
  });

test.describe("on a record's own width", () => {
  test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

  test("five columns start at 720 px, four at 560 px, and no cell is cut on either side", async ({
    page,
  }) => {
    await transactionsPlatform(page);
    await openRunDetail(page, runs.loop);
    const section = transactionsOf(page);
    const record = block(page, "move[4]").locator(".txn-record");
    /** Gives the record this width inside its border, by its section's, whose padding follows its width. */
    const widen = async (width: number) => {
      let outer = width + 34;
      for (let turn = 0; turn < 3; turn++) {
        await section.evaluate(
          (element, value) =>
            ((element as HTMLElement).style.width = `${value}px`),
          outer,
        );
        outer +=
          width - (await record.evaluate((element) => element.clientWidth));
      }
      expect(await record.evaluate((element) => element.clientWidth)).toBe(
        width,
      );
    };
    const rows = section.locator(".txn-step:not(.txn-head)");
    for (const [width, columns] of [
      [760, 5],
      [720, 5],
      [719, 4],
      [560, 4],
      [559, 2],
      [320, 2],
    ] as const) {
      await widen(width);
      for (const row of await rows.all())
        expect(await columnsOf(row), `${width} px`).toBe(columns);
      for (const part of [...(await rows.all()), record])
        expect(
          await part.evaluate((node) => node.scrollWidth - node.clientWidth),
          `${width} px`,
        ).toBeLessThanOrEqual(0);
      expect(
        await cutText(
          section,
          ".c-step strong, .c-how, .step-word, .step-note, .c-key code",
        ),
        `${width} px`,
      ).toEqual([]);
    }
  });
});

test.describe("at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

  test("a record of one step leaves Resolve this incident in view, and a longer one scrolls to it", async ({
    page,
  }) => {
    await transactionsPlatform(page, { capabilities: reconciler });
    await openIncident(page, ids.loop);
    expect(loop.transaction!.participants).toHaveLength(1);
    await expectInView(
      page,
      offers(page).getByRole("heading", { name: "Resolve this incident" }),
    );
    await openIncident(page, ids.caller);
    expect(caller.transaction!.participants).toHaveLength(3);
    await offers(page).scrollIntoViewIfNeeded();
    await expectInView(
      page,
      offers(page).getByRole("heading", { name: "Resolve this incident" }),
    );
  });

  test("a refused result, its message and the confirm are in view together", async ({
    page,
  }) => {
    await transactionsPlatform(page, { capabilities: reconciler });
    await page.route(`${environment}/incidents/*/resolve`, (route) =>
      route.fulfill({
        status: 422,
        json: {
          code: "WV-RUNTIME-OUTPUT",
          message: "The reconciliation record doesn't fit the transaction",
        },
      }),
    );
    await openIncident(page, ids.called);
    const name = "Mark as reconciled";
    await offer(page, name).click();
    const asked = dialog(page, name);
    await asked.getByRole("radio", { name: /^Finished/ }).check();
    const lines = asked.getByRole("checkbox");
    await expect(lines).toHaveCount(2);
    for (const line of await lines.all()) await line.check();
    const result = asked.getByLabel("Result of the transaction (JSON)");
    await result.fill('{"order": 1}');
    await asked.getByLabel("Reason").fill("Both systems agree");
    await asked.getByLabel("Evidence reference").fill("OPS-2304");
    await asked.getByRole("button", { name, exact: true }).click();
    const refusal = asked.getByText(
      "The reconciliation record doesn't fit the transaction",
    );
    await expect(refusal).toBeVisible();
    await expectInView(
      page,
      asked.getByRole("heading", { name }),
      result,
      refusal,
      asked.getByRole("button", { name, exact: true }),
    );
  });
});
