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
// The AI task in step details: a model chosen connection first, a prompt,
// options behind Add option and the timeout on Settings. The platform is
// mocked; real model calls are covered by the AI journeys.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import { openStepFixture, sizes, yamlFile } from "./step-details-po";
import {
  box,
  boxes,
  clippedText,
  contentEdges,
  density,
  expectAligned,
  intersects,
  showParameters,
  type Box,
} from "./step-geometry";
import { focusStop } from "./step-focus";
import { connected, expectHitTarget, sourceText } from "./support";

const workflow = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: support-inbox, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {text: {type: string}}}
  outputSchema: {}
  steps:
    - id: summarize
      kind: llm
      uses: weave-agentic-generate@1.0.0
      profile: default
      connection: ai
      prompt: {literal: ""}
      context: {literal: {}}
  output: {ref: /steps/summarize/output/result}
`;
const ollama = {
  connection_id: "11111111-1111-4111-8111-111111111111",
  revision_id: "22222222-2222-4222-8222-222222222222",
  name: "ollama-local",
  connector: "weave-agentic-provider@1.0.0",
  label: "Ollama · ollama-local",
  enabled: true,
  provider: "openai-chat",
  endpoint_origin: "http://ollama:11434",
};

/** The platform with one keyless Ollama connection serving two models. */
async function platform(
  page: Page,
  requests = { models: 0 },
  connections: unknown[] = [ollama],
) {
  await connected(page);
  await page.route("**/environments/development/bindable-connections?*", (r) =>
    r.fulfill({ json: { items: connections, next_cursor: null } }),
  );
  await page.route("**/environments/development/ai/models?*", (r) => {
    requests.models++;
    return r.fulfill({
      json: {
        approval: "served",
        discovery: "ok",
        discovered_at: "2026-10-20T10:00:00Z",
        models: [
          {
            name: "qwen3:4b",
            approved: true,
            available: true,
            tools: "yes",
            context_tokens: 8192,
          },
          {
            name: "gemma3:270m",
            approved: false,
            available: true,
            tools: "no",
          },
        ],
      },
    });
  });
  return requests;
}

for (const size of sizes)
  test.describe(`AI task at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a new AI task asks for a model, then writes its settings connection first", async ({
      page,
    }) => {
      const requests = { models: 0 };
      const details = await openStepFixture(
        page,
        yamlFile("support-inbox.yaml", workflow),
        (p) => platform(p, requests),
      );
      const tile = page.locator('.tile-node[data-tile="summarize"]');
      await expect(tile).toContainText("Choose a model");
      await expect(
        tile.getByRole("button", {
          name: "Show issues for summarize: Setup needed: choose a model",
        }),
      ).toBeVisible();
      await details.open("summarize");
      await expect(details.topFields()).toHaveCount(2);
      expect(
        await details
          .topFields()
          .evaluateAll((fields) =>
            fields.map((f) => f.getAttribute("data-param")),
          ),
      ).toEqual(["model", "prompt"]);
      const model = details.field("model");
      await model.getByRole("combobox", { name: "Model" }).click();
      const list = page.getByRole("listbox", { name: "Model options" });
      await expect(list.getByText("Ollama · ollama-local")).toBeVisible();
      await expect(
        list.getByRole("option", { name: /^gemma3:270m/ }),
      ).toHaveAttribute("aria-disabled", "true");
      await list.getByRole("option", { name: /^qwen3:4b/ }).click();
      await expect(model.locator(".param-model-facts")).toHaveText(
        /Calls tools\s*8,192 tokens of context\s*Local/,
      );
      await details.tab("Settings").click();
      await expect(details.field("timeout")).toBeVisible();
      await details.tab("Parameters").click();
      await details.dialog.getByRole("button", { name: "Add option" }).click();
      await expect(
        details.dialog.getByRole("menuitem", { name: /^Structured result/ }),
      ).not.toHaveAttribute("aria-disabled", "true");
      await page.keyboard.press("Escape");
      await details.close();
      await expect(tile).toContainText("qwen3:4b");
      await expect(
        tile.getByRole("button", {
          name: /^Show issues for summarize: Setup needed/,
        }),
      ).toHaveCount(0);
      const source = parse(await sourceText(page));
      expect(source.spec.llmProfiles).toEqual({
        summarize: {
          provider: "openai-chat",
          model: "qwen3:4b",
          options: { max_tokens: 1024 },
          outputSchema: { type: "string", maxLength: 20000 },
        },
      });
      expect(source.spec.connections).toEqual({
        ai: { connector: "weave-agentic-provider@1.0.0", required: true },
      });
      expect(source.spec.steps[0]).toMatchObject({
        profile: "summarize",
        connection: "ai",
        uses: "weave-agentic-generate@1.0.0",
      });
      expect(requests.models).toBe(1);
    });

    test("a model two AI connections serve stays chosen, with what it can do", async ({
      page,
    }) => {
      const second = {
        ...ollama,
        connection_id: "33333333-3333-4333-8333-333333333333",
        revision_id: "44444444-4444-4444-8444-444444444444",
        name: "ollama-gpu",
        label: "Ollama · ollama-gpu",
      };
      const details = await openStepFixture(
        page,
        yamlFile("support-inbox.yaml", workflow),
        (p) => platform(p, { models: 0 }, [ollama, second]),
      );
      await details.open("summarize");
      const model = details.field("model");
      const combobox = model.getByRole("combobox", { name: "Model" });
      await combobox.click();
      const list = page.getByRole("listbox", { name: "Model options" });
      await expect(list.getByText("Ollama · ollama-gpu")).toBeVisible();
      await list
        .getByRole("option", { name: /^qwen3:4b/ })
        .nth(1)
        .click();
      await expect(combobox).toHaveValue("qwen3:4b");
      await expect(combobox).toHaveAttribute(
        "data-value",
        /44444444-4444-4444-8444-444444444444/,
      );
      await expect(model.locator(".param-model-facts .pill")).toHaveCount(3);
      await expect(model.locator(".param-model-notice")).toHaveCount(0);
    });

    test("Set up AI opens the AI connection sheet over step details", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        yamlFile("support-inbox.yaml", workflow),
        platform,
      );
      await details.open("summarize");
      await details
        .field("model")
        .getByRole("button", { name: "Set up AI" })
        .click();
      await expect(
        page.locator("weave-ai-connection-sheet").getByRole("dialog"),
      ).toBeVisible();
    });
  });

/** Layout is measured on a desktop, at the smallest editor and on a phone. */
const layoutSizes = [...sizes, { tag: "360x640", width: 360, height: 640 }];

/** Expects `inner` to lie within the left and right edges. */
function expectInside(
  inner: Box,
  edges: { left: number; right: number },
  what: string,
) {
  expect(inner.x, `${what} left`).toBeGreaterThanOrEqual(edges.left - 0.5);
  expect(inner.right, `${what} right`).toBeLessThanOrEqual(edges.right + 0.5);
}

for (const size of layoutSizes)
  test.describe(`AI task layout at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the Model field lines up with Prompt at the form's density", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        yamlFile("support-inbox.yaml", workflow),
        platform,
      );
      await details.open("summarize");
      await showParameters(page, details.dialog);
      const sizes = await density(page);
      const edges = await contentEdges(
        details.dialog.locator(".sd-tabpanel:not([hidden])"),
      );
      const model = details.field("model");
      const prompt = details.field("prompt");
      const labelOf = (field: typeof model) =>
        box(field.locator(":scope > .param > .param-label-row"));
      const controlOf = (field: typeof model) =>
        box(field.locator(":scope > .param > .param-control"));
      // Labels and controls start on one edge; controls are one height.
      expectAligned(
        [(await labelOf(model)).x, (await labelOf(prompt)).x],
        "label left edges",
      );
      expectAligned(
        [(await controlOf(model)).x, (await controlOf(prompt)).x],
        "control left edges",
      );
      const select = await box(model.locator(".choice-field"));
      expectAligned([select.height, sizes.control], "Model control height");
      expectAligned([select.x, (await controlOf(prompt)).x], "Model edge");
      expectInside(select, edges, "Model control");
      // A choice made in place is as wide as its content.
      if (size.width >= 600)
        expect(select.width).toBeLessThan((edges.right - edges.left) * 0.75);
      // Set up AI sits below the control, on the field's left edge.
      const setUp = model.getByRole("button", { name: "Set up AI" });
      const action = await box(setUp);
      expect(action.y).toBeGreaterThanOrEqual(select.bottom - 0.5);
      // One control height: 36 px with a mouse, 44 px for touch.
      expectAligned([action.height, sizes.control], "Set up AI height");
      expectAligned(
        [action.x, (await controlOf(model)).x],
        "Set up AI left edge",
      );
      expectInside(action, edges, "Set up AI");
      await expectHitTarget(setUp);
      // From the keyboard, the list and Set up AI each show the focus ring.
      const combobox = model.getByRole("combobox", { name: "Model" });
      await combobox.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(combobox).toBeFocused();
      expect((await focusStop(page))?.ring).toBe(true);
      await page.keyboard.press("Tab");
      await expect(setUp).toBeFocused();
      expect((await focusStop(page))?.ring).toBe(true);
      // The list: one row per model under its connection, inside the window.
      await model.getByRole("combobox", { name: "Model" }).click();
      const list = page.getByRole("listbox", { name: "Model options" });
      await expect(list.getByRole("option")).toHaveCount(2);
      const listBox = await box(list);
      const opened = await box(model.locator(".choice-field"));
      expectAligned([listBox.x, opened.x], "list and control left edges");
      expect(
        listBox.y >= opened.bottom - 0.5 || listBox.bottom <= opened.y + 0.5,
        "the list does not cover the control",
      ).toBe(true);
      expect(listBox.x).toBeGreaterThanOrEqual(-0.5);
      expect(listBox.right).toBeLessThanOrEqual(size.width + 0.5);
      expect(listBox.bottom).toBeLessThanOrEqual(size.height + 0.5);
      const rows = await boxes(list.getByRole("option"));
      // Every row is as tall as its text plus the same small padding: rows
      // never grow to a touch height for one line.
      for (const row of rows)
        expect(row.height).toBeGreaterThanOrEqual(sizes.row - 0.5);
      const paddings = await list.getByRole("option").evaluateAll((options) =>
        options.map((option) => {
          const range = document.createRange();
          range.selectNodeContents(option);
          return (
            option.getBoundingClientRect().height -
            range.getBoundingClientRect().height
          );
        }),
      );
      expectAligned(paddings, "option row padding");
      expect(paddings[0]).toBeLessThanOrEqual(16);
      expectAligned(
        [
          (await box(list.locator(".choice-group"))).x,
          ...rows.map((row) => row.x),
        ],
        "group and option edges",
      );
      expect(await clippedText(list, "")).toEqual([]);
      await list.getByRole("option", { name: /^qwen3:4b/ }).click();
      // What the model can do: tags in a row under the control, then Set up AI.
      await expect(model.locator(".param-model-facts .pill")).toHaveCount(3);
      const tags = await boxes(model.locator(".param-model-facts .pill"));
      const chosen = await box(model.locator(".choice-field"));
      expectAligned([tags[0].x, chosen.x], "first tag edge");
      for (const [index, tag] of tags.entries()) {
        expect(tag.y).toBeGreaterThanOrEqual(chosen.bottom - 0.5);
        expectInside(tag, edges, `tag ${index + 1}`);
        for (const other of tags.slice(index + 1))
          expect(intersects(tag, other)).toBe(false);
      }
      const firstRow = tags.filter((tag) => tag.y < tags[0].bottom);
      expectAligned(
        firstRow.map((tag) => tag.middle),
        "tag centers",
      );
      // Pills are one height, with the same small gap above and below them.
      expectAligned(
        tags.map((tag) => tag.height),
        "pill heights",
      );
      const after = await box(setUp);
      const pillsBottom = Math.max(...tags.map((tag) => tag.bottom));
      expectAligned(
        [tags[0].y - chosen.bottom, after.y - pillsBottom],
        "gaps around the pills",
      );
      expect(tags[0].y - chosen.bottom).toBeLessThanOrEqual(8);
      expect(after.y).toBeGreaterThanOrEqual(
        Math.max(...tags.map((tag) => tag.bottom)) - 0.5,
      );
      expectAligned([after.x, action.x], "Set up AI stays on the edge");
      expect(await clippedText(details.dialog)).toEqual([]);
      const panel = details.dialog.locator(".sd-tabpanel:not([hidden])");
      expect(
        await panel.evaluate((e) => e.scrollWidth - e.clientWidth),
      ).toBeLessThanOrEqual(0);
    });

    test("without an AI connection the note leads to Set up AI", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        yamlFile("support-inbox.yaml", workflow),
        (p) => platform(p, { models: 0 }, []),
      );
      await details.open("summarize");
      await showParameters(page, details.dialog);
      const model = details.field("model");
      const note = model.locator(".param-model-notice");
      await expect(note).toHaveText(
        "No AI connection yet. Set up AI to add one.",
      );
      const setUp = model.getByRole("button", { name: "Set up AI" });
      await expect(setUp).toBeEnabled();
      const text = await box(note);
      const action = await box(setUp);
      const select = await box(model.locator(".choice-field"));
      const edges = await contentEdges(
        details.dialog.locator(".sd-tabpanel:not([hidden])"),
      );
      expect(intersects(text, action)).toBe(false);
      expectAligned([text.x, select.x], "note edge");
      if (action.y < text.bottom) {
        // One row: the note, then Set up AI, centered on each other.
        expect(action.x).toBeGreaterThanOrEqual(text.right - 0.5);
        expectAligned([text.middle, action.middle], "note and Set up AI");
      } else expectAligned([action.x, select.x], "Set up AI edge");
      expectInside(text, edges, "note");
      expectInside(action, edges, "Set up AI");
      expect(await clippedText(details.dialog)).toEqual([]);
    });
  });
