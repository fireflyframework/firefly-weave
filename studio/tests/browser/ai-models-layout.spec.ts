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
// Settings › AI models and its side sheet, measured: edges and centers line
// up, controls keep the desktop density (36 px fields and buttons, 32 px rows)
// or the touch one (44 px), and nothing is cut off or scrolls sideways.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { connected } from "./support";
import { expectNotClippedBy } from "./ux-assertions";
import {
  box,
  boxes,
  clippedText,
  contentEdges,
  density,
  expectAligned,
  textBox,
} from "./step-geometry";
import {
  AI_CAPABILITIES,
  mockAiPlatform,
  ollamaConnection,
  openaiConnection,
  openAiModels,
  readiness,
  sheet,
} from "./ai-models-po";

const sizes = [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "600x500", width: 600, height: 500, touch: false },
  { tag: "360x640", width: 360, height: 640, touch: true },
  { tag: "1024x768-touch", width: 1024, height: 768, touch: true },
];

async function open(page: Page) {
  await connected(page, { capabilities: AI_CAPABILITIES });
  await mockAiPlatform(page, {
    connections: [ollamaConnection, openaiConnection],
    readiness: readiness({
      actions_published: {
        status: "action",
        detail: "actions_missing",
        fix: {
          kind: "operation",
          label: "Publish",
          operation: "ai_setup.publish",
        },
      },
      worker_installed: {
        status: "action",
        detail: "worker_missing",
        fix: {
          kind: "command",
          label: "Ask an operator to enable AI",
          command: "weave platform ai enable --ollama auto",
        },
      },
    }),
  });
  await openAiModels(page);
  await expect(page.locator(".readiness-row")).toHaveCount(8);
  await expect(page.locator(".connection-row")).toHaveCount(2);
  await page.evaluate(() => document.fonts.ready);
}
/** Groups boxes that share a top edge (one visual row). */
function byRow<T extends { y: number }>(items: T[]): T[][] {
  const rows: T[][] = [];
  for (const item of [...items].sort((a, b) => a.y - b.y)) {
    const row = rows.find((group) => Math.abs(group[0].y - item.y) <= 1);
    if (row) row.push(item);
    else rows.push([item]);
  }
  return rows;
}
async function minimumHeight(controls: Locator, minimum: number, what: string) {
  for (const control of await boxes(controls))
    expect(
      control.height,
      `${what} is ${control.height} px high`,
    ).toBeGreaterThanOrEqual(minimum - 0.5);
}

for (const size of sizes)
  test.describe(`AI models layout at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("the page lines up and keeps its density", async ({ page }) => {
      await open(page);
      const d = await density(page);
      const root = page.locator("weave-ai-models-page");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(await clippedText(root, "")).toEqual([]);

      // Panels share the page's left edge; side by side from 1024 px, stacked (checklist first) below.
      const readinessPanel = await box(page.locator(".ai-readiness"));
      const providersPanel = await box(page.locator(".ai-providers"));
      const weavePanel = await box(page.locator("weave-ai-card"));
      if (size.width >= 1024) {
        expectAligned([readinessPanel.y, providersPanel.y], "panel tops");
        expect(readinessPanel.x).toBeGreaterThan(providersPanel.right);
        expectAligned(
          [providersPanel.x, weavePanel.x],
          "left edges of providers and Weave AI",
        );
        expectAligned(
          [readinessPanel.right, weavePanel.right],
          "right edges of readiness and Weave AI",
        );
      } else {
        expect(readinessPanel.bottom).toBeLessThanOrEqual(providersPanel.y);
        expectAligned(
          [readinessPanel.x, providersPanel.x, weavePanel.x],
          "panel left edges",
        );
      }

      // Each heading starts at its panel's content edge, like the first thing below it.
      const providers = page.locator(".ai-providers");
      const edge = (await contentEdges(providers)).left;
      expectAligned(
        [
          edge,
          (await textBox(page.locator("#ai-providers-title"))).x,
          (await box(page.locator(".provider-card"))).x,
        ],
        "providers heading and first card",
      );
      const readinessEdge = (await contentEdges(page.locator(".ai-readiness")))
        .left;
      expectAligned(
        [
          readinessEdge,
          (await textBox(page.locator("#ai-readiness-title"))).x,
          (await box(page.locator(".readiness-icon"))).x,
        ],
        "readiness heading and first icon",
      );

      // Cards in one row share their top, height and button line.
      const cards = await boxes(page.locator(".provider-card"));
      const buttons = await boxes(page.locator(".provider-card > button"));
      // Four cards fill their rows evenly: 4, 2 + 2 or one per row, never 3 + 1.
      const perRow = byRow(cards).map((row) => row.length);
      expect([[4], [2, 2], [1, 1, 1, 1]]).toContainEqual(perRow);
      if (size.width >= 1440) expect(perRow).toEqual([4]);
      for (const row of byRow(cards)) {
        expectAligned(
          row.map((card) => card.height),
          "card heights in a row",
        );
      }
      for (const row of byRow(buttons))
        expectAligned(
          row.map((button) => button.y),
          "card buttons in a row",
        );
      await minimumHeight(
        page.locator(".provider-card > button"),
        d.control,
        "a card button",
      );

      // Checklist rows: the icon sits on the text's center; fixes end on one right edge.
      const rows = page.locator(".readiness-row");
      for (let index = 0; index < 8; index++) {
        const row = rows.nth(index);
        const rowBox = await box(row);
        expect(rowBox.height).toBeGreaterThanOrEqual(d.row - 0.5);
        expectAligned(
          [
            (await box(row.locator(".readiness-icon"))).middle,
            (await box(row.locator(".readiness-text"))).middle,
          ],
          `checklist row ${index + 1}: icon and text centers`,
        );
      }
      const fixes = await boxes(page.locator(".readiness-action"));
      expectAligned(
        fixes.map((fix) => fix.right),
        "checklist fixes' right edges",
      );
      await minimumHeight(
        page.locator(".readiness-action"),
        d.control,
        "a checklist fix",
      );
      // A command wraps between its words, never inside one ("--ollama").
      const command = page.locator(".readiness-command code");
      // Selecting it copies the command as written.
      expect(await command.evaluate((code) => code.textContent)).toBe(
        "weave platform ai enable --ollama auto",
      );
      expect(
        await command.evaluate((code) => {
          const text = code.textContent ?? "";
          const split: string[] = [];
          for (const match of text.matchAll(/\S+/g)) {
            const range = document.createRange();
            let offset = 0;
            const walker = document.createTreeWalker(
              code,
              NodeFilter.SHOW_TEXT,
            );
            for (let node = walker.nextNode(); node; node = walker.nextNode()) {
              const length = node.textContent!.length;
              const start = match.index - offset;
              const end = start + match[0].length;
              if (start >= 0 && start < length) range.setStart(node, start);
              if (end > 0 && end <= length) range.setEnd(node, end);
              offset += length;
            }
            const lines = new Set(
              [...range.getClientRects()]
                .filter((rect) => rect.width > 0)
                .map((rect) => Math.round(rect.top)),
            );
            if (lines.size > 1) split.push(match[0]);
          }
          return split;
        }),
        "words split across lines",
      ).toEqual([]);

      // Connection rows: actions centered on the row beside the text, or under it on a phone.
      for (const row of await page.locator(".connection-row").all()) {
        const main = await box(row.locator(".connection-main"));
        const actions = await box(row.locator(".connection-actions"));
        if (size.width >= 600)
          expectAligned(
            [main.middle, actions.middle],
            "connection text and actions centers",
            1.5,
          );
        else
          expectAligned(
            [main.x, actions.x],
            "connection text and actions left edges",
          );
        await minimumHeight(
          row.locator("button"),
          d.control,
          "a connection action",
        );
      }
      // Weave AI's dropdowns (connection, model, context size) are exactly as
      // high as the buttons beside them; side by side they share a top,
      // stacked they share a left edge, never two in a row and one below.
      const card = page.locator("weave-ai-card");
      // The bordered field, not the text box inside it.
      const dropdowns = await boxes(card.locator("weave-select .choice-field"));
      expect(dropdowns).toHaveLength(3);
      expectAligned(
        [d.control, ...dropdowns.map((dropdown) => dropdown.height)],
        "Weave AI dropdown heights",
        0.5,
      );
      if (Math.abs(dropdowns[0].y - dropdowns[1].y) <= 1)
        expectAligned(
          dropdowns.map((dropdown) => dropdown.y),
          "Weave AI dropdown tops",
        );
      else
        expectAligned(
          dropdowns.map((dropdown) => dropdown.x),
          "Weave AI dropdown left edges",
        );
      // A dropdown's chevron sits inside its 44 px field, which opens the
      // same list: the field is the target, measured above.
      if (d.touch)
        await minimumHeight(
          root.locator("button:not(.choice-field button)"),
          44,
          "a touch target",
        );
    });

    test("the side sheet fills its edge and lines up its fields and footer", async ({
      page,
    }) => {
      await open(page);
      await page.getByRole("button", { name: "Connect Anthropic" }).click();
      const dialog = sheet(page);
      await expect(dialog.getByLabel("Secret handle")).toBeVisible();
      const d = await density(page);
      const panel = await box(dialog);
      expectAligned([panel.right, size.width], "sheet right edge");
      expectAligned([panel.y, 0], "sheet top");
      expectAligned([panel.bottom, size.height], "sheet bottom");
      expectAligned(
        [panel.width, size.width < 768 ? size.width : 480],
        "sheet width",
      );

      // The heading's first line sits on the close button's center.
      const heading = await textBox(dialog.locator(".modal-header h2"));
      const close = await box(
        dialog.getByRole("button", { name: "Close AI connection" }),
      );
      expectAligned(
        [heading.middle, close.middle],
        "heading and close centers",
      );

      // Labels, fields and group legends share the form's left edge.
      const left = (await contentEdges(dialog.locator(".ai-sheet"))).left;
      expectAligned(
        [
          left,
          (await textBox(dialog.locator(".choice-group legend").first())).x,
          (await box(dialog.getByLabel("Connection name"))).x,
          (await box(dialog.getByLabel("Secret handle"))).x,
        ],
        "sheet field edges",
      );
      // Radio rows: the radio sits on its label's center.
      for (const row of await dialog.locator(".choice-row").all()) {
        expect((await box(row)).height).toBeGreaterThanOrEqual(d.row - 0.5);
        expectAligned(
          [
            (await box(row.locator("input"))).middle,
            (await box(row.locator(".choice-text"))).middle,
          ],
          "radio and label centers",
          1.5,
        );
      }
      await minimumHeight(
        dialog.locator("input:not([type=radio])"),
        d.control,
        "a sheet field",
      );

      // Footer answers: one row ending at the content edge, or stacked full width on a phone.
      const footer = dialog.locator(".dialog-footer");
      const answers = await boxes(footer.locator("button"));
      if (new Set(answers.map((answer) => Math.round(answer.y))).size === 1)
        expectAligned(
          [answers.at(-1)!.right, (await contentEdges(footer)).right],
          "primary answer right edge",
        );
      else
        expectAligned(
          answers.map((answer) => answer.width),
          "stacked answers' widths",
        );
      await minimumHeight(
        footer.locator("button"),
        d.control,
        "a footer answer",
      );
      for (const answer of await footer.locator("button").all())
        await expectNotClippedBy(answer, dialog);
      expect(await clippedText(dialog, "")).toEqual([]);
    });
  });
