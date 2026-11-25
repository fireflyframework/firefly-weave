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
// A transaction's step details: its result in Parameters; in Settings its
// business key, how long it holds reservations, its undo retries and when
// Weave gives up, then On error in a transaction's words. The platform is
// mocked.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import {
  openStepFixture,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";
import { drawn, offline, sourceText } from "./support";

const MOVE = yamlFile(
  "move-between-systems.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: move-between-systems, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {moveId: {type: string}}}
  outputSchema: {}
  steps:
    - id: move
      kind: transaction
      businessKey: {ref: /input/moveId}
      steps:
        - id: hold
          kind: transform
          value: {literal: {}}
        - id: add
          kind: transform
          value: {literal: {}}
      output: {literal: null}
  output: {literal: null}
`,
);
async function open(page: Page): Promise<StepDetailsPage> {
  const details = await openStepFixture(page, MOVE, async (p) => {
    await offline(p);
    await p.route("**/studio/contracts/language", (route) =>
      route.fulfill({
        json: {
          features: ["text.concat", "flow.transaction", "flow.onError"],
          step_fields: [{ name: "onError", kinds: ["transaction"] }],
        },
      }),
    );
  });
  await details.open("move");
  return details;
}
/** move as Source holds it. */
async function move(page: Page, details: StepDetailsPage) {
  await details.close();
  return (
    parse(await sourceText(page)) as {
      spec: { steps: Record<string, unknown>[] };
    }
  ).spec.steps[0];
}

test.describe("a transaction in step details at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Parameters show its result; Settings show every default in its own unit, then On error in its words", async ({
    page,
  }) => {
    const details = await open(page);
    await expect(details.dialog).toContainText("Transaction · 2 steps");
    const result = details.field("output");
    await expect(result).toContainText("Result");
    await expect(result).toContainText(
      "What later steps read as this transaction's output.",
    );
    await details.tab("Settings").click();
    await expect(
      details.dialog.locator(
        ".sd-tabpanel:not([hidden]) .settings-form > [data-param]",
      ),
    ).toHaveCount(7);
    const key = details.field("businessKey");
    await expect(key).toContainText("Optional");
    await expect(key).toContainText("moveId");
    const shown = async (id: string) => [
      await details.field(id).getByRole("textbox").getAttribute("placeholder"),
      await details.field(id).getByRole("combobox").inputValue(),
    ];
    expect(await shown("holdSeconds")).toEqual(["15", "minutes"]);
    expect(await shown("undoRetry.initialDelaySeconds")).toEqual([
      "2",
      "seconds",
    ]);
    expect(await shown("undoRetry.maxDelaySeconds")).toEqual(["5", "minutes"]);
    expect(await shown("undoTimeout")).toEqual(["1", "days"]);
    const onError = details.field("onError");
    await expect(onError).toContainText(
      "If the transaction is undone, the run fails. Nothing stays applied.",
    );
    await expect(onError).toContainText(
      "A transaction in doubt always waits for a person; On error never handles it.",
    );
    // Every setting and On error is on screen without scrolling.
    const box = (await onError.boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual(900);
    // The tries and the two waits share one row.
    const tops = await Promise.all(
      ["maxAttempts", "initialDelaySeconds", "maxDelaySeconds"].map(
        async (id) =>
          (await details
            .field(`undoRetry.${id}`)
            .getByRole("textbox")
            .boundingBox())!.y,
      ),
    );
    expect(new Set(tops.map(Math.round)).size).toBe(1);
    // One hint for the three, under them.
    const hint = (await details
      .field("undoRetry")
      .getByText("Weave retries each undo step until it succeeds")
      .boundingBox())!;
    expect(hint.y).toBeGreaterThan(tops[0]);
  });

  test("a changed value writes the whole policy; its default again takes the policy away", async ({
    page,
  }) => {
    const details = await open(page);
    await details.tab("Settings").click();
    const hold = details.field("holdSeconds").getByRole("textbox");
    await hold.fill("10");
    await expect(
      details.dialog.getByRole("tab", { name: /^Settings/ }),
    ).toBeVisible();
    expect((await move(page, details))["policy"]).toEqual({
      holdSeconds: 600,
      undoRetry: {
        maxAttempts: 10,
        initialDelaySeconds: 2,
        maxDelaySeconds: 300,
      },
      undoTimeoutSeconds: 86400,
    });
    await details.open("move");
    await details.tab("Settings").click();
    await details.field("holdSeconds").getByRole("textbox").fill("15");
    expect(await move(page, details)).not.toHaveProperty("policy");
  });

  // Typed key by key: on the way, the digits are the default (10 tries, 15
  // minutes), which a transaction leaves out of its source.
  for (const { id, typed, saved } of [
    {
      id: "undoRetry.maxAttempts",
      typed: "105",
      saved: { undoRetry: { maxAttempts: 105 } },
    },
    {
      id: "undoRetry.maxAttempts",
      typed: "100",
      saved: { undoRetry: { maxAttempts: 100 } },
    },
    { id: "holdSeconds", typed: "152", saved: { holdSeconds: 9120 } },
    { id: "holdSeconds", typed: "150", saved: { holdSeconds: 9000 } },
    // 60 minutes on the way reads as 1 hour only once the box is left.
    { id: "holdSeconds", typed: "605", saved: { holdSeconds: 36300 } },
  ])
    test(`typing ${typed} in ${id} goes past its default`, async ({ page }) => {
      const details = await open(page);
      await details.tab("Settings").click();
      const box = details.field(id).getByRole("textbox");
      await box.click();
      for (const [index, key] of [...typed].entries()) {
        await page.keyboard.press(key);
        await drawn(page);
        expect(await box.inputValue()).toBe(typed.slice(0, index + 1));
      }
      await expect(details.field(id).getByRole("alert")).toHaveCount(0);
      expect((await move(page, details))["policy"]).toMatchObject(saved);
    });

  test("a typed default shows as the default once the box is left", async ({
    page,
  }) => {
    const details = await open(page);
    await details.tab("Settings").click();
    const tries = details.field("undoRetry.maxAttempts").getByRole("textbox");
    await tries.click();
    await page.keyboard.type("10");
    await expect(tries).toHaveValue("10");
    await page.keyboard.press("Tab");
    await expect(tries).toHaveValue("");
    await expect(tries).toHaveAttribute("placeholder", "10");
    // A duration typed in seconds keeps its unit until the box is left.
    const hold = details.field("holdSeconds");
    await hold.getByRole("combobox").selectOption("seconds");
    await hold.getByRole("textbox").click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type("900");
    await expect(hold.getByRole("textbox")).toHaveValue("900");
    await expect(hold.getByRole("combobox")).toHaveValue("seconds");
    await page.keyboard.type("0");
    await expect(hold.getByRole("textbox")).toHaveValue("9000");
    await hold.getByRole("textbox").blur();
    await expect(hold.getByRole("textbox")).toHaveValue("150");
    await expect(hold.getByRole("combobox")).toHaveValue("minutes");
    expect((await move(page, details))["policy"]).toMatchObject({
      holdSeconds: 9000,
      undoRetry: { maxAttempts: 10 },
    });
  });

  test("Undo while typing shows the value as it was", async ({ page }) => {
    const details = await open(page);
    await details.tab("Settings").click();
    const tries = details.field("undoRetry.maxAttempts").getByRole("textbox");
    await tries.click();
    await page.keyboard.type("25");
    await drawn(page);
    await expect(tries).toHaveValue("25");
    await page.keyboard.press("ControlOrMeta+z");
    await expect(tries).toHaveValue("");
    expect(await move(page, details)).not.toHaveProperty("policy");
  });

  test("out of range says what it takes in its units", async ({ page }) => {
    const details = await open(page);
    await details.tab("Settings").click();
    const giveUp = details.field("undoTimeout");
    await giveUp.getByRole("textbox").fill("8");
    await giveUp.getByRole("textbox").press("Tab");
    await expect(giveUp.getByRole("alert")).toContainText(
      "Use 1 minute to 7 days.",
    );
    await expect(giveUp.getByRole("textbox")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });
});

test.describe("a transaction in step details at 360x640", () => {
  test.use({ viewport: { width: 360, height: 640 }, hasTouch: true });

  test("Settings stay in one column, nothing wider than the phone", async ({
    page,
  }) => {
    const details = await open(page);
    await details.tab("Settings").click();
    const form = details.dialog.locator(
      ".sd-tabpanel:not([hidden]) .settings-form",
    );
    const wide = await form.evaluate(
      (element) => element.scrollWidth - element.clientWidth,
    );
    expect(wide).toBe(0);
    const boxes = await Promise.all(
      ["maxAttempts", "initialDelaySeconds", "maxDelaySeconds"].map(
        async (id) =>
          (await details
            .field(`undoRetry.${id}`)
            .getByRole("textbox")
            .boundingBox())!,
      ),
    );
    expect(boxes[1].y).toBeGreaterThan(boxes[0].y + boxes[0].height);
    expect(boxes[2].y).toBeGreaterThan(boxes[1].y + boxes[1].height);
    for (const box of boxes) expect(box.height).toBeGreaterThanOrEqual(44);
    // Every duration fills the column the tries do.
    const widths = await Promise.all(
      ["holdSeconds", "undoRetry.maxDelaySeconds", "undoTimeout"].map(
        async (id) =>
          (await details.field(id).locator(".param-duration").boundingBox())!
            .width,
      ),
    );
    expect(new Set(widths.map(Math.round))).toEqual(
      new Set([Math.round(boxes[0].width)]),
    );
  });
});
