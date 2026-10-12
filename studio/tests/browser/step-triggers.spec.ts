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
import { readFileSync } from "node:fs";
import { parse, stringify } from "yaml";
import {
  openStepFixture,
  settingsGear,
  sizes,
  stepFixture,
  yamlFile,
} from "./step-details-po";
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

/** The fixture with two more slots no step uses, and a long slot name. */
function withSpareSlots(long = false) {
  const document = parse(readFileSync(stepFixture, "utf8"));
  document.spec.connections = {
    ...document.spec.connections,
    archive: { connector: "weave-http@2.0.0", required: false },
    spare: { connector: "weave-http@2.0.0", required: false },
    ...(long
      ? {
          "customer-orders-archive-in-the-regional-data-warehouse-eu-west": {
            connector: "weave-postgresql-with-a-very-long-connector@1.0.0",
            required: false,
          },
        }
      : {}),
  };
  return yamlFile("order-intake.yaml", stringify(document));
}

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
      const gear = settingsGear(page);
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
      const gear = settingsGear(page);
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

for (const size of sizes)
  test.describe(`workflow settings, keys and unapplied work at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the gear has a name of its own", async ({ page }) => {
      await openStepFixture(page);
      await expect(settingsGear(page)).toHaveAccessibleName(
        "Open workflow settings",
      );
      await expect(
        page.getByRole("button", { name: "Open workflow settings" }),
      ).toHaveCount(1);
    });

    test("workflow settings open on the Name box, not on a field's menu", async ({
      page,
    }) => {
      await openStepFixture(page);
      await settingsGear(page).click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      await expect(
        dialog.getByRole("textbox", { name: "Name", exact: true }),
      ).toBeFocused();
    });

    test("focus stays in workflow settings after Rename slot and Remove slot", async ({
      page,
    }) => {
      await openStepFixture(page, withSpareSlots());
      await settingsGear(page).click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      const menuOf = (name: string) =>
        dialog.getByRole("button", { name: `Actions for slot ${name}` });
      const inDialog = () =>
        page.evaluate(
          () =>
            !!document.activeElement?.closest(
              '[role="dialog"][aria-modal="true"]',
            ),
        );
      const rename = async (from: string) => {
        await menuOf(from).click();
        await page.getByRole("menuitem", { name: "Rename slot" }).click();
        return page.getByRole("dialog", { name: `Rename slot ${from}` });
      };
      // Cancel gives focus back to the row's menu.
      let prompt = await rename("ledger-db");
      await prompt.getByRole("button", { name: "Cancel", exact: true }).click();
      await expect(prompt).toHaveCount(0);
      await expect(menuOf("ledger-db")).toBeFocused();
      // Rename gives it to the renamed row's menu.
      prompt = await rename("ledger-db");
      await prompt
        .getByRole("textbox", { name: "Slot name" })
        .fill("orders-db");
      await prompt.getByRole("button", { name: "Rename", exact: true }).click();
      await expect(prompt).toHaveCount(0);
      await expect(menuOf("orders-db")).toBeFocused();
      // Removing a row gives focus to the next row's menu, else the one before.
      const remove = async (name: string) => {
        await menuOf(name).click();
        await page.getByRole("menuitem", { name: /^Remove slot/ }).click();
        await expect(menuOf(name)).toHaveCount(0);
      };
      await remove("archive");
      await expect(menuOf("spare")).toBeFocused();
      await remove("spare");
      await expect(menuOf("orders-db")).toBeFocused();
      await page.keyboard.press("Tab");
      expect(await inDialog()).toBe(true);
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Shift+Tab");
      expect(await inDialog()).toBe(true);
    });

    test("closing workflow settings names what isn't applied, and step details name it later", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await settingsGear(page).click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      await dialog.getByRole("button", { name: "Add option" }).click();
      await page.getByRole("menuitem", { name: /^Stop runs after/ }).click();
      await dialog
        .getByRole("textbox", { name: "Stop runs after", exact: true })
        .fill("abc");
      await expect(dialog.getByText("Not applied")).toBeVisible();
      await dialog.getByRole("button", { name: "Close", exact: true }).click();
      await expect(dialog).toHaveCount(0);
      await expect(
        page.getByText(
          "Not applied yet: Stop runs after. What you typed is kept until you finish it.",
          { exact: true },
        ),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Dismiss notification" })
        .first()
        .click();
      await details.openWithKeyboard("check-customer");
      await details.close();
      await expect(
        page.getByText(
          "Not applied yet: Stop runs after in workflow settings. What you typed is kept until you finish it.",
          { exact: true },
        ),
      ).toBeVisible();
    });

    test("a dialog opened from workflow settings gives focus back to its opener there", async ({
      page,
    }) => {
      await openStepFixture(page);
      await settingsGear(page).click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      const menu = dialog.getByRole("button", {
        name: "Actions for slot ledger-db",
      });
      await menu.click();
      await page.getByRole("menuitem", { name: "Rename slot" }).click();
      const prompt = page.getByRole("dialog", {
        name: "Rename slot ledger-db",
      });
      await expect(prompt).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(prompt).toHaveCount(0);
      await expect(dialog).toBeVisible();
      await expect(menu).toBeFocused();
      await page.keyboard.press("Tab");
      expect(
        await page.evaluate(
          () => !!document.activeElement?.closest(".modal-panel"),
        ),
      ).toBe(true);
    });

    test("opening a step from a profile names what isn't applied in workflow settings", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await settingsGear(page).click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      await dialog.getByRole("button", { name: "Add option" }).click();
      await page.getByRole("menuitem", { name: /^Stop runs after/ }).click();
      await dialog
        .getByRole("textbox", { name: "Stop runs after", exact: true })
        .fill("abc");
      await expect(dialog.getByText("Not applied")).toBeVisible();
      await dialog
        .getByRole("button", { name: "Actions for AI profile default" })
        .click();
      await page.getByRole("menuitem", { name: "Open summarize" }).click();
      await expect(dialog).toHaveCount(0);
      await expect(details.dialogFor("summarize")).toBeVisible();
      await expect(
        page.getByText(
          "Not applied yet: Stop runs after. What you typed is kept until you finish it.",
          { exact: true },
        ),
      ).toBeVisible();
    });

    test("Escape in a text box of workflow settings blurs it first, then closes", async ({
      page,
    }) => {
      await openStepFixture(page);
      const gear = settingsGear(page);
      await gear.click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      const version = dialog.getByRole("textbox", { name: "Version" });
      await version.focus();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeVisible();
      await expect(version).not.toBeFocused();
      expect(
        await page.evaluate(
          () => !!document.activeElement?.closest('[role="dialog"]'),
        ),
      ).toBe(true);
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(gear).toBeFocused();
    });

    test("a version that isn't a semantic version is flagged", async ({
      page,
    }) => {
      await openStepFixture(page);
      await settingsGear(page).click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      const version = dialog.getByRole("textbox", { name: "Version" });
      await version.fill("abc");
      await version.press("Tab");
      const field = dialog.locator('[data-param="version"]');
      await expect(field).toContainText(
        "Use a semantic version such as 1.0.0.",
      );
      await expect(version).toHaveAttribute("aria-invalid", "true");
      await version.fill("1.2.0");
      await expect(field).not.toContainText("Use a semantic version");
    });

    test("workflow settings say why they are read-only", async ({ page }) => {
      await openStepFixture(page);
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      const source = page.getByRole("textbox", { name: "Workflow source" });
      await source.fill(`${await source.inputValue()}\n  broken: [`);
      await page
        .getByRole("button", { name: "Apply changes", exact: true })
        .click();
      await settingsGear(page).click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      await expect(dialog).toContainText(
        "Fix the source to edit workflow settings.",
      );
      await expect(dialog).not.toContainText("edit this step");
    });
  });

// The gear settles what the editor holds that isn't applied, as opening
// step details does: an inspector edit that can't apply keeps it closed,
// and text typed in Source applies first.
test.describe("the gear and unapplied edits", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("an inspector edit that can't apply keeps workflow settings closed", async ({
    page,
  }) => {
    await openStepFixture(page);
    const version = page
      .locator(".inspector")
      .getByRole("textbox", { name: "Version", exact: true })
      .first();
    await version.fill("");
    await settingsGear(page).click();
    await expect(
      page.getByText(
        "Fix the invalid fields before continuing. Your edits are still here.",
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("dialog", { name: "Workflow settings" }),
    ).toHaveCount(0);
    await expect(version).toHaveValue("");
  });

  for (const size of sizes)
    test(`text typed in Source applies before workflow settings open at ${size.tag}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width: size.width, height: size.height });
      await openStepFixture(page);
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      const source = page.getByRole("textbox", { name: "Workflow source" });
      await source.fill(
        (await source.inputValue()).replace(
          "id: check-customer",
          "id: check-client",
        ),
      );
      await settingsGear(page).click();
      const dialog = page.getByRole("dialog", { name: "Workflow settings" });
      await dialog.getByRole("textbox", { name: "Version" }).fill("1.0.1");
      await dialog.getByRole("button", { name: "Close", exact: true }).click();
      await expect(dialog).toHaveCount(0);
      const text = await source.inputValue();
      expect(text).toContain("id: check-client");
      expect(parse(text).metadata.version).toBe("1.0.1");
    });

  for (const size of sizes)
    for (const view of ["Designer", "Outline"] as const)
      for (const parses of [true, false])
        test(`from ${view}, text typed in Source ${parses ? "that parses" : "that doesn't parse"} is neither applied nor lost at ${size.tag}`, async ({
          page,
        }) => {
          await page.setViewportSize({
            width: size.width,
            height: size.height,
          });
          await openStepFixture(page);
          await page.getByRole("tab", { name: "Source", exact: true }).click();
          const source = page.getByRole("textbox", { name: "Workflow source" });
          const original = await source.inputValue();
          const typed = parses
            ? original.replace("id: check-customer", "id: check-client")
            : `${original}\n  broken: [`;
          await source.fill(typed);
          await page.getByRole("tab", { name: view, exact: true }).click();
          await settingsGear(page).click();
          // The reason shows where the person is, and nothing opens.
          await expect(
            page.getByText("Apply or undo the changes typed in Source first.", {
              exact: true,
            }),
          ).toBeVisible();
          await expect(
            page.getByRole("dialog", { name: "Workflow settings" }),
          ).toHaveCount(0);
          // Nothing was applied: the step keeps its name and the editor
          // isn't read-only.
          if (view === "Outline")
            await expect(
              page.locator('[data-outline="check-customer"]'),
            ).toHaveCount(1);
          else
            await expect(
              page.locator(
                '[data-step="check-customer"], .tile-node[data-tile="check-customer"]',
              ),
            ).not.toHaveCount(0);
          await page.getByRole("tab", { name: "Source", exact: true }).click();
          await expect(source).toHaveValue(typed);
          await expect(page.locator(".source-error")).toHaveCount(0);
        });

  test("source text that doesn't apply keeps workflow settings closed and stays", async ({
    page,
  }) => {
    await openStepFixture(page);
    await page.getByRole("tab", { name: "Source", exact: true }).click();
    const source = page.getByRole("textbox", { name: "Workflow source" });
    const typed = `${await source.inputValue()}\n  broken: [`;
    await source.fill(typed);
    await settingsGear(page).click();
    // The text is applied as Apply changes would: its problem shows there.
    await expect(page.locator(".source-error")).toBeVisible();
    await expect(
      page.getByRole("dialog", { name: "Workflow settings" }),
    ).toHaveCount(0);
    await expect(source).toHaveValue(typed);
  });
});

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
        const gear = settingsGear(page);
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

      test("a long slot name breaks between its parts, keeps its separator with the connector, and stays in its row", async ({
        page,
      }) => {
        await openStepFixture(page, withSpareSlots(true));
        await settingsGear(page).click();
        const dialog = page.getByRole("dialog", { name: "Workflow settings" });
        const rows = dialog.locator(".wf-row");
        await expect(rows).toHaveCount(5);
        const long = rows.filter({
          hasText: "customer-orders-archive-in-the-regional-data-warehouse",
        });
        await long.scrollIntoViewIfNeeded();
        const list = await box(long.locator("xpath=.."));
        const menu = await box(long.locator(".row-menu-toggle"));
        const text = await box(long.locator(".wf-text"));
        expect(intersects(menu, text)).toBe(false);
        expectAligned([menu.middle, text.middle], "long row center");
        expect(text.x).toBeGreaterThanOrEqual(list.x);
        expect(menu.right).toBeLessThanOrEqual(list.right);
        // Every part fits its line: nothing runs past the text column.
        const runs = await long.locator(".wf-title").evaluate((title) => {
          const column = title.getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(title);
          return [...range.getClientRects()].map((r) => ({
            left: r.left,
            right: r.right,
            column: column.right,
          }));
        });
        for (const run of runs)
          expect(run.right).toBeLessThanOrEqual(run.column + 1);
        // The separator shares its line with the connector's first letter.
        const lines = await long.locator(".wf-title").evaluate((title) => {
          const walker = document.createTreeWalker(title, NodeFilter.SHOW_TEXT);
          const chars: { node: Text; at: number }[] = [];
          for (let node = walker.nextNode(); node; node = walker.nextNode())
            for (let at = 0; at < (node.textContent ?? "").length; at++)
              chars.push({ node: node as Text, at });
          const top = ({ node, at }: { node: Text; at: number }) => {
            const range = document.createRange();
            range.setStart(node, at);
            range.setEnd(node, at + 1);
            return range.getClientRects()[0]?.top ?? NaN;
          };
          const dot = chars.findIndex(
            ({ node, at }) => node.textContent![at] === "·",
          );
          const next = chars.findIndex(
            ({ node, at }, index) =>
              index > dot && /\S/.test(node.textContent![at]),
          );
          return { dot: top(chars[dot]), next: top(chars[next]) };
        });
        expectAligned([lines.dot, lines.next], "separator line", 2);
        expect(await clippedText(dialog, "")).toEqual([]);
        await noSidewaysScroll(page, dialog);
      });

      test("workflow settings take every key on the way, with names, rings and 24 px targets", async ({
        page,
      }) => {
        await openStepFixture(page);
        await settingsGear(page).click();
        const dialog = page.getByRole("dialog", { name: "Workflow settings" });
        expect(await walkFocus(page, dialog)).toBeGreaterThanOrEqual(6);
      });
    });
});
