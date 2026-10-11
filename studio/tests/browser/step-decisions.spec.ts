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
// A decision's conditions, the URL of Call an API and schema fields in step
// details: what a person sees, types, drops and undoes, and how the new
// controls line up with the rest of the form at every supported size.
import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  openStepFixture,
  sizes,
  stepFixture,
  type StepDetailsPage,
} from "./step-details-po";
import { sourceText } from "./support";
import {
  hasPython,
  localAuthoring,
  stopLocalAuthoring,
} from "./local-authoring";
import { openHttpStep } from "./step-http";
import {
  box,
  boxes,
  clippedText,
  contentEdges,
  density,
  expectAligned,
  showParameters,
  type Box,
} from "./step-geometry";

const allSizes = [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
];

/** The Input pane, shown as a tab where the panes are tabs. */
async function inputPane(details: StepDetailsPage): Promise<Locator> {
  if ((details.page.viewportSize()?.width ?? 1440) < 768)
    await details.dialog
      .getByRole("tablist", { name: "Step details panes" })
      .getByRole("tab", { name: "Input", exact: true })
      .click();
  return details.dialog.locator(".sd-input");
}

/** The content edges of the visible tab: every field starts and ends on them. */
const paneEdges = (details: StepDetailsPage) =>
  contentEdges(details.dialog.locator(".sd-tabpanel:not([hidden])"));

/** Nothing scrolls the page sideways. */
const noSideways = (page: Page) =>
  page.evaluate(
    () => document.documentElement.scrollWidth <= window.innerWidth + 1,
  );

for (const size of sizes)
  test.describe(`decisions at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const sheet = size.width < 768;

    test("decision paths add, move and refuse to drop their steps", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("route-by-value");
      const paths = details.field("cases");
      const first = paths.getByRole("group", { name: "Path 1 of 2" });
      const remove = first.getByRole("button", { name: "Remove Path 1 of 2" });
      await expect(remove).toBeDisabled();
      await expect(remove).toHaveAttribute(
        "title",
        "Move or delete its steps first.",
      );
      await expect(first).toContainText("→ notify-sales");
      await paths.getByRole("button", { name: "Add path" }).click();
      await expect(
        paths.getByRole("group", { name: "Path 3 of 3" }),
      ).toBeVisible();
      await paths.getByRole("button", { name: "Move Path 2 of 3 up" }).click();
      await details.close();
      const decision = parse(await sourceText(page)).spec.steps[2];
      expect(decision.cases).toHaveLength(3);
      expect(JSON.stringify(decision.cases[0].when)).toContain("/input/tier");
      expect(decision.cases[1].steps[0].id).toBe("notify-sales");
    });

    test("conditions show all or any from the second row, take dropped data and offer its tests", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("route-by-value");
      const when = details
        .field("cases")
        .getByRole("group", { name: "Path 2 of 2" })
        .locator('[data-param="case.when"]');
      await expect(
        when.getByRole("combobox", { name: "Match rules" }),
      ).toHaveCount(0);
      await when.getByRole("button", { name: "Add condition" }).click();
      await expect(
        when.getByRole("combobox", { name: "Match rules" }),
      ).toBeVisible();
      await expect(when).toContainText("Runs when");
      const data = when.getByRole("combobox", { name: "Condition 2 data" });
      if (sheet) {
        await data.fill("/input/amount");
        await data.press("Enter");
      } else {
        const input = await inputPane(details);
        await input
          .getByRole("combobox", { name: "Input source" })
          .selectOption("input");
        await input
          .getByRole("listitem", { name: /^amount, Number/ })
          .dragTo(data);
        await expect(when.getByRole("status")).toHaveText(
          "Mapped Amount (Number). The comparisons now offer number tests.",
        );
        await expect(when.locator(".condition-adapted")).toContainText(
          "The comparisons now offer number tests.",
        );
      }
      await when.getByRole("combobox", { name: "Condition 2 test" }).click();
      await page.getByRole("option", { name: "is greater than" }).click();
      await when.getByLabel("Condition 2 value", { exact: true }).fill("500");
      await details.close();
      const condition = parse(await sourceText(page)).spec.steps[2].cases[1]
        .when;
      expect(condition.op.name).toBe("and");
      expect(condition.op.args[1]).toEqual({
        op: { name: "gt", args: [{ ref: "/input/amount" }, { literal: 500 }] },
      });
    });

    test("Map to… puts data in a path's first empty condition and continues on its test", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("route-by-value");
      const when = details
        .field("cases")
        .getByRole("group", { name: "Path 1 of 2" })
        .locator('[data-param="case.when"]');
      await when.getByRole("button", { name: "Add condition" }).click();
      const input = await inputPane(details);
      await input
        .getByRole("combobox", { name: "Input source" })
        .selectOption("input");
      const tier = input.getByRole("listitem", { name: /^tier, / });
      await tier.focus();
      await page.keyboard.press("Enter");
      const menu = page.getByRole("menu", { name: "Map tier to" });
      await menu
        .getByRole("menuitem", { name: /^Runs when/ })
        .first()
        .click();
      await expect(
        when.getByRole("combobox", { name: "Condition 2 test" }),
      ).toBeFocused();
      await expect(when.getByRole("status")).toHaveText(
        "Mapped tier (Text). The comparisons now offer text tests.",
      );
      await when.getByLabel("Condition 2 value", { exact: true }).fill("gold");
      await details.close();
      const condition = parse(await sourceText(page)).spec.steps[2].cases[0]
        .when;
      expect(condition.op.args[1]).toEqual({
        op: {
          name: "eq",
          args: [{ ref: "/input/tier" }, { literal: "gold" }],
        },
      });
    });

    test("a value compares with other data through Fixed | Mapped, by keyboard", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("route-by-value");
      const when = details
        .field("cases")
        .getByRole("group", { name: "Path 2 of 2" })
        .locator('[data-param="case.when"]');
      const value = when.getByLabel("Condition 1 value", { exact: true });
      const mode = when.getByRole("radiogroup", {
        name: "Value mode for Condition 1 value",
      });
      // The value mode shows while the value has focus.
      await expect(mode).toBeHidden();
      await value.focus();
      await expect(mode).toBeVisible();
      await page.keyboard.press("Tab");
      await expect(mode.getByRole("radio", { name: "Fixed" })).toBeFocused();
      await page.keyboard.press("ArrowRight");
      await expect(mode.getByRole("radio", { name: "Mapped" })).toBeFocused();
      await expect(mode.getByRole("radio", { name: "Mapped" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
      const data = when.getByRole("combobox", { name: "Condition 1 value" });
      await data.fill("/input/status");
      await data.press("Enter");
      await data.press("Tab");
      await details.close();
      const condition = parse(await sourceText(page)).spec.steps[2].cases[1]
        .when;
      expect(condition).toEqual({
        op: {
          name: "eq",
          args: [{ ref: "/input/tier" }, { ref: "/input/status" }],
        },
      });
    });

    test("an unfinished condition stays on screen and asks to be finished, and nothing is written", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("route-by-value");
      const when = details
        .field("cases")
        .getByRole("group", { name: "Path 2 of 2" })
        .locator('[data-param="case.when"]');
      await when.getByRole("button", { name: "Add condition" }).click();
      const data = when.getByRole("combobox", { name: "Condition 2 data" });
      await data.fill("/input/amount");
      await data.press("Enter");
      await when.getByRole("combobox", { name: "Condition 2 test" }).focus();
      await page.keyboard.press("Tab");
      await page.keyboard.press("Tab");
      await expect(
        details.field("cases").getByRole("group", { name: "Path 2 of 2" }),
      ).toContainText("Finish this condition, or remove it.");
      await expect(data).toHaveValue("Input › Amount");
      await when.getByRole("button", { name: "Remove condition 2" }).click();
      await details.close();
      const fixture = parse(readFileSync(stepFixture, "utf8"));
      expect(parse(await sourceText(page)).spec.steps[2].cases).toEqual(
        fixture.spec.steps[2].cases,
      );
    });
  });

for (const size of allSizes)
  test.describe(`condition rows at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("rows keep one height and center line, and end on the card's edges", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("route-by-value");
      await showParameters(page, details.dialog);
      const sizes = await density(page);
      const card = details
        .field("cases")
        .getByRole("group", { name: "Path 1 of 2" });
      const when = card.locator('[data-param="case.when"]');
      // One row: no join line, the row right under the card's header.
      await expect(when.locator(".condition-intro")).toHaveCount(0);
      const head = await box(
        card.locator(":scope > weave-param-field > .param > .param-label-row"),
      );
      await when.getByRole("button", { name: "Add condition" }).click();
      await page.mouse.move(1, 1);
      const body = await contentEdges(
        card.locator(":scope > weave-param-field > .param > .param-control"),
      );
      const intro = when.locator(".condition-intro");
      await intro.scrollIntoViewIfNeeded();
      const introBox = await box(intro);
      expect(introBox.y - head.bottom).toBeLessThanOrEqual(16.5);
      const join = await box(intro.locator(".choice-field"));
      const words = await box(intro.locator(":scope > span"));
      expectAligned([join.height, sizes.row], "join height");
      expectAligned([join.middle, words.middle], "join line center");
      const lefts = [body.left, words.x];
      const rights = [body.right];
      for (const row of await when.locator(".condition-row").all()) {
        await row.scrollIntoViewIfNeeded();
        const data = await box(row.locator(".condition-data .ref-combo-field"));
        const test = await box(
          row.locator(".condition-operator .choice-field"),
        );
        const value = await box(row.locator(".condition-value"));
        const trash = await box(row.locator(".condition-remove"));
        expectAligned(
          [data.height, test.height, value.height, sizes.row],
          "row heights",
        );
        // The trash button is a row-height target.
        expectAligned([trash.height, sizes.row], "trash target");
        lefts.push(data.x);
        rights.push(trash.right);
        if (test.y === data.y) {
          // One line: data, test, value and trash share a center.
          expectAligned(
            [data.middle, test.middle, value.middle, trash.middle],
            "row centers",
          );
          expect(test.x).toBeGreaterThanOrEqual(data.right);
          expect(value.x).toBeGreaterThanOrEqual(test.right);
          expect(trash.x).toBeGreaterThanOrEqual(value.right);
        } else {
          // Two lines: data and trash, then test and value, on the same edges.
          expectAligned([data.middle, trash.middle], "first line center");
          expectAligned([test.middle, value.middle], "second line center");
          expectAligned([test.x, data.x], "second line start");
          expectAligned([value.right, trash.right], "second line end");
          rights.push(value.right);
          expect(test.y).toBeGreaterThanOrEqual(data.bottom);
        }
        if (size.width >= 600) expect(test.y).toBe(data.y);
        // The test's words fit its box.
        expect(
          await row
            .locator(".condition-operator input")
            .evaluate((input) => input.scrollWidth <= input.clientWidth + 1),
        ).toBe(true);
      }
      // The data shows as a pill, centered in its box.
      const first = when.locator(".condition-row").first();
      const pill = await box(first.locator(".condition-pill"));
      const field = await box(
        first.locator(".condition-data .ref-combo-field"),
      );
      expectAligned([pill.middle, field.middle], "pill center");
      expect(pill.x).toBeGreaterThan(field.x);
      expect(pill.right).toBeLessThanOrEqual(field.right);
      // Add condition starts on the rows' edge; the field menu, shown on
      // hover, ends on the trash buttons' edge and centers on its line.
      const add = when.getByRole("button", { name: "Add condition" });
      lefts.push((await box(add.locator("weave-icon"))).x);
      await add.hover();
      const menu = when.getByRole("button", {
        name: "Field menu for Runs when",
      });
      const menuBox = await box(menu);
      rights.push(menuBox.right);
      expectAligned([menuBox.middle, (await box(add)).middle], "menu line");
      expectAligned(lefts, "conditions left edge");
      expectAligned(rights, "conditions right edge");
      expect(await clippedText(details.dialog)).toEqual([]);
      expect(await noSideways(page)).toBe(true);
    });
  });

test.describe("the URL of a Call an API step", () => {
  test.skip(!hasPython, "Needs the repository's Python environment.");
  test.afterAll(() => stopLocalAuthoring());

  for (const size of sizes)
    test.describe(`at ${size.tag}`, () => {
      test.use({ viewport: { width: size.width, height: size.height } });
      const sheet = size.width < 768;

      test("shows its placeholders as pills, edits as text and keeps each value in step", async ({
        page,
      }) => {
        const details = await openHttpStep(page);
        await showParameters(page, details.dialog);
        const url = details.field("url");
        await expect(url.locator(".param-url-prefix")).toHaveText(
          "Connection address",
        );
        const view = url.locator(".param-url-view");
        await expect(view).toContainText("/customers/");
        await expect(view.locator(".ref-pill")).toHaveText(
          /Input\s*›\s*customerId/,
        );
        await expect(view).toContainText("/orders");
        await expect(url.locator(".param-url-part")).toHaveText(
          /^\{customerId\}/,
        );
        const part = details.field("url.path.customerId");
        await expect(
          part.getByRole("button", { name: "reference Input customerId" }),
        ).toBeVisible();
        const path = url.getByRole("textbox", { name: "URL" });
        await path.click();
        await expect(view).toHaveCount(0);
        await expect(path).toHaveValue("/customers/{customerId}/orders");
        await path.fill("/customers/{customerId}/orders/{orderId}");
        const order = details.field("url.path.orderId");
        await order.getByRole("textbox", { name: "orderId" }).fill("o-1");
        await path.fill("/orders/{orderId}");
        await expect(details.field("url.path.customerId")).toHaveCount(0);
        await details.close();
        const step = parse(await sourceText(page)).spec.steps[0];
        expect(step.with.object.path).toEqual({
          literal: { orderId: "o-1" },
        });
        await details.open("get-customer-orders");
        await showParameters(page, details.dialog);
        await expect(
          details.field("url").getByRole("textbox", { name: "URL" }),
        ).toHaveValue("/orders/{orderId}");
        if (sheet) return;
        // Bringing a placeholder back while typing brings its value back.
        const again = details
          .field("url")
          .getByRole("textbox", { name: "URL" });
        await again.click();
        await again.press(
          process.platform === "darwin" ? "Meta+ArrowRight" : "End",
        );
        await again.press("Backspace");
        await expect(details.field("url.path.orderId")).toHaveCount(0);
        await again.press("}");
        await expect(
          details
            .field("url.path.orderId")
            .getByRole("textbox", { name: "orderId" }),
        ).toHaveValue("o-1");
      });

      test("takes dropped data as a placeholder where it lands, and one Undo takes it back", async ({
        page,
      }) => {
        const details = await openHttpStep(page);
        const url = details.field("url");
        const path = url.getByRole("textbox", { name: "URL" });
        const input = await inputPane(details);
        const status = input.getByRole("listitem", { name: /^status, / });
        if (sheet) {
          await status.focus();
          await page.keyboard.press("Enter");
          await page
            .getByRole("menu", { name: "Map status to" })
            .getByRole("menuitem", { name: "URL", exact: true })
            .click();
        } else {
          const target = (await path.boundingBox())!;
          await status.dragTo(path, {
            targetPosition: { x: target.width - 4, y: target.height / 2 },
          });
        }
        await expect(details.dialog.locator("[aria-live=polite]")).toHaveText(
          "Mapped Input › status to URL",
        );
        await expect(path).toHaveValue(
          "/customers/{customerId}/orders/{status}",
        );
        await expect(
          details
            .field("url.path.status")
            .getByRole("button", { name: "reference Input status" }),
        ).toBeVisible();
        await page.keyboard.press("ControlOrMeta+z");
        await expect(details.field("url.path.status")).toHaveCount(0);
        await expect(path).toHaveValue("/customers/{customerId}/orders");
        await details.close();
        const step = parse(await sourceText(page)).spec.steps[0];
        expect(Object.keys(step.with.object.path.object)).toEqual([
          "customerId",
        ]);
      });
    });

  for (const size of allSizes)
    test.describe(`URL alignment at ${size.tag}`, () => {
      test.use({ viewport: { width: size.width, height: size.height } });

      test("the URL and its values share the pane's edges, centers and heights", async ({
        page,
      }) => {
        const details = await openHttpStep(page);
        await showParameters(page, details.dialog);
        await page.mouse.move(1, 1);
        const sizes = await density(page);
        const edges = await paneEdges(details);
        const url = details.field("url");
        await url.scrollIntoViewIfNeeded();
        const field = await box(url.locator(".param-url"));
        const prefix = await box(url.locator(".param-url-prefix"));
        const pill = await box(url.locator(".param-url-view .ref-pill"));
        const label = await box(url.locator(".param-label"));
        expectAligned([field.height, sizes.control], "URL height");
        expectAligned([field.x, label.x, edges.left], "URL left edge");
        expectAligned([field.right, edges.right], "URL right edge");
        // The connection's address leads the path where there is room for both.
        if (size.width >= 600) {
          expectAligned([prefix.middle, field.middle], "prefix center");
          expectAligned([prefix.x, field.x + 1], "prefix start");
        } else await expect(url.locator(".param-url-prefix")).toBeHidden();
        expectAligned([pill.middle, field.middle], "pill center");
        expect(pill.x).toBeGreaterThanOrEqual(Math.max(prefix.right, field.x));
        expect(pill.right).toBeLessThanOrEqual(field.right);
        const row = details.field("url.path.customerId");
        const key = await box(url.locator(".param-url-key"));
        const value = await box(row);
        expectAligned([value.height, sizes.row], "value height");
        expectAligned([key.x, edges.left], "value name edge");
        expectAligned([value.right, edges.right], "value right edge");
        if (value.y < key.bottom)
          expectAligned([key.middle, value.middle], "value row center");
        else expectAligned([value.x, edges.left], "value under its name");
        const mode = await box(row.locator(".param-mode"));
        expectAligned([mode.middle, value.middle], "value mode center");
        expectAligned([mode.right, value.right - 12], "value mode inset", 2);
        // The next field starts one field gap below the values.
        const next = await box(details.field("query"));
        expectAligned(
          [next.y - value.bottom, sizes.fieldGap],
          "gap after the URL",
        );
        expect(await clippedText(details.dialog)).toEqual([]);
        expect(await noSideways(page)).toBe(true);
      });

      test("a long path ellipsizes at rest and scrolls while edited", async ({
        page,
      }) => {
        const long = `/${Array.from({ length: 24 }, (_, i) => `segment-${i}`).join("/")}/{customerId}`;
        const details = await openHttpStep(page, long);
        await showParameters(page, details.dialog);
        const url = details.field("url");
        await url.scrollIntoViewIfNeeded();
        const field = await box(url.locator(".param-url"));
        const view = url.locator(".param-url-view");
        expect((await box(view)).right).toBeLessThanOrEqual(field.right);
        await expect(view).toHaveAttribute("title", long);
        const path = url.getByRole("textbox", { name: "URL" });
        await path.click();
        // The end of the line: End, or Command+Right on a Mac.
        await path.press(
          process.platform === "darwin" ? "Meta+ArrowRight" : "End",
        );
        expect(
          await path.evaluate(
            (input) =>
              input.scrollWidth > input.clientWidth && input.scrollLeft > 0,
          ),
        ).toBe(true);
        expect(await noSideways(page)).toBe(true);
      });
    });
});

for (const size of sizes)
  test.describe(`schema fields at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the trigger's input fields use the schema designer, on the pane's edges", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.trigger().dblclick();
      await expect(details.dialogFor("Manual form trigger")).toBeVisible();
      await showParameters(page, details.dialog);
      const field = details.field("inputSchema");
      await expect(
        field.getByRole("button", { name: "Paste sample JSON" }),
      ).toBeVisible();
      const edges = await paneEdges(details);
      const rows = await boxes(field.locator(".sd-row"));
      expect(rows.length).toBe(5);
      const sizes = await density(page);
      for (const row of rows) {
        expectAligned([row.x, edges.left], "schema row left edge");
        expectAligned([row.right, edges.right], "schema row right edge");
        expect(row.height).toBeGreaterThanOrEqual(sizes.row);
        expect(row.height).toBeLessThanOrEqual(sizes.row + 2);
      }
      await field.getByRole("button", { name: "Add field" }).click();
      const name = field.getByRole("textbox", { name: "Field name" }).last();
      await expect(name).toBeFocused();
      await name.fill("orderId");
      await details.close();
      const schema = parse(await sourceText(page)).spec.inputSchema;
      expect(Object.keys(schema.properties)).toContain("orderId");
      expect(await clippedText(page.locator("body"))).toEqual([]);
    });
  });

test.describe("Paste sample JSON in step details", () => {
  test.skip(!hasPython, "Needs the repository's Python environment.");
  test.use({ viewport: { width: 1440, height: 900 } });
  test.afterAll(() => stopLocalAuthoring());

  test("adds the fields of an example to the trigger's input", async ({
    page,
  }) => {
    await localAuthoring(page);
    const details = await openStepFixture(page);
    await details.trigger().dblclick();
    const field = details.field("inputSchema");
    await field.getByRole("button", { name: "Paste sample JSON" }).click();
    const dialog = page.getByRole("dialog", {
      name: "Find fields from an example",
    });
    await dialog.getByLabel("Example 1").fill('{"orderId": "o-1"}');
    await dialog.getByRole("button", { name: "Find fields" }).click();
    // The repository's Python reads the example; it may take a moment to start.
    await expect(dialog).toHaveCount(0, { timeout: 30_000 });
    await expect(field.locator(".sd-row", { hasText: "orderId" })).toHaveCount(
      1,
    );
    await details.close();
    const schema = parse(await sourceText(page)).spec.inputSchema;
    expect(Object.keys(schema.properties)).toEqual([
      "customerId",
      "email",
      "status",
      "amount",
      "tier",
      "orderId",
    ]);
  });
});
