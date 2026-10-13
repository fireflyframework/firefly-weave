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
// The Settings tab of step details: Retry on fail, Timeout, On error, Notes
// and About this step, the tab's changed dot and a note on the canvas.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  addCommonStep,
  openConnectedFixture,
  openStepFixture,
  sizes,
  type StepDetailsPage,
} from "./step-details-po";
import {
  box,
  boxes,
  clippedText,
  density,
  expectAligned,
  intersects,
  textBox,
  type Box,
} from "./step-geometry";
import { httpWorkflow, openHttpStep } from "./step-http";
import { hasPython, stopLocalAuthoring } from "./local-authoring";
import { sourceText, tokenColor } from "./support";

/** The sections of the visible Settings tab, in order. */
const sections = (details: StepDetailsPage) =>
  details.dialog.locator(
    ".sd-tabpanel:not([hidden]) .settings-form > [data-param]",
  );
/** Waits for the tab to show `ids.length` sections, then checks their order. */
async function expectSections(details: StepDetailsPage, ids: string[]) {
  await expect(sections(details)).toHaveCount(ids.length);
  expect(
    await sections(details).evaluateAll((items) =>
      items.map((item) => item.getAttribute("data-param")),
    ),
  ).toEqual(ids);
}

/** Reopens the workflow kept on this computer from Home after a reload. */
async function reopenFromHome(page: Page, name: string) {
  const recent = page.locator(".preview-row", { hasText: name }).first();
  const editor = page.locator(".editor-bar");
  await expect(recent.or(editor).first()).toBeVisible();
  if (!(await editor.isVisible())) await recent.click();
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  const canvas = page.getByLabel("Workflow canvas", { exact: true });
  await expect(canvas.or(show).first()).toBeVisible();
  if (await show.isVisible()) await show.click();
  await expect(canvas).toBeVisible();
}

/** Every stored draft on this computer, as one text. */
const drafts = (page: Page) =>
  page.evaluate(() =>
    Object.keys(localStorage)
      .filter((key) => key.startsWith("weave.localDraft."))
      .map((key) => localStorage.getItem(key) ?? "")
      .join(),
  );
/** The retry the workflow-owned action keeps in the draft on this computer. */
const storedRetry = async (page: Page) =>
  /"retry":(\{[^}]*\})/.exec(await drafts(page))?.[1] ?? "";
/** The note get-customer-orders keeps in the draft on this computer. */
const storedNote = async (page: Page) =>
  /"stepNotes":\{"get-customer-orders":\{"text":"([^"]*)"/.exec(
    await drafts(page),
  )?.[1] ?? "";
/** An element's background color, as the browser computes it. */
const backgroundOf = (locator: Locator) =>
  locator.evaluate((element) => getComputedStyle(element).backgroundColor);

for (const size of sizes)
  test.describe(`settings at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Settings shows Retry on fail, Timeout, On error, Notes and About this step in order", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await addCommonStep(page, "Call an API");
      await expect(details.dialogFor("call-api")).toBeVisible();
      await details.tab("Settings").click();
      await expectSections(details, [
        "retry",
        "timeout",
        "onError",
        "notes",
        "about",
      ]);
      const onError = details.dialog.getByRole("radiogroup", {
        name: "On error",
      });
      const stop = onError.getByRole("radio", { name: "Stop the run" });
      await expect(stop).toBeChecked();
      await expect(stop).toBeDisabled();
      await expect(details.field("onError")).toContainText(
        "Update the platform to continue after errors.",
      );
      const about = details.field("about");
      await expect(
        about.getByRole("heading", { name: "About this step" }),
      ).toBeVisible();
      await expect(about).toContainText("call-api");
      await expect(about).toContainText(/Main sequence · step 1 of \d+/);
      await expect(about).toContainText("weave-http@2.0.0 · GET");
      await expect(about).toContainText("Reads data · safe to retry");
      await expect(
        about.getByRole("button", { name: "Copy step ID" }),
      ).toBeVisible();
      await details.close();

      // Decision and parallel steps always stop the run when they fail.
      await details.openWithKeyboard("fan-out");
      await details.tab("Settings").click();
      await expectSections(details, ["onError", "notes", "about"]);
      await expect(details.field("onError")).toContainText(
        "The run fails with this step's error. You can retry it from Operate.",
      );
      await expect(details.field("onError")).not.toContainText(
        "Update the platform",
      );
      await details.close();

      // A wait has no On error.
      await details.openWithKeyboard("wait-a-minute");
      await details.tab("Settings").click();
      await expectSections(details, ["notes", "about"]);
      await expect(details.field("onError")).toHaveCount(0);
      await details.close();

      // The trigger has a note and its place, but no step ID.
      await details.trigger().focus();
      await page.keyboard.press("Enter");
      await expect(details.dialogFor("Manual form trigger")).toBeVisible();
      await details.tab("Settings").click();
      await expectSections(details, ["notes", "about"]);
      await expect(details.field("about")).toContainText("Starts the workflow");
      await expect(details.field("about")).not.toContainText("Step ID");
    });

    test("retry writes the owned action, and a POST shows it disabled", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await addCommonStep(page, "Call an API");
      await expect(details.dialogFor("call-api")).toBeVisible();
      await details.tab("Settings").click();
      const retry = details
        .field("retry")
        .getByRole("switch", { name: "Retry on fail" });
      await expect(retry).toHaveAttribute("aria-checked", "false");
      await expect(retry).toHaveAccessibleDescription(
        /Tries again after temporary failures/,
      );
      await expect(details.field("retry.maxAttempts")).toHaveCount(0);
      await expect(details.tab("Settings")).toHaveAccessibleName("Settings");
      await retry.click();
      await expect(retry).toHaveAttribute("aria-checked", "true");
      await expect(
        details
          .field("retry.maxAttempts")
          .getByRole("textbox", { name: "Max tries" }),
      ).toHaveValue("3");
      await expect(
        details
          .field("retry.initialDelaySeconds")
          .getByRole("textbox", { name: "Wait between tries" }),
      ).toHaveValue("1");
      const longest = details
        .field("retry.maxDelaySeconds")
        .getByRole("textbox", { name: "Longest wait" });
      await expect(longest).toHaveValue("30");
      await expect(details.field("retry")).toContainText(
        "Waits double after each try, up to the longest wait.",
      );
      await expect(details.field("retry")).toContainText(
        "Saved in the API action order-intake.call-api@1.0.0, which only this step uses.",
      );
      await expect(details.tab("Settings")).toHaveAccessibleName(
        "Settings, changed",
      );
      // The workflow-owned action keeps it (the draft on this computer holds its recipe).
      await expect
        .poll(() => drafts(page))
        .toContain(
          '"retry":{"maxAttempts":3,"initialDelaySeconds":1,"maxDelaySeconds":30}',
        );

      // The longest wait can't be shorter than the first.
      const first = details
        .field("retry.initialDelaySeconds")
        .getByRole("textbox", { name: "Wait between tries" });
      await first.fill("40");
      await expect(details.field("retry").getByRole("alert")).toHaveText(
        "The longest wait must be at least the wait between tries.",
      );
      await first.fill("1");
      await expect(details.field("retry").getByRole("alert")).toHaveCount(0);

      await details.close();
      await details.openWithKeyboard("call-api");
      await details.tab("Settings").click();
      await expect(retry).toHaveAttribute("aria-checked", "true");
      await retry.click();
      await expect(retry).toHaveAttribute("aria-checked", "false");
      await expect(details.field("retry.maxAttempts")).toHaveCount(0);
      // Off is the default again: the dot goes.
      await expect(details.tab("Settings")).toHaveAccessibleName("Settings");
      // One Undo turns it back on.
      await page.keyboard.press("ControlOrMeta+z");
      await expect(retry).toHaveAttribute("aria-checked", "true");
      await retry.click();
      await expect(retry).toHaveAttribute("aria-checked", "false");

      await details.tab("Parameters").click();
      await details
        .field("method")
        .getByRole("combobox", { name: "Method" })
        .click();
      await page.getByRole("option", { name: /^POST/ }).click();
      await details.tab("Settings").click();
      await expect(retry).toBeDisabled();
      await expect(retry).toHaveAttribute("aria-checked", "false");
      await expect(details.field("retry")).toContainText(
        "Changes data, so Weave never retries it automatically.",
      );
      await expect(details.field("about")).toContainText(
        "Changes data · never retried automatically",
      );
      // Its timeout is still the action's own to set.
      await expect(
        details.field("timeout").getByRole("textbox", { name: "Timeout" }),
      ).toBeEditable();
    });

    test("a note shows the Settings changed dot and its first line on the canvas", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("lookup");
      await expect(details.tab("Settings")).toHaveAccessibleName("Settings");
      await details.tab("Settings").click();
      const notes = details
        .field("notes")
        .getByRole("textbox", { name: "Notes" });
      const show = details
        .field("notes")
        .getByRole("switch", { name: "Show on canvas" });
      await expect(show).toBeDisabled();
      await expect(details.field("notes")).toContainText(
        "Notes don't change how the workflow runs.",
      );
      await notes.fill("Check the ledger first\nThen the cache");
      await expect(show).toBeEnabled();
      await show.click();
      await expect(show).toHaveAttribute("aria-checked", "true");
      await expect(details.tab("Settings")).toHaveAccessibleName(
        "Settings, changed",
      );
      const dot = details.tab("Settings").locator(".sd-dot");
      await expect(dot).toBeVisible();
      expect(
        await dot.evaluate((element) => {
          const probe = document.createElement("span");
          probe.style.color = "var(--info-ink)";
          document.body.append(probe);
          const token = getComputedStyle(probe).color;
          probe.remove();
          return getComputedStyle(element).backgroundColor === token;
        }),
      ).toBe(true);
      await details.close();
      const line = page.locator('.tile-node[data-tile="lookup"] .tile-note');
      await expect(line).toHaveText("Check the ledger first");
      await expect(line).toHaveAttribute("title", "Check the ledger first");
      // The label stays three lines high: the name keeps to one line.
      expect(
        await page
          .locator('.tile-node[data-tile="lookup"] .tile-label strong')
          .evaluate((element) => getComputedStyle(element).webkitLineClamp),
      ).toBe("1");

      // Undo takes back Show on canvas, then the text, one step each.
      await details.node("lookup").focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(line).toHaveCount(0);
      await details.openWithKeyboard("lookup");
      await details.tab("Settings").click();
      await expect(notes).toHaveValue("Check the ledger first\nThen the cache");
      await expect(show).toHaveAttribute("aria-checked", "false");
      await page.keyboard.press("ControlOrMeta+z");
      await expect(notes).toHaveValue("");
      await expect(details.tab("Settings")).toHaveAccessibleName("Settings");
    });

    test("the trigger's note shows on its tile", async ({ page }) => {
      const details = await openStepFixture(page);
      await details.trigger().focus();
      await page.keyboard.press("Enter");
      await expect(details.dialogFor("Manual form trigger")).toBeVisible();
      await details.tab("Settings").click();
      await details
        .field("notes")
        .getByRole("textbox", { name: "Notes" })
        .fill("Sent by the front desk\nTwice a day");
      await details
        .field("notes")
        .getByRole("switch", { name: "Show on canvas" })
        .click();
      await details.close();
      await expect(
        page.locator('.tile-node[data-tile^="$trigger"] .tile-note'),
      ).toHaveText("Sent by the front desk");
    });

    test("a note survives closing and reopening the workflow", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("lookup");
      await details.tab("Settings").click();
      await details
        .field("notes")
        .getByRole("textbox", { name: "Notes" })
        .fill("Check the ledger first");
      await details.close();
      // The draft on this computer carries the canvas sidecar (autosave waits 800 ms).
      await expect.poll(() => drafts(page)).toContain("Check the ledger first");
      await page.reload();
      await reopenFromHome(page, "order-intake");
      await details.openWithKeyboard("lookup");
      await details.tab("Settings").click();
      await expect(
        details.field("notes").getByRole("textbox", { name: "Notes" }),
      ).toHaveValue("Check the ledger first");
    });

    test("an emptied duration shows its saved value in its own unit once left", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("wait-for-payment");
      await details.tab("Settings").click();
      const most = details
        .field("timeout")
        .getByRole("textbox", { name: "Wait at most" });
      const mostUnit = details
        .field("timeout")
        .getByRole("combobox", { name: "Wait at most unit" });
      await expect(most).toHaveValue("1");
      await expect(mostUnit).toHaveValue("hours");
      await most.focus();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.press("Backspace");
      await expect(most).toHaveValue("");
      // The default it stands for, in the unit beside it.
      await expect(most).toHaveAttribute("placeholder", "1");
      await page.keyboard.press("Tab");
      await expect(most).toHaveValue("1");
      await expect(mostUnit).toHaveValue("hours");
      await details.close();

      await details.openWithKeyboard("wait-a-minute");
      const wait = details
        .field("duration")
        .getByRole("textbox", { name: "Wait for" });
      const waitUnit = details
        .field("duration")
        .getByRole("combobox", { name: "Wait for unit" });
      await expect(wait).toHaveValue("1");
      await expect(waitUnit).toHaveValue("minutes");
      await wait.focus();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.press("Backspace");
      await expect(wait).toHaveAttribute("placeholder", "1");
      await page.keyboard.press("Tab");
      await expect(wait).toHaveValue("1");
      await expect(waitUnit).toHaveValue("minutes");
      await details.close();
      const workflow = await sourceText(page);
      expect(workflow).toContain("durationSeconds: 60");
      expect(workflow).toContain("timeoutSeconds: 3600");
    });

    test("a signal's timeout and a human task's deadlines are on Settings", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("wait-for-payment");
      await expect(details.field("timeout")).toHaveCount(0);
      await details.tab("Settings").click();
      await expectSections(details, ["timeout", "onError", "notes", "about"]);
      await expect(details.field("timeout").locator(".param-label")).toHaveText(
        "Wait at most",
      );
      await expect(
        details.field("timeout").getByRole("textbox", { name: "Wait at most" }),
      ).toHaveValue("1");
      await details.close();
      await details.openWithKeyboard("notify-sales");
      await details.tab("Settings").click();
      await expectSections(details, [
        "due",
        "expiry",
        "onError",
        "notes",
        "about",
      ]);
      await expect(details.field("due").locator(".param-label")).toHaveText(
        "Due after",
      );
      await expect(details.field("expiry").locator(".param-label")).toHaveText(
        "Expires after",
      );
    });

    test("a published action's retry and timeout are read-only, with its document a click away", async ({
      page,
    }) => {
      const details = await openConnectedFixture(page);
      await details.openWithKeyboard("lookup");
      await details.tab("Settings").click();
      const retry = details
        .field("retry")
        .getByRole("switch", { name: "Retry on fail" });
      await expect(retry).toHaveAttribute("aria-checked", "true");
      await expect(retry).toBeDisabled();
      // A switch that can't change keeps its position, without the accent.
      expect(await backgroundOf(retry.locator(".param-switch-track"))).toBe(
        await tokenColor(page, "--disabled-bg"),
      );
      await expect(details.field("retry")).toContainText(
        "Set by sql.lookup@1.0.0.",
      );
      await expect(
        details
          .field("retry.maxAttempts")
          .getByRole("textbox", { name: "Max tries" }),
      ).toHaveValue("3");
      await expect(
        details
          .field("retry.maxAttempts")
          .getByRole("textbox", { name: "Max tries" }),
      ).not.toBeEditable();
      await expect(
        details.field("timeout").getByRole("textbox", { name: "Timeout" }),
      ).not.toBeEditable();
      // Settings set by the action are not the step's changes: no dot.
      await expect(details.tab("Settings")).toHaveAccessibleName("Settings");
      await expect(details.field("about")).toContainText(
        "weave-postgresql@1.0.0 · read",
      );
      const open = details
        .field("retry")
        .getByRole("button", { name: "Open action" });
      await open.click();
      const viewer = page.getByRole("dialog", {
        name: "View sql.lookup@1.0.0",
      });
      await expect(viewer).toBeVisible();
      await expect(viewer).toContainText("maxAttempts: 3");
      await page.keyboard.press("Escape");
      await expect(viewer).toHaveCount(0);
      await expect(open).toBeFocused();
      await expect(details.dialog).toBeVisible();
    });

    test("Copy step ID copies it and says so", async ({ page, context }) => {
      await context.grantPermissions(["clipboard-read", "clipboard-write"]);
      const details = await openStepFixture(page);
      await details.openWithKeyboard("lookup");
      await details.tab("Settings").click();
      await details
        .field("about")
        .getByRole("button", { name: "Copy step ID" })
        .click();
      await expect
        .poll(() => page.evaluate(() => navigator.clipboard.readText()))
        .toBe("lookup");
      await expect(details.dialog.getByText("Copied lookup.")).toBeAttached();
    });
  });

// ------------------------------------------------- an owned API action

/** get-customer-orders and a second step that calls the same action. */
const sharedAction = httpWorkflow.replace(
  "  output: {literal: {}}",
  `    - id: get-recent-orders
      kind: action
      uses: order-intake.get-customer-orders@1.0.0
      connection: crm
      with:
        object:
          path:
            object:
              customerId: {ref: /input/customerId}
  output: {literal: {}}`,
);

test.describe("an owned API action's settings", () => {
  test.skip(!hasPython, "Needs the repository's Python environment.");
  test.afterAll(() => stopLocalAuthoring());

  for (const size of sizes)
    test.describe(`at ${size.tag}`, () => {
      test.use({ viewport: { width: size.width, height: size.height } });

      test("Max tries is edited without turning retry off or losing focus", async ({
        page,
      }) => {
        const details = await openHttpStep(page, undefined, undefined, {
          recipe: {
            retry: {
              maxAttempts: 4,
              initialDelaySeconds: 2,
              maxDelaySeconds: 30,
            },
          },
        });
        await details.tab("Settings").click();
        const retry = details
          .field("retry")
          .getByRole("switch", { name: "Retry on fail" });
        const field = details.field("retry.maxAttempts");
        const max = field.getByRole("textbox", { name: "Max tries" });
        const stillOn = async () => {
          await expect(retry).toHaveAttribute("aria-checked", "true");
          await expect(
            details
              .field("retry.initialDelaySeconds")
              .getByRole("textbox", { name: "Wait between tries" }),
          ).toBeVisible();
        };

        // Clearing the box to retype it: the default stands in until then.
        await max.focus();
        await page.keyboard.press("ControlOrMeta+a");
        await page.keyboard.press("Backspace");
        await stillOn();
        await expect(max).toBeFocused();
        await expect(max).toHaveValue("");
        await expect
          .poll(() => storedRetry(page))
          .toBe(
            '{"maxAttempts":3,"initialDelaySeconds":2,"maxDelaySeconds":30}',
          );
        await page.keyboard.type("5");
        await stillOn();
        await expect(max).toBeFocused();
        await expect(max).toHaveValue("5");
        await expect
          .poll(() => storedRetry(page))
          .toBe(
            '{"maxAttempts":5,"initialDelaySeconds":2,"maxDelaySeconds":30}',
          );

        // Typing 10 over it: the 1 on the way is an entry to finish, not one try.
        await page.keyboard.press("ControlOrMeta+a");
        await page.keyboard.type("1");
        await expect(field).toContainText("Use a number from 2 to 10.");
        await expect(max).toHaveAttribute("aria-invalid", "true");
        await stillOn();
        await expect(max).toBeFocused();
        await expect(max).toHaveValue("1");
        // Nothing is saved for it (the draft is kept 800 ms after a change).
        await page.waitForTimeout(1000);
        expect(await storedRetry(page)).toBe(
          '{"maxAttempts":5,"initialDelaySeconds":2,"maxDelaySeconds":30}',
        );
        // Left unfinished, it stays an entry to finish, with its message.
        await page.keyboard.press("Tab");
        await expect(max).toHaveValue("1");
        await expect(max).toHaveAttribute("aria-invalid", "true");
        await expect(field).toContainText("Use a number from 2 to 10.");
        await max.focus();
        await page.keyboard.press("End");
        await page.keyboard.type("0");
        await stillOn();
        await expect(max).toBeFocused();
        await expect(max).toHaveValue("10");
        await expect(field).not.toContainText("Use a number from 2 to 10.");
        await expect
          .poll(() => storedRetry(page))
          .toBe(
            '{"maxAttempts":10,"initialDelaySeconds":2,"maxDelaySeconds":30}',
          );

        // Reset to default, from the field's menu.
        await field.hover();
        await field
          .getByRole("button", { name: "Field menu for Max tries" })
          .click();
        await page.getByRole("menuitem", { name: /Reset to default/ }).click();
        await stillOn();
        await expect(max).toHaveValue("3");
        await expect
          .poll(() => storedRetry(page))
          .toBe(
            '{"maxAttempts":3,"initialDelaySeconds":2,"maxDelaySeconds":30}',
          );
        expect(
          await field.evaluate(
            (element) =>
              element.contains(document.activeElement) &&
              document.activeElement !== document.body,
          ),
        ).toBe(true);
      });

      test("the tries and waits stay while focus is in them, and only the switch turns retry off", async ({
        page,
      }) => {
        const details = await openHttpStep(page);
        await details.tab("Settings").click();
        const retry = details
          .field("retry")
          .getByRole("switch", { name: "Retry on fail" });
        await retry.click();
        await expect(retry).toHaveAttribute("aria-checked", "true");
        const max = details
          .field("retry.maxAttempts")
          .getByRole("textbox", { name: "Max tries" });
        await max.focus();
        // Undo takes back the switch, but the fields don't vanish under focus.
        await page.keyboard.press("ControlOrMeta+z");
        await expect(retry).toHaveAttribute("aria-checked", "false");
        await expect(max).toBeFocused();
        await expect.poll(() => storedRetry(page)).toBe("");
        // Once focus leaves them, they follow the switch.
        await retry.focus();
        await expect(max).toHaveCount(0);
      });

      test("Timeout is retyped from an empty box", async ({ page }) => {
        const details = await openHttpStep(page, undefined, undefined, {
          recipe: { timeoutSeconds: 20 },
        });
        await details.tab("Settings").click();
        const timeout = details
          .field("timeout")
          .getByRole("textbox", { name: "Timeout" });
        await expect(timeout).toHaveValue("20");
        await timeout.focus();
        await page.keyboard.press("ControlOrMeta+a");
        await page.keyboard.press("Backspace");
        await expect(timeout).toHaveValue("");
        // Clearing writes the default, which waits behind the empty box.
        await expect.poll(() => drafts(page)).toContain('"timeoutSeconds":30');
        await expect(timeout).toHaveValue("");
        await page.keyboard.type("25");
        await expect(timeout).toHaveValue("25");
        await expect(timeout).toBeFocused();
        await expect.poll(() => drafts(page)).toContain('"timeoutSeconds":25');
        // Emptied and left, the box shows the default it saved.
        await page.keyboard.press("ControlOrMeta+a");
        await page.keyboard.press("Backspace");
        await page.keyboard.press("Tab");
        await expect(timeout).toHaveValue("30");
        await expect.poll(() => drafts(page)).toContain('"timeoutSeconds":30');
      });

      test("switching a GET with retry on to POST turns retry off and disables it", async ({
        page,
      }) => {
        const details = await openHttpStep(page, undefined, undefined, {
          recipe: {
            retry: {
              maxAttempts: 3,
              initialDelaySeconds: 2,
              maxDelaySeconds: 30,
            },
          },
        });
        await details.tab("Settings").click();
        const retry = details
          .field("retry")
          .getByRole("switch", { name: "Retry on fail" });
        await expect(retry).toHaveAttribute("aria-checked", "true");
        await details.tab("Parameters").click();
        await details
          .field("method")
          .getByRole("combobox", { name: "Method" })
          .click();
        await page.getByRole("option", { name: /^POST/ }).click();
        await details.tab("Settings").click();
        await expect(retry).toHaveAttribute("aria-checked", "false");
        await expect(retry).toBeDisabled();
        await expect(details.field("retry.maxAttempts")).toHaveCount(0);
        await expect(details.field("retry")).toContainText(
          "Changes data, so Weave never retries it automatically.",
        );
      });

      test("an action two steps call says so", async ({ page }) => {
        const details = await openHttpStep(page, undefined, sharedAction);
        await details.tab("Settings").click();
        await expect(details.field("retry")).toContainText(
          "Saved in the API action order-intake.get-customer-orders@1.0.0, used by 2 steps.",
        );
      });

      test("a note the canvas has no room for stays on screen as not saved", async ({
        page,
      }) => {
        // A response sample that leaves the canvas just under its 256 KB.
        const details = await openHttpStep(page, undefined, undefined, {
          recipe: { responseSample: "s".repeat(261_300) },
        });
        await details.tab("Settings").click();
        const notes = details
          .field("notes")
          .getByRole("textbox", { name: "Notes" });
        await notes.click();
        await page.keyboard.type("abc");
        await expect.poll(() => storedNote(page)).toBe("abc");
        await page.keyboard.insertText("x".repeat(1500));
        const problem = details.field("notes").getByRole("alert");
        await expect(problem).toContainText("Not saved.");
        await expect(notes).toHaveAttribute("aria-invalid", "true");
        await expect(notes).toHaveValue(`abc${"x".repeat(1500)}`);
        await notes.blur();
        await expect(problem).toContainText("Not saved.");
        expect(await storedNote(page)).toBe("abc");
        // A note that fits is saved, and the message goes.
        await notes.fill("abcd");
        await expect(problem).toHaveCount(0);
        await expect(notes).not.toHaveAttribute("aria-invalid", "true");
        await expect.poll(() => storedNote(page)).toBe("abcd");
      });

      test("retry still marks Settings changed while the source has an error", async ({
        page,
      }) => {
        const details = await openHttpStep(page, undefined, undefined, {
          recipe: {
            retry: {
              maxAttempts: 3,
              initialDelaySeconds: 2,
              maxDelaySeconds: 30,
            },
          },
        });
        await expect(details.tab("Settings")).toHaveAccessibleName(
          "Settings, changed",
        );
        await details.close();
        await page.getByRole("tab", { name: "Source", exact: true }).click();
        const source = page.getByRole("textbox", { name: "Workflow source" });
        await source.fill(`${await source.inputValue()}\n  broken: [`);
        await page
          .getByRole("button", { name: "Apply changes", exact: true })
          .click();
        await page.getByRole("tab", { name: "Designer", exact: true }).click();
        await details.openWithKeyboard("get-customer-orders");
        await details.tab("Settings").click();
        await expect(
          details.field("retry").getByRole("switch", { name: "Retry on fail" }),
        ).toBeDisabled();
        await expect(details.tab("Settings")).toHaveAccessibleName(
          "Settings, changed",
        );
      });
    });
});

// ------------------------------------------------------------- geometry

/** The visible boxes of what `selector` matches inside `scope`. */
const shownBoxes = (scope: Locator, selector: string) =>
  boxes(scope.locator(selector));

/** True when an element's text sits on one line. */
const oneLine = (locator: Locator) =>
  locator.evaluate((element) => {
    const style = getComputedStyle(element);
    const line = parseFloat(style.lineHeight) || 20;
    const range = document.createRange();
    range.selectNodeContents(element);
    const tops = new Set(
      [...range.getClientRects()]
        .filter((rect) => rect.width > 0)
        .map((rect) => Math.round(rect.top)),
    );
    return (
      tops.size <= 1 && element.getBoundingClientRect().height < line * 1.5 + 8
    );
  });

test.describe("settings layout", () => {
  test.skip(!hasPython, "Needs the repository's Python environment.");
  test.afterAll(() => stopLocalAuthoring());

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

      test("sections share one left edge and one right edge, rows line up, and nothing clips or overlaps", async ({
        page,
      }) => {
        const details = await openHttpStep(page, undefined, undefined, {
          recipe: {
            retry: {
              maxAttempts: 3,
              initialDelaySeconds: 2,
              maxDelaySeconds: 30,
            },
          },
          stepNotes: {
            "get-customer-orders": {
              text: "Calls the CRM sandbox in local. Ask Ana before pointing it at production.",
              showOnCanvas: true,
            },
          },
        });
        await details.tab("Settings").click();
        await expect(details.tab("Settings")).toHaveAccessibleName(
          "Settings, changed",
        );
        const d = await density(page);
        const form = details.dialog.locator(
          ".sd-tabpanel:not([hidden]) .settings-form",
        );
        await expect(sections(details)).toHaveCount(5);
        const edge = await box(form);
        const left = edge.x;
        const right = edge.right;

        // One left edge: every section, its heading and its first control.
        const starts = [
          ...(await boxes(sections(details))).map((b) => b.x),
          ...(
            await shownBoxes(
              form,
              ":scope > [data-param] > .param > :is(.param-label-row, .param-control) > :first-child",
            )
          ).map((b) => b.x),
          (await box(form.locator(".settings-retry-grid input").first())).x,
          (await box(form.locator('[data-param="timeout"] input').first())).x,
          (
            await box(
              form.locator('[data-param="onError"] input[type="radio"]'),
            )
          ).x,
          (await box(form.locator('[data-param="notes"] textarea'))).x,
          ...(await shownBoxes(form, '[data-param="about"] dt')).map(
            (b) => b.x,
          ),
        ];
        expectAligned([left, ...starts], "left edges");

        // One right edge for what ends there at rest.
        const retry = form.locator('[data-param="retry"]');
        const notes = form.locator('[data-param="notes"]');
        const ends = [
          ...(await boxes(sections(details))).map((b) => b.right),
          (await box(retry.getByRole("switch"))).right,
          (await box(form.locator(".settings-retry-grid input").last())).right,
          (await box(notes.getByRole("switch"))).right,
          (await box(notes.locator("textarea"))).right,
        ];
        expectAligned([right, ...ends], "right edges");

        // Rows: centers shared.
        const title = await box(retry.locator(".settings-switch-text"));
        const retrySwitch = await box(retry.getByRole("switch"));
        expectAligned(
          [title.middle, retrySwitch.middle],
          "retry title and switch",
        );
        const labels = await shownBoxes(
          form,
          ".settings-retry-grid .param-label",
        );
        const inputs = await shownBoxes(form, ".settings-retry-grid input");
        expect(inputs).toHaveLength(3);
        for (const input of inputs) expect(input.height).toBe(d.control);
        const columns = new Set(inputs.map((b) => Math.round(b.y))).size === 1;
        if (columns) {
          expectAligned(
            labels.map((b) => b.middle),
            "retry labels",
          );
          expectAligned(
            inputs.map((b) => b.width),
            "retry input widths",
          );
          expect(edge.width).toBeGreaterThanOrEqual(400);
        } else {
          // Stacked under 400 px of form: each field takes the whole width.
          expect(edge.width).toBeLessThan(560);
          for (const input of inputs) expect(input.right).toBeCloseTo(right, 0);
        }
        const notesLabel = await textBox(notes.locator("label.param-label"));
        const showLabel = await textBox(
          notes.locator(".settings-inline-label"),
        );
        const notesSwitch = await box(notes.getByRole("switch"));
        expectAligned(
          [notesLabel.middle, showLabel.middle, notesSwitch.middle],
          "Notes, Show on canvas and its switch",
        );
        const onError = form.locator('[data-param="onError"]');
        expectAligned(
          [
            (await box(onError.locator('input[type="radio"]'))).middle,
            (await textBox(onError.locator(".settings-choice-label"))).middle,
          ],
          "Stop the run and its radio",
        );
        const about = form.locator('[data-param="about"]');
        const terms = about.locator("dt");
        const values = about.locator("dd");
        await expect(terms).toHaveCount(4);
        // A term reads on the first line of its value.
        for (let index = 0; index < 4; index++)
          expectAligned(
            [
              (await textBox(terms.nth(index))).bottom,
              (await textBox(values.nth(index))).bottom,
            ],
            `About row ${index + 1}`,
            2,
          );
        const copy = await box(
          about.getByRole("button", { name: "Copy step ID" }),
        );
        expectAligned(
          [copy.middle, (await textBox(about.locator("dd code"))).middle],
          "Copy step ID and the ID",
        );
        expectAligned(
          (await shownBoxes(about, "dd")).map((b) => b.x),
          "About values",
        );

        // The timeout's hint sits beside its box while there is room.
        const timeout = form.locator('[data-param="timeout"]');
        const timeoutBox = await box(timeout.locator(".param-duration"));
        const timeoutHint = await textBox(timeout.locator(".param-line"));
        if (edge.width >= 480) {
          expect(timeoutHint.x).toBeGreaterThanOrEqual(timeoutBox.right + 8);
          expectAligned(
            [timeoutBox.middle, timeoutHint.middle],
            "Timeout box and its hint",
          );
        } else expect(timeoutHint.y).toBeGreaterThanOrEqual(timeoutBox.bottom);
        // The reference's rhythm where the density allows it.
        await expect(retry.locator(".param-unit-suffix").first()).toHaveCSS(
          "color",
          await tokenColor(page, "--subtle"),
        );
        if (!d.touch) {
          const description = await textBox(
            retry.locator(".settings-description"),
          );
          const firstLabel = await textBox(
            retry.locator(".settings-retry-grid .param-label").first(),
          );
          expect(firstLabel.y - description.bottom).toBeCloseTo(8, 0);
          expect((await box(notes.locator("textarea"))).height).toBe(60);
        }

        // Controls follow the density; switches and buttons stay usable.
        for (const control of await shownBoxes(
          timeout,
          ".param-control :is(input, select)",
        ))
          expect(control.height).toBe(d.control);
        for (const target of [retrySwitch, notesSwitch, copy]) {
          expect(target.height).toBeGreaterThanOrEqual(d.target);
          expect(target.width).toBeGreaterThanOrEqual(d.touch ? 44 : 24);
        }
        const dot = await box(details.tab("Settings").locator(".sd-dot"));
        expect([dot.width, dot.height]).toEqual([6, 6]);

        // Sections and their rows never overlap.
        const all = await boxes(sections(details));
        for (let i = 1; i < all.length; i++)
          expect(all[i].y).toBeGreaterThanOrEqual(all[i - 1].bottom);
        for (const section of await sections(details).all()) {
          const rows: Box[] = await section.evaluate((element) =>
            [...element.querySelectorAll<HTMLElement>(":scope > .param > *")]
              .filter((row) => row.getClientRects().length > 0)
              .map((row) => {
                const r = row.getBoundingClientRect();
                return {
                  x: r.x,
                  y: r.y,
                  width: r.width,
                  height: r.height,
                  right: r.right,
                  bottom: r.bottom,
                  middle: r.y + r.height / 2,
                };
              }),
          );
          for (let i = 1; i < rows.length; i++)
            for (let j = 0; j < i; j++)
              expect(
                intersects(rows[i], rows[j]),
                `rows ${j} and ${i} of ${await section.getAttribute("data-param")}`,
              ).toBe(false);
        }

        // Words stay whole and on their line.
        expect(await clippedText(details.dialog)).toEqual([]);
        for (const single of [
          ...(await form.locator(":scope > [data-param] .param-label").all()),
          notes.locator(".settings-inline-label"),
          onError.locator(".settings-choice-label"),
          ...(await about.locator("dt").all()),
        ])
          expect(
            await oneLine(single),
            `${await single.textContent()} on one line`,
          ).toBe(true);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
        ).toBe(true);
        expect(
          await form.evaluate(
            (element) => element.scrollWidth <= element.clientWidth + 1,
          ),
        ).toBe(true);
        await page.screenshot({
          path: test.info().outputPath(`settings-${size.tag}.png`),
          fullPage: false,
        });
      });
    });
});
