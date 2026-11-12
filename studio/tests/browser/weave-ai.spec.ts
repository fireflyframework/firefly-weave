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
import { expect, test } from "@playwright/test";
import {
  withClassicDesigner,
  allCapabilities,
  command,
  connected,
  newWorkflow,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";

test.beforeEach(async ({ page }) => {
  await withClassicDesigner(page);
});

const proposal = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: reviewed-proposal, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {type: object}
  steps: [{id: check, kind: wait, durationSeconds: 120}]
  output: {literal: {}}
`;
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("Weave AI requires context opt-in and a reviewed validated explicit apply", async ({
      page,
    }) => {
      await connected(page, { capabilities: [...allCapabilities, "lumi.use"] });
      await page.route("**/lumi/status", (route) =>
        route.fulfill({
          json: {
            configured: true,
            provider: "openai",
            model: "test-model",
            revision: 1,
          },
        }),
      );
      const sent: any[] = [];
      await page.route("**/lumi/ask", async (route) => {
        sent.push(route.request().postDataJSON());
        await route.fulfill({
          json: {
            answer: "<img src=x onerror=alert(1)> Review this draft.",
            proposals: [
              {
                title: "Review workflow",
                kind: "workflow",
                format: "yaml",
                source: proposal,
              },
            ],
            followUps: ["Explain the wait"],
          },
        });
      });
      await page.route("**/studio/local/validate", (route) =>
        route.fulfill({
          json: { validationOk: true, errorCount: 0, diagnostics: [] },
        }),
      );
      await newWorkflow(page);
      await page
        .getByRole("button", { name: "Ask Weave AI", exact: true })
        .click();
      const panel = page.getByRole("dialog", {
        name: "Ask Weave AI",
        exact: true,
      });
      await panel
        .getByLabel("Message to Weave AI", { exact: true })
        .fill("Suggest a two-minute wait.");
      await panel.getByLabel("Include current source", { exact: true }).check();
      await panel
        .getByRole("button", { name: "Send message", exact: true })
        .click();
      await expect(panel).toContainText("<img src=x onerror=alert(1)>");
      await expect(panel.locator("img")).toHaveCount(0);
      expect(sent[0].draft.source).toContain("untitled-workflow");
      expect(sent[0].attachments).toEqual([]);
      await panel
        .getByRole("button", { name: "Review workflow", exact: true })
        .click();
      const apply = panel.getByRole("button", {
        name: "Apply to local draft",
        exact: true,
      });
      await expect(apply).toBeDisabled();
      await panel
        .getByRole("button", { name: "Validate proposal", exact: true })
        .click();
      await expect(apply).toBeEnabled();
      await apply.click();
      expect(await sourceText(page)).toContain("reviewed-proposal");
      const designer = new DesignerPage(page);
      await designer.open();
      await command(page, "Undo");
      expect(await sourceText(page)).toContain("untitled-workflow");
    });
  });

test("context defaults off, stale proposals cannot replace later edits, and reload clears conversation", async ({
  page,
}) => {
  await connected(page, { capabilities: [...allCapabilities, "lumi.use"] });
  await page.route("**/lumi/status", (r) =>
    r.fulfill({
      json: { configured: true, provider: "openai", model: "test" },
    }),
  );
  let sent: any;
  await page.route("**/lumi/ask", (r) => {
    sent = r.request().postDataJSON();
    return r.fulfill({
      json: {
        answer: "Private conversation",
        proposals: [
          {
            title: "Review workflow",
            kind: "workflow",
            format: "yaml",
            source: proposal,
          },
        ],
        followUps: [],
      },
    });
  });
  await newWorkflow(page);
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Ask Weave AI", exact: true });
  await expect(
    panel.getByLabel("Include current source", { exact: true }),
  ).not.toBeChecked();
  await panel.getByLabel("Message to Weave AI").fill("Suggest a change");
  await panel
    .getByRole("button", { name: "Send message", exact: true })
    .click();
  await expect(panel).toContainText("Private conversation");
  expect(sent.draft).toBeUndefined();
  expect(sent.attachments).toEqual([]);
  await panel
    .getByRole("button", { name: "Close Weave AI", exact: true })
    .click();
  const designer = new DesignerPage(page);
  await designer.append("wait");
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  await panel
    .getByRole("button", { name: "Review workflow", exact: true })
    .click();
  await expect(panel).toContainText(
    "The local draft changed after this request",
  );
  await expect(
    panel.getByRole("button", { name: "Apply to local draft", exact: true }),
  ).toBeDisabled();
  await page.reload();
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  await expect(panel).not.toContainText("Private conversation");
});

test("Set up Weave AI opens Settings › AI models", async ({ page }) => {
  await connected(page, {
    capabilities: [...allCapabilities, "lumi.use", "lumi.manage"],
  });
  await page.route("**/lumi/status", (r) =>
    r.fulfill({ json: { configured: false } }),
  );
  await newWorkflow(page);
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Ask Weave AI", exact: true });
  await expect(panel).toContainText(
    "Weave AI is not set up in this environment.",
  );
  await panel
    .getByRole("button", { name: "Set up Weave AI", exact: true })
    .click();
  await expect(panel).toHaveCount(0);
  await expect(page).toHaveURL(/\/settings\/ai$/);
});

test("Set up Weave AI with unsaved edits keeps the designer and its address when the person keeps editing", async ({
  page,
}) => {
  await connected(page, {
    capabilities: [...allCapabilities, "lumi.use", "lumi.manage"],
  });
  await page.route("**/lumi/status", (r) =>
    r.fulfill({ json: { configured: false } }),
  );
  // The draft can't save, so its edits stay unsaved and leaving asks first.
  await page.route("**/drafts/*", (r) =>
    r.request().method() === "PUT"
      ? r.fulfill({
          status: 422,
          json: { status: 422, code: "WV-VALIDATION", message: "Not saved." },
        })
      : r.fallback(),
  );
  await newWorkflow(page);
  await page
    .locator(".palette")
    .getByRole("button", { name: "AI task", exact: true })
    .click();
  await page.keyboard.press("Escape");
  const designer = page.url();
  expect(designer).toMatch(/\/designer$/);
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Ask Weave AI", exact: true })
    .getByRole("button", { name: "Set up Weave AI", exact: true })
    .click();
  await page
    .getByRole("dialog", { name: /^Leave/ })
    .getByRole("button", { name: "Keep editing" })
    .click();
  await expect(page.locator(".palette")).toBeVisible();
  await expect(page.getByRole("tab", { name: "AI models" })).toHaveCount(0);
  await expect(page).toHaveURL(designer);
  // The designer's address is still in history: Back leaves it, and
  // Forward returns to it, not to Settings.
  await page.goBack();
  await expect(page).not.toHaveURL(designer);
  await expect(page).not.toHaveURL(/\/settings/);
  await page.goForward();
  await expect(page).toHaveURL(designer);
});

test("non-workflow proposals are validated before saving a reviewed file", async ({
  page,
}) => {
  await connected(page, { capabilities: [...allCapabilities, "lumi.use"] });
  await page.route("**/lumi/status", (r) =>
    r.fulfill({
      json: { configured: true, provider: "openai-responses", model: "test" },
    }),
  );
  const source =
    "apiVersion: weave/v1alpha1\nkind: DecisionTable\nmetadata: {name: policy, version: 1.0.0}\nspec: {}";
  await page.route("**/lumi/ask", (r) =>
    r.fulfill({
      json: {
        answer: "Review the policy draft.",
        proposals: [
          {
            title: "Review policy",
            kind: "decisionTable",
            format: "yaml",
            source,
          },
        ],
        followUps: [],
      },
    }),
  );
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({ json: { validationOk: true, errorCount: 0, diagnostics: [] } }),
  );
  await newWorkflow(page);
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Ask Weave AI", exact: true });
  await panel.getByLabel("Message to Weave AI").fill("Propose a policy");
  await panel
    .getByRole("button", { name: "Send message", exact: true })
    .click();
  await panel
    .getByRole("button", { name: "Review policy", exact: true })
    .click();
  const save = panel.getByRole("button", {
    name: "Save reviewed draft file",
    exact: true,
  });
  await expect(save).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "Apply to local draft", exact: true }),
  ).toHaveCount(0);
  await panel
    .getByRole("button", { name: "Validate proposal", exact: true })
    .click();
  await expect(save).toBeEnabled();
  const download = page.waitForEvent("download");
  await save.click();
  expect((await download).suggestedFilename()).toBe("lumi-decisionTable.yaml");
  await panel
    .getByLabel("Proposed source")
    .fill(source.replace("kind: DecisionTable", "kind: Workflow"));
  await expect(save).toBeDisabled();
  await panel
    .getByRole("button", { name: "Validate proposal", exact: true })
    .click();
  await expect(panel).toContainText("must contain a DecisionTable document");
});

test("a proposal with problems offers Ask Weave AI to fix, with the problems and the proposal attached", async ({
  page,
}) => {
  await connected(page, { capabilities: [...allCapabilities, "lumi.use"] });
  await page.route("**/lumi/status", (r) =>
    r.fulfill({
      json: { configured: true, provider: "openai-chat", model: "qwen3:4b" },
    }),
  );
  const broken = proposal.replace(
    "durationSeconds: 120",
    "durationSeconds: -1",
  );
  const sent: any[] = [];
  await page.route("**/lumi/ask", (r) => {
    sent.push(r.request().postDataJSON());
    return r.fulfill({
      json: {
        answer: sent.length === 1 ? "Here is a draft." : "Fixed the wait.",
        proposals: [
          {
            title: "Review workflow",
            kind: "workflow",
            format: "yaml",
            source: sent.length === 1 ? broken : proposal,
            diagnostics: sent.length === 1 ? 1 : 0,
          },
        ],
        followUps: [],
      },
    });
  });
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({
      json: {
        validationOk: false,
        errorCount: 1,
        diagnostics: [
          {
            code: "SCHEMA",
            severity: "error",
            message: "durationSeconds must be at least 1.",
            path: "/spec/steps/0/durationSeconds",
          },
        ],
      },
    }),
  );
  await newWorkflow(page);
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Ask Weave AI", exact: true });
  await panel.getByLabel("Message to Weave AI").fill("Add a wait");
  await panel
    .getByRole("button", { name: "Send message", exact: true })
    .click();
  await panel
    .getByRole("button", { name: "Review workflow 1 problem", exact: true })
    .click();
  await expect(panel).toContainText(
    "The platform found 1 problem in this proposal.",
  );
  await panel
    .getByRole("button", { name: "Ask Weave AI to fix", exact: true })
    .click();
  await expect(panel).toContainText("Fixed the wait.");
  expect(sent[1].message).toBe(
    'Fix the proposal "Review workflow". The platform found 1 problem:\n- durationSeconds must be at least 1.',
  );
  expect(sent[1].draft).toEqual({ format: "yaml", source: broken });
  expect(sent[1].fix).toBe(true);
  expect(sent[0].fix).toBeUndefined();
  await expect(
    panel.getByRole("button", { name: "Review workflow", exact: true }),
  ).toBeVisible();
});
