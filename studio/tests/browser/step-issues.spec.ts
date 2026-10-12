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
// Issues in step details: messages at their fields, with their timing; the
// issues pill in the header and Fix; entries that don't apply yet, kept
// through closing, moving between steps and switching tabs; and how it all
// lines up.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { parse, stringify } from "yaml";
import {
  addCommonStep,
  openConnectedFixture,
  openStepFixture,
  sizes,
  stepFixture,
  withLanguageFeatures,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";
import {
  box,
  boxes,
  clippedText,
  density,
  expectAligned,
  intersects,
  showParameters,
  textBox,
} from "./step-geometry";
import { httpWorkflow, openHttpStep } from "./step-http";
import { hasPython, stopLocalAuthoring } from "./local-authoring";
import { sourceText, tokenColor } from "./support";

/** A diagnostic as live validation reports it. */
interface Reported {
  code: string;
  severity: "error" | "warning";
  message: string;
  path: string;
}
/** Makes validation report `diagnostics`, then runs Validate from the editor bar. */
async function validateWith(page: Page, diagnostics: Reported[]) {
  await page.route("**/studio/local/validate", (route) =>
    route.fulfill({
      json: {
        validationOk: !diagnostics.some((d) => d.severity === "error"),
        errorCount: diagnostics.filter((d) => d.severity === "error").length,
        diagnostics,
        partial: true,
      },
    }),
  );
  const validate = page.getByRole("button", { name: "Validate", exact: true });
  if (await validate.isVisible()) await validate.click();
  else {
    await page.getByRole("button", { name: "More workflow commands" }).click();
    await page.getByRole("menuitem", { name: "Validate", exact: true }).click();
  }
  const dismiss = page.getByRole("button", { name: "Dismiss notification" });
  await expect(dismiss.first()).toBeVisible();
  await dismiss.first().click();
}
/** The pill in the header, by its count. */
const issuesPill = (details: StepDetailsPage) =>
  details.dialog.getByRole("button", { name: /^\d+ issues?$/ });
/** The entries of the issues list, as read aloud ("" when the step has none). */
async function issueEntries(
  page: Page,
  details: StepDetailsPage,
): Promise<string[]> {
  const pill = issuesPill(details);
  if (!(await pill.count())) return [];
  await pill.click();
  const items = page.getByRole("menu").getByRole("menuitem");
  await expect(items.first()).toBeFocused();
  const texts = (await items.allInnerTexts()).map((text) =>
    text.replace(/\s+/g, " ").trim(),
  );
  await page.keyboard.press("Escape");
  await expect(pill).toBeFocused();
  return texts;
}
/** Opens the step's Home and the workflow file chooser there. */
async function goHome(page: Page) {
  const home = page.getByRole("button", { name: "Home", exact: true });
  if (!(await home.isVisible()))
    await page
      .getByRole("button", { name: /^(Expand|Open) navigation$/ })
      .first()
      .click();
  await home.click();
  // The workflow is kept on this computer; leaving it says so.
  const leave = page.getByRole("button", { name: "Leave", exact: true });
  const chooser = page.getByLabel("Choose a workflow file");
  await expect(leave.or(chooser).first()).toBeAttached();
  if (await leave.isVisible()) await leave.click();
  await expect(chooser).toBeAttached();
}
/** The line under a field (its message, hint or reason). */
const lineOf = (field: Locator) =>
  field.locator(":scope > .param > .param-line").first();
/** The step fixture with summarize's prompt as a text template. */
function templateWorkflow() {
  const workflow = parse(readFileSync(stepFixture, "utf8"));
  workflow.spec.steps.find(
    (step: { id: string }) => step.id === "summarize",
  ).prompt = {
    op: {
      name: "concat",
      args: [{ literal: "Hello " }, { ref: "/input/email" }],
    },
  };
  return yamlFile("template-order.yaml", stringify(workflow));
}
/** The HTTP workflow with a step after its API call, to move between them. */
const twoSteps = httpWorkflow.replace(
  "  output: {literal: {}}\n",
  `    - id: shape-orders
      kind: transform
      value: {object: {orders: {ref: /steps/get-customer-orders/output}}}
  output: {literal: {}}
`,
);

for (const size of sizes)
  test.describe(`issues at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a compiler message shows under its field, the indicator lists it and Fix focuses it", async ({
      page,
    }) => {
      const details = await openConnectedFixture(page);
      await validateWith(page, [
        {
          code: "WV-COMP-TYPE_MISMATCH",
          severity: "error",
          message: "customerId expects a string",
          path: "/spec/steps/1/with/object/parameters/object/customerId",
        },
      ]);
      await details.open("lookup");
      await showParameters(page, details.dialog);
      const field = details.field("input.parameters.customerId");
      await expect(lineOf(field)).toHaveText(
        "This value's type doesn't match what the field expects.",
      );
      await expect(lineOf(field)).toHaveAttribute("data-kind", "error");
      const pill = issuesPill(details);
      await expect(pill).toHaveAccessibleName("1 issue");
      await expect(pill).toHaveAttribute("data-tone", "danger");
      await pill.click();
      const entry = page.getByRole("menuitem", { name: /Customer ID/ });
      await expect(entry).toBeFocused();
      await entry.click();
      await expect(
        field
          .locator(
            ".param-control :is(input, textarea, button, [role=combobox])",
          )
          .first(),
      ).toBeFocused();
      // The keyboard reaches the same entry: ArrowDown opens, Escape returns.
      await pill.focus();
      await page.keyboard.press("ArrowDown");
      await expect(entry).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(pill).toBeFocused();
      await expect(details.dialog).toBeVisible();
    });

    test("a problem in the Problems strip opens step details on its exact field", async ({
      page,
    }) => {
      const details = await openConnectedFixture(page);
      await validateWith(page, [
        {
          code: "WV-COMP-TYPE_MISMATCH",
          severity: "error",
          message: "customerId expects a string",
          path: "/spec/steps/1/with/object/parameters/object/customerId",
        },
      ]);
      const strip = page.getByRole("region", { name: "Compiler diagnostics" });
      const toggle = strip.getByRole("button", { name: "Diagnostics" });
      if ((await toggle.getAttribute("aria-expanded")) !== "true")
        await toggle.click();
      const place = strip.locator(".diagnostic-row .diagnostic-where").first();
      await place.scrollIntoViewIfNeeded();
      await place.click();
      await expect(details.dialogFor("lookup")).toBeVisible();
      await expect
        .poll(() =>
          details.dialog.evaluate(() =>
            document.activeElement
              ?.closest("[data-param]")
              ?.getAttribute("data-param"),
          ),
        )
        .toBe("input.parameters.customerId");
    });

    test("required messages wait for blur on a new step", async ({ page }) => {
      const details = await openStepFixture(page);
      await addCommonStep(page, "Call an API");
      await expect(details.dialogFor("call-api")).toBeVisible();
      const connection = details.field("connection");
      const url = details.field("url");
      await expect(connection).toHaveAttribute("data-required-empty", "");
      await expect(
        details.dialog.locator('.param-line[data-kind="error"]'),
      ).toHaveCount(0);
      await expect(issuesPill(details)).toHaveCount(0);
      // Step details open on Connection: leaving it empty shows its message only.
      await expect
        .poll(() =>
          connection.evaluate((field) =>
            field.contains(document.activeElement),
          ),
        )
        .toBe(true);
      await page.keyboard.press("Tab");
      await expect(lineOf(connection)).toContainText("Choose a connection.");
      await expect(lineOf(url)).not.toHaveAttribute("data-kind", "error");
      await expect(issuesPill(details)).toHaveAccessibleName("1 issue");
      // Execute step (Ctrl/Cmd+Enter, also while typing) shows every message.
      await page.keyboard.press("ControlOrMeta+Enter");
      await expect(lineOf(url)).toHaveText("Enter a value.");
      await expect(url.getByRole("textbox", { name: "URL" })).toHaveAttribute(
        "aria-required",
        "true",
      );
      await expect(issuesPill(details)).toHaveAccessibleName("2 issues");
    });

    test("an out-of-range number is kept and flagged, and closing still works", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await addCommonStep(page, "Call an API");
      await expect(details.dialogFor("call-api")).toBeVisible();
      await details.tab("Settings").click();
      await details
        .field("retry")
        .getByRole("switch", { name: "Retry on fail" })
        .click();
      const tries = details
        .field("retry.maxAttempts")
        .getByRole("textbox", { name: "Max tries" });
      await tries.fill("50");
      await expect(lineOf(details.field("retry.maxAttempts"))).toHaveText(
        "Use a number from 2 to 10.",
      );
      expect(await issueEntries(page, details)).toContain(
        "Max tries Use a number from 2 to 10.",
      );
      await details.close();
      await details.open("call-api");
      await details.tab("Settings").click();
      await expect(
        details
          .field("retry.maxAttempts")
          .getByRole("textbox", { name: "Max tries" }),
      ).toHaveValue("50");
    });

    test("fields are read-only while the source has an error", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      const source = page.getByRole("textbox", { name: "Workflow source" });
      await source.fill(`${await source.inputValue()}\n  broken: [`);
      await page
        .getByRole("button", { name: "Apply changes", exact: true })
        .click();
      await page.getByRole("tab", { name: "Designer", exact: true }).click();
      await details.open("check-customer");
      await expect(
        details.dialog.getByRole("button", {
          name: "Execute step",
          exact: true,
        }),
      ).toHaveCount(0);
      await expect(
        details.field("value").getByRole("textbox", { name: "Name of row 1" }),
      ).toHaveAttribute("readonly", "");
      await expect(lineOf(details.field("value"))).toHaveText(
        "Fix the source to edit this step.",
      );
      await details.close();
    });

    test("a published action without its document says who sets its timeout", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      await details.tab("Settings").click();
      const timeout = details.field("timeout");
      await expect(lineOf(timeout)).toHaveText("Set by sql.lookup@1.0.0.");
      await expect(lineOf(timeout)).toHaveAttribute("data-kind", "reason");
      await expect(timeout).not.toContainText("Enter a value.");
      await expect(issuesPill(details)).toHaveCount(0);
    });

    test("an issue in a Settings field opens Settings at that field", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await validateWith(page, [
        {
          code: "WV-COMP-TYPE_MISMATCH",
          severity: "error",
          message: "Choose a timeout",
          path: "/spec/steps/5/timeoutSeconds",
        },
      ]);
      // From the step's badge on the canvas.
      await page
        .locator('.tile-node[data-tile="wait-for-payment"] .tile-issue')
        .click();
      await expect(details.dialogFor("wait-for-payment")).toBeVisible();
      await expect(details.tab("Settings")).toHaveAttribute(
        "aria-selected",
        "true",
      );
      const timeout = details.field("timeout");
      await expect(
        timeout.getByRole("textbox", { name: "Wait at most" }),
      ).toBeFocused();
      await expect(lineOf(timeout)).toContainText(
        "This value's type doesn't match what the field expects.",
      );
      // From the pill, after moving to Parameters.
      await details.tab("Parameters").click();
      await issuesPill(details).click();
      await page.getByRole("menuitem", { name: /Wait at most/ }).click();
      await expect(details.tab("Settings")).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await expect(
        details.field("timeout").getByRole("textbox", { name: "Wait at most" }),
      ).toBeFocused();
    });

    test("numbers and JSON that don't apply stay, marked Not applied, through closing and reopening", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("wait-a-minute");
      const wait = details.field("duration");
      const amount = wait.getByRole("textbox", { name: "Wait for" });
      await amount.fill("abc");
      await expect(lineOf(wait)).toContainText("Not applied");
      await expect(lineOf(wait)).toContainText("Enter a number.");
      await expect(amount).toHaveAttribute("aria-invalid", "true");
      await details.close();
      await expect(
        page.getByText(
          "Not applied yet: Wait for. What you typed is kept until you finish it.",
        ),
      ).toBeVisible();
      expect(await sourceText(page)).toContain("durationSeconds: 60");
      await details.openWithKeyboard("wait-a-minute");
      await expect(amount).toHaveValue("abc");
      await expect(lineOf(wait)).toContainText("Not applied");
      await expect(issuesPill(details)).toHaveAccessibleName("1 issue");
      // Escape in the box discards it: the saved value is back.
      await amount.focus();
      await page.keyboard.press("Escape");
      await expect(amount).toHaveValue("1");
      await expect(amount).toBeFocused();
      await expect(lineOf(wait)).not.toContainText("Not applied");
      await expect(issuesPill(details)).toHaveCount(0);
      await details.close();

      // A path's result as JSON that doesn't parse: it stays shown although
      // the result saved is still the default.
      await details.openWithKeyboard("route-by-value");
      await details.dialog
        .getByRole("button", { name: "Path result" })
        .first()
        .click();
      const result = details.dialog
        .locator('[data-param="case.output"]')
        .first();
      const json = result.locator("textarea");
      await json.fill('{"tier": ');
      await page.keyboard.press("Tab");
      await expect(lineOf(result)).toContainText("This isn't valid JSON");
      await expect(lineOf(result)).toContainText("Not applied");
      await details.close();
      await details.openWithKeyboard("route-by-value");
      await expect(json).toHaveValue('{"tier": ');
      await expect(lineOf(result)).toContainText("Not applied");
      // Mapped keeps it out of sight and out of the list; Fixed shows it again.
      await result.getByRole("radio", { name: "Mapped", exact: true }).click();
      await expect(json).toHaveCount(0);
      await expect(lineOf(result)).not.toContainText("Not applied");
      expect(
        (await issueEntries(page, details)).filter((entry) =>
          entry.includes("Not applied"),
        ),
      ).toEqual([]);
      await result.getByRole("radio", { name: "Fixed", exact: true }).click();
      await expect(json).toHaveValue('{"tier": ');
      await expect(lineOf(result)).toContainText("Not applied");
      await expect(lineOf(result)).toContainText("This isn't valid JSON");
    });

    test("the toast on closing names steps in the workflow's order, three at most", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      const hold = async (
        id: string,
        field: string,
        label: string,
        settings = false,
      ) => {
        await details.openWithKeyboard(id);
        if (settings) await details.tab("Settings").click();
        const box = details.field(field).getByRole("textbox", { name: label });
        await box.fill("abc");
        await expect(lineOf(details.field(field))).toContainText("Not applied");
        await details.close();
      };
      // Held from the last step back to the first.
      await hold("wait-for-payment", "timeout", "Wait at most", true);
      await hold("wait-a-minute", "duration", "Wait for");
      await hold("notify-sales", "due", "Due after", true);
      await details.openWithKeyboard("route-by-value");
      await details.dialog
        .getByRole("button", { name: "Path result" })
        .first()
        .click();
      const result = details.dialog
        .locator('[data-param="case.output"]')
        .first();
      await result.locator("textarea").fill('{"tier": ');
      await page.keyboard.press("Tab");
      await expect(lineOf(result)).toContainText("Not applied");
      await details.close();
      await expect(
        page.getByText(
          "Not applied yet: Path result, Due after in notify-sales, Wait for in wait-a-minute and 1 more step. What you typed is kept until you finish them.",
          { exact: true },
        ),
      ).toBeVisible();
    });

    test("an entry left on another step is named when step details close", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("wait-a-minute");
      await details
        .field("duration")
        .getByRole("textbox", { name: "Wait for" })
        .fill("abc");
      await expect(lineOf(details.field("duration"))).toContainText(
        "Not applied",
      );
      await details.dialog
        .getByRole("button", { name: "Next step", exact: true })
        .click();
      await expect(details.dialogFor("wait-a-minute")).toHaveCount(0);
      await expect(details.dialog).toBeVisible();
      // Said politely on the way: no toast covers the next step's fields.
      await expect(
        details.dialog.locator('.sr-only[aria-live="polite"]').first(),
      ).toHaveText("wait-a-minute keeps 1 entry that isn't applied yet.");
      await expect(page.getByText(/^Not applied yet/)).toHaveCount(0);
      await details.close();
      await expect(
        page.getByText(
          "Not applied yet: Wait for in wait-a-minute. What you typed is kept until you finish it.",
        ),
      ).toBeVisible();
    });

    test("Fix opens the data picker or the dropdown", async ({ page }) => {
      const details = await openConnectedFixture(page);
      await validateWith(page, [
        {
          code: "WV-COMP-UNAVAILABLE_REFERENCE",
          severity: "error",
          message: "Referenced step does not dominate this expression.",
          path: "/spec/steps/1/with/object/parameters/object/customerId",
        },
      ]);
      await details.open("lookup");
      await showParameters(page, details.dialog);
      const field = details.field("input.parameters.customerId");
      await lineOf(field).getByRole("button", { name: "Use data…" }).click();
      const picker = field.getByRole("combobox", { name: "Customer ID" });
      await expect(picker).toBeFocused();
      await expect(picker).toHaveAttribute("aria-expanded", "true");
      await expect(field.getByRole("listbox")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(picker).toHaveAttribute("aria-expanded", "false");
      // The issues list's entry opens the same list.
      await issuesPill(details).click();
      await page.getByRole("menuitem", { name: /^Customer ID/ }).click();
      await expect(picker).toBeFocused();
      await expect(picker).toHaveAttribute("aria-expanded", "true");
      await expect(field.getByRole("listbox")).toBeVisible();
      await page.keyboard.press("Escape");
      await details.close();
      // A new Call an API: its Connection entry opens the slot list.
      await addCommonStep(page, "Call an API");
      await expect(details.dialogFor("call-api")).toBeVisible();
      await page.keyboard.press("Tab");
      await issuesPill(details).click();
      await page.getByRole("menuitem", { name: /^Connection/ }).click();
      const slots = details
        .field("connection")
        .getByRole("combobox", { name: "Connection" });
      await expect(slots).toBeFocused();
      await expect(slots).toHaveAttribute("aria-expanded", "true");
    });

    test("an issue on a field with a choice of how to give it focuses the value", async ({
      page,
    }) => {
      const details = await openConnectedFixture(page);
      await validateWith(page, [
        {
          code: "WV-COMP-UNKNOWN_ACTION",
          severity: "error",
          message: "Unknown action",
          path: "/spec/steps/1/uses",
        },
      ]);
      const strip = page.getByRole("region", { name: "Compiler diagnostics" });
      const toggle = strip.getByRole("button", { name: "Diagnostics" });
      if ((await toggle.getAttribute("aria-expanded")) !== "true")
        await toggle.click();
      await strip.locator(".diagnostic-row .diagnostic-where").first().click();
      await expect(details.dialogFor("lookup")).toBeVisible();
      await expect
        .poll(() =>
          page.evaluate(() => ({
            param: document.activeElement
              ?.closest("[data-param]")
              ?.getAttribute("data-param"),
            value: document.activeElement?.tagName,
            mode: document.activeElement?.getAttribute("role") === "radio",
          })),
        )
        .toEqual({ param: "action", value: "INPUT", mode: false });
    });

    test("a held template hides while the source has an error, and comes back after", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, templateWorkflow());
      await details.openWithKeyboard("summarize");
      const prompt = details.field("prompt");
      const text = prompt.getByRole("textbox", { name: "Prompt" });
      await text.focus();
      await text.press("ControlOrMeta+a");
      await page.keyboard.type("Dear {{ input.customerId", { delay: 5 });
      await expect(lineOf(prompt)).toContainText("Not applied");
      await details.close();
      const kept = page.getByText(/^Not applied yet/);
      await expect(kept).toBeVisible();
      await page
        .getByRole("button", { name: "Dismiss notification" })
        .first()
        .click();
      await expect(kept).toHaveCount(0);
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      const source = page.getByRole("textbox", { name: "Workflow source" });
      const good = await source.inputValue();
      await source.fill(`${good}\n  broken: [`);
      await page
        .getByRole("button", { name: "Apply changes", exact: true })
        .click();
      await page.getByRole("tab", { name: "Designer", exact: true }).click();
      await details.openWithKeyboard("summarize");
      // Read-only: the text that applied, nothing marked or listed.
      await expect(text).toHaveValue("Hello {{ input.email }}");
      await expect(text).toHaveAttribute("readonly", "");
      await expect(prompt.locator(".param-status")).toHaveCount(0);
      expect(
        (await issueEntries(page, details)).filter((entry) =>
          entry.startsWith("Prompt"),
        ),
      ).toEqual([]);
      await details.close();
      await expect(kept).toHaveCount(0);
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      await source.fill(good);
      await page
        .getByRole("button", { name: "Apply changes", exact: true })
        .click();
      await page.getByRole("tab", { name: "Designer", exact: true }).click();
      await details.openWithKeyboard("summarize");
      await expect(text).toHaveValue("Dear {{ input.customerId");
      await expect(lineOf(prompt)).toContainText("Not applied");
    });
  });

test.describe("entries that don't apply yet in an owned API action", () => {
  test.skip(!hasPython, "Needs the repository's Python environment.");
  test.afterAll(() => stopLocalAuthoring());

  for (const size of sizes)
    test.describe(`at ${size.tag}`, () => {
      test.use({ viewport: { width: size.width, height: size.height } });

      test("a held Max tries stays through a tab switch, Next and Previous step, Close and reopen", async ({
        page,
      }) => {
        const details = await openHttpStep(page, undefined, twoSteps, {
          recipe: {
            retry: {
              maxAttempts: 5,
              initialDelaySeconds: 2,
              maxDelaySeconds: 30,
            },
          },
        });
        await details.tab("Settings").click();
        const field = details.field("retry.maxAttempts");
        const tries = field.getByRole("textbox", { name: "Max tries" });
        await tries.fill("1");
        await expect(lineOf(field)).toContainText("Not applied");
        await expect(lineOf(field)).toContainText("Use a number from 2 to 10.");
        // In the narrow column the message moves under the pill, whole.
        const status = await box(lineOf(field).locator(".param-status"));
        const message = await box(lineOf(field).locator(".param-line-text"));
        expect(intersects(status, message)).toBe(false);
        expect(status.x).toBeCloseTo((await box(tries)).x, 0);
        expect(message.height).toBeLessThanOrEqual(41);
        expect(message.right).toBeLessThanOrEqual(
          (await box(tries)).right + 0.5,
        );
        // Parameters and back.
        await details.tab("Parameters").click();
        await details.tab("Settings").click();
        await expect(tries).toHaveValue("1");
        // Next step, then Previous step.
        await details.dialog
          .getByRole("button", { name: "Next step", exact: true })
          .click();
        await expect(details.dialogFor("shape-orders")).toBeVisible();
        await details.dialog
          .getByRole("button", { name: "Previous step", exact: true })
          .click();
        await expect(details.dialogFor("get-customer-orders")).toBeVisible();
        await details.tab("Settings").click();
        await expect(tries).toHaveValue("1");
        // Close, then the issue opens it again from the pill.
        await details.close();
        await expect(
          page.getByText(
            "Not applied yet: Max tries. What you typed is kept until you finish it.",
          ),
        ).toBeVisible();
        await details.open("get-customer-orders");
        await expect(details.tab("Parameters")).toHaveAttribute(
          "aria-selected",
          "true",
        );
        await issuesPill(details).click();
        await page.getByRole("menuitem", { name: /Max tries/ }).click();
        await expect(tries).toBeFocused();
        await expect(tries).toHaveValue("1");
        await page.keyboard.press("Escape");
        await expect(tries).toHaveValue("5");
        await expect(issuesPill(details)).toHaveCount(0);
      });

      test("a note the canvas has no room for stays through a tab switch and a reopen", async ({
        page,
      }) => {
        const details = await openHttpStep(page, undefined, undefined, {
          recipe: { responseSample: "s".repeat(261_300) },
        });
        await details.tab("Settings").click();
        const notes = details
          .field("notes")
          .getByRole("textbox", { name: "Notes" });
        await notes.click();
        await page.keyboard.insertText("x".repeat(1500));
        await expect(details.field("notes").getByRole("alert")).toContainText(
          "Not saved.",
        );
        await details.tab("Parameters").click();
        await details.tab("Settings").click();
        await expect(notes).toHaveValue("x".repeat(1500));
        await expect(details.field("notes").getByRole("alert")).toContainText(
          "Not saved.",
        );
        await details.close();
        await details.open("get-customer-orders");
        await issuesPill(details).click();
        await page.getByRole("menuitem", { name: /Notes/ }).click();
        await expect(notes).toBeFocused();
        await expect(notes).toHaveValue("x".repeat(1500));
      });

      test("a saved timeout out of range is an issue that Fix opens on Settings", async ({
        page,
      }) => {
        const details = await openHttpStep(page, undefined, undefined, {
          recipe: { timeoutSeconds: 31 },
        });
        await expect(issuesPill(details)).toHaveAccessibleName("1 issue");
        await issuesPill(details).click();
        const entry = page.getByRole("menuitem", { name: /Timeout/ });
        await expect(entry).toContainText("Use a number from 1 to 30.");
        await entry.click();
        await expect(details.tab("Settings")).toHaveAttribute(
          "aria-selected",
          "true",
        );
        const timeout = details.field("timeout");
        await expect(
          timeout.getByRole("textbox", { name: "Timeout" }),
        ).toBeFocused();
        await expect(lineOf(timeout)).toHaveText("Use a number from 1 to 30.");
        await timeout.getByRole("textbox", { name: "Timeout" }).fill("20");
        await expect(issuesPill(details)).toHaveCount(0);
      });
    });
});

test.describe("template text that doesn't apply yet", () => {
  for (const size of sizes)
    test.describe(`at ${size.tag}`, () => {
      test.use({ viewport: { width: size.width, height: size.height } });

      test("survives closing, moving between steps and reopening; Escape restores the saved text", async ({
        page,
      }) => {
        await withLanguageFeatures(page);
        const details = await openStepFixture(page, templateWorkflow());
        await details.openWithKeyboard("summarize");
        const prompt = details.field("prompt");
        const text = prompt.getByRole("textbox", { name: "Prompt" });
        await expect(text).toHaveValue("Hello {{ input.email }}");
        await text.focus();
        await text.press("ControlOrMeta+a");
        await page.keyboard.type("Dear {{ input.customerId", { delay: 5 });
        await expect(lineOf(prompt)).toContainText("Not applied");
        await expect(lineOf(prompt)).toContainText(
          "Close the braces: {{ input.field }}.",
        );
        await expect(text).toHaveAttribute("aria-invalid", "true");
        expect(await issueEntries(page, details)).toContain(
          "Prompt Not applied Close the braces: {{ input.field }}.",
        );
        // Closing keeps it; the workflow keeps the last text that applied.
        await details.close();
        await expect(
          page.getByText("Not applied yet: Prompt.", { exact: false }),
        ).toBeVisible();
        const saved = await sourceText(page);
        expect(saved).toContain("Hello ");
        expect(saved).not.toContain("Dear ");
        await details.openWithKeyboard("summarize");
        await expect(text).toHaveValue("Dear {{ input.customerId");
        await expect(
          prompt.getByRole("radio", { name: "Mapped", exact: true }),
        ).toBeChecked();
        await expect(lineOf(prompt)).toContainText("Not applied");
        // Next step and back.
        await details.dialog
          .getByRole("button", { name: "Next step", exact: true })
          .click();
        await expect(details.dialogFor("score")).toBeVisible();
        await details.dialog
          .getByRole("button", { name: "Previous step", exact: true })
          .click();
        await expect(details.dialogFor("summarize")).toBeVisible();
        await expect(text).toHaveValue("Dear {{ input.customerId");
        // Finishing it applies it.
        await text.focus();
        await text.press("End");
        await page.keyboard.type(" }}", { delay: 5 });
        await expect(lineOf(prompt)).not.toContainText("Not applied");
        expect(
          (await issueEntries(page, details)).filter((entry) =>
            entry.startsWith("Prompt"),
          ),
        ).toEqual([]);
        await text.focus();
        await text.press("End");
        // Escape takes back what doesn't apply, then blurs, then closes.
        await page.keyboard.insertText(" {{ input");
        await expect(lineOf(prompt)).toContainText("Not applied");
        await page.keyboard.press("Escape");
        await expect(text).toHaveValue("Dear {{ input.customerId }}");
        await expect(text).toBeFocused();
        await expect(lineOf(prompt)).not.toContainText("Not applied");
        await page.keyboard.press("Escape");
        await expect(text).not.toBeFocused();
        await expect(details.dialog).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(details.dialog).toHaveCount(0);
        expect(await sourceText(page)).toContain("ref: /input/customerId");
      });

      test("follows a rename and its Undo, and is gone in another opened workflow", async ({
        page,
      }) => {
        await withLanguageFeatures(page);
        const details = await openStepFixture(page, templateWorkflow());
        await details.openWithKeyboard("summarize");
        const text = details
          .field("prompt")
          .getByRole("textbox", { name: "Prompt" });
        await text.focus();
        await text.press("ControlOrMeta+a");
        await page.keyboard.type("Dear {{ input.customerId", { delay: 5 });
        await details.dialog
          .getByRole("button", { name: "Rename summarize" })
          .click();
        const name = details.dialog.getByRole("textbox", { name: "Step name" });
        await name.fill("summary");
        await page.keyboard.press("Enter");
        await expect(details.dialogFor("summary")).toBeVisible();
        await expect(text).toHaveValue("Dear {{ input.customerId");
        await details.dialog
          .getByRole("button", { name: "Rename summary" })
          .focus();
        await page.keyboard.press("ControlOrMeta+z");
        await expect(details.dialogFor("summarize")).toBeVisible();
        await expect(text).toHaveValue("Dear {{ input.customerId");
        await details.close();
        // The same file opened again is another workflow session.
        await goHome(page);
        await page
          .getByLabel("Choose a workflow file")
          .setInputFiles(templateWorkflow() as never);
        await expect(page.locator(".editor-bar")).toBeVisible();
        await details.openWithKeyboard("summarize");
        await expect(text).toHaveValue("Hello {{ input.email }}");
        await expect(
          details.field("prompt").locator(".param-status"),
        ).toHaveCount(0);
      });
    });
});

// ------------------------------------------------------------- geometry

test.describe("issues layout", () => {
  for (const size of [
    ...sizes,
    { tag: "360x640", width: 360, height: 640 },
    { tag: "640x360", width: 640, height: 360 },
  ])
    test.describe(`at ${size.tag}`, () => {
      test.use({ viewport: { width: size.width, height: size.height } });

      test("the pill and the messages share the header's and the form's edges and centers, and nothing clips or overlaps", async ({
        page,
      }) => {
        const details = await openConnectedFixture(page);
        await validateWith(page, [
          {
            code: "WV-COMP-UNAVAILABLE_REFERENCE",
            severity: "error",
            message: "Referenced step does not dominate this expression.",
            path: "/spec/steps/1/with/object/parameters/object/customerId",
          },
          {
            code: "WV-COMP-UNUSED",
            severity: "warning",
            message: "The output of this step is never read.",
            path: "/spec/steps/1",
          },
        ]);
        await details.open("lookup");
        await showParameters(page, details.dialog);
        const d = await density(page);
        const pill = issuesPill(details);
        await expect(pill).toHaveAccessibleName("2 issues");
        await expect(pill).toHaveAttribute("data-tone", "danger");

        // The header: the pill sits on the name's line, centered on it and
        // no taller, after the name and clear of Execute step and More.
        const head = details.dialog.locator(".sd-head");
        const headBox = await box(head);
        const name = await box(details.dialog.locator(".sd-name"));
        const pillBox = await box(pill);
        const face = await box(pill.locator(".sd-issues-face"));
        expectAligned(
          [name.middle, pillBox.middle, face.middle],
          "name and pill centers",
        );
        expect(pillBox.y).toBeGreaterThanOrEqual(name.y - 0.5);
        expect(pillBox.bottom).toBeLessThanOrEqual(name.bottom + 0.5);
        expect(pillBox.x).toBeGreaterThanOrEqual(name.right + 7.5);
        expect(face.height).toBe(24);
        expect(pillBox.height).toBeGreaterThanOrEqual(d.touch ? 44 : 24);
        expect(pillBox.width).toBeGreaterThanOrEqual(d.touch ? 44 : 24);
        const execute = await box(
          details.dialog.getByRole("button", {
            name: "Execute step",
            exact: true,
          }),
        );
        const more = await box(
          details.dialog.getByRole("button", {
            name: "More actions for lookup",
          }),
        );
        for (const control of [pillBox, execute, more]) {
          expect(control.x).toBeGreaterThanOrEqual(headBox.x);
          expect(control.right).toBeLessThanOrEqual(headBox.right + 0.5);
          expect(control.bottom).toBeLessThanOrEqual(headBox.bottom + 0.5);
        }
        expect(intersects(pillBox, execute)).toBe(false);
        expect(intersects(pillBox, more)).toBe(false);

        // The field: one left edge for label, control and message; the Fix
        // at the form's right edge; the line's parts on one center.
        const field = details.field("input.parameters.customerId");
        await field.scrollIntoViewIfNeeded();
        const line = lineOf(field);
        await expect(line).toContainText(
          "This uses data from a step that hasn't finished at this point in the workflow.",
        );
        const fix = line.getByRole("button", { name: "Use data…" });
        await expect(fix).toBeVisible();
        const form = details.dialog.locator(
          ".sd-tabpanel:not([hidden]) .param-form",
        );
        const formBox = await box(form);
        const control = field.locator(".param-control").first();
        expectAligned(
          [
            formBox.x,
            (await box(field.locator(".param-label").first())).x,
            (await box(control)).x,
            (await box(line)).x,
          ],
          "label, control and message left edges",
        );
        const fixBox = await box(fix);
        expect(fixBox.right).toBeCloseTo(formBox.right, 0);
        // An in-form action: link color, underlined on hover and focus only.
        await expect(fix).toHaveCSS("color", await tokenColor(page, "--link"));
        await expect(fix).toHaveCSS("text-decoration-line", "none");
        await fix.hover();
        await expect(fix).toHaveCSS("text-decoration-line", "underline");
        await page.mouse.move(0, 0);
        expect(fixBox.height).toBeGreaterThanOrEqual(d.touch ? 44 : 24);
        expectAligned(
          [
            (await box(line.locator("weave-icon").first())).middle,
            (await textBox(line.locator(".param-line-text"))).middle,
            fixBox.middle,
          ],
          "message icon, text and Fix",
        );
        expect(intersects(await box(line), await box(control))).toBe(false);

        // The list: inside the window, entries apart, nothing cut off.
        await pill.click();
        const list = page.getByRole("menu", { name: "2 issues" });
        await expect(list).toBeVisible();
        const listBox = await box(list);
        expect(listBox.x).toBeGreaterThanOrEqual(0);
        expect(listBox.y).toBeGreaterThanOrEqual(0);
        expect(listBox.right).toBeLessThanOrEqual(size.width);
        expect(listBox.bottom).toBeLessThanOrEqual(size.height);
        const entries = await boxes(list.getByRole("menuitem"));
        expect(entries).toHaveLength(2);
        for (const entry of entries) {
          expect(entry.height).toBeGreaterThanOrEqual(d.row);
          expect(entry.x).toBeGreaterThanOrEqual(listBox.x);
          expect(entry.right).toBeLessThanOrEqual(listBox.right + 0.5);
        }
        expect(intersects(entries[0], entries[1])).toBe(false);
        expectAligned(
          (await boxes(list.locator(".sd-issue-field"))).map((b) => b.x),
          "entry labels",
        );
        expect(await clippedText(details.dialog)).toEqual([]);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
        ).toBe(true);
        await page.screenshot({
          path: test.info().outputPath(`issues-${size.tag}.png`),
        });
        // The header is as tall as the one of a step without issues.
        await page.keyboard.press("Escape");
        await details.dialog
          .getByRole("button", { name: "Previous step", exact: true })
          .click();
        await expect(details.dialogFor("check-customer")).toBeVisible();
        await expect(issuesPill(details)).toHaveCount(0);
        expect((await box(head)).height).toBeCloseTo(headBox.height, 0);
      });
    });
});
