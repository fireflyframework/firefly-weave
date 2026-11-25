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
// Testing a platform event trigger: a sample event of a chosen kind, or one
// pasted with its real IDs replaced, becomes the trigger's test event, which
// Execute workflow starts from and later steps map with their types. The
// platform is mocked; the event's fields, the local checks and Execute are
// the repository's own Python.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  ATTENTION,
  FORM,
  openEventTrigger,
  openNotifyTeam,
} from "./event-trigger-po";
import { executions, executeButton } from "./execute-po";
import { localAuthoring } from "./local-authoring";
import { outputPane } from "./test-data-po";
import { pauseClock, selectChoice } from "./support";
import { addCommonStep, type StepDetailsPage } from "./step-details-po";

/** send reads the business key, which every platform event carries. */
const READS_KEY = [
  {
    id: "send",
    kind: "transform",
    value: {
      object: { key: { ref: "/input/transaction/business_key" } },
    },
  },
];
const NOTE =
  "Platform events come from transactions, so Studio can't listen for one. Choose an event to build a sample.";
const SHARED =
  "Test data is shared with everyone who can test this workflow. Use a made-up business key.";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const REAL = {
  event: "a3d0c1b2-1111-4222-8333-000000000001",
  run: "a3d0c1b2-1111-4222-8333-000000000002",
  incident: "a3d0c1b2-1111-4222-8333-000000000003",
};
const pastedEvent = (transaction: Record<string, unknown> = {}) => ({
  version: "weave/platform-event-v1",
  event_id: REAL.event,
  type: "transaction.in_doubt",
  emitted_at: "2026-11-18T16:05:11Z",
  transaction: {
    workflow: { name: "sync-records", version: "2.0.1" },
    run_id: REAL.run,
    instance_key: "move",
    business_key: "REF-4410",
    state: "in_doubt",
    cause: { code: "WV-TXN-UNDO-STUCK", step: "add" },
    steps: { done: [], undone: [], pending: [] },
    incident_id: REAL.incident,
    test: false,
    ...transaction,
  },
});

/** The Test event pane, shown where the window folds it behind a tab. */
async function testEvent(page: Page, dialog: Locator): Promise<Locator> {
  const tab = dialog.getByRole("tab", { name: "Test event", exact: true });
  if (await tab.count()) await tab.first().click();
  const pane = dialog.getByRole("region", { name: "Input data" });
  await expect(pane).toBeVisible();
  return pane;
}
async function generated(page: Page, details: StepDetailsPage) {
  const dialog = await openEventTrigger(page, details);
  const pane = await testEvent(page, dialog);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(
    pane.getByText("Sample ready. Execute workflow starts from it."),
  ).toBeVisible();
  return { dialog, pane };
}

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`a platform event trigger's Test event at ${size.width}x${size.height}`, () => {
    test.use({ viewport: size });

    test("builds a sample of an event the trigger reacts to", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, {
        spec: {
          eventTrigger: {
            events: ["transaction.compensated", "transaction.in_doubt"],
            from: ["sync-records"],
          },
        },
      });
      const dialog = await openEventTrigger(page, details);
      const pane = await testEvent(page, dialog);
      await expect(pane).toContainText(NOTE);
      const event = pane.getByRole("combobox", { name: "Event" });
      await expect(event).toHaveValue("A transaction needs attention");
      await event.click();
      await expect(
        page
          .getByRole("listbox", { name: "Event options" })
          .getByRole("option"),
      ).toHaveText([
        "A transaction needs attention",
        "A transaction was undone",
      ]);
      await page.keyboard.press("Escape");
      await selectChoice(event, { label: "A transaction was undone" });
      const generate = pane.getByRole("button", { name: "Generate sample" });
      await generate.click();
      await expect(
        pane.getByText("Sample ready. Execute workflow starts from it."),
      ).toBeVisible();
      await expect(
        pane.getByRole("button", { name: "Generate again" }),
      ).toBeFocused();
      const summary = pane.getByRole("group", { name: "The test event" });
      await expect(summary.getByRole("definition")).toHaveText([
        "Undone",
        "sync-records 1.0.0",
        "REF-1029",
      ]);
      const output = await outputPane(details);
      await expect(output.locator(".pill")).toHaveText("Test event");
      await expect(output.locator(".pane-count")).toHaveText(
        "Sample · 5 fields",
      );
      await expect(output).toContainText('"transaction.compensated"');
      // More fields than the pane holds: the keyboard scrolls it.
      if (size.width >= 768) {
        const fields = output.getByRole("group", {
          name: "The test event's fields",
        });
        await fields.focus();
        await page.keyboard.press("End");
        await expect
          .poll(() => fields.evaluate((element) => element.scrollTop))
          .toBeGreaterThan(0);
      }
    });

    test("a pasted event's real IDs never reach the test data", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page);
      const sent: string[] = [];
      page.on("request", (request) => sent.push(request.postData() ?? ""));
      const dialog = await openEventTrigger(page, details);
      const pane = await testEvent(page, dialog);
      await pane.getByRole("button", { name: "Paste a sample event" }).click();
      const box = pane.getByRole("textbox", { name: "Sample event (JSON)" });
      await expect(box).toBeFocused();
      // The first problem, by field, with what the platform would refuse.
      for (const [event, problem] of [
        [
          pastedEvent({ state: "open" }),
          "transaction.state must be one of in_doubt, compensated, cancelled, completed, reconciled, force_ended.",
        ],
        [
          { ...pastedEvent(), root_event_id: REAL.event },
          "root_event_id isn't one of the platform event's fields.",
        ],
        [
          pastedEvent({ cause: "x", steps: { done: "no" } }),
          "transaction.cause must be a group of fields or null.",
        ],
        [
          { ...pastedEvent(), emitted_at: "2026" },
          "emitted_at must be a date and time, such as 2026-11-18T16:05:11Z.",
        ],
        [
          pastedEvent({ business_key: "x".repeat(300) }),
          "transaction.business_key can have up to 200 characters.",
        ],
      ] as const) {
        await box.fill(JSON.stringify(event, null, 2));
        await pane.getByRole("button", { name: "Use this sample" }).click();
        await expect(pane.getByRole("alert")).toHaveText(problem);
        expect(sent.some((body) => body.includes("REF-4410"))).toBe(false);
      }
      await box.fill(JSON.stringify(pastedEvent(), null, 2));
      await pane.getByRole("button", { name: "Use this sample" }).click();
      const info = pane.getByRole("note").filter({ hasText: SHARED });
      await expect(info).toContainText(
        "Studio replaced the event, run and incident IDs with sample IDs.",
      );
      await expect(
        pane
          .getByRole("group", { name: "The test event" })
          .getByRole("definition"),
      ).toHaveText(["Needs attention", "sync-records 2.0.1", "REF-4410"]);
      await expect(
        pane.getByRole("group", { name: "The test event" }),
      ).toBeFocused();
      const output = await outputPane(details);
      await expect(output).toContainText("Pasted");
      await output.getByRole("radio", { name: "JSON" }).click();
      const json = output.locator(".pane-json");
      await expect(json).toContainText("REF-4410");
      for (const id of Object.values(REAL))
        await expect(json).not.toContainText(id);
      const kept = JSON.parse(await json.innerText()) as {
        event_id: string;
        transaction: { run_id: string; incident_id: string };
      };
      for (const id of [
        kept.event_id,
        kept.transaction.run_id,
        kept.transaction.incident_id,
      ])
        expect(id).toMatch(UUID);
      // Not in what Studio sent its host either.
      expect(sent.some((body) => body.includes("REF-4410"))).toBe(true);
      for (const id of Object.values(REAL))
        expect(sent.filter((body) => body.includes(id))).toEqual([]);
    });

    test("a pasted event the trigger doesn't react to says a real one starts nothing", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page);
      const dialog = await openEventTrigger(page, details);
      let pane = await testEvent(page, dialog);
      const use = async (event: object) => {
        await pane
          .getByRole("button", { name: "Paste a sample event" })
          .click();
        await pane
          .getByRole("textbox", { name: "Sample event (JSON)" })
          .fill(JSON.stringify(event));
        await pane.getByRole("button", { name: "Use this sample" }).click();
        await expect(summary).toBeFocused();
      };
      const summary = dialog
        .getByRole("region", { name: "Input data" })
        .getByRole("group", { name: "The test event" });
      const note = dialog.getByText("This trigger doesn't react to");
      await use({
        ...pastedEvent({
          state: "compensated",
          cause: undefined,
          incident_id: undefined,
        }),
        type: "transaction.compensated",
      });
      await expect(note).toHaveText(
        "This trigger doesn't react to Undone, so a real one starts nothing.",
      );
      await expect(summary).toHaveAccessibleDescription(
        "This trigger doesn't react to Undone, so a real one starts nothing.",
      );
      const [above, below] = [
        (await summary.boundingBox())!,
        (await note.boundingBox())!,
      ];
      expect(below.y).toBeGreaterThanOrEqual(above.y + above.height);
      // Once it reacts to it, a real one starts a run.
      const parameters = dialog.getByRole("tab", { name: "Parameters" });
      if (size.width < 768) await parameters.first().click();
      await dialog
        .getByRole("checkbox", { name: "A transaction was undone" })
        .check();
      pane = await testEvent(page, dialog);
      await expect(summary).toBeVisible();
      await expect(note).toHaveCount(0);
      await use(pastedEvent());
      await expect(note).toHaveCount(0);
    });

    test("Escape leaves the paste without using it, and step details stay open", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page);
      const dialog = await openEventTrigger(page, details);
      const pane = await testEvent(page, dialog);
      const paste = pane.getByRole("button", { name: "Paste a sample event" });
      await paste.click();
      // Keys pressed before the box takes focus would be the button's.
      await expect(
        pane.getByRole("textbox", { name: "Sample event (JSON)" }),
      ).toBeFocused();
      await page.keyboard.type("{");
      await page.keyboard.press("Escape");
      await expect(dialog).toBeVisible();
      await expect(paste).toBeFocused();
      await expect(
        pane.getByRole("group", { name: "The test event" }),
      ).toHaveCount(0);
    });

    test("Execute workflow starts from the sample, and a later step reads its business key", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, {
        spec: { steps: READS_KEY },
      });
      const calls = await localAuthoring(page);
      const { dialog } = await generated(page, details);
      const parameters = dialog
        .getByRole("tablist", { name: "Step details panes" })
        .getByRole("tab", { name: "Parameters" });
      if (await parameters.count()) await parameters.click();
      await executeButton(dialog).click();
      await expect.poll(() => executions(calls).length).toBe(1);
      expect(executions(calls)[0]!.body).toMatchObject({
        input: { type: "transaction.in_doubt" },
      });
      await details.close();
      const log = page.getByRole("region", { name: "Run log" });
      await expect(log.locator(".run-log-status")).toHaveText(
        "Completed · 1 step",
      );
      // What send read from the event is its result in that run.
      await details.open("send");
      const output = await outputPane(details);
      await expect(output.locator(".pane-source-tag")).toHaveText("Simulated");
      await expect(output).toContainText("REF-1029");
    });
  });

test.describe("a real platform event entered in another Test event pane", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  /** Nothing Studio sent its host, and nothing it kept, holds a real ID. */
  async function nothingReal(page: Page, details: StepDetailsPage) {
    const dialog = await openEventTrigger(page, details);
    const output = await outputPane(details);
    await output.getByRole("radio", { name: "JSON" }).click();
    await expect(output.locator(".pane-json")).toContainText("REF-4410");
    for (const id of Object.values(REAL))
      await expect(dialog).not.toContainText(id);
  }
  const NOTE_ALL = `Studio replaced the event, run and incident IDs with sample IDs. ${SHARED}`;

  test("the manual form's", async ({ page }) => {
    const details = await openNotifyTeam(page);
    const sent: string[] = [];
    page.on("request", (request) => sent.push(request.postData() ?? ""));
    await page
      .locator('.tile-node[data-tile^="$trigger"]')
      .filter({ hasText: "Manual" })
      .locator(".tile-body")
      .dblclick();
    const pane = page.getByRole("region", { name: "Input data" });
    const box = pane.locator("textarea.json-field");
    await box.focus();
    await page.keyboard.insertText(JSON.stringify(pastedEvent()));
    await expect(pane.getByText(NOTE_ALL)).toBeVisible();
    // The box, still open, holds the event as it's kept, the caret at its end.
    const caret = () =>
      box.evaluate((element: HTMLTextAreaElement) => {
        const { value, selectionStart, selectionEnd } = element;
        return selectionStart === selectionEnd && value.slice(0, selectionEnd);
      });
    const held = async () => {
      const text = await box.inputValue();
      expect(text).toContain('"business_key": "REF-4410"');
      for (const id of Object.values(REAL)) expect(text).not.toContain(id);
      return text;
    };
    /** How far the caret's line ends below the box's view, and how far above its end it starts. */
    const caretLine = () =>
      box.evaluate((element: HTMLTextAreaElement) => {
        const copy = element.cloneNode() as HTMLTextAreaElement;
        copy.style.cssText = `position: absolute; resize: none; height: 0; min-height: 0; width: ${element.offsetWidth}px`;
        element.after(copy);
        copy.value = element.value.slice(0, element.selectionEnd);
        const bottom = copy.scrollHeight;
        copy.remove();
        const { scrollTop, clientHeight } = element;
        return {
          below: bottom - scrollTop - clientHeight,
          above: scrollTop - bottom,
        };
      });
    const inView = async () => {
      const line = await caretLine();
      expect(line.below).toBeLessThanOrEqual(1);
      expect(line.above).toBeLessThan(0);
    };
    const first = await held();
    await expect(box).toBeFocused();
    expect(await caret()).toBe(first);
    await inView();
    // A real ID put back gets the sample ID it had, the caret right after it.
    await box.evaluate((element: HTMLTextAreaElement) => {
      const start = element.value.indexOf('"event_id": "') + 13;
      element.setSelectionRange(start, start + 36);
    });
    await page.keyboard.insertText(REAL.event);
    await expect(box).toHaveValue(first);
    await expect(box).toBeFocused();
    expect(await caret()).toMatch(/"event_id": "[0-9a-f-]{36}$/);
    await inView();
    // A refused change says what is wrong, and no more what Studio replaced.
    await box.fill(JSON.stringify({ ...pastedEvent(), root_event_id: "x" }));
    await expect(pane.getByRole("alert")).toHaveText(
      "root_event_id isn't one of the platform event's fields.",
    );
    await expect(pane.getByText(NOTE_ALL)).toHaveCount(0);
    await box.fill(first);
    await expect(pane.getByText(NOTE_ALL)).toBeVisible();
    await expect(pane.getByRole("alert")).toHaveCount(0);
    await details.close();
    await nothingReal(page, details);
    expect(sent.some((body) => body.includes("REF-4410"))).toBe(true);
    for (const id of Object.values(REAL))
      expect(sent.filter((body) => body.includes(id))).toEqual([]);
  });

  test("but the manual form's own sample is an event the trigger reacts to", async ({
    page,
  }) => {
    const details = await openNotifyTeam(page);
    await page
      .locator('.tile-node[data-tile^="$trigger"]')
      .filter({ hasText: "Manual" })
      .locator(".tile-body")
      .dblclick();
    const pane = page.getByRole("region", { name: "Input data" });
    await pane.getByRole("button", { name: "Generate sample" }).click();
    await expect(pane.locator("textarea.json-field")).toHaveValue(
      /"type": "transaction\.in_doubt"/,
    );
    await expect(pane.getByRole("alert")).toHaveCount(0);
    await details.close();
    const dialog = await openEventTrigger(page, details);
    await expect(
      dialog
        .getByRole("group", { name: "The test event" })
        .getByRole("definition"),
    ).toHaveText(["Needs attention", "move-between-systems 1.0.0", "REF-1029"]);
  });

  test("a Kafka message trigger's", async ({ page }) => {
    const details = await openNotifyTeam(page);
    const sent: string[] = [];
    page.on("request", (request) => sent.push(request.postData() ?? ""));
    await addCommonStep(page, "Kafka message", "Add a step after send");
    const pane = page.getByRole("region", { name: "Input data" });
    await pane.getByRole("button", { name: "Paste a sample message" }).click();
    await pane
      .getByRole("textbox", { name: "Sample message (JSON)" })
      .fill(JSON.stringify(pastedEvent()));
    await pane.getByRole("button", { name: "Use this sample" }).click();
    await expect(pane.getByText(NOTE_ALL)).toBeVisible();
    // An event runs past a short pane, which scrolls to its end.
    await page.setViewportSize({ width: 1440, height: 600 });
    const end = await pane.locator(".sd-pane-body").evaluate((body) => {
      body.scrollTop = body.scrollHeight;
      const json = body.querySelector("pre")!.getBoundingClientRect();
      return {
        scrolled: body.scrollTop > 0,
        below: json.bottom - body.getBoundingClientRect().bottom,
      };
    });
    expect(end.scrolled).toBe(true);
    expect(end.below).toBeLessThanOrEqual(0);
    await page.setViewportSize({ width: 1440, height: 900 });
    await details.close();
    await nothingReal(page, details);
    expect(sent.some((body) => body.includes("REF-4410"))).toBe(true);
    for (const id of Object.values(REAL))
      expect(sent.filter((body) => body.includes(id))).toEqual([]);
  });

  test("the manual form's, in a field of a workflow's own input, beside text that isn't a value yet", async ({
    page,
  }) => {
    await openNotifyTeam(page, {
      spec: {
        inputSchema: {
          type: "object",
          properties: {
            count: { type: "integer" },
            events: { type: "array", items: { type: "object" } },
          },
        },
      },
    });
    await page
      .locator('.tile-node[data-tile^="$trigger"]')
      .filter({ hasText: "Manual" })
      .locator(".tile-body")
      .dblclick();
    const pane = page.getByRole("region", { name: "Input data" });
    const count = pane.getByLabel(/^Count/);
    await count.fill("1.5");
    const box = pane.locator("textarea.json-field");
    await box.focus();
    await page.keyboard.insertText(JSON.stringify([pastedEvent()]));
    await expect(pane.getByText(NOTE_ALL)).toBeVisible();
    await expect(box).toHaveValue(/"business_key": "REF-4410"/);
    for (const id of Object.values(REAL))
      await expect(box).not.toHaveValue(new RegExp(id));
    await expect(count).toHaveValue("1.5");
  });

  test("a Kafka message trigger's, in a field of a workflow's own input", async ({
    page,
  }) => {
    const details = await openNotifyTeam(page, {
      spec: { inputSchema: { type: "object" } },
    });
    const sent: string[] = [];
    page.on("request", (request) => sent.push(request.postData() ?? ""));
    await addCommonStep(page, "Kafka message", "Add a step after send");
    const pane = page.getByRole("region", { name: "Input data" });
    await pane.getByRole("button", { name: "Paste a sample message" }).click();
    await pane
      .getByRole("textbox", { name: "Sample message (JSON)" })
      .fill(JSON.stringify({ body: { events: [pastedEvent()] } }));
    await pane.getByRole("button", { name: "Use this sample" }).click();
    await expect(pane.getByText(NOTE_ALL)).toBeVisible();
    const json = pane.getByLabel("Test event", { exact: true });
    await expect(json).toContainText("REF-4410");
    expect(sent.some((body) => body.includes("REF-4410"))).toBe(true);
    const drawn = await page.content();
    for (const id of Object.values(REAL)) {
      expect(sent.filter((body) => body.includes(id))).toEqual([]);
      expect(drawn).not.toContain(id);
    }
    await details.close();
  });
});

test.describe("a later step of a workflow a platform event starts", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("lists the event's fields with their types and maps one, without a presence warning", async ({
    page,
  }) => {
    const details = await openNotifyTeam(page, { spec: { steps: READS_KEY } });
    await generated(page, details);
    await details.close();
    await details.open("send");
    const pane = details.dialog.getByRole("region", { name: "Input data" });
    await expect(pane).toContainText("Platform event");
    const field = (path: string) =>
      pane.locator(`[data-ref="/input/transaction/${path}"]`);
    await expect(field("business_key")).toContainText("Text or empty");
    await expect(field("business_key")).toContainText('"REF-1029"');
    await expect(field("business_key")).not.toContainText("Optional");
    await expect(field("incident_id")).toContainText("Optional");
    await expect(field("cause/code")).toContainText("WV-TXN-UNDO-STUCK");
    // What the step maps reads as the event's field.
    await expect(
      details.dialog.getByRole("button", {
        name: "reference Platform event transaction business_key",
      }),
    ).toBeVisible();
    await details.close();
    await expect(page.getByText("Checked locally").first()).toBeVisible();
    await expect(page.getByText(/may be missing|presence/i)).toHaveCount(0);
  });

  test("says when the event's fields couldn't be read, and asks again only after the pause", async ({
    page,
  }) => {
    let asked = 0;
    const details = await openNotifyTeam(page, {
      view: async (route) => {
        asked++;
        await route.fulfill({
          status: 503,
          json: { code: "WV-STUDIO-UPSTREAM", message: "Try again later." },
        });
      },
    });
    const dialog = await openEventTrigger(page, details);
    const pane = await testEvent(page, dialog);
    const problem = pane.locator(".es-callout").filter({
      hasText: "Studio couldn't read the platform event's fields.",
    });
    await expect(problem).toContainText(
      "Later steps can't list them until it does.",
    );
    const again = problem.getByRole("button", { name: /^Try again/ });
    await expect(again).toHaveText(/Try again in [1-5] s/);
    await expect(again).toBeDisabled();
    // The pause is 5 s, the next one 10: whatever draws the pane meanwhile asks nothing.
    await pauseClock(page);
    await page.clock.runFor(2_000);
    expect(asked).toBe(1);
    await page.clock.runFor(4_000);
    await expect.poll(() => asked).toBe(2);
    await page.clock.resume();
    await expect(again).toHaveText(/Try again in (?:10|9|8|7) s/);
    // A sample doesn't need the fields.
    await pane.getByRole("button", { name: "Generate sample" }).click();
    await expect(
      pane.getByText("Sample ready. Execute workflow starts from it."),
    ).toBeVisible();
  });

  test("builds and takes no event that the workflow's input refuses", async ({
    page,
  }) => {
    let answer: () => void = () => undefined;
    const read = new Promise<void>((done) => (answer = done));
    const details = await openNotifyTeam(page, {
      spec: { inputSchema: FORM },
      check: () => read,
    });
    const dialog = await openEventTrigger(page, details);
    const pane = await testEvent(page, dialog);
    const generate = pane.getByRole("button", { name: "Generate sample" });
    const paste = pane.getByRole("button", { name: "Paste a sample event" });
    // Nothing has compared them yet.
    await expect(pane).toContainText(
      "Not checked yet. Select Validate to see whether this workflow's input takes the platform event.",
    );
    await expect(generate).toBeDisabled();
    answer();
    const refused =
      "This workflow's input doesn't take the platform event. Reset the input in Parameters to test with one.";
    await expect(pane).toContainText(refused);
    await expect(generate).toBeDisabled();
    await expect(generate).toHaveAccessibleDescription(refused);
    await expect(paste).toBeDisabled();
    const looks = () =>
      generate.evaluate((button) => {
        const { color, backgroundColor } = getComputedStyle(button);
        return { color, backgroundColor };
      });
    const off = await looks();
    await dialog.getByRole("button", { name: "Reset the input" }).click();
    await expect(pane).not.toContainText(refused);
    await expect(generate).toBeEnabled();
    expect(await looks()).not.toEqual(off);
    await generate.click();
    await expect(
      pane.getByText("Sample ready. Execute workflow starts from it."),
    ).toBeVisible();
  });

  test("offers no event to build while none is chosen", async ({ page }) => {
    const details = await openNotifyTeam(page, {
      spec: { eventTrigger: { events: [] } },
    });
    const dialog = await openEventTrigger(page, details);
    const pane = await testEvent(page, dialog);
    await expect(
      pane.getByRole("button", { name: "Generate sample" }),
    ).toBeDisabled();
    await expect(pane).toContainText("Choose at least one event first.");
    await dialog
      .getByRole("checkbox", { name: "A transaction completed" })
      .check();
    await expect(
      pane.getByRole("button", { name: "Generate sample" }),
    ).toBeEnabled();
    expect(ATTENTION.events).toHaveLength(1);
  });
});
