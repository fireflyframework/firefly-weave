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
import { execFileSync } from "node:child_process";
import { expect, test } from "@playwright/test";
import {
  allCapabilities,
  command,
  connected,
  newWorkflow,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";

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
    test("Lumi requires context opt-in and a reviewed validated explicit apply", async ({
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
      await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
      const panel = page.getByRole("dialog", { name: "Ask Lumi", exact: true });
      await panel
        .getByLabel("Message to Lumi", { exact: true })
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
  await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Ask Lumi", exact: true });
  await expect(
    panel.getByLabel("Include current source", { exact: true }),
  ).not.toBeChecked();
  await panel.getByLabel("Message to Lumi").fill("Suggest a change");
  await panel
    .getByRole("button", { name: "Send message", exact: true })
    .click();
  await expect(panel).toContainText("Private conversation");
  expect(sent.draft).toBeUndefined();
  expect(sent.attachments).toEqual([]);
  await panel.getByRole("button", { name: "Close Lumi", exact: true }).click();
  const designer = new DesignerPage(page);
  await designer.append("wait");
  await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
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
  await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
  await expect(panel).not.toContainText("Private conversation");
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 700 },
])
  test(`Lumi settings wizard preserves drafts and saves reviewed configuration at ${viewport.width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    const schema = JSON.parse(
      execFileSync(
        ".venv/bin/python",
        [
          "-c",
          "import json; from firefly_weave.contracts.lumi import LumiConfigurationRequest; print(json.dumps(LumiConfigurationRequest.model_json_schema(by_alias=True)))",
        ],
        { cwd: "..", encoding: "utf8" },
      ),
    );
    const fixed = schema.$defs.LumiProfile.properties.outputSchema.const;
    await connected(page, {
      capabilities: [...allCapabilities, "lumi.use", "lumi.manage"],
    });
    await page.route("**/lumi/status", (r) =>
      r.fulfill({
        json: {
          configured: true,
          provider: "openai-responses",
          model: "before",
        },
      }),
    );
    await page.route("**/studio/contracts/lumi-configuration", (r) =>
      r.fulfill({ json: schema }),
    );
    const original = {
      enabled: true,
      connection_revision_id: "11111111-1111-4111-8111-111111111111",
      profile: {
        provider: "openai-responses",
        model: "before",
        options: { max_tokens: 512 },
        reasoning: { pattern: "none", maxSteps: 6 },
        maxCalls: 8,
        timeoutSeconds: 120,
        outputSchema: fixed,
      },
      revision: 7,
    };
    await page.route("**/environments/development/connections?*", (r) =>
      r.fulfill({
        json: {
          items: [
            {
              id: original.connection_revision_id,
              name: "lumi-provider",
              revision: 1,
              connector: "weave-agentic-provider@1.0.0",
              config: {
                provider: "openai-responses",
                endpoint: "https://api.openai.com",
              },
            },
          ],
          next_cursor: null,
        },
      }),
    );
    let saved: any;
    let etag: string | undefined;
    let finishSave: () => void = () => {};
    const saving = new Promise<void>((resolve) => {
      finishSave = resolve;
    });
    await page.route("**/lumi/configuration", async (r) => {
      if (r.request().method() === "PUT") {
        saved = r.request().postDataJSON();
        etag = r.request().headers()["if-match"];
        await saving;
        return r.fulfill({ json: { ...saved, revision: 8 } });
      }
      return r.fulfill({ json: original });
    });
    await newWorkflow(page);
    await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
    const panel = page.getByRole("dialog", { name: "Ask Lumi", exact: true });
    await panel
      .getByRole("button", { name: "Lumi settings", exact: true })
      .click();
    await panel.getByLabel("Model", { exact: true }).fill("configured-model");
    await panel.getByLabel("Max tokens", { exact: true }).fill("1024");
    await expect(panel.getByText("Output schema", { exact: true })).toHaveCount(
      0,
    );
    await expect(
      panel.getByRole("button", { name: "Save Lumi settings", exact: true }),
    ).toHaveCount(0);
    await panel
      .getByRole("button", { name: "Continue to connection", exact: true })
      .click();
    await expect(
      panel.getByRole("heading", {
        name: "Choose a provider connection",
        exact: true,
      }),
    ).toBeFocused();
    expect(saved).toBeUndefined();
    await panel.getByRole("button", { name: "Back", exact: true }).click();
    await expect(panel.getByLabel("Model", { exact: true })).toHaveValue(
      "configured-model",
    );
    await expect(panel.getByLabel("Max tokens", { exact: true })).toHaveValue(
      "1024",
    );
    await panel
      .getByRole("button", { name: "Continue to connection", exact: true })
      .click();
    await panel
      .getByRole("button", { name: "Review settings", exact: true })
      .click();
    await expect(
      panel.getByRole("heading", { name: "Review Lumi settings", exact: true }),
    ).toBeFocused();
    await expect(panel.locator(".settings-summary")).toContainText(
      "configured-model",
    );
    await expect(panel.locator(".settings-summary")).toContainText(
      "https://api.openai.com",
    );
    await expect(panel.locator(".settings-summary")).toContainText(
      "lumi-provider",
    );
    expect(saved).toBeUndefined();
    await panel.screenshot({
      path: testInfo.outputPath(`lumi-review-${viewport.width}.png`),
    });
    expect(await panel.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(
      true,
    );
    await panel
      .getByRole("button", { name: "Save Lumi settings", exact: true })
      .click();
    await expect(
      panel.getByRole("button", { name: "Lumi settings", exact: true }),
    ).toBeDisabled();
    await expect(
      panel.getByRole("button", { name: "New conversation", exact: true }),
    ).toBeDisabled();
    await expect(
      panel.getByRole("button", { name: "Reload settings", exact: true }),
    ).toBeDisabled();
    finishSave();
    await expect(panel.getByLabel("Message to Lumi")).toBeVisible();
    expect(etag).toBe('"7"');
    expect(saved.profile.model).toBe("configured-model");
    expect(saved.profile.options.max_tokens).toBe(1024);
    expect(saved.profile.outputSchema).toEqual(fixed);
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
  await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Ask Lumi", exact: true });
  await panel.getByLabel("Message to Lumi").fill("Propose a policy");
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
