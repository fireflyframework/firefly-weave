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
import {
  offline,
  sourceText,
  connected,
  workerAction,
  selectChoice,
} from "./support";
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
async function open(page: Page, online = false, definition = source) {
  if (online)
    await connected(page, {
      catalog: [
        {
          id: "ai-action",
          document: {
            ...workerAction,
            metadata: { name: "published-ai", version: "2.0.0" },
            spec: {
              ...workerAction.spec,
              implementation: {
                kind: "worker",
                taskType: "weave-agentic.generate",
                taskVersion: "1.0.0",
              },
            },
          },
        },
      ],
    });
  else await offline(page);
  await page.route("**/studio/contracts/llm-profile/validate", (route) =>
    route.fulfill({ json: { valid: true, issues: [] } }),
  );
  await page.route("**/studio/contracts/llm-profile", (route) =>
    route.fulfill({ json: profileSchema }),
  );
  await page.getByLabel("Choose a workflow file").setInputFiles({
    name: "ai-review.yaml",
    mimeType: "text/yaml",
    buffer: Buffer.from(definition),
  });
  const designer = new DesignerPage(page);
  await designer.open();
  await designer.selectStep("review");
  return page.locator("weave-llm-inspector");
}
for (const width of [1440, 600])
  test.describe(`Workflow AI setup at ${width}`, () => {
    test.use({ viewport: { width, height: 900 } });
    test("connection slot usage includes the AI steps that depend on it", async ({
      page,
    }) => {
      await open(page);
      await page
        .getByRole("button", { name: "Workflow settings", exact: true })
        .click();
      await expect(
        page.getByRole("group", { name: "ai", exact: true }),
      ).toContainText("Used by 3 steps");
    });
    test("prompt sources explain their purpose on demand and match the field type", async ({
      page,
    }, testInfo) => {
      const form = await open(page);
      const prompt = form.locator('[data-field="prompt"]');
      await expect(prompt.getByRole("radio")).toHaveCount(3);
      await expect(
        prompt.getByRole("radio", { name: "Fields", exact: true }),
      ).toHaveCount(0);
      await expect(
        form.locator('[data-field="context"]').getByRole("radio"),
      ).toHaveCount(5);
      const help = prompt.getByLabel("Help for AI prompt: data sources", {
        exact: true,
      });
      await expect(
        prompt.getByText("Choose a data source", { exact: true }),
      ).toBeHidden();
      await help.focus();
      await help.press("Enter");
      await expect(
        prompt.getByText("Choose a data source", { exact: true }),
      ).toBeVisible();
      await expect(prompt.locator(".source-help-content")).toContainText(
        "same on every run",
      );
      const box = await prompt.locator(".source-help-content").boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      await page.screenshot({ path: testInfo.outputPath("prompt-help.png") });
      await help.press("Escape");
      await expect(help).toBeFocused();
      await expect(prompt.locator(".source-help-content")).toBeHidden();
      await prompt.getByRole("radio", { name: "Formula", exact: true }).click();
      await prompt
        .getByRole("combobox", { name: "AI prompt operator", exact: true })
        .click();
      await expect(prompt.getByRole("option")).toHaveCount(1);
      await expect(prompt.getByRole("option")).toContainText(
        "first available of",
      );
    });
    test("provider choices stay open inside the workflow profile dialog", async ({
      page,
    }) => {
      if (width === 1440)
        await page.setViewportSize({ width: 1600, height: 900 });
      const form = await open(page);
      await form
        .getByText("Configure workflow AI profiles", { exact: true })
        .click();
      await form
        .getByRole("button", { name: "Add AI profile", exact: true })
        .click();
      const provider = form.getByRole("combobox", {
        name: "Provider",
        exact: true,
      });
      await provider.click();
      await expect(provider).toHaveAttribute("aria-expanded", "true");
      await expect(
        form.getByRole("listbox", { name: "Provider options" }),
      ).toBeVisible();
      await provider.press("Escape");
      await form
        .getByRole("button", { name: "Show choices for Provider", exact: true })
        .click();
      await expect(provider).toHaveAttribute("aria-expanded", "true");
      await form
        .getByRole("option", { name: "openai-chat", exact: true })
        .click();
      await expect(provider).toHaveValue("openai-chat");
    });
    test("provider-only edits cannot be dismissed without applying or cancelling", async ({
      page,
    }) => {
      const form = await open(page);
      await form
        .getByText("Configure workflow AI profiles", { exact: true })
        .click();
      await form
        .getByRole("combobox", { name: "Provider", exact: true })
        .click();
      await form
        .getByRole("option", { name: "openai-chat", exact: true })
        .click();
      await form
        .getByRole("button", {
          name: "Close workflow AI profiles",
          exact: true,
        })
        .click();
      await expect(
        form.getByText(
          "Apply or cancel the workflow AI profile changes before continuing. Your draft is still here.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        page.getByRole("dialog", { name: "Workflow AI profiles", exact: true }),
      ).toBeVisible();
      await expect(
        form.getByRole("combobox", { name: "Provider", exact: true }),
      ).toHaveValue("openai-chat");
      await form
        .getByRole("button", { name: "Cancel profile changes", exact: true })
        .click();
      expect(
        parse(await sourceText(page)).spec.llmProfiles.default.provider,
      ).toBe("azure-responses");
    });
    test("profile edits remain a local draft until Apply", async ({ page }) => {
      const form = await open(page);
      await form
        .getByText("Configure workflow AI profiles", { exact: true })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Workflow AI profiles", exact: true }),
      ).toBeVisible();
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
      await form
        .getByRole("button", {
          name: "Close workflow AI profiles",
          exact: true,
        })
        .click();
      await expect(
        page.getByText(
          "Apply or cancel the workflow AI profile changes before continuing. Your draft is still here.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        form.getByLabel("AI connection slot", { exact: true }),
      ).toHaveValue("ai-2");
      await form.getByRole("button", { name: "Back", exact: true }).click();
      await expect(form.getByLabel("Model", { exact: true })).toHaveValue(
        "unapplied-deployment",
      );
      await form
        .getByRole("button", { name: "Cancel profile changes", exact: true })
        .click();
      const document = parse(await sourceText(page));
      expect(document.spec.llmProfiles.default.model).toBe(
        "original-deployment",
      );
      expect(document.spec.steps[1].connection).toBe("ai");
      expect(Object.keys(document.spec.connections)).toEqual(["ai"]);
    });
    test("changes only the AI step connection using an existing slot", async ({
      page,
    }) => {
      const form = await open(
        page,
        false,
        source.replace(
          "  llmProfiles:",
          "    backup: {connector: weave-agentic-provider@1.0.0, required: true}\n  llmProfiles:",
        ),
      );
      await selectChoice(
        form.getByLabel("AI connection slot", { exact: true }),
        "backup",
      );
      const document = parse(await sourceText(page));
      expect(document.spec.steps[1].connection).toBe("backup");
      expect(document.spec.steps[0].connection).toBe("ai");
      expect(document.spec.llmProfiles.default.model).toBe(
        "original-deployment",
      );
    });
    test("chooses a published AI action without typing a version", async ({
      page,
    }) => {
      const form = await open(page, true);
      const picker = form.getByRole("combobox", {
        name: "Published AI action",
        exact: true,
      });
      await picker.click();
      await picker.fill("published-ai");
      await page
        .locator('[role="option"][data-uses="published-ai@2.0.0"]')
        .click();
      const document = parse(await sourceText(page));
      expect(document.spec.steps[1].uses).toBe("published-ai@2.0.0");
    });
    test("AI prompt preserves multiple lines through close and source round trip", async ({
      page,
    }) => {
      const form = await open(page);
      const prompt = form.getByRole("textbox", {
        name: "AI prompt",
        exact: true,
      });
      await expect(prompt).toHaveAttribute("rows", "6");
      await prompt.fill(
        "Summarize the request.\nReturn the decision on a new line.",
      );
      const document = parse(await sourceText(page));
      expect(document.spec.steps[1].prompt).toEqual({
        literal: "Summarize the request.\nReturn the decision on a new line.",
      });
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
      await form
        .getByRole("button", {
          name: "Close workflow AI profiles",
          exact: true,
        })
        .click();
      expect(parse(await sourceText(page)).spec.llmProfiles.default.model).toBe(
        "original-deployment",
      );
    });
    test("canonical profile rejection keeps Apply unavailable and preserves the saved profile", async ({
      page,
    }) => {
      const form = await open(page);
      await page.route("**/studio/contracts/llm-profile/validate", (route) => {
        const profile = route.request().postDataJSON();
        expect(profile.outputSchema).toEqual({});
        return route.fulfill({
          json:
            profile.model === "changed-deployment"
              ? {
                  valid: false,
                  issues: [
                    {
                      path: ["options", "request_timeout"],
                      message:
                        "A model call timeout cannot exceed the total AI step timeout",
                    },
                  ],
                }
              : { valid: true, issues: [] },
        });
      });
      await form
        .getByRole("button", {
          name: "Configure workflow AI profiles",
          exact: true,
        })
        .click();
      await form
        .getByLabel("Model", { exact: true })
        .fill("changed-deployment");
      await expect(
        form.getByText(
          "A model call timeout cannot exceed the total AI step timeout",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        form.getByRole("button", {
          name: "Continue to connection",
          exact: true,
        }),
      ).toBeDisabled();
      await form
        .getByRole("button", { name: "Cancel profile changes", exact: true })
        .click();
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
