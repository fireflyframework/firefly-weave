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
import { selectChoice } from "./support";
import { test, expect } from "@playwright/test";
import { parse } from "yaml";
import { execFileSync } from "node:child_process";
import { offline, sourceText, connected, allCapabilities } from "./support";

const profileSchema = JSON.parse(
  execFileSync(
    ".venv/bin/python",
    [
      "-c",
      "import json; from firefly_weave.contracts.llm import LLMProfile; print(json.dumps(LLMProfile.model_json_schema()))",
    ],
    { cwd: "..", encoding: "utf8" },
  ),
);

const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: policies, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - {id: policy, kind: decisionTable, uses: payment-policy@1.0.0, with: {ref: /input}}
    - {id: generate, kind: llm, uses: weave-agentic-generate@1.0.0, profile: default, connection: ai, prompt: {literal: Review the payment}, context: {ref: /input}}
  output: {ref: /steps/generate/output}
`;

for (const width of [1440, 600])
  test.describe(`Policy nodes at ${width}`, () => {
    test.use({ viewport: { width, height: 900 } });
    test("edits a reusable table reference without an Apply step", async ({
      page,
    }) => {
      await offline(page);
      await page.getByLabel("Choose a workflow file").setInputFiles({
        name: "policies.yaml",
        mimeType: "text/yaml",
        buffer: Buffer.from(source),
      });
      const show = page.getByRole("button", { name: "Show canvas" });
      if (await show.isVisible()) await show.click();
      await page.locator('[data-step="policy"]').click();
      const version = page.getByLabel("Table version", { exact: true });
      await expect(version).toBeVisible();
      await version.fill("payment-policy@2.0.0");
      await version.press("Tab");
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "payment-policy@2.0.0",
      );
    });
    test("authors ordered rules and publishes the selected table version", async ({
      page,
    }) => {
      await connected(page, { capabilities: [...allCapabilities, "compile"] });
      let published: any;
      await page.route("**/projects/project/decision-tables", async (route) => {
        if (route.request().method() === "POST") {
          published = parse(route.request().postDataJSON().source);
          await route.fulfill({
            status: 201,
            json: {
              id: "table",
              name: published.metadata.name,
              version: published.metadata.version,
            },
          });
        } else await route.fulfill({ json: { items: [], next_cursor: null } });
      });
      await page.getByLabel("Choose a workflow file").setInputFiles({
        name: "policies.yaml",
        mimeType: "text/yaml",
        buffer: Buffer.from(source),
      });
      const show = page.getByRole("button", { name: "Show canvas" });
      if (await show.isVisible()) await show.click();
      await page.locator('[data-step="policy"]').click();
      await page
        .getByRole("button", { name: "Create a decision table", exact: true })
        .click();
      const editor = page.getByRole("region", {
        name: "Decision table editor",
      });
      await editor
        .getByLabel("Table name", { exact: true })
        .fill("approval-rules");
      await selectChoice(editor.getByLabel("Matching policy"), "collect");
      await editor.getByLabel("Always match", { exact: true }).check();
      await editor.getByLabel("Rule 1 name", { exact: true }).fill("approve");
      await editor
        .getByRole("button", { name: "Add decision rule", exact: true })
        .click();
      await editor.getByLabel("Always match", { exact: true }).nth(1).check();
      await editor.getByLabel("Rule 2 name", { exact: true }).fill("audit");
      await editor
        .getByRole("button", { name: "Move rule 2 up", exact: true })
        .click();
      await expect(
        editor.getByLabel("Rule 1 name", { exact: true }),
      ).toHaveValue("audit");
      await editor
        .getByRole("button", { name: "Publish and use table", exact: true })
        .click();
      await expect(
        page.getByLabel("Table version", { exact: true }),
      ).toHaveValue("approval-rules@1.0.0");
      expect(published.spec.rules.map((rule: any) => rule.id)).toEqual([
        "audit",
        "approve",
      ]);
      expect(published.spec.outputSchema).toEqual({
        type: "array",
        items: { type: "object", properties: {} },
      });
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "approval-rules@1.0.0",
      );
    });
    test("edits AI prompt and canonical typed profile fields without losing earlier edits", async ({
      page,
    }) => {
      await offline(page);
      await page.route("**/studio/contracts/llm-profile/validate", (route) =>
        route.fulfill({ json: { valid: true, issues: [] } }),
      );
      await page.route("**/studio/contracts/llm-profile", (route) =>
        route.fulfill({ json: profileSchema }),
      );
      const configured = source.replace(
        "  output: {ref:",
        `  llmProfiles:
    default:
      provider: openai-responses
      model: original-model
      options: {max_tokens: 512}
      outputSchema: {type: object, properties: {summary: {type: string}}}
  output: {ref:`,
      );
      await page.getByLabel("Choose a workflow file").setInputFiles({
        name: "policies.yaml",
        mimeType: "text/yaml",
        buffer: Buffer.from(configured),
      });
      const show = page.getByRole("button", { name: "Show canvas" });
      if (await show.isVisible()) await show.click();
      await page.locator('[data-step="generate"]').click();
      const inspector = page.locator("weave-llm-inspector");
      await inspector
        .getByLabel("AI action version", { exact: true })
        .fill("weave-agentic-generate@1.1.0");
      await inspector
        .locator('[data-field="prompt"]')
        .getByLabel("AI prompt", { exact: true })
        .fill("Summarize the payment");
      await inspector
        .getByText("Configure workflow AI profiles", { exact: true })
        .click();
      await inspector
        .getByLabel("Model", { exact: true })
        .fill("updated-model");
      await inspector.getByLabel("Max tokens", { exact: true }).fill("1024");
      await inspector
        .getByRole("button", { name: "Continue to connection", exact: true })
        .click();
      await inspector
        .getByRole("button", { name: "Add AI connection slot", exact: true })
        .click();
      await inspector
        .getByRole("button", { name: "Review settings", exact: true })
        .click();
      await inspector
        .getByRole("button", {
          name: "Apply workflow AI settings",
          exact: true,
        })
        .click();
      const doc = parse(await sourceText(page));
      expect(doc.spec.steps[1].uses).toBe("weave-agentic-generate@1.1.0");
      expect(doc.spec.steps[1].prompt).toEqual({
        literal: "Summarize the payment",
      });
      expect(doc.spec.llmProfiles.default.model).toBe("updated-model");
      expect(doc.spec.llmProfiles.default.options.max_tokens).toBe(1024);
      expect(
        doc.spec.llmProfiles.default.outputSchema.properties.summary.type,
      ).toBe("string");
    });
  });
