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
import { test, expect, type Page } from "@playwright/test";
import { parse } from "yaml";
import { offline, sourceText } from "./support";
import { DesignerPage } from "./designer-po";

const profileSchema = JSON.parse(
  execFileSync(
    ".venv/bin/python",
    [
      "-c",
      "import json; from firefly_weave.contracts.llm import LLMProfile; print(json.dumps(LLMProfile.model_json_schema(by_alias=True)))",
    ],
    { cwd: "..", encoding: "utf8" },
  ),
);
const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: ai-review, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  connections:
    ai: {connector: weave-agentic-provider@1.0.0, required: true}
  llmProfiles:
    default:
      provider: azure-responses
      model: original-deployment
      options: {max_tokens: 512}
      outputSchema: {type: object, properties: {summary: {type: string}}}
  steps:
    - {id: earlier, kind: llm, uses: weave-agentic-generate@1.0.0, profile: default, connection: ai, prompt: {literal: First}, context: {ref: /input}}
    - {id: review, kind: llm, uses: weave-agentic-generate@1.0.0, profile: default, connection: ai, prompt: {literal: Review}, context: {literal: {note: Keep this context}}}
    - {id: later, kind: llm, uses: weave-agentic-generate@1.0.0, profile: default, connection: ai, prompt: {literal: Later}, context: {ref: /input}}
  output: {ref: /steps/review/output/result}
`;
async function open(page: Page) {
  await offline(page);
  await page.route("**/studio/contracts/llm-profile", (route) =>
    route.fulfill({ json: profileSchema }),
  );
  await page.getByLabel("Choose a workflow file").setInputFiles({
    name: "ai-review.yaml",
    mimeType: "text/yaml",
    buffer: Buffer.from(source),
  });
  const designer = new DesignerPage(page);
  await designer.open();
  await designer.selectStep("review");
  return page.locator("weave-llm-inspector");
}
for (const width of [1440, 600])
  test.describe(`Workflow AI setup at ${width}`, () => {
    test.use({ viewport: { width, height: 900 } });
    test("profile edits remain a local draft until Apply", async ({ page }) => {
      const form = await open(page);
      await form
        .getByText("Configure workflow AI profiles", { exact: true })
        .click();
      await form
        .getByLabel("Model", { exact: true })
        .fill("unapplied-deployment");
      await form
        .getByRole("button", { name: "Continue to connection", exact: true })
        .click();
      await form
        .getByRole("button", { name: "Add AI connection slot", exact: true })
        .click();
      await expect(
        form.getByLabel("AI connection slot", { exact: true }),
      ).toHaveValue("ai-2");
      const document = parse(await sourceText(page));
      expect(document.spec.llmProfiles.default.model).toBe(
        "original-deployment",
      );
      expect(document.spec.steps[1].connection).toBe("ai");
      expect(Object.keys(document.spec.connections)).toEqual(["ai"]);
    });
    test("Back preserves the draft and Review shows affected steps before applying", async ({
      page,
    }) => {
      const form = await open(page);
      await form
        .getByText("Configure workflow AI profiles", { exact: true })
        .click();
      await form
        .getByLabel("Model", { exact: true })
        .fill("reviewed-deployment");
      await form.getByLabel("Max tokens", { exact: true }).fill("1024");
      await form
        .getByRole("button", { name: "Continue to connection", exact: true })
        .click();
      await expect(
        form.getByRole("heading", {
          name: "Choose a workflow connection slot",
          exact: true,
        }),
      ).toBeFocused();
      await expect(
        form.getByLabel("AI connection slot", { exact: true }),
      ).toHaveValue("ai");
      await form.getByRole("button", { name: "Back", exact: true }).click();
      await expect(form.getByLabel("Model", { exact: true })).toHaveValue(
        "reviewed-deployment",
      );
      await form
        .getByRole("button", { name: "Continue to connection", exact: true })
        .click();
      await form
        .getByRole("button", { name: "Review settings", exact: true })
        .click();
      const affected = form.getByRole("list", {
        name: "Steps using this AI profile",
      });
      await expect(affected.getByRole("listitem")).toHaveText([
        "earlier",
        "review",
        "later",
      ]);
      await expect(form).toContainText("Activation binds this slot");
      await form
        .getByRole("button", {
          name: "Apply workflow AI settings",
          exact: true,
        })
        .click();
      const document = parse(await sourceText(page));
      expect(document.spec.llmProfiles.default.model).toBe(
        "reviewed-deployment",
      );
      expect(document.spec.llmProfiles.default.options.max_tokens).toBe(1024);
      expect(
        document.spec.llmProfiles.default.outputSchema.properties.summary.type,
      ).toBe("string");
      expect(
        document.spec.steps.map((s: { connection: string }) => s.connection),
      ).toEqual(["ai", "ai", "ai"]);
      expect(document.spec.connections.ai).toEqual({
        connector: "weave-agentic-provider@1.0.0",
        required: true,
      });
    });
    test("invalid profile cannot advance and Cancel keeps the saved profile", async ({
      page,
    }) => {
      const form = await open(page);
      await form
        .getByText("Configure workflow AI profiles", { exact: true })
        .click();
      await form.getByLabel("Model", { exact: true }).fill("");
      await expect(
        form.getByRole("button", {
          name: "Continue to connection",
          exact: true,
        }),
      ).toBeDisabled();
      await form
        .getByRole("button", { name: "Cancel profile changes", exact: true })
        .click();
      await form
        .getByText("Configure workflow AI profiles", { exact: true })
        .click();
      await expect(form.getByLabel("Model", { exact: true })).toHaveValue(
        "original-deployment",
      );
      await expect(
        form.getByRole("button", {
          name: "Continue to connection",
          exact: true,
        }),
      ).toBeEnabled();
      expect(parse(await sourceText(page)).spec.llmProfiles.default.model).toBe(
        "original-deployment",
      );
    });
    test("shared context includes only explicitly selected earlier AI results", async ({
      page,
    }) => {
      const form = await open(page);
      await expect(
        form.getByRole("checkbox", {
          name: "Use result from earlier",
          exact: true,
        }),
      ).not.toBeChecked();
      await expect(
        form.getByRole("checkbox", {
          name: "Use result from later",
          exact: true,
        }),
      ).toHaveCount(0);
      await expect(
        form.getByRole("checkbox", {
          name: "Use result from review",
          exact: true,
        }),
      ).toHaveCount(0);
      await form
        .getByRole("checkbox", { name: "Use result from earlier", exact: true })
        .check();
      const document = parse(await sourceText(page));
      expect(document.spec.steps[1].context).toEqual({
        object: {
          data: { literal: { note: "Keep this context" } },
          sharedAiResults: {
            object: { earlier: { ref: "/steps/earlier/output/result" } },
          },
        },
      });
      await new DesignerPage(page).selectStep("review");
      await form
        .getByRole("checkbox", { name: "Use result from earlier", exact: true })
        .uncheck();
      expect(parse(await sourceText(page)).spec.steps[1].context).toEqual({
        literal: { note: "Keep this context" },
      });
    });
  });
