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
// The trigger's step details (Test event, its input fields, the event as
// Output) and workflow settings, drawn by the same form as step details.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse } from "yaml";
import { openStepFixture, sizes } from "./step-details-po";
import { walkFocus } from "./step-focus";
import {
  box,
  boxes,
  clippedText,
  contentEdges,
  density,
  expectAligned,
  intersects,
  textBox,
} from "./step-geometry";
import { sourceText } from "./support";

for (const size of sizes)
  test.describe(`the trigger and workflow settings at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const sheet = size.width < 768;

    test("the trigger shows a Test event pane with Generate sample and Paste sample JSON", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.trigger().dblclick();
      const dialog = details.dialogFor("Manual form trigger");
      await expect(dialog).toBeVisible();
      const pane = (name: string) =>
        sheet
          ? dialog
              .getByRole("tablist", { name: "Step details panes" })
              .getByRole("tab", { name })
              .click()
          : Promise.resolve();
      await pane("Test event");
      await dialog.getByRole("button", { name: "Generate sample" }).click();
      await expect(
        dialog.locator(".sd-input").getByLabel("Customer ID"),
      ).toHaveValue("text");
      await pane("Parameters");
      await expect(details.topFields()).toHaveCount(1);
      await expect(
        details
          .field("inputSchema")
          .getByRole("button", { name: "Paste sample JSON" }),
      ).toBeVisible();
      await pane("Output");
      await dialog
        .locator(".sd-output")
        .getByRole("radio", { name: "JSON" })
        .click();
      await expect(dialog.locator(".sd-output .pane-json")).toContainText(
        '"customerId": "text"',
      );
    });

    test("workflow settings show Name and Version, with Stop runs after and Callable as options", async ({
      page,
    }) => {
      await openStepFixture(page);
      const gear = page.getByRole("button", {
        name: "Workflow settings",
        exact: true,
      });
      await gear.click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      await expect(
        dialog.locator(".param-form > [data-param] .param-label"),
      ).toHaveText(["Name", "Version"]);
      await dialog.getByRole("button", { name: "Add option" }).click();
      await expect(
        page.getByRole("menuitem", { name: /^Stop runs after/ }),
      ).toBeVisible();
      await expect(
        page.getByRole("menuitem", { name: /^Callable by other workflows/ }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(
        dialog.getByRole("region", { name: "Connection slots" }),
      ).toContainText("ledger-db · weave-postgresql@1.0.0");
      await expect(
        dialog.getByRole("region", { name: "AI profiles" }),
      ).toContainText("default · ollama · qwen3:4b");
      await dialog.getByRole("textbox", { name: "Version" }).fill("1.1.0");
      await dialog.getByRole("button", { name: "Close", exact: true }).click();
      await expect(dialog).toHaveCount(0);
      await expect(gear).toBeFocused();
      expect(parse(await sourceText(page)).metadata.version).toBe("1.1.0");
    });

    test("workflow settings rename a slot, keep a used one, and open the step that uses a profile", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      const gear = page.getByRole("button", {
        name: "Workflow settings",
        exact: true,
      });
      await gear.click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      const slots = dialog.getByRole("region", { name: "Connection slots" });
      await expect(slots).toContainText("Required · Used by lookup");
      await slots
        .getByRole("button", { name: "Actions for slot ledger-db" })
        .click();
      // A slot a step uses stays; the menu says why.
      const remove = page.getByRole("menuitem", { name: /^Remove slot/ });
      await expect(remove).toHaveAttribute("aria-disabled", "true");
      await expect(remove).toContainText(
        "Used by lookup. Choose another slot there first.",
      );
      await page.getByRole("menuitem", { name: "Rename slot" }).click();
      const prompt = page.getByRole("dialog", {
        name: "Rename slot ledger-db",
      });
      await prompt
        .getByRole("textbox", { name: "Slot name" })
        .fill("Orders DB");
      await prompt.getByRole("button", { name: "Rename", exact: true }).click();
      await expect(prompt).toHaveCount(0);
      await expect(slots).toContainText("orders-db · weave-postgresql@1.0.0");
      await expect(dialog.locator("[aria-live=polite]")).toHaveText(
        "Renamed the slot to orders-db.",
      );
      await dialog
        .getByRole("region", { name: "AI profiles" })
        .getByRole("button", { name: "Actions for AI profile default" })
        .click();
      await page.getByRole("menuitem", { name: "Open summarize" }).click();
      await expect(dialog).toHaveCount(0);
      await expect(details.dialogFor("summarize")).toBeVisible();
      await details.close();
      const source = parse(await sourceText(page));
      expect(Object.keys(source.spec.connections)).toEqual(["orders-db"]);
      expect(
        source.spec.steps.find((step: { id: string }) => step.id === "lookup")
          .connection,
      ).toBe("orders-db");
    });
  });

/** The page never scrolls sideways, and neither does the box given. */
async function noSidewaysScroll(page: Page, scroller: Locator) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    "the page scrolls sideways",
  ).toBe(true);
  expect(
    await scroller.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
    "the dialog scrolls sideways",
  ).toBe(true);
}
/** Text a select shows that its box cuts off ("minu" for "minutes"). */
async function clippedSelects(scope: Locator): Promise<string[]> {
  return scope.evaluate((root) =>
    [...root.querySelectorAll<HTMLSelectElement>("select")]
      .filter((select) => select.getClientRects().length > 0)
      .filter((select) => {
        const style = getComputedStyle(select);
        const probe = document.createElement("span");
        probe.style.font = style.font;
        probe.style.position = "absolute";
        probe.style.whiteSpace = "pre";
        probe.textContent = select.selectedOptions[0]?.text.trim() ?? "";
        document.body.append(probe);
        const text = probe.getBoundingClientRect().width;
        probe.remove();
        // The text, its padding and room for the arrow.
        const room =
          select.clientWidth -
          parseFloat(style.paddingLeft) -
          parseFloat(style.paddingRight) -
          16;
        return text > room;
      })
      .map((select) => select.selectedOptions[0]?.text.trim() ?? ""),
  );
}

test.describe("workflow settings and the trigger's details, laid out", () => {
  for (const size of [
    ...sizes,
    { tag: "360x640", width: 360, height: 640 },
    { tag: "640x360", width: 640, height: 360 },
    { tag: "1440x900-touch", width: 1440, height: 900, touch: true },
  ])
    test.describe(`at ${size.tag}`, () => {
      test.use({
        viewport: { width: size.width, height: size.height },
        hasTouch: "touch" in size,
      });

      test("workflow settings keep one left edge and one right edge, rows line up, and nothing clips or overlaps", async ({
        page,
      }) => {
        await openStepFixture(page);
        const d = await density(page);
        const gear = page.getByRole("button", {
          name: "Workflow settings",
          exact: true,
        });
        // The gear sits on the workflow's name line, a whole target of its own.
        const gearBox = await box(gear);
        expectAligned(
          [gearBox.width, gearBox.height, d.touch ? 44 : 36],
          "gear size",
        );
        expectAligned(
          [
            gearBox.middle,
            (await box(page.locator(".editor-identity h1"))).middle,
          ],
          "gear on the name line",
        );
        for (const other of await boxes(
          page.locator(".editor-identity > :not(.workflow-settings-button)"),
        ))
          expect(
            intersects(gearBox, other),
            "gear overlaps the name line",
          ).toBe(false);

        await gear.click();
        const dialog = page.getByRole("dialog", { name: "Workflow settings" });
        await dialog.getByRole("button", { name: "Add option" }).click();
        await page.getByRole("menuitem", { name: /^Stop runs after/ }).click();
        const scope = dialog.locator(".sd-form-scope");
        await expect(scope.locator(".param-form > [data-param]")).toHaveCount(
          3,
        );
        const edge = await box(scope);

        // One left edge: labels, fields, Add option, headings, lists, hints.
        const starts = [
          ...(await boxes(
            scope.locator(".param-form > [data-param] .param-label"),
          )),
          ...(await boxes(
            scope.locator(".param-form .param-control > :first-child"),
          )),
          await box(scope.getByRole("button", { name: "Add option" })),
          ...(await boxes(scope.locator(".wf-section > *"))),
        ].map((b) => b.x);
        expectAligned([edge.x, ...starts], "left edges");

        // One right edge for what ends there at rest.
        const ends = [
          ...(await boxes(scope.locator(".param-form .param-control"))),
          ...(await boxes(
            scope.locator(".param-form .param-control > :last-child"),
          )),
          ...(await boxes(scope.locator(".wf-list"))),
        ].map((b) => b.right);
        expectAligned([edge.right, ...ends], "right edges");

        // Density: fields at the control height, row menus at the target size.
        for (const field of await boxes(
          scope.locator(".param-form .param-input"),
        ))
          expect(field.height).toBe(d.control);
        const rows = scope.locator(".wf-row");
        await expect(rows).toHaveCount(2);
        for (let index = 0; index < 2; index++) {
          const row = rows.nth(index);
          const menu = await box(row.locator(".row-menu-toggle"));
          const text = await box(row.locator(".wf-text"));
          expectAligned(
            [menu.width, menu.height, d.touch ? 44 : 32],
            `row ${index + 1} menu size`,
          );
          expectAligned([menu.middle, text.middle], `row ${index + 1} center`);
          expect(intersects(menu, text), `row ${index + 1} menu overlaps`).toBe(
            false,
          );
          // The name starts the row's text, on the list's inner edge.
          expectAligned(
            [(await textBox(row.locator("code"))).x, text.x],
            `row ${index + 1} text start`,
          );
        }
        const rowBoxes = await boxes(rows);
        expect(intersects(rowBoxes[0], rowBoxes[1])).toBe(false);

        // Nothing cut off, and nothing scrolls sideways.
        expect(await clippedText(dialog, "")).toEqual([]);
        expect(await clippedSelects(scope)).toEqual([]);
        await noSidewaysScroll(page, dialog);
        await dialog
          .getByRole("region", { name: "AI profiles" })
          .scrollIntoViewIfNeeded();
        expect(await clippedText(dialog, "")).toEqual([]);
      });

      test("the trigger's panes keep their edges, and nothing clips", async ({
        page,
      }) => {
        const details = await openStepFixture(page);
        const d = await density(page);
        await details.trigger().dblclick();
        const dialog = details.dialogFor("Manual form trigger");
        const sheet = size.width < 768;
        const show = async (name: string) => {
          if (sheet)
            await dialog
              .getByRole("tablist", { name: "Step details panes" })
              .getByRole("tab", { name })
              .click();
          else if (size.width < 1100 && name !== "Parameters")
            await dialog
              .getByRole("tablist", { name: "Data" })
              .getByRole("tab", { name })
              .click();
        };
        await show("Test event");
        const event = dialog.locator(".sd-input");
        await event.getByRole("button", { name: "Generate sample" }).click();
        await expect(event.getByLabel("Customer ID")).toHaveValue("text");
        // Test event: the button, labels and fields on the pane's one left
        // edge, the fields on one right edge, at the control height.
        const body = event.locator(".pane-content");
        const content = await box(body);
        // A select's box is its field, around its input and its arrow.
        const fields = await boxes(
          body.locator(
            ".choice-field, input:not(.choice-field input), select, textarea",
          ),
        );
        expect(fields.length).toBeGreaterThanOrEqual(5);
        expectAligned(
          [
            content.x,
            (await box(body.getByRole("button", { name: "Generate sample" })))
              .x,
            ...(await boxes(body.locator("label"))).map((b) => b.x),
            ...fields.map((b) => b.x),
          ],
          "Test event left edges",
        );
        expectAligned(
          [content.right, ...fields.map((b) => b.right)],
          "Test event right edges",
        );
        for (const field of fields) expect(field.height).toBe(d.control);
        expect(await clippedText(dialog)).toEqual([]);

        // Parameters: the input fields' buttons and rows on the form's edges.
        await show("Parameters");
        const schema = details.field("inputSchema");
        await expect(
          schema.getByRole("button", { name: "Paste sample JSON" }),
        ).toBeVisible();
        const tabpanel = await contentEdges(
          dialog.locator(".sd-tabpanel:not([hidden])"),
        );
        const form = { x: tabpanel.left, right: tabpanel.right };
        const paste = await box(
          schema.getByRole("button", { name: "Paste sample JSON" }),
        );
        expectAligned([form.x, paste.x], "Paste sample JSON on the left edge");
        const rows = await boxes(schema.locator(".sd-row"));
        expect(rows.length).toBe(5);
        for (const row of rows) {
          expectAligned([row.x, form.x], "input field row left edge");
          expectAligned([row.right, form.right], "input field row right edge");
        }
        expect(await clippedText(dialog)).toEqual([]);

        // Output: the event, as the workflow input every step reads.
        await show("Output");
        await expect(dialog.locator(".sd-output")).toBeVisible();
        expect(await clippedText(dialog)).toEqual([]);
        await noSidewaysScroll(page, dialog);
      });

      test("workflow settings take every key on the way, with names, rings and 24 px targets", async ({
        page,
      }) => {
        await openStepFixture(page);
        await page
          .getByRole("button", { name: "Workflow settings", exact: true })
          .click();
        const dialog = page.getByRole("dialog", { name: "Workflow settings" });
        expect(await walkFocus(page, dialog)).toBeGreaterThanOrEqual(6);
      });
    });
});
