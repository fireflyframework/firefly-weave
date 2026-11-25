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
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse } from "yaml";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
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
import { PRODUCTION, openConnected } from "./connections-po";
import { executeButton, executions } from "./execute-po";
import { localAuthoring } from "./local-authoring";
import {
  RUN_ID,
  accepted,
  executeIn,
  routeStepTests,
  sent,
  showOutput,
  succeeded,
  view,
} from "./step-tests-po";

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
      // The setup list says the same as the card.
      const setupList = page.getByRole("list", { name: "Setup needed" });
      await expect(setupList.getByRole("button")).toHaveText([
        "summarize · Choose a model",
      ]);
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
      await expect(setupList.getByRole("button")).toHaveCount(0);
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

/** An AI task with its model chosen, a prompt and a context: ready to execute. */
const chosen = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: support-inbox, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {text: {type: string}}}
  outputSchema: {}
  connections:
    ai: {connector: weave-agentic-provider@1.0.0, required: true}
  llmProfiles:
    summarize:
      provider: openai-chat
      model: "qwen3:4b"
      options: {max_tokens: 1024}
      outputSchema: {type: string, maxLength: 20000}
  steps:
    - id: summarize
      kind: llm
      uses: weave-agentic-generate@1.0.0
      profile: summarize
      connection: ai
      prompt: {literal: "Summarize the ticket in two sentences."}
      context: {literal: {ticket: "T-1"}}
  output: {ref: /steps/summarize/output/result}
`;
const cloud = {
  ...ollama,
  connection_id: "77777777-7777-4777-8777-777777777777",
  revision_id: "88888888-8888-4888-8888-888888888888",
  name: "openai-prod",
  label: "OpenAI · openai-prod",
  endpoint_origin: "https://api.openai.com",
};
const usage = "1 request · 812 in · 64 out tokens";
const replayed = "No model call · replayed the saved answer";
const sampleAnswer = "Sample answer from summarize.";
const keptTitle = "Kept as test data. Execute step replays this answer.";
const answer = {
  result: "The customer can't sign in. Support reset the password.",
  usage: { requests: 1, inputTokens: 812, outputTokens: 64 },
  provider: "openai-chat",
  model: "qwen3:4b",
};
/** The Output pane's button that keeps the answer shown as test data. */
const keep = (pane: Locator) =>
  pane.getByRole("button", { name: "Keep as test data" });

/** Studio connected to Production with one AI connection, the step's model chosen there. */
async function inProduction(
  page: Page,
  connection: typeof ollama,
  output: typeof answer = answer,
  /** False: the model is chosen elsewhere, so this computer has no binding for its slot. */
  choose = true,
) {
  const opened = await openConnected(
    page,
    { bindable: { [PRODUCTION]: [connection] } },
    yamlFile("support-inbox.yaml", chosen),
  );
  await page.route("**/environments/*/ai/models?*", (r) =>
    r.fulfill({
      json: {
        approval: "served",
        discovery: "ok",
        discovered_at: "2026-11-19T10:00:00Z",
        models: [
          {
            name: "qwen3:4b",
            approved: true,
            available: true,
            tools: "yes",
            context_tokens: 8192,
          },
        ],
      },
    }),
  );
  const steps = await routeStepTests(page, {
    starts: [
      { status: 202, json: { ...accepted, side_effect: "non_idempotent" } },
    ],
    views: [
      view({ side_effect: "non_idempotent" }),
      succeeded(output, { side_effect: "non_idempotent" }),
    ],
  });
  const { details } = opened;
  await details.open("summarize");
  if (!choose) return { details, steps };
  await showParameters(page, details.dialog);
  const model = details.field("model");
  await model.getByRole("combobox", { name: "Model" }).click();
  await page
    .getByRole("listbox", { name: "Model options" })
    .getByRole("option", { name: /^qwen3:4b/ })
    .click();
  return { details, steps };
}

for (const size of sizes)
  test.describe(`AI task executed at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Generate sample saves an answer that Execute step replays without a model", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        yamlFile("support-inbox.yaml", chosen),
        async (p) => {
          await platform(p);
          await withLanguageFeatures(p);
        },
      );
      const calls = await localAuthoring(page);
      await details.open("summarize");
      const pane = await showOutput(details);
      await pane.getByRole("button", { name: "Generate sample" }).click();
      // Said aloud as an answer kept, not a pinned output.
      await expect(
        details.dialog.locator('p.sr-only[aria-live="polite"]').first(),
      ).toHaveText("Kept a sample answer as test data for summarize.");
      // The sample shows at once as what Execute step returns: no model call.
      await expect(pane.locator(".pane-usage")).toHaveText(replayed);
      await expect(pane.locator(".pane-answer-text")).toHaveText(sampleAnswer);
      await expect(pane.locator(".pinned-banner")).toHaveCount(0);
      // It is the answer kept as test data.
      await expect(keep(pane)).toHaveAttribute("aria-pressed", "true");
      await expect(keep(pane)).toHaveAttribute("title", keptTitle);
      const answered = page.waitForResponse("**/studio/local/debug/execute");
      const run = executeButton(details.dialog);
      if (!(await run.isVisible())) await showParameters(page, details.dialog);
      await run.click();
      expect((await answered).status()).toBe(200);
      const sentTurns = executions(calls).at(-1)!.body as {
        scripts: { aiTurns: Record<string, unknown> };
      };
      expect(sentTurns.scripts.aiTurns).toEqual({
        summarize: { turns: [{ final: sampleAnswer }], source: "simulated" },
      });
      const shown = await showOutput(details);
      await expect(shown.locator(".pane-source-tag")).toHaveText("Simulated");
      await expect(shown.locator(".pane-usage")).toHaveText(replayed);
      await expect(shown.locator(".pane-answer-text")).toHaveText(sampleAnswer);
    });

    test("Execute in Production calls a local model at once and keeps its answer as test data", async ({
      page,
    }) => {
      const { details, steps } = await inProduction(page, ollama);
      await (await executeIn(details, "Production")).click();
      await sent(steps, 1);
      expect(steps.posts[0].postDataJSON()).toMatchObject({
        kind: "llm",
        step_id: "summarize",
        uses: "weave-agentic-generate@1.0.0",
        profile: "summarize",
        input: {
          prompt: "Summarize the ticket in two sentences.",
          context: { ticket: "T-1" },
        },
        connections: { ai: ollama.revision_id },
        timeout_seconds: 120,
        acknowledged_side_effect: "non_idempotent",
      });
      await expect(page.getByRole("alertdialog")).toHaveCount(0);
      const pane = await showOutput(details);
      await expect(pane).toContainText(
        `From Production · run ${RUN_ID.slice(0, 8)}`,
      );
      await expect(pane.locator(".pane-usage")).toHaveText(usage);
      // The answer reads in full, wrapped, above its fields.
      await expect(pane.locator(".pane-answer-text")).toHaveText(answer.result);
      await expect(pane.getByRole("button", { name: "Show all" })).toHaveCount(
        0,
      );
      // The usage takes a row of its own: the run's source keeps its words,
      // and the keep, Edit output and More output actions buttons end the
      // source's row inside the pane. The count may take the line under it.
      const source = pane.locator(".pane-real-text");
      expect(
        await source.evaluate((e) => e.scrollWidth - e.clientWidth),
      ).toBeLessThanOrEqual(1);
      const edges = await box(pane);
      const tag = await box(pane.locator(".pane-real-tag"));
      for (const name of [
        "Keep as test data",
        "Edit output",
        "More output actions",
      ]) {
        const control = await box(pane.getByRole("button", { name }));
        expect(control.right, name).toBeLessThanOrEqual(edges.right + 0.5);
        expect(
          Math.abs(control.y + control.height / 2 - (tag.y + tag.height / 2)),
          name,
        ).toBeLessThan(1);
      }
      const count = await box(pane.locator(".pane-count"));
      expect(count.right).toBeLessThanOrEqual(edges.right + 0.5);
      // Keeping the real answer makes it the turn Execute step replays, and
      // the button says so for as long as it holds.
      await expect(keep(pane)).toHaveAttribute("aria-pressed", "false");
      await keep(pane).click();
      await expect(
        page.getByText(
          "Kept this answer as test data for summarize. Execute step replays it.",
        ),
      ).toBeVisible();
      await expect(keep(pane)).toHaveAttribute("aria-pressed", "true");
      await expect(keep(pane)).toHaveAttribute("title", keptTitle);
      // Execute step now replays it, with the run it came from.
      const calls = await localAuthoring(page);
      const run = executeButton(details.dialog);
      if (!(await run.isVisible())) await showParameters(page, details.dialog);
      await run.click();
      await expect.poll(() => executions(calls).length).toBe(1);
      const kept = executions(calls)[0].body as {
        scripts: { aiTurns: Record<string, unknown> };
      };
      expect(kept.scripts.aiTurns).toEqual({
        summarize: {
          turns: [{ final: answer.result }],
          source: "test-call",
          runId: RUN_ID,
          runEnvironmentId: PRODUCTION,
        },
      });
      // A second click removes it, with Undo.
      const after = await showOutput(details);
      await expect(keep(after)).toHaveAttribute("aria-pressed", "true");
      await keep(after).click();
      const removed = page
        .locator(".toast")
        .filter({ hasText: "Removed the kept answer for summarize." });
      await expect(removed).toBeVisible();
      await expect(removed.getByRole("button", { name: "Undo" })).toBeVisible();
      await expect(keep(after)).toHaveAttribute("aria-pressed", "false");
    });

    test("without the AI connection chosen here, Fix opens the Model field", async ({
      page,
    }) => {
      const { details, steps } = await inProduction(
        page,
        ollama,
        answer,
        false,
      );
      await (await executeIn(details, "Production")).click();
      const pane = await showOutput(details);
      await expect(pane).toContainText(
        "Choose a connection for slot ai to execute summarize in Production.",
      );
      expect(steps.posts).toHaveLength(0);
      await pane.getByRole("button", { name: "Fix" }).click();
      await expect(
        page.getByRole("listbox", { name: "Model options" }),
      ).toBeVisible();
    });

    test("a long answer shows eight lines, then all of it on Show all", async ({
      page,
    }) => {
      const long = Array.from(
        { length: 30 },
        (_, n) => `Line ${n + 1} of the answer.`,
      ).join("\n");
      const { details } = await inProduction(page, ollama, {
        ...answer,
        result: long,
      });
      await (await executeIn(details, "Production")).click();
      const pane = await showOutput(details);
      const text = pane.locator(".pane-answer-text");
      await expect(text).toContainText("Line 1 of the answer.");
      const lines = async () =>
        text.evaluate((e) =>
          Math.round(
            e.clientHeight / parseFloat(getComputedStyle(e).lineHeight),
          ),
        );
      expect(await lines()).toBe(8);
      const all = pane.getByRole("button", { name: "Show all" });
      await expect(all).toHaveAttribute("aria-expanded", "false");
      await all.click();
      await expect(
        pane.getByRole("button", { name: "Show less" }),
      ).toHaveAttribute("aria-expanded", "true");
      expect(await lines()).toBeGreaterThanOrEqual(30);
    });

    test("a connection that may bill asks first, naming the model and the cost", async ({
      page,
    }) => {
      const { details, steps } = await inProduction(page, cloud);
      await (await executeIn(details, "Production")).click();
      const question = page.getByRole("alertdialog", {
        name: "Execute summarize in Production?",
      });
      await expect(question).toContainText(
        "This calls qwen3:4b on OpenAI (openai-prod) and may incur cost.",
      );
      await expect(question).not.toContainText("changes data");
      expect(steps.posts).toHaveLength(0);
      await question.getByRole("button", { name: "Run it" }).click();
      await sent(steps, 1);
      expect(steps.posts[0].postDataJSON()).toMatchObject({
        connections: { ai: cloud.revision_id },
        acknowledged_side_effect: "non_idempotent",
      });
      const pane = await showOutput(details);
      await expect(pane.locator(".pane-usage")).toHaveText(usage);
    });
  });

test.describe("the cost question on a phone", () => {
  test.use({ viewport: { width: 360, height: 740 } });

  test("keeps the connection's name on one line", async ({ page }) => {
    const { details } = await inProduction(page, cloud);
    await (await executeIn(details, "Production")).click();
    const question = page.getByRole("alertdialog", {
      name: "Execute summarize in Production?",
    });
    await expect(question).toContainText(
      "This calls qwen3:4b on OpenAI (openai-prod) and may incur cost.",
    );
    // "(openai-prod)" is one line box: it never breaks at its hyphen.
    const name = question.getByText("(openai-prod)", { exact: true });
    expect(await name.evaluate((e) => e.getClientRects().length)).toBe(1);
  });
});
