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
// The Platform event trigger's Parameters: which events start the workflow,
// which workflows they may come from, and the input each run starts with.
// The platform is mocked; the event's fields and the local checks are the
// repository's own Python.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  ATTENTION,
  declared,
  eventTile,
  FORM,
  openEventTrigger,
  openNotifyTeam,
  REFERENCE,
  spec,
} from "./event-trigger-po";
import { command, expectHitTarget } from "./support";

const LABELS = [
  "A transaction needs attention",
  "A transaction was undone",
  "A transaction was canceled",
  "A transaction completed",
  "A transaction was reconciled",
  "A transaction was force ended",
];
const events = (dialog: Locator) =>
  dialog.getByRole("group", { name: "Which events" });
const from = (dialog: Locator) =>
  dialog.getByRole("radiogroup", { name: "From which workflows" });
const any = (dialog: Locator) =>
  dialog.getByRole("radio", { name: "Any workflow in this project" });
const only = (dialog: Locator) =>
  dialog.getByRole("radio", { name: "Only these workflows" });
const chips = (dialog: Locator) =>
  dialog
    .getByRole("list", { name: "Workflows this one reacts to" })
    .locator("[data-source]");
const field = (dialog: Locator) =>
  dialog.getByRole("combobox", { name: "Add a workflow" });
const inputSection = (dialog: Locator) =>
  dialog.getByRole("region", { name: "Input", exact: true });
const suggestions = (page: Page) =>
  page.getByRole("listbox", { name: "Workflow suggestions" });
const badge = (page: Page) => eventTile(page).locator(".tile-badge");

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`the Platform event trigger's Parameters at ${size.width}x${size.height}`, () => {
    test.use({ viewport: size });

    test("shows the six events in the order people choose, and each change is one undo step", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page);
      const dialog = await openEventTrigger(page, details);
      const boxes = events(dialog).getByRole("checkbox");
      await expect(boxes).toHaveCount(6);
      for (const [index, label] of LABELS.entries())
        await expect(boxes.nth(index)).toHaveAccessibleName(label);
      await expect(boxes.nth(0)).toHaveAccessibleDescription(
        "Its outcome is unclear, and a person has to resolve it.",
      );
      await expect(boxes.nth(0)).toBeChecked();
      await expect(boxes.nth(1)).not.toBeChecked();
      await expect(any(dialog)).toBeChecked();
      await expect(
        dialog.getByRole("button", { name: "Execute workflow" }),
      ).toBeVisible();
      await boxes.nth(1).check();
      await boxes.nth(3).check();
      await details.close();
      await expect(eventTile(page)).toContainText("3 events · any workflow");
      // In the platform's order, whatever order they were chosen in.
      expect(await declared(page)).toEqual({
        events: [
          "transaction.completed",
          "transaction.compensated",
          "transaction.in_doubt",
        ],
      });
      await command(page, "Undo");
      expect(await declared(page)).toEqual({
        events: ["transaction.compensated", "transaction.in_doubt"],
      });
      await command(page, "Undo");
      expect(await declared(page)).toEqual(ATTENTION);
    });

    test("asks for an event when none is chosen, under the list and on the tile", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page);
      const dialog = await openEventTrigger(page, details);
      await events(dialog).getByRole("checkbox").first().uncheck();
      await expect(
        dialog.getByText("Choose at least one event."),
      ).toBeVisible();
      await expect(events(dialog)).toHaveAccessibleDescription(
        "Choose at least one event.",
      );
      await details.close();
      await expect(eventTile(page)).toContainText("No events chosen");
      await expect(badge(page)).toHaveAttribute("data-badge", "setup");
      // The badge opens the trigger's step details, where the event is chosen.
      await eventTile(page)
        .getByRole("button", {
          name: "Show issues for Platform event: Setup needed: Choose at least one event.",
        })
        .click();
      await expect(details.dialogFor("Platform event trigger")).toBeVisible();
    });

    test("takes workflow names from the project's published workflows, then this computer's", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, { drafts: ["move-archive"] });
      const dialog = await openEventTrigger(page, details);
      await only(dialog).check();
      await expect(
        dialog.getByText(
          "Add at least one workflow, or choose Any workflow in this project.",
        ),
      ).toBeVisible();
      const input = field(dialog);
      await expect(input).toBeFocused();
      await input.fill("mo");
      const list = suggestions(page);
      await expect(list.getByRole("group")).toHaveCount(2);
      // The text being typed stays in view: beside the list, or above the sheet.
      await expect
        .poll(async () => {
          const box = (await input.boundingBox())!;
          const panel = (await page.locator(".et-list").boundingBox())!;
          return (
            box.y >= 0 &&
            box.y + box.height <= size.height &&
            (box.y + box.height <= panel.y + 1 ||
              box.y >= panel.y + panel.height - 1)
          );
        })
        .toBe(true);
      await expectHitTarget(input);
      await expect(
        list
          .getByRole("group", { name: "In this project" })
          .getByRole("option"),
      ).toHaveText([
        /monitor-queues\s*1\.0\.4/,
        /move-between-systems\s*1\.4\.0/,
        /move-files-nightly\s*2\.3\.0/,
      ]);
      await expect(
        list
          .getByRole("group", { name: "On this computer" })
          .getByRole("option"),
      ).toHaveText([/move-archive\s*Draft, not published/]);
      await input.press("ArrowDown");
      await input.press("ArrowDown");
      await expect(input).toHaveAttribute(
        "aria-activedescendant",
        (await list
          .getByRole("option", { name: /move-between-systems/ })
          .getAttribute("id"))!,
      );
      await input.press("Enter");
      await expect(chips(dialog)).toHaveText(["move-between-systems"]);
      // Chosen: it isn't listed again, and the text typed is added as it is.
      await input.fill("mo");
      await expect(
        list.getByRole("option", { name: /move-between-systems/ }),
      ).toHaveCount(0);
      await list.getByRole("option", { name: /move-archive/ }).click();
      await input.fill("sync-records");
      await input.press("Enter");
      await expect(chips(dialog)).toHaveText([
        "move-between-systems",
        "move-archive",
        "sync-records",
      ]);
      await input.fill("-sync");
      await input.press("Enter");
      await expect(dialog.getByRole("alert")).toContainText(
        "“-sync” isn't a workflow name.",
      );
      await input.fill("sync-records");
      await input.press("Enter");
      await expect(dialog.getByRole("alert")).toContainText(
        "sync-records is already here.",
      );
      await details.close();
      await expect(eventTile(page)).toContainText(
        "Needs attention · 3 workflows",
      );
      expect(await declared(page)).toEqual({
        ...ATTENTION,
        from: ["move-between-systems", "move-archive", "sync-records"],
      });
    });

    test("warns when the workflow reacts to its own transactions", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, {
        spec: { eventTrigger: { ...ATTENTION, from: ["sync-records"] } },
      });
      const dialog = await openEventTrigger(page, details);
      const warning = dialog.getByText(
        "notify-team reacts to its own transactions too. Reactions stop after 3 in a row.",
      );
      await expect(warning).toHaveCount(0);
      await field(dialog).fill("notify-team");
      await field(dialog).press("Enter");
      await expect(warning).toBeVisible();
      await dialog
        .getByRole("button", { name: "Remove notify-team", exact: true })
        .click();
      await expect(warning).toHaveCount(0);
    });

    test("Any workflow leaves the names out, and Only these workflows brings them back", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, {
        spec: { eventTrigger: { ...ATTENTION, from: ["sync-records"] } },
      });
      const dialog = await openEventTrigger(page, details);
      await any(dialog).check();
      await expect(chips(dialog)).toHaveCount(0);
      await only(dialog).check();
      await expect(chips(dialog)).toHaveText(["sync-records"]);
      await any(dialog).check();
      await details.close();
      expect(await declared(page)).toEqual(ATTENTION);
    });

    test("says the runs start with the platform event, with its fields", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page);
      const dialog = await openEventTrigger(page, details);
      const input = inputSection(dialog);
      await expect(input).toContainText("Runs start with the platform event.");
      const fields = input
        .getByRole("group", { name: "Platform event" })
        .getByRole("listitem");
      await expect(fields).toHaveText([
        /type\s*Choice\s*Which event happened/,
        /emitted_at\s*Date and time\s*When it happened/,
        /transaction\s*Group\s*The transaction's facts, business key and steps/,
        /event_id\s*Text\s*The event's ID/,
        /version\s*Fixed value\s*The version of these fields/,
      ]);
    });

    test("an input that can't take the event says so first, and Reset the input brings the event back", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, {
        spec: { inputSchema: FORM },
      });
      await expect(badge(page)).toHaveAttribute("data-badge", "error");
      const dialog = await openEventTrigger(page, details);
      const problem = dialog.getByRole("alert").filter({
        hasText:
          "This trigger can't start runs: the workflow's input doesn't accept the platform event.",
      });
      await expect(problem).toContainText("WV-COMP-TYPE_MISMATCH");
      await expect(problem).toContainText("Undo brings the 2 fields back.");
      const input = inputSection(dialog);
      await expect(input).toContainText(
        "Runs start with these 2 fields, not the platform event.",
      );
      await expect(input.getByRole("listitem")).toContainText([
        "reference",
        "message",
      ]);
      await problem.getByRole("button", { name: "Reset the input" }).click();
      await expect(problem).toHaveCount(0);
      await expect(events(dialog).getByRole("checkbox").first()).toBeFocused();
      await expect(input).toContainText("Runs start with the platform event.");
      await details.close();
      expect((await spec(page))["inputSchema"]).toEqual(REFERENCE);
      await expect(badge(page)).toHaveCount(0);
      await command(page, "Undo");
      expect((await spec(page))["inputSchema"]).toEqual(FORM);
    });
  });

test.describe("the Platform event trigger's Parameters", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("without catalog.read, suggests this computer's workflows and says why", async ({
    page,
  }) => {
    let asked = 0;
    const details = await openNotifyTeam(page, {
      spec: { eventTrigger: { ...ATTENTION, from: ["sync-records"] } },
      capabilities: ["workflow.edit"],
      drafts: ["move-archive"],
      catalog: async (route) => {
        asked++;
        await route.fulfill({ status: 403, json: {} });
      },
    });
    const dialog = await openEventTrigger(page, details);
    await field(dialog).fill("mo");
    await expect(page.locator(".et-list")).toContainText(
      "You can't read this project's published workflows here.",
    );
    await expect(suggestions(page).getByRole("option")).toHaveText([
      /move-archive/,
    ]);
    expect(asked).toBe(0);
  });

  test("a hundred workflows is the most", async ({ page }) => {
    const names = Array.from({ length: 100 }, (_, i) => `workflow-${i}`);
    const details = await openNotifyTeam(page, {
      spec: { eventTrigger: { ...ATTENTION, from: names } },
    });
    const dialog = await openEventTrigger(page, details);
    await expect(field(dialog)).toHaveCount(0);
    await expect(
      dialog.getByText("100 workflows, the most a trigger can list."),
    ).toBeVisible();
    await dialog
      .getByRole("button", { name: "Remove workflow-99", exact: true })
      .click();
    await expect(field(dialog)).toBeVisible();
  });

  test("says nothing of an input of its own until a check has read it", async ({
    page,
  }) => {
    let answer: () => void = () => undefined;
    const read = new Promise<void>((done) => (answer = done));
    const details = await openNotifyTeam(page, {
      spec: { inputSchema: FORM },
      check: () => read,
    });
    const dialog = await openEventTrigger(page, details);
    const input = inputSection(dialog);
    await expect(input).toContainText(
      "The next check says whether this workflow's input takes the platform event.",
    );
    await expect(input).not.toContainText("Runs start with");
    answer();
    await expect(input).toContainText(
      "Runs start with these 2 fields, not the platform event.",
    );
  });

  test("an input Studio can't compare gets a warning, not an error", async ({
    page,
  }) => {
    const details = await openNotifyTeam(page, {
      spec: { inputSchema: { not: { type: "string" } } },
    });
    await expect(badge(page)).toHaveAttribute("data-badge", "warning");
    const dialog = await openEventTrigger(page, details);
    const unsure = dialog.getByRole("note").filter({
      hasText:
        "Studio can't confirm that the platform event fits this workflow's input.",
    });
    await expect(unsure).toContainText(
      "Each start checks it, and a start that doesn't fit is recorded as refused.",
    );
    await unsure.getByRole("button", { name: "Reset the input" }).click();
    await expect(unsure).toHaveCount(0);
  });

  test("the keyboard reaches every control, in order", async ({ page }) => {
    const details = await openNotifyTeam(page, {
      spec: { eventTrigger: { ...ATTENTION, from: ["sync-records"] } },
    });
    const dialog = await openEventTrigger(page, details);
    const first = events(dialog).getByRole("checkbox").first();
    await first.focus();
    const reached: string[] = [];
    for (let i = 0; i < 9; i++) {
      reached.push(
        await page.evaluate(() => {
          const active = document.activeElement as HTMLElement;
          return (
            active.getAttribute("aria-label") ??
            (active as HTMLInputElement).value ??
            ""
          );
        }),
      );
      await page.keyboard.press("Tab");
    }
    expect(reached).toEqual([
      "transaction.in_doubt",
      "transaction.compensated",
      "transaction.cancelled",
      "transaction.completed",
      "transaction.reconciled",
      "transaction.force_ended",
      "only",
      "Remove sync-records",
      "Add a workflow",
    ]);
    // Space toggles an event; the arrows choose between the workflows' options.
    await first.focus();
    await page.keyboard.press("Space");
    await expect(first).not.toBeChecked();
    await page.keyboard.press("Space");
    await only(dialog).focus();
    await page.keyboard.press("ArrowUp");
    await expect(any(dialog)).toBeChecked();
    await page.keyboard.press("ArrowDown");
    await expect(chips(dialog)).toHaveText(["sync-records"]);
    await expect(from(dialog)).toBeVisible();
    // Escape closes the suggestions and keeps the text; Backspace in an empty field goes to the last name.
    await field(dialog).fill("mo");
    await expect(suggestions(page)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(suggestions(page)).toBeHidden();
    await expect(field(dialog)).toBeFocused();
    await expect(field(dialog)).toHaveValue("mo");
    await expect(dialog).toBeVisible();
    await field(dialog).fill("");
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    await field(dialog).focus();
    await page.keyboard.press("Backspace");
    await expect(
      dialog.getByRole("button", { name: "Remove sync-records", exact: true }),
    ).toBeFocused();
  });
});
