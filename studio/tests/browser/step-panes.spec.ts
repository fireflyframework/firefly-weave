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
import { expect, test, type Page } from "@playwright/test";
import { parse, stringify } from "yaml";
import { readFileSync, mkdirSync } from "node:fs";
import {
  openStepFixture,
  stepFixture,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";

async function show(
  details: StepDetailsPage,
  name: "Input" | "Output" | "Test event",
) {
  const segments = details.dialog.getByRole("tablist", {
    name: "Step details panes",
  });
  const tabs = details.dialog.getByRole("tablist", {
    name: "Data",
    exact: true,
  });
  if (await segments.isVisible())
    await segments.getByRole("tab", { name, exact: true }).click();
  else if (await tabs.isVisible())
    await tabs.getByRole("tab", { name, exact: true }).click();
}
async function trigger(details: StepDetailsPage) {
  await details.trigger().focus();
  await details.page.keyboard.press("Enter");
  await expect(details.dialog).toBeVisible();
  await show(details, "Test event");
}
function workflow(schema?: unknown) {
  const value = parse(readFileSync(stepFixture, "utf8"));
  if (schema !== undefined) value.spec.inputSchema = schema;
  return value;
}
const errors = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const found: string[] = [];
  errors.set(page, found);
  page.on("pageerror", (error) => found.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") found.push(message.text());
  });
});
test.afterEach(({ page }) => expect(errors.get(page)).toEqual([]));

for (const size of [
  { tag: "desktop", width: 1440, height: 900 },
  { tag: "narrow", width: 600, height: 500 },
  { tag: "small", width: 360, height: 740 },
]) {
  test.describe(size.tag, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("predecessor schemas, radio keys and visible pane search", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("lookup");
      await show(details, "Input");
      const input = details.dialog.locator(".sd-input");
      const source = input.getByLabel("Input source", { exact: true });
      await expect(source).toHaveValue("step:check-customer");
      await source.selectOption("input");
      await expect(
        input.getByRole("listitem", { name: /^Customer ID, Text/ }),
      ).toBeVisible();
      const schema = input.getByRole("radio", { name: "Schema", exact: true });
      await schema.focus();
      await page.keyboard.press("ArrowRight");
      await expect(
        input.getByRole("radio", { name: "Table", exact: true }),
      ).toBeFocused();
      await expect(input).toContainText("No input data yet");
      await page.keyboard.press("Home");
      await expect(schema).toBeFocused();
      await page.keyboard.press("/");
      const search = input.getByRole("textbox", {
        name: "Search fields",
        exact: true,
      });
      await expect(search).toBeFocused();
      await search.fill("Customer");
      await expect(input).toContainText("1 of 5 fields");
      await search.press("Escape");
      await expect(search).toHaveCount(0);
      await show(details, "Output");
      const output = details.dialog.locator(".sd-output");
      await output.getByRole("radio", { name: "Schema", exact: true }).focus();
      await page.keyboard.press("/");
      await expect(
        output.getByRole("textbox", { name: "Search output", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Escape");
      await page.keyboard.press("/");
      await expect(
        output.getByRole("textbox", { name: "Search output", exact: true }),
      ).toBeFocused();
    });
    test("sample typing, defaults, partial drafts and reopen stay in memory", async ({
      page,
    }) => {
      const schema = {
        type: "object",
        required: ["enabled"],
        properties: {
          name: { type: "string", title: "Name" },
          amount: { type: "number", title: "Amount" },
          enabled: { type: "boolean", title: "Enabled" },
          token: { type: "string", writeOnly: true, default: "DO-NOT-SHOW" },
        },
      };
      const details = await openStepFixture(
        page,
        yamlFile("sample.yaml", stringify(workflow(schema))),
      );
      await trigger(details);
      const input = details.dialog.locator(".sd-input");
      const name = input.getByRole("textbox", { name: /^Name/ });
      await name.pressSequentially("Alice");
      await expect(name).toHaveValue("Alice");
      const amount = input
        .getByRole("spinbutton", { name: /^Amount/ })
        .or(input.getByRole("textbox", { name: /^Amount/ }));
      await amount.pressSequentially("-");
      expect(
        await amount.evaluate(
          (el) => (el as HTMLInputElement).validity.badInput,
        ),
      ).toBe(true);
      await name.fill("Alicia");
      expect(
        await amount.evaluate(
          (el) => (el as HTMLInputElement).validity.badInput,
        ),
      ).toBe(true);
      await show(details, "Output");
      const output = details.dialog.locator(".sd-output");
      await output.getByRole("radio", { name: "JSON", exact: true }).click();
      await expect(output).toContainText('"name": "Alicia"');
      await expect(output).toContainText('"enabled": false');
      await expect(details.dialog).not.toContainText("DO-NOT-SHOW");
      await details.close();
      await trigger(details);
      await expect(
        details.dialog
          .locator(".sd-input")
          .getByRole("textbox", { name: /^Name/ }),
      ).toHaveValue("Alicia");
      await details.dialog
        .getByRole("button", { name: "Generate sample", exact: true })
        .click();
      await show(details, "Output");
      await output.getByRole("radio", { name: "Table", exact: true }).click();
      await expect(output).toContainText(
        "Table view isn't available for this sample yet.",
      );
      await output
        .getByRole("button", { name: "View JSON", exact: true })
        .click();
      await expect(output.locator("pre")).toContainText('"name": "text"');
      await expect(output.locator("pre")).not.toContainText("token");
      await expect
        .poll(() => page.evaluate(() => Object.values(localStorage).join("")))
        .not.toContain("Alicia");
      mkdirSync("../build/editor-m3/task-14", { recursive: true });
      await details.dialog.screenshot({
        path: `../build/editor-m3/task-14/sample-${size.tag}.png`,
      });
      await expect(details.dialog).toBeVisible();
      const bounds = await output.locator(".sd-pane-body").evaluate((el) => ({
        width: el.clientWidth,
        scroll: el.scrollWidth,
      }));
      expect(bounds.scroll).toBeLessThanOrEqual(bounds.width + 1);
    });
  });
}

test("null is a present sample; impossible generation is explained", async ({
  page,
}) => {
  const details = await openStepFixture(
    page,
    yamlFile("null.yaml", stringify(workflow({ type: "null" }))),
  );
  await trigger(details);
  await details.dialog
    .getByRole("button", { name: "Generate sample", exact: true })
    .click();
  const output = details.dialog.locator(".sd-output");
  await expect(
    output.getByRole("radio", { name: "JSON", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(output.locator("pre")).toHaveText("null");
  await expect(output).toContainText("Sample");
  await details.close();
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await page
    .getByLabel("Choose a workflow file")
    .setInputFiles(yamlFile("impossible.yaml", stringify(workflow(false))));
  await trigger(details);
  await details.dialog
    .getByRole("button", { name: "Generate sample", exact: true })
    .click();
  await expect(
    details.dialog.locator(".sd-input").getByRole("alert"),
  ).toContainText("sample");
  await expect(output).toContainText("No test event yet");
});

for (const size of [
  { tag: "desktop", width: 1440, height: 900 },
  { tag: "two", width: 900, height: 700 },
  { tag: "small", width: 360, height: 740 },
  { tag: "200-percent-reflow", width: 640, height: 360 },
])
  test.describe(`data control geometry ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("visible controls have44px targets, fit and receive real hits", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await trigger(details);
      for (const name of ["Test event", "Output", "Input"] as const) {
        if (name === "Input") {
          await details.close();
          await details.openWithKeyboard("lookup");
        }
        await show(details, name);
        const pane = details.dialog.locator(
          name === "Output" ? ".sd-output" : ".sd-input",
        );
        for (const control of await pane
          .locator(
            'button, input:not([type="checkbox"]), select, textarea, label.checkbox-field, .data-row[tabindex]',
          )
          .all()) {
          if (!(await control.isVisible())) continue;
          await control.scrollIntoViewIfNeeded();
          const geometry = await control.evaluate((el) => {
            const bounds = el.getBoundingClientRect();
            return {
              name: el.getAttribute("aria-label") ?? el.textContent,
              tag: el.tagName,
              width: bounds.width,
              height: bounds.height,
              left: bounds.left,
              right: bounds.right,
              hit: el.contains(
                document.elementFromPoint(
                  bounds.x + bounds.width / 2,
                  bounds.y + bounds.height / 2,
                ),
              ),
            };
          });
          expect(
            geometry.width,
            JSON.stringify(geometry),
          ).toBeGreaterThanOrEqual(44);
          expect(
            geometry.height,
            JSON.stringify(geometry),
          ).toBeGreaterThanOrEqual(44);
          expect(geometry.left).toBeGreaterThanOrEqual(0);
          expect(geometry.right).toBeLessThanOrEqual(size.width);
          expect(geometry.hit).toBe(true);
        }
        const body = pane.locator(".sd-pane-body");
        expect(
          await body.evaluate((el) => el.scrollWidth <= el.clientWidth + 1),
        ).toBe(true);
        await details.dialog.screenshot({
          path: `../build/editor-m3/task-14/geometry-${size.tag}-${name.replace(" ", "-")}.png`,
        });
      }
    });
  });

test("partial JSON remains typed through another field edit", async ({
  page,
}) => {
  const schema = {
    type: "object",
    properties: {
      name: { type: "string", title: "Name" },
      payload: {
        title: "Payload",
        oneOf: [{ type: "string" }, { type: "object" }],
      },
    },
  };
  const details = await openStepFixture(
    page,
    yamlFile("json-draft.yaml", stringify(workflow(schema))),
  );
  await trigger(details);
  const input = details.dialog.locator(".sd-input");
  const payload = input.getByRole("textbox", { name: /^Payload/ });
  await payload.fill('{"partial":');
  await input.getByRole("textbox", { name: /^Name/ }).fill("Still typing");
  await expect(payload).toHaveValue('{"partial":');
  await expect(payload).toHaveAttribute("aria-invalid", "true");
});

test("object-list samples default to Table and keep an explicit JSON choice", async ({
  page,
}) => {
  const details = await openStepFixture(
    page,
    yamlFile(
      "list.yaml",
      stringify(
        workflow({
          type: "array",
          items: { type: "object", properties: { name: { type: "string" } } },
        }),
      ),
    ),
  );
  await trigger(details);
  await details.dialog
    .getByRole("button", { name: "Generate sample", exact: true })
    .click();
  const output = details.dialog.locator(".sd-output");
  await expect(
    output.getByRole("radio", { name: "Table", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(output.locator("pre")).toHaveCount(0);
  await output.getByRole("button", { name: "View JSON", exact: true }).click();
  await expect(output.locator("pre")).toContainText('"name": "text"');
  await details.close();
  await trigger(details);
  await expect(
    output.getByRole("radio", { name: "JSON", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
});

test("F6 enters the checked data view instead of an inactive radio", async ({
  page,
}) => {
  const details = await openStepFixture(page);
  await details.openWithKeyboard("lookup");
  const json = details.dialog
    .locator(".sd-output")
    .getByRole("radio", { name: "JSON", exact: true });
  await json.click();
  await details.dialog.getByRole("button", { name: "Rename lookup" }).focus();
  await page.keyboard.press("Shift+F6");
  await expect(json).toBeFocused();
});

test.describe("sample boundary regressions", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  async function openSchema(page: Page, schema: unknown) {
    const details = await openStepFixture(
      page,
      yamlFile("sample-boundary.yaml", stringify(workflow(schema))),
    );
    await trigger(details);
    return details;
  }
  async function json(details: StepDetailsPage) {
    const output = details.dialog.locator(".sd-output");
    await output.getByRole("radio", { name: "JSON", exact: true }).click();
    return output;
  }
  async function capture(details: StepDetailsPage, name: string) {
    const folder = "../build/editor-m3/task-14-fix1/screenshots";
    mkdirSync(folder, { recursive: true });
    await details.dialog.screenshot({ path: `${folder}/${name}.png` });
  }
  test("generated additional-property secrets stay outside Output", async ({
    page,
  }) => {
    const details = await openSchema(page, {
      type: "object",
      additionalProperties: { type: "string", writeOnly: true },
      default: JSON.parse(
        '{"constructor":"SAMPLE-PRIVATE","toString":"SAMPLE-PRIVATE","__proto__":"SAMPLE-PRIVATE"}',
      ),
    });
    await details.dialog
      .getByRole("button", { name: "Generate sample", exact: true })
      .click();
    const output = await json(details);
    await capture(details, "additional-properties");
    await expect(output).not.toContainText("SAMPLE-PRIVATE");
    await expect(output.locator("pre")).toHaveText("{}");
    await details.close();
    await trigger(details);
    await expect(output.locator("pre")).toHaveText("{}");
  });
  test("manual tuple secrets withhold the array without moving later items", async ({
    page,
  }) => {
    const details = await openSchema(page, {
      type: "array",
      prefixItems: [{ type: "string", writeOnly: true }],
      items: { type: "string" },
    });
    await details.dialog
      .locator(".sd-input")
      .getByRole("textbox", { name: /Value/ })
      .fill('["SAMPLE-PRIVATE","public"]');
    const output = await json(details);
    await capture(details, "tuple");
    await expect(output).not.toContainText("SAMPLE-PRIVATE");
    await expect(output.locator("pre")).toHaveCount(0);
    await details.close();
    await trigger(details);
    await expect(output.locator("pre")).toHaveCount(0);
  });
  test("scalar reference keeps the ordinary text field and edits across reopen", async ({
    page,
  }) => {
    const details = await openSchema(page, {
      $defs: { Value: { type: "string", title: "Display name" } },
      $ref: "#/$defs/Value",
    });
    const input = details.dialog.locator(".sd-input");
    await expect(input.locator("textarea")).toHaveCount(0);
    const field = input.getByRole("textbox", { name: /Display name/ });
    await field.pressSequentially("Alice");
    const output = await json(details);
    await expect(output.locator("pre")).toHaveText('"Alice"');
    await details.close();
    await trigger(details);
    await expect(field).toHaveValue("Alice");
    await field.press("End");
    await field.pressSequentially(" Smith");
    await expect(output.locator("pre")).toHaveText('"Alice Smith"');
    await capture(details, "scalar-reference");
  });
  test("array item reference keeps text items and edits across reopen", async ({
    page,
  }) => {
    const details = await openSchema(page, {
      $defs: { Value: { type: "string" } },
      type: "array",
      items: { $ref: "#/$defs/Value" },
    });
    const input = details.dialog.locator(".sd-input");
    await expect(input.locator("textarea")).toHaveCount(0);
    await input.getByRole("button", { name: /Add/ }).click();
    const field = input.getByRole("textbox");
    await field.fill("first");
    const output = await json(details);
    await expect(output.locator("pre")).toContainText('"first"');
    await details.close();
    await trigger(details);
    await expect(field).toHaveValue("first");
    await field.fill("updated");
    await expect(output.locator("pre")).toContainText('"updated"');
    await capture(details, "array-reference");
  });
  test("reference-hidden composition is refused without a generated output", async ({
    page,
  }) => {
    const details = await openSchema(page, {
      $defs: {
        Pair: {
          allOf: [
            {
              type: "object",
              properties: { a: { type: "string" } },
              additionalProperties: false,
            },
            { properties: { b: { type: "string" } } },
          ],
        },
      },
      $ref: "#/$defs/Pair",
    });
    await details.dialog
      .getByRole("button", { name: "Generate sample", exact: true })
      .click();
    const output = await json(details);
    await expect(
      details.dialog.locator(".sd-input").getByRole("alert"),
    ).toContainText("couldn't generate a sample");
    await expect(output.locator("pre")).toHaveCount(0);
    await capture(details, "composition-refusal");
  });
  test("nullable default generates an explicit null sample", async ({
    page,
  }) => {
    const details = await openSchema(page, {
      type: ["string", "null"],
      default: null,
    });
    await details.dialog
      .getByRole("button", { name: "Generate sample", exact: true })
      .click();
    const output = await json(details);
    await expect(output.locator("pre")).toHaveText("null");
    await expect(
      details.dialog.locator(".sd-input").getByRole("alert"),
    ).toHaveCount(0);
  });
});
