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
// Call an API, email and file steps offered by what they do: each insert
// brings an action the workflow owns and opens the new step's details on
// its first required empty field. The repository's own Python builds the
// API actions and lists the built-in email and file actions.
import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { parse, stringify } from "yaml";
import {
  builtinActions,
  hasPython,
  localAuthoring,
  stopLocalAuthoring,
} from "./local-authoring";
import {
  START_PLUS,
  StepDetailsPage,
  addCommonStep,
  fitCanvas,
  openStepFixture,
  sizes,
  stepFixture,
  withLanguageFeatures,
  withNewEditor,
  yamlFile,
} from "./step-details-po";
import { box, density, expectAligned } from "./step-geometry";
import { connected, newWorkflow, offline, sourceText } from "./support";

test.skip(!hasPython, "Needs the repository's Python environment.");
test.afterAll(() => stopLocalAuthoring());

/** Offline Studio whose host builds API actions and lists its built-in actions. */
async function authoring(page: Page) {
  await localAuthoring(page);
  await page.route("**/studio/contracts/connector-actions", (route) =>
    route.fulfill({ json: builtinActions() }),
  );
  await offline(page);
}

/** The fixture with these connection slots declared. */
const withSlots = (slots: Record<string, string>) => {
  const document = parse(readFileSync(stepFixture, "utf8"));
  document.spec.connections = {
    ...document.spec.connections,
    ...Object.fromEntries(
      Object.entries(slots).map(([name, connector]) => [name, { connector }]),
    ),
  };
  return yamlFile("order-intake.yaml", stringify(document));
};

const stepsOf = async (page: Page) =>
  parse(await sourceText(page)).spec.steps as Record<string, unknown>[];
const stepOf = async (page: Page, id: string) =>
  (await stepsOf(page)).find((step) => step["id"] === id)!;

/** The labels of the visible tab's top-level fields. */
const labels = (details: StepDetailsPage) =>
  details.topFields().locator(":scope > .param .param-label");

/**
 * Records every field that takes focus inside step details from now on, by
 * its parameter ID ("connection", "file"), in order.
 */
async function recordFocus(page: Page): Promise<() => Promise<string[]>> {
  await page.evaluate(() => {
    const seen: string[] = [];
    (window as unknown as { fieldFocus: string[] }).fieldFocus = seen;
    document.addEventListener(
      "focusin",
      (event) => {
        const target = event.target as HTMLElement;
        if (!target.closest("section.step-details")) return;
        seen.push(
          target.closest("[data-param]")?.getAttribute("data-param") ??
            target.getAttribute("aria-label") ??
            target.tagName,
        );
      },
      true,
    );
  });
  return () =>
    page.evaluate(() => [
      ...(window as unknown as { fieldFocus: string[] }).fieldFocus,
    ]);
}

/** Fields of the visible tab that show a problem. */
const fieldsWithProblems = (details: StepDetailsPage) =>
  details.dialog.locator(".sd-tabpanel:not([hidden]) .param.has-error");

/** The focused element sits in step details on the first field that still needs a value. */
async function expectFirstRequiredEmptyFocused(details: StepDetailsPage) {
  const focus = await details.dialog.evaluate((dialog) => {
    const active = document.activeElement as HTMLElement | null;
    const panel = dialog.querySelector(".sd-tabpanel:not([hidden])");
    const first = panel?.querySelector("[data-param][data-required-empty]");
    const field = active?.closest("[data-param]");
    const rect = active?.getBoundingClientRect();
    return {
      inside: !!active && dialog.contains(active),
      field: field?.getAttribute("data-param") ?? null,
      first: first?.getAttribute("data-param") ?? null,
      inView:
        !!rect &&
        rect.top >= 0 &&
        rect.left >= 0 &&
        rect.bottom <= innerHeight &&
        rect.right <= innerWidth,
    };
  });
  expect(focus.inside).toBe(true);
  expect(focus.first).not.toBeNull();
  expect(focus.field).toBe(focus.first);
  expect(focus.inView).toBe(true);
}

for (const size of sizes)
  test.describe(`Call an API and owned steps at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const sheet = size.width < 768;

    test("inserting Call an API opens its step details on Connection", async ({
      page,
    }) => {
      await withNewEditor(page);
      await authoring(page);
      await newWorkflow(page);
      await addCommonStep(page, "Call an API");
      const details = new StepDetailsPage(page);
      await expect(details.dialogFor("call-api")).toBeVisible();
      await expect(
        details.dialog.getByRole("combobox", { name: "Connection" }),
      ).toBeFocused();
      await expect(details.topFields()).toHaveCount(3);
      await expect(labels(details)).toHaveText(["Connection", "Method", "URL"]);
      await expectFirstRequiredEmptyFocused(details);
      await details.close();
      await expect(details.node("call-api")).toHaveCount(1);
      const step = await stepOf(page, "call-api");
      expect(step["uses"]).toBe("untitled-workflow.call-api@1.0.0");
    });

    test("POST adds Body, and GET removes it with an Undo toast", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      await addCommonStep(page, "Call an API");
      const method = details
        .field("method")
        .getByRole("combobox", { name: "Method" });
      await method.click();
      await page.getByRole("option", { name: /^POST/ }).click();
      await expect(details.field("body")).toBeVisible();
      const body = details.field("body");
      await body.getByRole("textbox", { name: "Name of row 1" }).fill("note");
      await body.getByRole("textbox", { name: "Name of row 1" }).press("Tab");
      await expect(
        body.getByRole("group", { name: /^Row 1 of 1/ }),
      ).toHaveCount(1);
      await method.click();
      await page.getByRole("option", { name: /^GET/ }).click();
      await expect(details.field("body")).toHaveCount(0);
      const toast = page.locator(".toast-region");
      await expect(toast).toContainText(
        "Removed Body. Only for POST, PUT and PATCH.",
      );
      await toast.getByRole("button", { name: "Undo" }).click();
      await expect(details.field("body")).toBeVisible();
      await expect(method).toHaveValue("POST · Changes data");
    });

    test("Call an API edits method, URL, query, headers and body of its own action", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      await addCommonStep(page, "Call an API");
      const connection = details
        .field("connection")
        .getByRole("combobox", { name: "Connection" });
      await connection.click();
      await page.getByRole("option", { name: /^New slot/ }).click();
      await expect(connection).toHaveAttribute("data-value", "http");
      const url = details.field("url").getByRole("textbox", { name: "URL" });
      await url.fill("/customers/");
      await url.press("Tab");
      const panes = details.dialog.getByRole("tablist", {
        name: "Step details panes",
      });
      const customer = details.dialog
        .locator(".sd-input")
        .getByRole("listitem", { name: /^customerId, / });
      if (sheet) {
        await panes.getByRole("tab", { name: "Input" }).click();
        await customer.focus();
        await page.keyboard.press("Enter");
        await page
          .getByRole("menu", { name: "Map customerId to" })
          .getByRole("menuitem", { name: "URL", exact: true })
          .click();
        await panes.getByRole("tab", { name: "Parameters" }).click();
      } else {
        const target = (await url.boundingBox())!;
        await customer.dragTo(url, {
          targetPosition: { x: target.width - 4, y: target.height / 2 },
        });
      }
      await expect(
        details
          .field("url.path.customerId")
          .getByRole("button", { name: "reference Input Customer ID" }),
      ).toBeVisible();
      const method = details
        .field("method")
        .getByRole("combobox", { name: "Method" });
      await method.click();
      await page.getByRole("option", { name: /^POST/ }).click();
      /** Names a new row of a key-value field, then gives it a fixed value. */
      const row = async (field: string, key: string, value: string) => {
        const names = details
          .field(field)
          .getByRole("textbox", { name: /^Name of row / });
        await names.last().fill(key);
        await names.last().press("Tab");
        await details
          .field(field)
          .getByRole("textbox", { name: "Value", exact: true })
          .last()
          .fill(value);
      };
      for (const [option, field, key, value] of [
        ["Query parameters", "query", "limit", "25"],
        ["Headers", "headers", "Accept", "application/json"],
      ]) {
        await details.dialog
          .getByRole("button", { name: "Add option" })
          .click();
        await page
          .getByRole("menuitem", { name: new RegExp(`^${option}`) })
          .click();
        await row(field, key, value);
      }
      await row("body", "note", "hi");
      await details.close();
      const step = await stepOf(page, "call-api");
      expect(step["uses"]).toBe("order-intake.call-api@1.0.0");
      expect(step["connection"]).toBe("http");
      const input = JSON.stringify(step["with"]);
      expect(input).toContain('"customerId":{"ref":"/input/customerId"}');
      for (const [key, value] of [
        ["limit", "25"],
        ["Accept", "application/json"],
        ["note", "hi"],
      ]) {
        // A fixed value, inside a literal object or as a literal of its own.
        const text = value.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
        expect(input).toMatch(
          new RegExp(`"${key}":(\\{"literal":"${text}"\\}|"${text}")`),
        );
      }
      expect(parse(await sourceText(page)).spec.connections.http).toEqual({
        connector: "weave-http@2.0.0",
        required: true,
      });
      await details.open("call-api");
      await expect(
        details.field("method").getByRole("combobox", { name: "Method" }),
      ).toHaveValue("POST · Changes data");
      await expect(details.field("headers")).toBeVisible();
      await expect(
        details.field("url").getByRole("textbox", { name: "URL" }),
      ).toHaveValue("/customers/{customerId}");
    });

    test("Send email, Reply to an email and file steps insert with their fields", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      await addCommonStep(page, "Send email");
      await expect(details.dialogFor("send-email")).toBeVisible();
      await expect(labels(details)).toHaveText([
        "Connection",
        "To",
        "Subject",
        "Message",
      ]);
      await expectFirstRequiredEmptyFocused(details);
      await details.close();
      await addCommonStep(page, "Reply to an email");
      await expect(details.dialogFor("reply-email")).toBeVisible();
      await expect(labels(details)).toHaveText([
        "Connection",
        "Reply to message",
        "Conversation",
        "Message",
      ]);
      await details.close();
      await addCommonStep(page, "Write a file");
      await expect(details.dialogFor("write-file")).toBeVisible();
      await expect(labels(details)).toHaveText([
        "Connection",
        "File",
        "Destination",
      ]);
      await details.close();
      await addCommonStep(page, "List files");
      await expect(labels(details)).toHaveText(["Connection", "Folder"]);
      await details.close();
      await addCommonStep(page, "Send email");
      await expect(details.dialogFor("send-email-2")).toBeVisible();
      await details.close();
      const uses = Object.fromEntries(
        (await stepsOf(page)).map((step) => [step["id"], step["uses"]]),
      );
      expect(uses["send-email"]).toBe("order-intake.send-email@1.0.0");
      expect(uses["send-email-2"]).toBe("order-intake.send-email@1.0.0");
      expect(uses["reply-email"]).toBe("order-intake.reply-email@1.0.0");
      expect(uses["write-file"]).toBe("order-intake.write-file@1.0.0");
      expect(uses["list-files"]).toBe("order-intake.list-files@1.0.0");
      // The built-in actions arrived: no step says its action is missing.
      await details.open("send-email");
      await expect(details.dialog).not.toContainText(
        "Couldn't load the action this step uses.",
      );
    });

    test("rename follows the owned action, and Undo restores it", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      await addCommonStep(page, "Call an API");
      await details.dialog
        .getByRole("button", { name: "Rename call-api" })
        .click();
      const name = details.dialog.getByRole("textbox", { name: "Step name" });
      await name.fill("Get customer orders!");
      await name.press("Enter");
      await expect(details.dialogFor("get-customer-orders")).toBeVisible();
      await details.close();
      expect((await stepOf(page, "get-customer-orders"))["uses"]).toBe(
        "order-intake.get-customer-orders@1.0.0",
      );
      await details.node("get-customer-orders").focus();
      await page.keyboard.press("ControlOrMeta+z");
      expect((await stepOf(page, "call-api"))["uses"]).toBe(
        "order-intake.call-api@1.0.0",
      );
    });

    test("a Call an API step keeps its own action after a reload", async ({
      page,
    }) => {
      await withNewEditor(page);
      await authoring(page);
      await newWorkflow(page);
      await addCommonStep(page, "Call an API");
      const details = new StepDetailsPage(page);
      await details
        .field("method")
        .getByRole("combobox", { name: "Method" })
        .click();
      await page.getByRole("option", { name: /^POST/ }).click();
      await details.close();
      // The draft on this computer carries the canvas sidecar, recipe included (autosave waits 800 ms).
      await expect
        .poll(() =>
          page.evaluate(() =>
            Object.keys(localStorage)
              .filter((key) => key.startsWith("weave.localDraft."))
              .map((key) => localStorage.getItem(key) ?? "")
              .join(),
          ),
        )
        .toContain('"method":"POST"');
      await page.reload();
      // Studio reopens the draft, or lists it on Home.
      const recent = page
        .locator(".preview-row", { hasText: "untitled-workflow" })
        .first();
      const editor = page.locator(".editor-bar");
      await expect(recent.or(editor).first()).toBeVisible();
      if (!(await editor.isVisible())) await recent.click();
      const show = page.getByRole("button", {
        name: "Show canvas",
        exact: true,
      });
      const canvas = page.getByLabel("Workflow canvas", { exact: true });
      await expect(canvas.or(show).first()).toBeVisible();
      if (await show.isVisible()) await show.click();
      await details.open("call-api");
      await expect(
        details.field("method").getByRole("combobox", { name: "Method" }),
      ).toHaveValue("POST · Changes data");
    });

    test("a step added inside a path opens on its first required empty field, and each Undo takes back one insert", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      const plus = "Add a step to path tier is gold of route-by-value";
      await addCommonStep(page, "Send email", plus);
      await expect(details.dialogFor("send-email")).toBeVisible();
      await expectFirstRequiredEmptyFocused(details);
      await details.close();
      await addCommonStep(page, "Send email", START_PLUS);
      await expect(details.dialogFor("send-email-2")).toBeVisible();
      await details.close();
      const decision = (await stepOf(page, "route-by-value")) as {
        cases: { steps: { id: string; uses: string }[] }[];
      };
      expect(decision.cases[1].steps.map((s) => [s.id, s.uses])).toEqual([
        ["send-email", "order-intake.send-email@1.0.0"],
      ]);
      expect((await stepsOf(page))[0]["uses"]).toBe(
        "order-intake.send-email@1.0.0",
      );
      await details.node("send-email-2").focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(details.node("send-email-2")).toHaveCount(0);
      await expect(details.node("send-email")).toHaveCount(1);
      await details.node("send-email").focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(details.node("send-email")).toHaveCount(0);
    });

    test("a new Write a file step without a file server slot focuses only Connection, and File waits until it is left", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      const focused = await recordFocus(page);
      await addCommonStep(page, "Write a file");
      await expect(details.dialogFor("write-file")).toBeVisible();
      const connection = details
        .field("connection")
        .getByRole("combobox", { name: "Connection" });
      await expect(connection).toBeFocused();
      // Long enough for a late focus from a field's first render to show.
      await page.waitForTimeout(500);
      expect(await focused()).toEqual(["connection"]);
      const file = details.field("file");
      await expect(file).not.toContainText("Map a value.");
      await expect(fieldsWithProblems(details)).toHaveCount(0);
      // Entered and left empty, File asks for its value.
      await file.getByRole("combobox", { name: "File" }).focus();
      await page.keyboard.press("Tab");
      await expect(file).toContainText("Map a value.");
    });

    test("every common action opens on its first required empty field, once, with no message showing", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      const focused = await recordFocus(page);
      for (const label of [
        "Call an API",
        "Send email",
        "Reply to an email",
        "List files",
        "Read a file",
        "Download a file",
        "Write a file",
        "Move a file",
        "Delete a file",
      ]) {
        await page.evaluate(
          () =>
            ((window as unknown as { fieldFocus: string[] }).fieldFocus.length =
              0),
        );
        await addCommonStep(page, label);
        await expect(details.dialog).toBeVisible();
        await expectFirstRequiredEmptyFocused(details);
        await page.waitForTimeout(300);
        expect(await focused(), label).toHaveLength(1);
        await expect(fieldsWithProblems(details), label).toHaveCount(0);
        await details.close();
      }
    });

    test("api, http and request then Enter insert Call an API", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      for (const [word, id] of [
        ["api", "call-api"],
        ["http", "call-api-2"],
        ["request", "call-api-3"],
      ]) {
        await fitCanvas(page);
        await page
          .getByRole("button", { name: START_PLUS })
          .filter({ visible: true })
          .first()
          .click();
        const search = page.getByRole("combobox", {
          name: "Search steps and actions",
        });
        await search.fill(word);
        await search.press("Enter");
        await expect(details.dialogFor(id)).toBeVisible();
        await details.close();
      }
      const uses = (await stepsOf(page))
        .filter((step) => String(step["id"]).startsWith("call-"))
        .map((step) => step["uses"]);
      expect(uses).toEqual([
        "order-intake.call-api-3@1.0.0",
        "order-intake.call-api-2@1.0.0",
        "order-intake.call-api@1.0.0",
      ]);
    });

    test("a file step moves to another file server without changing a step that shares its action", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        withSlots({
          files: "weave-sftp@1.0.0",
          docs: "weave-google-drive@1.0.0",
        }),
        authoring,
      );
      await addCommonStep(page, "Write a file");
      await details.close();
      await addCommonStep(page, "Write a file");
      await expect(details.dialogFor("write-file-2")).toBeVisible();
      const connection = details
        .field("connection")
        .getByRole("combobox", { name: "Connection" });
      await expect(connection).toHaveAttribute("data-value", "files");
      await connection.click();
      // Every file server's slots are offered for the step's own file action.
      await expect(page.getByRole("option", { name: /^docs/ })).toBeVisible();
      await expect(
        page.getByRole("option", { name: /^ledger-db/ }),
      ).toHaveCount(0);
      await page.getByRole("option", { name: /^docs/ }).click();
      await expect(connection).toHaveAttribute("data-value", "docs");
      // A drive also asks for the file's name.
      await expect(labels(details)).toHaveText([
        "Connection",
        "File",
        "Destination",
        "Name",
      ]);
      await details.close();
      const steps = await stepsOf(page);
      expect(
        steps.slice(0, 2).map((s) => [s["id"], s["uses"], s["connection"]]),
      ).toEqual([
        ["write-file-2", "order-intake.write-file-2@1.0.0", "docs"],
        ["write-file", "order-intake.write-file@1.0.0", "files"],
      ]);
      // One Undo puts the step back on its first file server and action.
      await details.node("write-file-2").focus();
      await page.keyboard.press("ControlOrMeta+z");
      const back = await stepOf(page, "write-file-2");
      expect([back["uses"], back["connection"]]).toEqual([
        "order-intake.write-file@1.0.0",
        "files",
      ]);
    });
  });

test.describe("inserting from the step picker with the platform", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a published action opens on its first required empty field", async ({
    page,
  }) => {
    await withNewEditor(page);
    await connected(page);
    await withLanguageFeatures(page);
    await page.getByLabel("Choose a workflow file").setInputFiles(stepFixture);
    const details = new StepDetailsPage(page);
    await expect(page.locator(".editor-bar")).toBeVisible();
    await page
      .getByRole("button", { name: START_PLUS })
      .filter({ visible: true })
      .first()
      .click();
    await page
      .getByRole("group", { name: "Published actions" })
      .getByRole("option", { name: /^crm\.lookup/ })
      .click();
    await expect(details.dialogFor("call-action-1")).toBeVisible();
    await expectFirstRequiredEmptyFocused(details);
  });

  test("an insert that finishes after another workflow opened changes nothing there", async ({
    page,
  }) => {
    await withNewEditor(page);
    await connected(page);
    let release!: () => void;
    const held = new Promise<void>((done) => (release = done));
    // The action's details take their time to arrive.
    let asked = false;
    await page.route("**/projects/project/actions/a2/export", async (route) => {
      asked = true;
      await held;
      await route.fallback();
    });
    await page.getByLabel("Choose a workflow file").setInputFiles(stepFixture);
    await expect(page.locator(".editor-bar")).toBeVisible();
    await page
      .getByRole("button", { name: START_PLUS })
      .filter({ visible: true })
      .first()
      .click();
    await page
      .getByRole("group", { name: "Published actions" })
      .getByRole("option", { name: /^crm\.lookup/ })
      .click();
    // The insert is waiting for the action's details.
    await expect.poll(() => asked).toBe(true);
    await page.getByRole("button", { name: "Home", exact: true }).click();
    await newWorkflow(page);
    release();
    // The late answer neither inserts nor opens anything in the new workflow.
    await page.waitForTimeout(500);
    await expect(
      page.getByRole("dialog", { name: /^Step details: / }),
    ).toHaveCount(0);
    expect(parse(await sourceText(page)).spec.steps).toEqual([]);
  });
});

test.describe("the step picker with the new editor off", () => {
  for (const size of sizes)
    test.describe(`at ${size.tag}`, () => {
      test.use({ viewport: { width: size.width, height: size.height } });

      test("offers no common actions and inserts without opening step details", async ({
        page,
      }) => {
        await offline(page);
        await newWorkflow(page);
        await page
          .getByRole("button", { name: START_PLUS })
          .filter({ visible: true })
          .first()
          .click();
        await expect(
          page.getByRole("group", { name: "Steps" }).first(),
        ).toBeVisible();
        await expect(
          page.getByRole("group", { name: "Common actions" }),
        ).toHaveCount(0);
        await page
          .getByRole("option", { name: /^Call an action/ })
          .filter({ visible: true })
          .first()
          .click();
        await expect(page.locator('[data-step="call-action-1"]')).toHaveCount(
          1,
        );
        await expect(
          page.getByRole("dialog", { name: /^Step details: / }),
        ).toHaveCount(0);
      });
    });
});

const allSizes = [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
];

for (const size of allSizes)
  test.describe(`the Common actions group at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("lines up with the picker's other rows and fits the window", async ({
      page,
    }) => {
      await openStepFixture(page, stepFixture, authoring);
      const fit = page.getByRole("button", { name: "Fit all", exact: true });
      if (await fit.isVisible()) await fit.click();
      await page
        .getByRole("button", { name: START_PLUS })
        .filter({ visible: true })
        .first()
        .click();
      const picker = page.locator(".step-picker");
      const group = picker.getByRole("group", { name: "Common actions" });
      const list = picker.locator(".step-picker-list");
      await group.scrollIntoViewIfNeeded();
      await expect(group.getByRole("option")).toHaveCount(9);
      const frame = await box(picker);
      // The picker stays in the window.
      expect(frame.x).toBeGreaterThanOrEqual(0);
      expect(frame.y).toBeGreaterThanOrEqual(0);
      expect(frame.right).toBeLessThanOrEqual(size.width);
      expect(frame.bottom).toBeLessThanOrEqual(size.height);
      const steps = page
        .getByRole("group", { name: "Steps" })
        .getByRole("option")
        .first();
      const reference = {
        row: await box(steps),
        icon: await box(steps.locator(".step-icon")),
        label: await box(steps.locator(".step-picker-label")),
      };
      const shown = await box(list);
      const heading = await box(group.locator(".step-picker-group"));
      expectAligned(
        [heading.x, (await box(page.locator(".step-picker-group").first())).x],
        "group headings",
      );
      // Every row of the group, measured in the list's own coordinates.
      const rows = await group.evaluate((element) => {
        const list = element.closest<HTMLElement>(".step-picker-list")!;
        const top = list.getBoundingClientRect().top - list.scrollTop;
        return [...element.querySelectorAll<HTMLElement>("[role=option]")].map(
          (row) => {
            const at = (selector: string) => {
              const r = row.querySelector(selector)!.getBoundingClientRect();
              return { x: r.x, right: r.right, y: r.y - top, height: r.height };
            };
            const r = row.getBoundingClientRect();
            return {
              row: { x: r.x, right: r.right, y: r.y - top, height: r.height },
              icon: at(".step-icon"),
              label: at(".step-picker-label"),
              text: at(".step-picker-text"),
              cut: [...row.querySelectorAll<HTMLElement>("span, small")].some(
                (part) => part.scrollWidth > part.clientWidth + 1,
              ),
            };
          },
        );
      });
      const target = (await density(page)).touch ? 44 : 24;
      const middle = (b: { y: number; height: number }) => b.y + b.height / 2;
      rows.forEach((item, i) => {
        // One left and one right edge with the Steps rows.
        expectAligned([item.row.x, reference.row.x], "row left");
        expectAligned([item.row.right, reference.row.right], "row right");
        expectAligned([item.icon.x, reference.icon.x], "icon left");
        expectAligned([item.label.x, reference.label.x], "label left");
        // The icon sits on the row's center line, beside the text.
        expectAligned([middle(item.icon), middle(item.text)], "icon center");
        expect(item.icon.right).toBeLessThanOrEqual(item.label.x);
        expect(item.text.right).toBeLessThanOrEqual(item.row.right + 0.5);
        // Inside the list's box, a full target, after the row above it.
        expect(item.row.x).toBeGreaterThanOrEqual(shown.x - 0.5);
        expect(item.row.right).toBeLessThanOrEqual(shown.right + 0.5);
        expect(item.row.height).toBeGreaterThanOrEqual(target);
        if (i)
          expect(item.row.y).toBeGreaterThanOrEqual(
            rows[i - 1].row.y + rows[i - 1].row.height - 0.5,
          );
        // No word is cut.
        expect(item.cut).toBe(false);
      });
      expect(shown.width).toBeGreaterThan(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth + 1,
        ),
      ).toBe(true);
    });

    test("an insert opens step details with the first required empty field focused in view", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      await addCommonStep(page, "Call an API");
      await expect(details.dialogFor("call-api")).toBeVisible();
      await expectFirstRequiredEmptyFocused(details);
      const field = await box(details.field("connection"));
      const control = await box(
        details.dialog.getByRole("combobox", { name: "Connection" }),
      );
      expect(control.x).toBeGreaterThanOrEqual(field.x - 0.5);
      expect(control.right).toBeLessThanOrEqual(field.right + 0.5);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth + 1,
        ),
      ).toBe(true);
    });
  });

for (const size of allSizes)
  test.describe(`the data picker of a Mapped field at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("looks and sizes like the other fields: one height, body text and full targets", async ({
      page,
    }) => {
      const details = await openStepFixture(page, stepFixture, authoring);
      const sizes = await density(page);
      await addCommonStep(page, "Write a file");
      await expect(details.dialogFor("write-file")).toBeVisible();
      const file = details.field("file");
      const picker = file.locator(".ref-combo-field");
      const destination = details.field("destination").locator(".param-input");
      const connection = details.field("connection").locator(".choice-field");
      await file.scrollIntoViewIfNeeded();
      const field = await box(picker);
      // The same box as the fields around it, at the density's height.
      expectAligned(
        [field.height, (await box(connection)).height, sizes.control],
        "picker height",
      );
      const next = await box(destination);
      expectAligned([field.x, next.x], "picker left");
      expectAligned([field.right, next.right], "picker right");
      // Its text is the fields' text.
      const fontSize = (locator: typeof picker) =>
        locator.evaluate((element) => getComputedStyle(element).fontSize);
      const bodySize = await fontSize(destination);
      expect(await fontSize(picker.locator("input"))).toBe(bodySize);
      // The text box and the toggle fill the box: each a full target.
      for (const part of [
        picker.locator("input"),
        picker.locator(".ref-combo-toggle"),
      ]) {
        const target = await box(part);
        expect(target.height).toBeGreaterThanOrEqual(sizes.target - 0.5);
        expect(target.width).toBeGreaterThanOrEqual(sizes.target - 0.5);
        expect(target.y).toBeGreaterThanOrEqual(field.y - 0.5);
        expect(target.bottom).toBeLessThanOrEqual(field.bottom + 0.5);
      }
      // A field switched to Mapped while empty shows the same picker.
      await details.close();
      await addCommonStep(page, "Send email");
      await expect(details.dialogFor("send-email")).toBeVisible();
      const message = details.field("message");
      await message.scrollIntoViewIfNeeded();
      await message.hover();
      await message
        .getByRole("radiogroup", { name: "Value mode for Message" })
        .getByRole("radio", { name: "Mapped" })
        .click();
      const mapped = message.locator(".ref-combo-field");
      await expect(mapped.locator("input")).toBeFocused();
      expectAligned([(await box(mapped)).height, sizes.control], "Message");
      expect(await fontSize(mapped.locator("input"))).toBe(bodySize);
      // In a key-value row, the picker is the row's height.
      await details.close();
      await addCommonStep(page, "Call an API");
      await expect(details.dialogFor("call-api")).toBeVisible();
      await details.dialog.getByRole("button", { name: "Add option" }).click();
      await page.getByRole("menuitem", { name: /^Query parameters/ }).click();
      const query = details.field("query");
      const name = query.getByRole("textbox", { name: "Name of row 1" });
      await name.fill("limit");
      await name.press("Tab");
      const row = query.locator(".param-kv-row").first();
      await row.hover();
      await row
        .getByRole("radiogroup", { name: "Value mode for Value" })
        .getByRole("radio", { name: "Mapped" })
        .click();
      const inRow = row.locator(".ref-combo-field");
      await expect(inRow.locator("input")).toBeFocused();
      expectAligned(
        [
          (await box(inRow)).height,
          (await box(row.locator(".param-kv-name"))).height,
          sizes.row,
        ],
        "row picker",
      );
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth + 1,
        ),
      ).toBe(true);
    });
  });
